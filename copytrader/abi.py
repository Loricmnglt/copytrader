"""Just enough ABI machinery to (a) build log filters and (b) read ERC-20
metadata via eth_call. No keccak needed at runtime: every selector and the
Transfer topic are well-known constants, hardcoded below."""
from __future__ import annotations

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55"
                  "a4df523b3ef")

# ERC-20 function selectors (first 4 bytes of keccak of the signature)
SEL_SYMBOL = "0x95d89b41"    # symbol()
SEL_NAME = "0x06fdde03"      # name()
SEL_DECIMALS = "0x313ce567"  # decimals()
SEL_TOTALSUPPLY = "0x18160ddd"  # totalSupply()
SEL_BALANCEOF = "0x70a08231"    # balanceOf(address)
SEL_ALLOWANCE = "0xdd62ed3e"    # allowance(address,address)
SEL_APPROVE = "0x095ea7b3"      # approve(address,uint256)

MAX_UINT256 = (1 << 256) - 1


def _pad_addr(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _pad_uint(value: int) -> str:
    return f"{value:064x}"


def encode_balance_of(owner: str) -> str:
    return SEL_BALANCEOF + _pad_addr(owner)


def encode_allowance(owner: str, spender: str) -> str:
    return SEL_ALLOWANCE + _pad_addr(owner) + _pad_addr(spender)


def encode_approve(spender: str, amount: int = MAX_UINT256) -> str:
    return SEL_APPROVE + _pad_addr(spender) + _pad_uint(amount)


def addr_to_topic(address: str) -> str:
    """Left-pad a 20-byte address to a 32-byte log topic."""
    a = address.lower().removeprefix("0x")
    return "0x" + a.rjust(64, "0")


def topic_to_addr(topic: str) -> str:
    """Extract the 20-byte address from a 32-byte topic."""
    t = topic.lower().removeprefix("0x")
    return "0x" + t[-40:]


def decode_uint(hexdata: str) -> int:
    h = hexdata.removeprefix("0x")
    return int(h, 16) if h else 0


def decode_string(hexdata: str) -> str:
    """Decode an eth_call return that is either an ABI dynamic string or an
    old-style bytes32 (some tokens, e.g. MKR, return bytes32 for symbol)."""
    h = hexdata.removeprefix("0x")
    if not h:
        return ""
    raw = bytes.fromhex(h)
    # dynamic string layout: [offset(32)][length(32)][data...]
    if len(raw) >= 64:
        try:
            offset = int.from_bytes(raw[0:32], "big")
            if offset == 32 and len(raw) >= 64:
                length = int.from_bytes(raw[32:64], "big")
                if 0 < length <= len(raw) - 64:
                    return raw[64:64 + length].decode("utf-8", "replace")
        except Exception:
            pass
    # fallback: bytes32, strip trailing zeros
    return raw.rstrip(b"\x00").decode("utf-8", "replace")
