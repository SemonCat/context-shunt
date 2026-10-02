"""The registered Hermes ``context_shunt_inspect`` entrance defaults bytes ``align`` to char.

The core request contract keeps ``strict`` as its default; only the registered tool fills in
``char`` when a ``bytes`` selector omits ``align``. Every case drives the handler the plugin
actually registered, never the core seam, so the normalization is proven where an agent
reaches it. Synthetic multilingual text only.
``adapters/openclaw/test/inspect-default-char.test.ts`` asserts the identical fixture and
expectations against the OpenClaw entrance.
"""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from context_shunt.session import ShuntSession
from tests.test_gate_capability import FakeCtx, FakeLlm, _config, _load_adapter
from tests.test_gate_inspect import _captured, _request, _session

pytestmark = pytest.mark.gate_inspect

# a(0) é(1,2) 日(3..5) 🎯(6..9) ñ(10,11) x(12) LF(13): one-, two-, three- and four-byte
# characters plus an accent, so each misaligned offset below lands inside a character.
TEXT = "aé日🎯ñx\n"
RAW = TEXT.encode("utf-8")
assert len(RAW) == 14

# (start, end) requested -> (effective start, effective end, text). Shared with the TS test.
MISALIGNED = [
    ((2, 5), (1, 3, "é")),
    ((4, 8), (3, 6, "日")),
    ((7, 11), (6, 10, "🎯")),
    ((11, 13), (10, 13, "ñx")),
    ((2, 9), (1, 6, "é日")),
]


@pytest.fixture
def entrance(tmp_path):
    module = _load_adapter()
    ctx = FakeCtx(_config(tmp_path), llm=FakeLlm())
    module.register(ctx)
    path = tmp_path / "ws" / "mixed.txt"
    path.write_bytes(RAW * 3)
    read = json.loads(
        ctx.registered_handlers["context_shunt_read"](
            {"question": "What is here?", "paths": [str(path)]}, task_id="char-default"
        )
    )
    handle = read["sources"][0]
    handler = ctx.registered_handlers["context_shunt_inspect"]

    def call(selector, task_id="char-default", **extra):
        args = {
            "source_id": handle["source_id"],
            "snapshot_id": handle["snapshot_id"],
            "selector": selector,
            **extra,
        }
        return json.loads(handler(args, task_id=task_id))

    call.module = module
    call.ctx = ctx
    call.handle = handle
    return call


def _bytes(start, end, **extra):
    return {"kind": "bytes", "start": start, "end": end, **extra}


@pytest.mark.parametrize(("requested", "expected"), MISALIGNED)
def test_omitted_align_floors_misaligned_offsets_at_the_registered_entrance(
    entrance, requested, expected
):
    out = entrance(_bytes(*requested))
    assert out["code"] == "EXTRACTED", out.get("failure_detail")
    lo, hi, text = expected
    assert out["extraction"]["byte_range"] == {"start": lo, "end": hi}
    assert "".join(s["text"] for s in out["extraction"]["segments"]) == text
    assert out["extraction"]["complete"] is True


@pytest.mark.parametrize(("requested", "expected"), MISALIGNED)
def test_omitted_align_is_the_same_selector_as_explicit_char(entrance, requested, expected):
    omitted = entrance(_bytes(*requested))
    explicit = entrance(_bytes(*requested, align="char"))
    # Same handle and session, so only per-call identifiers and the running cumulative
    # disclosure counters may differ; the counters must advance by exactly one page.
    for envelope in (omitted, explicit):
        envelope.pop("request_id", None)
        envelope.pop("accounting_id", None)
    advanced = {
        key: explicit["extraction"].pop(key) - omitted["extraction"].pop(key)
        for key in ("disclosed_bytes_source", "disclosed_bytes_session")
    }
    assert advanced == dict.fromkeys(advanced, omitted["extraction"]["result_bytes"])
    assert omitted == explicit


