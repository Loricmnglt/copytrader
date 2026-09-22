"""Perp position book + risk for the Hyperliquid copy engine.

Accounting is pure bookkeeping (no network), identical between paper and live.
Equity = budget + realized price P&L - fees - funding + Σ unrealized(mark).
Gross exposure is capped in the signal layer; here we track the book, realize
P&L on reductions/flips, accrue funding, and run a daily loss circuit-breaker.
"""
from __future__ import annotations

import logging
import time

from .config import AppCfg
from .hl_models import OurPosition

log = logging.getLogger("copytrader.hl.risk")


class HLRiskManager:
    def __init__(self, cfg: AppCfg):
        self.hl = cfg.hyperliquid
        self.budget_usd = self.hl.budget * self.hl.quote_to_usd
        self.realized_price_pnl = 0.0
        self.total_fees = 0.0
        self.total_funding = 0.0
        self.positions: dict[str, OurPosition] = {}
        self.cooldowns: dict[str, float] = {}   # coin -> re-entry allowed after
        self.killed = False                      # account kill-switch tripped
        self._day_start = time.time()
        self._day_start_equity = self.budget_usd

    # -- valuation ---------------------------------------------------------
    def equity(self, mids: dict) -> float:
        unreal = 0.0
        for p in self.positions.values():
            mark = mids.get(p.coin, p.entry_px)
            unreal += p.unrealized(mark)
        return (self.budget_usd + self.realized_price_pnl - self.total_fees
                - self.total_funding + unreal)

    def gross_notional(self, mids: dict) -> float:
        return sum(abs(p.szi * mids.get(p.coin, p.entry_px))
                   for p in self.positions.values())

    def sizing_base(self, mids: dict) -> float:
        """Capital that leverage/sizing is measured against: current equity
        (constant leverage + compounding) or the fixed budget (clean test)."""
        if self.hl.size_on_equity:
            return max(0.0, self.equity(mids))
        return self.budget_usd

    def held_coins(self) -> set[str]:
        return set(self.positions.keys())

    # -- fills -------------------------------------------------------------
    def apply_fill(self, coin: str, delta_szi: float, px: float, fee: float,
                   source_wallets: list | None = None) -> dict:
        """Apply a signed size change at price px. Returns realized info for
        the (partial) close, if any."""
        self.total_fees += fee
        pos = self.positions.get(coin)
        if pos is None or pos.szi == 0:
            self.positions[coin] = OurPosition(
                coin=coin, szi=delta_szi, entry_px=px, opened_ts=time.time(),
                fees_paid=fee, source_wallets=list(source_wallets or []))
            return {"realized": 0.0, "closed_units": 0.0, "entry": px,
                    "closed": False, "opened": True}

        szi0, entry0 = pos.szi, pos.entry_px
        pos.fees_paid += fee
        same_dir = (szi0 > 0 and delta_szi > 0) or (szi0 < 0 and delta_szi < 0)
        if same_dir:
            new_szi = szi0 + delta_szi
            pos.entry_px = (szi0 * entry0 + delta_szi * px) / new_szi
            pos.szi = new_szi
            return {"realized": 0.0, "closed_units": 0.0, "entry": pos.entry_px,
                    "closed": False, "opened": False}

        # reduce or flip
        close_units = min(abs(delta_szi), abs(szi0))
        realized = close_units * (px - entry0) * (1.0 if szi0 > 0 else -1.0)
        self.realized_price_pnl += realized
        new_szi = szi0 + delta_szi
        closed = False
        if abs(delta_szi) <= abs(szi0):
            pos.szi = new_szi
            if abs(new_szi) < 1e-12:
                self.positions.pop(coin, None)
                closed = True
        else:                                   # flip through zero
            pos.szi = new_szi
            pos.entry_px = px
        return {"realized": realized, "closed_units": close_units,
                "entry": entry0, "closed": closed, "opened": False}

    # -- funding -----------------------------------------------------------
    def accrue_funding(self, mids: dict, funding: dict, dt_seconds: float) -> None:
        if not self.hl.funding_enabled or dt_seconds <= 0:
            return
        frac = dt_seconds / 3600.0            # funding rates are hourly
        for p in self.positions.values():
            rate = funding.get(p.coin)
            if not rate:
                continue
            mark = mids.get(p.coin, p.entry_px)
            cost = p.szi * mark * rate * frac  # long pays when rate>0
            p.funding_paid += cost
            self.total_funding += cost

    # -- daily circuit breaker --------------------------------------------
    def roll_day_if_needed(self, mids: dict) -> None:
        if time.time() - self._day_start >= 86400:
            self._day_start = time.time()
            self._day_start_equity = self.equity(mids)
            self.killed = False              # reset kill-switch each day

    def halted(self, mids: dict) -> bool:
        if self.killed:
            return True
        if self.hl.daily_loss_halt_pct <= 0:
            return False
        limit = self._day_start_equity * (1 - self.hl.daily_loss_halt_pct)
        return self.equity(mids) <= limit

    # -- per-trade stop-loss + account kill-switch ------------------------
    def stops_due(self, mids: dict) -> list[tuple[str, float]]:
        """Positions whose loss from entry exceeds stop_loss_pct."""
        sl = self.hl.stop_loss_pct
        if sl <= 0:
            return []
        out = []
        for p in self.positions.values():
            mark = mids.get(p.coin, p.entry_px)
            if p.entry_px <= 0:
                continue
            pnl_pct = (mark - p.entry_px) / p.entry_px * (1 if p.szi > 0 else -1)
            if pnl_pct <= -sl:
                out.append((p.coin, pnl_pct))
        return out

    def kill_due(self, mids: dict) -> bool:
        """True if equity has fallen past the account kill threshold."""
        k = self.hl.account_kill_pct
        if k <= 0 or self.killed:
            return False
        return self.equity(mids) <= self.budget_usd * (1 - k)

    def maintenance_margin(self, mids: dict, max_levs: dict) -> float:
        """Cross maintenance margin required to hold the book (HL ~ half the
        max-leverage initial margin, i.e. 1/(2*maxLev) of each notional)."""
        mm = 0.0
        for p in self.positions.values():
            lev = max_levs.get(p.coin, 3) or 3
            mm += abs(p.szi * mids.get(p.coin, p.entry_px)) / (2 * lev)
        return mm

    def liquidation_due(self, mids: dict, max_levs: dict) -> bool:
        """True if equity has fallen to the exchange maintenance margin — a
        real cross-margin account would be liquidated here."""
        if not self.positions:
            return False
        return self.equity(mids) <= self.maintenance_margin(mids, max_levs)

    def in_cooldown(self, coin: str) -> bool:
        return self.cooldowns.get(coin, 0) > time.time()

    def set_cooldown(self, coin: str) -> None:
        self.cooldowns[coin] = time.time() + self.hl.stop_cooldown_min * 60

    def trip_kill(self) -> None:
        self.killed = True
        log.warning("ACCOUNT KILL-SWITCH tripped — flattened + halted for the day")

    # -- persistence -------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "budget_usd": self.budget_usd,
            "realized_price_pnl": self.realized_price_pnl,
            "total_fees": self.total_fees,
            "total_funding": self.total_funding,
            "day_start": self._day_start,
            "day_start_equity": self._day_start_equity,
            "cooldowns": self.cooldowns,
            "killed": self.killed,
            "positions": [p.to_dict() for p in self.positions.values()],
        }

    def restore(self, state: dict) -> int:
        self.realized_price_pnl = state.get("realized_price_pnl", 0.0)
        self.total_fees = state.get("total_fees", 0.0)
        self.total_funding = state.get("total_funding", 0.0)
        self._day_start = state.get("day_start", self._day_start)
        self._day_start_equity = state.get("day_start_equity",
                                           self._day_start_equity)
        self.cooldowns = {k: float(v)
                          for k, v in (state.get("cooldowns") or {}).items()}
        self.killed = bool(state.get("killed", False))
        self.positions = {}
        for pd in state.get("positions", []):
            p = OurPosition.from_dict(pd)
            self.positions[p.coin] = p
        return len(self.positions)
