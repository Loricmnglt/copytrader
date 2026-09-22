"""Real-time (WebSocket) copy engine for Hyperliquid.

Instead of polling positions every 30s, it subscribes to each followed wallet's
`userFills` and reacts the moment they trade (sub-second), and to `allMids` for
live marks. This captures their entry/exit TIMING, not just their drifting net
position — the thing the polling engine misses.

Runs with its OWN db/state (hl_ws.sqlite) so it can run ALONGSIDE the polling
bot for a head-to-head comparison. Reuses the same strategy config, signal
engine, risk manager, executor and ledger."""
from __future__ import annotations

import json
import logging
import os
import signal as _signal
import socket
import time

from .config import AppCfg
from .hl_api import HLInfo, HLPosition
from .hl_risk import HLRiskManager
from .hl_signals import HLSignalEngine
from .ledger import Ledger
from .wsclient import WSClient, WSClosed

log = logging.getLogger("copytrader.hl.ws")

WS_URL = "wss://api.hyperliquid.xyz/ws"


class HLWSEngine:
    def __init__(self, cfg: AppCfg, executor_factory, mode: str = "paper"):
        self.cfg = cfg
        self.hl = cfg.hyperliquid
        self.mode = mode
        self._stop = False

        self.followed = [w.address.lower() for w in cfg.wallets if w.enabled]
        if not self.followed:
            raise SystemExit("no enabled wallets in config")

        # dedicated files so this never collides with the polling bot; and the
        # live book is kept fully separate from the paper book.
        base_dir = os.path.dirname(self.hl.db_path) or "."
        suffix = "" if mode == "paper" else f"_{mode}"
        self.db_path = os.path.join(base_dir, f"hl_ws{suffix}.sqlite")
        self.csv_dir = os.path.join(base_dir, f"hl_ws{suffix}_reports")
        self.state_path = os.path.join(base_dir, f"hl_ws_state_{mode}.json")

        self.info = HLInfo()
        self.ledger = Ledger(self.db_path)
        self.risk = HLRiskManager(cfg)
        self.weights = self._load_weights()
        self.signals = HLSignalEngine(cfg, self.weights)
        self.executor = executor_factory(cfg, self.risk, self.ledger, self.info)

        self.states: dict = {}      # addr -> WalletState (updated by fills)
        self.mids: dict = {}
        self.max_levs: dict = {}
        self._funding: dict = {}
        self._last_snap = 0.0
        self._last_reconcile = 0.0
        self._last_funding_acc = time.time()
        self._last_ping = time.time()
        self._fills_seen = 0
        self._load_state()

    # -- weights / state (same shape as the polling engine) ----------------
    def _load_weights(self) -> dict[str, float]:
        weights = {w.address.lower(): w.weight for w in self.cfg.wallets
                   if w.enabled}
        path = os.path.join(os.path.dirname(self.hl.db_path) or ".",
                            "weights_hl.json")
        if os.path.exists(path):
            try:
                for a, v in json.load(open(path, encoding="utf-8")).items():
                    if a.lower() in weights:
                        weights[a.lower()] = float(v)
            except Exception as exc:
                log.warning("weights_hl.json: %s", exc)
        return weights

    def _load_state(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                n = self.risk.restore(json.load(fh))
            log.info("restored WS state: %d position(s)", n)
        except Exception as exc:
            log.warning("restore WS state: %s", exc)

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.risk.snapshot(), fh, indent=2)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            log.warning("save WS state: %s", exc)

    # -- seed via REST -----------------------------------------------------
    def _seed(self) -> None:
        self.mids = self.info.all_mids()
        self.max_levs = self.info.max_leverages()
        self._funding = self.info.funding_rates()
        for addr in self.followed:
            try:
                self.states[addr] = self.info.clearinghouse_state(addr)
            except Exception as exc:
                log.warning("seed %s: %s", addr[:10], exc)
        log.info("seeded: %d wallets, %d mids", len(self.states),
                 len(self.mids))

    # -- run loop ----------------------------------------------------------
    def run(self) -> None:
        self._install_signal_handlers()
        log.info("HL WS %s | budget $%.2f | wallets=%d | max_gross_lev=%.1fx | "
                 "db=%s", self.mode.upper(), self.risk.budget_usd,
                 len(self.followed), self.hl.max_gross_leverage, self.db_path)
        while not self._stop:
            ws = None
            try:
                ws = WSClient(WS_URL)
                ws.connect()
                self._seed()
                self._rebalance()                 # align immediately on connect
                ws.send_text(json.dumps({"method": "subscribe",
                             "subscription": {"type": "allMids"}}))
                for addr in self.followed:
                    ws.send_text(json.dumps({"method": "subscribe",
                                 "subscription": {"type": "userFills",
                                                  "user": addr}}))
                log.info("subscribed to allMids + %d userFills streams",
                         len(self.followed))
                self._recv_loop(ws)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                log.warning("WS loop error: %s — reconnecting in 3s", exc)
                time.sleep(3)
            finally:
                if ws:
                    ws.close()
        self.shutdown()

    def _recv_loop(self, ws: WSClient) -> None:
        while not self._stop:
            try:
                msg = ws.recv()
            except socket.timeout:
                ws.send_text(json.dumps({"method": "ping"}))
                self._periodic()
                continue
            if msg is None:
                continue
            try:
                d = json.loads(msg)
            except Exception:
                continue
            ch = d.get("channel")
            if ch == "allMids":
                self._on_mids(d.get("data", {}))
            elif ch == "userFills":
                self._on_fills(d.get("data", {}))
            # heartbeat + periodic housekeeping
            now = time.time()
            if now - self._last_ping > 30:
                try:
                    ws.send_text(json.dumps({"method": "ping"}))
                except Exception:
                    raise WSClosed("ping failed")
                self._last_ping = now
            self._periodic()

    # -- message handlers --------------------------------------------------
    def _on_mids(self, data: dict) -> None:
        mids = data.get("mids")
        if mids:
            for k, v in mids.items():
                try:
                    self.mids[k] = float(v)
                except (TypeError, ValueError):
                    pass

    def _on_fills(self, data: dict) -> None:
        if data.get("isSnapshot"):
            return                      # startup snapshot: state already seeded
        addr = (data.get("user") or "").lower()
        if addr not in self.states:
            return
        changed = False
        for f in data.get("fills", []):
            try:
                coin = f["coin"]
                sz = float(f["sz"])
                delta = sz if f.get("side") == "B" else -sz
            except (KeyError, TypeError, ValueError):
                continue
            st = self.states[addr]
            p = st.positions.get(coin)
            if p:
                p.szi += delta
                if abs(p.szi) < 1e-12:
                    del st.positions[coin]
            else:
                st.positions[coin] = HLPosition(
                    coin=coin, szi=delta, entry_px=float(f.get("px") or 0),
                    leverage=0, unrealized_pnl=0, position_value=0)
            self._fills_seen += 1
            changed = True
        if changed:
            self._rebalance()           # react NOW to their trade

    # -- core actions ------------------------------------------------------
    def _rebalance(self) -> None:
        if not self.mids or not self.states:
            return
        base = self.risk.sizing_base(self.mids)
        targets = self.signals.targets(self.states, self.mids, base,
                                       self.max_levs)
        self.executor.rebalance(targets, self.mids)
        # risk controls
        if self.risk.liquidation_due(self.mids, self.max_levs):
            for coin in list(self.risk.positions):
                self.executor.flatten(coin, self.mids, "LIQUIDATION", 0.01)
            self.risk.trip_kill()
        elif self.risk.kill_due(self.mids):
            for coin in list(self.risk.positions):
                self.executor.flatten(coin, self.mids, "kill-switch")
            self.risk.trip_kill()
        else:
            for coin, pnl in self.risk.stops_due(self.mids):
                self.executor.flatten(coin, self.mids, f"stop-loss {pnl:+.1%}")
                self.risk.set_cooldown(coin)

    def _periodic(self) -> None:
        now = time.time()
        # funding accrual
        self.risk.accrue_funding(self.mids, self._funding,
                                 now - self._last_funding_acc)
        self._last_funding_acc = now
        # equity snapshot + save state every 30s
        if now - self._last_snap > 30:
            unreal = sum(p.unrealized(self.mids.get(p.coin, p.entry_px))
                         for p in self.risk.positions.values())
            cash = (self.risk.budget_usd + self.risk.realized_price_pnl
                    - self.risk.total_fees - self.risk.total_funding)
            self.ledger.record_equity(cash, unreal, cash - self.risk.budget_usd)
            self.risk.roll_day_if_needed(self.mids)
            self._save_state()
            self._last_snap = now
        # reconcile wallet state + funding via REST every 90s (correctness net)
        if now - self._last_reconcile > 90:
            try:
                for addr in self.followed:
                    self.states[addr] = self.info.clearinghouse_state(addr)
                self._funding = self.info.funding_rates()
                self._rebalance()
            except Exception as exc:
                log.debug("reconcile: %s", exc)
            self._last_reconcile = now

    # -- lifecycle ---------------------------------------------------------
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
            eq = self.risk.equity(self.mids)
        except Exception:
            eq = 0
        s = self.ledger.summary()
        log.info("---- HL WS session (%s) ----", self.mode)
        log.info("equity $%.2f | realized(price) $%+.2f | fees $%.2f | "
                 "fills reacted: %d", eq, self.risk.realized_price_pnl,
                 self.risk.total_fees, self._fills_seen)
        log.info("closed legs: %d | win %.0f%%", s["closed_trades"],
                 s["win_rate"] * 100)
        self._save_state()
        try:
            self.ledger.export_csv(self.csv_dir)
        except Exception:
            pass
        self.ledger.close()
