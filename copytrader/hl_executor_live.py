"""Live executor for Hyperliquid — REAL money, gated exactly like the spot one.

Sends real orders ONLY when:  [hl_live].enabled = true  AND  COPYTRADER_ARM=1
AND  [hl_live].dry_run_first = false.  Otherwise it runs the paper simulation
tagged "live-dryrun" and sends nothing.

The private key is read once from the env var named by [hl_live].private_key_env
and never logged/stored. Orders go through the official hyperliquid SDK
(imported lazily), and we update our book from the ACTUAL fill (avgPx/totalSz)
returned by the exchange, so accounting matches reality.

This path has not been run with real funds by the author — validate with tiny
sizes first."""
from __future__ import annotations

import logging
import os
import time

from .config import AppCfg
from .hl_executor_paper import HLPaperExecutor
from .hl_risk import HLRiskManager
from .ledger import ClosedTrade, Ledger
from .models import Fill, Token

log = logging.getLogger("copytrader.hl.live")


class HLLiveExecutor(HLPaperExecutor):
    mode = "live"

    def __init__(self, cfg: AppCfg, risk: HLRiskManager, ledger: Ledger, info):
        super().__init__(cfg, risk, ledger, info)
        self.hllive = cfg.hl_live
        self.armed = bool(self.hllive.enabled
                          and os.environ.get("COPYTRADER_ARM") == "1"
                          and not self.hllive.dry_run_first)
        self._ex = None
        self._sz_decimals: dict[str, int] = {}
        if self.armed:
            self._init_exchange()
        self.mode = "live" if self.armed else "live-dryrun"
        log.warning("HLLiveExecutor: armed=%s mode=%s", self.armed, self.mode)

    def _init_exchange(self) -> None:
        pk = os.environ.get(self.hllive.private_key_env)
        if not pk:
            raise SystemExit(f"HL live armed but {self.hllive.private_key_env} "
                             "env var is empty")
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
        except ImportError:
            raise SystemExit("HL live needs: pip install hyperliquid-python-sdk "
                             "eth-account")
        wallet = Account.from_key(pk)
        pk = None
        # If a trade-only API/agent wallet is used, orders act on the main
        # account_address (the agent key alone cannot withdraw funds).
        acct_addr = (self.hllive.account_address or "").strip() or None
        self._ex = Exchange(wallet, constants.MAINNET_API_URL,
                            account_address=acct_addr)
        try:
            universe, _ = self.info.meta_and_ctxs()
            self._sz_decimals = {u["name"]: int(u.get("szDecimals", 4))
                                 for u in universe}
        except Exception as exc:
            log.warning("could not load szDecimals: %s", exc)
        log.warning("HL live: signer %s | account %s", wallet.address,
                    acct_addr or wallet.address)

    def _round_sz(self, coin: str, sz: float) -> float:
        dec = self._sz_decimals.get(coin, 4)
        return round(sz, dec)

    # -- override the fill step -------------------------------------------
    def _execute(self, coin, delta_not, mark, target, pos, penalty=0.0) -> None:
        if not self.armed:
            return super()._execute(coin, delta_not, mark, target, pos, penalty)
        try:
            self._execute_real(coin, delta_not, mark, target, pos, penalty)
        except Exception as exc:
            log.error("HL LIVE order failed for %s: %s", coin, exc)

    def _execute_real(self, coin, delta_not, mark, target, pos,
                      penalty=0.0) -> None:
        is_buy = delta_not > 0
        sz = self._round_sz(coin, abs(delta_not) / mark)
        if sz <= 0:
            log.info("skip %s: size rounds to 0", coin)
            return
        wallets = (target.wallets if target and target.notional != 0
                   else (pos.source_wallets if pos else []))
        slippage = self.hllive.max_slippage_pct + penalty
        resp = self._ex.market_open(coin, is_buy, sz, None, slippage)
        filled = self._parse_fill(resp)
        if not filled:
            log.warning("HL order for %s not filled: %s", coin, resp)
            return
        total_sz, avg_px = filled
        delta_szi = total_sz * (1 if is_buy else -1)
        fee = total_sz * avg_px * self.hl.taker_fee_pct
        res = self.risk.apply_fill(coin, delta_szi, avg_px, fee, wallets)

        tok = Token(address=coin, chain="hyperliquid", symbol=coin, decimals=0)
        self.ledger.record_fill(Fill(
            ts=time.time(), chain="hyperliquid",
            side="buy" if is_buy else "sell", token=tok, qty_token=total_sz,
            price_usd=avg_px, gross_usd=total_sz * avg_px, fee_usd=fee,
            slippage_pct=0.0, latency_s=0.0, source_wallet=",".join(wallets),
            tx_hash="HL", note="LIVE"), self.mode)
        if res["closed_units"] > 0:
            units, entry = res["closed_units"], res["entry"]
            pnl = res["realized"] - fee
            self.ledger.record_trade(ClosedTrade(
                chain="hyperliquid", token=coin, token_symbol=coin,
                source_wallets=",".join(wallets),
                open_ts=(pos.opened_ts if pos else time.time()),
                close_ts=time.time(), qty_token=units, entry_price_usd=entry,
                exit_price_usd=avg_px, cost_usd=units * entry,
                proceeds_usd=units * avg_px, fees_usd=fee, pnl_usd=pnl,
                pnl_pct=(pnl / (units * entry)) if entry else 0.0,
                exit_reason="close" if res["closed"] else "reduce",
                mode=self.mode))
        log.warning("HL LIVE %s %s %.4g @ $%.4g", "BUY" if is_buy else "SELL",
                    coin, total_sz, avg_px)

    @staticmethod
    def _parse_fill(resp: dict):
        try:
            statuses = resp["response"]["data"]["statuses"]
            for s in statuses:
                if "filled" in s:
                    f = s["filled"]
                    return float(f["totalSz"]), float(f["avgPx"])
                if "error" in s:
                    log.warning("HL order status error: %s", s["error"])
            return None
        except (KeyError, TypeError, ValueError):
            return None
