"""Password login, stateless signed session cookies and a tiny brute-force limiter."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from typing import Dict, List, Optional

COOKIE_NAME = "dda_session"
SESSION_TTL = 7 * 24 * 3600


class Auth:
    def __init__(self, password: str, ttl: int = SESSION_TTL) -> None:
        self.password = password
        self.enabled = bool(password)
        self.ttl = ttl
        self._key = hashlib.sha256(b"dda-session-v1\0" + password.encode()).digest()

    def check_password(self, candidate: str) -> bool:
        if not self.enabled:
            return True
        return hmac.compare_digest(candidate.encode(), self.password.encode())

    def _sign(self, expiry: int) -> str:
        return hmac.new(self._key, str(expiry).encode(), hashlib.sha256).hexdigest()

    def make_token(self, now: Optional[float] = None) -> str:
        expiry = int((now if now is not None else time.time()) + self.ttl)
        return f"v1.{expiry}.{self._sign(expiry)}"

    def verify_token(self, token: Optional[str], now: Optional[float] = None) -> bool:
        if not self.enabled:
            return True
        if not token:
            return False
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "v1" or not parts[1].isdigit():
            return False
        expiry = int(parts[1])
        if expiry < (now if now is not None else time.time()):
            return False
        return hmac.compare_digest(parts[2], self._sign(expiry))


def parse_cookies(header: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for piece in (header or "").split(";"):
        name, sep, value = piece.strip().partition("=")
        if sep:
            out[name] = value
    return out


class RateLimiter:
    """Allow ``limit`` failures per ``window`` seconds per key, then lock the key out."""

    def __init__(self, limit: int = 5, window: float = 300.0) -> None:
        self.limit, self.window = limit, window
        self._fails: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> List[float]:
        recent = [t for t in self._fails.get(key, []) if now - t < self.window]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def blocked(self, key: str, now: Optional[float] = None) -> bool:
        with self._lock:
            return len(self._recent(key, now if now is not None else time.monotonic())) >= self.limit

    def fail(self, key: str, now: Optional[float] = None) -> None:
        with self._lock:
            t = now if now is not None else time.monotonic()
            self._recent(key, t)
            self._fails.setdefault(key, []).append(t)

    def success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
