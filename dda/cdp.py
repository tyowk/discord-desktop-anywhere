"""Chrome DevTools Protocol client, used for sharp HD screenshots and page reloads."""

from __future__ import annotations

import base64
import json
import urllib.request
from typing import Any, Dict, Optional

from dda import wsproto


class CDPError(RuntimeError):
    pass


def find_page_ws(port: int) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as r:
            items = json.load(r)
    except OSError as exc:
        raise CDPError(f"debug port {port} is not reachable (is Discord running and SCREENSHOT=1?)") from exc
    pages = [
        i
        for i in items
        if i.get("type") == "page" and i.get("webSocketDebuggerUrl") and not i.get("url", "").startswith("devtools://")
    ]
    if not pages:
        raise CDPError("Discord page not found (has Discord finished loading?)")
    pages.sort(key=lambda i: 0 if "discord" in i.get("url", "") else 1)
    return pages[0]["webSocketDebuggerUrl"]


class CDPSession:
    def __init__(self, port: int, timeout: float = 90.0) -> None:
        self.ws = wsproto.connect(find_page_ws(port), timeout=timeout)
        self._id = 0

    def __enter__(self) -> "CDPSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.ws.sock.close()
        except OSError:
            pass

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv_message().decode("utf-8"))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(msg["error"].get("message", "CDP error"))
                return msg.get("result", {})


def capture_png(port: int, target_width: int) -> "tuple[bytes, int, int]":
    """Re-render the page at a larger device scale (real detail, not an upscale)."""
    with CDPSession(port) as cdp:
        metrics = cdp.call("Page.getLayoutMetrics")
        vp = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or metrics.get("layoutViewport")
        width = int(vp.get("clientWidth") or vp.get("width"))
        height = int(vp.get("clientHeight") or vp.get("height"))
        scale = max(1.0, float(target_width) / width)
        res = cdp.call(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": scale},
            },
        )
    return base64.b64decode(res["data"]), int(round(width * scale)), int(round(height * scale))


def reload_page(port: int, hard: bool = False) -> None:
    with CDPSession(port, timeout=15) as cdp:
        cdp.call("Page.reload", {"ignoreCache": hard})
