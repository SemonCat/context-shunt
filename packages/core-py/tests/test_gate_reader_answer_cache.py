"""Session-local exact reader reuse with authorization and completeness boundaries."""

from __future__ import annotations

from dataclasses import replace

import pytest

from context_shunt import envelope as E
from context_shunt.errors import ShuntError
from context_shunt.limits import DEFAULT_LIMITS
from context_shunt.provider import FallbackChainProvider
from context_shunt.session import ShuntSession
from tests.support import FakeLuna, claims_json, make_capability, make_config

pytestmark = pytest.mark.gate_reader


def _fixture(tmp_path, lines=2):
    path = tmp_path / "ws" / "source.txt"
    path.parent.mkdir()
    path.write_text(
        "\n".join(
            "retry_limit: 7" if index == 0 else f"padding-{index}-" + "x" * 200
            for index in range(lines)
        )
        + "\n"
    )
    reply = claims_json(
        [{"text": "retry_limit is 7", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "retry_limit: 7"}],
    )
    provider = FakeLuna(default_reply=reply)
    session = ShuntSession("sess", make_config(tmp_path), make_capability(), provider=provider)
    return session, provider, session.register_path(str(path))


def _request(entry, request_id, **overrides):
    request = {
        "schema_version": "1.3",
        "request_id": request_id,
        "operation": "read",
        "question": "What is retry_limit?",
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "lines", "start": 1, "end": entry.snapshot.line_count},
            }
        ],
        "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
    }
    request.update(overrides)
    return request


def test_reuses_complete_exact_query_and_reports_zero_usage_for_hit(tmp_path):
    session, provider, entry = _fixture(tmp_path)
    first = session.read(_request(entry, "req_first"))
    second = session.read(_request(entry, "req_second"))
    assert first["code"] == "ANSWERED" and second["answer"] == first["answer"]
    assert provider.call_count == 1
    assert second["provenance"]["cache_reused"] is True
    assert second["provenance"]["attempts_started"] == 0
    assert second["provenance"]["attempts_usage_complete"] == 0
    assert second["provenance"]["usage_complete"] is True
    stats = session.stats(
        {
            "schema_version": "1.2",
            "request_id": "req_stats",
            "operation": "stats",
            "page_size": 8,
        }
    )
    row = next(
        record
        for record in stats["stats"]["records"]
        if record["operation_id"] == second["accounting_id"]
    )
    assert row["attempts_started"] == 0 and row["attempts_usage_complete"] == 0
    assert row["reader_token_method"] == "not_applicable"
    assert row["reader_input_tokens"] is None
    assert row["reader_output_tokens"] is None
    assert row["reader_cache_tokens"] is None


def test_query_selector_budget_model_snapshot_and_authorization_boundaries_do_not_hit(tmp_path):
    session, provider, entry = _fixture(tmp_path)
    session.read(_request(entry, "req_first"))
    session.read(_request(entry, "req_question", question="What retry limit applies?"))
    session.read(
        _request(
            entry,
            "req_selector",
            sources=[
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": {"kind": "lines", "start": 1, "end": 1},
                }
            ],
        )
    )
    session.read(
        _request(
            entry,
            "req_budget",
            budgets={"max_chunks": 8, "max_answer_bytes": 4096, "deadline_ms": 60000},
        )
    )
    provider.model = "gpt-5.6-luna-reconfigured"
    session.read(_request(entry, "req_model"))
    assert provider.call_count == 5
    changed = session.read(
        _request(
            entry,
            "req_snapshot",
            sources=[
                {
                    "source_id": entry.source_id,
                    "snapshot_id": "sha256:" + "0" * 64,
                    "selector": {
                        "kind": "lines",
                        "start": 1,
                        "end": entry.snapshot.line_count,
                    },
                }
            ],
        )
    )
    assert changed["code"] == "SOURCE_CHANGED"
    assert provider.call_count == 5
    session.close()
    revoked = session.read(_request(entry, "req_revoked"))
    assert revoked["code"] == "SOURCE_EXPIRED"
    assert "cache_reused" not in revoked["provenance"]


def test_partial_answer_is_never_stored(tmp_path):
    session, provider, entry = _fixture(tmp_path, lines=400)
    limited = _request(
        entry,
        "req_partial",
        budgets={"max_chunks": 1, "max_answer_bytes": 8192, "deadline_ms": 60000},
    )
    assert session.read(limited)["status"] == "partial"
    limited["request_id"] = "req_partial_again"
    assert session.read(limited)["status"] == "partial"
    assert provider.call_count == 2


def test_fallback_answer_is_never_stored(tmp_path):
    path = tmp_path / "ws" / "source.txt"
    path.parent.mkdir()
    path.write_text("retry_limit: 7\n")
    reply = claims_json(
        [{"text": "retry_limit is 7", "citation_ids": ["c1"]}],
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "retry_limit: 7"}],
    )
    primary = FakeLuna(
        replies=[ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED", retryable=True)],
        default_reply=reply,
    )
    fallback = FakeLuna(default_reply=reply, model="fallback-model")
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=FallbackChainProvider(primary, [fallback]),
    )
    entry = session.register_path(str(path))

    first = session.read(_request(entry, "req_fallback"))
    second = session.read(_request(entry, "req_primary"))
    assert first["provenance"]["fallback_used"] is True
    assert second["provenance"].get("cache_reused") is not True
    assert primary.call_count == 2 and fallback.call_count == 1


def test_cache_hit_rechecks_envelope_cap_after_request_metadata_changes(tmp_path):
    session, provider, entry = _fixture(tmp_path)
    first = session.read(_request(entry, "a"))
    session._reader._limits = replace(  # narrow only after a valid answer was cached
        DEFAULT_LIMITS, max_envelope_bytes=E.serialized_bytes(first) + 10
    )

    second = session.read(_request(entry, "x" * 64))
    assert provider.call_count == 2
    assert second["provenance"].get("cache_reused") is not True


def test_least_recently_used_answer_is_evicted_after_32_entries(tmp_path):
    session, provider, entry = _fixture(tmp_path)
    for index in range(33):
        session.read(
            _request(entry, f"req_{index}", question=f"What is retry_limit? variant {index}")
        )
    session.read(_request(entry, "req_again", question="What is retry_limit? variant 0"))
    assert provider.call_count == 34


def test_cache_hashes_and_accounts_for_the_retained_query_key(tmp_path):
    session, _provider, entry = _fixture(tmp_path)
    question = "What is retry_limit? unique retained key"
    session.read(_request(entry, "req_large_key", question=question))

    cache = session._reader._answer_cache
    assert len(cache) == 1
    key, cached = next(iter(cache.items()))
    assert len(key) == 64
    assert question not in key
    assert cached[3] == E.serialized_bytes(cached[0]) + len(key.encode("utf-8"))
    assert session._reader._answer_cache_bytes == cached[3] <= 256 * 1024