def test_explicit_strict_still_refuses_without_disclosure(entrance):
    refused = entrance(_bytes(2, 9, align="strict"))
    assert refused["code"] == "INVALID_REQUEST"
    assert refused["failure_detail"] == "UTF8_RANGE_BOUNDARY"
    assert "extraction" not in refused
    exact = entrance(_bytes(1, 6, align="strict"))
    assert exact["code"] == "EXTRACTED"
    assert exact["extraction"]["segments"][0]["text"] == "é日"
    # Strict selectors never report an effective range: offsets are exactly as requested.
    assert "byte_range" not in exact["extraction"]
    # The refusal charged nothing: the next exact page advances the counter by its own bytes.
    again_refused = entrance(_bytes(2, 9, align="strict"))
    assert again_refused["failure_detail"] == "UTF8_RANGE_BOUNDARY"
    again = entrance(_bytes(1, 6, align="strict"))
    assert (
        again["extraction"]["disclosed_bytes_source"]
        == exact["extraction"]["disclosed_bytes_source"] + 5
    )


@pytest.mark.parametrize("align", ["CHAR", "Strict", "", None, 1, True, "auto"])
def test_an_invalid_explicit_align_is_rejected_not_repaired(entrance, align):
    out = entrance(_bytes(2, 9, align=align))
    assert out["code"] == "INVALID_REQUEST"
    assert out["failure_detail"] == "TOOL_ARGS_VIOLATION"
    assert "extraction" not in out


def test_non_bytes_selectors_are_unaffected(entrance):
    lines = entrance({"kind": "lines", "start": 1, "end": 1})
    assert lines["code"] == "EXTRACTED"
    assert "byte_range" not in lines["extraction"]
    search = entrance({"kind": "search", "needle": "日", "max_matches": 1})
    assert search["code"] == "EXTRACTED"
    assert "byte_range" not in search["extraction"]
    # align stays a bytes-only field: it is rejected on any other kind, never dropped.
    for selector in (
        {"kind": "lines", "start": 1, "end": 1, "align": "char"},
        {"kind": "search", "needle": "日", "max_matches": 1, "align": "char"},
        {"kind": "aggregate", "records_pointer": "", "align": "char"},
    ):
        out = entrance(selector)
        assert out["code"] == "INVALID_REQUEST"
        assert out["failure_detail"] == "TOOL_ARGS_VIOLATION"


def test_omitted_align_pages_and_continues_with_omitted_or_explicit_char(entrance):
    start, end = 2, len(RAW) * 3 - 1
    collected = bytearray()
    cursor = None
    charged_before = None
    for page_number in range(64):
        # Alternate the spelling: both name the same normalized selector and cursor binding.
        selector = _bytes(start, end) if page_number % 2 == 0 else _bytes(start, end, align="char")
        extra = {"max_result_bytes": 4, **({"cursor": cursor} if cursor else {})}
        out = entrance(selector, **extra)
        assert out["code"] == "EXTRACTED", out.get("failure_detail")
        page = out["extraction"]
        assert page["result_bytes"] <= 4
        # 2 is inside é and floors to 1; the final offset is the trailing LF's own start.
        assert page["byte_range"] == {"start": 1, "end": end}
        if charged_before is None:
            charged_before = page["disclosed_bytes_source"] - page["result_bytes"]
        for segment in page["segments"]:
            collected.extend(segment["text"].encode("utf-8"))
        # Every disclosed byte is charged, and nothing beyond what was emitted.
        assert page["disclosed_bytes_source"] == charged_before + len(collected)
        if page["complete"]:
            break
        cursor = page["next_cursor"]
    else:
        pytest.fail("pagination did not finish")
    assert bytes(collected) == (RAW * 3)[1:end]


def test_a_cursor_cannot_cross_between_the_default_and_explicit_strict(entrance):
    first = entrance(_bytes(0, 28), max_result_bytes=4)
    cursor = first["extraction"]["next_cursor"]
    assert cursor
    crossed = entrance(_bytes(0, 28, align="strict"), max_result_bytes=4, cursor=cursor)
    assert crossed["code"] == "INVALID_REQUEST"
    assert crossed["failure_detail"] == "BAD_CURSOR"
    assert "extraction" not in crossed

    strict_first = entrance(_bytes(0, 28, align="strict"), max_result_bytes=4)
    strict_cursor = strict_first["extraction"]["next_cursor"]
    assert strict_cursor
    back = entrance(_bytes(0, 28), max_result_bytes=4, cursor=strict_cursor)
    assert back["code"] == "INVALID_REQUEST"
    assert back["failure_detail"] == "BAD_CURSOR"


