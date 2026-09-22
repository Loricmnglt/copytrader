"""Persistence + reporting.

Every signal, fill and closed trade is written to SQLite. A closed "trade" is
a full round-trip (entry fill + exit fill) and carries exactly the fields the
brief asks for:

    source wallet, token, time, amount, entry price, exit price, fees,
    result (pnl), and running total performance.

CSV exports are produced on demand for eyeballing / spreadsheets."""
from __future__ import annotations

import csv
import os
import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, chain TEXT, action TEXT, token TEXT, token_symbol TEXT,
    source_wallet TEXT, conviction REAL, reasons TEXT, acted INTEGER,
    skip_reason TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, chain TEXT, side TEXT, token TEXT, token_symbol TEXT,
    qty_token REAL, price_usd REAL, gross_usd REAL, fee_usd REAL,
    slippage_pct REAL, latency_s REAL, source_wallet TEXT, tx_hash TEXT,
    mode TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain TEXT, token TEXT, token_symbol TEXT, source_wallets TEXT,
    open_ts REAL, close_ts REAL,
    qty_token REAL,
    entry_price_usd REAL, exit_price_usd REAL,
    cost_usd REAL, proceeds_usd REAL,
    fees_usd REAL, pnl_usd REAL, pnl_pct REAL,
    exit_reason TEXT, mode TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts REAL, cash_usd REAL, positions_value_usd REAL, equity_usd REAL,
    realized_pnl_usd REAL
);
"""


@dataclass
class ClosedTrade:
    chain: str
    token: str
    token_symbol: str
    source_wallets: str
    open_ts: float
    close_ts: float
    qty_token: float
    entry_price_usd: float
    exit_price_usd: float
    cost_usd: float
    proceeds_usd: float
    fees_usd: float
    pnl_usd: float
    pnl_pct: float
    exit_reason: str
    mode: str


class Ledger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # -- writes ------------------------------------------------------------
    def record_signal(self, sig, acted: bool, skip_reason: str = "") -> None:
        self.conn.execute(
            "INSERT INTO signals (ts,chain,action,token,token_symbol,"
            "source_wallet,conviction,reasons,acted,skip_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), sig.chain, sig.action, sig.token.address,
             sig.token.symbol, sig.source_trade.wallet, sig.conviction,
             "; ".join(sig.reasons), int(acted), skip_reason))
        self.conn.commit()

    def record_fill(self, fill, mode: str) -> None:
        self.conn.execute(
            "INSERT INTO fills (ts,chain,side,token,token_symbol,qty_token,"
            "price_usd,gross_usd,fee_usd,slippage_pct,latency_s,"
            "source_wallet,tx_hash,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fill.ts, fill.chain, fill.side, fill.token.address,
             fill.token.symbol, fill.qty_token, fill.price_usd, fill.gross_usd,
             fill.fee_usd, fill.slippage_pct, fill.latency_s,
             fill.source_wallet, fill.tx_hash, mode))
        self.conn.commit()

    def record_trade(self, t: ClosedTrade) -> None:
        self.conn.execute(
            "INSERT INTO trades (chain,token,token_symbol,source_wallets,"
            "open_ts,close_ts,qty_token,entry_price_usd,exit_price_usd,"
            "cost_usd,proceeds_usd,fees_usd,pnl_usd,pnl_pct,exit_reason,mode) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t.chain, t.token, t.token_symbol, t.source_wallets, t.open_ts,
             t.close_ts, t.qty_token, t.entry_price_usd, t.exit_price_usd,
             t.cost_usd, t.proceeds_usd, t.fees_usd, t.pnl_usd, t.pnl_pct,
             t.exit_reason, t.mode))
        self.conn.commit()

    def record_equity(self, cash, pos_val, realized) -> None:
        self.conn.execute(
            "INSERT INTO equity (ts,cash_usd,positions_value_usd,equity_usd,"
            "realized_pnl_usd) VALUES (?,?,?,?,?)",
            (time.time(), cash, pos_val, cash + pos_val, realized))
        self.conn.commit()

    # -- reads / reporting -------------------------------------------------
    def summary(self) -> dict:
        cur = self.conn.execute(
            "SELECT COUNT(*) n, "
            "COALESCE(SUM(pnl_usd),0) pnl, "
            "COALESCE(SUM(fees_usd),0) fees, "
            "SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) wins "
            "FROM trades")
        row = cur.fetchone()
        n = row["n"] or 0
        wins = row["wins"] or 0
        return {
            "closed_trades": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": (wins / n) if n else 0.0,
            "net_pnl_usd": row["pnl"] or 0.0,
            "total_fees_usd": row["fees"] or 0.0,
        }

    def per_wallet(self) -> list[dict]:
        # attribute each closed trade's pnl to its (possibly multiple) wallets
        rows = self.conn.execute(
            "SELECT source_wallets, pnl_usd FROM trades").fetchall()
        agg: dict[str, dict] = {}
        for r in rows:
            wallets = [w for w in (r["source_wallets"] or "").split(",") if w]
            if not wallets:
                continue
            share = r["pnl_usd"] / len(wallets)
            for w in wallets:
                a = agg.setdefault(w, {"wallet": w, "trades": 0, "pnl": 0.0})
                a["trades"] += 1
                a["pnl"] += share
        return sorted(agg.values(), key=lambda x: x["pnl"], reverse=True)

    def export_csv(self, out_dir: str) -> list[str]:
        os.makedirs(out_dir, exist_ok=True)
        written = []
        for table in ("trades", "fills", "signals", "equity"):
            path = os.path.join(out_dir, f"{table}.csv")
            rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if rows:
                    w.writerow(rows[0].keys())
                    for r in rows:
                        w.writerow(list(r))
                else:
                    # still emit a header row where we can
                    cols = [d[1] for d in self.conn.execute(
                        f"PRAGMA table_info({table})").fetchall()]
                    w.writerow(cols)
            written.append(path)
        return written

    def close(self) -> None:
        self.conn.close()
