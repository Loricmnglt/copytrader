"""Signal engine: turn raw wallet trades into scored copy decisions.

This is the "intelligence" layer the brief asks for. It does NOT copy blindly:
each buy is filtered (liquidity, volume, age, honeypot/tax, price already run
up = too late) and scored into a `conviction` in [0,1] that blends:

  * the source wallet's weight (from the analyzer / manual config)
  * confluence  (several followed wallets buying the same token in a window)
  * freshness   (how far price has drifted from the wallet's entry -> penalty)
  * market      (liquidity / 24h volume adequacy)

Sells from a followed wallet mirror into a close signal for tokens we hold."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .chains import get_chain
from .config import AppCfg
from .models import Signal, WalletTrade
from .prices import HoneypotChecker, PriceFeed

log = logging.getLogger("copytrader.signals")

# composite conviction weights (sum = 1.0)
W_WALLET = 0.35
W_CONFLUENCE = 0.25
W_FRESHNESS = 0.25
W_MARKET = 0.15


class SignalEngine:
    def __init__(self, cfg: AppCfg, wallet_weights: dict[str, float],
                 price_feed: PriceFeed, honeypot: HoneypotChecker,
                 held_tokens):
        self.cfg = cfg
        self.sig = cfg.signal
        self.weights = {k.lower(): v for k, v in wallet_weights.items()}
        self.max_weight = max(self.weights.values(), default=1.0) or 1.0
        self.prices = price_feed
        self.honeypot = honeypot
        self.held_tokens = held_tokens  # callable -> set of held token addrs
        # recent buys per token for confluence: token -> [(wallet, ts)]
        self._recent: dict[str, list[tuple[str, float]]] = {}
        self._native_usd_cache: dict[str, tuple[float, float]] = {}

    # ---------------------------------------------------------------------
    def process(self, trade: WalletTrade) -> tuple[Signal | None, str]:
        """Return (signal, skip_reason). skip_reason is "" when a signal is
        produced."""
        if trade.wallet.lower() not in self.weights:
            return None, "wallet-not-followed"

        if trade.side == "sell":
            return self._process_sell(trade)
        return self._process_buy(trade)

    # ---- sells (mirror exit) --------------------------------------------
    def _process_sell(self, trade: WalletTrade):
        if not self.sig.mirror_wallet_sells:
            return None, "mirror-sells-disabled"
        held = self.held_tokens()
        if trade.token.address.lower() not in held:
            return None, "sell-of-token-not-held"
        sig = Signal(
            action="close",
            chain=trade.chain,
            token=trade.token,
            source_trade=trade,
            conviction=1.0,
            reasons=[f"source wallet {trade.wallet[:10]} sold {trade.token.symbol}"],
            contributing_wallets=[trade.wallet],
        )
        return sig, ""

    # ---- buys ------------------------------------------------------------
    def _process_buy(self, trade: WalletTrade):
        chain = get_chain(trade.chain)
        token_addr = trade.token.address.lower()

        # dust filter (wallet spent too little to be a real conviction trade)
        base_usd = self._base_usd(trade)
        if base_usd < self.sig.min_wallet_base_usd:
            return None, f"wallet-trade-too-small (${base_usd:.0f})"

        # record for confluence regardless of whether this one passes filters
        self._note_recent(token_addr, trade.wallet, trade.ts or time.time())

        # already holding -> let risk manager decide add; here we still allow
        # a signal so confluence can top up, but flag it
        md = self.prices.get(chain, token_addr)
        if md is None:
            return None, "no-market-data"
        if md.liquidity_usd < self.sig.min_liquidity_usd:
            return None, (f"liquidity ${md.liquidity_usd:,.0f} < "
                          f"${self.sig.min_liquidity_usd:,.0f}")
        if md.volume_h24 < self.sig.min_volume_h24_usd:
            return None, f"low 24h volume ${md.volume_h24:,.0f}"
        age_min = md.age_hours * 60
        if age_min < self.sig.min_pair_age_minutes:
            return None, f"pair too new ({age_min:.0f} min)"
        if md.age_hours > self.sig.max_pair_age_hours:
            return None, f"pair too old ({md.age_hours:.0f} h)"

        # freshness: how far has price moved past the wallet's entry?
        entry_usd = (base_usd / trade.token_amount) if trade.token_amount else 0
        drift = ((md.price_usd - entry_usd) / entry_usd) if entry_usd > 0 else 0.0
        if drift > self.sig.max_price_drift_pct:
            return None, (f"too late: price +{drift:.1%} vs wallet entry "
                          f"(max {self.sig.max_price_drift_pct:.0%})")

        # honeypot / tax
        safety = self.honeypot.check(chain, token_addr)
        if not safety.ok:
            return None, f"unsafe token: {safety.reason}"

        # ---- score --------------------------------------------------------
        w_score = min(1.0, self.weights.get(trade.wallet.lower(), 0.0)
                      / self.max_weight)
        confl_wallets = self._confluence(token_addr)
        confl_score = min(1.0, (len(confl_wallets) - 1) * 0.5)  # 0,0.5,1.0...
        fresh_score = max(0.0, 1.0 - (max(0.0, drift)
                                      / max(1e-9, self.sig.max_price_drift_pct)))
        market_score = self._market_score(md)

        conviction = (W_WALLET * w_score + W_CONFLUENCE * confl_score
                      + W_FRESHNESS * fresh_score + W_MARKET * market_score)

        reasons = [
            f"wallet_w={w_score:.2f}",
            f"confluence={len(confl_wallets)}w({confl_score:.2f})",
            f"freshness={fresh_score:.2f}(drift {drift:+.1%})",
            f"market={market_score:.2f}(liq ${md.liquidity_usd:,.0f})",
            f"tax buy/sell {safety.buy_tax:.0%}/{safety.sell_tax:.0%}",
        ]

        if conviction < self.sig.min_conviction:
            return None, (f"conviction {conviction:.2f} < "
                          f"{self.sig.min_conviction:.2f}")

        sig = Signal(
            action="open",
            chain=trade.chain,
            token=trade.token,
            source_trade=trade,
            conviction=round(conviction, 3),
            reasons=reasons,
            contributing_wallets=confl_wallets,
        )
        return sig, ""

    # ---- scoring helpers -------------------------------------------------
    def _market_score(self, md) -> float:
        # scale liquidity into 0..1: 20k->~0, 200k+->~1
        import math
        lo = self.sig.min_liquidity_usd
        if md.liquidity_usd <= lo:
            return 0.0
        return min(1.0, math.log10(md.liquidity_usd / lo) / math.log10(10))

    def _note_recent(self, token_addr: str, wallet: str, ts: float) -> None:
        lst = self._recent.setdefault(token_addr, [])
        lst.append((wallet.lower(), ts))
        cutoff = time.time() - self.sig.confluence_window_s
        self._recent[token_addr] = [(w, t) for w, t in lst
                                    if (t or time.time()) >= cutoff]

    def _confluence(self, token_addr: str) -> list[str]:
        seen = {}
        for w, _t in self._recent.get(token_addr, []):
            seen[w] = True
        return list(seen.keys())

    def _base_usd(self, trade: WalletTrade) -> float:
        """Convert the wallet's money leg into USD."""
        chain = get_chain(trade.chain)
        sym = trade.base_symbol.upper()
        if sym in ("USDC", "USDT", "DAI", "BUSD", "USDBC", "USDC.E"):
            return trade.base_amount
        # native / wrapped native -> multiply by native USD price
        return trade.base_amount * self._native_usd(trade.chain)

    def _native_usd(self, chain_key: str) -> float:
        now = time.time()
        hit = self._native_usd_cache.get(chain_key)
        if hit and now - hit[1] < 60:
            return hit[0]
        chain = get_chain(chain_key)
        md = self.prices.get(chain, chain.wrapped_native)
        val = md.price_usd if md else 0.0
        if val > 0:
            self._native_usd_cache[chain_key] = (val, now)
        return val
