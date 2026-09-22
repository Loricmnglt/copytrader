"""Cohort → target exposure.

For each coin we turn the followed wallets' positions into a single signed
target notional for OUR book:

  score(coin)   = Σ_wallets  weight_w × clamp(their_signed_notional / their_acct)
  target(coin)  = score × our_budget × exposure_scale       (then capped)

Normalizing each wallet by ITS OWN account value means a $300k account and an
$870k account contribute comparably (by conviction, not raw size). Opposite
positions net out (reflects genuine consensus). Finally we cap per-coin and cap
total gross leverage so our whole book stays within budget × max_gross_leverage.
Flat cohort → zero targets → we close."""
from __future__ import annotations

import logging

from .config import AppCfg
from .hl_models import TargetPosition

log = logging.getLogger("copytrader.hl.signals")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class HLSignalEngine:
    def __init__(self, cfg: AppCfg, weights: dict[str, float]):
        self.hl = cfg.hyperliquid
        self.weights = {k.lower(): float(v) for k, v in weights.items()}

    def _coin_allowed(self, coin: str) -> bool:
        wl = self.hl.coins_whitelist
        bl = self.hl.coins_blacklist
        if wl and coin not in wl:
            return False
        if coin in bl:
            return False
        return True

    def targets(self, states: dict, mids: dict, budget_usd: float,
                max_levs: dict | None = None) -> dict[str, TargetPosition]:
        clamp = self.hl.per_wallet_exposure_clamp
        scores: dict[str, float] = {}
        contrib: dict[str, set] = {}

        for addr, st in states.items():
            w = self.weights.get(addr.lower(), 1.0)
            if w <= 0 or st.account_value <= 0:
                continue
            for coin, pos in st.positions.items():
                if not self._coin_allowed(coin):
                    continue
                mark = mids.get(coin) or pos.entry_px
                if mark <= 0:
                    continue
                frac = _clamp(pos.szi * mark / st.account_value, -clamp, clamp)
                scores[coin] = scores.get(coin, 0.0) + w * frac
                contrib.setdefault(coin, set()).add(addr.lower())

        targets: dict[str, TargetPosition] = {}
        per_coin_cap = budget_usd * self.hl.max_per_coin_pct
        for coin, score in scores.items():
            notional = _clamp(score * budget_usd * self.hl.exposure_scale,
                              -per_coin_cap, per_coin_cap)
            targets[coin] = TargetPosition(coin=coin, notional=notional,
                                           score=round(score, 4),
                                           wallets=sorted(contrib[coin]))

        # cap total gross exposure to base * max_gross_leverage
        gross = sum(abs(t.notional) for t in targets.values())
        gmax = budget_usd * self.hl.max_gross_leverage
        factor = 1.0
        if gross > gmax and gross > 0:
            factor = min(factor, gmax / gross)

        # REAL constraint: initial margin Σ|notional|/maxLev must fit the
        # capital (you can't lever a 3x-max memecoin at 10x on Hyperliquid).
        if max_levs:
            im = sum(abs(t.notional) / (max_levs.get(c, 3) or 3)
                     for c, t in targets.items())
            if im > budget_usd and im > 0:
                factor = min(factor, budget_usd / im)

        if factor < 1.0:
            for t in targets.values():
                t.notional *= factor
            log.debug("scaled targets by %.2f (gross/margin constraint)", factor)
        return targets
