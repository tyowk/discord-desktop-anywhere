"""Shared, thread-safe runtime state shown in the web UI."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict


class AppState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self._phase = "starting"
        self._message = "Starting..."
        self.discord_running = False

    def set(self, phase: str, message: str = "") -> None:
        with self._lock:
            self._phase, self._message = phase, message

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"phase": self._phase, "message": self._message, "uptime": int(time.time() - self.started_at)}
