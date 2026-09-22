"""Hyperliquid copy engine loop: poll the followed wallets' live state, turn it
into target exposures, and rebalance our (paper or live) book toward them."""
from __future__ import annotations

import json
import logging
import os
import signal as _signal
import time
from concurrent.futures import ThreadPoolExecutor

from .config import AppCfg
from .hl_api import HLInfo
from .hl_risk import HLRiskManager
from .hl_signals import HLSignalEngine
from .ledger import Ledger

log = logging.getLogger("copytrader.hl.engine")

FUNDING_TTL = 300.0  # refresh funding rates every 5 min


class HLEngine:
    def __init__(self, cfg: AppCfg, executor_factory, mode: str):
        self.cfg = cfg
        self.hl = cfg.hyperliquid
        self.mode = mode
        self._stop = False

        self.followed = [w.address for w in cfg.wallets if w.enabled]
        if not self.followed:
            raise SystemExit("no enabled wallets in config")

        self.info = HLInfo()
        self.ledger = Ledger(self.hl.db_path)
        self.risk = HLRiskManager(cfg)
        self.weights = self._load_weights()
        self.signals = HLSignalEngine(cfg, self.weights)
        self.executor = executor_factory(cfg, self.risk, self.ledger, self.info)

        self._pool = ThreadPoolExecutor(max_workers=max(1, len(self.followed)),
                                        thread_name_prefix="hlpoll")
        self._funding: dict = {}
        self._funding_ts = 0.0
        self._last_funding_accrual = time.time()
        self._last_equity_snap = 0.0
        out_dir = os.path.dirname(self.hl.db_path) or "."
        self.state_path = os.path.join(out_dir, f"hl_state_{mode}.json")
        self._load_state()

    # ---------------------------------------------------------------------
    def _load_weights(self) -> dict[str, float]:
        weights = {w.address.lower(): w.weight for w in self.cfg.wallets
                   if w.enabled}
        path = os.path.join(os.path.dirname(self.hl.db_path) or ".",
                            "weights_hl.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    for a, v in json.load(fh).items():
                        if a.lower() in weights:
                            weights[a.lower()] = float(v)
                log.info("loaded HL analyzer weights")
            except Exception as exc:
                log.warning("weights_hl.json unreadable: %s", exc)
        return weights

    # ---------------------------------------------------------------------
    def run(self) -> None:
        self._install_signal_handlers()
        log.info("HL %s mode | budget $%.2f (%.0f %s) | wallets=%d | "
                 "max_gross_lev=%.1fx", self.mode.upper(), self.risk.budget_usd,
                 self.hl.budget, self.hl.quote_currency, len(self.followed),
                 self.hl.max_gross_leverage)
        try:
            while not self._stop:
                try:
                    self._tick()
                except Exception as exc:
                    log.error("tick error (continuing): %s", exc)
                time.sleep(self.hl.poll_interval_s)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def _tick(self) -> None:
        mids = self.info.all_mids()
        states = self._poll_states()
        if not states:
            return

        # funding rates (cached) + accrual since last tick
        now = time.time()
        if now - self._funding_ts > FUNDING_TTL:
            self._funding = self.info.funding_rates()
            self._funding_ts = now
        self.risk.accrue_funding(mids, self._funding,
                                 now - self._last_funding_accrual)
        self._last_funding_accrual = now

        base = self.risk.sizing_base(mids)
        targets = self.signals.targets(states, mids, base,
                                       self.info.max_leverages())
        self.executor.rebalance(targets, mids)

        # exchange liquidation (hard reality): equity hit maintenance margin
        if self.risk.liquidation_due(mids, self.info.max_leverages()):
            log.error("LIQUIDATION: equity hit maintenance margin — "
                      "forced close of the whole book")
            for coin in list(self.risk.positions):
                self.executor.flatten(coin, mids, "LIQUIDATION", penalty=0.01)
            self.risk.trip_kill()
        # our own account kill-switch: liquidate everything, halt for the day
        elif self.risk.kill_due(mids):
            for coin in list(self.risk.positions):
                self.executor.flatten(coin, mids, "kill-switch")
            self.risk.trip_kill()
        else:
            # per-position stop-losses (+ cooldown to avoid instant re-entry)
            for coin, pnl_pct in self.risk.stops_due(mids):
                self.executor.flatten(coin, mids, f"stop-loss {pnl_pct:+.1%}")
                self.risk.set_cooldown(coin)

        if now - self._last_equity_snap > 30:
            self._snapshot(mids)
            self.risk.roll_day_if_needed(mids)
            self._save_state()
            self._last_equity_snap = now

    def _poll_states(self) -> dict:
        futs = {self._pool.submit(self.info.clearinghouse_state, addr): addr
                for addr in self.followed}
        states = {}
        for fut, addr in futs.items():
            try:
                states[addr.lower()] = fut.result()
            except Exception as exc:
                log.warning("state poll failed %s: %s", addr[:10], exc)
        return states

    def _snapshot(self, mids: dict) -> None:
        unreal = sum(p.unrealized(mids.get(p.coin, p.entry_px))
                     for p in self.risk.positions.values())
        cash = (self.risk.budget_usd + self.risk.realized_price_pnl
                - self.risk.total_fees - self.risk.total_funding)
        self.ledger.record_equity(cash, unreal, cash - self.risk.budget_usd)

    # ---------------------------------------------------------------------
    def _load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                n = self.risk.restore(json.load(fh))
            log.info("restored HL state: %d position(s), realized $%+.2f",
                     n, self.risk.realized_price_pnl)
        except Exception as exc:
            log.warning("could not restore HL state: %s", exc)

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.risk.snapshot(), fh, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            log.warning("could not save HL state: %s", exc)

    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            self._stop = True
        try:
            _signal.signal(_signal.SIGINT, handler)
            _signal.signal(_signal.SIGTERM, handler)
        except (ValueError, AttributeError):
            pass

    def shutdown(self) -> None:
        try:
            mids = self.info.all_mids()
        except Exception:
            mids = {}
        eq = self.risk.equity(mids)
        s = self.ledger.summary()
        log.info("---- HL session summary (%s) ----", self.mode)
        log.info("equity $%.2f (budget $%.2f) | realized(price) $%+.2f | "
                 "fees $%.2f | funding $%.2f", eq, self.risk.budget_usd,
                 self.risk.realized_price_pnl, self.risk.total_fees,
                 self.risk.total_funding)
        log.info("open positions: %d | closed trade-legs: %d | win-rate %.0f%%",
                 len(self.risk.positions), s["closed_trades"],
                 s["win_rate"] * 100)
        self._save_state()
        paths = self.ledger.export_csv(self.hl.csv_dir)
        log.info("CSV exported: %s", ", ".join(paths))
        self._pool.shutdown(wait=False)
        self.ledger.close()
