"""Test-only, single-route OpenAI-compatible relay with durable USD reservation.

The relay is intended for the isolated Hermes canary (`--network none` with a loopback
socket relay). It is deliberately narrow: one exact model, one upstream URL, no redirects,
streaming, tools, retries, or fallback. The API key is supplied by the isolated launcher and
is never logged. Every physical upstream HTTP dispatch is preceded by a ledger reservation;
unknown responses retain the full hold.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bridges._usd_budget import RoutePricing, UsdBudgetLedger, upper_bound_input_tokens

ROUTE = "sub2api-openai/gpt-5.6-luna"
MODEL = "gpt-5.6-luna"


def _json_env(name: str) -> dict:
    raw = os.environ.get(name, "")
    if not raw:
        raise RuntimeError(f"{name} is required")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be an object")
    return value


class Proxy(BaseHTTPRequestHandler):
    server_version = "context-shunt-luna-proxy/1"

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("content-length", "-1"))
            if length <= 0 or length > 262_144:
                raise ValueError("bounded request body required")
            body = json.loads(self.rfile.read(length))
            if (
                body.get("model") != MODEL
                or body.get("stream") is True
                or body.get("tools")
                or not isinstance(body.get("messages"), list)
                or not isinstance(body.get("max_tokens"), int)
                or not 1 <= body["max_tokens"] <= 2048
            ):
                raise ValueError("only bounded non-streaming Luna requests are supported")
            route = os.environ.get("CONTEXT_SHUNT_PROXY_ROUTE", ROUTE)
            if route != ROUTE:
                raise ValueError("route is not the pinned Luna route")
            ledger = self.server.ledger  # type: ignore[attr-defined]
            reservation = ledger.reserve(
                input_token_upper_bound=upper_bound_input_tokens(
                    "", json.dumps(body["messages"], ensure_ascii=False)
                ),
                max_output_tokens=body["max_tokens"],
            )
            try:
                request = Request(
                    os.environ["CONTEXT_SHUNT_PROXY_UPSTREAM"],
                    data=json.dumps(body).encode(),
                    headers={
                        "content-type": "application/json",
                        "authorization": "Bearer " + os.environ["CONTEXT_SHUNT_PROXY_API_KEY"],
                    },
                    method="POST",
                )
                opener = build_opener(NoRedirect)
                with opener.open(request, timeout=60) as response:
                    payload = json.loads(response.read(2_000_000))
                usage = payload.get("usage")
                inp = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                out = usage.get("completion_tokens") if isinstance(usage, dict) else None
                if not isinstance(inp, int) or not isinstance(out, int):
                    ledger.record_result(reservation, status="usage_unknown")
                elif inp > reservation_input_bound(body) or out > body["max_tokens"]:
                    ledger.record_result(reservation, status="bound_breach", reported_input_tokens=inp, reported_output_tokens=out)
                else:
                    ledger.record_result(reservation, status="completed", reported_input_tokens=inp, reported_output_tokens=out)
            except Exception:
                ledger.record_result(reservation, status="usage_unknown")
                raise
            encoded = json.dumps(payload).encode()
            self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
        except (ValueError, KeyError, json.JSONDecodeError, HTTPError, URLError, RuntimeError):
            # Never reflect provider errors, URLs, headers, or response fragments to the
            # Hermes caller; the relay is a test boundary, not an error transport.
            self.send_error(502, "bounded upstream failure")


def reservation_input_bound(body: dict) -> int:
    return upper_bound_input_tokens("", json.dumps(body["messages"], ensure_ascii=False))


class NoRedirect(HTTPRedirectHandler):
    def http_error_301(self, req, fp, code, msg, headers): raise HTTPError(req.full_url, code, msg, headers, fp)
    http_error_302 = http_error_303 = http_error_307 = http_error_308 = http_error_301


def main() -> None:
    pricing = RoutePricing.from_host_identity(_json_env("CONTEXT_SHUNT_PROXY_PRICING_JSON"), expected_route=ROUTE)
    ledger = UsdBudgetLedger(os.environ["CONTEXT_SHUNT_LUNA_BUDGET_DB"], pricing)
    server = HTTPServer(("127.0.0.1", int(os.environ.get("CONTEXT_SHUNT_PROXY_PORT", "0"))), Proxy)
    server.ledger = ledger  # type: ignore[attr-defined]
    print(json.dumps({"ready": True, "port": server.server_port, "route": ROUTE}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
