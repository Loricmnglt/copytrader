"""Data structures for the Hyperliquid copy engine: the target exposure the
cohort implies, and our own simulated/real perp position."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TargetPosition:
    coin: str
    notional: float                 # signed target USD notional (+long/-short)
    score: float                    # cohort directional score that produced it
    wallets: list = field(default_factory=list)  # contributing source wallets


@dataclass
class OurPosition:
    coin: str
    szi: float                      # signed size in coin units (+long/-short)
    entry_px: float                 # average entry price
    opened_ts: float
    fees_paid: float = 0.0          # cumulative taker fees (USD)
    funding_paid: float = 0.0       # cumulative funding (USD, +=cost)
    source_wallets: list = field(default_factory=list)

    @property
    def side(self) -> str:
        return "long" if self.szi > 0 else ("short" if self.szi < 0 else "flat")

    def notional(self, mark: float) -> float:
        return self.szi * mark

    def unrealized(self, mark: float) -> float:
        return self.szi * (mark - self.entry_px)

    def to_dict(self) -> dict:
        return {"coin": self.coin, "szi": self.szi, "entry_px": self.entry_px,
                "opened_ts": self.opened_ts, "fees_paid": self.fees_paid,
                "funding_paid": self.funding_paid,
                "source_wallets": self.source_wallets}

    @classmethod
    def from_dict(cls, d: dict) -> "OurPosition":
        return cls(coin=d["coin"], szi=d["szi"], entry_px=d["entry_px"],
                   opened_ts=d.get("opened_ts", 0.0),
                   fees_paid=d.get("fees_paid", 0.0),
                   funding_paid=d.get("funding_paid", 0.0),
                   source_wallets=d.get("source_wallets", []))
