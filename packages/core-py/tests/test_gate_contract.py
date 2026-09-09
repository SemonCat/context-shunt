"""unit contract: the schemas are the cross-language boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from context_shunt.config import load as load_config
from context_shunt.errors import ShuntError
from context_shunt.limits import DEFAULT_LIMITS, legal_pair, status_code_pairs
from context_shunt.schema import envelope_validator, request_validator, validate_request

pytestmark = pytest.mark.gate_contract

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "v1" / "fixtures"


def _docs(kind: str, validity: str):
    paths = sorted((FIXTURES / kind / validity).glob("*.json"))
    assert paths, f"fixture corpus {kind}/{validity} is empty"
    for path in paths:
        with path.open("rb") as fh:
            yield path.name, json.load(fh)["document"]


@pytest.mark.parametrize("name,doc", list(_docs("request", "valid")))
def test_valid_requests_accepted(name, doc):
    assert request_validator().is_valid(doc), name


@pytest.mark.parametrize("name,doc", list(_docs("request", "invalid")))
def test_invalid_requests_rejected(name, doc):
    assert not request_validator().is_valid(doc), name


@pytest.mark.parametrize("name,doc", list(_docs("envelope", "valid")))
def test_valid_envelopes_accepted(name, doc):
    assert envelope_validator().is_valid(doc), name


@pytest.mark.parametrize("name,doc", list(_docs("envelope", "invalid")))
def test_invalid_envelopes_rejected(name, doc):
    assert not envelope_validator().is_valid(doc), name


def test_propose_patch_is_rejected_as_unsupported_operation():
    doc = next(d for n, d in _docs("request", "valid") if n == "minimal.json")
    with pytest.raises(ShuntError) as exc:
        validate_request({**doc, "operation": "propose_patch"})
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.detail == "WRITER_OPERATION_UNSUPPORTED"


def test_unknown_version_is_its_own_code():
    doc = next(d for n, d in _docs("request", "valid") if n == "minimal.json")
    with pytest.raises(ShuntError) as exc:
        validate_request({**doc, "schema_version": "2.0"})
    assert exc.value.code == "UNSUPPORTED_VERSION"


def test_whitespace_question_makes_the_request_invalid():
    doc = next(d for n, d in _docs("request", "valid") if n == "minimal.json")
    with pytest.raises(ShuntError):
        validate_request({**doc, "question": "   "})


def test_status_code_pairing_table_matches_schema_enums():
    pairs = status_code_pairs()
    schema = envelope_validator().schema
    schema_codes = set(schema["properties"]["code"]["enum"])
    assert set(pairs["codes"]) == schema_codes
    assert set(pairs["statuses"]) == set(schema["properties"]["status"]["enum"])
    for status, codes in pairs["pairs"].items():
        assert set(codes) <= schema_codes
        for code in codes:
            assert legal_pair(status, code)


def test_every_code_is_reachable_from_some_status():
    pairs = status_code_pairs()
    reachable = {c for codes in pairs["pairs"].values() for c in codes}
    assert reachable == set(pairs["codes"])


def test_limits_match_the_shared_contract():
    raw = json.loads((FIXTURES.parent / "limits.json").read_text())
    assert DEFAULT_LIMITS.full_read_max_lines == raw["gate"]["full_read_max_lines"] == 350
    assert DEFAULT_LIMITS.reader_model == raw["reader_model"] == "gpt-5.6-luna"
    assert DEFAULT_LIMITS.max_envelope_bytes == 16384


@pytest.mark.parametrize(
    ("section", "value"),
    [
        ("reader", {"enabld": True}),
        ("inspect", {"enabld": True}),
        ("stats", {"enabld": True}),
        ("tool_result_capture", {"enabld": True}),
        ("suma_post_tool", {"enabld": True}),  # deprecated alias, still validated
        ("writer", {"enabld": True}),
    ],
)
def test_config_rejects_unknown_nested_keys(tmp_path, section, value):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ShuntError) as exc:
        load_config(
            {"workspace_roots": [str(workspace)], section: value},
            default_spill_dir=tmp_path / "cache",
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.detail == "BAD_CONFIGURATION"


def test_config_rejects_unknown_limit_and_top_level_sections(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for extra in (
        {"limits": {"store_busy_timeout_mss": 1000}},
        {"store": {"busy_timeout_ms": 1000}},
        {"accounting": {"max_stats_pages": 4}},
    ):
        with pytest.raises(ShuntError):
            load_config(
                {"workspace_roots": [str(workspace)], **extra},
                default_spill_dir=tmp_path / "cache",
            )


# -- tool_result_capture / suma_post_tool config migration ------------------------------


def test_tool_result_capture_accepts_the_deprecated_suma_post_tool_alias(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = load_config(
        {"workspace_roots": [str(workspace)], "suma_post_tool": {"enabled": True}},
        default_spill_dir=tmp_path / "cache",
    )
    assert config.tool_result_capture.enabled is True
    assert config.suma_post_tool.enabled is True
    assert config.suma_post_tool is config.tool_result_capture


def test_tool_result_capture_key_takes_precedence_when_alias_absent(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = load_config(
        {"workspace_roots": [str(workspace)], "tool_result_capture": {"enabled": True}},
        default_spill_dir=tmp_path / "cache",
    )
    assert config.tool_result_capture.enabled is True


def test_tool_result_capture_agreeing_alias_and_canonical_key_is_accepted(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = load_config(
        {
            "workspace_roots": [str(workspace)],
            "tool_result_capture": {"enabled": True},
            "suma_post_tool": {"enabled": True},
        },
        default_spill_dir=tmp_path / "cache",
    )
    assert config.tool_result_capture.enabled is True


def test_tool_result_capture_conflicting_alias_and_canonical_key_is_refused(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ShuntError) as exc:
        load_config(
            {
                "workspace_roots": [str(workspace)],
                "tool_result_capture": {"enabled": True},
                "suma_post_tool": {"enabled": False},
            },
            default_spill_dir=tmp_path / "cache",
        )
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.detail == "TOOL_RESULT_CAPTURE_CONFIG_CONFLICT"


def test_tool_result_capture_host_ordering_attestation_field(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = load_config(
        {
            "workspace_roots": [str(workspace)],
            "tool_result_capture": {"enabled": True, "host_ordering_verified_locally": True},
        },
        default_spill_dir=tmp_path / "cache",
    )
    assert config.tool_result_capture.host_ordering_verified_locally is True
    # Default is false: an operator attestation is never assumed.
    default_config = load_config(
        {"workspace_roots": [str(workspace)]}, default_spill_dir=tmp_path / "cache"
    )
    assert default_config.tool_result_capture.host_ordering_verified_locally is False
