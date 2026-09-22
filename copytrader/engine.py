"""Engine: wires monitor -> signal engine -> risk -> executor -> ledger and
runs the poll/manage loop. Used by both `paper` and (later) `live` modes; the
only difference is which executor is injected."""
from __future__ import annotations

import json
import logging
import os
import signal as _signal
import time

from .config import AppCfg
from .ledger import Ledger
from .monitor import MultiChainMonitor
from .prices import HoneypotChecker, PriceFeed
from .risk import RiskManager
from .signals import SignalEngine

log = logging.getLogger("copytrader.engine")


class Engine:
    def __init__(self, cfg: AppCfg, executor_factory, mode: str,
                 lookback_blocks: int = 0):
        self.cfg = cfg
        self.mode = mode
        self._stop = False

        followed = [w.address for w in cfg.wallets if w.enabled]
        if not followed:
            raise SystemExit("no enabled wallets in config")

        self.ledger = Ledger(cfg.db_path)
        self.prices = PriceFeed()
        self.honeypot = HoneypotChecker(enabled=cfg.signal.honeypot_check,
                                        max_tax=cfg.signal.max_token_tax_pct)
        self.risk = RiskManager(cfg)
        self.monitor = MultiChainMonitor(
            cfg.chains, followed, cfg.rpc_overrides, lookback_blocks,
            ignore_patterns=cfg.signal.ignore_token_symbol_patterns)
        self.weights = self._load_weights()
        self.signals = SignalEngine(cfg, self.weights, self.prices,
                                    self.honeypot, self.risk.held_token_addrs)
        # executor is built last (needs risk/ledger/prices)
        self.executor = executor_factory(cfg, self.risk, self.ledger,
                                         self.prices)
        self._last_equity_snap = 0.0
        out_dir = os.path.dirname(cfg.db_path) or "."
        self.state_path = os.path.join(out_dir, f"state_{mode}.json")
        self._load_state()

    # ---------------------------------------------------------------------
    def _load_weights(self) -> dict[str, float]:
        """Prefer analyzer output (weights.json) if present, else config."""
        path = os.path.join(os.path.dirname(self.cfg.db_path) or ".",
                            "weights.json")
        weights = {w.address.lower(): w.weight for w in self.cfg.wallets
                   if w.enabled}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                for addr, val in data.items():
                    if addr.lower() in weights:
                        weights[addr.lower()] = float(val)
                log.info("loaded analyzer weights from %s", path)
            except Exception as exc:
                log.warning("could not read %s: %s", path, exc)
        return weights

    # ---------------------------------------------------------------------
    def run(self) -> None:
        self.monitor.start()
        self._install_signal_handlers()
        budget = self.risk.budget_usd
        log.info("%s mode | budget $%.2f (%.0f %s) | chains=%s | wallets=%d",
                 self.mode.upper(), budget, self.cfg.risk.budget,
                 self.cfg.risk.quote_currency, ",".join(self.cfg.chains),
                 len(self.weights))
        try:
            while not self._stop:
                try:
                    self._tick()
                except Exception as exc:      # never die on a transient error
                    log.error("tick error (continuing): %s", exc)
                time.sleep(self.cfg.poll_interval_s)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    # ---------------------------------------------------------------------
    def _load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                state = json.load(fh)
            n = self.risk.restore(state)
            log.info("restored state: %d open position(s), cash $%.2f, "
                     "realized $%+.2f", n, self.risk.cash_usd,
                     self.risk.realized_pnl_usd)
        except Exception as exc:
            log.warning("could not restore state (%s): %s", self.state_path,
                        exc)

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.risk.snapshot(), fh, indent=2)
            os.replace(tmp, self.state_path)   # atomic on the same volume
        except Exception as exc:
            log.warning("could not save state: %s", exc)

    def _tick(self) -> None:
        # 1) new wallet activity -> signals -> orders
        for trade in self.monitor.poll():
            sig, skip = self.signals.process(trade)
            if sig is None:
                if skip not in ("wallet-not-followed",):
                    log.debug("no signal (%s) %s %s", skip, trade.side,
                              trade.token.symbol)
                continue
            if sig.action == "open":
                self.executor.open(sig)
            elif sig.action == "close":
                pos = self.risk.positions.get(sig.token.address.lower())
                if pos:
                    self.executor.close(pos, "mirror-wallet-sell")

        # 2) manage open positions (TP/SL/trailing/timeout)
        for pos, reason in self.risk.exits_due(self.executor.price_lookup):
            self.executor.close(pos, reason)

        # 3) periodic equity snapshot + daily roll
        now = time.time()
        if now - self._last_equity_snap > 30:
            pos_val = self.risk.equity_usd(self.executor.price_lookup) \
                - self.risk.cash_usd
            self.ledger.record_equity(self.risk.cash_usd, pos_val,
                                      self.risk.realized_pnl_usd)
            self.risk.roll_day_if_needed(self.executor.price_lookup)
            self._save_state()
            self._last_equity_snap = now

    # ---------------------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        def handler(signum, frame):
            log.info("signal %s received, stopping...", signum)
            self._stop = True
        try:
            _signal.signal(_signal.SIGINT, handler)
            _signal.signal(_signal.SIGTERM, handler)
        except (ValueError, AttributeError):
            pass  # not in main thread / unsupported platform

    def shutdown(self) -> None:
        s = self.ledger.summary()
        log.info("---- session summary (%s) ----", self.mode)
        log.info("closed trades: %d | win-rate: %.0f%% | net pnl: $%+.2f | "
                 "fees: $%.2f", s["closed_trades"], s["win_rate"] * 100,
                 s["net_pnl_usd"], s["total_fees_usd"])
        log.info("cash $%.2f | open positions: %d | realized pnl $%+.2f",
                 self.risk.cash_usd, len(self.risk.positions),
                 self.risk.realized_pnl_usd)
        self._save_state()
        paths = self.ledger.export_csv(self.cfg.csv_dir)
        log.info("CSV exported: %s", ", ".join(paths))
        try:
            self.monitor.shutdown()
        except Exception:
            pass
        self.ledger.close()
