"""Deterministic structured aggregation for production-shaped minified Loki JSON."""

from __future__ import annotations

import json

import pytest

from context_shunt.provider import UnavailableProvider
from context_shunt.session import ShuntSession
from tests.support import make_capability, make_config

pytestmark = pytest.mark.gate_inspect


def _setup(tmp_path):
    logs = [
        {"level": "error", "trace_id": "tr-a", "service": "billing"},
        {"level": "info", "trace_id": "tr-b", "service": "billing"},
        {"level": "error", "trace_id": "tr-a", "service": "checkout"},
        {"level": "error", "trace_id": "tr-c", "service": "billing"},
        {"level": "error", "trace_id": None},
        {"level": "error", "trace_id": "tr-d", "service": None},
    ]
    body = {
        "data": {
            "result": [
                {
                    "stream": {"job": "synthetic"},
                    "values": [
                        [str(index), json.dumps(row, separators=(",", ":"))]
                        for index, row in enumerate(logs)
                    ],
                }
            ]
        }
    }
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "loki.json"
    path.write_text(json.dumps(body, separators=(",", ":")))
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    return session, session.register_path(str(path))


def _request(entry, selector, max_scan=20_000):
    return {
        "schema_version": "1.3",
        "request_id": "req_aggregate",
        "operation": "inspect",
        "source_id": entry.source_id,
        "snapshot_id": entry.snapshot.snapshot_id,
        "selector": selector,
        "budgets": {"max_result_bytes": 16_384, "max_scan_lines": max_scan},
    }


def test_counts_groups_and_distinct_fields_in_minified_loki_json(tmp_path):
    session, entry = _setup(tmp_path)
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/data/result",
                "expand_pointer": "/values",
                "record_pointer": "/1",
                "parse_json": True,
                "filter": {"pointer": "/level", "equals": "error"},
                "distinct": ["/trace_id"],
                "group_by": ["/service"],
            },
        )
    )
    assert env["code"] == "EXTRACTED" and env["status"] == "ok"
    assert env["provenance"]["attempts_started"] == 0
    result = json.loads(env["extraction"]["segments"][0]["text"])
    assert result["matched_count"] == 5 and result["records_scanned"] == 6
    assert result["distinct"][0] == {
        "path": "/trace_id",
        "count": 4,
        "values": ["tr-a", "tr-c", "tr-d", None],
        "values_complete": True,
    }
    assert result["groups"] == [
        {"key": ["billing"], "count": 2},
        {"key": ["checkout"], "count": 1},
        {"key": [None], "count": 1},
        {"key": [{"missing": True}], "count": 1},
    ]
    assert result["group_count"] == 4


def test_refuses_over_budget_scan_without_partial_count(tmp_path):
    session, entry = _setup(tmp_path)
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/data/result",
                "expand_pointer": "/values",
                "record_pointer": "/1",
                "parse_json": True,
            },
            max_scan=3,
        )
    )
    assert env["code"] == "LIMIT_EXCEEDED"
    assert "extraction" not in env


def test_empty_expansions_still_consume_the_outer_scan_budget(tmp_path):
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "empty-expansions.json"
    path.write_text(json.dumps({"outer": [{"records": []}, {"records": []}]}))
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/outer",
                "expand_pointer": "/records",
            },
            max_scan=1,
        )
    )
    assert env["code"] == "LIMIT_EXCEEDED"
    assert "extraction" not in env


@pytest.mark.parametrize("field", ["distinct", "group_by"])
def test_non_scalar_aggregate_targets_are_rejected_not_reported_missing(tmp_path, field):
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "object-value.json"
    path.write_text(json.dumps({"records": [{"value": {"nested": 1}}, {}]}))
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "records_pointer": "/records", field: ["/value"]},
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_SELECTOR"
    assert "extraction" not in env


def test_aggregate_keys_use_cross_runtime_utf8_order(tmp_path):
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "unicode-order.json"
    path.write_text(json.dumps({"records": [{"value": "\ue000"}, {"value": "😀"}]}))
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/records",
                "distinct": ["/value"],
                "group_by": ["/value"],
            },
        )
    )
    result = json.loads(env["extraction"]["segments"][0]["text"])
    assert result["distinct"][0]["values"] == ["\ue000", "😀"]
    assert [row["key"] for row in result["groups"]] == [["\ue000"], ["😀"]]


def test_aggregate_rejects_escaped_lone_surrogate_key(tmp_path):
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "lone-surrogate.json"
    path.write_text('{"records":[{"value":"\\ud800"}]}')
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/records",
                "distinct": ["/value"],
                "group_by": ["/value"],
            },
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_SELECTOR"
    assert "extraction" not in env


