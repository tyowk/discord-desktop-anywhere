import base64
import http.client
import json
import os
import socket
import threading

import pytest

from dda import wsproto
from dda.audio import AudioManager
from dda.auth import Auth
from dda.config import Config
from dda.server import Context, Server
from dda.state import AppState


@pytest.fixture()
def fake_vnc():
    """A tiny TCP server that sends an RFB banner and echoes whatever it receives."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            c.sendall(b"RFB 003.008\n")

            def echo(conn=c):
                try:
                    while True:
                        d = conn.recv(4096)
                        if not d:
                            return
                        conn.sendall(d)
                except OSError:
                    pass

            threading.Thread(target=echo, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


@pytest.fixture()
def web(tmp_path, fake_vnc):
    novnc = tmp_path / "novnc"
    (novnc / "app").mkdir(parents=True)
    (novnc / "vnc.html").write_text("<html>vnc</html>")
    (novnc / "app" / "ui.js").write_text("export default 1;")
    (tmp_path / "secret.txt").write_text("nope")
    cfg = Config.from_env(
        {
            "DDA_DATA_DIR": str(tmp_path / "data"),
            "HOME": str(tmp_path),
            "VNC_PASSWORD": "pw",
            "VNC_PORT": str(fake_vnc) if fake_vnc >= 1024 else "5900",
            "PORT": "1024",
            "HOST": "127.0.0.1",
        },
        root=tmp_path,
    )
    ctx = Context(cfg, Auth("pw"), AppState(), AudioManager(cfg), None, novnc, lambda: ":99")
    server = Server(("127.0.0.1", 0), ctx)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], ctx
    server.shutdown()
    server.server_close()


def request(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    data = res.read()
    return res, data


def login(port):
    res, _ = request(
        port, "POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"}, "password=pw&next=/"
    )
    assert res.status == 302
    return res.getheader("Set-Cookie").split(";")[0]


def test_health_is_public(web):
    res, data = request(web[0], "GET", "/healthz")
    assert res.status == 200 and json.loads(data)["ok"]


def test_requires_login(web):
    port = web[0]
    res, _ = request(port, "GET", "/")
    assert res.status == 302 and res.getheader("Location").startswith("/login")
    assert request(port, "GET", "/api/status")[0].status == 401
    assert request(port, "GET", "/novnc/vnc.html")[0].status == 401


def test_login_flow(web):
    port = web[0]
    bad, _ = request(port, "POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"}, "password=wrong")
    assert bad.status == 401
    cookie = login(port)
    res, body = request(port, "GET", "/", {"Cookie": cookie})
    assert res.status == 200 and b"dda-config" in body and b"{{CONFIG}}" not in body
    assert "frame-src 'self'" in res.getheader("Content-Security-Policy")
    status = json.loads(request(port, "GET", "/api/status", {"Cookie": cookie})[1])
    assert status["ok"] and "audio" in status


def test_legacy_key_param_logs_in(web):
    res, _ = request(web[0], "GET", "/?key=pw")
    assert res.status == 302 and "dda_session=" in res.getheader("Set-Cookie")


def test_login_rate_limit(web):
    port = web[0]
    for _ in range(5):
        request(port, "POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"}, "password=x")
    res, _ = request(port, "POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"}, "password=pw")
    assert res.status == 429


def test_open_redirect_is_blocked(web):
    res, _ = request(
        web[0],
        "POST",
        "/login",
        {"Content-Type": "application/x-www-form-urlencoded"},
        "password=pw&next=//evil.example",
    )
    assert res.getheader("Location") == "/"


def test_static_serving_and_traversal(web):
    port = web[0]
    cookie = login(port)
    h = {"Cookie": cookie}
    res, body = request(port, "GET", "/novnc/app/ui.js", h)
    assert res.status == 200 and body == b"export default 1;" and "javascript" in res.getheader("Content-Type")
    etag = res.getheader("ETag")
    assert request(port, "GET", "/novnc/app/ui.js", {**h, "If-None-Match": etag})[0].status == 304
    for evil in ("/novnc/../secret.txt", "/novnc/%2e%2e/secret.txt", "/novnc/..%2fsecret.txt"):
        assert request(port, "GET", evil, h)[0].status == 404
    assert request(port, "GET", "/assets/app.js")[0].status == 200
    assert request(port, "GET", "/assets/../server.py")[0].status == 404


def test_post_requires_csrf_header(web):
    port = web[0]
    h = {"Cookie": login(port), "Content-Type": "application/json"}
    assert request(port, "POST", "/api/action", h, '{"name":"esc"}')[0].status == 403
    h["X-DDA-Request"] = "1"
    h["Origin"] = "https://evil.example"
    assert request(port, "POST", "/api/action", h, '{"name":"esc"}')[0].status == 403
    del h["Origin"]
    assert request(port, "POST", "/api/action", h, '{"name":"bogus"}')[0].status == 404
    assert request(port, "POST", "/api/action", h, "not json")[0].status == 400
    assert request(port, "POST", "/api/action", h, '{"name":"devtools"}')[0].status == 400  # disabled


def test_allow_cross_site_bypasses_csrf_checks(web):
    port, ctx = web
    import dataclasses

    ctx.cfg = dataclasses.replace(ctx.cfg, allow_cross_site=True)
    h = {"Cookie": login(port), "Content-Type": "application/json", "Origin": "https://evil.example"}
    assert request(port, "POST", "/api/action", h, '{"name":"bogus"}')[0].status == 404


def ws_handshake(port, headers):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        "GET /websockify HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        "Sec-WebSocket-Protocol: binary",
    ]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    head, _, rest = buf.partition(b"\r\n\r\n")
    return s, head.decode(), rest, key


def test_websocket_proxy_end_to_end(web):
    port, _ = web
    s, head, rest, key = ws_handshake(port, {"Cookie": login(port)})
    assert " 101 " in head.split("\r\n")[0]
    assert wsproto.accept_key(key) in head and "Sec-WebSocket-Protocol: binary" in head
    ws = wsproto.WebSocket(s, client=True, initial=rest)
    assert ws.recv_data() == (wsproto.OP_BINARY, b"RFB 003.008\n")
    ws.send_binary(b"hello vnc")
    assert ws.recv_data() == (wsproto.OP_BINARY, b"hello vnc")
    ws.close()


def test_websocket_requires_auth_and_same_origin(web):
    port, _ = web
    s, head, _, _ = ws_handshake(port, {})
    assert " 401 " in head.split("\r\n")[0]
    s.close()
    s, head, _, _ = ws_handshake(port, {"Cookie": login(port), "Origin": "https://evil.example"})
    assert " 403 " in head.split("\r\n")[0]
    s.close()
    s, head, _, _ = ws_handshake(port, {"Cookie": login(port), "Origin": f"http://127.0.0.1:{port}"})
    assert " 101 " in head.split("\r\n")[0]
    s.close()


def test_audio_unavailable_returns_503(web):
    port, _ = web
    assert request(port, "GET", "/audio", {"Cookie": login(port)})[0].status == 503
