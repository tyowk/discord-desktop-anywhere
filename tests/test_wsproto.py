import socket
import threading

import pytest

from dda import wsproto


def test_accept_key_rfc_example():
    assert wsproto.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("size", [0, 1, 5, 125, 126, 70000])
def test_roundtrip_masked_and_unmasked(size):
    a, b = socket.socketpair()
    payload = bytes(range(256)) * (size // 256 + 1)
    payload = payload[:size]
    client, server = wsproto.WebSocket(a, client=True), wsproto.WebSocket(b, client=False)
    threading.Thread(target=lambda: client.send(wsproto.OP_BINARY, payload), daemon=True).start()
    assert server.recv_data() == (wsproto.OP_BINARY, payload)
    threading.Thread(target=lambda: server.send(wsproto.OP_BINARY, payload), daemon=True).start()
    assert client.recv_data() == (wsproto.OP_BINARY, payload)


def test_ping_is_answered_and_skipped():
    a, b = socket.socketpair()
    client, server = wsproto.WebSocket(a, client=True), wsproto.WebSocket(b, client=False)
    client.send(wsproto.OP_PING, b"hi")
    client.send(wsproto.OP_TEXT, b"data")
    assert server.recv_data() == (wsproto.OP_TEXT, b"data")
    assert client.recv_frame()[1] == wsproto.OP_PONG


def test_fragmented_message():
    a, b = socket.socketpair()
    server = wsproto.WebSocket(b, client=False)
    a.sendall(wsproto.encode_frame(wsproto.OP_TEXT, b"hel", mask=True, fin=False))
    a.sendall(wsproto.encode_frame(wsproto.OP_CONT, b"lo", mask=True))
    assert server.recv_message() == b"hello"


def test_close_raises():
    a, b = socket.socketpair()
    client, server = wsproto.WebSocket(a, client=True), wsproto.WebSocket(b, client=False)
    client.close()
    with pytest.raises(wsproto.WebSocketClosed):
        server.recv_data()


def test_oversized_frame_rejected():
    a, b = socket.socketpair()
    server = wsproto.WebSocket(b, client=False)
    a.sendall(bytes([0x82, 127]) + (wsproto.MAX_FRAME + 1).to_bytes(8, "big"))
    with pytest.raises(wsproto.WebSocketError):
        server.recv_frame()


def test_subprotocol():
    assert wsproto.optional_subprotocol("binary, base64", "binary") == "binary"
    assert wsproto.optional_subprotocol("base64", "binary") is None
    assert wsproto.optional_subprotocol(None, "binary") is None
