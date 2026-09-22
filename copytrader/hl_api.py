"""Hyperliquid public info API client (read-only, stdlib only).

Everything the copy engine needs to SEE what the followed wallets are doing:
their live positions, account value, mark prices, and fill history. No key
needed for reads. Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("copytrader.hl.api")

MAINNET = "https://api.hyperliquid.xyz/info"


def _post(url: str, payload: dict, timeout: float = 15.0, retries: int = 3):
    data = json.dumps(payload).encode()
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"content-type": "application/json",
                         "user-agent": "copytrader/0.1"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
            time.sleep(0.6 * (i + 1))
    raise last


@dataclass
class HLPosition:
    coin: str
    szi: float                 # signed size (+long / -short)
    entry_px: float
    leverage: float
    unrealized_pnl: float
    position_value: float      # |notional| reported by HL

    @property
    def side(self) -> str:
        return "long" if self.szi > 0 else ("short" if self.szi < 0 else "flat")


@dataclass
class WalletState:
    address: str
    account_value: float
    positions: dict = field(default_factory=dict)  # coin -> HLPosition

    def signed_notional(self, coin: str, mark: float) -> float:
        p = self.positions.get(coin)
        return p.szi * mark if p else 0.0


class HLInfo:
    def __init__(self, url: str = MAINNET):
        self.url = url
        self._mids_cache: tuple[float, dict] | None = None

    # -- market data -------------------------------------------------------
    def all_mids(self, ttl: float = 3.0) -> dict:
        """coin -> mid price (float). Cached briefly."""
        now = time.time()
        if self._mids_cache and now - self._mids_cache[0] < ttl:
            return self._mids_cache[1]
        raw = _post(self.url, {"type": "allMids"})
        mids = {k: float(v) for k, v in raw.items()}
        self._mids_cache = (now, mids)
        return mids

    def meta_and_ctxs(self) -> tuple[list, list]:
        """(universe, assetCtxs) — assetCtxs carry current funding, oi, etc."""
        raw = _post(self.url, {"type": "metaAndAssetCtxs"})
        return raw[0]["universe"], raw[1]

    def l2_book(self, coin: str) -> dict:
        """Live order book: {'levels': [bids, asks]} with px/sz per level."""
        return _post(self.url, {"type": "l2Book", "coin": coin})

    def candle_snapshot(self, coin: str, interval: str, start_ms: int,
                        end_ms: int) -> list:
        """Historical OHLC candles (for the long-crypto benchmark)."""
        return _post(self.url, {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval,
            "startTime": int(start_ms), "endTime": int(end_ms)}}) or []

    def max_leverages(self, ttl: float = 3600.0) -> dict:
        """coin -> max leverage (for realistic maintenance-margin / liq calc)."""
        now = time.time()
        if getattr(self, "_maxlev", None) and now - self._maxlev[0] < ttl:
            return self._maxlev[1]
        try:
            universe, _ = self.meta_and_ctxs()
            out = {u["name"]: float(u.get("maxLeverage") or 3)
                   for u in universe}
        except Exception:
            out = {}
        self._maxlev = (now, out)
        return out

    def funding_rates(self) -> dict:
        """coin -> current hourly funding rate (float)."""
        try:
            universe, ctxs = self.meta_and_ctxs()
            out = {}
            for u, c in zip(universe, ctxs):
                try:
                    out[u["name"]] = float(c.get("funding") or 0.0)
                except (TypeError, ValueError):
                    pass
            return out
        except Exception as exc:
            log.warning("funding fetch failed: %s", exc)
            return {}

    # -- account state -----------------------------------------------------
    def clearinghouse_state(self, user: str) -> WalletState:
        raw = _post(self.url, {"type": "clearinghouseState", "user": user})
        acct = float((raw.get("marginSummary") or {}).get("accountValue") or 0.0)
        positions: dict[str, HLPosition] = {}
        for ap in raw.get("assetPositions", []):
            p = ap.get("position") or {}
            try:
                szi = float(p.get("szi") or 0.0)
                if szi == 0:
                    continue
                positions[p["coin"]] = HLPosition(
                    coin=p["coin"], szi=szi,
                    entry_px=float(p.get("entryPx") or 0.0),
                    leverage=float((p.get("leverage") or {}).get("value") or 0),
                    unrealized_pnl=float(p.get("unrealizedPnl") or 0.0),
                    position_value=float(p.get("positionValue") or 0.0))
            except (TypeError, KeyError, ValueError):
                continue
        return WalletState(address=user.lower(), account_value=acct,
                           positions=positions)

    def user_fills(self, user: str) -> list:
        """Recent fills for a user (list of dicts)."""
        try:
            return _post(self.url, {"type": "userFills", "user": user}) or []
        except Exception as exc:
            log.warning("userFills failed for %s: %s", user, exc)
            return []
