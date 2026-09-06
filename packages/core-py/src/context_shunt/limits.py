"""Normative caps, loaded from the shared contract rather than hardcoded.

Both language cores read ``contracts/v1/limits.json``. A deployment may lower a cap
(``Limits.narrow``); raising one is rejected here so a config file can never widen the
security envelope that the acceptance gates measure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts" / "v1"
SCHEMA_VERSION = "1.0"


@lru_cache(maxsize=None)
def _load(name: str) -> dict[str, Any]:
    with (CONTRACTS_DIR / name).open("rb") as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def raw_limits() -> dict[str, Any]:
    return _load("limits.json")


@lru_cache(maxsize=None)
def status_code_pairs() -> dict[str, Any]:
    return _load("status-code-pairs.json")


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
    max_targeted_read_bytes: int
    max_source_bytes: int
    max_chunk_bytes: int
    max_answer_bytes: int
    max_quote_bytes: int
    max_question_bytes: int
    session_spill_quota_bytes: int
    max_sources_per_request: int
    max_chunks_per_request: int
    max_citations: int
    max_concurrent_model_calls: int
    max_chunk_overlap_lines: int
    max_transient_retries: int
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
            max_targeted_read_bytes=raw["bytes"]["max_targeted_read_bytes"],
            max_source_bytes=raw["bytes"]["max_source_bytes"],
            max_chunk_bytes=raw["bytes"]["max_chunk_bytes"],
            max_answer_bytes=raw["bytes"]["max_answer_bytes"],
            max_quote_bytes=raw["bytes"]["max_quote_bytes"],
            max_question_bytes=raw["bytes"]["max_question_bytes"],
            session_spill_quota_bytes=raw["bytes"]["session_spill_quota_bytes"],
            max_sources_per_request=raw["counts"]["max_sources_per_request"],
            max_chunks_per_request=raw["counts"]["max_chunks_per_request"],
            max_citations=raw["counts"]["max_citations"],
            max_concurrent_model_calls=raw["counts"]["max_concurrent_model_calls"],
            max_chunk_overlap_lines=raw["counts"]["max_chunk_overlap_lines"],
            max_transient_retries=raw["counts"]["max_transient_retries"],
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
        )

    def narrow(self, **overrides: int) -> Limits:
        """Return a copy with lowered caps. Raising a cap is a configuration error."""
        from dataclasses import replace

        for key, value in overrides.items():
            if not hasattr(self, key):
                raise ValueError(f"unknown limit: {key}")
            current = getattr(self, key)
            if not isinstance(current, int):
                raise ValueError(f"limit is not numeric: {key}")
            if value > current:
                raise ValueError(f"limit {key} may only be narrowed (max {current})")
            if value < 0:
                raise ValueError(f"limit {key} may not be negative")
        return replace(self, **overrides)


DEFAULT_LIMITS = Limits.defaults()
READER_MODEL = DEFAULT_LIMITS.reader_model


def legal_pair(status: str, code: str) -> bool:
    pairs = status_code_pairs()["pairs"]
    return status in pairs and code in pairs[status]
