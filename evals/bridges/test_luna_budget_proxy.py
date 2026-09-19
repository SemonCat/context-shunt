from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

import bridges.luna_budget_proxy as proxy
from bridges._usd_budget import RoutePricing, UsdBudgetLedger
from bridges.luna_budget_proxy import MODEL, NoRedirect, reservation_input_bound, sanitize_request


def test_omitted_output_cap_is_filled_and_payload_is_rebuilt() -> None:
    outbound, cap = sanitize_request({"messages": [{"role": "user", "content": "x"}]})
    assert cap == 2048
    assert outbound == {
        "model": MODEL,
        "messages": [{"role": "user", "content": "x"}],
        "n": 1,
        "stream": False,
        "max_tokens": 2048,
    }


@pytest.mark.parametrize("extra", [{"n": 2}, {"max_completion_tokens": 999}, {"tools": []}])
def test_overrides_and_tools_are_rejected(extra: dict) -> None:
    with pytest.raises(ValueError):
        sanitize_request({"messages": [{"role": "user", "content": "x"}], **extra})


def test_native_benign_fixed_values_are_accepted_and_rebuilt() -> None:
    outbound, _ = sanitize_request({"messages": [{"role": "user", "content": "x"}], "n": 1, "stream": False, "temperature": 0})
    assert outbound["n"] == 1 and outbound["stream"] is False and outbound["max_tokens"] == 2048


def test_conflicting_fixed_values_are_rejected() -> None:
    for key, value in (("n", 2), ("stream", True), ("temperature", 0.1)):
        with pytest.raises(ValueError):
            sanitize_request({"messages": [{"role": "user", "content": "x"}], key: value})


def test_cumulative_input_bytes_are_bounded() -> None:
    with pytest.raises(ValueError, match="65536"):
        sanitize_request({"messages": [{"role": "user", "content": "x" * 70_000}]})


def test_reservation_does_not_use_bytes_divided_by_four() -> None:
    body = {"messages": [{"role": "user", "content": "A" * 1024}]}
    bound = reservation_input_bound(body)
    assert bound == len(json.dumps(body["messages"], ensure_ascii=False, separators=(",", ":")).encode()) + 256
    assert bound > 1024 // 4


def test_long_permitted_payload_reserves_full_conservative_bound() -> None:
    body = {"messages": [{"role": "system", "content": "é" * 20_000}, {"role": "user", "content": "Z" * 20_000}]}
    outbound, _ = sanitize_request(body)
    encoded_bytes = len(json.dumps(outbound["messages"], ensure_ascii=False, separators=(",", ":")).encode())
    assert encoded_bytes <= 65_536
    assert reservation_input_bound(outbound) == encoded_bytes + 512


def test_non_text_and_invalid_message_object_are_rejected() -> None:
    with pytest.raises(ValueError):
        sanitize_request({"messages": [{"role": "user", "content": [{"type": "text"}]}]})
    with pytest.raises(ValueError):
        sanitize_request({"messages": "not-a-list"})


def test_redirect_handler_fails_closed() -> None:
    handler = NoRedirect()
    with pytest.raises(HTTPError):
        handler.http_error_302(Request("http://127.0.0.1"), None, 302, "redirect", {})


class _Upstream(BaseHTTPRequestHandler):
    calls = 0
    payload = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
    fail = False

    def log_message(self, *_args):
        return

    def do_POST(self):  # noqa: N802
        type(self).calls += 1
        if self.fail:
            self.send_response(500); self.end_headers(); return
        data = json.dumps(self.payload).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)


class _Ledger:
    def __init__(self, refuse=False):
        self.refuse = refuse; self.reservations = []; self.results = []

    def reserve(self, **kwargs):
        if self.refuse:
            raise RuntimeError("provider request refused: cumulative USD ceiling")
        item = SimpleNamespace(reservation_id=str(len(self.reservations)))
        self.reservations.append(kwargs)
        return item

    def record_result(self, reservation, **kwargs):
        self.results.append((reservation.reservation_id, kwargs))


def _proxy_server(ledger, upstream_url):
    server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
    server.ledger = ledger
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    return server, server_thread


def _upstream_server():
    _Upstream.calls = 0; _Upstream.fail = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    return server, thread


