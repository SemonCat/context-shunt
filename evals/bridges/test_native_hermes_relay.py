from __future__ import annotations

import socket
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from bridges.native_hermes_relay import serve


def _short_unix_socket_path() -> str:
    # AF_UNIX paths are limited (~104-108 bytes on macOS/BSD); pytest's tmp_path is
    # often too deep, so use a short name directly under the system temp root.
    return str(Path(tempfile.gettempdir()) / f"nhr-{uuid.uuid4().hex[:8]}.sock")


def _run_unix_echo_server(sock_path: str, ready: threading.Event, stop: threading.Event) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    server.settimeout(5)
    ready.set()
    try:
        conn, _ = server.accept()
    except socket.timeout:
        return
    with conn:
        conn.settimeout(5)
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            conn.sendall(chunk)
    stop.set()
    server.close()


def test_relay_forwards_bytes_between_tcp_client_and_unix_socket() -> None:
    sock_path = _short_unix_socket_path()
    ready = threading.Event()
    stopped = threading.Event()
    echo_thread = threading.Thread(
        target=_run_unix_echo_server, args=(sock_path, ready, stopped), daemon=True
    )
    echo_thread.start()
    assert ready.wait(5)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()

    relay_thread = threading.Thread(
        target=serve,
        kwargs=dict(
            listen_host="127.0.0.1",
            listen_port=port,
            upstream_socket_path=sock_path,
            max_connections=1,
        ),
        daemon=True,
    )
    relay_thread.start()

    client = None
    for _ in range(200):
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=0.1)
            break
        except OSError:
            continue
    assert client is not None, "relay never opened its TCP listener"

    with client:
        payload = b"POST /v1/chat/completions HTTP/1.1\r\n\r\n{\"ping\":true}"
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)
        received = b""
        client.settimeout(5)
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            received += chunk
        assert received == payload

    relay_thread.join(timeout=5)
    echo_thread.join(timeout=5)
    assert not relay_thread.is_alive()
    assert stopped.is_set()


def test_ready_callback_fires_before_any_accept_and_not_on_a_probe_connection() -> None:
    sock_path = _short_unix_socket_path()
    ready = threading.Event()
    stopped = threading.Event()
    echo_thread = threading.Thread(
        target=_run_unix_echo_server, args=(sock_path, ready, stopped), daemon=True
    )
    echo_thread.start()
    assert ready.wait(5)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()

    callback_calls = []
    relay_thread = threading.Thread(
        target=serve,
        kwargs=dict(
            listen_host="127.0.0.1",
            listen_port=port,
            upstream_socket_path=sock_path,
            max_connections=1,
            ready_callback=lambda: callback_calls.append(True),
        ),
        daemon=True,
    )
    relay_thread.start()

    for _ in range(200):
        if callback_calls:
            break
        import time

        time.sleep(0.01)
    assert callback_calls == [True], "ready_callback must fire without any TCP connection"

    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    with client:
        client.sendall(b"probe")
        client.shutdown(socket.SHUT_WR)
        client.settimeout(5)
        while client.recv(65536):
            pass
    relay_thread.join(timeout=5)
    assert not relay_thread.is_alive()


def test_relay_rejects_a_second_connection_after_max_reached() -> None:
    sock_path = _short_unix_socket_path()
    ready = threading.Event()
    stopped = threading.Event()
    echo_thread = threading.Thread(
        target=_run_unix_echo_server, args=(sock_path, ready, stopped), daemon=True
    )
    echo_thread.start()
    assert ready.wait(5)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()

    relay_thread = threading.Thread(
        target=serve,
        kwargs=dict(
            listen_host="127.0.0.1",
            listen_port=port,
            upstream_socket_path=sock_path,
            max_connections=1,
        ),
        daemon=True,
    )
    relay_thread.start()

    client = None
    for _ in range(200):
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=0.1)
            break
        except OSError:
            continue
    assert client is not None
    client.close()

    relay_thread.join(timeout=5)
    assert not relay_thread.is_alive(), "relay must exit after exactly max_connections"

    with pytest.raises(OSError):
        second = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        second.close()
