"""Question-driven read-only reader.

Order of operations, and why:

1. Validate against the v1 contract. A missing or blank question stops here, so the
   model invocation count for such a request is provably zero.
2. Resolve every source handle in this session and confirm the snapshot hash the caller
   named still matches. One unsafe/secret/binary source rejects the whole request rather
   than answering from the remaining ones.
3. Plan chunks under the token budget before any call is made.
4. Call Luna once per chunk with the original question, at most two concurrently, with
   at most one transient retry that spends the same shared budget.
5. Verify every citation against the snapshot, delete assertions that lost their
   evidence, and only then decide status/coverage.
6. Hand the result to the output guard.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import envelope as E
from .chunking import Chunk, plan
from .citations import CitationVerifier, referenced_ids, strip_unsupported_assertions
from .clock import Clock, Deadline, MonotonicClock
from .errors import CancelledError, DeadlineExceeded, ShuntError
from .limits import DEFAULT_LIMITS, READER_MODEL, Limits
from .metrics import MetricsSink, NullMetrics
from .paths import assert_no_secret
from .provider import LunaProvider, ModelUsage, build_user_message
from .registry import SourceRegistry
from .schema import validate_request


@dataclass
class ChunkOutcome:
    chunk: Chunk
    answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    failed_reason: str | None = None
    usage: ModelUsage = field(default_factory=ModelUsage)
    calls: int = 0


class Reader:
    def __init__(
        self,
        registry: SourceRegistry,
        provider: LunaProvider,
        *,
        limits: Limits = DEFAULT_LIMITS,
        clock: Clock | None = None,
        metrics: MetricsSink | None = None,
    ):
        self._registry = registry
        self._provider = provider
        self._limits = limits
        self._clock = clock or MonotonicClock()
        self._metrics = metrics or NullMetrics()
        self._verifier = CitationVerifier(registry, limits)

    # -- public ------------------------------------------------------------
    def answer(
        self, session_id: str, request: dict[str, Any], *, deadline: Deadline | None = None
    ) -> dict[str, Any]:
        request_id = str((request or {}).get("request_id") or "req_unknown")
        deadline = deadline or Deadline.start(self._clock, self._limits.request_deadline_ms)
        try:
            return self._answer(session_id, request, request_id, deadline)
        except ShuntError as exc:
            self._metrics.count("reader_error", {"code": exc.code})
            return E.error_envelope(request_id, exc)

    # -- internals ---------------------------------------------------------
    def _answer(
        self, session_id: str, request: dict[str, Any], request_id: str, deadline: Deadline
    ) -> dict[str, Any]:
        validate_request(request)
        question = request["question"]
        assert_no_secret(question.encode("utf-8"), "QUESTION")

        deadline.check("RESOLVE")
        selections: list[tuple[str, Any, dict[str, Any]]] = []
        handles: list[dict[str, Any]] = []
        for source in request["sources"]:
            entry = self._registry.resolve(session_id, source["source_id"])
            if entry.snapshot.snapshot_id != source["snapshot_id"]:
                raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")
            selections.append((entry.source_id, entry.snapshot, source["selector"]))
            handles.append(
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "media_type": entry.snapshot.media_type,
                    "bytes": entry.snapshot.bytes_len,
                    "expires_at": E.iso_expiry(entry.expires_at_epoch),
                }
            )

        search_only = [s for s in selections if s[2].get("kind") == "search"]
        if search_only:
            selections = [
                (sid, snap, _search_selector_to_lines(snap, sel))
                for sid, snap, sel in selections
            ]

        deadline.check("PLAN")
        # A search that matched nothing contributes no range; it must become NO_MATCH,
        # not an out-of-range planning error.
        selections = [
            (sid, snap, sel)
            for sid, snap, sel in selections
            if not (sel.get("kind") == "lines" and int(sel.get("end", 0)) < int(sel.get("start", 1)))
        ]
        budgets = request["budgets"]
        the_plan = plan(selections, max_chunks=budgets["max_chunks"], limits=self._limits)
        coverage = E.Coverage(planned_chunks=len(the_plan.chunks))
        for omission in the_plan.omitted:
            coverage.omit(omission["source_id"], omission["selector"], omission["reason"])

        if not the_plan.chunks:
            coverage.upstream_truncated = False
            return E.build(
                request_id=request_id,
                status="ok",
                code="NO_MATCH",
                coverage=E.Coverage(
                    complete=True, processed_chunks=0, planned_chunks=0, upstream_truncated=False
                ),
                sources=handles,
            )

        outcomes = self._run_chunks(question, the_plan.chunks, deadline)

        answers: list[str] = []
        raw_citations: list[dict[str, Any]] = []
        total_calls = 0
        usage_in = usage_out = 0
        for outcome in outcomes:
            total_calls += outcome.calls
            usage_in += outcome.usage.input_tokens
            usage_out += outcome.usage.output_tokens
            if outcome.failed_reason:
                coverage.omit(
                    outcome.chunk.source_id, outcome.chunk.locator, outcome.failed_reason
                )
                continue
            coverage.processed_chunks += 1
            if outcome.answer:
                answers.append(outcome.answer)
            raw_citations.extend(outcome.citations)

        self._metrics.observe("reader_model_calls", total_calls, {"model": READER_MODEL})
        self._metrics.observe("reader_input_tokens", usage_in, {"model": READER_MODEL})
        self._metrics.observe("reader_output_tokens", usage_out, {"model": READER_MODEL})

        verified, rejected = self._verify_all(session_id, raw_citations)
        self._metrics.observe("citations_verified", len(verified), {"result": "verified"})
        self._metrics.observe("citations_rejected", rejected, {"result": "rejected"})

        answer = strip_unsupported_assertions(" ".join(answers), {c["id"] for c in verified})
        used_ids = set(referenced_ids(answer))
        verified = [c for c in verified if c["id"] in used_ids][: self._limits.max_citations]
        answer = _cap_bytes(answer, min(budgets["max_answer_bytes"], self._limits.max_answer_bytes))

        deadline.check("PUBLISH")
        complete = (
            not coverage.omitted
            and coverage.processed_chunks == coverage.planned_chunks
            and coverage.planned_chunks > 0
        )
        coverage.upstream_truncated = False

        if not answer:
            if rejected and not verified and raw_citations:
                raise ShuntError("CITATION_INVALID", "NO_VALID_EVIDENCE")
            coverage.complete = complete
            return E.build(
                request_id=request_id,
                status="ok" if complete else "partial",
                code="NO_MATCH",
                coverage=coverage,
                sources=handles,
            )

        coverage.complete = complete
        return E.build(
            request_id=request_id,
            status="ok" if complete else "partial",
            code="ANSWERED",
            answer=answer,
            citations=verified,
            coverage=coverage,
            sources=handles,
        )

    def _run_chunks(
        self, question: str, chunks: tuple[Chunk, ...], deadline: Deadline
    ) -> list[ChunkOutcome]:
        workers = min(self._limits.max_concurrent_model_calls, max(1, len(chunks)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._run_chunk, question, chunk, deadline) for chunk in chunks]
            return [f.result() for f in futures]

    def _run_chunk(self, question: str, chunk: Chunk, deadline: Deadline) -> ChunkOutcome:
        outcome = ChunkOutcome(chunk=chunk)
        attempts = 1 + self._limits.max_transient_retries
        for attempt in range(attempts):
            try:
                deadline.check("MODEL_CALL")
            except (DeadlineExceeded, CancelledError) as exc:
                outcome.failed_reason = "TIMEOUT" if exc.code == "TIMEOUT" else "CANCELLED"
                return outcome
            try:
                response = self._provider.complete(
                    system=_system_prompt(),
                    user=build_user_message(question, chunk.text, chunk.locator),
                    max_output_tokens=self._limits.max_output_tokens_per_call,
                    timeout_ms=deadline.sub_budget(self._limits.model_call_deadline_ms),
                )
                outcome.calls += 1
                outcome.usage = response.usage
                parsed = _parse_model_json(response.text)
                outcome.answer = str(parsed.get("answer", ""))
                outcome.citations = _normalize_citations(parsed.get("citations", []), chunk)
                return outcome
            except ShuntError as exc:
                outcome.calls += 1
                if exc.retryable and attempt + 1 < attempts and not deadline.expired():
                    continue
                outcome.failed_reason = (
                    "MODEL_ERROR" if exc.code == "MODEL_ERROR"
                    else "INVALID_MODEL_OUTPUT" if exc.code == "INVALID_MODEL_OUTPUT"
                    else "TIMEOUT" if exc.code == "TIMEOUT"
                    else "CHUNK_FAILED"
                )
                return outcome
        outcome.failed_reason = "CHUNK_FAILED"
        return outcome

    def _verify_all(
        self, session_id: str, citations: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        verified: list[dict[str, Any]] = []
        rejected = 0
        seen: set[str] = set()
        for citation in citations:
            result = self._verifier.verify(session_id, citation)
            if not result.verified:
                rejected += 1
                continue
            cid = citation["id"]
            if cid in seen:
                continue
            seen.add(cid)
            verified.append({**citation, "verified": True})
        return verified, rejected


def _system_prompt() -> str:
    from .provider import READER_SYSTEM_PROMPT

    return READER_SYSTEM_PROMPT


def _cap_bytes(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    raw = raw[:max_bytes]
    while raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]
    return ""


def _parse_model_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[: -3]
    try:
        value = json.loads(stripped)
    except ValueError:
        raise ShuntError("INVALID_MODEL_OUTPUT", "NOT_JSON", retryable=False) from None
    if not isinstance(value, dict):
        raise ShuntError("INVALID_MODEL_OUTPUT", "NOT_OBJECT", retryable=False)
    return value


def _normalize_citations(raw: Any, chunk: Chunk) -> list[dict[str, Any]]:
    """Rebuild each citation from trusted chunk metadata.

    Only the id, the addressed range and the quote come from the model; the source and
    snapshot always come from the chunk, and ``verified`` is never taken from the model.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:64]:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id", ""))
        quote = item.get("quote")
        if not cid or not isinstance(quote, str):
            continue
        locator = _locator_for(item, chunk)
        if locator is None:
            continue
        out.append(
            {
                "id": cid,
                "source_id": chunk.source_id,
                "snapshot_id": chunk.snapshot_id,
                "locator": locator,
                "quote": quote,
            }
        )
    return out