def test_the_entrance_never_mutates_caller_owned_arguments(entrance):
    handler = entrance.ctx.registered_handlers["context_shunt_inspect"]
    selector = _bytes(2, 9)
    args = {
        "source_id": entrance.handle["source_id"],
        "snapshot_id": entrance.handle["snapshot_id"],
        "selector": selector,
    }
    before = copy.deepcopy(args)
    out = json.loads(handler(args, task_id="char-default"))
    assert out["code"] == "EXTRACTED"
    assert args == before
    assert "align" not in selector
    # The keyword path, which Hermes also uses, is equally side-effect free.
    out = json.loads(entrance.module.context_shunt_inspect(**args, task_id="char-default"))
    assert out["code"] == "EXTRACTED"
    assert args == before


def test_max_result_bytes_cap_still_holds_for_the_default(entrance):
    out = entrance(_bytes(2, 9), max_result_bytes=2)
    assert out["code"] == "EXTRACTED"
    assert out["extraction"]["result_bytes"] <= 2
    assert out["extraction"]["complete"] is False


def test_handles_stay_session_isolated_under_the_default(entrance):
    out = entrance(_bytes(2, 9), task_id="another-session")
    assert out["code"] != "EXTRACTED"
    assert "extraction" not in out


def test_registered_schema_presents_the_tool_default_and_keeps_the_canonical_enum(entrance):
    module = entrance.module
    parameters = entrance.ctx.registered_schemas["context_shunt_inspect"]["parameters"]
    (bytes_branch,) = [
        branch
        for branch in parameters["properties"]["selector"]["oneOf"]
        if branch["properties"]["kind"].get("const") == "bytes"
    ]
    align = bytes_branch["properties"]["align"]
    assert align["enum"] == ["strict", "char"]
    assert align["default"] == "char"
    assert "Omitted at this tool: char" in align["description"]
    assert "core request default remains strict" in align["description"]
    description = module.INSPECT_TOOL_SCHEMA["description"]
    assert 'without "align" uses "char"' in description
    # The canonical shared contract, which the core validates against, is unchanged.
    canonical = module._registered_tool_parameters("inspectArgs")
    (canonical_bytes,) = [
        branch
        for branch in canonical["properties"]["selector"]["oneOf"]
        if branch["properties"]["kind"].get("const") == "bytes"
    ]
    assert canonical_bytes["properties"]["align"]["description"].startswith("strict (default)")
    assert "default" not in canonical_bytes["properties"]["align"]
    # The presented schema accepts and rejects exactly what the canonical one does.
    presented = Draft202012Validator(parameters)
    reference = Draft202012Validator(canonical)
    base = {"source_id": "src_abcd", "snapshot_id": "sha256:" + "a" * 64}
    for selector in (
        _bytes(0, 1),
        _bytes(0, 1, align="char"),
        _bytes(0, 1, align="strict"),
        _bytes(0, 1, align="CHAR"),
        _bytes(0, 1, align=None),
        {"kind": "lines", "start": 1, "end": 1, "align": "char"},
    ):
        args = {**base, "selector": selector}
        assert presented.is_valid(args) == reference.is_valid(args)


def test_the_core_request_default_is_still_strict(tmp_path):
    """Only the tool entrance changed; a core request without align stays strict."""
    session: ShuntSession = _session(tmp_path)
    entry = _captured(tmp_path, session, TEXT * 3)
    refused = session.inspect(_request(entry, _bytes(2, 9)))
    assert refused["code"] == "INVALID_REQUEST"
    assert refused["failure_detail"] == "UTF8_RANGE_BOUNDARY"
    floored = session.inspect(_request(entry, _bytes(2, 9, align="char")))
    assert floored["extraction"]["byte_range"] == {"start": 1, "end": 6}
