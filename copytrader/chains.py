"""Static per-chain metadata: ids, public RPC endpoints, wrapped-native and
stablecoin ("base asset") addresses used to detect the fiat/native leg of a
swap.

Addresses are stored lowercase because log topics and eth_call results are
compared lowercased throughout the codebase (we never rely on EIP-55
checksums for matching).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Chain:
    key: str                 # our internal key, e.g. "arbitrum"
    chain_id: int            # EVM chain id
    dexscreener_id: str      # DexScreener "chainId" string
    wrapped_native: str      # WETH / WBNB address (lowercase)
    native_symbol: str       # "ETH" / "BNB"
    stables: frozenset       # stablecoin addresses (lowercase)
    rpcs: tuple              # ordered list of public RPC endpoints (failover)
    block_time: float        # approx seconds per block (for range sizing)
    explorer: str            # human explorer base url

    @property
    def base_assets(self) -> frozenset:
        """All addresses that count as the 'money' leg of a swap."""
        return frozenset({self.wrapped_native}) | self.stables


# NOTE: public RPCs are best-effort and rate-limited. For production use,
# put your own endpoints (Alchemy/Infura/QuickNode) in config.toml under
# [chains.<key>].rpcs — they override these defaults.
CHAINS: dict[str, Chain] = {
    "arbitrum": Chain(
        key="arbitrum",
        chain_id=42161,
        dexscreener_id="arbitrum",
        wrapped_native="0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
        native_symbol="ETH",
        stables=frozenset({
            "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC (native)
            "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",  # USDC.e (bridged)
            "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
            "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",  # DAI
        }),
        rpcs=(
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum-one-rpc.publicnode.com",
            "https://arbitrum.llamarpc.com",
        ),
        block_time=0.25,
        explorer="https://arbiscan.io",
    ),
    "base": Chain(
        key="base",
        chain_id=8453,
        dexscreener_id="base",
        wrapped_native="0x4200000000000000000000000000000000000006",
        native_symbol="ETH",
        stables=frozenset({
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
        }),
        rpcs=(
            "https://mainnet.base.org",
            "https://base-rpc.publicnode.com",
            "https://base.llamarpc.com",
        ),
        block_time=2.0,
        explorer="https://basescan.org",
    ),
    "ethereum": Chain(
        key="ethereum",
        chain_id=1,
        dexscreener_id="ethereum",
        wrapped_native="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        native_symbol="ETH",
        stables=frozenset({
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
            "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
            "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        }),
        rpcs=(
            "https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
            "https://rpc.mevblocker.io",
        ),
        block_time=12.0,
        explorer="https://etherscan.io",
    ),
    "bsc": Chain(
        key="bsc",
        chain_id=56,
        dexscreener_id="bsc",
        wrapped_native="0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
        native_symbol="BNB",
        stables=frozenset({
            "0x55d398326f99059ff775485246999027b3197955",  # USDT
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
            "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
        }),
        rpcs=(
            "https://bsc-dataseed.bnbchain.org",
            "https://bsc-rpc.publicnode.com",
            "https://binance.llamarpc.com",
        ),
        block_time=3.0,
        explorer="https://bscscan.com",
    ),
}


def get_chain(key: str) -> Chain:
    try:
        return CHAINS[key]
    except KeyError:
        raise KeyError(f"unknown chain '{key}'. known: {sorted(CHAINS)}")
