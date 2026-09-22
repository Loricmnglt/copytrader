"""Wallet analyzer.

Reconstructs each followed wallet's realized/unrealized P&L, win-rate and
activity, then derives a copy WEIGHT used by the signal engine. This is what
decides "which wallets deserve more capital" and "which trades/wallets to
mostly ignore" — grounded in their actual history rather than a guess.

Data source: the Etherscan V2 unified API (one key, `chainid` selects the
chain) if an API key is present — efficient and complete. Without a key it
falls back to a bounded on-chain getLogs scan (recent history only) and says
so in the notes.

P&L method (per token, aggregate): USD invested on buys vs USD returned on
sells plus current mark-to-market of any remaining holding. Base legs are
valued in USD (stables ~ $1; native/WETH at the CURRENT native price — a
documented approximation, since we don't pull historical FX)."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request

from .chains import get_chain
from .config import AppCfg
from .prices import PriceFeed
from .rpc import RpcClient
from . import abi

log = logging.getLogger("copytrader.analyzer")

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
_STABLE_SYMS = {"USDC", "USDT", "DAI", "BUSD", "USDBC", "USDC.E", "FDUSD"}


def _http_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"user-agent": "copytrader/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# raw transfer fetching
# ---------------------------------------------------------------------------
def _etherscan(chain_id: int, params: dict, key: str) -> list:
    q = dict(params)
    q.update({"chainid": chain_id, "apikey": key})
    url = ETHERSCAN_V2 + "?" + urllib.parse.urlencode(q)
    try:
        data = _http_json(url)
    except Exception as exc:
        log.warning("etherscan %s failed: %s", params.get("action"), exc)
        return []
    if str(data.get("status")) == "1" and isinstance(data.get("result"), list):
        return data["result"]
    # status 0 with "No transactions found" is normal
    return []


def fetch_history_etherscan(chain_key: str, wallet: str, key: str) -> dict:
    chain = get_chain(chain_key)
    common = {"address": wallet, "startblock": 0, "endblock": 99999999,
              "sort": "asc"}
    tokentx = _etherscan(chain.chain_id, {"module": "account",
                                          "action": "tokentx", **common}, key)
    time.sleep(0.25)
    txlist = _etherscan(chain.chain_id, {"module": "account",
                                         "action": "txlist", **common}, key)
    time.sleep(0.25)
    internal = _etherscan(chain.chain_id, {"module": "account",
                                           "action": "txlistinternal",
                                           **common}, key)
    time.sleep(0.25)
    # native sent per hash (buys) and native received per hash (sells)
    native_out: dict[str, int] = {}
    for t in txlist:
        if (t.get("from", "").lower() == wallet.lower()
                and t.get("isError") == "0"):
            v = int(t.get("value") or 0)
            if v:
                native_out[t["hash"]] = native_out.get(t["hash"], 0) + v
    native_in: dict[str, int] = {}
    for t in internal:
        if t.get("to", "").lower() == wallet.lower():
            v = int(t.get("value") or 0)
            if v:
                native_in[t["hash"]] = native_in.get(t["hash"], 0) + v
    return {"tokentx": tokentx, "native_out": native_out,
            "native_in": native_in}


# ---------------------------------------------------------------------------
# reconstruction + P&L
# ---------------------------------------------------------------------------
class TokenStat:
    __slots__ = ("symbol", "invested_usd", "returned_usd", "qty_in",
                 "qty_out", "buys", "sells", "last_ts", "decimals", "address")

    def __init__(self, address, symbol, decimals):
        self.address = address
        self.symbol = symbol
        self.decimals = decimals
        self.invested_usd = 0.0
        self.returned_usd = 0.0
        self.qty_in = 0.0
        self.qty_out = 0.0
        self.buys = 0
        self.sells = 0
        self.last_ts = 0.0


def _value_usd_of_base(sym: str, amount: float, native_usd: float) -> float:
    if sym.upper() in _STABLE_SYMS:
        return amount
    return amount * native_usd  # WETH / native


def analyze_wallet_chain(chain_key: str, wallet: str, key: str,
                         prices: PriceFeed) -> dict[str, TokenStat]:
    chain = get_chain(chain_key)
    hist = fetch_history_etherscan(chain_key, wallet, key)
    rows = hist["tokentx"]
    if not rows:
        return {}
    native_md = prices.get(chain, chain.wrapped_native)
    native_usd = native_md.price_usd if native_md else 0.0
    base_addrs = chain.base_assets
    wl = wallet.lower()

    # group token transfers by hash
    by_hash: dict[str, list] = {}
    for r in rows:
        by_hash.setdefault(r["hash"], []).append(r)

    stats: dict[str, TokenStat] = {}
    for h, legs in by_hash.items():
        recv = []  # (addr, sym, dec, amt)
        sent = []
        ts = 0.0
        for r in legs:
            ts = float(r.get("timeStamp") or 0)
            addr = r["contractAddress"].lower()
            dec = int(r.get("tokenDecimal") or 18)
            amt = int(r["value"]) / (10 ** dec)
            sym = r.get("tokenSymbol") or "?"
            if r["to"].lower() == wl:
                recv.append((addr, sym, dec, amt))
            if r["from"].lower() == wl:
                sent.append((addr, sym, dec, amt))

        token_recv = [x for x in recv if x[0] not in base_addrs]
        base_recv = [x for x in recv if x[0] in base_addrs]
        token_sent = [x for x in sent if x[0] not in base_addrs]
        base_sent = [x for x in sent if x[0] in base_addrs]
        nat_out = hist["native_out"].get(h, 0) / 1e18
        nat_in = hist["native_in"].get(h, 0) / 1e18

        # BUY: received a token, paid base or native
        if token_recv and (base_sent or nat_out > 0):
            addr, sym, dec, amt = max(token_recv, key=lambda x: x[3])
            if base_sent:
                b = max(base_sent, key=lambda x: x[3])
                usd = _value_usd_of_base(b[1], b[3], native_usd)
            else:
                usd = nat_out * native_usd
            st = stats.setdefault(addr, TokenStat(addr, sym, dec))
            st.invested_usd += usd
            st.qty_in += amt
            st.buys += 1
            st.last_ts = max(st.last_ts, ts)
        # SELL: sent a token, received base or native
        elif token_sent and (base_recv or nat_in > 0):
            addr, sym, dec, amt = max(token_sent, key=lambda x: x[3])
            if base_recv:
                b = max(base_recv, key=lambda x: x[3])
                usd = _value_usd_of_base(b[1], b[3], native_usd)
            else:
                usd = nat_in * native_usd
            st = stats.setdefault(addr, TokenStat(addr, sym, dec))
            st.returned_usd += usd
            st.qty_out += amt
            st.sells += 1
            st.last_ts = max(st.last_ts, ts)
    return stats


def analyze_wallet(wallet: str, cfg: AppCfg, prices: PriceFeed) -> dict:
    key = cfg.etherscan_key()
    if not key:
        return {"address": wallet, "error": "no-etherscan-key",
                "note": ("set the ETHERSCAN_API_KEY env var (free key covers "
                         "all chains via V2) for full history analysis"),
                "weight": 1.0}
    invested = returned = holding_value = 0.0
    tokens = wins = round_trips = buys = sells = 0
    last_ts = 0.0
    per_chain = {}
    for chain_key in cfg.chains:
        try:
            stats = analyze_wallet_chain(chain_key, wallet, key, prices)
        except Exception as exc:
            log.warning("analyze %s on %s failed: %s", wallet, chain_key, exc)
            continue
        c_invested = c_returned = c_holding = 0.0
        for addr, st in stats.items():
            hold_qty = max(0.0, st.qty_in - st.qty_out)
            hv = 0.0
            if hold_qty > 0:
                md = prices.get(get_chain(chain_key), addr)
                if md:
                    hv = hold_qty * md.price_usd
            token_pnl = st.returned_usd + hv - st.invested_usd
            invested += st.invested_usd
            returned += st.returned_usd
            holding_value += hv
            c_invested += st.invested_usd
            c_returned += st.returned_usd
            c_holding += hv
            tokens += 1
            buys += st.buys
            sells += st.sells
            last_ts = max(last_ts, st.last_ts)
            if st.sells > 0:
                round_trips += 1
                if token_pnl > 0:
                    wins += 1
        per_chain[chain_key] = {
            "tokens": len(stats),
            "invested_usd": round(c_invested, 2),
            "returned_usd": round(c_returned, 2),
            "holding_usd": round(c_holding, 2),
            "pnl_usd": round(c_returned + c_holding - c_invested, 2),
        }
    pnl = returned + holding_value - invested
    roi = (pnl / invested) if invested > 0 else 0.0
    win_rate = (wins / round_trips) if round_trips else 0.0
    return {
        "address": wallet,
        "invested_usd": round(invested, 2),
        "returned_usd": round(returned, 2),
        "holding_value_usd": round(holding_value, 2),
        "pnl_usd": round(pnl, 2),
        "roi": round(roi, 4),
        "tokens_traded": tokens,
        "round_trips": round_trips,
        "wins": wins,
        "win_rate": round(win_rate, 3),
        "buys": buys,
        "sells": sells,
        "last_trade_ts": last_ts,
        "last_trade_age_days": round((time.time() - last_ts) / 86400, 1)
        if last_ts else None,
        "per_chain": per_chain,
        "weight": derive_weight(roi, win_rate, round_trips, last_ts),
    }


def derive_weight(roi: float, win_rate: float, round_trips: int,
                  last_ts: float) -> float:
    """Map history onto a copy weight in ~[0.05, 2.0].

    - unprofitable or barely-active wallets get near-zero weight
    - a solid, active, positive-ROI wallet gets >1
    """
    if round_trips < 3:
        return 0.15  # not enough evidence -> tiny weight
    roi_score = max(-1.0, min(2.0, roi))          # cap influence of outliers
    activity = min(1.0, round_trips / 20.0)
    recency = 1.0
    if last_ts:
        age_days = (time.time() - last_ts) / 86400
        recency = 1.0 if age_days < 7 else (0.5 if age_days < 30 else 0.2)
    base = (0.5 + roi_score) * (0.5 + 0.5 * win_rate) * (0.4 + 0.6 * activity)
    weight = base * recency
    return round(max(0.05, min(2.0, weight)), 3)


def run_analysis(cfg: AppCfg) -> dict:
    prices = PriceFeed(ttl=30.0)
    reports = []
    for w in cfg.wallets:
        if not w.enabled:
            continue
        log.info("analyzing %s ...", w.address)
        rep = analyze_wallet(w.address, cfg, prices)
        rep["label"] = w.label
        reports.append(rep)
    weights = {r["address"]: r.get("weight", 1.0) for r in reports}
    out_dir = os.path.dirname(cfg.db_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "weights.json"), "w",
              encoding="utf-8") as fh:
        json.dump(weights, fh, indent=2)
    return {"reports": reports, "weights": weights}
