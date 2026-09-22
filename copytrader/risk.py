"""Risk manager: capital allocation and position lifecycle rules.

All accounting is in USD internally; the CHF (or other) budget is converted at
`risk.quote_to_usd`. Everything here is pure bookkeeping + policy — it never
touches the network — so it is trivially unit-testable and identical between
paper and live modes.

Protections implemented:
  * dry powder reserve (never deploy the whole budget)
  * base allocation scaled by conviction
  * hard cap per single token and per single position
  * max number of concurrent positions
  * per-token cooldown after a close (avoid re-buying the same dump)
  * daily loss circuit-breaker (stop opening if the day is down too much)
  * TP / SL / trailing-stop / max-hold-time exits
"""
from __future__ import annotations

import logging
import time

from .config import AppCfg
from .models import Position, Signal, Token

log = logging.getLogger("copytrader.risk")


class RiskManager:
    def __init__(self, cfg: AppCfg):
        self.cfg = cfg
        self.r = cfg.risk
        self.budget_usd = self.r.budget * self.r.quote_to_usd
        self.cash_usd = self.budget_usd
        self.realized_pnl_usd = 0.0
        self.positions: dict[str, Position] = {}   # token_addr -> Position
        self.cooldowns: dict[str, float] = {}       # token_addr -> until ts
        self._next_id = 1
        self._day_start = time.time()
        self._day_start_equity = self.budget_usd

    # -- persistence -------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "cash_usd": self.cash_usd,
            "realized_pnl_usd": self.realized_pnl_usd,
            "next_id": self._next_id,
            "cooldowns": self.cooldowns,
            "day_start": self._day_start,
            "day_start_equity": self._day_start_equity,
            "positions": [p.to_dict() for p in self.positions.values()],
        }

    def restore(self, state: dict) -> int:
        self.cash_usd = state.get("cash_usd", self.cash_usd)
        self.realized_pnl_usd = state.get("realized_pnl_usd", 0.0)
        self._next_id = state.get("next_id", 1)
        self.cooldowns = {k: float(v)
                          for k, v in (state.get("cooldowns") or {}).items()}
        self._day_start = state.get("day_start", self._day_start)
        self._day_start_equity = state.get("day_start_equity",
                                           self._day_start_equity)
        self.positions = {}
        for pd in state.get("positions", []):
            p = Position.from_dict(pd)
            self.positions[p.token.address.lower()] = p
        return len(self.positions)

    # -- queries -----------------------------------------------------------
    def held_token_addrs(self) -> set[str]:
        return set(self.positions.keys())

    def deployable_cash(self) -> float:
        return max(0.0, self.cash_usd - self.reserve_usd())

    def reserve_usd(self) -> float:
        return self.budget_usd * self.r.reserve_pct

    def equity_usd(self, price_lookup) -> float:
        val = self.cash_usd
        for pos in self.positions.values():
            px = price_lookup(pos) or pos.entry_price_usd
            val += pos.qty_token * px
        return val

    # -- open gating -------------------------------------------------------
    def can_open(self, token_addr: str) -> tuple[bool, str]:
        token_addr = token_addr.lower()
        now = time.time()
        if self._daily_halt_active():
            return False, "daily-loss-halt"
        if token_addr in self.positions:
            return False, "already-holding"
        cd = self.cooldowns.get(token_addr, 0)
        if cd > now:
            return False, f"cooldown {int((cd - now)/60)}min"
        if len(self.positions) >= self.r.max_concurrent:
            return False, f"max concurrent {self.r.max_concurrent}"
        if self.deployable_cash() <= 1.0:
            return False, "no deployable cash"
        return True, ""

    def size_for(self, signal: Signal) -> float:
        """USD notional to deploy for this new position."""
        base = self.budget_usd * self.r.base_alloc_pct
        sized = base * (0.5 + signal.conviction)  # conviction 0->0.5x, 1->1.5x
        pos_cap = self.budget_usd * self.r.max_position_pct
        tok_cap = self.budget_usd * self.r.max_per_token_pct
        sized = min(sized, pos_cap, tok_cap, self.deployable_cash())
        return max(0.0, sized)

    # -- lifecycle ---------------------------------------------------------
    def open_position(self, signal: Signal, entry_price_usd: float,
                      qty_token: float, cost_usd: float) -> Position:
        pos = Position(
            id=self._next_id,
            chain=signal.chain,
            token=signal.token,
            opened_ts=time.time(),
            entry_price_usd=entry_price_usd,
            entry_price_base=signal.source_trade.price_in_base,
            qty_token=qty_token,
            cost_usd=cost_usd,
            source_wallets=list(dict.fromkeys(
                [signal.source_trade.wallet] + signal.contributing_wallets)),
            high_price_usd=entry_price_usd,
            take_profit_usd=entry_price_usd * (1 + self.r.take_profit_pct),
            stop_loss_usd=entry_price_usd * (1 - self.r.stop_loss_pct),
        )
        self._next_id += 1
        self.positions[pos.token.address.lower()] = pos
        self.cash_usd -= cost_usd
        log.info("OPEN #%d %s %.4g tok @ $%.6g cost $%.2f (cash $%.2f)",
                 pos.id, pos.token.symbol, qty_token, entry_price_usd,
                 cost_usd, self.cash_usd)
        return pos

    def close_position(self, pos: Position, proceeds_usd: float) -> None:
        self.cash_usd += proceeds_usd
        pnl = proceeds_usd - pos.cost_usd
        self.realized_pnl_usd += pnl
        pos.status = "closed"
        self.positions.pop(pos.token.address.lower(), None)
        self.cooldowns[pos.token.address.lower()] = (
            time.time() + self.r.cooldown_minutes * 60)
        log.info("CLOSE #%d %s proceeds $%.2f pnl $%+.2f (cash $%.2f)",
                 pos.id, pos.token.symbol, proceeds_usd, pnl, self.cash_usd)

    # -- exit rules --------------------------------------------------------
    def exits_due(self, price_lookup) -> list[tuple[Position, str]]:
        due = []
        now = time.time()
        for pos in list(self.positions.values()):
            px = price_lookup(pos)
            if px is None or px <= 0:
                continue
            pos.high_price_usd = max(pos.high_price_usd, px)
            if px >= pos.take_profit_usd:
                due.append((pos, "take-profit"))
            elif px <= pos.stop_loss_usd:
                due.append((pos, "stop-loss"))
            elif (self.r.trailing_stop_pct > 0 and pos.high_price_usd > 0
                  and px <= pos.high_price_usd * (1 - self.r.trailing_stop_pct)
                  and px > pos.entry_price_usd):
                due.append((pos, "trailing-stop"))
            elif (now - pos.opened_ts) >= self.r.max_hold_minutes * 60:
                due.append((pos, "max-hold-time"))
        return due

    # -- daily circuit breaker --------------------------------------------
    def roll_day_if_needed(self, price_lookup) -> None:
        if time.time() - self._day_start >= 86400:
            self._day_start = time.time()
            self._day_start_equity = self.equity_usd(price_lookup)

    def _daily_halt_active(self) -> bool:
        # compare realized+unrealized against the day's start equity
        if self.r.daily_loss_halt_pct <= 0:
            return False
        # use realized pnl since day start as a cheap proxy (no price lookup)
        loss_limit = self._day_start_equity * self.r.daily_loss_halt_pct
        return (self.realized_pnl_usd) <= -loss_limit
