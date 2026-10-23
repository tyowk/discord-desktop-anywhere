#!/usr/bin/env python3
"""A small, mobile-friendly control panel for Discord PTB in noVNC.

Features:
  - Keyboard shortcuts (Reload, Esc, and DevTools when enabled) sent via xdotool
  - HD / Ultra HD screenshots: Discord re-renders the page at a larger scale through the
    Chrome DevTools Protocol (not a simple upscale), so text and icons stay sharp at 4K/8K
  - Quick full-screen (X11) screenshot as a fallback
  - Screenshot gallery to view / download from a phone
Uses only the Python standard library (no pip).
"""
import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

PORT = int(os.environ.get("CONTROL_PORT", "6081"))
TOKEN = os.environ.get("VNC_PASSWORD", "")  # If set, the panel requires ?key=PASSWORD
DEVTOOLS = os.environ.get("DEVTOOLS", "0") == "1"        # DevTools are OFF by default
SCREENSHOT = os.environ.get("SCREENSHOT", "1") == "1"    # HD screenshots via CDP
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
SHOT_DIR = os.environ.get("SHOT_DIR", os.path.join(os.getcwd(), "screenshots"))
MAX_KEEP = 40  # Number of most recent screenshots to keep
os.makedirs(SHOT_DIR, exist_ok=True)

ACTIONS = {
    "reload": "ctrl+r",
    "hardreload": "ctrl+shift+r",
    "esc": "Escape",
}
if DEVTOOLS:
    ACTIONS["devtools"] = "ctrl+shift+i"
    ACTIONS["console"] = "ctrl+shift+j"


# ----------------------------------------------------------------- WebSocket mini
class WS:
    """Minimal WebSocket client (RFC 6455), enough for CDP on localhost."""

    def __init__(self, url, timeout=90):
        u = urlparse(url)
        port = u.port or 80
        self.sock = socket.create_connection((u.hostname, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        req = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, u.hostname, port, key)
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("WebSocket handshake failed")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        first = head.split(b"\r\n")[0]
        if b" 101 " not in first:
            raise IOError("handshake rejected: " + first.decode(errors="replace"))
        self.buf = bytearray(rest)
        self.n = 0

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(262144)
            if not chunk:
                raise IOError("WebSocket connection lost")
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    @staticmethod
    def _mask(data, mask):
        # Fast XOR using big integers
        if not data:
            return b""
        n = len(data)
        m = (mask * (n // 4 + 1))[:n]
        return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        self.sock.sendall(bytes(header) + mask + self._mask(payload, mask))

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def recv_msg(self):
        msg = bytearray()
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            op = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            data = self._read_exact(length) if length else b""
            if masked:
                data = self._mask(data, mask)
            if op == 0x8:
                raise IOError("WebSocket closed by Discord")
            if op == 0x9:  # ping -> pong
                self._send_frame(0xA, data)
                continue
            if op == 0xA:
                continue
            msg += data
            if fin:
                return bytes(msg).decode()

    def call(self, method, params=None):
        self.n += 1
        mid = self.n
        self.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.recv_msg())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(m["error"].get("message", "CDP error"))
                return m.get("result", {})

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ----------------------------------------------------------------- Screenshot
def _new_name(tag, px):
    return time.strftime("discord-%Y%m%d-%H%M%S") + "-%s-%dpx.png" % (tag, px)


def _prune():
    files = sorted(
        (f for f in os.listdir(SHOT_DIR) if f.startswith("discord-") and f.endswith(".png")),
        key=lambda f: os.path.getmtime(os.path.join(SHOT_DIR, f)),
    )
    for f in files[:-MAX_KEEP]:
        try:
            os.remove(os.path.join(SHOT_DIR, f))
        except OSError:
            pass


def find_page_ws():
    with urllib.request.urlopen("http://127.0.0.1:%d/json" % CDP_PORT, timeout=5) as r:
        items = json.load(r)
    pages = [
        i for i in items
        if i.get("type") == "page" and i.get("webSocketDebuggerUrl")
        and not i.get("url", "").startswith("devtools://")
    ]
    if not pages:
        raise RuntimeError("Discord page not found (has Discord finished loading?)")
    pages.sort(key=lambda i: 0 if "discord" in i.get("url", "") else 1)
    return pages[0]["webSocketDebuggerUrl"]


def capture_hd(target_w):
    """Re-render the Discord page at a large scale -> real detail, not an upscale."""
    ws = WS(find_page_ws())
    try:
        lm = ws.call("Page.getLayoutMetrics")
        vp = lm.get("cssVisualViewport") or lm.get("visualViewport") or lm.get("layoutViewport")
        w = int(vp.get("clientWidth") or vp.get("width"))
        h = int(vp.get("clientHeight") or vp.get("height"))
        scale = max(1.0, float(target_w) / w)
        res = ws.call(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "clip": {"x": 0, "y": 0, "width": w, "height": h, "scale": scale},
            },
        )
    finally:
        ws.close()
    data = base64.b64decode(res["data"])
    out_w, out_h = int(round(w * scale)), int(round(h * scale))
    name = _new_name("hd", out_w)
    with open(os.path.join(SHOT_DIR, name), "wb") as f:
        f.write(data)
    _prune()
    return {"ok": True, "name": name, "dims": "%dx%d" % (out_w, out_h), "bytes": len(data)}


def capture_x11(target_w):
    """Fallback: grab the full X11 screen (including any DevTools), upscale if requested."""
    display = os.environ.get("DISPLAY", ":1")
    name = _new_name("x11", target_w or 0)
    path = os.path.join(SHOT_DIR, name)
    subprocess.run(["import", "-display", display, "-window", "root", path],
                   check=True, timeout=30)
    dims = "native"
    if target_w:
        subprocess.run(["convert", path, "-filter", "Lanczos", "-resize", "%dx" % target_w, path],
                       check=True, timeout=60)
        dims = "%dpx (upscaled)" % target_w
    _prune()
    return {"ok": True, "name": name, "dims": dims, "bytes": os.path.getsize(path)}


# ----------------------------------------------------------------- Keyboard
def send_key(combo):
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":1"))
    try:
        out = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", "Discord"],
            env=env, capture_output=True, text=True, timeout=5,
        ).stdout.split()
        if not out:
            return "Discord window not found"
        wid = out[-1]
        subprocess.run(["xdotool", "windowfocus", "--sync", wid], env=env, timeout=5)
        subprocess.run(["xdotool", "key", "--clearmodifiers", combo], env=env, timeout=5)
        return "OK: " + combo
    except Exception as e:  # noqa
        return "Failed: %s" % e


