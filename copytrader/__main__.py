"""Command line entry point.

    python -m copytrader doctor    # check RPC / API connectivity
    python -m copytrader analyze   # score the followed wallets -> weights.json
    python -m copytrader paper     # run the simulation (NO real money)
    python -m copytrader report    # print performance from the ledger
    python -m copytrader live      # REAL money (multiple hard gates)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import load_config
from .chains import get_chain


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S")


def _fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


# ---------------------------------------------------------------------------
def cmd_doctor(cfg, args) -> int:
    from .rpc import RpcClient
    from .prices import PriceFeed, HoneypotChecker
    print("== connectivity check ==")
    for ck in cfg.chains:
        chain = get_chain(ck)
        endpoints = (cfg.rpc_overrides or {}).get(ck) or list(chain.rpcs)
        try:
            bn = RpcClient(endpoints).block_number()
            print(f"  [OK ] {ck:9s} block {bn}")
        except Exception as exc:
            print(f"  [ERR] {ck:9s} {exc}")
    # dexscreener sample (WETH on arbitrum)
    try:
        chain = get_chain("arbitrum")
        md = PriceFeed().get(chain, chain.wrapped_native)
        print(f"  [OK ] dexscreener  WETH=${md.price_usd:,.2f}"
              if md else "  [ERR] dexscreener  no data")
    except Exception as exc:
        print(f"  [ERR] dexscreener  {exc}")
    key = cfg.etherscan_key()
    print(f"  [{'OK ' if key else '-- '}] etherscan key "
          f"{'present' if key else 'MISSING (analyzer limited)'}")
    print(f"\n  followed wallets: {sum(1 for w in cfg.wallets if w.enabled)}")
    print(f"  budget: {cfg.risk.budget} {cfg.risk.quote_currency} "
          f"(~{_fmt_usd(cfg.risk.budget * cfg.risk.quote_to_usd)})")
    return 0


def cmd_analyze(cfg, args) -> int:
    from .analyzer import run_analysis
    res = run_analysis(cfg)
    print("\n== wallet analysis ==")
    hdr = (f"{'wallet':44} {'label':12} {'ROI':>8} {'win':>6} "
           f"{'rtrips':>7} {'pnl$':>10} {'weight':>7}")
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(res["reports"], key=lambda x: x.get("weight", 0),
                    reverse=True):
        if r.get("error"):
            print(f"{r['address']:44} {r.get('label',''):12} "
                  f"  {r['error']}  (weight {r['weight']})")
            continue
        print(f"{r['address']:44} {r.get('label',''):12} "
              f"{r['roi']*100:7.1f}% {r['win_rate']*100:5.0f}% "
              f"{r['round_trips']:7d} {r['pnl_usd']:10.0f} "
              f"{r['weight']:7.2f}")
    print("\nweights.json written next to the DB. `paper` will use it.")
    if not cfg.etherscan_key():
        print("\nNOTE: no ETHERSCAN_API_KEY set -> analysis was skipped. Get a "
              "free key at etherscan.io and `export "
              f"{cfg.etherscan_api_key_env}=...`")
    return 0


def cmd_paper(cfg, args) -> int:
    from .engine import Engine
    from .executor_paper import PaperExecutor
    engine = Engine(cfg, lambda c, r, l, p: PaperExecutor(c, r, l, p),
                    mode="paper", lookback_blocks=args.lookback)
    engine.run()
    return 0


def cmd_live(cfg, args) -> int:
    from .engine import Engine
    from .executor_live import LiveExecutor
    if not cfg.live.enabled:
        print("live mode is disabled. Set [live].enabled=true in the config "
              "AND launch with COPYTRADER_ARM=1 to send real transactions.")
        return 2
    if os.environ.get("COPYTRADER_ARM") != "1":
        print("refusing: COPYTRADER_ARM=1 not set. Running would only dry-run "
              "anyway. Set it explicitly to acknowledge real-money risk.")
    if not args.yes:
        print("re-run with --yes to confirm you understand this can spend real "
              "funds from the wallet in your private-key env var.")
        return 2
    engine = Engine(cfg, lambda c, r, l, p: LiveExecutor(c, r, l, p),
                    mode="live", lookback_blocks=args.lookback)
    engine.run()
    return 0


def cmd_livecheck(cfg, args) -> int:
    """Validate the LIVE plumbing (RPC + aggregator quote) from THIS machine,
    without a key, funds, or web3. Run this before ever arming live."""
    from .rpc import RpcClient
    from .prices import PriceFeed
    from .config import resolve_rpcs
    from .executor_live import _post_json, ODOS_QUOTE
    ck = cfg.live.chain
    chain = get_chain(ck)
    # checksummed WETH/USDC per chain (Odos requires EIP-55 addresses)
    pairs = {
        "arbitrum": ("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
                     "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
        "base": ("0x4200000000000000000000000000000000000006",
                 "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
        "ethereum": ("0xC02aaa39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                     "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    }
    print(f"== live plumbing check ({ck}) ==")
    try:
        bn = RpcClient(resolve_rpcs(cfg.rpc_overrides, ck)).block_number()
        print(f"  [OK ] RPC block {bn}")
    except Exception as exc:
        print(f"  [ERR] RPC: {exc}")
    md = PriceFeed().get(chain, chain.wrapped_native)
    print(f"  [OK ] native price ${md.price_usd:,.2f}" if md
          else "  [ERR] native price unavailable")
    if ck not in pairs:
        print(f"  no test pair configured for {ck}")
        return 0
    weth, usdc = pairs[ck]
    payload = {"chainId": chain.chain_id,
               "inputTokens": [{"tokenAddress": weth, "amount": str(int(1e16))}],
               "outputTokens": [{"tokenAddress": usdc, "proportion": 1}],
               "userAddr": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
               "slippageLimitPercent": 1.0}
    try:
        q = _post_json(ODOS_QUOTE, payload)
        out = q.get("outAmounts")
        print(f"  [OK ] Odos quote 0.01 WETH -> USDC: out={out} "
              f"impact={q.get('priceImpact')} gas~{q.get('gasEstimate')}")
        print("\n  live plumbing looks reachable. You still need: "
              "web3+eth-account, a funded wallet (WETH+gas), and to flip the "
              "gates. See README §7.")
    except Exception as exc:
        print(f"  [ERR] Odos quote: {exc}")
        print("        (if this is a datacenter/VPN IP, Cloudflare may block "
              "it; try from a normal connection.)")
    return 0


def cmd_hl_doctor(cfg, args) -> int:
    from .hl_api import HLInfo
    info = HLInfo()
    print("== Hyperliquid check ==")
    try:
        mids = info.all_mids()
        print(f"  [OK ] API reachable, {len(mids)} markets "
              f"(BTC=${float(mids.get('BTC', 0)):,.0f})")
    except Exception as exc:
        print(f"  [ERR] API: {exc}")
        return 1
    print(f"\n  budget: {cfg.hyperliquid.budget} {cfg.hyperliquid.quote_currency}"
          f" (~{_fmt_usd(cfg.hyperliquid.budget * cfg.hyperliquid.quote_to_usd)})"
          f" | max gross {cfg.hyperliquid.max_gross_leverage:.1f}x")
    print("\n  followed wallets (live positions):")
    for w in cfg.wallets:
        if not w.enabled:
            continue
        try:
            st = info.clearinghouse_state(w.address)
        except Exception as exc:
            print(f"   {w.address[:12]}  ERR {exc}")
            continue
        pos = ", ".join(f"{c} {p.side} {abs(p.szi):g}"
                        for c, p in st.positions.items()) or "(flat)"
        print(f"   {w.address[:12]} {w.label:10} acct ${st.account_value:,.0f}"
              f" | {pos}")
    return 0


def cmd_hl_analyze(cfg, args) -> int:
    from .hl_analyzer import run_hl_analysis
    res = run_hl_analysis(cfg)
    print("\n== Hyperliquid wallet analysis (recent fills) ==")
    hdr = (f"{'wallet':14}{'label':10}{'acct$':>11}{'netPnL$':>11}"
           f"{'ROI':>8}{'win':>6}{'closes':>7}{'weight':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(res["reports"], key=lambda x: x.get("weight", 0),
                    reverse=True):
        print(f"{r['address'][:14]:14}{r.get('label',''):10}"
              f"{r['account_value']:>11,.0f}{r['net_pnl']:>11,.0f}"
              f"{r['roi_vs_acct']*100:>7.1f}%{r['win_rate']*100:>5.0f}%"
              f"{r['closes']:>7}{r['weight']:>8.2f}")
    print("\nweights_hl.json written; `hl-paper` will use it.")
    return 0


def cmd_hl_paper(cfg, args) -> int:
    from .hl_engine import HLEngine
    from .hl_executor_paper import HLPaperExecutor
    HLEngine(cfg, lambda c, r, l, i: HLPaperExecutor(c, r, l, i),
             mode="paper").run()
    return 0


def cmd_hl_live(cfg, args) -> int:
    from .hl_engine import HLEngine
    from .hl_executor_live import HLLiveExecutor
    if not cfg.hl_live.enabled:
        print("HL live disabled. Set [hl_live].enabled=true AND launch with "
              "COPYTRADER_ARM=1 to send real orders.")
        return 2
    if not args.yes:
        print("re-run with --yes to confirm real-money risk on Hyperliquid.")
        return 2
    HLEngine(cfg, lambda c, r, l, i: HLLiveExecutor(c, r, l, i),
             mode="live").run()
    return 0


def _hl_ws_paths(cfg):
    import os
    base = os.path.dirname(cfg.hyperliquid.db_path) or "."
    return (os.path.join(base, "hl_ws.sqlite"),
            os.path.join(base, "hl_ws_reports"))


def cmd_hl_ws_paper(cfg, args) -> int:
    from .hl_ws_engine import HLWSEngine
    from .hl_executor_paper import HLPaperExecutor
    HLWSEngine(cfg, lambda c, r, l, i: HLPaperExecutor(c, r, l, i),
               mode="paper").run()
    return 0


def cmd_hl_ws_live(cfg, args) -> int:
    from .hl_ws_engine import HLWSEngine
    from .hl_executor_live import HLLiveExecutor
    if not cfg.hl_live.enabled:
        print("HL live disabled. Set [hl_live].enabled=true AND launch with "
              "COPYTRADER_ARM=1 to send real orders.")
        return 2
    if not args.yes:
        print("re-run with --yes to confirm real-money risk on Hyperliquid "
              "(WebSocket real-time).")
        return 2
    HLWSEngine(cfg, lambda c, r, l, i: HLLiveExecutor(c, r, l, i),
               mode="live").run()
    return 0


def cmd_hl_ws_report(cfg, args) -> int:
    db, csv = _hl_ws_paths(cfg)
    import os
    if not os.path.exists(db):
        print("pas encore de donnees WS (lance 'hl-ws-paper' d'abord).")
        return 0
    rc = _report(db, csv)
    try:
        from .hl_benchmark import compute, lines
        print("\n== benchmark WS : talent (alpha) vs marche+levier (beta) ==")
        for ln in lines(compute(db)):
            print("  " + ln)
    except Exception as exc:
        print(f"\n(benchmark indisponible: {exc})")
    return rc


def cmd_hl_report(cfg, args) -> int:
    rc = _report(cfg.hyperliquid.db_path, cfg.hyperliquid.csv_dir)
    try:
        from .hl_benchmark import compute, lines
        print("\n== benchmark : talent (alpha) vs marche+levier (beta) ==")
        for ln in lines(compute(cfg.hyperliquid.db_path)):
            print("  " + ln)
    except Exception as exc:
        print(f"\n(benchmark indisponible: {exc})")
    return rc


def _report(db_path: str, csv_dir: str) -> int:
    from .ledger import Ledger
    led = Ledger(db_path)
    s = led.summary()
    print("== performance ==")
    print(f"  closed trades : {s['closed_trades']}")
    print(f"  win-rate      : {s['win_rate']*100:.0f}% "
          f"({s['wins']}W / {s['losses']}L)")
    print(f"  net P&L       : {_fmt_usd(s['net_pnl_usd'])}")
    print(f"  total fees    : {_fmt_usd(s['total_fees_usd'])}")
    print("\n== per source wallet ==")
    for w in led.per_wallet():
        print(f"  {w['wallet']:44} trades {w['trades']:4d}  "
              f"pnl {_fmt_usd(w['pnl'])}")
    rows = led.conn.execute(
        "SELECT close_ts, token_symbol, source_wallets, cost_usd, "
        "proceeds_usd, pnl_usd, pnl_pct, exit_reason FROM trades "
        "ORDER BY close_ts DESC LIMIT 15").fetchall()
    if rows:
        print("\n== last closed trades ==")
        for r in rows:
            print(f"  {r['token_symbol']:10} cost {r['cost_usd']:7.2f} -> "
                  f"{r['proceeds_usd']:7.2f}  pnl {r['pnl_usd']:+7.2f} "
                  f"({r['pnl_pct']*100:+.0f}%)  {r['exit_reason']}")
    paths = led.export_csv(csv_dir)
    print(f"\nCSV exported to: {', '.join(paths)}")
    led.close()
    return 0


def cmd_report(cfg, args) -> int:
    return _report(cfg.db_path, cfg.csv_dir)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copytrader")
    p.add_argument("--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("analyze")
    sub.add_parser("livecheck")
    pp = sub.add_parser("paper")
    pp.add_argument("--lookback", type=int, default=0,
                    help="blocks of history to scan at startup (0=only new)")
    rp = sub.add_parser("report")
    lp = sub.add_parser("live")
    lp.add_argument("--yes", action="store_true")
    lp.add_argument("--lookback", type=int, default=0)
    # Hyperliquid perp copy-trader
    sub.add_parser("hl-doctor")
    sub.add_parser("hl-analyze")
    sub.add_parser("hl-paper")
    sub.add_parser("hl-report")
    sub.add_parser("hl-ws-paper")
    sub.add_parser("hl-ws-report")
    hlw = sub.add_parser("hl-ws-live")
    hlw.add_argument("--yes", action="store_true")
    hl = sub.add_parser("hl-live")
    hl.add_argument("--yes", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"config not found: {args.config}\n"
              "copy config.example.toml to config.toml and edit it.")
        return 2
    return {
        "doctor": cmd_doctor, "analyze": cmd_analyze, "paper": cmd_paper,
        "report": cmd_report, "live": cmd_live, "livecheck": cmd_livecheck,
        "hl-doctor": cmd_hl_doctor, "hl-analyze": cmd_hl_analyze,
        "hl-paper": cmd_hl_paper, "hl-report": cmd_hl_report,
        "hl-live": cmd_hl_live, "hl-ws-paper": cmd_hl_ws_paper,
        "hl-ws-report": cmd_hl_ws_report, "hl-ws-live": cmd_hl_ws_live,
    }[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
