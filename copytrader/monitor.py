"""On-chain monitor.

Strategy (no router ABIs needed): for the followed wallets we pull ERC-20
`Transfer` logs where the wallet is the sender OR the receiver, group them by
transaction, and infer a swap from the legs:

    wallet RECEIVES token X  +  SENDS a base asset (WETH/USDC/native)  -> BUY X
    wallet SENDS   token X    +  RECEIVES a base asset                 -> SELL X

Base assets are the wrapped native + stablecoins configured per chain. The
native (unwrapped ETH) leg has no Transfer event, so we read it from the
transaction's `value`. Gas is read from the receipt.

This catches swaps on ANY dex/router/aggregator on the chain, because it only
looks at token movements, not at which contract was called."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from . import abi
from .chains import Chain, get_chain
from .config import resolve_rpcs
from .models import Token, WalletTrade
from .prices import TokenResolver
from .rpc import RpcClient

log = logging.getLogger("copytrader.monitor")

MAX_RANGE = 700  # cap getLogs span per call; chunk if we fell behind


class ChainMonitor:
    def __init__(self, chain_key: str, wallets: list[str],
                 rpc_overrides: dict | None = None,
                 lookback_blocks: int = 0,
                 ignore_patterns: list | None = None):
        self.chain: Chain = get_chain(chain_key)
        endpoints = resolve_rpcs(rpc_overrides, chain_key)
        self.rpc = RpcClient(endpoints)
        self.resolver = TokenResolver(self.rpc, chain_key)
        self.wallets = {w.lower() for w in wallets}
        self._wallet_topics = [abi.addr_to_topic(w) for w in self.wallets]
        self.last_block = 0
        self._lookback = lookback_blocks
        self._ignore_res = [re.compile(p, re.IGNORECASE)
                            for p in (ignore_patterns or [])]

    def start(self) -> None:
        latest = self.rpc.block_number()
        self.last_block = max(0, latest - self._lookback)
        log.info("[%s] monitor start at block %d (latest %d)",
                 self.chain.key, self.last_block, latest)

    # -- polling -----------------------------------------------------------
    def poll_once(self) -> list[WalletTrade]:
        latest = self.rpc.block_number()
        if latest <= self.last_block:
            return []
        trades: list[WalletTrade] = []
        frm = self.last_block + 1
        while frm <= latest:
            to = min(frm + MAX_RANGE - 1, latest)
            try:
                trades.extend(self._scan_range(frm, to))
            except Exception as exc:  # keep the loop alive on RPC hiccups
                log.warning("[%s] scan %d-%d failed: %s",
                            self.chain.key, frm, to, exc)
                break  # retry same range next poll
            self.last_block = to
            frm = to + 1
        return trades

    def _scan_range(self, frm: int, to: int) -> list[WalletTrade]:
        # OR-match the wallet set in one topic position per direction.
        out_logs = self.rpc.get_logs(frm, to,
                                     [abi.TRANSFER_TOPIC, self._wallet_topics])
        in_logs = self.rpc.get_logs(frm, to,
                                    [abi.TRANSFER_TOPIC, None,
                                     self._wallet_topics])
        logs = out_logs + in_logs
        if not logs:
            return []

        # group transfer logs by (tx, wallet)
        groups: dict[tuple[str, str], list[dict]] = {}
        blocks: set[int] = set()
        for lg in logs:
            topics = lg.get("topics") or []
            if len(topics) < 3:
                continue
            frm_addr = abi.topic_to_addr(topics[1])
            to_addr = abi.topic_to_addr(topics[2])
            wallet = frm_addr if frm_addr in self.wallets else (
                to_addr if to_addr in self.wallets else None)
            if wallet is None:
                continue
            key = (lg["transactionHash"], wallet)
            groups.setdefault(key, []).append(lg)
            blocks.add(int(lg["blockNumber"], 16))

        block_ts = self._block_timestamps(blocks)
        trades: list[WalletTrade] = []
        for (tx_hash, wallet), tx_logs in groups.items():
            trade = self._reconstruct(tx_hash, wallet, tx_logs, block_ts)
            if trade is not None:
                trades.append(trade)
        return trades

    # -- helpers -----------------------------------------------------------
    def _block_timestamps(self, blocks: Iterable[int]) -> dict[int, float]:
        blocks = list(blocks)
        if not blocks:
            return {}
        calls = [("eth_getBlockByNumber", [hex(b), False]) for b in blocks]
        try:
            res = self.rpc.batch(calls)
        except Exception:
            return {}
        out = {}
        for b, r in zip(blocks, res):
            try:
                out[b] = float(int(r["timestamp"], 16))
            except (TypeError, KeyError, ValueError):
                pass
        return out

    def _reconstruct(self, tx_hash: str, wallet: str, tx_logs: list[dict],
                     block_ts: dict[int, float]) -> WalletTrade | None:
        base_assets = self.chain.base_assets
        received: list[tuple[str, int]] = []  # to == wallet
        sent: list[tuple[str, int]] = []      # from == wallet
        for lg in tx_logs:
            token_addr = lg["address"].lower()
            topics = lg["topics"]
            frm_addr = abi.topic_to_addr(topics[1])
            to_addr = abi.topic_to_addr(topics[2])
            val = abi.decode_uint(lg.get("data", "0x"))
            if to_addr == wallet:
                received.append((token_addr, val))
            if frm_addr == wallet:
                sent.append((token_addr, val))

        token_recv = [(a, v) for a, v in received if a not in base_assets]
        base_recv = [(a, v) for a, v in received if a in base_assets]
        token_sent = [(a, v) for a, v in sent if a not in base_assets]
        base_sent = [(a, v) for a, v in sent if a in base_assets]

        # need tx (for native value/gas) and receipt (for gas paid)
        try:
            tx, receipt = self.rpc.batch([
                ("eth_getTransactionByHash", [tx_hash]),
                ("eth_getTransactionReceipt", [tx_hash]),
            ])
        except Exception:
            tx, receipt = None, None
        native_value = 0
        if tx and tx.get("value"):
            native_value = int(tx["value"], 16)
        gas_native = self._gas_native(tx, receipt)

        block = int(tx_logs[0]["blockNumber"], 16)
        ts = block_ts.get(block, 0.0)

        # ---- classify -----------------------------------------------------
        if token_recv and (base_sent or native_value > 0):
            token_addr, token_raw = max(token_recv, key=lambda x: x[1])
            side = "buy"
            base_sym, base_amt, base_native = self._base_leg(
                base_sent, native_value, "out")
        elif token_sent and (base_recv or native_value == 0):
            token_addr, token_raw = max(token_sent, key=lambda x: x[1])
            side = "sell"
            base_sym, base_amt, base_native = self._base_leg(
                base_recv, 0, "in")
        else:
            return None  # not a clean base<->token swap; ignore

        tok = self.resolver.resolve(token_addr)
        trade = WalletTrade(
            chain=self.chain.key,
            wallet=wallet,
            tx_hash=tx_hash,
            block=block,
            ts=ts,
            side=side,
            token=tok,
            token_amount=tok.amount(token_raw),
            base_symbol=base_sym,
            base_amount=base_amt,
            base_is_native=base_native,
            gas_native=gas_native,
        )
        if self._is_noise(trade):
            return None
        return trade

    def _is_noise(self, trade: WalletTrade) -> bool:
        """Drop non-memecoin 'swaps': lending aTokens, LP tokens, wrapping.
        Two rules: a symbol denylist, and a structural 1:1-vs-WETH check
        (an aToken/wrapped receipt trades ~1.0 against WETH; a memecoin never
        does)."""
        sym = trade.token.symbol or ""
        for rx in self._ignore_res:
            if rx.search(sym):
                log.debug("noise (symbol %s) tx=%s", sym, trade.tx_hash[:12])
                return True
        base = trade.base_symbol.upper()
        if base in ("WETH", "ETH", self.chain.native_symbol.upper()):
            p = trade.price_in_base
            if 0.95 <= p <= 1.05:          # ~1:1 with WETH => wrap/lending
                log.debug("noise (1:1 vs WETH) %s tx=%s", sym,
                          trade.tx_hash[:12])
                return True
        return False

    def _base_leg(self, base_legs: list[tuple[str, int]], native_value: int,
                  direction: str) -> tuple[str, float, bool]:
        if base_legs:
            addr, raw = max(base_legs, key=lambda x: x[1])
            btok = self.resolver.resolve(addr)
            return btok.symbol, btok.amount(raw), False
        if native_value > 0:
            return self.chain.native_symbol, native_value / 1e18, True
        # unknown (e.g. native proceeds via internal tx on a sell)
        return self.chain.native_symbol, 0.0, True

    def _gas_native(self, tx, receipt) -> float:
        if not receipt:
            return 0.0
        try:
            gas_used = int(receipt["gasUsed"], 16)
            price = receipt.get("effectiveGasPrice")
            if price is None and tx:
                price = tx.get("gasPrice")
            gp = int(price, 16) if price else 0
            return gas_used * gp / 1e18
        except (TypeError, KeyError, ValueError):
            return 0.0


class MultiChainMonitor:
    """Runs one ChainMonitor per configured chain and merges their output.
    Chains are polled CONCURRENTLY (one thread each) so total detection latency
    is the slowest single chain, not the sum across chains."""

    def __init__(self, chain_keys: list[str], wallets: list[str],
                 rpc_overrides: dict | None = None,
                 lookback_blocks: int = 0,
                 ignore_patterns: list | None = None):
        self.monitors = {
            k: ChainMonitor(k, wallets, rpc_overrides, lookback_blocks,
                            ignore_patterns)
            for k in chain_keys
        }
        self._pool = ThreadPoolExecutor(max_workers=max(1, len(chain_keys)),
                                        thread_name_prefix="poll")

    def start(self) -> None:
        for m in self.monitors.values():
            try:
                m.start()
            except Exception as exc:
                log.error("could not start monitor %s: %s", m.chain.key, exc)

    def poll(self) -> list[WalletTrade]:
        trades: list[WalletTrade] = []

        def _poll(m: ChainMonitor):
            return m.poll_once()

        futures = {self._pool.submit(_poll, m): m
                   for m in self.monitors.values()}
        for fut, m in futures.items():
            try:
                trades.extend(fut.result())
            except Exception as exc:
                log.warning("poll %s failed: %s", m.chain.key, exc)
        trades.sort(key=lambda t: (t.ts or t.detected_ts))
        return trades

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
