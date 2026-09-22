"""Configuration loading (TOML, parsed with the stdlib tomllib on 3.11+).

Everything the operator tunes lives in one config.toml; this module maps it
onto typed dataclasses with sane defaults, so a partial config still works.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _sub_env(url: str) -> str:
    """Expand ${VAR} in an RPC URL from the environment, so API keys live in
    env vars, never in config.toml."""
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), url)


def resolve_rpcs(rpc_overrides: dict, chain_key: str) -> list[str]:
    """Endpoints for a chain: config overrides (with ${ENV} expanded) if given,
    else the built-in public defaults."""
    from .chains import get_chain
    over = (rpc_overrides or {}).get(chain_key)
    urls = list(over) if over else list(get_chain(chain_key).rpcs)
    resolved = [_sub_env(u) for u in urls]
    return [u for u in resolved if u and "${" not in u]

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


@dataclass
class WalletCfg:
    address: str
    label: str = ""
    weight: float = 1.0          # manual override; analyzer can supersede
    enabled: bool = True


@dataclass
class RiskCfg:
    budget: float = 100.0                 # total budget, in the quote currency
    quote_currency: str = "CHF"
    quote_to_usd: float = 1.12            # 1 quote unit -> USD (CHF~1.12)
    reserve_pct: float = 0.10             # keep this fraction as dry powder
    base_alloc_pct: float = 0.10          # base % of budget per new position
    max_position_pct: float = 0.20        # hard cap of budget in one token
    max_concurrent: int = 6               # max simultaneous open positions
    max_per_token_pct: float = 0.20       # cap of budget per single token
    take_profit_pct: float = 0.60         # +60% -> take profit
    stop_loss_pct: float = 0.35           # -35% -> stop loss
    trailing_stop_pct: float = 0.25       # give back 25% from the high -> exit
    max_hold_minutes: float = 240.0       # time-based exit
    cooldown_minutes: float = 30.0        # per token, after a close
    daily_loss_halt_pct: float = 0.25     # stop opening if -25% on the day


@dataclass
class SignalCfg:
    min_liquidity_usd: float = 20_000.0   # skip illiquid tokens
    min_volume_h24_usd: float = 10_000.0
    min_pair_age_minutes: float = 15.0    # avoid brand-new snipes we can't verify
    max_pair_age_hours: float = 720.0     # ignore ancient/dead tokens (optional)
    max_price_drift_pct: float = 0.12     # skip if price already +12% vs wallet entry
    confluence_window_s: float = 900.0    # 2+ wallets within 15 min => boost
    min_conviction: float = 0.35          # below this, ignore the signal
    min_wallet_base_usd: float = 50.0     # ignore wallet's dust trades
    honeypot_check: bool = True
    max_token_tax_pct: float = 0.10
    mirror_wallet_sells: bool = True      # sell when a source wallet sells
    # noise filter: drop non-memecoin "swaps" (lending aTokens, LP, wrapping).
    # a token priced ~1:1 against WETH is a wrap/lending receipt, not a trade.
    ignore_token_symbol_patterns: list = field(default_factory=lambda: [
        r"^aArb", r"^aEth", r"^aBas", r"^aOpt", r"^aPol", r"^aAvax",
        r"^am[A-Z]", r"^av[A-Z]", r"variableDebt", r"stableDebt",
        r"UNI-V2", r"SLP$", r"CAKE-LP", r"-LP$", r"^yv", r"^moo",
        r"^wst", r"vAMM-", r"sAMM-", r"/",
    ])


@dataclass
class ExecCfg:
    latency_ms: float = 4000.0            # modeled detection->fill delay
    dex_fee_pct: float = 0.003            # pool fee (0.30% typical)
    extra_slippage_pct: float = 0.0       # operator fudge factor on top of model
    price_impact_factor: float = 1.0      # scale the impact model (calibrate)
    # gas: when use_live_gas, cost = live eth_gasPrice * swap_gas_units *
    # native_usd; the static gas_usd_* below are the fallback if the RPC gas
    # price is unavailable.
    use_live_gas: bool = True
    gas_usd_arbitrum: float = 0.05
    gas_usd_base: float = 0.03
    gas_usd_ethereum: float = 8.0
    gas_usd_bsc: float = 0.15
    swap_gas_units_arbitrum: int = 500_000
    swap_gas_units_base: int = 250_000
    swap_gas_units_ethereum: int = 220_000
    swap_gas_units_bsc: int = 300_000

    def swap_gas_units(self, chain_key: str) -> int:
        return int(getattr(self, f"swap_gas_units_{chain_key}", 300_000))


@dataclass
class LiveCfg:
    enabled: bool = False                 # HARD gate for real money
    chain: str = "arbitrum"               # single chain for live execution
    aggregator: str = "odos"              # odos | zerox
    private_key_env: str = "COPYTRADER_PK"  # env var name (never in config!)
    max_gas_price_gwei: float = 2.0       # refuse if gas above this
    dry_run_first: bool = True            # build+quote but do not send


@dataclass
class HLCfg:
    """Hyperliquid perp copy-trader settings."""
    budget: float = 100.0
    quote_currency: str = "CHF"
    quote_to_usd: float = 1.12
    # how their fractional book exposure maps to ours, and our exposure caps
    exposure_scale: float = 1.0           # 1 => match their frac-of-book
    max_gross_leverage: float = 2.0       # our total |notional| <= base*this
    size_on_equity: bool = False          # False: size on fixed budget (clean
    #   measurement). True: size on CURRENT equity => constant leverage +
    #   compounding (gains AND losses scale with the account).
    max_per_coin_pct: float = 0.60        # |notional per coin| <= budget*this
    per_wallet_exposure_clamp: float = 3.0  # clamp each wallet's frac exposure
    rebalance_band_pct: float = 0.06      # ignore target deltas < 6% of budget
    min_order_usd: float = 12.0           # HL min notional ~ $10
    daily_loss_halt_pct: float = 0.25     # stop opening if account -25% on day
    # per-trade + per-account protection (essential at high leverage)
    stop_loss_pct: float = 0.0            # 0=off; 0.10 => close a pos -10% from entry
    account_kill_pct: float = 0.0         # 0=off; 0.45 => flatten ALL + halt if equity -45%
    stop_cooldown_min: float = 90.0       # after a stop, no re-entry on that coin
    taker_fee_pct: float = 0.00045        # HL taker fee ~0.045%
    slippage_pct: float = 0.0005          # modeled slippage on liquid perps
    funding_enabled: bool = True
    poll_interval_s: float = 5.0
    coins_whitelist: list = field(default_factory=list)  # empty => all
    coins_blacklist: list = field(default_factory=list)
    db_path: str = "hl_copytrader.sqlite"
    csv_dir: str = "hl_reports"


@dataclass
class HLLiveCfg:
    enabled: bool = False
    private_key_env: str = "COPYTRADER_PK"   # env var NAME (not the key)
    dry_run_first: bool = True
    max_slippage_pct: float = 0.01
    account_address: str = ""                 # main account addr when the key
    #   is a trade-only API/agent wallet (empty = key IS the account)


@dataclass
class AppCfg:
    chains: list = field(default_factory=lambda: ["arbitrum", "base",
                                                   "ethereum"])
    wallets: list = field(default_factory=list)
    poll_interval_s: float = 3.0
    db_path: str = "copytrader.sqlite"
    csv_dir: str = "reports"
    etherscan_api_key_env: str = "ETHERSCAN_API_KEY"  # optional, for analyzer
    rpc_overrides: dict = field(default_factory=dict)  # chain -> [urls]
    risk: RiskCfg = field(default_factory=RiskCfg)
    signal: SignalCfg = field(default_factory=SignalCfg)
    execution: ExecCfg = field(default_factory=ExecCfg)
    live: LiveCfg = field(default_factory=LiveCfg)
    hyperliquid: HLCfg = field(default_factory=HLCfg)
    hl_live: HLLiveCfg = field(default_factory=HLLiveCfg)

    def etherscan_key(self) -> str | None:
        return os.environ.get(self.etherscan_api_key_env) or None

    def gas_usd(self, chain_key: str) -> float:
        return getattr(self.execution, f"gas_usd_{chain_key}", 0.5)


def _fill(dc_cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown keys."""
    known = {f.name for f in fields(dc_cls)}
    return dc_cls(**{k: v for k, v in (data or {}).items() if k in known})


def load_config(path: str) -> AppCfg:
    if tomllib is None:
        raise RuntimeError("Python 3.11+ required (tomllib not found)")
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    cfg = AppCfg()
    for key in ("chains", "poll_interval_s", "db_path", "csv_dir",
                "etherscan_api_key_env", "rpc_overrides"):
        if key in raw:
            setattr(cfg, key, raw[key])

    cfg.wallets = [_fill(WalletCfg, w) for w in raw.get("wallets", [])]
    if "risk" in raw:
        cfg.risk = _fill(RiskCfg, raw["risk"])
    if "signal" in raw:
        cfg.signal = _fill(SignalCfg, raw["signal"])
    if "execution" in raw:
        cfg.execution = _fill(ExecCfg, raw["execution"])
    if "live" in raw:
        cfg.live = _fill(LiveCfg, raw["live"])
    if "hyperliquid" in raw:
        cfg.hyperliquid = _fill(HLCfg, raw["hyperliquid"])
    if "hl_live" in raw:
        cfg.hl_live = _fill(HLLiveCfg, raw["hl_live"])
    return cfg
