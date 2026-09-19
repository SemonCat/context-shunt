"""In-container TCP-to-Unix-socket relay for the isolated native Hermes canary.

Runs *inside* the `--network none` disposable container, using only the image's own
stdlib Python. It has no knowledge of HTTP, credentials, or pricing: it is a dumb
bidirectional byte pump between a TCP listener that Hermes's `custom` provider dials
(`base_url` = this loopback address) and a Unix domain socket bind-mounted read-write
into the container's filesystem namespace.

That socket is the far end of an `ssh -R` reverse streamlocal forward whose *local*
side is the real budget-enforcing proxy (`luna_budget_proxy.py`) running outside the
namespace, on the operator's machine, where the upstream API key lives. `--network
none` removes every network device from the container, so this Unix-domain path
mounted from the host is the container's only route to the outside world; there is no
egress a fallback/retry could reach even if Hermes attempted one.

Never logs connection payloads (may contain the synthetic prompt or, in a paid run,
provider content) — only connection lifecycle and byte counts.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading

_BUF = 65536


def _pump(src: socket.socket, dst: socket.socket, done: threading.Event) -> None:
    try:
        while True:
            chunk = src.recv(_BUF)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        done.set()
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def relay_one(client: socket.socket, upstream_socket_path: str) -> tuple[int, int]:
    """Bridge one accepted TCP connection to the mounted Unix socket.

    Returns (client_to_upstream_bytes, upstream_to_client_bytes) approximated by
    whether each direction ran; exact byte counts are not tracked to keep this file a
    minimal, auditable pump. Blocks until both directions are closed.
    """
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.connect(upstream_socket_path)
    done_a = threading.Event()
    done_b = threading.Event()
    t1 = threading.Thread(target=_pump, args=(client, upstream, done_a), daemon=True)
    t2 = threading.Thread(target=_pump, args=(upstream, client, done_b), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    upstream.close()
    return (1 if done_a.is_set() else 0, 1 if done_b.is_set() else 0)


def serve(
    *,
    listen_host: str,
    listen_port: int,
    upstream_socket_path: str,
    max_connections: int,
    ready_callback: "callable | None" = None,
) -> None:
    """Accept up to ``max_connections`` sequential TCP connections, then exit.

    The canary launcher passes ``max_connections=1`` so the process terminates once
    Hermes's dispatch call finishes with that one TCP connection. This bounds the
    number of *connections*, not the number of physical upstream (HTTP/provider)
    requests: a client can send more than one request over the same connection (a
    retry, or a provider client that reuses its transport), and a real run recorded
    exactly that -- one relayed connection carrying two upstream attempts
    (`provenance.attempts_started: 2`) after the first came back `MODEL_ERROR`. The
    budget ledger, which counts each physical dispatch it is asked to authorize, is
    the actual source of truth for how many upstream attempts occurred -- this
    connection cap is only a coarse container-lifetime bound, not a dispatch count.

    ``ready_callback``, if given, is invoked right after the listener is bound and
    listening (before any ``accept()``). A caller running this in a background thread
    must use this callback -- not a probe connection -- to detect readiness: a probe
    TCP connection would itself count as the one connection ``max_connections=1``
    allows, and would be relayed to the real upstream socket as a bogus dispatch.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((listen_host, listen_port))
    listener.listen(1)
    print(f"relay-ready host={listen_host} port={listener.getsockname()[1]}", flush=True)
    if ready_callback is not None:
        ready_callback()
    accepted = 0
    try:
        while accepted < max_connections:
            client, _addr = listener.accept()
            accepted += 1
            print(f"relay-accept n={accepted}", flush=True)
            try:
                relay_one(client, upstream_socket_path)
            finally:
                client.close()
            print(f"relay-closed n={accepted}", flush=True)
    finally:
        listener.close()
    print(f"relay-done accepted={accepted}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-socket", required=True, help="path to the mounted Unix socket")
    parser.add_argument("--max-connections", type=int, default=1)
    args = parser.parse_args(argv)
    serve(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        upstream_socket_path=args.upstream_socket,
        max_connections=args.max_connections,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
