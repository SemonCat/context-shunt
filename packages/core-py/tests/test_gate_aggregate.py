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


def _request(entry, selector, max_scan=20_000, max_result_bytes=16_384):
    return {
        "schema_version": "1.3",
        "request_id": "req_aggregate",
        "operation": "inspect",
        "source_id": entry.source_id,
        "snapshot_id": entry.snapshot.snapshot_id,
        "selector": selector,
        "budgets": {"max_result_bytes": max_result_bytes, "max_scan_lines": max_scan},
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
    env = session.inspect(_request(entry, {"kind": "aggregate", "records_pointer": "/records"}))
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


def _decode_setup(tmp_path, body, *, name="decode.json", config_overrides=None):
    (tmp_path / "ws").mkdir(exist_ok=True)
    path = tmp_path / "ws" / name
    path.write_text(json.dumps(body, separators=(",", ":")))
    session = ShuntSession(
        "sess",
        make_config(tmp_path, **(config_overrides or {})),
        make_capability(),
        provider=UnavailableProvider("MUST_NOT_RUN"),
    )
    entry = session.register_path(str(path))
    return session, entry


def test_decode_pointer_omitted_matches_current_output_exactly(tmp_path):
    session, entry = _setup(tmp_path)
    env = session.inspect(_request(entry, {"kind": "aggregate", "records_pointer": "/data/result"}))
    result = json.loads(env["extraction"]["segments"][0]["text"])
    assert "decoded_from" not in result


def test_decode_pointer_decodes_one_layer_then_records_pointer_reads_decoded_root(tmp_path):
    records = [
        {"status": "200", "page": "1", "per_page": "10"},
        {"status": "200", "page": "1", "per_page": "10"},
        {"status": "404", "page": "2", "per_page": "10"},
        {"status": "200", "page": "2", "per_page": "20"},
        {"status": "500", "page": "1", "per_page": "10"},
        {"status": "200", "page": "3", "per_page": "20"},
        {"status": "404", "page": "1", "per_page": "10"},
        {"status": "200", "page": "2", "per_page": "10"},
        {"status": "301", "page": "1", "per_page": "10"},
    ]
    inner = json.dumps(
        {"data": [{"line": json.dumps(row, separators=(",", ":"))} for row in records]},
        separators=(",", ":"),
    )
    session, entry = _decode_setup(tmp_path, {"result": inner}, name="wrapped.json")
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "decode_pointer": "/result",
                "records_pointer": "/data",
                "record_pointer": "/line",
                "parse_json": True,
                "distinct": ["/status"],
                "group_by": ["/status"],
            },
        )
    )
    assert env["code"] == "EXTRACTED" and env["status"] == "ok"
    result = json.loads(env["extraction"]["segments"][0]["text"])
    assert result["decoded_from"] == "/result"
    assert result["records_scanned"] == 9 and result["matched_count"] == 9
    assert result["distinct"][0] == {
        "path": "/status",
        "count": 4,
        "values": ["200", "301", "404", "500"],
        "values_complete": True,
    }
    assert result["groups"] == [
        {"key": ["200"], "count": 5},
        {"key": ["301"], "count": 1},
        {"key": ["404"], "count": 2},
        {"key": ["500"], "count": 1},
    ]
    assert result["group_count"] == 4


def test_decode_pointer_does_not_recurse_through_a_second_json_string_layer(tmp_path):
    doubly_encoded = json.dumps(json.dumps({"data": []}, separators=(",", ":")))
    session, entry = _decode_setup(tmp_path, {"result": doubly_encoded}, name="double.json")
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "decode_pointer": "/result", "records_pointer": ""},
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_SELECTOR"
    assert "extraction" not in env


def test_decode_pointer_rejects_malformed_syntax(tmp_path):
    # Every pointer field shares the same `jsonPointer` schema pattern (leading "/"), so a
    # malformed decode_pointer is refused at request validation, before the core ever sees
    # it - the same path records_pointer/expand_pointer/record_pointer already go through.
    session, entry = _decode_setup(tmp_path, {"result": "{}"})
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "decode_pointer": "result", "records_pointer": "/data"},
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "SCHEMA_VIOLATION"
    assert "extraction" not in env


def test_decode_pointer_rejects_missing_path(tmp_path):
    session, entry = _decode_setup(tmp_path, {"result": "{}"})
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "decode_pointer": "/missing", "records_pointer": "/data"},
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "POINTER_NOT_FOUND"
    assert "extraction" not in env