def test_aggregate_rejects_lone_surrogate_before_embedded_json_byte_measurement(tmp_path):
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "embedded-surrogate.json"
    path.write_text('{"records":[{"value":"\\ud800"}]}')
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/records",
                "record_pointer": "/value",
                "parse_json": True,
            },
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_JSON"
    assert "extraction" not in env


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_aggregate_rejects_nonstandard_constants_in_text_plain_json(tmp_path, constant):
    session = ShuntSession(
        "sess",
        make_config(
            tmp_path,
            tool_result_capture={"enabled": True, "host_ordering_verified_locally": True},
        ),
        make_capability(tool_result_capture=True),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    body = f'{{"records":[{constant}],"padding":"' + "x" * 17_000 + '"}'
    outcome = session.post_tool_result("spill-constant", body)
    assert outcome.action == "spill"
    entry = session.registry.resolve("sess", outcome.source_id)
    env = session.inspect(
        _request(entry, {"kind": "aggregate", "records_pointer": "/records"})
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_JSON"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_aggregate_rejects_nonstandard_constants_in_embedded_json(tmp_path, constant):
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "embedded-constant.json"
    path.write_text(json.dumps({"records": [{"value": constant}]}))
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/records",
                "record_pointer": "/value",
                "parse_json": True,
            },
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_JSON"


@pytest.mark.parametrize("field", ["distinct", "group_by"])
def test_aggregate_rejects_lone_surrogate_in_emitted_pointer_name(tmp_path, field):
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "ordinary.json"
    path.write_text('{"records":[{}]}')
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "records_pointer": "/records", field: ["/\ud800"]},
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_SELECTOR"
    assert "extraction" not in env


@pytest.mark.parametrize(
    "raw_records",
    [
        '[{"value":1}]',
        '[{"value":9007199254740992},{"value":9007199254740993}]',
        '[{"value":1.5}]',
        '[{"value":1e-400}]',
        '[{"value":9007199254740991.1}]',
    ],
)
def test_aggregate_rejects_numeric_keys_whose_original_lexeme_may_be_lost(tmp_path, raw_records):
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "unsafe-number.json"
    path.write_text('{"records":' + raw_records + "}")
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "records_pointer": "/records", "distinct": ["/value"]},
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_SELECTOR"
    assert "extraction" not in env


@pytest.mark.parametrize(("length", "detail"), [(513, "SCHEMA_VIOLATION"), (511, "BAD_SELECTOR")])
def test_aggregate_rejects_oversize_equality_filter_before_scan(tmp_path, length, detail):
    session, entry = _setup(tmp_path)
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/data/result",
                "filter": {"pointer": "/value", "equals": "x" * length},
            },
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == detail
    assert "extraction" not in env


def test_exact_cardinalities_survive_bounded_key_sample_truncation(tmp_path):
    (tmp_path / "ws").mkdir()
    path = tmp_path / "ws" / "records.json"
    path.write_text(
        json.dumps(
            {"records": [{"key": f"key-{index:03d}"} for index in range(205)]},
            separators=(",", ":"),
        )
    )
    session = ShuntSession(
        "sess",
        make_config(tmp_path),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/records",
                "distinct": ["/key"],
                "group_by": ["/key"],
            },
        )
    )
    result = json.loads(env["extraction"]["segments"][0]["text"])
    assert result["matched_count"] == 205
    assert result["group_count"] == 205 and result["groups_complete"] is False
    assert len(result["groups"]) == 200
    assert result["distinct"][0]["count"] == 205
    assert result["distinct"][0]["values_complete"] is False
    assert len(result["distinct"][0]["values"]) == 200


def test_aggregates_json_captured_through_real_oversized_tool_result_route(tmp_path):
    value = {
        "data": {
            "result": [
                {
                    "values": [
                        [
                            "0",
                            json.dumps(
                                {
                                    "level": "error",
                                    "trace_id": "trace-spill",
                                    "service": "billing",
                                },
                                separators=(",", ":"),
                            ),
                        ]
                    ]
                }
            ]
        },
        "padding": "x" * 17_000,
    }
    session = ShuntSession(
        "sess",
        make_config(
            tmp_path,
            tool_result_capture={
                "enabled": True,
                "host_ordering_verified_locally": True,
            },
        ),
        make_capability(tool_result_capture=True),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    outcome = session.post_tool_result("spill-json", json.dumps(value, separators=(",", ":")))
    assert outcome.action == "spill"
    entry = session.registry.resolve("sess", outcome.source_id)
    assert entry.snapshot.media_type == "text/plain"
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "records_pointer": "/data/result",
                "expand_pointer": "/values",
                "record_pointer": "/1",
                "parse_json": True,
                "group_by": ["/service"],
            },
        )
    )
    result = json.loads(env["extraction"]["segments"][0]["text"])
    assert result["matched_count"] == 1 and result["group_count"] == 1
