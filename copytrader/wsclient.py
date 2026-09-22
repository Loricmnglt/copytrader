"""Minimal WebSocket client (RFC 6455) using only the standard library
(socket + ssl). Enough to talk to the Hyperliquid WS API: TLS connect, HTTP
upgrade handshake, send masked text frames (subscribe/ping), receive frames,
answer server pings. No pip install required.

Not a general-purpose implementation — it covers exactly what the copy engine
needs: text messages, ping/pong, close, and basic continuation frames."""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlparse

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WSClosed(Exception):
    pass


class WSClient:
    def __init__(self, url: str, timeout: float = 40.0):
        u = urlparse(url)
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == "wss" else 80)
        self.path = u.path or "/"
        self.secure = u.scheme == "wss"
        self.timeout = timeout
        self.sock: ssl.SSLSocket | socket.socket | None = None
        self._buf = b""

    # -- connection --------------------------------------------------------
    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=15)
        if self.secure:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        else:
            self.sock = raw
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\n"
               f"Host: {self.host}\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = self._read_until(b"\r\n\r\n")
        if b" 101 " not in resp:
            raise WSClosed(f"handshake failed: {resp[:120]!r}")

    def _read_until(self, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSClosed("closed during handshake")
            data += chunk
        return data

    # -- low level frames --------------------------------------------------
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSClosed("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        ln = len(payload)
        if ln < 126:
            header += bytes([0x80 | ln])
        elif ln < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", ln)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", ln)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode())

    def _read_frame(self):
        b0, b1 = self._recv_exact(2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(ln) if ln else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    # -- public: one logical message --------------------------------------
    def recv(self) -> str | None:
        """Return the next text message, or None for a control frame handled
        internally (ping answered, pong ignored)."""
        fin, opcode, payload = self._read_frame()
        if opcode == OP_PING:
            self._send_frame(OP_PONG, payload)
            return None
        if opcode == OP_PONG:
            return None
        if opcode == OP_CLOSE:
            raise WSClosed("server sent close")
        # text/binary, possibly fragmented
        data = payload
        while not fin:
            fin, opcode, payload = self._read_frame()
            data += payload
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