def _request(port, body=None):
    body = body or {"messages": [{"role": "user", "content": "read"}]}
    req = Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    return urlopen(req, timeout=3)


def test_real_http_reserves_before_fake_upstream_dispatch(monkeypatch):
    upstream, _ = _upstream_server(); ledger = _Ledger(); relay, _ = _proxy_server(ledger, upstream.server_address)
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_UPSTREAM", f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions")
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_API_KEY", "test-nonce")
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_ROUTE", proxy.ROUTE)
    try:
        assert json.loads(_request(relay.server_port).read())["choices"][0]["message"]["content"] == "ok"
        assert len(ledger.reservations) == 1 and _Upstream.calls == 1
        assert ledger.results[0][1]["status"] == "completed"
    finally:
        relay.shutdown(); upstream.shutdown()


def test_real_sqlite_ledger_reserve_and_settle(tmp_path: Path, monkeypatch):
    upstream, _ = _upstream_server()
    pricing = RoutePricing(proxy.ROUTE, "a" * 64, "openclaw.resolveModelCostConfig", (("input", 1), ("output", 6), ("cacheRead", 1), ("cacheWrite", 1)))
    ledger = UsdBudgetLedger(tmp_path / "budget.sqlite3", pricing)
    relay, _ = _proxy_server(ledger, upstream.server_address)
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_UPSTREAM", f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions")
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_API_KEY", "test-nonce")
    try:
        assert json.loads(_request(relay.server_port).read())["choices"]
        summary = ledger.summary()
        assert summary["statuses"]["completed"] == 1
        assert summary["active_or_unknown_reserved_usd"] == "0"
    finally:
        relay.shutdown(); upstream.shutdown()


def test_unknown_usage_retains_hold_and_returns_no_provider_body(monkeypatch):
    upstream, _ = _upstream_server(); _Upstream.payload = {"choices": [{"message": {"content": "secret-looking"}}]}
    ledger = _Ledger(); relay, _ = _proxy_server(ledger, upstream.server_address)
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_UPSTREAM", f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions")
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_API_KEY", "test-nonce")
    try:
        with pytest.raises(HTTPError) as error: _request(relay.server_port).read()
        assert error.value.code == 502 and b"secret-looking" not in error.value.read()
        assert ledger.results[0][1]["status"] == "usage_unknown"
    finally:
        relay.shutdown(); upstream.shutdown(); _Upstream.payload = {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}


def test_timeout_retains_full_hold_without_dispatch(monkeypatch):
    ledger = _Ledger(); relay, _ = _proxy_server(ledger, None)
    class TimeoutOpener:
        def open(self, *_args, **_kwargs):
            raise URLError("timed out")
    monkeypatch.setattr(proxy, "build_opener", lambda *_args: TimeoutOpener())
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_UPSTREAM", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_API_KEY", "test-nonce")
    try:
        with pytest.raises(HTTPError): _request(relay.server_port).read()
        assert len(ledger.reservations) == 1
        assert ledger.results == [("0", {"status": "usage_unknown"})]
    finally:
        relay.shutdown()


def test_each_retry_is_a_distinct_reservation(monkeypatch):
    upstream, _ = _upstream_server(); ledger = _Ledger(); relay, _ = _proxy_server(ledger, upstream.server_address)
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_UPSTREAM", f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions")
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_API_KEY", "test-nonce")
    try:
        _request(relay.server_port).read(); _request(relay.server_port).read()
        assert len(ledger.reservations) == 2
        assert ledger.reservations[0] == ledger.reservations[1]
        assert [item[0] for item in ledger.results] == ["0", "1"]
    finally:
        relay.shutdown(); upstream.shutdown()


def test_insufficient_budget_makes_zero_upstream_requests(monkeypatch):
    upstream, _ = _upstream_server(); ledger = _Ledger(refuse=True); relay, _ = _proxy_server(ledger, upstream.server_address)
    monkeypatch.setenv("CONTEXT_SHUNT_PROXY_UPSTREAM", f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions")
    try:
        with pytest.raises(HTTPError): _request(relay.server_port).read()
        assert _Upstream.calls == 0
    finally:
        relay.shutdown(); upstream.shutdown()
