"""Citation verification.

``verified`` is written here and nowhere else. The verifier re-reads the snapshot the
citation names, checks that the handle belongs to this session, that the snapshot hash
matches in full, that the locator is in range, and that the quote is an exact substring
of the addressed line range (text) or of the canonical JSON of the addressed record.

A model claiming ``verified: true`` proves nothing; every assertion whose citation fails
is removed from the answer, and an answer with nothing left becomes ``CITATION_INVALID``.
Mechanical verification proves location and exactness only - semantic support is what
the opt-in Luna eval gate measures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits
from .registry import SourceRegistry
from .snapshot import canonical_json, record_at, record_count, resolve_pointer


class Reason(str, Enum):
    OK = "OK"
    LINE_OUT_OF_RANGE = "LINE_OUT_OF_RANGE"
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
    POINTER_NOT_FOUND = "POINTER_NOT_FOUND"
    RECORD_OUT_OF_RANGE = "RECORD_OUT_OF_RANGE"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    HANDLE_UNKNOWN = "HANDLE_UNKNOWN"
    HANDLE_EXPIRED = "HANDLE_EXPIRED"
    QUOTE_OVER_CAP = "QUOTE_OVER_CAP"
    LOCATOR_UNSUPPORTED = "LOCATOR_UNSUPPORTED"


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    reason: Reason


_CITATION_REF = re.compile(r"\[(c[0-9]{1,3})\]")


class CitationVerifier:
    def __init__(self, registry: SourceRegistry, limits: Limits = DEFAULT_LIMITS):
        self._registry = registry
        self._limits = limits

    def verify(self, session_id: str, citation: dict[str, Any]) -> VerificationResult:
        quote = citation.get("quote")
        if not isinstance(quote, str) or not quote:
            return VerificationResult(False, Reason.QUOTE_NOT_FOUND)
        if len(quote.encode("utf-8")) > self._limits.max_quote_bytes:
            return VerificationResult(False, Reason.QUOTE_OVER_CAP)

        try:
            entry = self._registry.resolve(session_id, str(citation.get("source_id", "")))
        except ShuntError as exc:
            reason = Reason.HANDLE_EXPIRED if exc.detail == "TTL_ELAPSED" else Reason.HANDLE_UNKNOWN
            return VerificationResult(False, reason)

        snapshot = entry.snapshot
        if citation.get("snapshot_id") != snapshot.snapshot_id:
            return VerificationResult(False, Reason.SNAPSHOT_MISMATCH)

        locator = citation.get("locator") or {}
        kind = locator.get("kind")
        if kind == "lines":
            return self._verify_lines(snapshot, locator, quote)
        if kind == "records":
            return self._verify_records(snapshot, locator, quote)
        # "all" and "search" address a scope, not a location, so they cannot support a quote.
        return VerificationResult(False, Reason.LOCATOR_UNSUPPORTED)

    def _verify_lines(self, snapshot, locator: dict[str, Any], quote: str) -> VerificationResult:
        if snapshot.json_value is not None:
            # A JSON snapshot is addressed by record, never by pretty-printed line number.
            return VerificationResult(False, Reason.LOCATOR_UNSUPPORTED)
        start, end = locator.get("start"), locator.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            return VerificationResult(False, Reason.LINE_OUT_OF_RANGE)
        if start < 1 or end < start or end > snapshot.line_count:
            return VerificationResult(False, Reason.LINE_OUT_OF_RANGE)
        try:
            window = snapshot.line_index.range_text(start, end)
        except (IndexError, UnicodeDecodeError):
            return VerificationResult(False, Reason.LINE_OUT_OF_RANGE)
        if quote not in window:
            return VerificationResult(False, Reason.QUOTE_NOT_FOUND)
        return VerificationResult(True, Reason.OK)

    def _verify_records(self, snapshot, locator: dict[str, Any], quote: str) -> VerificationResult:
        if snapshot.json_value is None:
            return VerificationResult(False, Reason.LOCATOR_UNSUPPORTED)
        pointer = locator.get("pointer")
        start, end = locator.get("start"), locator.get("end")
        if not isinstance(pointer, str) or not isinstance(start, int) or not isinstance(end, int):
            return VerificationResult(False, Reason.RECORD_OUT_OF_RANGE)
        if start < 1 or end < start:
            return VerificationResult(False, Reason.RECORD_OUT_OF_RANGE)
        try:
            node = resolve_pointer(snapshot.json_value, pointer)
        except ShuntError:
            return VerificationResult(False, Reason.POINTER_NOT_FOUND)
        if end > record_count(node):
            return VerificationResult(False, Reason.RECORD_OUT_OF_RANGE)
        try:
            records = [record_at(node, i) for i in range(start, end + 1)]
        except ShuntError:
            return VerificationResult(False, Reason.RECORD_OUT_OF_RANGE)
        window = "".join(canonical_json(r) for r in records)
        if quote not in window:
            return VerificationResult(False, Reason.QUOTE_NOT_FOUND)
        return VerificationResult(True, Reason.OK)


def referenced_ids(text: str) -> list[str]:
    return _CITATION_REF.findall(text)


def strip_unsupported_assertions(answer: str, valid_ids: set[str]) -> str:
    """Drop every sentence whose citation did not verify.

    Sentences with no citation at all are also dropped: an assertion about the source
    with no evidence is exactly what must not survive into the envelope.
    """
    if not answer.strip():
        return ""
    parts = re.split(r"(?<=[.!?。！？\n])\s+", answer.strip())
    kept: list[str] = []
    for part in parts:
        refs = set(referenced_ids(part))
        if refs and refs.issubset(valid_ids):
            kept.append(part.strip())
    return " ".join(kept).strip()