def _locator_for(item: dict[str, Any], chunk: Chunk) -> dict[str, Any] | None:
    if chunk.locator["kind"] == "lines":
        start = item.get("line_start")
        end = item.get("line_end", start)
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        return {"kind": "lines", "start": start, "end": end}
    start = item.get("record_start", item.get("line_start"))
    end = item.get("record_end", item.get("line_end", start))
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return {
        "kind": "records",
        "pointer": chunk.locator.get("pointer", ""),
        "start": start,
        "end": end,
    }


def _search_selector_to_lines(snapshot, selector: dict[str, Any]) -> dict[str, Any]:
    """Resolve a bounded literal search to the line range that actually matched.

    Only literal patterns are accepted, so match time is linear in the snapshot size and
    no user-supplied regex can be made to backtrack.
    """
    pattern = selector["pattern"]
    limit = int(selector["max_matches"])
    hits: list[int] = []
    for ordinal in range(1, snapshot.line_count + 1):
        try:
            line = snapshot.line_index.line_text(ordinal)
        except UnicodeDecodeError:
            continue
        if pattern in line:
            hits.append(ordinal)
            if len(hits) >= limit:
                break
    if not hits:
        return {"kind": "lines", "start": 1, "end": 0}
    return {"kind": "lines", "start": min(hits), "end": max(hits)}
