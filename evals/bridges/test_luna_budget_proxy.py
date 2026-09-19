from __future__ import annotations

from urllib.error import HTTPError
from urllib.request import Request

import pytest

from bridges.luna_budget_proxy import MODEL, NoRedirect, sanitize_request


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


def test_non_text_and_invalid_message_object_are_rejected() -> None:
    with pytest.raises(ValueError):
        sanitize_request({"messages": [{"role": "user", "content": [{"type": "text"}]}]})
    with pytest.raises(ValueError):
        sanitize_request({"messages": "not-a-list"})


def test_redirect_handler_fails_closed() -> None:
    handler = NoRedirect()
    with pytest.raises(HTTPError):
        handler.http_error_302(Request("http://127.0.0.1"), None, 302, "redirect", {})
