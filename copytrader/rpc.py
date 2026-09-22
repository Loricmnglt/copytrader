"""Minimal, dependency-free JSON-RPC client over urllib with endpoint
failover and light retry. One RpcClient instance per chain.

Deliberately synchronous and simple: the monitor polls on a timer, so we do
not need async here, and staying stdlib-only keeps the paper engine trivial
to run (no pip install)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

_HEADERS = {"content-type": "application/json", "user-agent": "copytrader/0.1"}


class RpcError(RuntimeError):
    pass


class RpcClient:
    def __init__(self, endpoints: list[str], timeout: float = 15.0,
                 max_retries: int = 2):
        if not endpoints:
            raise ValueError("RpcClient needs at least one endpoint")
        self.endpoints = list(endpoints)
        self.timeout = timeout
        self.max_retries = max_retries
        self._id = 0
        self._active = 0  # index of the endpoint we currently prefer

    # -- low level ---------------------------------------------------------
    def _post(self, endpoint: str, payload: Any) -> Any:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(endpoint, data=data, headers=_HEADERS,
                                     method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def _rotate(self) -> None:
        self._active = (self._active + 1) % len(self.endpoints)

    # -- public ------------------------------------------------------------
    def call(self, method: str, params: list | None = None) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method,
                   "params": params or []}
        last_err: Exception | None = None
        # try every endpoint, starting from the currently preferred one
        for attempt in range(len(self.endpoints) * (self.max_retries + 1)):
            endpoint = self.endpoints[self._active]
            try:
                out = self._post(endpoint, payload)
                if isinstance(out, dict) and out.get("error"):
                    raise RpcError(f"{method}: {out['error']}")
                return out["result"]
            except (urllib.error.URLError, TimeoutError, RpcError,
                    ValueError, KeyError) as exc:
                last_err = exc
                self._rotate()
                time.sleep(min(0.4 * (attempt + 1), 2.0))
        raise RpcError(f"all endpoints failed for {method}: {last_err}")

    def batch(self, calls: list[tuple[str, list]]) -> list[Any]:
        """Send several calls in one HTTP round-trip. Returns results in the
        same order. Falls back to sequential calls if the node rejects the
        batch."""
        if not calls:
            return []
        payload = []
        for i, (method, params) in enumerate(calls):
            payload.append({"jsonrpc": "2.0", "id": i, "method": method,
                            "params": params or []})
        for attempt in range(len(self.endpoints)):
            endpoint = self.endpoints[self._active]
            try:
                out = self._post(endpoint, payload)
                if not isinstance(out, list):
                    raise RpcError(f"batch: unexpected response {out!r}")
                by_id = {item["id"]: item for item in out}
                results = []
                for i in range(len(calls)):
                    item = by_id.get(i)
                    if item is None or item.get("error"):
                        raise RpcError(f"batch item {i}: "
                                       f"{item.get('error') if item else 'missing'}")
                    results.append(item["result"])
                return results
            except (urllib.error.URLError, TimeoutError, RpcError,
                    ValueError, KeyError):
                self._rotate()
        # fallback: one by one (slower but resilient)
        return [self.call(m, p) for m, p in calls]

    # -- convenience -------------------------------------------------------
    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def get_logs(self, from_block: int, to_block: int,
                 topics: list) -> list[dict]:
        return self.call("eth_getLogs", [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "topics": topics,
        }])

    def get_tx(self, tx_hash: str) -> dict | None:
        return self.call("eth_getTransactionByHash", [tx_hash])

    def get_receipt(self, tx_hash: str) -> dict | None:
        return self.call("eth_getTransactionReceipt", [tx_hash])

    def get_code(self, address: str) -> str:
        return self.call("eth_getCode", [address, "latest"])

    def eth_call(self, to: str, data: str) -> str:
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])
