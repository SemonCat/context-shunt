"""Normative caps, loaded from the shared contract rather than hardcoded.

Both language cores read ``contracts/v1/limits.json``. A deployment may lower a cap
(``Limits.narrow``); raising one is rejected here so a config file can never widen the
security envelope that the acceptance gates measure.

Contract revision
-----------------
Two version constants, deliberately separate:

* :data:`EMITTED_SCHEMA_VERSION` - the revision every envelope this core builds declares.
* :data:`SUPPORTED_REQUEST_VERSIONS` - the revisions this core will accept on input.

Revision 1.1 is backward compatible: a 1.0 request is still accepted unchanged, and a 1.0
envelope still validates. What 1.1 adds is mandatory *for envelopes that declare 1.1* -
``result_kind``, ``provenance`` and ``accounting_id`` - plus the optional extraction,
stats and recovery blocks and the inspect/stats operations. A request that declares 1.0
and carries a 1.1 field is rejected rather than accepted with the field ignored.

:data:`SCHEMA_VERSION` is kept as an alias of the emitted revision so existing call sites
keep working; new code should say which of the two it means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

CONTRACTS_ROOT = Path(__file__).resolve().parent / "contracts"
CONTRACTS_DIR = CONTRACTS_ROOT / "v1"
STORE_DDL_PATH = CONTRACTS_ROOT / "store" / "v1.sql"


@cache
def _load(name: str) -> dict[str, Any]:
    with (CONTRACTS_DIR / name).open("rb") as fh:
        return json.load(fh)


@cache
def raw_limits() -> dict[str, Any]:
    return _load("limits.json")


@cache
def status_code_pairs() -> dict[str, Any]:
    return _load("status-code-pairs.json")


EMITTED_SCHEMA_VERSION: str = raw_limits()["contract"]["emitted_version"]
SUPPORTED_REQUEST_VERSIONS: frozenset[str] = frozenset(
    raw_limits()["contract"]["supported_request_versions"]
)
#: Backward-compatible alias. Prefer the explicit constant that says which side you mean.
SCHEMA_VERSION: str = EMITTED_SCHEMA_VERSION

#: Fields that only exist from 1.1 onward. A request declaring an earlier revision that
#: carries one of these is refused, so an unknown mandatory field is never ignored.
V11_ONLY_REQUEST_FIELDS: frozenset[str] = frozenset({"refined"})
V11_ONLY_OPERATIONS: frozenset[str] = frozenset({"inspect", "stats"})
V11_ONLY_ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {
        "result_kind",
        "provenance",
        "accounting_id",
        "extraction",
        "stats",
        "recovery",
        "import_receipt",
    }
)


def store_ddl() -> str:
    """The normative store DDL, read from the vendored contract."""
    return STORE_DDL_PATH.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Limits:
    """Effective caps for one deployment. Frozen: nothing mutates caps at runtime."""

    reader_model: str
    full_read_max_lines: int
    targeted_read_max_lines: int
    targeted_search_max_matches: int
    probe_max_lines_scanned: int
    max_tool_result_bytes: int
    max_envelope_bytes: int
    max_extended_envelope_bytes: int
    max_targeted_read_bytes: int
    max_extraction_bytes: int
    max_source_bytes: int
    max_chunk_bytes: int
    max_answer_bytes: int
    max_quote_bytes: int
    max_question_bytes: int
    session_spill_quota_bytes: int
    max_claim_text_bytes: int
    max_sources_per_request: int
    max_chunks_per_request: int
    max_citations: int
    max_concurrent_model_calls: int
    max_chunk_overlap_lines: int
    max_transient_retries: int
    max_claims_per_answer: int
    max_citation_ids_per_claim: int
    max_format_retries: int
    max_chunk_tokens: int
    max_request_input_tokens: int
    max_output_tokens_per_call: int
    bytes_per_token_estimate: int
    gate_probe_deadline_ms: int
    spill_io_deadline_ms: int
    model_call_deadline_ms: int
    request_deadline_ms: int
    spill_ttl_seconds: int
    json_max_depth: int
    json_max_nodes: int
    # -- 1.1 additions ---------------------------------------------------
    inspect_max_result_bytes: int
    inspect_max_segments: int
    inspect_max_lines_per_page: int
    inspect_max_bytes_per_page: int
    inspect_max_scan_lines: int
    inspect_max_scan_bytes: int
    inspect_max_search_matches: int
    inspect_max_needle_bytes: int
    disclosure_max_per_source_bytes: int
    disclosure_max_per_session_bytes: int
    store_ddl_version: int
    store_busy_timeout_ms: int
    store_max_entries: int
    store_max_bytes: int
    store_handle_ttl_seconds: int
    stats_max_records_per_page: int
    stats_max_pages: int

    @classmethod
    def defaults(cls) -> Limits:
        raw = raw_limits()
        return cls(
            reader_model=raw["reader_model"],
            full_read_max_lines=raw["gate"]["full_read_max_lines"],
            targeted_read_max_lines=raw["gate"]["targeted_read_max_lines"],
            targeted_search_max_matches=raw["gate"]["targeted_search_max_matches"],
            probe_max_lines_scanned=raw["gate"]["probe_max_lines_scanned"],
            max_tool_result_bytes=raw["bytes"]["max_tool_result_bytes"],
            max_envelope_bytes=raw["bytes"]["max_envelope_bytes"],
            max_extended_envelope_bytes=raw["bytes"]["max_extended_envelope_bytes"],
            max_targeted_read_bytes=raw["bytes"]["max_targeted_read_bytes"],
            max_extraction_bytes=raw["bytes"]["max_extraction_bytes"],
            max_source_bytes=raw["bytes"]["max_source_bytes"],
            max_chunk_bytes=raw["bytes"]["max_chunk_bytes"],
            max_answer_bytes=raw["bytes"]["max_answer_bytes"],
            max_quote_bytes=raw["bytes"]["max_quote_bytes"],
            max_question_bytes=raw["bytes"]["max_question_bytes"],
            session_spill_quota_bytes=raw["bytes"]["session_spill_quota_bytes"],
            max_claim_text_bytes=raw["bytes"]["max_claim_text_bytes"],
            max_sources_per_request=raw["counts"]["max_sources_per_request"],
            max_chunks_per_request=raw["counts"]["max_chunks_per_request"],
            max_citations=raw["counts"]["max_citations"],
            max_concurrent_model_calls=raw["counts"]["max_concurrent_model_calls"],
            max_chunk_overlap_lines=raw["counts"]["max_chunk_overlap_lines"],
            max_transient_retries=raw["counts"]["max_transient_retries"],
            max_claims_per_answer=raw["counts"]["max_claims_per_answer"],
            max_citation_ids_per_claim=raw["counts"]["max_citation_ids_per_claim"],
            max_format_retries=raw["counts"]["max_format_retries"],
            max_chunk_tokens=raw["tokens"]["max_chunk_tokens"],
            max_request_input_tokens=raw["tokens"]["max_request_input_tokens"],
            max_output_tokens_per_call=raw["tokens"]["max_output_tokens_per_call"],
            bytes_per_token_estimate=raw["tokens"]["bytes_per_token_estimate"],
            gate_probe_deadline_ms=raw["deadlines_ms"]["gate_probe"],
            spill_io_deadline_ms=raw["deadlines_ms"]["spill_io"],
            model_call_deadline_ms=raw["deadlines_ms"]["model_call"],
            request_deadline_ms=raw["deadlines_ms"]["request"],
            spill_ttl_seconds=raw["spill"]["ttl_seconds"],
            json_max_depth=raw["json"]["max_depth"],
            json_max_nodes=raw["json"]["max_nodes"],
            inspect_max_result_bytes=raw["inspect"]["max_result_bytes"],
            inspect_max_segments=raw["inspect"]["max_segments"],
            inspect_max_lines_per_page=raw["inspect"]["max_lines_per_page"],
            inspect_max_bytes_per_page=raw["inspect"]["max_bytes_per_page"],
            inspect_max_scan_lines=raw["inspect"]["max_scan_lines"],
            inspect_max_scan_bytes=raw["inspect"]["max_scan_bytes"],
            inspect_max_search_matches=raw["inspect"]["max_search_matches"],
            inspect_max_needle_bytes=raw["inspect"]["max_needle_bytes"],
            disclosure_max_per_source_bytes=raw["disclosure"]["max_per_source_bytes"],
            disclosure_max_per_session_bytes=raw["disclosure"]["max_per_session_bytes"],
            store_ddl_version=raw["store"]["ddl_version"],
            store_busy_timeout_ms=raw["store"]["busy_timeout_ms"],
            store_max_entries=raw["store"]["max_entries"],
            store_max_bytes=raw["store"]["max_bytes"],
            store_handle_ttl_seconds=raw["store"]["handle_ttl_seconds"],
            stats_max_records_per_page=raw["accounting"]["max_stats_records_per_page"],
            stats_max_pages=raw["accounting"]["max_stats_pages"],
        )

    def narrow(self, **overrides: int) -> Limits:
        """Return a copy with lowered caps. Raising a cap is a configuration error."""
        from dataclasses import replace

        for key, value in overrides.items():
            if not hasattr(self, key):
                raise ValueError(f"unknown limit: {key}")
            current = getattr(self, key)
            if (
                not isinstance(current, int)
                or isinstance(value, bool)
                or not isinstance(value, int)
            ):
                raise ValueError(f"limit is not numeric: {key}")
            if value > current:
                raise ValueError(f"limit {key} may only be narrowed (max {current})")
            if value < 0:
                raise ValueError(f"limit {key} may not be negative")
            if value == 0 and key in POSITIVE_LIMITS:
                raise ValueError(f"limit {key} must be positive")
        return replace(self, **overrides)


#: Caps a deployment may not set to zero, because zero would disable rather than tighten.
POSITIVE_LIMITS = frozenset(
    {
        "bytes_per_token_estimate",
        "max_chunk_bytes",
        "max_chunk_tokens",
        "max_concurrent_model_calls",
        "inspect_max_result_bytes",
        "inspect_max_scan_lines",
        "store_ddl_version",
        "store_busy_timeout_ms",
        "stats_max_records_per_page",
        "stats_max_pages",
    }
)

DEFAULT_LIMITS = Limits.defaults()
READER_MODEL = DEFAULT_LIMITS.reader_model
#: Deterministic estimator name recorded whenever provider usage is not available.
BASELINE_ESTIMATE_METHOD: str = raw_limits()["accounting"]["baseline_method"]


def legal_pair(status: str, code: str) -> bool:
    pairs = status_code_pairs()["pairs"]
    return status in pairs and code in pairs[status]


def supported_request_version(version: Any) -> bool:
    return isinstance(version, str) and version in SUPPORTED_REQUEST_VERSIONS


def envelope_byte_cap(result_kind: str | None, limits: Limits = DEFAULT_LIMITS) -> int:
    """The serialized cap that applies to one envelope.

    Deterministic extraction and stats carry a bounded payload of their own - up to
    ``max_extraction_bytes`` of exact snapshot bytes, or one page of operation records -
    so they are measured against ``max_extended_envelope_bytes``. Every other envelope
    keeps the original 16 KiB cap. Both values live in ``contracts/v1/limits.json``.
    """
    if result_kind in ("deterministic_extraction", "stats"):
        return limits.max_extended_envelope_bytes
    return limits.max_envelope_bytes
