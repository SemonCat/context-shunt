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

from .binaryguard import JSON_MEDIA_TYPE
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
        if snapshot.media_type == JSON_MEDIA_TYPE:
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
        if snapshot.media_type != JSON_MEDIA_TYPE:
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

    This is the legacy contract: a model that places its own ``[cN]`` markers in prose. It
    is kept, unmodified, for a response that already used that shape - see
    :func:`normalize_claims` and :func:`render_claims` for the current one, where the
    program places every marker instead of trusting the model to.
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


_CLAIM_CITATION_ID = re.compile(r"c[0-9]{1,3}")
_TRAILING_PUNCT = re.compile(r"^(.*?)([.!?。！？]*)$", re.S)


def normalize_claims(
    raw: Any, valid_local_ids: set[str], limits: Limits = DEFAULT_LIMITS
) -> list[dict[str, Any]]:
    """Structurally validate a model's ``claims`` array against its own ``citations``.

    A claim survives only if ``text`` is a non-empty string within
    ``limits.max_claim_text_bytes`` and ``citation_ids`` is a non-empty, duplicate-free
    list of well-formed ids that all appear in ``valid_local_ids`` - the ids the same
    response actually declared in its ``citations`` array (before namespacing). Unknown,
    duplicate or missing ids drop *that claim*, never the whole answer, and never guessed
    at: a dropped claim is exactly as much evidence-free as a legacy sentence with no
    marker, so it is held to the same fail-closed rule.

    This is structural validation only. Whether a surviving id also verifies against the
    snapshot bytes is decided later, once, by :class:`CitationVerifier` - this function
    never marks anything ``verified``.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[: limits.max_claims_per_answer]:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        ids = item.get("citation_ids")
        if not isinstance(text, str) or not text.strip():
            continue
        if len(text.encode("utf-8")) > limits.max_claim_text_bytes:
            continue
        if not isinstance(ids, list) or not ids or len(ids) > limits.max_citation_ids_per_claim:
            continue
        seen: set[str] = set()
        malformed = False
        for cid in ids:
            if (
                not isinstance(cid, str)
                or not _CLAIM_CITATION_ID.fullmatch(cid)
                or cid in seen
                or cid not in valid_local_ids
            ):
                malformed = True
                break
            seen.add(cid)
        if not malformed:
            out.append({"text": text.strip(), "citation_ids": list(ids)})
    return out


def render_claims(claims: list[dict[str, Any]]) -> str:
    """Deterministically render surviving claims into prose with ``[cN]`` markers.

    The model never places a marker itself; every one in a published answer is put there
    by this function, from a citation id the model supplied *and* the verifier confirmed.
    Marker placement is therefore no longer a formatting task the model can get right or
    wrong - the historical failure this replaces was exactly that: a correct answer with a
    valid ``citations`` entry, discarded because the marker was missing from the prose.

    A claim with no ``citation_ids`` is not rendered: an assertion with nothing left to
    support it is exactly what must not survive, matching the legacy rule in
    :func:`strip_unsupported_assertions`.
    """
    parts: list[str] = []
    for claim in claims:
        ids = claim.get("citation_ids") or []
        text = str(claim.get("text", "")).strip()
        if not ids or not text:
            continue
        markers = "".join(f"[{cid}]" for cid in ids)
        match = _TRAILING_PUNCT.match(text)
        body, punct = match.groups() if match else (text, "")
        rendered = f"{body} {markers}{punct}" if punct else f"{text} {markers}"
        parts.append(rendered.strip())
    return " ".join(parts).strip()
