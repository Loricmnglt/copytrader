"""Live executor — REAL money. Disabled by default and heavily gated.

SAFETY MODEL (read this):
  * It sends real transactions ONLY if ALL of these hold:
      - config `[live].enabled = true`
      - environment variable  COPYTRADER_ARM=1
      - config `[live].dry_run_first = false`
    If any is missing it behaves exactly like paper (simulated fills tagged
    "live-dryrun"), never sending a transaction.
  * The private key is read ONCE, here, from the env var named by
    `[live].private_key_env` (default COPYTRADER_PK). It is never written to
    the config, the DB, the CSVs, or any log line.
  * It trades only on the single chain in `[live].chain` (default arbitrum).
  * Base currency is WETH on both legs, so every fill is reconstructed EXACTLY
    from on-chain Transfer logs (no guessing). Fund the wallet with WETH for
    trading + a little native ETH for gas.

The quote + dry-run path and the receipt-accounting are exercised by tests; the
end-to-end armed send has NOT been run with real funds by the author. Validate
with a few CHF and tiny caps first. web3/eth-account are imported lazily so
paper mode needs no extra install."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

from . import abi
from .chains import get_chain
from .config import AppCfg
from .executor_paper import PaperExecutor
from .ledger import ClosedTrade, Ledger
from .models import Fill, Position, Signal
from .prices import PriceFeed
from .rpc import RpcClient
from .risk import RiskManager

log = logging.getLogger("copytrader.live")

ODOS_QUOTE = "https://api.odos.xyz/sor/quote/v2"
ODOS_ASSEMBLE = "https://api.odos.xyz/sor/assemble"


def _post_json(url: str, payload: dict, timeout: float = 20.0, retries: int = 3):
    data = json.dumps(payload).encode()
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"content-type": "application/json",
                         "accept": "application/json",
                         "user-agent": "Mozilla/5.0 copytrader/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # transient 5xx / network
            last = exc
            time.sleep(1.0 * (i + 1))
    raise last


# ---------------------------------------------------------------------------
# pure helpers (no web3) — unit-tested against real receipts
# ---------------------------------------------------------------------------
def parse_incoming_amount(receipt: dict, token_addr: str, wallet: str) -> int:
    """Sum ERC-20 Transfer amounts of `token_addr` sent TO `wallet` in a raw
    JSON-RPC receipt. This is how we learn the EXACT amount a swap produced."""
    token_addr = token_addr.lower()
    wallet = wallet.lower()
    total = 0
    for lg in receipt.get("logs", []):
        if lg.get("address", "").lower() != token_addr:
            continue
        topics = lg.get("topics") or []
        if len(topics) < 3 or topics[0].lower() != abi.TRANSFER_TOPIC:
            continue
        if abi.topic_to_addr(topics[1]) == wallet:  # skip our own sends
            continue
        if abi.topic_to_addr(topics[2]) == wallet:
            total += abi.decode_uint(lg.get("data", "0x"))
    return total


def gas_spent_native(receipt: dict) -> float:
    try:
        used = int(receipt["gasUsed"], 16)
        price = receipt.get("effectiveGasPrice")
        gp = int(price, 16) if price else 0
        return used * gp / 1e18
    except (TypeError, KeyError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
class LiveExecutor(PaperExecutor):
    mode = "live"

    def __init__(self, cfg: AppCfg, risk: RiskManager, ledger: Ledger,
                 price_feed: PriceFeed):
        super().__init__(cfg, risk, ledger, price_feed)
        self.live = cfg.live
        self.chain = get_chain(self.live.chain)
        self.armed = bool(self.live.enabled
                          and os.environ.get("COPYTRADER_ARM") == "1"
                          and not self.live.dry_run_first)
        self._acct = None
        self._w3 = None
        self._rpc: RpcClient | None = None
        if self.armed:
            self._init_wallet()
        self.mode = "live" if self.armed else "live-dryrun"
        log.warning("LiveExecutor: armed=%s mode=%s chain=%s "
                    "(enabled=%s arm=%s dry_run_first=%s)",
                    self.armed, self.mode, self.live.chain, self.live.enabled,
                    os.environ.get("COPYTRADER_ARM") == "1",
                    self.live.dry_run_first)

    # -- wallet ------------------------------------------------------------
    def _init_wallet(self) -> None:
        pk = os.environ.get(self.live.private_key_env)
        if not pk:
            raise SystemExit(
                f"live armed but {self.live.private_key_env} env var is empty")
        try:
            from eth_account import Account
            from web3 import Web3
        except ImportError:
            raise SystemExit("live mode needs: pip install web3 eth-account")
        endpoints = self._endpoints()
        self._w3 = Web3(Web3.HTTPProvider(endpoints[0]))
        self._rpc = RpcClient(endpoints)
        self._acct = Account.from_key(pk)
        pk = None  # drop the raw key reference immediately
        bal_native = self._w3.eth.get_balance(self._acct.address) / 1e18
        log.warning("live wallet %s ready on %s (native bal %.5f)",
                    self._acct.address, self.live.chain, bal_native)

    def _endpoints(self) -> list[str]:
        from .config import resolve_rpcs
        return resolve_rpcs(self.cfg.rpc_overrides, self.live.chain)

    # -- overrides ---------------------------------------------------------
    def open(self, signal: Signal) -> bool:
        if signal.chain != self.live.chain:
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason=f"live-chain!={signal.chain}")
            return False
        if not self.armed:
            return super().open(signal)     # realistic paper sim (live-dryrun)
        try:
            return self._open_real(signal)
        except Exception as exc:
            log.error("LIVE open failed for %s: %s", signal.token.symbol, exc)
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason=f"live-error:{exc}")
            return False

    def close(self, pos: Position, reason: str) -> bool:
        if not self.armed:
            return super().close(pos, reason)
        try:
            return self._close_real(pos, reason)
        except Exception as exc:
            log.error("LIVE close failed for %s: %s — POSITION STILL OPEN "
                      "ON-CHAIN, handle manually", pos.token.symbol, exc)
            return False

    # -- real BUY (WETH -> token) -----------------------------------------
    def _open_real(self, signal: Signal) -> bool:
        ok, why = self.risk.can_open(signal.token.address)
        if not ok:
            self.ledger.record_signal(signal, acted=False, skip_reason=why)
            return False
        if not self._gas_ok():
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason="gas-too-high")
            return False

        native_usd = self._native_usd()
        if native_usd <= 0:
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason="no-native-price")
            return False
        notional = self.risk.size_for(signal)
        weth = self.chain.wrapped_native
        raw_in = int((notional / native_usd) * 1e18)

        have = self._erc20_balance(weth)
        if have < raw_in:
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason=f"insufficient WETH "
                                      f"({have/1e18:.5f})")
            return False

        quote = self._odos_quote(weth, signal.token.address, raw_in)
        if not quote or "pathId" not in quote:
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason="odos-quote-failed")
            return False
        tx = self._odos_assemble(quote["pathId"])
        spender = tx["to"]
        gas_approve = self._ensure_allowance(weth, spender, raw_in)
        receipt = self._send(tx)
        if receipt.get("status") not in ("0x1", 1):
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason="buy-tx-reverted")
            return False

        qty_raw = parse_incoming_amount(receipt, signal.token.address,
                                        self._acct.address)
        if qty_raw <= 0:
            log.error("buy landed but no %s received?? tx=%s",
                      signal.token.symbol, receipt.get("transactionHash"))
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason="buy-no-output")
            return False
        qty = qty_raw / (10 ** signal.token.decimals)
        gas_native = gas_spent_native(receipt) + gas_approve
        cost_usd = (raw_in / 1e18 + gas_native) * native_usd

        pos = self.risk.open_position(signal, entry_price_usd=cost_usd / qty,
                                      qty_token=qty, cost_usd=cost_usd)
        fill = Fill(ts=time.time(), chain=signal.chain, side="buy",
                    token=signal.token, qty_token=qty,
                    price_usd=(raw_in / 1e18 * native_usd) / qty,
                    gross_usd=cost_usd, fee_usd=gas_native * native_usd,
                    slippage_pct=0.0, latency_s=self._latency(signal),
                    source_wallet=signal.source_trade.wallet,
                    tx_hash=receipt.get("transactionHash", ""),
                    note="LIVE buy")
        self.ledger.record_fill(fill, self.mode)
        self.ledger.record_signal(signal, acted=True)
        return True

    # -- real SELL (token -> WETH) ----------------------------------------
    def _close_real(self, pos: Position, reason: str) -> bool:
        weth = self.chain.wrapped_native
        token = pos.token.address
        native_usd = self._native_usd()
        bal_raw = self._erc20_balance(token)   # sell the FULL on-chain balance
        if bal_raw <= 0:
            log.warning("close %s: zero on-chain balance, dropping position",
                        pos.token.symbol)
            self.risk.close_position(pos, 0.0)
            return True

        quote = self._odos_quote(token, weth, bal_raw)
        if not quote or "pathId" not in quote:
            log.warning("odos quote failed for close %s; will retry",
                        pos.token.symbol)
            return False
        tx = self._odos_assemble(quote["pathId"])
        spender = tx["to"]
        gas_approve = self._ensure_allowance(token, spender, bal_raw)
        receipt = self._send(tx)
        if receipt.get("status") not in ("0x1", 1):
            log.warning("sell tx reverted for %s; will retry", pos.token.symbol)
            return False

        weth_raw = parse_incoming_amount(receipt, weth, self._acct.address)
        gas_native = gas_spent_native(receipt) + gas_approve
        proceeds_usd = max(0.0, (weth_raw / 1e18 - gas_native) * native_usd)

        self.risk.close_position(pos, proceeds_usd)
        pnl = proceeds_usd - pos.cost_usd
        self.ledger.record_trade(ClosedTrade(
            chain=pos.chain, token=token, token_symbol=pos.token.symbol,
            source_wallets=",".join(pos.source_wallets),
            open_ts=pos.opened_ts, close_ts=time.time(),
            qty_token=pos.qty_token, entry_price_usd=pos.entry_price_usd,
            exit_price_usd=(proceeds_usd / pos.qty_token) if pos.qty_token else 0,
            cost_usd=pos.cost_usd, proceeds_usd=proceeds_usd,
            fees_usd=gas_native * native_usd, pnl_usd=pnl,
            pnl_pct=(pnl / pos.cost_usd) if pos.cost_usd else 0.0,
            exit_reason=reason, mode=self.mode))
        self.ledger.record_fill(Fill(
            ts=time.time(), chain=pos.chain, side="sell", token=pos.token,
            qty_token=pos.qty_token, price_usd=native_usd * weth_raw / 1e18
            / pos.qty_token if pos.qty_token else 0,
            gross_usd=proceeds_usd, fee_usd=gas_native * native_usd,
            slippage_pct=0.0, latency_s=0.0,
            source_wallet=",".join(pos.source_wallets),
            tx_hash=receipt.get("transactionHash", ""), note=reason),
            self.mode)
        return True

    # -- erc20 / tx helpers ------------------------------------------------
    def _erc20_balance(self, token: str) -> int:
        data = abi.encode_balance_of(self._acct.address)
        return abi.decode_uint(self._rpc.eth_call(token, data))

    def _allowance(self, token: str, spender: str) -> int:
        data = abi.encode_allowance(self._acct.address, spender)
        return abi.decode_uint(self._rpc.eth_call(token, data))

    def _ensure_allowance(self, token: str, spender: str,
                          needed: int) -> float:
        """Approve `spender` for `token` if the allowance is short. Returns the
        native gas spent on the approval (0 if none was needed)."""
        if self._allowance(token, spender) >= needed:
            return 0.0
        log.warning("approving %s for router %s", token[:10], spender[:10])
        tx = {"to": token, "data": "0x" + abi.encode_approve(spender),
              "value": 0}
        receipt = self._send(tx)
        return gas_spent_native(receipt)

    def _send(self, tx: dict) -> dict:
        """Fill, sign, send a tx; wait for the mined receipt (raw JSON)."""
        w3 = self._w3
        addr = self._acct.address
        tx = dict(tx)
        # normalize hex-string numeric fields from odos
        for k in ("value", "gas", "gasPrice", "maxFeePerGas",
                  "maxPriorityFeePerGas", "nonce", "chainId"):
            if isinstance(tx.get(k), str):
                tx[k] = int(tx[k], 0)
        tx.setdefault("chainId", self.chain.chain_id)
        tx["nonce"] = w3.eth.get_transaction_count(addr)
        tx.setdefault("from", addr)
        if "gas" not in tx:
            try:
                tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.25)
            except Exception:
                tx["gas"] = 800_000
        if "gasPrice" not in tx and "maxFeePerGas" not in tx:
            tx["gasPrice"] = w3.eth.gas_price
        signed = self._acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        hexh = h.hex() if hasattr(h, "hex") else str(h)
        log.warning("LIVE tx sent: %s", hexh)
        w3.eth.wait_for_transaction_receipt(h, timeout=180)
        rcpt = self._rpc.get_receipt(hexh) or {}
        rcpt.setdefault("transactionHash", hexh)
        return rcpt

    # -- odos --------------------------------------------------------------
    def _odos_quote(self, token_in: str, token_out: str, amount_in_raw: int):
        payload = {
            "chainId": self.chain.chain_id,
            "inputTokens": [{"tokenAddress": self._cs(token_in),
                             "amount": str(amount_in_raw)}],
            "outputTokens": [{"tokenAddress": self._cs(token_out),
                              "proportion": 1}],
            "userAddr": self._cs(self._acct.address) if self._acct else
            "0x0000000000000000000000000000000000000000",
            "slippageLimitPercent": 1.0,
        }
        try:
            return _post_json(ODOS_QUOTE, payload)
        except Exception as exc:
            log.warning("odos quote failed: %s", exc)
            return None

    def _odos_assemble(self, path_id: str) -> dict:
        assembled = _post_json(ODOS_ASSEMBLE, {
            "userAddr": self._cs(self._acct.address),
            "pathId": path_id, "simulate": False})
        return assembled["transaction"]

    @staticmethod
    def _cs(addr: str) -> str:
        """Odos expects EIP-55 checksummed addresses; use web3 if available."""
        try:
            from web3 import Web3
            return Web3.to_checksum_address(addr)
        except Exception:
            return addr

    # -- misc --------------------------------------------------------------
    def _native_usd(self) -> float:
        md = self.prices.get(self.chain, self.chain.wrapped_native)
        return md.price_usd if md else 0.0

    def _gas_ok(self) -> bool:
        try:
            gp_gwei = self._w3.eth.gas_price / 1e9
            if gp_gwei > self.live.max_gas_price_gwei:
                log.warning("gas %.3f gwei > cap %.3f", gp_gwei,
                            self.live.max_gas_price_gwei)
                return False
            return True
        except Exception:
            return False
