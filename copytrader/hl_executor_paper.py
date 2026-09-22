"""Paper executor for Hyperliquid: rebalance our book toward the cohort target,
simulating fills at the live mark price (+ modeled slippage + HL taker fee).

Rebalancing (not event-copying) means opens, adds, trims, closes and flips all
fall out of one rule: move each coin's notional toward its target, but only
when the gap is bigger than a band (avoids churn on tiny wiggles) and above the
exchange minimum order size."""
from __future__ import annotations

import logging
import time

from .config import AppCfg
from .ledger import ClosedTrade, Ledger
from .hl_models import TargetPosition
from .hl_risk import HLRiskManager
from .models import Fill, Token

log = logging.getLogger("copytrader.hl.paper")


class HLPaperExecutor:
    mode = "paper"

    def __init__(self, cfg: AppCfg, risk: HLRiskManager, ledger: Ledger, info):
        self.cfg = cfg
        self.hl = cfg.hyperliquid
        self.risk = risk
        self.ledger = ledger
        self.info = info

    def rebalance(self, targets: dict, mids: dict) -> None:
        budget = self.risk.sizing_base(mids)
        band = max(self.hl.min_order_usd, self.hl.rebalance_band_pct * budget)
        halted = self.risk.halted(mids)
        if halted:
            log.warning("daily loss halt active — only reductions allowed")
        coins = set(targets) | self.risk.held_coins()
        for coin in sorted(coins):
            mark = mids.get(coin)
            if not mark or mark <= 0:
                continue
            target = targets.get(coin)
            tnot = target.notional if target else 0.0
            pos = self.risk.positions.get(coin)
            cur = pos.szi * mark if pos else 0.0
            delta_not = tnot - cur
            # while halted/killed, only allow trades that shrink exposure
            if halted and abs(tnot) > abs(cur):
                continue
            # while a coin is cooling down after a stop, don't grow it back
            if self.risk.in_cooldown(coin) and abs(tnot) > abs(cur):
                continue
            if abs(delta_not) < band:
                continue
            self._execute(coin, delta_not, mark, target, pos)

    def flatten(self, coin: str, mids: dict, reason: str,
                penalty: float = 0.0) -> None:
        """Close a coin's whole position at market (for stop-loss / kill /
        liquidation). `penalty` adds extra slippage (forced exits fill worse)."""
        pos = self.risk.positions.get(coin)
        if not pos:
            return
        mark = mids.get(coin) or pos.entry_px
        self._execute(coin, -pos.szi * mark, mark, None, pos, penalty)
        log.warning("FLATTEN %s (%s)", coin, reason)

    def _book_slippage(self, coin, notional, is_buy, mark) -> float:
        """Realistic slippage by walking the live order book for this size."""
        if mark <= 0:
            return self.hl.slippage_pct
        size = abs(notional) / mark
        try:
            book = self.info.l2_book(coin)
            levels = book["levels"][1 if is_buy else 0]  # asks buy / bids sell
            remaining, cost = size, 0.0
            for lvl in levels:
                px, sz = float(lvl["px"]), float(lvl["sz"])
                take = min(remaining, sz)
                cost += take * px
                remaining -= take
                if remaining <= 0:
                    break
            if remaining > 0:            # not enough depth: last px + hard hit
                last = float(levels[-1]["px"]) if levels else mark
                cost += remaining * last * (1.03 if is_buy else 0.97)
            avg = cost / size
            slip = (avg - mark) / mark if is_buy else (mark - avg) / mark
            return max(0.0, slip) + self.hl.slippage_pct  # + tiny latency slip
        except Exception:
            return self.hl.slippage_pct

    def _execute(self, coin, delta_not, mark, target, pos, penalty=0.0) -> None:
        side = "buy" if delta_not > 0 else "sell"
        delta_szi = delta_not / mark
        slip = self._book_slippage(coin, delta_not, delta_not > 0, mark) + penalty
        fill_px = mark * (1 + slip) if delta_not > 0 else mark * (1 - slip)
        fee = abs(delta_not) * self.hl.taker_fee_pct
        wallets = (target.wallets if target and target.notional != 0
                   else (pos.source_wallets if pos else []))

        res = self.risk.apply_fill(coin, delta_szi, fill_px, fee, wallets)

        tok = Token(address=coin, chain="hyperliquid", symbol=coin, decimals=0)
        self.ledger.record_fill(Fill(
            ts=time.time(), chain="hyperliquid", side=side, token=tok,
            qty_token=abs(delta_szi), price_usd=fill_px,
            gross_usd=abs(delta_not), fee_usd=fee, slippage_pct=slip,
            latency_s=0.0, source_wallet=",".join(wallets),
            note=f"target={target.notional:.1f}" if target else "flatten"),
            self.mode)

        if res["closed_units"] > 0:
            units = res["closed_units"]
            entry = res["entry"]
            pnl = res["realized"] - fee
            reason = "close" if res["closed"] else "reduce"
            self.ledger.record_trade(ClosedTrade(
                chain="hyperliquid", token=coin, token_symbol=coin,
                source_wallets=",".join(wallets),
                open_ts=(pos.opened_ts if pos else time.time()),
                close_ts=time.time(), qty_token=units,
                entry_price_usd=entry, exit_price_usd=fill_px,
                cost_usd=units * entry, proceeds_usd=units * fill_px,
                fees_usd=fee, pnl_usd=pnl,
                pnl_pct=(pnl / (units * entry)) if entry else 0.0,
                exit_reason=reason, mode=self.mode))
        log.info("%s %s %.4g @ $%.4g (Δntl $%+.1f)%s", side.upper(), coin,
                 abs(delta_szi), fill_px, delta_not,
                 "  [CLOSE]" if res["closed"] else "")
