"""Paper executor: simulate fills against REAL market data.

The point of paper mode is an honest edge measurement, so we do not assume
perfect fills. Every simulated trade pays:

  * gas       (per-chain USD estimate from config)
  * dex fee   (pool fee %, config)
  * slippage  (price-impact model from notional vs on-chain liquidity, plus
               an operator fudge factor)

and it fills at the CURRENT DexScreener price, which is already later than the
source wallet's entry — that is exactly the "we arrived late" penalty, made
explicit. Latency is recorded (block time -> now, plus a modeled execution
delay) so reports can show how stale our fills were."""
from __future__ import annotations

import logging
import time

from .chains import get_chain
from .config import AppCfg
from .ledger import ClosedTrade, Ledger
from .models import Fill, Position, Signal
from .prices import GasOracle, PriceFeed
from .risk import RiskManager

log = logging.getLogger("copytrader.paper")


class PaperExecutor:
    mode = "paper"

    def __init__(self, cfg: AppCfg, risk: RiskManager, ledger: Ledger,
                 price_feed: PriceFeed):
        self.cfg = cfg
        self.risk = risk
        self.ledger = ledger
        self.prices = price_feed
        self.gas_oracle = GasOracle(cfg.rpc_overrides)

    # -- price helpers -----------------------------------------------------
    def current_price(self, chain_key: str, token_addr: str) -> float | None:
        md = self.prices.get(get_chain(chain_key), token_addr)
        return md.price_usd if md else None

    def _liquidity(self, chain_key: str, token_addr: str) -> float:
        md = self.prices.get(get_chain(chain_key), token_addr)
        return md.liquidity_usd if md else 0.0

    def _native_usd(self, chain_key: str) -> float:
        chain = get_chain(chain_key)
        md = self.prices.get(chain, chain.wrapped_native)
        return md.price_usd if md else 0.0

    def _gas_usd(self, chain_key: str) -> float:
        """Live gas cost of one swap when enabled, else the static estimate."""
        if self.cfg.execution.use_live_gas:
            native_usd = self._native_usd(chain_key)
            gp = self.gas_oracle.gas_price_wei(chain_key)
            if native_usd > 0 and gp > 0:
                units = self.cfg.execution.swap_gas_units(chain_key)
                return gp * units / 1e18 * native_usd
        return self.cfg.gas_usd(chain_key)

    def price_lookup(self, pos: Position) -> float | None:
        return self.current_price(pos.chain, pos.token.address)

    def _slippage(self, chain_key: str, token_addr: str, notional: float) -> float:
        liq = self._liquidity(chain_key, token_addr)
        # constant-product-ish impact: ~ notional / (one side of the pool),
        # scaled by an operator-calibratable factor
        impact = (2.0 * notional / liq) if liq > 0 else 0.5
        impact *= self.cfg.execution.price_impact_factor
        return min(0.5, impact + self.cfg.execution.extra_slippage_pct)

    def _latency(self, signal: Signal) -> float:
        src = signal.source_trade
        detect = 0.0
        if src.ts:
            detect = max(0.0, time.time() - src.ts)
        return detect + self.cfg.execution.latency_ms / 1000.0

    # -- open --------------------------------------------------------------
    def open(self, signal: Signal) -> bool:
        ok, reason = self.risk.can_open(signal.token.address)
        if not ok:
            self.ledger.record_signal(signal, acted=False, skip_reason=reason)
            log.info("skip open %s: %s", signal.token.symbol, reason)
            return False

        notional = self.risk.size_for(signal)
        if notional < 1.0:
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason="size<$1")
            return False

        price = self.current_price(signal.chain, signal.token.address)
        if not price or price <= 0:
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason="no-price-at-fill")
            return False

        gas = self._gas_usd(signal.chain)
        slip = self._slippage(signal.chain, signal.token.address, notional)
        usable = max(0.0, notional - gas)
        dex_fee = usable * self.cfg.execution.dex_fee_pct
        fill_price = price * (1 + slip)
        qty = (usable - dex_fee) / fill_price if fill_price > 0 else 0.0
        if qty <= 0:
            self.ledger.record_signal(signal, acted=False,
                                      skip_reason="qty<=0")
            return False

        cost = notional                       # full USD leaving the book
        fee_all_in = cost - qty * price        # gas + dex fee + slippage
        latency = self._latency(signal)

        pos = self.risk.open_position(signal, entry_price_usd=cost / qty,
                                      qty_token=qty, cost_usd=cost)
        fill = Fill(ts=time.time(), chain=signal.chain, side="buy",
                    token=signal.token, qty_token=qty, price_usd=price,
                    gross_usd=cost, fee_usd=fee_all_in, slippage_pct=slip,
                    latency_s=latency,
                    source_wallet=signal.source_trade.wallet,
                    note="; ".join(signal.reasons))
        self.ledger.record_fill(fill, self.mode)
        self.ledger.record_signal(signal, acted=True)
        return True

    # -- close -------------------------------------------------------------
    def close(self, pos: Position, reason: str) -> bool:
        price = self.current_price(pos.chain, pos.token.address)
        if not price or price <= 0:
            log.warning("no price to close %s; will retry", pos.token.symbol)
            return False

        gross = pos.qty_token * price
        gas = self._gas_usd(pos.chain)
        slip = self._slippage(pos.chain, pos.token.address, gross)
        dex_fee = gross * self.cfg.execution.dex_fee_pct
        fill_price = price * (1 - slip)
        proceeds = pos.qty_token * fill_price - dex_fee - gas
        proceeds = max(0.0, proceeds)
        fee_all_in = gross - proceeds

        self.risk.close_position(pos, proceeds)

        pnl = proceeds - pos.cost_usd
        closed = ClosedTrade(
            chain=pos.chain, token=pos.token.address,
            token_symbol=pos.token.symbol,
            source_wallets=",".join(pos.source_wallets),
            open_ts=pos.opened_ts, close_ts=time.time(),
            qty_token=pos.qty_token,
            entry_price_usd=pos.entry_price_usd,
            exit_price_usd=(proceeds / pos.qty_token) if pos.qty_token else 0,
            cost_usd=pos.cost_usd, proceeds_usd=proceeds,
            fees_usd=fee_all_in, pnl_usd=pnl,
            pnl_pct=(pnl / pos.cost_usd) if pos.cost_usd else 0.0,
            exit_reason=reason, mode=self.mode)
        self.ledger.record_trade(closed)

        fill = Fill(ts=time.time(), chain=pos.chain, side="sell",
                    token=pos.token, qty_token=pos.qty_token, price_usd=price,
                    gross_usd=gross, fee_usd=fee_all_in, slippage_pct=slip,
                    latency_s=self.cfg.execution.latency_ms / 1000.0,
                    source_wallet=",".join(pos.source_wallets),
                    note=reason)
        self.ledger.record_fill(fill, self.mode)
        return True
