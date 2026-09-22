"""Normalized data structures passed between the monitor, signal engine,
risk manager, executors and ledger."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Token:
    address: str
    chain: str
    symbol: str = "?"
    decimals: int = 18

    def amount(self, raw: int) -> float:
        return raw / (10 ** self.decimals)

    def to_dict(self) -> dict:
        return {"address": self.address, "chain": self.chain,
                "symbol": self.symbol, "decimals": self.decimals}

    @classmethod
    def from_dict(cls, d: dict) -> "Token":
        return cls(address=d["address"], chain=d["chain"],
                   symbol=d.get("symbol", "?"), decimals=d.get("decimals", 18))


@dataclass
class WalletTrade:
    """A buy or sell reconstructed from a followed wallet's on-chain tx."""
    chain: str
    wallet: str
    tx_hash: str
    block: int
    ts: float                    # unix seconds (block time if known, else now)
    side: str                    # "buy" or "sell" (from the wallet's view)
    token: Token                 # the non-base (memecoin) token
    token_amount: float          # amount of the memecoin bought/sold
    base_symbol: str             # WETH / USDC / ...
    base_amount: float           # size of the money leg (in base units)
    base_is_native: bool = False
    gas_native: float = 0.0      # gas paid by the wallet in native units
    detected_ts: float = field(default_factory=time.time)

    @property
    def price_in_base(self) -> float:
        if self.token_amount <= 0:
            return 0.0
        return self.base_amount / self.token_amount

    def key(self) -> str:
        return f"{self.chain}:{self.tx_hash}:{self.token.address}"


@dataclass
class Signal:
    """Output of the signal engine: an intent to act, before risk sizing."""
    action: str                  # "open" or "close"
    chain: str
    token: Token
    source_trade: WalletTrade
    conviction: float            # 0..1 composite score
    reasons: list = field(default_factory=list)
    contributing_wallets: list = field(default_factory=list)


@dataclass
class Position:
    id: int
    chain: str
    token: Token
    opened_ts: float
    entry_price_usd: float
    entry_price_base: float
    qty_token: float             # simulated token quantity held
    cost_usd: float              # USD deployed (incl. fees) at entry
    source_wallets: list = field(default_factory=list)
    high_price_usd: float = 0.0  # for trailing stop
    status: str = "open"         # open | closed
    take_profit_usd: float = 0.0
    stop_loss_usd: float = 0.0

    def unrealized_usd(self, price_usd: float) -> float:
        return self.qty_token * price_usd - self.cost_usd

    def to_dict(self) -> dict:
        return {
            "id": self.id, "chain": self.chain, "token": self.token.to_dict(),
            "opened_ts": self.opened_ts,
            "entry_price_usd": self.entry_price_usd,
            "entry_price_base": self.entry_price_base,
            "qty_token": self.qty_token, "cost_usd": self.cost_usd,
            "source_wallets": self.source_wallets,
            "high_price_usd": self.high_price_usd, "status": self.status,
            "take_profit_usd": self.take_profit_usd,
            "stop_loss_usd": self.stop_loss_usd,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            id=d["id"], chain=d["chain"], token=Token.from_dict(d["token"]),
            opened_ts=d["opened_ts"], entry_price_usd=d["entry_price_usd"],
            entry_price_base=d.get("entry_price_base", 0.0),
            qty_token=d["qty_token"], cost_usd=d["cost_usd"],
            source_wallets=d.get("source_wallets", []),
            high_price_usd=d.get("high_price_usd", 0.0),
            status=d.get("status", "open"),
            take_profit_usd=d.get("take_profit_usd", 0.0),
            stop_loss_usd=d.get("stop_loss_usd", 0.0))


@dataclass
class Fill:
    """A simulated (or, in live mode, real) execution result."""
    ts: float
    chain: str
    side: str                    # buy | sell
    token: Token
    qty_token: float
    price_usd: float
    gross_usd: float             # notional before fees
    fee_usd: float               # gas + dex + slippage cost, all-in
    slippage_pct: float
    latency_s: float
    source_wallet: str
    tx_hash: str = "PAPER"
    note: str = ""
