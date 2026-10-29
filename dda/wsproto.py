"""Minimal RFC 6455 WebSocket implementation (client and server side).

Used for the browser <-> VNC proxy (server role) and for talking to the Chrome
DevTools Protocol (client role). Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from typing import Optional, Tuple
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_FRAME = 16 * 1024 * 1024


class WebSocketError(IOError):
    """Protocol violation or failed handshake."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection."""


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def mask_bytes(data: bytes, mask: bytes) -> bytes:
    """XOR ``data`` with a repeating 4-byte mask (big-int trick: fast in pure Python)."""
    n = len(data)
    if not n:
        return b""
    full = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(full, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes, mask: bool = False, fin: bool = True) -> bytes:
    head = bytearray([(0x80 if fin else 0) | opcode])
    length = len(payload)
    flag = 0x80 if mask else 0
    if length < 126:
        head.append(flag | length)
    elif length < 65536:
        head.append(flag | 126)
        head += struct.pack(">H", length)
    else:
        head.append(flag | 127)
        head += struct.pack(">Q", length)
    if mask:
        key = os.urandom(4)
        return bytes(head) + key + mask_bytes(payload, key)
    return bytes(head) + payload


class WebSocket:
    """A connected WebSocket. ``client=True`` masks outgoing frames as the RFC requires."""

    def __init__(self, sock: socket.socket, client: bool, initial: bytes = b"") -> None:
        self.sock = sock
        self.client = client
        self._buf = bytearray(initial)
        self._send_lock = threading.Lock()
        self.closed = False

    # -- reading ----------------------------------------------------------
    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WebSocketClosed("connection lost")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv_frame(self) -> Tuple[bool, int, bytes]:
        """Return ``(fin, opcode, payload)`` for the next frame, unmasked."""
        b1, b2 = self._read_exact(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        masked, length = bool(b2 & 0x80), b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if length > MAX_FRAME:
            raise WebSocketError("frame too large")
        if opcode >= 0x8 and (length > 125 or not fin):
            raise WebSocketError("invalid control frame")
        key = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if key:
            payload = mask_bytes(payload, key)
        return fin, opcode, payload

    def recv_data(self) -> Tuple[int, bytes]:
        """Next data frame (opcode, payload); pings/pongs/close are handled here."""
        while True:
            _fin, op, payload = self.recv_frame()
            if op == OP_PING:
                self.send(OP_PONG, payload)
            elif op == OP_PONG:
                continue
            elif op == OP_CLOSE:
                self.close()
                raise WebSocketClosed("closed by peer")
            else:
                return op, payload

    def recv_message(self) -> bytes:
        """A complete (possibly fragmented) message."""
        parts = []
        while True:
            fin, op, payload = self.recv_frame()
            if op == OP_PING:
                self.send(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                self.close()
                raise WebSocketClosed("closed by peer")
            parts.append(payload)
            if fin:
                return b"".join(parts)

    # -- writing ----------------------------------------------------------
    def send(self, opcode: int, payload: bytes = b"") -> None:
        frame = encode_frame(opcode, payload, mask=self.client)
        with self._send_lock:
            self.sock.sendall(frame)

    def send_text(self, text: str) -> None:
        self.send(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self.send(OP_BINARY, data)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.send(OP_CLOSE, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def connect(url: str, timeout: float = 30.0) -> WebSocket:
    """Open a client connection to a ``ws://`` URL."""
    u = urlparse(url)
    if u.scheme != "ws" or not u.hostname:
        raise WebSocketError(f"only ws:// URLs are supported, got {url!r}")
    port = u.port or 80
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("handshake failed: connection closed")
            buf += chunk
            if len(buf) > 65536:
                raise WebSocketError("handshake response too large")
        head, _, rest = buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise WebSocketError("handshake rejected: " + status.decode("latin-1"))
        return WebSocket(sock, client=True, initial=rest)
    except Exception:
        sock.close()
        raise


def optional_subprotocol(header: Optional[str], wanted: str) -> Optional[str]:
    """Return ``wanted`` if the client offered it in ``Sec-WebSocket-Protocol``."""
    if not header:
        return None
    offered = [p.strip() for p in header.split(",")]
    return wanted if wanted in offered else None
