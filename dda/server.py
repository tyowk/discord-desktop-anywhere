"""The web gateway: one port serving the UI, noVNC, the VNC WebSocket proxy, audio and the API."""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import os
import socket
import subprocess
import threading
import time
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

from dda import __version__, cdp, wsproto
from dda.audio import AudioBusy, AudioManager
from dda.auth import COOKIE_NAME, Auth, RateLimiter, parse_cookies
from dda.config import Config
from dda.shots import ShotStore
from dda.state import AppState

log = logging.getLogger("dda.server")

WEB_DIR = Path(__file__).resolve().parent / "web"
MIME = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
PAGE_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "media-src 'self' blob:; connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'self'"
)
ACTION_KEYS = {
    "esc": "Escape",
    "devtools": "ctrl+shift+i",
    "console": "ctrl+shift+j",
    "reload": "ctrl+r",
    "hardreload": "ctrl+shift+r",
}
MAX_BODY = 16 * 1024


class Context:
    """Everything request handlers need (injected, so tests can fake it)."""

    def __init__(
        self,
        cfg: Config,
        auth: Auth,
        state: AppState,
        audio: AudioManager,
        shots: Optional[ShotStore],
        novnc_dir: Optional[Path],
        display: Callable[[], str],
    ) -> None:
        self.cfg, self.auth, self.state, self.audio, self.shots = cfg, auth, state, audio, shots
        self.novnc_dir = novnc_dir
        self.display = display
        self.limiter = RateLimiter()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, addr: Tuple[str, int], ctx: Context) -> None:
        self.ctx = ctx
        super().__init__(addr, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"dda/{__version__}"
    sys_version = ""
    timeout = 120

    # ------------------------------------------------------------ plumbing
    @property
    def ctx(self) -> Context:
        return self.server.ctx  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def client_ip(self) -> str:
        if self.ctx.cfg.trust_proxy:
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def is_https(self) -> bool:
        return self.ctx.cfg.trust_proxy and self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"

    def send_bytes(
        self,
        body: bytes,
        ctype: str = "text/html; charset=utf-8",
        code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        page: bool = False,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if page:
            self.send_header("Content-Security-Policy", PAGE_CSP)
        if "Cache-Control" not in (headers or {}):
            self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj: Any, code: int = 200, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_bytes(json.dumps(obj).encode(), "application/json", code, headers)

    def fail(self, code: int, message: str) -> None:
        self.send_json({"ok": False, "error": message}, code)

    def redirect(self, location: str, headers: Optional[Dict[str, str]] = None) -> None:
        h = {"Location": location}
        h.update(headers or {})
        self.send_bytes(b"", "text/plain", 302, h)

    def read_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            raise ValueError("bad body size")
        return self.rfile.read(n) if n else b""

    # ------------------------------------------------------------ security
    def origin_ok(self) -> bool:
        """Reject cross-site requests (WebSocket hijacking / CSRF) when an Origin is sent."""
        if self.ctx.cfg.allow_cross_site:
            return True
        origin = self.headers.get("Origin")
        if not origin:
            return True
        origin = origin.rstrip("/")
        if origin in self.ctx.cfg.allowed_origins:
            return True
        host = self.headers.get("X-Forwarded-Host") if self.ctx.cfg.trust_proxy else None
        host = (host or self.headers.get("Host") or "").split(",")[0].strip()
        parsed = urlparse(origin)
        return bool(host) and parsed.netloc == host

    def session_cookie(self, value: str, max_age: int) -> str:
        secure = self.is_https()
        flags = "Secure; SameSite=None; Partitioned" if secure else "SameSite=Lax"
        return f"{COOKIE_NAME}={value}; Path=/; HttpOnly; Max-Age={max_age}; {flags}"

    def authenticated(self) -> bool:
        auth = self.ctx.auth
        return (not auth.enabled) or auth.verify_token(parse_cookies(self.headers.get("Cookie")).get(COOKIE_NAME))

    # ------------------------------------------------------------ routing
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path, params = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path == "/healthz":
                return self.send_json({"ok": True, "version": __version__})
            if path == "/login":
                return self.page_login(params)
            if path == "/logout":
                return self.redirect("/login", {"Set-Cookie": self.session_cookie("", 0)})
            if path.startswith("/assets/"):
                return self.serve_static(WEB_DIR, path[len("/assets/") :], cache=True)
            # legacy v1 style: /?key=PASSWORD logs in and strips the secret from the URL
            if "key" in params and self.ctx.auth.enabled and not self.ctx.limiter.blocked(self.client_ip()):
                if self.ctx.auth.check_password(params["key"]):
                    token = self.ctx.auth.make_token()
                    return self.redirect(
                        path if path != "/websockify" else "/",
                        {"Set-Cookie": self.session_cookie(token, self.ctx.auth.ttl)},
                    )
                self.ctx.limiter.fail(self.client_ip())
            if not self.authenticated():
                if path in ("/", "/panel", "/index.html"):
                    return self.redirect("/login?next=" + quote(path))
                return self.fail(401, "authentication required")
            if path in ("/", "/index.html"):
                return self.page_index(panel=False)
            if path == "/panel":
                return self.page_index(panel=True)
            if path == "/websockify":
                return self.proxy_vnc()
            if path == "/audio":
                return self.stream_audio(params)
            if path == "/api/status":
                return self.api_status()
            if path == "/api/shots":
                return self.send_json({"ok": True, "shots": self.ctx.shots.list() if self.ctx.shots else []})
            if path.startswith("/shots/"):
                return self.serve_shot(path[len("/shots/") :], params.get("dl") == "1")
            if path.startswith("/novnc/") and self.ctx.novnc_dir:
                return self.serve_static(self.ctx.novnc_dir, path[len("/novnc/") :], cache=True)
            return self.fail(404, "not found")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:  # noqa: BLE001 - never kill the server thread
            log.exception("unhandled error for %s", self.path)
            self.close_connection = True
            try:
                self.fail(500, "internal error")
            except OSError:
                pass

    def do_POST(self) -> None:  # noqa: N802
        self.api_write()

    def do_DELETE(self) -> None:  # noqa: N802
        self.api_write()

    def api_write(self) -> None:
        path = urlparse(self.path).path
        try:
            if self.command == "POST" and path == "/login":
                return self.post_login()
            if not self.authenticated():
                return self.fail(401, "authentication required")
            if not self.ctx.cfg.allow_cross_site and (
                self.headers.get("X-DDA-Request") != "1" or not self.origin_ok()
            ):
                return self.fail(403, "cross-site request blocked")
            if self.command == "POST" and path == "/api/action":
                return self.api_action()
            if self.command == "POST" and path == "/api/shot":
                return self.api_shot()
            if self.command == "DELETE" and path.startswith("/api/shots/") and self.ctx.shots:
                ok = self.ctx.shots.delete(path[len("/api/shots/") :])
                return self.send_json({"ok": ok}, 200 if ok else 404)
            return self.fail(404, "not found")
        except ValueError as exc:
            self.fail(400, str(exc))
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            log.exception("unhandled error for %s %s", self.command, self.path)
            self.fail(500, "internal error")

    # ------------------------------------------------------------ pages
    def _template(self, name: str) -> str:
        return (WEB_DIR / name).read_text(encoding="utf-8")

    def page_login(self, params: Dict[str, str], error: str = "", code: int = 200) -> None:
        if self.authenticated() and not error:
            return self.redirect(params.get("next") or "/")
        nxt = params.get("next", "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        body = (
            self._template("login.html")
            .replace("{{NEXT}}", html.escape(nxt, quote=True))
            .replace("{{ERROR}}", html.escape(error))
        )
        self.send_bytes(body.encode(), code=code, page=True)

    def post_login(self) -> None:
        ip = self.client_ip()
        if not self.ctx.cfg.allow_cross_site and not self.origin_ok():
            return self.fail(403, "cross-site request blocked")
        if self.ctx.limiter.blocked(ip):
            return self.page_login({}, "Too many attempts. Try again in a few minutes.", 429)
        form = parse_qs(self.read_body().decode("utf-8", "replace"))
        password = (form.get("password") or [""])[0]
        nxt = (form.get("next") or ["/"])[0]
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if self.ctx.auth.check_password(password):
            self.ctx.limiter.success(ip)
            token = self.ctx.auth.make_token()
            return self.redirect(nxt, {"Set-Cookie": self.session_cookie(token, self.ctx.auth.ttl)})
        self.ctx.limiter.fail(ip)
        time.sleep(0.5)
        self.page_login({"next": nxt}, "Wrong password.", 401)

    def page_index(self, panel: bool) -> None:
        cfg = self.ctx.cfg
        vnc = (
            "/novnc/vnc.html?autoconnect=true&resize=scale&reconnect=true&reconnect_delay=2000"
            "&show_dot=true&path=websockify"
        )
        conf = {
            "audio": self.ctx.audio.available,
            "audioFormat": self.ctx.audio.fmt,
            "devtools": cfg.devtools,
            "screenshot": cfg.screenshot,
            "panel": panel,
            "vnc": vnc,
            "auth": self.ctx.auth.enabled,
            "version": __version__,
        }
        blob = json.dumps(conf).replace("<", "\\u003c")
        body = self._template("index.html").replace("{{CONFIG}}", blob)
        self.send_bytes(body.encode(), page=True)

    # ------------------------------------------------------------ static
    def serve_static(self, root: Path, rel: str, cache: bool) -> None:
        rel = rel.split("?", 1)[0]
        target = os.path.realpath(os.path.join(str(root), rel.lstrip("/")))
        base = os.path.realpath(str(root))
        if not (target == base or target.startswith(base + os.sep)) or not os.path.isfile(target):
            return self.fail(404, "not found")
        st = os.stat(target)
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        headers = {
            "ETag": etag,
            "Last-Modified": formatdate(st.st_mtime, usegmt=True),
            "Cache-Control": "private, max-age=300" if cache else "no-store",
        }
        if self.headers.get("If-None-Match") == etag:
            return self.send_bytes(b"", "text/plain", 304, headers)
        ims = self.headers.get("If-Modified-Since")
        if ims:
            try:
                if parsedate_to_datetime(ims).timestamp() >= int(st.st_mtime):
                    return self.send_bytes(b"", "text/plain", 304, headers)
            except (TypeError, ValueError):
                pass
        ext = os.path.splitext(target)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            self.send_bytes(fh.read(), ctype, 200, headers)

    def serve_shot(self, name: str, download: bool) -> None:
        p = self.ctx.shots.path(os.path.basename(name)) if self.ctx.shots else None
        if not p:
            return self.fail(404, "not found")
        headers = {"Cache-Control": "private, max-age=3600"}
        if download:
            headers["Content-Disposition"] = f'attachment; filename="{p.name}"'
        self.send_bytes(p.read_bytes(), "image/png", 200, headers)

    # ------------------------------------------------------------ API
    def api_status(self) -> None:
        ctx = self.ctx
        snap = ctx.state.snapshot()
        snap.update(
            {
                "ok": True,
                "version": __version__,
                "discord": ctx.state.discord_running,
                "audio": {
                    "available": ctx.audio.available,
                    "reason": ctx.audio.reason,
                    "format": ctx.audio.fmt,
                    "listeners": ctx.audio.listeners,
                },
                "devtools": ctx.cfg.devtools,
                "screenshot": ctx.cfg.screenshot,
                "resolution": str(ctx.cfg.resolution),
            }
        )
        self.send_json(snap)

    def _json_body(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.read_body() or b"{}")
        except json.JSONDecodeError:
            raise ValueError("invalid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def api_action(self) -> None:
        cfg = self.ctx.cfg
        name = str(self._json_body().get("name", ""))
        if name not in ACTION_KEYS:
            return self.fail(404, "unknown action")
        if name in ("devtools", "console") and not cfg.devtools:
            return self.fail(400, "DevTools are disabled (set DEVTOOLS=1)")
        if name in ("reload", "hardreload") and cfg.screenshot:
            try:  # CDP reload is more reliable than a synthetic key press
                cdp.reload_page(cfg.cdp_port, hard=name == "hardreload")
                return self.send_json({"ok": True, "message": "Reloaded"})
            except (cdp.CDPError, OSError) as exc:
                log.debug("CDP reload failed (%s), falling back to xdotool", exc)
        self.send_json({"ok": True, "message": self._send_key(ACTION_KEYS[name])})

    def _send_key(self, combo: str) -> str:
        env = dict(os.environ, DISPLAY=self.ctx.display())
        try:
            found = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--name", "Discord"],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.split()
            if not found:
                return "Discord window not found"
            subprocess.run(["xdotool", "windowfocus", "--sync", found[-1]], env=env, timeout=5, capture_output=True)
            subprocess.run(["xdotool", "key", "--clearmodifiers", combo], env=env, timeout=5, capture_output=True)
            return "Sent " + combo
        except (OSError, subprocess.SubprocessError) as exc:
            return f"Failed: {exc}"

    def api_shot(self) -> None:
        if not self.ctx.shots:
            return self.fail(503, "screenshots unavailable")
        body = self._json_body()
        mode = body.get("mode", "hd")
        try:
            width = int(body.get("width", 3840))
        except (TypeError, ValueError):
            raise ValueError("width must be an integer") from None
        try:
            if mode == "hd":
                if not self.ctx.cfg.screenshot:
                    return self.fail(400, "HD screenshots are disabled (SCREENSHOT=0)")
                result = self.ctx.shots.capture_hd(width)
            elif mode == "x11":
                result = self.ctx.shots.capture_x11(width)
            else:
                return self.fail(400, "mode must be hd or x11")
        except Exception as exc:  # noqa: BLE001 - report capture errors to the UI
            hint = " | fallback: use the native full-screen button" if mode == "hd" else ""
            return self.fail(500, f"{type(exc).__name__}: {exc}{hint}")
        result["ok"] = True
        self.send_json(result)

    # ------------------------------------------------------------ audio
    def stream_audio(self, params: Dict[str, str]) -> None:
        audio = self.ctx.audio
        if not audio.available:
            return self.fail(503, audio.reason or "audio unavailable")
        try:
            stream = audio.open_stream(params.get("fmt"))
        except AudioBusy:
            return self.fail(429, "too many listeners")
        except Exception as exc:  # noqa: BLE001
            return self.fail(500, f"could not start audio: {exc}")
        self.close_connection = True
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.settimeout(30)
            self.send_response(200)
            self.send_header("Content-Type", stream.content_type)  # type: ignore[attr-defined]
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in stream:
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
            pass
        finally:
            stream.close()

    # ------------------------------------------------------------ VNC proxy
    def proxy_vnc(self) -> None:
        h = self.headers
        key = h.get("Sec-WebSocket-Key")
        if "websocket" not in h.get("Upgrade", "").lower() or not key:
            return self.fail(400, "WebSocket upgrade required")
        if not self.ctx.cfg.allow_cross_site and not self.origin_ok():
            return self.fail(403, "cross-site WebSocket blocked")
        try:
            upstream = socket.create_connection(("127.0.0.1", self.ctx.cfg.vnc_port), timeout=5)
        except OSError:
            return self.fail(503, "desktop is not ready yet")
        upstream.settimeout(None)
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.close_connection = True
        sock = self.connection
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        lines = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Accept: {wsproto.accept_key(key)}",
        ]
        sub = wsproto.optional_subprotocol(h.get("Sec-WebSocket-Protocol"), "binary")
        if sub:
            lines.append(f"Sec-WebSocket-Protocol: {sub}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        sock.settimeout(None)
        ws = wsproto.WebSocket(sock, client=False)

        def vnc_to_browser() -> None:
            try:
                while True:
                    data = upstream.recv(65536)
                    if not data:
                        break
                    ws.send_binary(data)
            except OSError:
                pass
            finally:
                ws.close()

        pump = threading.Thread(target=vnc_to_browser, name="vnc->ws", daemon=True)
        pump.start()
        try:
            while True:
                op, payload = ws.recv_data()
                if op in (wsproto.OP_BINARY, wsproto.OP_TEXT, wsproto.OP_CONT):
                    upstream.sendall(payload)
        except (wsproto.WebSocketError, OSError):
            pass
        finally:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            upstream.close()
            ws.close()
            pump.join(2)


def make_server(ctx: Context) -> Server:
    return Server((ctx.cfg.host, ctx.cfg.port), ctx)