def test_decode_pointer_rejects_non_string_target(tmp_path):
    session, entry = _decode_setup(tmp_path, {"result": {"data": []}})
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "decode_pointer": "/result", "records_pointer": "/data"},
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_SELECTOR"
    assert "extraction" not in env


def test_decode_pointer_rejects_malformed_json_text(tmp_path):
    session, entry = _decode_setup(tmp_path, {"result": "not json{"})
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "decode_pointer": "/result", "records_pointer": "/data"},
        )
    )
    assert env["code"] == "INVALID_REQUEST"
    assert env["failure_detail"] == "BAD_JSON"
    assert "extraction" not in env


def test_decode_pointer_and_parse_json_share_one_byte_budget(tmp_path):
    # A source file's raw bytes are always >= the decode_pointer target's decoded bytes
    # (JSON-string embedding only ever adds escaping overhead), so decode_pointer's own
    # byte cap can't be isolated from the file-level read cap using a single decode. What
    # *is* reachable - and is the actual doubling risk the shared budget guards against -
    # is decode_pointer's bytes plus every subsequent per-record parse_json's bytes adding
    # up past the cap even though the file itself comfortably fit under it.
    records = [{"n": index, "pad": "z" * 20} for index in range(50)]
    inner = json.dumps(
        {"data": [json.dumps(row, separators=(",", ":")) for row in records]},
        separators=(",", ":"),
    )
    body = {"result": inner}
    outer_bytes = len(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    session, entry = _decode_setup(
        tmp_path,
        body,
        config_overrides={"limits": {"max_source_bytes": outer_bytes + 50}},
    )
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "decode_pointer": "/result",
                "records_pointer": "/data",
                "parse_json": True,
            },
        )
    )
    assert env["code"] == "LIMIT_EXCEEDED"
    assert env["failure_detail"] == "RESULT_OVER_SOURCE_CAP"
    assert "extraction" not in env


def test_decode_pointer_enforces_the_node_cap(tmp_path):
    body = {"result": json.dumps({"data": [{"n": i} for i in range(200)]})}
    session, entry = _decode_setup(
        tmp_path, body, config_overrides={"limits": {"json_max_nodes": 20}}
    )
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "decode_pointer": "/result", "records_pointer": "/data"},
        )
    )
    assert env["code"] == "LIMIT_EXCEEDED"
    assert env["failure_detail"] == "JSON_TOO_MANY_NODES"
    assert "extraction" not in env


def test_decode_pointer_enforces_the_depth_cap(tmp_path):
    # The raw file is shallow - one top-level object holding one string - regardless of how
    # deeply the *decoded* value nests, so depth-cap isolation (unlike byte-cap isolation) is
    # reachable standalone: only the decode stage's own json_depth_and_nodes walk can trip it.
    nested = {"data": []}
    for _ in range(10):
        nested = {"data": nested}
    body = {"result": json.dumps(nested)}
    session, entry = _decode_setup(
        tmp_path, body, config_overrides={"limits": {"json_max_depth": 5}}
    )
    env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "decode_pointer": "/result", "records_pointer": "/data"},
        )
    )
    assert env["code"] == "LIMIT_EXCEEDED"
    assert env["failure_detail"] == "JSON_TOO_DEEP"
    assert "extraction" not in env


def test_decode_pointer_shares_its_budget_with_per_record_parse_json(tmp_path):
    # decode_pointer alone walks {"data": [{"line": "..."} x 50]} = 102 nodes (1 root object
    # + 1 array + 50 * (1 object + 1 string)). Each subsequent per-record parse_json of
    # {"n": i} adds 2 more nodes, 100 total across all 50 records. A cap strictly between
    # 102 and 202 proves the two stages share one cumulative counter: the decode alone must
    # fit under it, and only accumulating per-record parses pushes the total over.
    records = [{"n": i} for i in range(50)]
    inner = json.dumps(
        {"data": [{"line": json.dumps(row, separators=(",", ":"))} for row in records]},
        separators=(",", ":"),
    )
    session, entry = _decode_setup(
        tmp_path, {"result": inner}, config_overrides={"limits": {"json_max_nodes": 150}}
    )
    decode_only_env = session.inspect(
        _request(
            entry,
            {"kind": "aggregate", "decode_pointer": "/result", "records_pointer": "/data"},
        )
    )
    assert decode_only_env["code"] == "EXTRACTED"

    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "decode_pointer": "/result",
                "records_pointer": "/data",
                "record_pointer": "/line",
                "parse_json": True,
            },
        )
    )
    assert env["code"] == "LIMIT_EXCEEDED"
    assert env["failure_detail"] == "JSON_TOO_MANY_NODES"
    assert "extraction" not in env


