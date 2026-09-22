"""Analyze the followed wallets' Hyperliquid trading and derive copy weights.

Uses userFills (recent history): sums realized closedPnl and fees, computes
win-rate over closing fills, activity and recency, and maps it onto a weight
in ~[0.05, 2.0]. This answers "which of these traders is actually good, and how
much should each drive our capital" — grounded in their real perp results.

Note: userFills returns RECENT fills (bounded by the API), so this is a strong
recent-form estimate, not necessarily lifetime P&L."""
from __future__ import annotations

import json
import logging
import os
import time

from .config import AppCfg
from .hl_api import HLInfo

log = logging.getLogger("copytrader.hl.analyzer")


def analyze_wallet_hl(info: HLInfo, addr: str) -> dict:
    fills = info.user_fills(addr)
    state = info.clearinghouse_state(addr)
    acct = state.account_value
    realized = 0.0
    fees = 0.0
    closes = 0
    wins = 0
    coins = set()
    times = []
    for f in fills:
        coins.add(f.get("coin"))
        try:
            cp = float(f.get("closedPnl") or 0.0)
            fee = float(f.get("fee") or 0.0)
        except (TypeError, ValueError):
            continue
        realized += cp
        fees += fee
        if f.get("time"):
            times.append(int(f["time"]))
        is_close = str(f.get("dir", "")).startswith("Close") or cp != 0.0
        if is_close:
            closes += 1
            if cp > 0:
                wins += 1
    net = realized - fees
    last_ms = max(times) if times else 0
    first_ms = min(times) if times else 0
    span_days = (last_ms - first_ms) / 86_400_000 if last_ms > first_ms else 0.0
    win_rate = (wins / closes) if closes else 0.0
    roi = (net / acct) if acct > 0 else 0.0
    return {
        "address": addr,
        "account_value": round(acct, 2),
        "n_fills": len(fills),
        "closes": closes,
        "wins": wins,
        "win_rate": round(win_rate, 3),
        "realized_pnl": round(realized, 2),
        "fees": round(fees, 2),
        "net_pnl": round(net, 2),
        "roi_vs_acct": round(roi, 4),
        "coins": sorted(c for c in coins if c),
        "history_days": round(span_days, 1),
        "last_trade_age_h": round((time.time() * 1000 - last_ms) / 3.6e6, 1)
        if last_ms else None,
        "open_positions": {c: round(p.szi, 4)
                           for c, p in state.positions.items()},
        "weight": _weight(net, win_rate, closes, last_ms, acct),
    }


def _weight(net: float, win_rate: float, closes: int, last_ms: float,
            acct: float) -> float:
    if closes < 5:
        return 0.15
    roi = (net / acct) if acct > 0 else 0.0
    roi_score = max(-1.0, min(2.0, roi * 5))       # ROI vs current book, scaled
    activity = min(1.0, closes / 40.0)
    recency = 1.0
    if last_ms:
        age_days = (time.time() * 1000 - last_ms) / 86_400_000
        recency = 1.0 if age_days < 3 else (0.5 if age_days < 14 else 0.2)
    base = (0.5 + roi_score) * (0.4 + 0.6 * win_rate) * (0.4 + 0.6 * activity)
    return round(max(0.05, min(2.0, base * recency)), 3)


def run_hl_analysis(cfg: AppCfg) -> dict:
    info = HLInfo()
    reports = []
    for w in cfg.wallets:
        if not w.enabled:
            continue
        log.info("analyzing %s on Hyperliquid ...", w.address)
        rep = analyze_wallet_hl(info, w.address)
        rep["label"] = w.label
        reports.append(rep)
    weights = {r["address"]: r.get("weight", 1.0) for r in reports}
    out_dir = os.path.dirname(cfg.hyperliquid.db_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "weights_hl.json"), "w",
              encoding="utf-8") as fh:
        json.dump(weights, fh, indent=2)
    return {"reports": reports, "weights": weights}
