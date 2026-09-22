"""Pricing, liquidity and token-safety helpers.

- Token metadata (symbol/decimals) via eth_call, cached.
- USD price + liquidity via the free DexScreener API.
- Optional honeypot/tax check via the free honeypot.is API.

All network calls are wrapped so a failure degrades gracefully (returns
None / conservative defaults) rather than crashing the engine."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from . import abi
from .chains import Chain, get_chain
from .models import Token
from .rpc import RpcClient

_HTTP_HEADERS = {"user-agent": "copytrader/0.1", "accept": "application/json"}


def _http_get_json(url: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers=_HTTP_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Token metadata
# ---------------------------------------------------------------------------
class TokenResolver:
    def __init__(self, rpc: RpcClient, chain_key: str):
        self.rpc = rpc
        self.chain_key = chain_key
        self._cache: dict[str, Token] = {}

    def resolve(self, address: str) -> Token:
        address = address.lower()
        if address in self._cache:
            return self._cache[address]
        symbol, decimals = "?", 18
        try:
            dec_hex = self.rpc.eth_call(address, abi.SEL_DECIMALS)
            decimals = abi.decode_uint(dec_hex) if dec_hex not in ("0x", "") else 18
            if decimals > 36:  # garbage guard
                decimals = 18
        except Exception:
            pass
        try:
            sym_hex = self.rpc.eth_call(address, abi.SEL_SYMBOL)
            symbol = abi.decode_string(sym_hex) or "?"
        except Exception:
            pass
        tok = Token(address=address, chain=self.chain_key,
                    symbol=symbol[:24], decimals=decimals)
        self._cache[address] = tok
        return tok


# ---------------------------------------------------------------------------
# DexScreener price + liquidity
# ---------------------------------------------------------------------------
@dataclass
class MarketData:
    price_usd: float
    liquidity_usd: float
    volume_h24: float
    pair_created_ms: int
    pair_address: str
    fdv_usd: float

    @property
    def age_hours(self) -> float:
        if self.pair_created_ms <= 0:
            return 1e9
        return max(0.0, (time.time() * 1000 - self.pair_created_ms) / 3.6e6)


class PriceFeed:
    """Thin DexScreener client with a short TTL cache to avoid hammering the
    public API when polling many open positions."""

    BASE = "https://api.dexscreener.com/latest/dex/tokens/"

    def __init__(self, ttl: float = 8.0):
        self.ttl = ttl
        self._cache: dict[str, tuple[float, Optional[MarketData]]] = {}

    def get(self, chain: Chain, token_address: str) -> Optional[MarketData]:
        key = f"{chain.dexscreener_id}:{token_address.lower()}"
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        data = _http_get_json(self.BASE + token_address.lower())
        md = self._best_pair(data, chain.dexscreener_id) if data else None
        self._cache[key] = (now, md)
        return md

    @staticmethod
    def _best_pair(data: dict, chain_id: str) -> Optional[MarketData]:
        pairs = data.get("pairs") or []
        best: Optional[MarketData] = None
        for p in pairs:
            if p.get("chainId") != chain_id:
                continue
            try:
                liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
                price = float(p.get("priceUsd") or 0.0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            md = MarketData(
                price_usd=price,
                liquidity_usd=liq,
                volume_h24=float((p.get("volume") or {}).get("h24") or 0.0),
                pair_created_ms=int(p.get("pairCreatedAt") or 0),
                pair_address=p.get("pairAddress", ""),
                fdv_usd=float(p.get("fdv") or 0.0),
            )
            if best is None or md.liquidity_usd > best.liquidity_usd:
                best = md
        return best


# ---------------------------------------------------------------------------
# Honeypot / tax check
# ---------------------------------------------------------------------------
@dataclass
class SafetyReport:
    ok: bool
    is_honeypot: bool
    buy_tax: float
    sell_tax: float
    reason: str

    @classmethod
    def unknown(cls) -> "SafetyReport":
        return cls(ok=True, is_honeypot=False, buy_tax=0.0, sell_tax=0.0,
                   reason="check-skipped-or-unavailable")


class GasOracle:
    """Live gas price per chain via eth_gasPrice, cached with a short TTL.
    Turns a static gas assumption into one that tracks network congestion."""

    def __init__(self, rpc_overrides: dict | None = None, ttl: float = 15.0):
        self.rpc_overrides = rpc_overrides or {}
        self.ttl = ttl
        self._rpc: dict = {}
        self._gp: dict[str, tuple[int, float]] = {}

    def _client(self, chain_key: str):
        if chain_key not in self._rpc:
            from .config import resolve_rpcs
            from .rpc import RpcClient
            self._rpc[chain_key] = RpcClient(
                resolve_rpcs(self.rpc_overrides, chain_key))
        return self._rpc[chain_key]

    def gas_price_wei(self, chain_key: str) -> int:
        now = time.time()
        hit = self._gp.get(chain_key)
        if hit and now - hit[1] < self.ttl:
            return hit[0]
        gp = 0
        try:
            gp = int(self._client(chain_key).call("eth_gasPrice"), 16)
        except Exception:
            gp = 0
        if gp > 0:
            self._gp[chain_key] = (gp, now)
        return gp


class HoneypotChecker:
    URL = "https://api.honeypot.is/v2/IsHoneypot"

    def __init__(self, enabled: bool = True, max_tax: float = 0.10):
        self.enabled = enabled
        self.max_tax = max_tax
        self._cache: dict[str, SafetyReport] = {}

    def check(self, chain: Chain, token_address: str) -> SafetyReport:
        if not self.enabled:
            return SafetyReport.unknown()
        key = f"{chain.chain_id}:{token_address.lower()}"
        if key in self._cache:
            return self._cache[key]
        url = f"{self.URL}?address={token_address}&chainID={chain.chain_id}"
        data = _http_get_json(url)
        rep = self._parse(data)
        self._cache[key] = rep
        return rep

    def _parse(self, data: Optional[dict]) -> SafetyReport:
        if not data:
            return SafetyReport.unknown()
        try:
            hp = bool((data.get("honeypotResult") or {}).get("isHoneypot"))
            sim = data.get("simulationResult") or {}
            buy_tax = float(sim.get("buyTax") or 0.0) / 100.0
            sell_tax = float(sim.get("sellTax") or 0.0) / 100.0
        except (TypeError, ValueError):
            return SafetyReport.unknown()
        if hp:
            return SafetyReport(False, True, buy_tax, sell_tax, "honeypot")
        if sell_tax > self.max_tax or buy_tax > self.max_tax:
            return SafetyReport(False, False, buy_tax, sell_tax,
                                f"tax too high (buy {buy_tax:.0%} / "
                                f"sell {sell_tax:.0%})")
        return SafetyReport(True, False, buy_tax, sell_tax, "ok")
