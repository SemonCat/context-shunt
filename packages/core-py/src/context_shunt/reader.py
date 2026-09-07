"""Question-driven read-only reader.

Order of operations, and why:

1. Validate against the contract. A missing or blank question stops here, so the model
   invocation count for such a request is provably zero.
2. Resolve every source handle in this scope and confirm the snapshot hash the caller
   named still matches. One unsafe/secret/binary source rejects the whole request rather
   than answering from the remaining ones. A refined question reuses that same immutable
   snapshot - it never silently recaptures the source.
3. Plan chunks under the token budget before any call is made.
4. Call the reader model once per chunk with the original question, at most two
   concurrently, with at most one transient retry that spends the same shared budget.
5. Verify every citation against the snapshot, delete assertions that lost their evidence,
   and only then decide status/coverage.
6. Attach truthful provenance and hand the result to the output guard.

What the reader publishes about itself
--------------------------------------
Every answer is labelled ``model_derived`` with ``provenance.derived = true``: it is a
model's reading of the source, not the source. The provenance block keeps requested,
resolved and reported provider/model apart and states the strongest attribution the host
actually supports - which on a host whose plugin LLM facade cannot distinguish a provider
report from an echo of the request is ``unverified``, not ``actual``.

Failure preserves recovery
--------------------------
A provider failure, a timeout, a malformed response, a citation failure or a provenance
failure leaves every handle valid. The failure envelope says so and names deterministic
next steps, so the caller retries or refines over the same snapshot instead of paying to
capture the source again.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from . import envelope as E
from .accounting import ReaderCost
from .accounting import estimate_tokens as accounting_tokens
from .chunking import Chunk, estimate_tokens, plan
from .citations import CitationVerifier, referenced_ids, strip_unsupported_assertions
from .clock import Clock, Deadline, MonotonicClock
from .errors import CancelledError, DeadlineExceeded, ShuntError
from .limits import DEFAULT_LIMITS, Limits, envelope_byte_cap
from .metrics import MetricsSink, NullMetrics
from .paths import assert_no_secret
from .provenance import (
    Attribution,
    AttributionPolicy,
    Confidence,
    ModelIdentity,
    Provenance,
    ProvenanceLabel,
    ResultKind,
    TokenMethod,
    Usage,
    enforce_policy,
)
from .provider import (
    READER_SYSTEM_PROMPT,
    ModelResponse,
    ProviderTarget,
    ReaderProvider,
    TransientProviderError,
    build_user_message,
)
from .registry import SourceRegistry
from .schema import validate_request


@dataclass
class ChunkOutcome:
    chunk: Chunk
    answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    failed_reason: str | None = None
    # An empty accumulator, not an attempt that reported nothing. `Usage()` defaults to
    # `UNKNOWN`, which is the right answer for a *bridge* that returned no counts - but as
    # a starting value it poisoned the merge: `UNKNOWN + EXACT` is `UNKNOWN`, so exact
    # provider usage was downgraded to a byte estimate on *every* request, and the
    # `usage_exact` distinction the accounting layer exists to make never survived.
    usage: Usage = field(default_factory=lambda: Usage(method=TokenMethod.NOT_APPLICABLE))
    calls: int = 0
    usage_complete_calls: int = 0
    prompt_bytes: int = 0
    completion_bytes: int = 0
    attribution: Attribution = Attribution.UNKNOWN
    confidence: Confidence = Confidence.NONE
    resolved: ModelIdentity = field(default_factory=ModelIdentity)
    reported: ModelIdentity = field(default_factory=ModelIdentity)
    fallback_used: bool = False


#: Bounded wait used only when a request has already missed its deadline, to collect the
#: cost of a call the bridge had in fact completed. Never used on a publishing path.
_LATE_CALL_ACCOUNTING_WAIT_S = 0.25


@dataclass
class _CostSink:
    """Carries what an attempt actually spent out past a later failure.

    ``_answer`` fills this in as soon as the cost is known, so the error path in
    ``answer`` can report real spend instead of "no attempt was made".
    """

    cost: ReaderCost = field(default_factory=ReaderCost.none)

    @property
    def attempts(self) -> int:
        return self.cost.attempts_started

    def record(self, cost: ReaderCost) -> ReaderCost:
        self.cost = cost
        return cost


@dataclass
class ReaderResult(dict):
    """What the session needs to finish the operation: an envelope plus its true cost.

    Also a ``dict`` carrying the envelope. ``Reader.answer`` used to return the envelope
    dict itself, and the 1.1 revision changed it to this record without an overload, so
    every ``answer(...)["status"]`` call site broke. A ``Mapping`` restored subscripting
    but not the rest: ``json.dumps`` still raised, and code requiring a real ``dict`` still
    failed. Subclassing ``dict`` keeps the whole published API working:

        result["status"]      -> the envelope's status  (the pre-1.1 shape)
        result.envelope       -> the same dict, explicitly
        result.provenance     -> the rich Provenance object, not the envelope's dict

    Bracket access always means "the envelope"; attribute access means "the record". The
    two never collide even though both carry a ``provenance``, because they are reached
    by different syntax. :meth:`Reader.answer_envelope` is the explicit alternative for
    callers that only ever wanted the envelope.
    """

    envelope: dict[str, Any]
    provenance: Provenance
    cost: ReaderCost
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Carry the envelope's own contents, so a pre-1.1 caller can subscript it, pass it
        # to `json.dumps`, hand it to something that requires a real `dict`, or compare it
        # against an expected envelope - all of which a `Mapping` refused. The envelope
        # stays the authority; this is a snapshot of it taken at construction, and an
        # envelope is never mutated after it is built.
        dict.update(self, self.envelope)


class _InputTokenBudget:
    def __init__(self, maximum: int):
        self._maximum = maximum
        self._spent = 0
        self._lock = threading.Lock()

    def spend(self, tokens: int) -> None:
        with self._lock:
            if tokens < 0 or self._spent + tokens > self._maximum:
                raise ShuntError("LIMIT_EXCEEDED", "REQUEST_OVER_TOKEN_CAP", retryable=False)
            self._spent += tokens


class Reader:
    def __init__(
        self,
        registry: SourceRegistry,
        provider: ReaderProvider,
        *,
        limits: Limits = DEFAULT_LIMITS,
        clock: Clock | None = None,
        metrics: MetricsSink | None = None,
        attribution_policy: AttributionPolicy = AttributionPolicy.ALLOW_UNVERIFIED,
    ):
        self._registry = registry
        self._provider = provider
        self._limits = limits
        self._clock = clock or MonotonicClock()
        self._metrics = metrics or NullMetrics()
        self._verifier = CitationVerifier(registry, limits)
        self._policy = attribution_policy

    # -- public ------------------------------------------------------------

    def answer_envelope(
        self,
        session_id: str,
        request: dict[str, Any],
        *,
        deadline: Deadline | None = None,
        accounting_id: str | None = None,
    ) -> dict[str, Any]:
        """:meth:`answer`, returning only the envelope.

        The explicit form of the pre-1.1 contract, for callers that never wanted the cost
        and provenance record. :class:`ReaderResult` also subscripts like the envelope, so
        existing call sites keep working either way.
        """
        return self.answer(
            session_id, request, deadline=deadline, accounting_id=accounting_id
        ).envelope

    def answer(
        self,
        session_id: str,
        request: dict[str, Any],
        *,
        deadline: Deadline | None = None,
        accounting_id: str | None = None,
    ) -> ReaderResult:
        request_id = _read_request_id(request)
        requested_deadline = _read_requested_deadline(request, self._limits.request_deadline_ms)
        deadline = deadline or Deadline.start(self._clock, requested_deadline)
        # Model calls are billed the moment they complete, but the request budget is
        # checked again at PUBLISH. A request that ran its calls and then ran out of time
        # used to report `ReaderCost.none()` - "no attempt was made" - so real spend
        # vanished from the session's accounting and every savings figure derived from it
        # was overstated. Whatever was actually spent before the failure is carried out.
        spent = _CostSink()
        try:
            return self._answer(session_id, request, request_id, deadline, accounting_id, spent)
        except ShuntError as exc:
            self._metrics.count("reader_error", {"code": exc.code})
            provenance = self._failure_provenance(exc, attempts_started=spent.attempts)
            return ReaderResult(
                envelope=E.error_envelope(
                    request_id,
                    exc,
                    accounting_id=accounting_id,
                    provenance=provenance,
                    handles_valid=_handles_survive(exc),
                ),
                provenance=provenance,
                cost=spent.cost,
            )

    # -- internals ---------------------------------------------------------

    def _answer(
        self,
        session_id: str,
        request: dict[str, Any],
        request_id: str,
        deadline: Deadline,
        accounting_id: str | None,
        spent: _CostSink,
    ) -> ReaderResult:
        request = validate_request(request)
        question = request["question"]
        assert_no_secret(question.encode("utf-8"), "QUESTION")

        deadline.check("RESOLVE")
        selections: list[tuple[str, Any, dict[str, Any]]] = []
        handles: list[dict[str, Any]] = []
        source_ids: list[str] = []
        for source in request["sources"]:
            entry = self._registry.resolve(session_id, source["source_id"])
            if entry.snapshot.snapshot_id != source["snapshot_id"]:
                # A refined question must address the snapshot it was given. Recapturing
                # here would answer a new question about a different file under the old
                # hash, so it is refused instead.
                raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")
            selections.append((entry.source_id, entry.snapshot, source["selector"]))
            source_ids.append(entry.source_id)
            handles.append(
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "media_type": entry.snapshot.media_type,
                    "bytes": entry.snapshot.bytes_len,
                    "expires_at": E.iso_expiry(entry.expires_at_epoch),
                }
            )

        if any(s[2].get("kind") == "search" for s in selections):
            selections = [
                (sid, snap, _search_selector_to_lines(snap, sel)) for sid, snap, sel in selections
            ]

        deadline.check("PLAN")
        # A search that matched nothing contributes no range; it must become NO_MATCH,
        # not an out-of-range planning error.
        selections = [
            (sid, snap, sel)
            for sid, snap, sel in selections
            if not (
                sel.get("kind") == "lines" and int(sel.get("end", 0)) < int(sel.get("start", 1))
            )
        ]
        budgets = request["budgets"]
        the_plan = plan(
            selections,
            max_chunks=budgets["max_chunks"],
            limits=self._limits,
            question=question,
        )
        coverage = E.Coverage(planned_chunks=len(the_plan.chunks))
        for omission in the_plan.omitted:
            coverage.omit(omission["source_id"], omission["selector"], omission["reason"])

        if not the_plan.chunks:
            # Nothing to read means nothing was generated: the answer is empty and the
            # provenance says no model output rather than claiming a derived answer.
            provenance = self._no_output_provenance()
            return ReaderResult(
                envelope=E.build(
                    request_id=request_id,
                    status="ok",
                    code="NO_MATCH",
                    coverage=E.Coverage(
                        complete=True,
                        processed_chunks=0,
                        planned_chunks=0,
                        upstream_truncated=False,
                    ),
                    sources=handles,
                    result_kind=ResultKind.MODEL_DERIVED,
                    provenance=provenance,
                    accounting_id=accounting_id,
                ),
                provenance=provenance,
                cost=ReaderCost.none(),
                source_ids=tuple(source_ids),
            )

        outcomes = self._run_chunks(
            question,
            the_plan.chunks,
            deadline,
            _InputTokenBudget(self._limits.max_request_input_tokens),
        )

        answers: list[str] = []
        raw_citations: list[dict[str, Any]] = []
        total_calls = 0
        usage_complete_calls = 0
        usage = Usage(method=TokenMethod.NOT_APPLICABLE)
        prompt_bytes = completion_bytes = 0
        next_citation = 1
        attribution = Attribution.NOT_APPLICABLE
        confidence = Confidence.NONE
        resolved = ModelIdentity()
        reported = ModelIdentity()
        fallback_used = False

        for outcome in outcomes:
            total_calls += outcome.calls
            usage_complete_calls += outcome.usage_complete_calls
            usage = usage.merge(outcome.usage)
            prompt_bytes += outcome.prompt_bytes
            completion_bytes += outcome.completion_bytes
            fallback_used = fallback_used or outcome.fallback_used
            if outcome.calls:
                attribution, confidence = _weakest(
                    attribution, confidence, outcome.attribution, outcome.confidence
                )
                resolved = resolved if resolved.known else outcome.resolved
                reported = reported if reported.known else outcome.reported
            if outcome.failed_reason:
                coverage.omit(outcome.chunk.source_id, outcome.chunk.locator, outcome.failed_reason)
                continue
            coverage.processed_chunks += 1
            namespaced_answer, namespaced_citations, allocated = _namespace_outcome(
                outcome, next_citation
            )
            next_citation += allocated
            if namespaced_answer:
                answers.append(namespaced_answer)
            raw_citations.extend(namespaced_citations)

        target = _target_of(self._provider)
        self._metrics.observe("reader_model_calls", total_calls)
        self._metrics.observe("reader_attempts_usage_complete", usage_complete_calls)

        cost = spent.record(
            _reader_cost(
                usage,
                attempts=total_calls,
                usage_complete=usage_complete_calls,
                prompt_bytes=prompt_bytes,
                completion_bytes=completion_bytes,
                limits=self._limits,
            )
        )

        verified, rejected = self._verify_all(session_id, raw_citations)
        self._metrics.observe("citations_verified", len(verified), {"result": "verified"})
        self._metrics.observe("citations_rejected", rejected, {"result": "rejected"})

        allowed = verified[: self._limits.max_citations]
        allowed_ids = {c["id"] for c in allowed}
        answer = strip_unsupported_assertions(" ".join(answers), allowed_ids)
        answer = _cap_bytes(answer, min(budgets["max_answer_bytes"], self._limits.max_answer_bytes))
        # Truncation can remove a marker or split an assertion. Verify the exact string
        # that will cross the output boundary a second time.
        answer = strip_unsupported_assertions(answer, allowed_ids)
        used_ids = set(referenced_ids(answer))
        verified = [c for c in allowed if c["id"] in used_ids]

        provenance = Provenance(
            derived=True,
            label=(
                ProvenanceLabel.MODEL_GENERATED_ANSWER
                if total_calls
                else ProvenanceLabel.NO_MODEL_OUTPUT
            ),
            attribution_status=attribution,
            attribution_confidence=confidence,
            attribution_policy=(self._policy if total_calls else AttributionPolicy.NOT_APPLICABLE),
            attempts_started=total_calls,
            usage_complete=bool(total_calls) and usage_complete_calls == total_calls,
            citations_mechanically_verified=True,
            requested=target.identity(),
            resolved=resolved,
            reported=reported,
            fallback_used=fallback_used if total_calls else None,
        )
        # Policy runs before publication so a refused attribution never ships an answer.
        # The failure keeps the provenance it was judged on: an operator needs to see the
        # value that contradicted the request, not a blank "unknown".
        try:
            enforce_policy(provenance, self._policy)
        except ShuntError as exc:
            self._metrics.count("reader_error", {"code": exc.code})
            refused = _as_failure_provenance(provenance)
            return ReaderResult(
                envelope=E.error_envelope(
                    request_id,
                    exc,
                    accounting_id=accounting_id,
                    provenance=refused,
                    sources=handles,
                    handles_valid=True,
                ),
                provenance=refused,
                cost=cost,
                source_ids=tuple(source_ids),
            )

        deadline.check("PUBLISH")
        complete = (
            not coverage.omitted
            and coverage.processed_chunks == coverage.planned_chunks
            and coverage.planned_chunks > 0
        )
        coverage.upstream_truncated = False

        if not answer:
            if rejected and not verified and raw_citations:
                # The handles are still valid and the caller is told so, so they have to be
                # listed too: "recovery.handles_valid: true" is only actionable if the
                # envelope still says which handles survived.
                exc = ShuntError("CITATION_INVALID", "NO_VALID_EVIDENCE")
                # Nothing survived verification, so nothing model-generated is published:
                # the failure is labelled not-derived while keeping the attribution facts.
                failed = _as_failure_provenance(provenance)
                return ReaderResult(
                    envelope=E.error_envelope(
                        request_id,
                        exc,
                        accounting_id=accounting_id,
                        provenance=failed,
                        sources=handles,
                        handles_valid=True,
                    ),
                    provenance=failed,
                    cost=cost,
                    source_ids=tuple(source_ids),
                )
            coverage.complete = complete
            return ReaderResult(
                envelope=E.build(
                    request_id=request_id,
                    status="ok" if complete else "partial",
                    code="NO_MATCH",
                    coverage=coverage,
                    sources=handles,
                    result_kind=ResultKind.MODEL_DERIVED,
                    provenance=provenance,
                    accounting_id=accounting_id,
                ),
                provenance=provenance,
                cost=cost,
                source_ids=tuple(source_ids),
            )

        def answered(answer_text: str, citations: list[dict[str, Any]], ok: bool) -> dict[str, Any]:
            coverage.complete = ok
            return E.build(
                request_id=request_id,
                status="ok" if ok else "partial",
                code="ANSWERED",
                answer=answer_text,
                citations=citations,
                coverage=coverage,
                sources=handles,
                result_kind=ResultKind.MODEL_DERIVED,
                provenance=provenance,
                accounting_id=accounting_id,
            )

        answer, verified, dropped = self._fit_to_envelope(answer, verified, coverage, answered)
        if not answer:
            # Every piece of evidence had to go, so there is no supported answer left to
            # publish. Saying NO_MATCH here would claim the sources held nothing, which is a
            # different and untrue statement; the honest report is that it would not fit.
            exc = ShuntError("LIMIT_EXCEEDED", "ANSWER_OVER_ENVELOPE", retryable=False)
            self._metrics.count("reader_error", {"code": exc.code})
            failed = _as_failure_provenance(provenance)
            return ReaderResult(
                envelope=E.error_envelope(
                    request_id,
                    exc,
                    accounting_id=accounting_id,
                    provenance=failed,
                    sources=handles,
                    handles_valid=True,
                ),
                provenance=failed,
                cost=cost,
                source_ids=tuple(source_ids),
            )

        return ReaderResult(
            envelope=answered(answer, verified, complete and not dropped),
            provenance=provenance,
            cost=cost,
            source_ids=tuple(source_ids),
        )

    def _fit_to_envelope(
        self,
        answer: str,
        verified: list[dict[str, Any]],
        coverage: E.Coverage,
        build: Any,
    ) -> tuple[str, list[dict[str, Any]], int]:
        """Shrink an over-large answer until the output guard will accept it.

        Every field can be individually within its cap while the assembled envelope is not:
        a full answer plus the maximum number of maximum-length quotes already exceeds the
        16 KiB envelope cap before JSON escaping is counted, and quotes are copied from
        source text, so quote-dense sources escape wide. Without this the guard converts a
        good, fully verified answer into a bare ``LIMIT_EXCEEDED`` - after the model call
        has been paid for, and with no indication of what went wrong.

        Evidence is dropped largest-first rather than last-first: the model's citation order
        is arbitrary, so trimming by position would make the surviving set depend on it,
        while trimming by cost is deterministic and converges fastest. Each drop re-strips
        the assertions it orphaned, which shrinks the answer too, so the loop re-measures
        between drops and stops as soon as it fits.
        """
        dropped = 0
        # One drop per pass, so this cannot run longer than there are citations.
        for _ in range(len(verified) + 1):
            candidate = build(answer, verified, False)
            # The same function the guard uses, not a constant: if a later revision moves
            # model_derived to a different cap, trimming must move with it rather than
            # quietly dropping evidence that would have fit.
            cap = envelope_byte_cap(candidate.get("result_kind"), self._limits)
            if E.serialized_bytes(candidate) <= cap:
                return answer, verified, dropped
            if not verified:
                return "", [], dropped
            victim = max(verified, key=lambda c: E.serialized_bytes(c))
            coverage.omit(
                str(victim.get("source_id", "")),
                victim.get("locator") or {"kind": "all"},
                "BUDGET_EXCEEDED",
            )
            dropped += 1
            kept = [c for c in verified if c["id"] != victim["id"]]
            answer = strip_unsupported_assertions(answer, {c["id"] for c in kept})
            used = set(referenced_ids(answer))
            verified = [c for c in kept if c["id"] in used]
        return "", [], dropped

    # -- provenance for paths that never produced model output --------------

    def _no_output_provenance(self) -> Provenance:
        return Provenance(
            derived=True,
            label=ProvenanceLabel.NO_MODEL_OUTPUT,
            attribution_status=Attribution.NOT_APPLICABLE,
            attribution_confidence=Confidence.NONE,
            attribution_policy=AttributionPolicy.NOT_APPLICABLE,
            attempts_started=0,
            usage_complete=True,
            requested=_target_of(self._provider).identity(),
        )

    def _failure_provenance(self, exc: ShuntError, *, attempts_started: int = 0) -> Provenance:
        """Provenance for a request that published nothing.

        ``attempts_started`` is not always zero: a request can complete its model calls
        and then fail at PUBLISH, and reporting no attempts there would contradict the
        cost the same envelope carries.
        """
        return Provenance(
            derived=False,
            label=ProvenanceLabel.NO_MODEL_OUTPUT,
            attribution_status=(
                Attribution.UNKNOWN
                if exc.code in ("MODEL_ERROR", "INVALID_MODEL_OUTPUT", "PROVENANCE_UNAVAILABLE")
                else Attribution.NOT_APPLICABLE
            ),
            attribution_confidence=Confidence.NONE,
            attribution_policy=self._policy,
            attempts_started=attempts_started,
            usage_complete=False,
            requested=_target_of(self._provider).identity(),
        )

    # -- chunk execution ---------------------------------------------------

    def _run_chunks(
        self,
        question: str,
        chunks: tuple[Chunk, ...],
        deadline: Deadline,
        input_budget: _InputTokenBudget,
    ) -> list[ChunkOutcome]:
        workers = min(self._limits.max_concurrent_model_calls, max(1, len(chunks)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._run_chunk, question, chunk, deadline, input_budget)
                for chunk in chunks
            ]
            return [f.result() for f in futures]

    def _run_chunk(
        self,
        question: str,
        chunk: Chunk,
        deadline: Deadline,
        input_budget: _InputTokenBudget,
    ) -> ChunkOutcome:
        outcome = ChunkOutcome(chunk=chunk)
        attempts = 1 + self._limits.max_transient_retries
        for attempt in range(attempts):
            try:
                deadline.check("MODEL_CALL")
            except (DeadlineExceeded, CancelledError) as exc:
                outcome.failed_reason = "TIMEOUT" if exc.code == "TIMEOUT" else "CANCELLED"
                return outcome
            try:
                user = build_user_message(question, chunk.text, chunk.locator)
                input_budget.spend(
                    estimate_tokens(READER_SYSTEM_PROMPT, self._limits)
                    + estimate_tokens(user, self._limits)
                )
                outcome.calls += 1
                outcome.prompt_bytes += len(READER_SYSTEM_PROMPT.encode("utf-8")) + len(
                    user.encode("utf-8")
                )
                response = self._complete_with_deadline(
                    system=READER_SYSTEM_PROMPT,
                    user=user,
                    max_output_tokens=self._limits.max_output_tokens_per_call,
                    deadline=deadline,
                    outcome=outcome,
                )
                _validate_model_response(response, self._limits)
                # An availability fallback may have taken several attempts inside this one
                # call, and every one of them reached a provider and was billed. `calls`
                # was already incremented once above for the attempt we started.
                outcome.calls += max(0, response.attempts - 1)
                outcome.completion_bytes += len(response.text.encode("utf-8"))
                outcome.usage = outcome.usage.merge(response.usage)
                outcome.usage_complete_calls += 1 if response.usage.complete else 0
                outcome.attribution, outcome.confidence = response.attribution()
                outcome.resolved = response.resolved
                outcome.reported = response.reported
                outcome.fallback_used = outcome.fallback_used or response.fallback_used
                if outcome.attribution is Attribution.MISMATCH:
                    raise ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", retryable=False)
                parsed = _parse_model_json(response.text, self._limits.max_tool_result_bytes)
                if not isinstance(parsed.get("answer"), str) or not isinstance(
                    parsed.get("citations"), list
                ):
                    raise ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", retryable=False)
                assert_no_secret(parsed["answer"].encode("utf-8"), "ANSWER")
                outcome.answer = parsed["answer"]
                outcome.citations = _normalize_citations(parsed.get("citations", []), chunk)
                return outcome
            except Exception as raw_exc:
                exc = (
                    raw_exc
                    if isinstance(raw_exc, ShuntError)
                    else TransientProviderError("PROVIDER_CALL_FAILED")
                )
                if (
                    exc.code == "MODEL_ERROR"
                    and exc.retryable
                    and attempt + 1 < attempts
                    and not deadline.expired()
                ):
                    continue
                outcome.failed_reason = _omission_reason(exc)
                return outcome
        outcome.failed_reason = "CHUNK_FAILED"
        return outcome

    def _complete_with_deadline(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int,
        deadline: Deadline,
        outcome: ChunkOutcome,
    ) -> ModelResponse:
        """Run an untrusted host bridge behind a real hard wall-clock deadline.

        Python cannot forcibly stop an arbitrary blocking host call. The bridge runs in a
        daemon thread and the request stops waiting at the earlier stage/request limit;
        a late answer is discarded and can never be published.

        Its *usage* is not discarded. A call that came back after the deadline still
        reached the provider and was still billed, so dropping the response wholesale made
        real spend disappear from the session's accounting. The answer is refused; the
        tokens are recorded.
        """
        deadline.check("MODEL_CALL")
        timeout_ms = deadline.sub_budget(self._limits.model_call_deadline_ms)
        if timeout_ms <= 0:
            raise DeadlineExceeded("MODEL_CALL")

        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put_nowait(
                    (
                        True,
                        self._provider.complete(
                            system=system,
                            user=user,
                            max_output_tokens=max_output_tokens,
                            timeout_ms=timeout_ms,
                        ),
                    )
                )
            except Exception as exc:
                with suppress(queue.Full):
                    result_queue.put_nowait((False, exc))

        worker = threading.Thread(target=invoke, name="context-shunt-reader", daemon=True)
        worker.start()
        wall_end = time.monotonic() + timeout_ms / 1000.0
        while True:
            try:
                deadline.check("MODEL_CALL")
            except (DeadlineExceeded, CancelledError):
                self._account_for_a_late_call(outcome, result_queue)
                raise
            remaining = wall_end - time.monotonic()
            if remaining <= 0:
                self._account_for_a_late_call(outcome, result_queue)
                raise DeadlineExceeded("MODEL_CALL")
            try:
                ok, value = result_queue.get(timeout=min(0.01, remaining))
            except queue.Empty:
                continue
            if ok:
                try:
                    deadline.check("MODEL_CALL")
                except (DeadlineExceeded, CancelledError):
                    # Too late to publish, but the provider was already paid. Record what
                    # the call cost before refusing its answer.
                    if isinstance(value, ModelResponse):
                        outcome.usage = outcome.usage.merge(value.usage)
                        outcome.usage_complete_calls += 1 if value.usage.complete else 0
                        outcome.completion_bytes += len(value.text.encode("utf-8"))
                    raise
                return value
            deadline.check("MODEL_CALL")
            if isinstance(value, ShuntError):
                raise value
            raise TransientProviderError("PROVIDER_CALL_FAILED")

    def _account_for_a_late_call(
        self, outcome: ChunkOutcome, result_queue: queue.Queue[tuple[bool, Any]]
    ) -> None:
        """Record the cost of a call that finished too late to publish.

        The request stops waiting at its deadline, but the bridge may already have
        returned - the response is sitting in the queue, delivered and billed, and simply
        never read. Dropping it wholesale made real spend disappear from the session's
        accounting; the answer still never reaches the envelope.

        The short wait is bounded and happens only on this failure path, so that a call
        which completed at essentially the same moment as the deadline is still counted.
        """
        try:
            ok, value = result_queue.get(timeout=_LATE_CALL_ACCOUNTING_WAIT_S)
        except queue.Empty:
            return
        if not ok or not isinstance(value, ModelResponse):
            return
        outcome.usage = outcome.usage.merge(value.usage)
        outcome.usage_complete_calls += 1 if value.usage.complete else 0
        outcome.completion_bytes += len(value.text.encode("utf-8"))

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


# -- helpers ----------------------------------------------------------------


def _target_of(provider: Any) -> ProviderTarget:
    target = getattr(provider, "target", None)
    if isinstance(target, ProviderTarget):
        return target
    model = getattr(provider, "model", None)
    return ProviderTarget(model=model if isinstance(model, str) else DEFAULT_LIMITS.reader_model)


def _as_failure_provenance(provenance: Provenance) -> Provenance:
    """The same attribution facts, relabelled for an envelope that publishes no answer."""
    return Provenance(
        derived=False,
        label=ProvenanceLabel.NO_MODEL_OUTPUT,
        attribution_status=provenance.attribution_status,
        attribution_confidence=provenance.attribution_confidence,
        attribution_policy=provenance.attribution_policy,
        attempts_started=provenance.attempts_started,
        usage_complete=provenance.usage_complete,
        citations_mechanically_verified=provenance.citations_mechanically_verified,
        requested=provenance.requested,
        resolved=provenance.resolved,
        reported=provenance.reported,
        fallback_used=provenance.fallback_used,
    )


def _handles_survive(exc: ShuntError) -> bool:
    """Only a failure of the handle itself invalidates it."""
    return exc.code not in ("SOURCE_EXPIRED", "SOURCE_CHANGED", "STORE_FAILED", "UNSAFE_SOURCE")


def _weakest(
    current: Attribution,
    current_confidence: Confidence,
    candidate: Attribution,
    candidate_confidence: Confidence,
) -> tuple[Attribution, Confidence]:
    """A multi-chunk answer is only as well attributed as its weakest call."""
    order = {
        Attribution.MISMATCH: 0,
        Attribution.UNKNOWN: 1,
        Attribution.UNVERIFIED: 2,
        Attribution.RESOLVED: 3,
        Attribution.ACTUAL: 4,
        Attribution.NOT_APPLICABLE: 5,
    }
    if order[candidate] < order[current]:
        return candidate, candidate_confidence
    return current, current_confidence


def _reader_cost(
    usage: Usage,
    *,
    attempts: int,
    usage_complete: int,
    prompt_bytes: int,
    completion_bytes: int,
    limits: Limits,
) -> ReaderCost:
    """Exact provider usage wins; otherwise a named deterministic estimate."""
    if attempts == 0:
        return ReaderCost.none()
    if usage.complete:
        return ReaderCost(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_tokens=usage.cache_tokens,
            method=TokenMethod.EXACT,
            attempts_started=attempts,
            attempts_usage_complete=usage_complete,
        )
    return ReaderCost(
        input_tokens=accounting_tokens(prompt_bytes, limits),
        output_tokens=accounting_tokens(completion_bytes, limits),
        cache_tokens=None,
        method=TokenMethod.BYTES_DIV_4,
        attempts_started=attempts,
        attempts_usage_complete=usage_complete,
    )


def _omission_reason(exc: ShuntError) -> str:
    return {
        "MODEL_ERROR": "MODEL_ERROR",
        "INVALID_MODEL_OUTPUT": "INVALID_MODEL_OUTPUT",
        "LIMIT_EXCEEDED": "BUDGET_EXCEEDED",
        "TIMEOUT": "TIMEOUT",
        "CANCELLED": "CANCELLED",
        "PROVENANCE_UNAVAILABLE": "PROVENANCE_UNAVAILABLE",
    }.get(exc.code, "CHUNK_FAILED")


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


def _read_request_id(request: Any) -> str:
    if isinstance(request, dict):
        candidate = request.get("request_id")
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", candidate):
            return candidate
    return "req_unknown"


def _read_requested_deadline(request: Any, maximum: int) -> int:
    if not isinstance(request, dict) or not isinstance(request.get("budgets"), dict):
        return maximum
    value = request["budgets"].get("deadline_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return maximum
    return min(value, maximum)


def _parse_model_json(text: str, max_bytes: int) -> dict[str, Any]:
    if len(text.encode("utf-8")) > max_bytes:
        raise ShuntError("INVALID_MODEL_OUTPUT", "MODEL_OUTPUT_OVER_CAP", retryable=False)
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
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
        cid = item.get("id")
        quote = item.get("quote")
        if not isinstance(cid, str) or not re.fullmatch(r"c[0-9]{1,3}", cid):
            continue
        if not isinstance(quote, str) or not quote or quote not in chunk.text:
            out.append(_invalid_citation(cid, chunk))
            continue
        locator = _locator_for(item, chunk)
        if locator is None:
            out.append(_invalid_citation(cid, chunk))
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


def _invalid_citation(cid: str, chunk: Chunk) -> dict[str, Any]:
    return {
        "id": cid,
        "source_id": chunk.source_id,
        "snapshot_id": chunk.snapshot_id,
        "locator": chunk.locator,
        "quote": "",
    }


def _locator_for(item: dict[str, Any], chunk: Chunk) -> dict[str, Any] | None:
    if chunk.locator["kind"] == "lines":
        start = item.get("line_start")
        end = item.get("line_end", start)
        if type(start) is not int or type(end) is not int:
            return None
        if start < int(chunk.locator["start"]) or end > int(chunk.locator["end"]) or end < start:
            return None
        return {"kind": "lines", "start": start, "end": end}
    start = item.get("record_start", item.get("line_start"))
    end = item.get("record_end", item.get("line_end", start))
    if type(start) is not int or type(end) is not int:
        return None
    if start < int(chunk.locator["start"]) or end > int(chunk.locator["end"]) or end < start:
        return None
    return {
        "kind": "records",
        "pointer": chunk.locator.get("pointer", ""),
        "start": start,
        "end": end,
    }


def _validate_model_response(response: Any, limits: Limits) -> None:
    """Shape and bounds only. *Which* model answered is a provenance question, not a
    validation one: it is classified truthfully and then judged by the configured policy,
    rather than being asserted here from what we happened to request."""
    if not isinstance(response, ModelResponse) or not isinstance(response.text, str):
        raise ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", retryable=False)
    usage = response.usage
    if not isinstance(usage, Usage):
        raise ShuntError("INVALID_MODEL_OUTPUT", "BAD_USAGE", retryable=False)
    for value, maximum in (
        (usage.input_tokens, limits.max_request_input_tokens),
        (usage.output_tokens, limits.max_output_tokens_per_call),
        (usage.cache_tokens, limits.max_request_input_tokens),
    ):
        if value is None:
            continue
        if type(value) is not int or value < 0 or value > maximum:
            raise ShuntError("INVALID_MODEL_OUTPUT", "BAD_USAGE", retryable=False)


def _namespace_outcome(
    outcome: ChunkOutcome, first_id: int
) -> tuple[str, list[dict[str, Any]], int]:
    names: dict[str, str] = {}
    citations: list[dict[str, Any]] = []
    for citation in outcome.citations:
        local = citation["id"]
        global_id = names.setdefault(local, f"c{first_id + len(names)}")
        citations.append({**citation, "id": global_id})

    def replace(match: re.Match[str]) -> str:
        global_id = names.get(match.group(1))
        return f"[{global_id}]" if global_id else match.group(0)

    answer = re.sub(r"\[(c[0-9]{1,3})\]", replace, outcome.answer)
    return answer, citations, len(names)


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
