#!/usr/bin/env python3
"""A small, mobile-friendly control panel for Discord PTB in noVNC.
Buttons: DevTools, Reload, Esc, etc. -> sent to the Discord window via xdotool.
"""

import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("CONTROL_PORT", "6081"))
TOKEN = os.environ.get("VNC_PASSWORD", "")  # If set, the panel requires ?key=PASSWORD

ACTIONS = {
    "devtools": "ctrl+shift+i",
    "reload": "ctrl+r",
    "hardreload": "ctrl+shift+r",
    "esc": "Escape",
    "console": "ctrl+shift+j",
}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Discord Control</title>
<style>
body{font-family:sans-serif;background:#1e1f22;color:#fff;margin:0;padding:16px}
h2{margin-top:0}
button{display:block;width:100%;padding:18px;margin:10px 0;font-size:18px;
border:0;border-radius:10px;background:#5865f2;color:#fff}
button:active{background:#4752c4}
#s{margin-top:14px;color:#9aa}
a{color:#8ab4ff}
</style></head><body>
<h2>Discord PTB Control</h2>
<button onclick="go('devtools')">Open / Close DevTools</button>
<button onclick="go('console')">Open DevTools Console</button>
<button onclick="go('reload')">Reload (Ctrl+R)</button>
<button onclick="go('hardreload')">Hard Reload</button>
<button onclick="go('esc')">Escape</button>
<div id="s">Ready</div>
<script>
const key = new URLSearchParams(location.search).get('key') || '';
function go(a){
  document.getElementById('s').textContent = 'Sending command...';
  fetch('/do/'+a+'?key='+encodeURIComponent(key)).then(r=>r.text())
   .then(t=>document.getElementById('s').textContent=t)
   .catch(e=>document.getElementById('s').textContent='Error: '+e);
}
</script></body></html>"""


def send_key(combo: str) -> str:
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":1"))
    try:
        out = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", "Discord"],
            env=env, capture_output=True, text=True, timeout=5,
        ).stdout.split()

        if not out:
            return "Discord window not found"

        wid = out[-1]
        subprocess.run(
            ["xdotool", "windowfocus", "--sync", wid],
            env=env, timeout=5,
        )
        subprocess.run(
            ["xdotool", "key", "--clearmodifiers", combo],
            env=env, timeout=5,
        )
        return "OK: " + combo

    except Exception as e:  # noqa
        return "Failed: %s" % e


class H(BaseHTTPRequestHandler):
    def _ok(self, body, ctype="text/html; charset=utf-8", code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

        if TOKEN and params.get("key") != TOKEN:
            return self._ok(
                "Authentication required: ?key=VNC_PASSWORD",
                "text/plain",
                403,
            )

        if path.startswith("/do/"):
            action = path[4:]
            if action in ACTIONS:
                return self._ok(send_key(ACTIONS[action]), "text/plain")
            return self._ok("Unknown action", "text/plain", 404)

        return self._ok(PAGE)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