def test_decode_pointer_output_still_shrinks_to_fit_the_result_cap(tmp_path):
    records = [{"key": f"key-{index:03d}"} for index in range(205)]
    inner = json.dumps(
        {"data": [{"line": json.dumps(row, separators=(",", ":"))} for row in records]},
        separators=(",", ":"),
    )
    session, entry = _decode_setup(tmp_path, {"result": inner}, name="shrink.json")
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "decode_pointer": "/result",
                "records_pointer": "/data",
                "record_pointer": "/line",
                "parse_json": True,
                "distinct": ["/key"],
                "group_by": ["/key"],
            },
        )
    )
    assert env["code"] == "EXTRACTED"
    result = json.loads(env["extraction"]["segments"][0]["text"])
    assert result["decoded_from"] == "/result"
    assert result["matched_count"] == 205
    assert result["groups_complete"] is False
    assert result["distinct"][0]["values_complete"] is False


def test_decode_pointer_output_byte_shrink_preserves_decoded_from(tmp_path):
    # Only 20 distinct groups here - well under the 200-row hard sample cap the previous
    # test exercises - so a tightened max_result_bytes below the naturally emitted size is
    # the only thing that can force the fits()-loop's group/distinct truncation. That proves
    # decoded_from survives the byte-driven shrink path specifically, not just the cap path.
    records = [{"key": f"key-{index:03d}"} for index in range(20)]
    inner = json.dumps(
        {"data": [{"line": json.dumps(row, separators=(",", ":"))} for row in records]},
        separators=(",", ":"),
    )
    session, entry = _decode_setup(tmp_path, {"result": inner}, name="byte-shrink.json")
    full_env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "decode_pointer": "/result",
                "records_pointer": "/data",
                "record_pointer": "/line",
                "parse_json": True,
                "distinct": ["/key"],
                "group_by": ["/key"],
            },
        )
    )
    full_result = json.loads(full_env["extraction"]["segments"][0]["text"])
    assert full_result["group_count"] == 20 and len(full_result["groups"]) == 20

    shrunk_env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "decode_pointer": "/result",
                "records_pointer": "/data",
                "record_pointer": "/line",
                "parse_json": True,
                "distinct": ["/key"],
                "group_by": ["/key"],
            },
            max_result_bytes=600,
        )
    )
    assert shrunk_env["code"] == "EXTRACTED"
    shrunk_result = json.loads(shrunk_env["extraction"]["segments"][0]["text"])
    assert shrunk_result["decoded_from"] == "/result"
    assert shrunk_result["matched_count"] == 20
    assert shrunk_result["group_count"] == 20
    assert shrunk_result["groups_complete"] is False
    assert len(shrunk_result["groups"]) < 20


def test_decode_pointer_never_mutates_the_original_snapshot(tmp_path):
    body = {"result": json.dumps({"data": [{"line": json.dumps({"status": "200"})}]})}
    session, entry = _decode_setup(tmp_path, body, name="immutable.json")
    original_value = json.loads(json.dumps(entry.snapshot.json_value))
    original_snapshot_id = entry.snapshot.snapshot_id
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "decode_pointer": "/result",
                "records_pointer": "/data",
                "record_pointer": "/line",
                "parse_json": True,
            },
        )
    )
    assert env["code"] == "EXTRACTED"
    assert entry.snapshot.json_value == original_value
    assert entry.snapshot.snapshot_id == original_snapshot_id


def test_decode_pointer_empty_string_decodes_a_root_that_is_itself_a_json_string(tmp_path):
    # decode_pointer's "" means "the whole root" (same empty-pointer convention every other
    # pointer field already uses), so this covers a snapshot whose root value *is* a bare
    # JSON string rather than an object wrapping one, e.g. a source that serializes its
    # entire body as `"{\"data\":[...]}"`.
    inner = json.dumps({"data": [{"line": json.dumps({"status": "200"})}]})
    session, entry = _decode_setup(tmp_path, inner, name="empty-decode-pointer.json")
    env = session.inspect(
        _request(
            entry,
            {
                "kind": "aggregate",
                "decode_pointer": "",
                "records_pointer": "/data",
                "record_pointer": "/line",
                "parse_json": True,
            },
        )
    )
    assert env["code"] == "EXTRACTED"
    result = json.loads(env["extraction"]["segments"][0]["text"])
    assert result["decoded_from"] == ""
    assert result["records_scanned"] == 1
    assert result["matched_count"] == 1


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