# ----------------------------------------------------------------- Halaman
PAGE_TMPL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Discord Control</title>
<style>
body{font-family:sans-serif;background:#1e1f22;color:#fff;margin:0;padding:16px}
h2{margin:0 0 8px} h3{margin:22px 0 4px;color:#b5bac1;font-size:14px;text-transform:uppercase}
button,a.btn{display:block;width:100%;box-sizing:border-box;text-align:center;text-decoration:none;
padding:16px;margin:8px 0;font-size:17px;border:0;border-radius:10px;background:#5865f2;color:#fff}
button.g,a.btn.g{background:#3a3c43}
button:active{background:#4752c4}
#s{margin-top:14px;color:#9aa;word-break:break-all}
#s a{color:#8ab4ff}
small{color:#80848e}
</style></head><body>
<h2>Discord PTB Control</h2>
<h3>Controls</h3>
%%DEVTOOLS%%
<button class="g" onclick="go('reload')">Reload (Ctrl+R)</button>
<button class="g" onclick="go('hardreload')">Hard Reload</button>
<button class="g" onclick="go('esc')">Escape</button>
<h3>Screenshot</h3>
%%SHOTS%%
<a class="btn g" id="gal" href="#">Screenshot gallery</a>
<div id="s">Ready</div>
<script>
const key = new URLSearchParams(location.search).get('key') || '';
const q = '?key=' + encodeURIComponent(key);
document.getElementById('gal').href = '/shots' + q;
const S = document.getElementById('s');
function go(a){
  S.textContent = 'Sending command...';
  fetch('/do/'+a+q).then(r=>r.text()).then(t=>S.textContent=t)
   .catch(e=>S.textContent='Error: '+e);
}
function shot(kind, w){
  S.textContent = 'Taking screenshot... (4K/8K can take a few seconds)';
  fetch('/'+kind+q+'&w='+w).then(r=>r.json()).then(j=>{
    if(!j.ok){S.textContent='Failed: '+j.error;return;}
    const mb=(j.bytes/1048576).toFixed(1);
    S.innerHTML='OK '+j.dims+' ('+mb+' MB)<br>'+
      '<a href="/shots/'+j.name+q+'" target="_blank">View</a> | '+
      '<a href="/shots/'+j.name+q+'&dl=1">Download</a>';
  }).catch(e=>S.textContent='Error: '+e);
}
</script></body></html>"""

DEVTOOLS_BUTTONS = (
    "<button onclick=\"go('devtools')\">Open / Close DevTools</button>\n"
    "<button onclick=\"go('console')\">Open DevTools Console</button>"
)
SHOT_HD_BUTTONS = (
    "<button onclick=\"shot('shot',3840)\">Ultra HD 4K (3840 px)</button>\n"
    "<button class=\"g\" onclick=\"shot('shot',1920)\">Full HD (1920 px)</button>\n"
    "<button class=\"g\" onclick=\"shot('shot',7680)\">8K (7680 px, heavy)</button>"
)
SHOT_X11_BUTTON = (
    "<button class=\"g\" onclick=\"shot('xshot',0)\">Quick: full screen (native resolution)</button>"
)


def page_html():
    shots = (SHOT_HD_BUTTONS + "\n" if SCREENSHOT else "") + SHOT_X11_BUTTON
    if DEVTOOLS and SCREENSHOT:
        shots = "<small>Close DevTools first so the Discord page is captured in full.</small>\n" + shots
    return (
        PAGE_TMPL.replace("%%DEVTOOLS%%", DEVTOOLS_BUTTONS if DEVTOOLS else "")
        .replace("%%SHOTS%%", shots)
    )


def gallery_html(key):
    q = "?key=" + quote(key)
    files = sorted(
        (f for f in os.listdir(SHOT_DIR) if f.startswith("discord-") and f.endswith(".png")),
        key=lambda f: os.path.getmtime(os.path.join(SHOT_DIR, f)),
        reverse=True,
    )
    rows = []
    for f in files:
        mb = os.path.getsize(os.path.join(SHOT_DIR, f)) / 1048576
        rows.append(
            '<div class="r"><div>%s<br><small>%.1f MB</small></div>'
            '<a href="/shots/%s%s" target="_blank">View</a>'
            '<a href="/shots/%s%s&dl=1">Download</a></div>'
            % (escape(f), mb, quote(f), q, quote(f), q)
        )
    body = "".join(rows) or "<p>No screenshots yet.</p>"
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Gallery</title><style>"
        "body{font-family:sans-serif;background:#1e1f22;color:#fff;padding:16px}"
        "small{color:#80848e}a{color:#8ab4ff;margin-left:14px}"
        ".r{display:flex;align-items:center;justify-content:space-between;"
        "padding:10px 0;border-bottom:1px solid #333;word-break:break-all}"
        "</style></head><body><h2>Screenshot gallery</h2>"
        '<a style="margin:0" href="/%s">&larr; back</a>%s'
        "<p><small>Files are also saved in the <b>screenshots/</b> folder in Replit "
        "(downloadable from the Files panel).</small></p></body></html>"
        % (q, body)
    )


# ----------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/html; charset=utf-8", code=200, extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj), "application/json", code)

    def do_GET(self):
        u = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        if TOKEN and params.get("key") != TOKEN:
            return self._send("Authentication required: ?key=VNC_PASSWORD", "text/plain", 403)

        if path.startswith("/do/"):
            action = path[4:]
            if action in ACTIONS:
                return self._send(send_key(ACTIONS[action]), "text/plain")
            if action in ("devtools", "console") and not DEVTOOLS:
                return self._send("DevTools are disabled (set DEVTOOLS=1 in Secrets)", "text/plain")
            return self._send("Unknown action", "text/plain", 404)

        if path in ("/shot", "/xshot"):
            try:
                w = int(params.get("w", "3840"))
            except ValueError:
                w = 3840
            if path == "/shot" and not SCREENSHOT:
                return self._json({"ok": False, "error": "HD screenshots are disabled (SCREENSHOT=0)"})
            try:
                if path == "/shot":
                    return self._json(capture_hd(max(640, min(w, 7680))))
                return self._json(capture_x11(max(0, min(w, 7680))))
            except Exception as e:  # noqa
                msg = "%s: %s" % (type(e).__name__, e)
                if path == "/shot":
                    msg += " | fallback: use the 'full screen' button"
                return self._json({"ok": False, "error": msg})

        if path == "/shots":
            return self._send(gallery_html(params.get("key", "")))

        if path.startswith("/shots/"):
            name = os.path.basename(path[7:])
            fp = os.path.join(SHOT_DIR, name)
            if not (name.startswith("discord-") and name.endswith(".png") and os.path.isfile(fp)):
                return self._send("Not found", "text/plain", 404)
            with open(fp, "rb") as f:
                data = f.read()
            extra = {}
            if params.get("dl") == "1":
                extra["Content-Disposition"] = 'attachment; filename="%s"' % name
            return self._send(data, "image/png", 200, extra)

        return self._send(page_html())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
