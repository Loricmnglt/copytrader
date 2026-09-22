"""Beta-vs-alpha benchmark: compare the copy bot's equity to simply being
long BTC/ETH at the same leverage over the same period.

The honest question this answers: are we making money because these traders are
good (alpha), or just because we're leveraged-long in a rising market (beta)?
If the bot doesn't clearly beat "long ETH 10x", the copy isn't adding value.
Wired into `hl-report` so it prints on every results check."""
from __future__ import annotations

import sqlite3

from .hl_api import HLInfo


def compute(db_path: str, coins=("BTC", "ETH"), levs=(1, 5, 10)) -> dict | None:
    c = sqlite3.connect(db_path)
    row = c.execute("SELECT min(ts), max(ts) FROM equity").fetchone()
    if not row or not row[0]:
        return None
    start_ts, end_ts = row
    start_eq = c.execute(
        "SELECT equity_usd FROM equity ORDER BY ts ASC LIMIT 1").fetchone()[0]
    bot_now = c.execute(
        "SELECT equity_usd FROM equity ORDER BY ts DESC LIMIT 1").fetchone()[0]
    c.close()

    info = HLInfo()
    out = {"start_ts": start_ts, "end_ts": end_ts, "start_eq": start_eq,
           "bot_now": bot_now, "coins": {}}
    for coin in coins:
        try:
            candles = info.candle_snapshot(coin, "1h", start_ts * 1000,
                                           end_ts * 1000)
            closes = [float(k["c"]) for k in candles]
        except Exception:
            continue
        if len(closes) < 2:
            continue
        paths = {}
        for L in levs:
            eq, liq = start_eq, False
            for i in range(1, len(closes)):
                eq *= (1 + L * (closes[i] / closes[i - 1] - 1))
                if eq <= 0:
                    eq, liq = 0.0, True
                    break
            paths[L] = (eq, liq)
        out["coins"][coin] = {"spot": closes[-1] / closes[0] - 1,
                              "paths": paths}
    return out


def lines(bench: dict | None) -> list[str]:
    if not bench or not bench["coins"]:
        return ["(benchmark indisponible — pas de donnees)"]
    se, bn = bench["start_eq"], bench["bot_now"]
    bot_ret = (bn / se - 1) * 100 if se else 0
    res = [f"TON BOT (copie ~10x, panier) : ${bn:.2f} ({bot_ret:+.1f}%)"]
    for coin, d in bench["coins"].items():
        eq10, liq = d["paths"].get(10, (None, False))
        if eq10 is None:
            continue
        gap = bot_ret - (eq10 / se - 1) * 100
        tag = " [aurait LIQUIDE]" if liq else ""
        res.append(f"  vs long {coin} 10x : ${eq10:.2f} "
                   f"({(eq10/se-1)*100:+.1f}%){tag}  ecart {gap:+.1f} pts "
                   f"[{coin} spot {d['spot']*100:+.1f}%]")
    # verdict
    eth = bench["coins"].get("ETH", {}).get("paths", {}).get(10, (None,))[0]
    btc = bench["coins"].get("BTC", {}).get("paths", {}).get(10, (None,))[0]
    ref = max(x for x in (eth, btc) if x is not None) if (eth or btc) else None
    if ref is not None and se:
        gap = bot_ret - (ref / se - 1) * 100
        if gap > 10:
            res.append(f"  => le bot bat le meilleur 'long 10x' de {gap:+.1f} pts "
                       "= debut d'alpha (a confirmer sur un jour rouge)")
        elif gap > -5:
            res.append(f"  => ~a egalite avec 'long 10x' ({gap:+.1f} pts) = "
                       "surtout du BETA (marche), pas d'alpha prouve")
        else:
            res.append(f"  => en-dessous de 'long 10x' ({gap:+.1f} pts) = "
                       "la copie coute plus qu'elle ne rapporte ici")
    return res
