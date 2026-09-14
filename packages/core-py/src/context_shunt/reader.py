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

import copy
import hashlib
import json
import queue
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any

from . import envelope as E
from .accounting import ReaderCost
from .accounting import estimate_tokens as accounting_tokens
from .chunking import Chunk, estimate_tokens, plan
from .citations import (
    CitationVerifier,
    Reason,
    normalize_claims,
    referenced_ids,
    render_claims,
    strip_unsupported_assertions,
    unpublished_marker_ids,
)
from .clock import Clock, Deadline, MonotonicClock
from .errors import CancelledError, DeadlineExceeded, ShuntError, fallback_allowed
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
    CallIdentity,
    FallbackChainProvider,
    ModelResponse,
    ProviderTarget,
    ReaderProvider,
    TransientProviderError,
    build_user_message,
    deadline_kwarg,
    input_budget_kwarg,
    normalize_response_usage,
    normalize_usage,
)
from .registry import SourceRegistry
from .schema import validate_request

#: ``INVALID_MODEL_OUTPUT`` details eligible for the one-shot format retry: a shape or
#: claims/citations *relationship* failure, never a content judgement. Retrying
#: ``MODEL_OUTPUT_OVER_CAP`` would not make the model write less, so it does not belong
#: here.
_FORMAT_RETRY_DETAILS = frozenset(
    {"NOT_JSON", "NOT_OBJECT", "BAD_RESPONSE_SHAPE", "AMBIGUOUS_RESPONSE_SHAPE"}
)

# Citation repair is deliberately narrower than the ordinary format/transient retry
# budgets: one request gets at most one extra provider invocation, and that invocation
# receives only fixed verifier reasons plus the already-authorized chunk. The count cap
# keeps the feedback deterministic and prevents a model-controlled citation list from
# becoming a prompt-sized side channel.
_MAX_CITATION_REPAIR_REASONS = 8

# Coverage-aware publication is keyed only on coverage, never on guessed prose intent.
# `status: partial` and a sibling guidance field were not enough: a consumer that surfaced
# only `answer` could still publish the model's "exactly zero" as a source-wide conclusion.
# Prefixing the main answer creates a boundary that survives such consumers and languages.
_INCOMPLETE_ANSWER_PREFIX = "[Reviewed subset only; citations verify bytes, not claims] "
_INCOMPLETE_NO_MATCH_GUIDANCE = (
    "Coverage is incomplete: not every planned chunk was reviewed, and/or source content "
    "was omitted or reported as upstream-truncated (see coverage.omitted and "
    "coverage.upstream_truncated). No matching evidence was found in what was reviewed, but "
    "this is not a confirmed absence in the whole source - only in the part covered."
)
_INCOMPLETE_ANSWER_GUIDANCE = (
    "Coverage is incomplete: not every planned chunk was reviewed, and/or source content "
    "was omitted or reported as upstream-truncated (see coverage.omitted and "
    "coverage.upstream_truncated). The answer is explicitly scoped to the reviewed subset. "
    "Mechanical citation checks do not establish that the cited bytes support its prose."
)


def _scope_published_answer(answer: str, *, complete: bool) -> str:
    """Make incomplete scope part of the consumer-visible answer itself."""
    if not answer or complete:
        return answer
    return _INCOMPLETE_ANSWER_PREFIX + answer


def _coverage_is_complete(coverage: E.Coverage) -> bool:
    return (
        not coverage.omitted
        and coverage.processed_chunks == coverage.planned_chunks
        and coverage.planned_chunks > 0
        and coverage.upstream_truncated is False
    )


def _aggregate_upstream_truncation(values: list[bool | None]) -> bool | None:
    """Aggregate trusted per-handle origin completeness without turning unknown into false."""
    if any(value is True for value in values):
        return True
    if any(value is None for value in values):
        return None
    return False


@dataclass
class ChunkOutcome:
    chunk: Chunk
    #: The current contract: structurally valid {"text", "citation_ids"} objects, chunk-
    #: local ids. Populated only when this call's reply used the ``claims`` shape.
    claims: list[dict[str, Any]] = field(default_factory=list)
    #: The legacy contract: raw prose the model marked up itself with ``[cN]``. Populated
    #: only when this call's reply used the ``answer`` shape - never both, an ambiguous
    #: reply carrying both fails the call instead of guessing which one to trust.
    legacy_answer: str = ""
    requires_evidence: bool = False
    semantic_content: bool = False
    citations: list[dict[str, Any]] = field(default_factory=list)
    failed_reason: str | None = None
    #: The bounded contract failure behind ``failed_reason``. Kept separately because
    #: coverage uses a small omission vocabulary while the envelope must preserve the
    #: precise safe Shunt detail for callers and fallback classification.
    failure_code: str | None = None
    failure_detail: str | None = None
    availability_only: bool = True
    # An empty accumulator, not an attempt that reported nothing. `Usage()` defaults to
    # `UNKNOWN`, which is the right answer for a *bridge* that returned no counts - but as
    # a starting value it poisoned the merge: `UNKNOWN + EXACT` is `UNKNOWN`, so exact
    # provider usage was downgraded to a byte estimate on *every* request, and the
    # `usage_exact` distinction the accounting layer exists to make never survived.
    usage: Usage = field(default_factory=lambda: Usage(method=TokenMethod.NOT_APPLICABLE))
    #: Usage reported by attempts whose output was never seen. Held apart from ``usage`` so
    #: a byte estimate can be topped up with it without double counting the winner, whose
    #: output *can* be measured from the text it returned.
    unseen_usage: Usage = field(default_factory=lambda: Usage(method=TokenMethod.NOT_APPLICABLE))
    calls: int = 0
    usage_complete_calls: int = 0
    prompt_bytes: int = 0
    completion_bytes: int = 0
    attribution: Attribution = Attribution.UNKNOWN
    confidence: Confidence = Confidence.NONE
    #: What the call that actually answered asked for. Not always the chain head: an
    #: availability fallback answers as the candidate it advanced to, and publishing the
    #: head's identity for that answer certifies a request that was never served.
    requested: ModelIdentity = field(default_factory=ModelIdentity)
    resolved: ModelIdentity = field(default_factory=ModelIdentity)
    reported: ModelIdentity = field(default_factory=ModelIdentity)
    #: Whether a provider response was ever seen for this chunk. The identity fields above
    #: mean nothing without one, so aggregation reads this rather than ``calls`` - a call
    #: can be started, billed and still return nothing.
    responses_seen: int = 0
    fallback_used: bool = False
    #: With output caps enabled, claims the model wrote beyond
    #: ``max_claims_per_answer``. They are never read in that mode, so they are material
    #: this request dropped, and the caller has to be told.
    claims_over_cap: int = 0
    #: With output caps enabled, claims whose evidence was cut by the raw-citation bound
    #: before anything could verify it. ``_normalize_citations`` then stops at
    #: ``MAX_RAW_CITATIONS``, so a claim citing ``c65`` lost its citation to a ceiling, not
    #: to failed verification - and reporting that as "the model cited something that does
    #: not exist" blamed the model for the program's own bound.
    citations_over_cap: int = 0
    #: One record per physical call this chunk made. Exactly as many as ``calls``, which
    #: is what makes a count over them a count over calls.
    call_identities: list[CallIdentity] = field(default_factory=list)


class _AttemptLedger:
    """Accounting for exactly one physical invocation of the provider.

    Ordinary, late and cancelled outcomes all report through this object, and it takes the
    first report and ignores every later one. Before it existed each branch did its own
    partial bookkeeping - or none: a late response recorded its own usage but not the
    attempts, repeated prompts or unseen billing behind it, and a cancellation between the
    provider returning and the reader reading dropped a billed call entirely. Whether a
    physical call was counted once, twice or not at all depended on when the deadline
    happened to fire.

    One physical invocation, one report. ``record_*`` is safe to call from every path that
    might be the one to notice the call is over.
    """

    __slots__ = ("_outcome", "_recorded", "_extra_counted", "per_call_prompt_bytes")

    def __init__(self, outcome: ChunkOutcome):
        self._outcome = outcome
        self._recorded = False
        self._extra_counted = 0
        #: Bytes one candidate's prompt occupies. Set once the prompt exists; a fallback
        #: re-sends the same prompt to every candidate it tries, so each extra attempt
        #: costs this again.
        self.per_call_prompt_bytes = 0

    def record_extra_attempts(self, count: int) -> None:
        # A timed-out chain may not have delivered its aggregate yet. Budget debits
        # already prove which extra prompts started; reconcile, never add both counts.
        extra = max(0, count - self._extra_counted)
        self._extra_counted += extra
        self._outcome.calls += extra
        self._outcome.prompt_bytes += self.per_call_prompt_bytes * extra

    def record_success(self, response: Any) -> None:
        """What a returned response cost, whether or not its answer can be published."""
        # Even late or malformed delivered output is conservatively not unavailability.
        self._outcome.availability_only = False
        if self._recorded or not isinstance(response, ModelResponse):
            return
        response = normalize_response_usage(response)
        self._recorded = True
        outcome = self._outcome
        # An availability fallback may have taken several attempts inside this one call,
        # and every one of them reached a provider and was billed. `calls` was already
        # incremented once by the caller for the attempt it started.
        attempts = _physical_attempts(response.attempts)
        extra_attempts = attempts - 1
        self.record_extra_attempts(extra_attempts)
        # Output the reader never saw: a failed candidate returned no text to measure, so
        # its reported tokens are the only evidence of what it produced. Disjoint from
        # `completion_bytes` by construction.
        unseen, unseen_well_formed = normalize_usage(response.billed_from_failed_attempts)
        if response.billed_from_failed_attempts is not None and unseen_well_formed:
            outcome.unseen_usage = outcome.unseen_usage.merge(unseen)
        outcome.completion_bytes += len(response.text.encode("utf-8"))
        outcome.call_identities.extend(_response_identities(response))
        outcome.usage = outcome.usage.merge(response.usage)
        # A composite provider reports how many of its attempts supplied complete usage; a
        # plain one supplies one attempt, so the winner alone decides.
        outcome.usage_complete_calls += _usage_complete_attempts(
            response.usage_complete_attempts, attempts, response.usage
        )

    def record_failure(self, exc: BaseException) -> None:
        """What a failed call cost. A rejected reply is still a paid call."""
        if self._recorded:
            return
        self._recorded = True
        outcome = self._outcome
        attempts = _physical_attempts(getattr(exc, "internal_attempts", 1))
        raw_billed = getattr(exc, "billed_usage", None)
        billed, billed_well_formed = normalize_usage(raw_billed)
        reported_complete = getattr(exc, "usage_complete_attempts", None)
        if raw_billed is not None and billed_well_formed:
            outcome.usage = outcome.usage.merge(billed)
            # The same aggregate as the success path: a chain that gave up still reports
            # how many of its candidates were billed and how many of those said what they
            # cost. Counting one aggregate error as one report made two billed candidates
            # look like one usage-complete attempt out of two started.
            outcome.usage_complete_calls += _usage_complete_attempts(
                reported_complete, attempts, billed
            )
            # Nothing came back, so every attempt here is one whose output was never seen.
            outcome.unseen_usage = outcome.unseen_usage.merge(billed)
        # Bytes a reply carried that this core measured itself. Present when the reply
        # arrived intact but its usage claim did not: the claim is refused, the
        # measurement is kept, and the estimate built from it is the conservative one.
        # Without this a malformed usage block erased known output entirely.
        measured = getattr(exc, "response_bytes", None)
        if isinstance(measured, int) and not isinstance(measured, bool) and measured > 0:
            outcome.completion_bytes += measured
        # A composite provider may have made several calls inside this one invocation
        # before giving up. `calls` was incremented once by the caller for the invocation;
        # the rest are the ones the chain made and was billed for.
        extra_attempts = attempts - 1
        self.record_extra_attempts(extra_attempts)
        # Every physical call behind this failure still happened. Whatever the provider
        # observed is taken; the rest are unobserved, which is the truthful record for a
        # call that returned nothing.
        outcome.call_identities.extend(
            _pad_identities(getattr(exc, "call_identities", ()), extra_attempts + 1)
        )


def _response_identities(response: Any) -> list[CallIdentity]:
    """One record per physical call behind a returned response.

    A provider that reports its own records is believed. One that reports none still
    answered *this* call, so its origin describes one of them and every other call it
    made stays unobserved - the alternative, repeating this identity for each of them, is
    the false certification these records exist to prevent.
    """
    attempts = _physical_attempts(response.attempts)
    carried = getattr(response, "call_identities", ())
    if not carried:
        carried = (response.identity_of_this_call(),)
    return _pad_identities(carried, max(1, attempts))


def _pad_identities(carried: Any, calls: int) -> list[CallIdentity]:
    """``carried``, trimmed or extended with unobserved records to cover ``calls``."""
    records = [c for c in carried if isinstance(c, CallIdentity)] if carried else []
    if len(records) >= calls:
        return records[:calls]
    return [*records, *(CallIdentity() for _ in range(calls - len(records)))]


def _physical_attempts(count: Any) -> int:
    """A response's physical-call count, defaulting malformed values to one."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return 1
    return count


def _usage_complete_attempts(value: Any, attempts: int, usage: Usage) -> int:
    """A bounded completeness count that cannot make accounting contradictory."""
    if (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= attempts
        and (usage.complete or value < attempts)
    ):
        return value
    return 1 if usage.complete else 0


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

    @property
    def usage_complete_attempts(self) -> int:
        return self.cost.attempts_usage_complete

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
    availability_failure: str | None = None
    cache_hit: bool = False

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

    def per_call(self, tokens: int) -> _PerCallDebit:
        """A debit handle for one chunk's prompt, spendable once per physical call."""
        return _PerCallDebit(self, tokens)


@dataclass(frozen=True)
class _PerCallDebit:
    """Charges the shared request budget for one more physical call of the same prompt.

    ``max_request_input_tokens`` bounds what one request may transmit, and the reader used
    to debit it once per *invocation* - outside the provider chain. A composite provider
    then sent the same prompt to two or three candidates on that single debit, so a chain
    of three could transmit three times the request's ceiling. The chain debits through
    this handle before it starts each extra candidate, so the count of debits equals the
    count of physical calls, and a candidate whose prompt no longer fits is never started:
    :meth:`_InputTokenBudget.spend` raises first.
    """

    budget: _InputTokenBudget
    tokens: int
    _extra_calls: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def debit_call(self) -> None:
        with self._lock:
            if self._closed:
                raise DeadlineExceeded("MODEL_CALL")
            self.budget.spend(self.tokens)
            object.__setattr__(self, "_extra_calls", self._extra_calls + 1)

    def close(self) -> int:
        with self._lock:
            object.__setattr__(self, "_closed", True)
            return self._extra_calls


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
        enforce_output_caps: bool = True,
    ):
        self._registry = registry
        self._provider = provider
        self._limits = limits
        self._clock = clock or MonotonicClock()
        self._metrics = metrics or NullMetrics()
        self._enforce_output_caps = enforce_output_caps
        self._verifier = CitationVerifier(registry, limits, enforce_output_caps=enforce_output_caps)
        self._policy = attribution_policy
        self._answer_cache: OrderedDict[
            str, tuple[dict[str, Any], Provenance, tuple[str, ...], int]
        ] = OrderedDict()
        self._answer_cache_bytes = 0
        self._answer_cache_lock = threading.Lock()

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
        started_ms = self._clock.now_ms()
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
            cache_key = self._authorized_cache_key(session_id, request, deadline)
            cached = self._cache_get(cache_key, request_id, accounting_id)
            if cached is not None:
                result = cached
            else:
                result = self._answer(
                    session_id, request, request_id, deadline, accounting_id, spent
                )
                self._cache_put(cache_key, result)
        except Exception as raw_exc:
            exc = (
                raw_exc
                if isinstance(raw_exc, ShuntError)
                else ShuntError("STORE_FAILED", "INTERNAL_ERROR")
            )
            self._metrics.count("reader_error", {"code": exc.code})
            provenance = self._failure_provenance(
                exc,
                attempts_started=spent.attempts,
                attempts_usage_complete=spent.usage_complete_attempts,
            )
            result = ReaderResult(
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
        self._metrics.observe(
            "reader_duration_ms",
            max(0, self._clock.now_ms() - started_ms),
            {
                "status": result.envelope["status"],
                "code": result.envelope["code"],
            },
        )
        return result

    # -- internals ---------------------------------------------------------

    def _authorized_cache_key(
        self, session_id: str, raw: dict[str, Any], deadline: Deadline
    ) -> str:
        """Validate and re-authorize every handle before looking up cached content."""
        request = validate_request(raw)
        assert_no_secret(request["question"].encode("utf-8"), "QUESTION")
        deadline.check("RESOLVE")
        for source in request["sources"]:
            entry = self._registry.resolve(session_id, source["source_id"])
            if entry.snapshot.snapshot_id != source["snapshot_id"]:
                raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH", retryable=False)
        deadline.check("RESOLVE")
        target = getattr(self._provider, "target", ProviderTarget(model="", provider=""))
        material = json.dumps(
            {
                "session": session_id,
                "schema": request["schema_version"],
                "question": request["question"],
                "sources": request["sources"],
                "budgets": request["budgets"],
                "refined": request.get("refined", False),
                "reader_contract": {
                    "system": READER_SYSTEM_PROMPT,
                    "target": {"provider": target.provider, "model": target.model},
                    "attribution_policy": self._policy.value,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _cache_get(
        self, key: str, request_id: str, accounting_id: str | None
    ) -> ReaderResult | None:
        with self._answer_cache_lock:
            cached = self._answer_cache.pop(key, None)
            if cached is None:
                return None
            envelope, original, source_ids, _size = cached
            self._answer_cache[key] = cached
        provenance = replace(
            original,
            attempts_started=0,
            attempts_usage_complete=0,
            usage_complete=True,
            fallback_used=None,
            call_identities=(),
            cache_reused=True,
        )
        result = copy.deepcopy(envelope)
        result["request_id"] = request_id
        if accounting_id is None:
            result.pop("accounting_id", None)
        else:
            result["accounting_id"] = accounting_id
        result["provenance"] = provenance.to_dict()
        if self._enforce_output_caps and E.serialized_bytes(result) > envelope_byte_cap(
            result.get("result_kind"), self._limits
        ):
            with self._answer_cache_lock:
                current = self._answer_cache.get(key)
                if current is cached:
                    self._answer_cache.pop(key)
                    self._answer_cache_bytes -= cached[3]
            self._metrics.count("reader_answer_cache", {"result": "oversize_miss"})
            return None
        self._metrics.count("reader_answer_cache", {"result": "hit"})
        return ReaderResult(
            envelope=result,
            provenance=provenance,
            cost=ReaderCost.none(),
            source_ids=source_ids,
            cache_hit=True,
        )

    def _cache_put(self, key: str, result: ReaderResult) -> None:
        envelope = result.envelope
        if not (
            envelope.get("status") == "ok"
            and envelope.get("coverage", {}).get("complete") is True
            and envelope.get("code") in ("ANSWERED", "NO_MATCH")
            and result.cost.attempts_started > 0
            and result.provenance.derived
            and result.provenance.fallback_used is not True
        ):
            return
        stored = copy.deepcopy(envelope)
        size = E.serialized_bytes(stored) + len(key.encode("utf-8"))
        max_bytes = 256 * 1024
        if size > max_bytes:
            return
        with self._answer_cache_lock:
            prior = self._answer_cache.pop(key, None)
            if prior is not None:
                self._answer_cache_bytes -= prior[3]
            self._answer_cache[key] = (
                stored,
                copy.deepcopy(result.provenance),
                tuple(result.source_ids),
                size,
            )
            self._answer_cache_bytes += size
            while len(self._answer_cache) > 32 or self._answer_cache_bytes > max_bytes:
                _, removed = self._answer_cache.popitem(last=False)
                self._answer_cache_bytes -= removed[3]
        self._metrics.count("reader_answer_cache", {"result": "stored"})

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
        upstream_truncation: list[bool | None] = []
        for source in request["sources"]:
            entry = self._registry.resolve(session_id, source["source_id"])
            if entry.snapshot.snapshot_id != source["snapshot_id"]:
                # A refined question must address the snapshot it was given. Recapturing
                # here would answer a new question about a different file under the old
                # hash, so it is refused instead.
                raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")
            selections.append((entry.source_id, entry.snapshot, source["selector"]))
            source_ids.append(entry.source_id)
            upstream_truncation.append(entry.upstream_truncated)
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
        trusted_upstream = _aggregate_upstream_truncation(upstream_truncation)
        coverage = E.Coverage(
            planned_chunks=len(the_plan.chunks),
            upstream_truncated=trusted_upstream,
        )
        for omission in the_plan.omitted:
            coverage.omit(omission["source_id"], omission["selector"], omission["reason"])

        if not the_plan.chunks:
            # Nothing to read means nothing was generated: the answer is empty and the
            # provenance says no model output rather than claiming a derived answer.
            provenance = self._no_output_provenance()
            complete = trusted_upstream is False
            return ReaderResult(
                envelope=E.build(
                    request_id=request_id,
                    status="ok" if complete else "partial",
                    code="NO_MATCH",
                    coverage=E.Coverage(
                        complete=complete,
                        processed_chunks=0,
                        planned_chunks=0,
                        upstream_truncated=trusted_upstream,
                    ),
                    sources=handles,
                    result_kind=ResultKind.MODEL_DERIVED,
                    provenance=provenance,
                    accounting_id=accounting_id,
                    guidance=None if complete else _INCOMPLETE_NO_MATCH_GUIDANCE,
                ),
                provenance=provenance,
                cost=ReaderCost.none(),
                source_ids=tuple(source_ids),
            )

        input_budget = _InputTokenBudget(self._limits.max_request_input_tokens)
        outcomes = self._run_chunks(
            question,
            the_plan.chunks,
            deadline,
            input_budget,
        )

        all_claims: list[dict[str, Any]] = []
        legacy_parts: list[str] = []
        raw_citations: list[dict[str, Any]] = []
        total_calls = 0
        usage_complete_calls = 0
        usage = Usage(method=TokenMethod.NOT_APPLICABLE)
        unseen_usage = Usage(method=TokenMethod.NOT_APPLICABLE)
        prompt_bytes = completion_bytes = 0
        next_citation = 1
        attribution = Attribution.NOT_APPLICABLE
        confidence = Confidence.NONE
        # Every answering call's own identity, kept apart until aggregation: one shared
        # slot filled by whichever chunk happened to report first hid divergence, which is
        # exactly what a provenance block must not do.
        requested_seen: list[ModelIdentity] = []
        resolved_seen: list[ModelIdentity] = []
        reported_seen: list[ModelIdentity] = []
        fallback_used = False
        #: Material this request produced and then dropped against a ceiling. Counted so
        #: an answer that ends up empty can say *why* it is empty.
        cap_dropped = 0
        #: One record per physical call across every chunk, in the order the chunks were
        #: processed. Counting identity per *call* is the only way a release can say which
        #: model produced every measured answer; counting per run and weighting by the
        #: call count credits failed candidates with the winner's identity.
        call_identities: list[CallIdentity] = []

        def add_outcome(outcome: ChunkOutcome, *, include_content: bool = True) -> None:
            """Merge one outcome's bounded facts into this request's aggregates.

            Citation repair is a second physical call for an already-planned chunk. Its
            spend and provenance must join the request totals, while its content is
            selected explicitly below after the repair response is verified; otherwise a
            failed first citation set could leak into the rebuilt answer.
            """
            nonlocal total_calls, usage_complete_calls, usage
            nonlocal unseen_usage, prompt_bytes, completion_bytes, fallback_used
            nonlocal attribution, confidence, cap_dropped, next_citation
            total_calls += outcome.calls
            usage_complete_calls += outcome.usage_complete_calls
            usage = usage.merge(outcome.usage)
            unseen_usage = unseen_usage.merge(outcome.unseen_usage)
            prompt_bytes += outcome.prompt_bytes
            completion_bytes += outcome.completion_bytes
            call_identities.extend(_pad_identities(outcome.call_identities, outcome.calls))
            fallback_used = fallback_used or outcome.fallback_used
            if outcome.calls:
                attribution, confidence = _weakest(
                    attribution, confidence, outcome.attribution, outcome.confidence
                )
            if outcome.responses_seen:
                requested_seen.append(outcome.requested)
                resolved_seen.append(outcome.resolved)
                reported_seen.append(outcome.reported)
            dropped_by_ceiling = outcome.claims_over_cap + outcome.citations_over_cap
            if include_content and dropped_by_ceiling:
                cap_dropped += dropped_by_ceiling
                coverage.omit_once(
                    outcome.chunk.source_id, outcome.chunk.locator, "BUDGET_EXCEEDED"
                )
            if not include_content:
                return
            if outcome.failed_reason:
                coverage.omit(outcome.chunk.source_id, outcome.chunk.locator, outcome.failed_reason)
                return
            coverage.processed_chunks += 1
            namespaced_claims, namespaced_legacy, namespaced_citations, allocated = (
                _namespace_outcome(outcome, next_citation)
            )
            next_citation += allocated
            all_claims.extend(namespaced_claims)
            if namespaced_legacy:
                legacy_parts.append(namespaced_legacy)
            raw_citations.extend(namespaced_citations)

        for outcome in outcomes:
            add_outcome(outcome)

        target = _target_of(self._provider)
        requested = UNKNOWN
        resolved = UNKNOWN
        reported = UNKNOWN

        # One answer, one identity - or none. Each side is published only when every
        # answering call agreed on it; a request whose calls disagree cannot be described
        # by any single value, and picking one would certify a model that produced part of
        # the answer as the model that produced all of it. Divergence also drops the
        # attribution to `unknown`, because a status is a claim *about* the requested
        # identity and there is no longer one to make it about.
        def refresh_identity() -> None:
            """Recompute identity after every answering call, including repair."""
            nonlocal attribution, confidence, requested, resolved, reported
            agreed_requested = _agreed_identity(requested_seen)
            agreed_resolved = _agreed_identity(resolved_seen)
            agreed_reported = _agreed_identity(reported_seen)
            if None in (agreed_requested, agreed_resolved, agreed_reported):
                attribution, confidence = _weakest(
                    attribution, confidence, Attribution.UNKNOWN, Confidence.NONE
                )
            # No answering call at all: the strongest truthful statement is what was
            # asked for, which is what the pre-1.1 envelope always published.
            requested = target.identity() if not requested_seen else (agreed_requested or UNKNOWN)
            resolved = agreed_resolved or UNKNOWN
            reported = agreed_reported or UNKNOWN

        refresh_identity()
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
                unseen_usage=unseen_usage,
            )
        )

        # Only a wholly unavailable read qualifies. A delivered response (even malformed)
        # or a non-availability failure prevents automatic disclosure. Preserve chunk order
        # when naming a mixed MODEL_ERROR/TIMEOUT failure; there is no error ranking.
        if (
            total_calls
            and not deadline.cancelled
            and outcomes
            and all(
                o.availability_only
                and not o.responses_seen
                and o.failed_reason in ("MODEL_ERROR", "TIMEOUT")
                for o in outcomes
            )
        ):
            category = next(o.failed_reason for o in outcomes if o.calls)
            exc = ShuntError(category, "AVAILABILITY_EXHAUSTED")
            failed = replace(
                self._failure_provenance(
                    exc,
                    attempts_started=total_calls,
                    attempts_usage_complete=usage_complete_calls,
                ),
                call_identities=tuple(call_identities),
                fallback_used=fallback_used,
                usage_complete=usage_complete_calls == total_calls,
                attempts_usage_complete=usage_complete_calls,
            )
            env = E.error_envelope(
                request_id,
                exc,
                accounting_id=accounting_id,
                provenance=failed,
                sources=handles,
                handles_valid=True,
            )
            env["coverage"] = coverage.to_dict()
            return ReaderResult(
                envelope=env,
                provenance=failed,
                cost=cost,
                source_ids=tuple(source_ids),
                availability_failure=category,
            )

        # Attribution and provenance policy are caller boundaries. Judge them before the
        # optional semantic repair so a refused answer never spends a second provider
        # call. A call with no response has no attribution to judge; its explicit reader
        # failure remains eligible for the ordinary failure path below.
        if any(outcome.responses_seen for outcome in outcomes):
            pre_repair = Provenance(
                derived=True,
                label=ProvenanceLabel.MODEL_GENERATED_ANSWER,
                attribution_status=attribution,
                attribution_confidence=confidence,
                attribution_policy=self._policy,
                attempts_started=total_calls,
                usage_complete=bool(total_calls) and usage_complete_calls == total_calls,
                attempts_usage_complete=usage_complete_calls,
                citations_mechanically_verified=True,
                requested=requested,
                resolved=resolved,
                reported=reported,
                fallback_used=fallback_used if total_calls else None,
                call_identities=tuple(call_identities),
            )
            try:
                enforce_policy(pre_repair, self._policy)
            except ShuntError as exc:
                self._metrics.count("reader_error", {"code": exc.code})
                refused = _as_failure_provenance(pre_repair)
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

        if outcomes and all(outcome.failed_reason for outcome in outcomes):
            # A chunk that never produced publishable content still has a concrete reader
            # failure. Returning NO_MATCH here erased malformed model output (and its safe
            # detail), which made the outer session unable to apply the mandatory failure
            # fallback. Valid empty replies have ``failed_reason is None`` and therefore
            # stay on the normal NO_MATCH path below.
            terminal = next(
                (outcome for outcome in outcomes if outcome.failure_code is not None),
                outcomes[0],
            )
            code = terminal.failure_code or "MODEL_ERROR"
            detail = terminal.failure_detail
            failure = ShuntError(code, detail, retryable=False)
            failed = Provenance(
                derived=False,
                label=ProvenanceLabel.NO_MODEL_OUTPUT,
                attribution_status=attribution,
                attribution_confidence=confidence,
                attribution_policy=self._policy,
                attempts_started=total_calls,
                usage_complete=usage_complete_calls == total_calls,
                attempts_usage_complete=usage_complete_calls,
                citations_mechanically_verified=True,
                requested=requested,
                resolved=resolved,
                reported=reported,
                fallback_used=fallback_used if total_calls else None,
                call_identities=tuple(call_identities),
            )
            env = E.error_envelope(
                request_id,
                failure,
                accounting_id=accounting_id,
                provenance=failed,
                sources=handles,
                handles_valid=True,
            )
            env["coverage"] = coverage.to_dict()
            return ReaderResult(
                envelope=env,
                provenance=failed,
                cost=cost,
                source_ids=tuple(source_ids),
            )

        verified, rejected, rejection_reasons = self._verify_all(session_id, raw_citations)
        # Keep mechanical verification separate from the evidence actually used below.
        # Unrelated citations cannot support semantic content stripped from the answer.
        verified_evidence = verified
        self._metrics.observe("citations_verified", len(verified), {"result": "verified"})
        self._metrics.observe("citations_rejected", rejected, {"result": "rejected"})

        # A citation failure is the one reader-owned quality failure that gets a focused
        # repair opportunity. It is deliberately request-wide (one extra provider
        # invocation total), has no transient/format retry budget of its own, and targets
        # the first affected planned chunk in deterministic order. A valid answer already
        # exists whenever any normalized claim or legacy sentence is backed by a verified
        # citation, so a partial result never spends this extra call.
        verified_ids = {citation["id"] for citation in verified}
        supported_claims = any(
            claim["citation_ids"] and set(claim["citation_ids"]) <= verified_ids
            for claim in all_claims
        )
        supported_legacy = bool(strip_unsupported_assertions(" ".join(legacy_parts), verified_ids))
        repair_target = next(
            (
                outcome
                for outcome in outcomes
                if not outcome.failed_reason
                and outcome.requires_evidence
                and outcome.semantic_content
            ),
            None,
        )
        repair_provider = (
            self._repair_provider_for(repair_target.requested)
            if repair_target is not None
            else None
        )
        if (
            repair_target is not None
            and repair_provider is not None
            and not supported_claims
            and not supported_legacy
            and cap_dropped == 0
        ):
            repair_question = _citation_repair_question(
                question,
                _citation_repair_feedback(
                    _repair_feedback_reasons(rejection_reasons, len(raw_citations), len(verified))
                ),
            )
            for source in request["sources"]:
                current = self._registry.resolve(session_id, source["source_id"])
                if current.snapshot.snapshot_id != source["snapshot_id"]:
                    raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")
            repaired = self._run_chunk(
                repair_question,
                repair_target.chunk,
                deadline,
                input_budget,
                allow_retries=False,
                provider=repair_provider,
            )
            # The repair call is part of this request's cost and provenance, even though
            # its content is selected only after a second mechanical verification pass.
            add_outcome(repaired, include_content=False)
            refresh_identity()
            if repaired.claims_over_cap or repaired.citations_over_cap:
                cap_dropped += repaired.claims_over_cap + repaired.citations_over_cap
                coverage.omit_once(
                    repaired.chunk.source_id, repaired.chunk.locator, "BUDGET_EXCEEDED"
                )
            cost = spent.record(
                _reader_cost(
                    usage,
                    attempts=total_calls,
                    usage_complete=usage_complete_calls,
                    prompt_bytes=prompt_bytes,
                    completion_bytes=completion_bytes,
                    limits=self._limits,
                    unseen_usage=unseen_usage,
                )
            )
            if repaired.responses_seen > 0 and (
                repaired.fallback_used or repaired.requested != repair_target.requested
            ):
                # Availability fallback is valid for the original read, but a semantic
                # citation repair must stay with the provider that produced the rejected
                # evidence. Otherwise the repaired answer would be attributed to a
                # different target (and potentially several physical calls).
                failure = ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", retryable=False)
                failed = replace(
                    self._failure_provenance(
                        failure,
                        attempts_started=total_calls,
                        attempts_usage_complete=usage_complete_calls,
                    ),
                    call_identities=tuple(call_identities),
                    fallback_used=fallback_used,
                    usage_complete=usage_complete_calls == total_calls,
                    attempts_usage_complete=usage_complete_calls,
                )
                env = E.error_envelope(
                    request_id,
                    failure,
                    accounting_id=accounting_id,
                    provenance=failed,
                    sources=handles,
                    handles_valid=True,
                )
                env["coverage"] = coverage.to_dict()
                return ReaderResult(
                    envelope=env,
                    provenance=failed,
                    cost=cost,
                    source_ids=tuple(source_ids),
                )
            if (
                repaired.failure_code == "MODEL_ERROR"
                and repaired.failure_detail == "MODEL_SUBSTITUTED"
            ):
                # A provider identity mismatch is an explicit provenance refusal. It is
                # never turned into a citation fallback merely because repair was active.
                failure = ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", retryable=False)
                failed = replace(
                    self._failure_provenance(
                        failure,
                        attempts_started=total_calls,
                        attempts_usage_complete=usage_complete_calls,
                    ),
                    call_identities=tuple(call_identities),
                    fallback_used=fallback_used,
                    usage_complete=usage_complete_calls == total_calls,
                    attempts_usage_complete=usage_complete_calls,
                )
                env = E.error_envelope(
                    request_id,
                    failure,
                    accounting_id=accounting_id,
                    provenance=failed,
                    sources=handles,
                    handles_valid=True,
                )
                env["coverage"] = coverage.to_dict()
                return ReaderResult(
                    envelope=env,
                    provenance=failed,
                    cost=cost,
                    source_ids=tuple(source_ids),
                )
            if repaired.failure_code in ("LIMIT_EXCEEDED", "TIMEOUT", "CANCELLED") or (
                repaired.failure_code is not None
                and not fallback_allowed(repaired.failure_code, repaired.failure_detail)
            ):
                # Capacity and caller deadline/cancellation boundaries remain explicit.
                # The original citation failure stays visible in coverage; the outer
                # session can classify this bounded stage failure without guessing.
                code = repaired.failure_code
                detail = repaired.failure_detail
                failure = ShuntError(code, detail, retryable=False)
                failed = replace(
                    self._failure_provenance(
                        failure,
                        attempts_started=total_calls,
                        attempts_usage_complete=usage_complete_calls,
                    ),
                    call_identities=tuple(call_identities),
                    fallback_used=fallback_used,
                    usage_complete=usage_complete_calls == total_calls,
                    attempts_usage_complete=usage_complete_calls,
                )
                env = E.error_envelope(
                    request_id,
                    failure,
                    accounting_id=accounting_id,
                    provenance=failed,
                    sources=handles,
                    handles_valid=True,
                )
                env["coverage"] = coverage.to_dict()
                return ReaderResult(
                    envelope=env,
                    provenance=failed,
                    cost=cost,
                    source_ids=tuple(source_ids),
                )
            if repaired.failed_reason or not repaired.semantic_content:
                # The request still has no verified evidence. Preserve that stable reason
                # for the caller and the mandatory deterministic fallback; the repair
                # attempt's own bounded cost is carried above.
                failure = ShuntError("CITATION_INVALID", "NO_VALID_EVIDENCE", retryable=False)
                failed = replace(
                    self._failure_provenance(
                        failure,
                        attempts_started=total_calls,
                        attempts_usage_complete=usage_complete_calls,
                    ),
                    call_identities=tuple(call_identities),
                    fallback_used=fallback_used,
                    usage_complete=usage_complete_calls == total_calls,
                    attempts_usage_complete=usage_complete_calls,
                )
                env = E.error_envelope(
                    request_id,
                    failure,
                    accounting_id=accounting_id,
                    provenance=failed,
                    sources=handles,
                    handles_valid=True,
                )
                env["coverage"] = coverage.to_dict()
                return ReaderResult(
                    envelope=env,
                    provenance=failed,
                    cost=cost,
                    source_ids=tuple(source_ids),
                )

            # Replace only the affected chunk's answer material. Its first response is
            # retained in the aggregates above for truthful spend/identity accounting;
            # only the evidence set used for publication is rebuilt from the repaired
            # response and the untouched chunks.
            repair_target.claims = repaired.claims
            repair_target.legacy_answer = repaired.legacy_answer
            repair_target.requires_evidence = repaired.requires_evidence
            repair_target.semantic_content = repaired.semantic_content
            repair_target.citations = repaired.citations
            repair_target.claims_over_cap = repaired.claims_over_cap
            repair_target.citations_over_cap = repaired.citations_over_cap
            all_claims.clear()
            legacy_parts.clear()
            raw_citations.clear()
            next_citation = 1
            for outcome in outcomes:
                if outcome.failed_reason:
                    continue
                namespaced_claims, namespaced_legacy, namespaced_citations, allocated = (
                    _namespace_outcome(outcome, next_citation)
                )
                next_citation += allocated
                all_claims.extend(namespaced_claims)
                if namespaced_legacy:
                    legacy_parts.append(namespaced_legacy)
                raw_citations.extend(namespaced_citations)
            verified, rejected, rejection_reasons = self._verify_all(session_id, raw_citations)
            verified_evidence = verified
            self._metrics.observe("citations_verified", len(verified), {"result": "verified"})
            self._metrics.observe("citations_rejected", rejected, {"result": "rejected"})

        # The citation ceiling is applied to a *prioritized* list, not to whatever order
        # the model happened to emit. Truncating arbitrarily lost twice over: the citation
        # went, and then every claim that referenced it went with it - so an answer could
        # lose material that would have fitted had the surviving citations been the ones
        # anything actually cited.
        legacy_all = " ".join(legacy_parts)
        wanted = {cid for c in all_claims for cid in c["citation_ids"]}
        wanted |= set(referenced_ids(legacy_all))
        prioritized = sorted(verified, key=lambda c: c["id"] not in wanted)
        allowed = (
            prioritized[: self._limits.max_citations] if self._enforce_output_caps else prioritized
        )
        overflow = prioritized[self._limits.max_citations :] if self._enforce_output_caps else []
        for citation in overflow:
            # Only a *referenced* citation losing its place costs the answer anything. An
            # unreferenced one is already discarded further down - the envelope publishes
            # `used_ids` and nothing else - so reporting its overflow as material dropped
            # would make identical answers differ by which side of the ceiling their
            # unused evidence happened to land on, and would let an answer that never
            # existed come back as one a ceiling emptied.
            if citation["id"] not in wanted:
                continue
            cap_dropped += 1
            coverage.omit_once(
                str(citation.get("source_id", "")),
                citation.get("locator") or _WHOLE_SOURCE,
                "BUDGET_EXCEEDED",
            )
        allowed_ids = {c["id"] for c in allowed}
        by_id = {c["id"]: c for c in allowed}
        kept_claims = [
            c for c in all_claims if c["citation_ids"] and set(c["citation_ids"]) <= allowed_ids
        ]
        legacy_answer = strip_unsupported_assertions(legacy_all, allowed_ids)
        answer = _render_answer(kept_claims, legacy_answer)
        # Drop whole claims/sentences from the end until the render fits, rather than
        # truncating raw bytes: a byte cut can split a marker or a multi-byte character,
        # which is why the old pipeline had to strip a second time after truncating.
        # Dropping structured units instead never produces a half-written marker.
        #
        # Each drop is recorded. Silently shrinking the answer to fit `max_answer_bytes`
        # and then reporting `complete: true` told the caller the whole selection had been
        # read when part of the reading had just been deleted.
        max_answer = min(budgets["max_answer_bytes"], self._limits.max_answer_bytes)
        while (
            self._enforce_output_caps
            and len(
                _scope_published_answer(answer, complete=_coverage_is_complete(coverage)).encode(
                    "utf-8"
                )
            )
            > max_answer
            and (kept_claims or legacy_answer)
        ):
            if kept_claims:
                orphaned = set(kept_claims[-1]["citation_ids"])
                kept_claims = kept_claims[:-1]
            else:
                shorter = _drop_last_sentence(legacy_answer)
                orphaned = set(referenced_ids(legacy_answer)) - set(referenced_ids(shorter))
                legacy_answer = shorter
            cap_dropped += 1
            for cid in sorted(orphaned):
                citation = by_id.get(cid)
                if citation is None:
                    continue
                coverage.omit_once(
                    str(citation.get("source_id", "")),
                    citation.get("locator") or _WHOLE_SOURCE,
                    "BUDGET_EXCEEDED",
                )
            answer = _render_answer(kept_claims, legacy_answer)
        used_ids = {cid for c in kept_claims for cid in c["citation_ids"]}
        used_ids |= set(referenced_ids(legacy_answer))
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
            attempts_usage_complete=usage_complete_calls,
            citations_mechanically_verified=True,
            requested=requested,
            resolved=resolved,
            reported=reported,
            fallback_used=fallback_used if total_calls else None,
            call_identities=tuple(call_identities),
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
        complete = _coverage_is_complete(coverage)

        # Publication invariant: every marker in the answer names a citation this
        # envelope publishes. `render_claims` only ever writes ids the model supplied
        # *and* the verifier confirmed, and `normalize_claims` drops a claim that wrote
        # its own marker - so a violation here is a program bug, not a model one, and it
        # is refused rather than published. Without it a forged `[c999]` in claim text
        # shipped inside an answer whose provenance said every citation had been
        # mechanically verified.
        forged = unpublished_marker_ids(answer, {c["id"] for c in verified})
        if forged:
            exc = ShuntError("CITATION_INVALID", "MARKER_NOT_PUBLISHED", retryable=False)
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
            if cap_dropped:
                # The sources did answer, and every piece of the answer hit a ceiling.
                # NO_MATCH would report that the sources held nothing, which is a
                # different and untrue statement; `LIMIT_EXCEEDED` names the real cause,
                # and the coverage omissions above say which source lost what. Checked
                # *after* the verification branch, so a request whose evidence never
                # verified is still reported as a citation failure rather than as a size
                # one - the cap is not what emptied that answer.
                exc = ShuntError("LIMIT_EXCEEDED", "ANSWER_OVER_CAP", retryable=False)
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
            # Distinguish a valid empty no-match from assertions stripped for lacking
            # evidence. Capture this before normalization can discard uncited claims
            # or malformed citations; supplied evidence must still verify.
            if (
                not verified and any(o.semantic_content and not o.failed_reason for o in outcomes)
            ) or (
                not verified_evidence
                and any(o.requires_evidence and not o.failed_reason for o in outcomes)
            ):
                exc = ShuntError("CITATION_INVALID", "NO_VALID_EVIDENCE")
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
                    guidance=None if complete else _INCOMPLETE_NO_MATCH_GUIDANCE,
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
                answer=_scope_published_answer(answer_text, complete=ok),
                citations=citations,
                coverage=coverage,
                sources=handles,
                result_kind=ResultKind.MODEL_DERIVED,
                provenance=provenance,
                accounting_id=accounting_id,
                guidance=None if ok else _INCOMPLETE_ANSWER_GUIDANCE,
            )

        if self._enforce_output_caps:
            answer, verified, dropped = self._fit_to_envelope(
                kept_claims, legacy_answer, verified, coverage, answered
            )
        else:
            dropped = 0
        # The fit loop rewrites both halves, so the invariant is re-established on what is
        # actually published rather than on what was measured before trimming. A violation
        # here is a program bug and is reported as the citation failure it is - calling it
        # `ANSWER_OVER_ENVELOPE` would blame a size ceiling for a marker that names
        # evidence the envelope does not carry.
        if answer and unpublished_marker_ids(answer, {c["id"] for c in verified}):
            exc = ShuntError("CITATION_INVALID", "MARKER_NOT_PUBLISHED", retryable=False)
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
        kept_claims: list[dict[str, Any]],
        legacy_answer: str,
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
        while trimming by cost is deterministic and converges fastest. Each drop removes the
        claims and legacy sentences it orphaned, which shrinks the answer too, so the loop
        re-measures between drops and stops as soon as it fits.
        """
        dropped = 0
        # One drop per pass, so this cannot run longer than there are citations.
        for _ in range(len(verified) + 1):
            answer = _render_answer(kept_claims, legacy_answer)
            # A still-complete candidate needs no scope prefix. Once a drop records an
            # omission, the next candidate becomes partial and pays for that prefix.
            candidate = build(
                answer,
                verified,
                _coverage_is_complete(coverage) and dropped == 0,
            )
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
            kept_ids = {c["id"] for c in verified if c["id"] != victim["id"]}
            kept_claims = [c for c in kept_claims if set(c["citation_ids"]) <= kept_ids]
            legacy_answer = strip_unsupported_assertions(legacy_answer, kept_ids)
            used = {cid for c in kept_claims for cid in c["citation_ids"]}
            used |= set(referenced_ids(legacy_answer))
            verified = [c for c in verified if c["id"] != victim["id"] and c["id"] in used]
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

    def _failure_provenance(
        self,
        exc: ShuntError,
        *,
        attempts_started: int = 0,
        attempts_usage_complete: int = 0,
    ) -> Provenance:
        """Provenance for a request that published nothing.

        ``attempts_started`` is not always zero: a request can complete its model calls
        and then fail at PUBLISH, and reporting no attempts there would contradict the
        cost the same envelope carries. ``attempts_usage_complete`` carries the real count
        of attempts that reported usage before the failure. Failure describes publication,
        not accounting quality: when every started attempt did report usage, the boolean
        remains true even though no model output was published.
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
            usage_complete=attempts_usage_complete == attempts_started,
            attempts_usage_complete=attempts_usage_complete,
            requested=_target_of(self._provider).identity(),
        )

    def _repair_provider_for(self, identity: ModelIdentity) -> ReaderProvider | None:
        """Pin semantic repair to the provider that produced the rejected answer."""
        if not identity.known:
            return None
        if isinstance(self._provider, FallbackChainProvider):
            return self._provider.repair_provider_for(identity)
        return self._provider

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
        *,
        allow_retries: bool = True,
        provider: ReaderProvider | None = None,
    ) -> ChunkOutcome:
        outcome = ChunkOutcome(chunk=chunk)
        active_provider = provider if provider is not None else self._provider
        # Two independent, separately bounded retry budgets: a transient provider failure
        # and a schema failure on the same chunk can each spend their own allotted retry,
        # and both may fire for the same chunk. "Independent" means neither budget can
        # borrow the other's slot - it does not mean only one of them may ever fire. The
        # combined worst case for one chunk is bounded by the sum of the two limits
        # (1 + max_transient_retries + max_format_retries), never more.
        transient_used = 0
        format_used = 0
        max_attempts = (
            1 + self._limits.max_transient_retries + self._limits.max_format_retries
            if allow_retries
            else 1
        )
        for _attempt in range(max_attempts):
            try:
                deadline.check("MODEL_CALL")
            except (DeadlineExceeded, CancelledError) as exc:
                outcome.failed_reason = "TIMEOUT" if exc.code == "TIMEOUT" else "CANCELLED"
                outcome.failure_code = exc.code
                outcome.failure_detail = exc.detail
                return outcome
            # One ledger per physical invocation, created before anything can fail.
            # Ordinary, late and cancelled outcomes all report through it, and it counts
            # the first report only.
            ledger = _AttemptLedger(outcome)
            try:
                user = build_user_message(question, chunk.text, chunk.locator)
                per_call_tokens = estimate_tokens(
                    READER_SYSTEM_PROMPT, self._limits
                ) + estimate_tokens(user, self._limits)
                # This debit covers the physical call this frame is about to start. A
                # composite provider that advances to another candidate re-sends the same
                # prompt, and debits again through the handle below before it does.
                input_budget.spend(per_call_tokens)
                outcome.calls += 1
                # The same prompt is sent again by every candidate a fallback tries, so
                # this is per physical call, not per invocation. Charging it once per
                # invocation halved the input estimate of any two-candidate chain: four
                # calls transmitting 3,448 bytes were estimated from 1,724.
                per_call_prompt_bytes = len(READER_SYSTEM_PROMPT.encode("utf-8")) + len(
                    user.encode("utf-8")
                )
                outcome.prompt_bytes += per_call_prompt_bytes
                ledger.per_call_prompt_bytes = per_call_prompt_bytes
                debit = input_budget.per_call(per_call_tokens)
                try:
                    response = self._complete_with_deadline(
                        system=READER_SYSTEM_PROMPT,
                        user=user,
                        max_output_tokens=self._limits.max_output_tokens_per_call,
                        deadline=deadline,
                        ledger=ledger,
                        input_budget=debit,
                        provider=active_provider,
                    )
                finally:
                    ledger.record_extra_attempts(debit.close())
                response = _validate_model_response(response)
                ledger.record_success(response)
                outcome.responses_seen += 1
                outcome.attribution, outcome.confidence = response.attribution()
                outcome.requested = response.requested
                outcome.resolved = response.resolved
                outcome.reported = response.reported
                outcome.fallback_used = outcome.fallback_used or response.fallback_used
                if outcome.attribution is Attribution.MISMATCH:
                    raise ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", retryable=False)
                parsed = _parse_model_json(
                    response.text,
                    self._limits.max_tool_result_bytes if self._enforce_output_caps else None,
                )
                has_claims = "claims" in parsed
                has_legacy_answer = "answer" in parsed
                if has_claims and has_legacy_answer:
                    # Both shapes at once is not "prefer one" - it is a response the
                    # program cannot trust to say which one the model meant, so it is
                    # refused rather than silently picking a side.
                    raise ShuntError(
                        "INVALID_MODEL_OUTPUT", "AMBIGUOUS_RESPONSE_SHAPE", retryable=False
                    )
                if not isinstance(parsed.get("citations"), list):
                    raise ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", retryable=False)
                citations_local = _normalize_citations(
                    parsed["citations"],
                    chunk,
                    enforce_output_caps=self._enforce_output_caps,
                )
                over_cap_ids = _citation_ids_over_cap(
                    parsed["citations"], enforce_output_caps=self._enforce_output_caps
                )
                if has_claims:
                    raw_claims = parsed["claims"]
                    if not isinstance(raw_claims, list):
                        raise ShuntError(
                            "INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", retryable=False
                        )
                    # Scan every claim the model wrote, not only the ones that survive
                    # structural validation: a malformed claim (bad citation_ids) can
                    # still carry a secret in its text, and a claim dropped later must
                    # still have been scanned before it is discarded.
                    outcome.requires_evidence = bool(parsed["citations"])
                    outcome.semantic_content = False
                    bounded_claims = (
                        raw_claims[: self._limits.max_claims_per_answer]
                        if self._enforce_output_caps
                        else raw_claims
                    )
                    for item in bounded_claims:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            assert_no_secret(item["text"].encode("utf-8"), "ANSWER")
                            outcome.semantic_content |= bool(item["text"].strip())
                            outcome.requires_evidence |= outcome.semantic_content
                    # Claims past the ceiling are never read. That is dropped material,
                    # so it is carried out and reported as an omission rather than
                    # silently disappearing behind a `complete: true`.
                    if self._enforce_output_caps:
                        outcome.claims_over_cap = max(
                            0, len(raw_claims) - self._limits.max_claims_per_answer
                        )
                    valid_local_ids = {c["id"] for c in citations_local}
                    # A claim whose only evidence sat past the raw-citation bound is about
                    # to be dropped by `normalize_claims` for citing an unknown id. It is
                    # dropped either way - nothing verified that citation - but the reason
                    # is a ceiling this program chose, so it is counted here and reported
                    # as an omission instead of vanishing behind `complete: true`.
                    if over_cap_ids:
                        outcome.citations_over_cap = sum(
                            1
                            for item in bounded_claims
                            if _claim_cites_over_cap(item, over_cap_ids)
                        )
                    outcome.claims = normalize_claims(
                        raw_claims,
                        valid_local_ids,
                        self._limits,
                        enforce_output_caps=self._enforce_output_caps,
                    )
                    outcome.citations = citations_local
                    return outcome
                if not has_legacy_answer or not isinstance(parsed["answer"], str):
                    raise ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", retryable=False)
                assert_no_secret(parsed["answer"].encode("utf-8"), "ANSWER")
                # Same ceiling, the legacy shape: a hand-placed marker naming an id past
                # the bound would be published as a marker no citation backs, which reads
                # as a model fault. It is the program's bound, so it is reported as one.
                outcome.citations_over_cap = len(
                    over_cap_ids & set(referenced_ids(parsed["answer"]))
                )
                outcome.semantic_content = bool(parsed["answer"].strip())
                outcome.requires_evidence = outcome.semantic_content or bool(parsed["citations"])
                outcome.legacy_answer = parsed["answer"]
                outcome.citations = citations_local
                return outcome
            except Exception as raw_exc:
                exc = (
                    raw_exc
                    if isinstance(raw_exc, ShuntError)
                    else TransientProviderError("PROVIDER_CALL_FAILED")
                )
                # A rejected reply is still a paid call, and the ledger is where that is
                # recorded. It is a no-op when the call was already accounted for on the
                # way out - a late response, or a cancellation that landed between the
                # provider returning and this frame seeing it.
                ledger.record_failure(exc)
                outcome.availability_only = outcome.availability_only and (
                    exc.code == "TIMEOUT"
                    or (exc.code == "MODEL_ERROR" and exc.detail != "MODEL_SUBSTITUTED")
                )
                outcome.failure_code = exc.code
                outcome.failure_detail = exc.detail
                can_retry_transient = (
                    exc.code == "MODEL_ERROR"
                    and exc.retryable
                    and transient_used < self._limits.max_transient_retries
                )
                can_retry_format = (
                    exc.code == "INVALID_MODEL_OUTPUT"
                    and exc.detail in _FORMAT_RETRY_DETAILS
                    and format_used < self._limits.max_format_retries
                )
                if (
                    allow_retries
                    and (can_retry_transient or can_retry_format)
                    and not deadline.expired()
                ):
                    if can_retry_transient:
                        transient_used += 1
                    else:
                        format_used += 1
                    continue
                outcome.failed_reason = _omission_reason(exc)
                return outcome
        outcome.failure_code = "MODEL_ERROR"
        outcome.failure_detail = "CHUNK_FAILED"
        outcome.failed_reason = "CHUNK_FAILED"
        return outcome

    def _complete_with_deadline(
        self,
        *,
        system: str,
        user: str,
        max_output_tokens: int,
        deadline: Deadline,
        ledger: _AttemptLedger,
        input_budget: _PerCallDebit | None = None,
        provider: ReaderProvider | None = None,
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

        active_provider = provider if provider is not None else self._provider

        def invoke() -> None:
            try:
                result_queue.put_nowait(
                    (
                        True,
                        active_provider.complete(
                            system=system,
                            user=user,
                            max_output_tokens=max_output_tokens,
                            timeout_ms=timeout_ms,
                            # So a composite provider can stop between attempts rather
                            # than advancing after the caller has already given up. Only
                            # passed to a provider that accepts it: `deadline` is a
                            # widening of a published protocol, and a provider written
                            # against the previous signature must keep working.
                            **deadline_kwarg(active_provider, deadline),
                            # The same widening, for the same reason: a composite provider
                            # debits the shared request input budget before every extra
                            # candidate it starts, so no chain can transmit more than the
                            # request's ceiling. A provider that ignores it makes one
                            # call, which this frame has already debited.
                            **input_budget_kwarg(active_provider, input_budget),
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
                self._account_for_a_late_call(ledger, result_queue)
                raise
            remaining = wall_end - time.monotonic()
            if remaining <= 0:
                self._account_for_a_late_call(ledger, result_queue)
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
                    # the call cost - all of it, through the same ledger the ordinary path
                    # uses - before refusing its answer. Recording only the returned
                    # response's own usage here lost the attempts, repeated prompts and
                    # unseen billing of a composite call: a two-candidate chain whose
                    # winner came back late reported one attempt and 220 input tokens
                    # where two attempts had transmitted 439.
                    ledger.record_success(value)
                    raise
                return value
            # A failure this frame pulled off the queue is accounted for before the
            # deadline is consulted, because consulting it may raise `CANCELLED` and take
            # the provider's own error - and the usage it was billed - with it.
            ledger.record_failure(
                value
                if isinstance(value, BaseException)
                else ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED")
            )
            deadline.check("MODEL_CALL")
            if isinstance(value, ShuntError):
                raise value
            raise TransientProviderError("PROVIDER_CALL_FAILED")

    def _account_for_a_late_call(
        self, ledger: _AttemptLedger, result_queue: queue.Queue[tuple[bool, Any]]
    ) -> None:
        """Record the cost of a call that finished too late to publish.

        The request stops waiting at its deadline, but the bridge may already have
        returned - the response is sitting in the queue, delivered and billed, and simply
        never read. Dropping it wholesale made real spend disappear from the session's
        accounting; the answer still never reaches the envelope.

        The read is non-blocking. Recovering this cost must not extend the wall clock it
        is accounting for: waiting even briefly here made a 10 ms request against a slow
        bridge return after ~263 ms, which breaks the hard deadline the cancellation
        contract rests on. A response already delivered is counted; one still in flight is
        not, and the attempt stands as started with usage unknown - which is exactly what
        the accounting columns are for.
        """
        try:
            ok, value = result_queue.get_nowait()
        except queue.Empty:
            return
        if ok:
            ledger.record_success(value)
        elif isinstance(value, BaseException):
            # A *failure* delivered but never read was billed too, and it carries the
            # attempt count and billed usage of everything the chain tried. Ignoring it
            # here dropped a cancelled-after-billing call to zero usage-complete attempts
            # and zero output tokens.
            ledger.record_failure(value)

    def _verify_all(
        self, session_id: str, citations: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        verified: list[dict[str, Any]] = []
        rejected = 0
        rejection_reasons: dict[str, int] = {}
        seen: set[str] = set()
        for citation in citations:
            result = self._verifier.verify(session_id, citation)
            if not result.verified:
                rejected += 1
                reason = result.reason.value
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                continue
            cid = citation["id"]
            if cid in seen:
                continue
            seen.add(cid)
            verified.append({**citation, "verified": True})
        return verified, rejected, rejection_reasons


# -- helpers ----------------------------------------------------------------

#: Nothing is known about this side of the provenance triple.
UNKNOWN = ModelIdentity()

#: The coarsest legal locator, for an omission whose material had no narrower one.
_WHOLE_SOURCE: dict[str, Any] = {"kind": "all"}


def _citation_repair_feedback(reasons: dict[str, int]) -> str:
    """Render only bounded, fixed verifier reasons for the one repair prompt."""
    if not reasons:
        return "NO_VALID_EVIDENCE"
    entries = []
    for reason, count in sorted(reasons.items())[:_MAX_CITATION_REPAIR_REASONS]:
        # ``Reason`` is an enum owned by the verifier. The check documents the trust
        # boundary and prevents a future verifier change from turning a diagnostic into
        # arbitrary model-visible text.
        if reason not in (
            {member.value for member in Reason} | {"NO_VALID_EVIDENCE", "MARKER_NOT_PUBLISHED"}
        ) or not isinstance(count, int):
            continue
        entries.append(f"{reason} x{min(count, 64)}")
    return "; ".join(entries) or "NO_VALID_EVIDENCE"


def _repair_feedback_reasons(
    reasons: dict[str, int], citation_count: int, verified_count: int
) -> dict[str, int]:
    """Supply a fixed reason when verification had no rejected citation entry."""
    if reasons:
        return reasons
    # A valid citation that did not back any surviving assertion means the marker/evidence
    # relationship was not published. With no citation at all, the only safe feedback is
    # the bounded absence-of-evidence reason.
    return (
        {"MARKER_NOT_PUBLISHED": 1}
        if citation_count > 0 and verified_count > 0
        else {"NO_VALID_EVIDENCE": 1}
    )


def _citation_repair_question(question: str, feedback: str) -> str:
    """Augment the original question with safe feedback, never prior model bytes."""
    return (
        f"{question}\n\n"
        "CITATION REPAIR: The previous evidence did not pass the bounded verifier. "
        f"Fixed verifier feedback: {feedback}. "
        "Answer the original question using only the SOURCE EXCERPT above. "
        "Return JSON with claims and exact citations; every claim must cite evidence."
    )


def _agreed_identity(values: list[ModelIdentity]) -> ModelIdentity | None:
    """The one identity every answering call reported, or ``None`` when they differ.

    ``None`` is the honest answer for a mixed request, and it is deliberately also the
    answer when one call named a model and another named nothing: publishing the one that
    did would describe the whole answer by the half of it that could be identified. The
    caller degrades the attribution status alongside it, so the envelope never carries a
    strong status over an identity that covers only part of the work.
    """
    if not values:
        return ModelIdentity()
    first = values[0]
    return first if all(value == first for value in values[1:]) else None


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
        attempts_usage_complete=provenance.attempts_usage_complete,
        citations_mechanically_verified=provenance.citations_mechanically_verified,
        requested=provenance.requested,
        resolved=provenance.resolved,
        reported=provenance.reported,
        fallback_used=provenance.fallback_used,
        call_identities=provenance.call_identities,
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
    unseen_usage: Usage | None = None,
) -> ReaderCost:
    """Exact provider usage wins; otherwise a named deterministic estimate."""
    if attempts == 0:
        return ReaderCost.none()
    # `exact` is a claim about the whole request, not about whichever attempt happened to
    # win. A chain that failed once and then succeeded merged the winner's exact usage
    # into an empty accumulator and came back `exact` while only one of two billed
    # attempts had reported - a partial sum wearing the strongest label. Every started
    # attempt has to have reported for the total to be exact.
    if usage.complete and usage_complete == attempts:
        return ReaderCost(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_tokens=usage.cache_tokens,
            method=TokenMethod.EXACT,
            attempts_started=attempts,
            attempts_usage_complete=usage_complete,
        )
    # The estimate covers every physical call: `prompt_bytes` already includes each
    # fallback attempt's prompt, and the output side adds what attempts the reader never
    # saw reported they produced. Reporting zero output for an all-failure chain that was
    # billed for it understated real spend, which is the direction this must never err in.
    # The two populations are disjoint by construction - `completion_bytes` is text the
    # reader received, `unseen_usage` is attempts it did not - so nothing is counted twice.
    unseen_output = (unseen_usage.output_tokens or 0) if unseen_usage is not None else 0
    return ReaderCost(
        input_tokens=accounting_tokens(prompt_bytes, limits),
        output_tokens=accounting_tokens(completion_bytes, limits) + unseen_output,
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


def _render_answer(claims: list[dict[str, Any]], legacy_answer: str) -> str:
    """The one place the two published shapes are joined into the public ``answer`` field.

    Order is deliberate: rendered claims first, then whatever legacy prose survived - a
    request mixing both shapes across its chunks (a stale cached response alongside a
    current one, say) still reads as one coherent answer rather than interleaving.
    """
    parts = [p for p in (render_claims(claims), legacy_answer) if p]
    return " ".join(parts).strip()


def _drop_last_sentence(text: str) -> str:
    """Drop the last legacy sentence, for the same byte-fit loop that drops claims."""
    if not text.strip():
        return ""
    parts = re.split(r"(?<=[.!?。！？\n])\s+", text.strip())
    return " ".join(parts[:-1]).strip()


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


def _parse_model_json(text: str, max_bytes: int | None) -> dict[str, Any]:
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
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


#: How many raw citation entries one chunk's reply may declare while output caps are
#: enabled. It is the program's bound, so what it cuts is the program's omission to report.
#: The trusted uncapped mode still applies all structural and mechanical verification to
#: every entry. See :func:`_citation_ids_over_cap`.
MAX_RAW_CITATIONS = 64


def _citation_ids_over_cap(raw: Any, *, enforce_output_caps: bool = True) -> set[str]:
    """The well-formed citation ids ``_normalize_citations`` will not reach.

    Read from the entries past :data:`MAX_RAW_CITATIONS` so a claim referencing one can be
    told apart from a claim referencing an id that was never declared at all. Only the id
    is read, and only to recognise it later - nothing here is trusted as evidence.
    """
    if not enforce_output_caps or not isinstance(raw, list) or len(raw) <= MAX_RAW_CITATIONS:
        return set()
    out: set[str] = set()
    for item in raw[MAX_RAW_CITATIONS:]:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        if isinstance(cid, str) and re.fullmatch(r"c[0-9]{1,3}", cid):
            out.add(cid)
    return out


def _claim_cites_over_cap(item: Any, over_cap_ids: set[str]) -> bool:
    """Whether this raw claim named a citation the bound cut before it could verify.

    One such id is enough. :func:`normalize_claims` is fail-closed on *any* unknown id, so
    a claim citing one surviving citation and one the ceiling removed is dropped whole -
    the surviving evidence buys it nothing. Requiring that the claim be left with *no*
    valid id would therefore under-report: the claim is gone either way, and the ceiling
    is still why.
    """
    if not isinstance(item, dict) or not isinstance(item.get("text"), str):
        return False
    ids = item.get("citation_ids")
    if not isinstance(ids, list):
        return False
    return any(isinstance(cid, str) and cid in over_cap_ids for cid in ids)


def _normalize_citations(
    raw: Any, chunk: Chunk, *, enforce_output_caps: bool = True
) -> list[dict[str, Any]]:
    """Rebuild each citation from trusted chunk metadata.

    Only the id, the addressed range and the quote come from the model; the source and
    snapshot always come from the chunk, and ``verified`` is never taken from the model.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    items = raw[:MAX_RAW_CITATIONS] if enforce_output_caps else raw
    for item in items:
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


def _validate_model_response(response: Any) -> ModelResponse:
    """Validate response text shape and normalize observational usage metadata.

    *Which* model answered is a provenance question, not a
    validation one: it is classified truthfully and then judged by the configured policy,
    rather than being asserted here from what we happened to request. Usage anomalies do
    not decide whether the answer is valid: malformed counts become unknown and valid
    nonnegative integers remain reported even when they exceed a configured call cap.
    """
    if not isinstance(response, ModelResponse) or not isinstance(response.text, str):
        raise ShuntError("INVALID_MODEL_OUTPUT", "BAD_RESPONSE_SHAPE", retryable=False)
    return normalize_response_usage(response)


#: Stands in for a legacy marker that names an id its own chunk never declared. Inside
#: the `[cN]` grammar, so it is still seen by `referenced_ids`, and outside the allocated
#: id space, so it can never match a published citation.
_UNMAPPABLE_MARKER = "[c0]"


def _namespace_outcome(
    outcome: ChunkOutcome, first_id: int
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]], int]:
    """Give one chunk's local ``cN`` ids a slice of the answer's global id space.

    Returns ``(claims, legacy_answer, citations, ids_allocated)`` with every id rewritten.
    A claim's ``citation_ids`` are remapped through the same table built from this chunk's
    own ``citations`` array - the same table :func:`normalize_claims` already checked them
    against, so every id here is guaranteed present and the remap can never drop one.
    """
    names: dict[str, str] = {}
    citations: list[dict[str, Any]] = []
    for citation in outcome.citations:
        local = citation["id"]
        global_id = names.setdefault(local, f"c{first_id + len(names)}")
        citations.append({**citation, "id": global_id})

    claims = [
        {"text": claim["text"], "citation_ids": [names[cid] for cid in claim["citation_ids"]]}
        for claim in outcome.claims
    ]

    def replace(match: re.Match[str]) -> str:
        global_id = names.get(match.group(1))
        # A marker naming an id this chunk never declared cannot be remapped, and leaving
        # it alone let it collide with a *different* chunk's global id: chunk 2 writing
        # `[c2]` for evidence it never declared was published as chunk 1's verified
        # citation c2. Global ids are allocated from c1 upwards, so `c0` can never be one
        # - the sentence carrying it fails the subset test in
        # `strip_unsupported_assertions` and is dropped, which is the fail-closed answer.
        return f"[{global_id}]" if global_id else _UNMAPPABLE_MARKER

    legacy_answer = re.sub(r"\[(c[0-9]{1,3})\]", replace, outcome.legacy_answer)
    return claims, legacy_answer, citations, len(names)


def _search_selector_to_lines(snapshot, selector: dict[str, Any]) -> dict[str, Any]:
    """Resolve a bounded literal search to the line range that actually matched.

    Only literal patterns are accepted, so match time is linear in the snapshot size and
    no user-supplied regex can be made to backtrack. A caller who wants "any of several
    exact strings" sends ``patterns`` (1.3+), a list ORed together with plain substring
    matching on each - not a delimiter embedded in ``pattern``. A single ``pattern`` is
    never split or parsed: a literal that happens to contain ``|`` (or any other character
    that looks like OR/regex syntax) matched only itself before this field existed and
    still matches only itself now. Production evidence for why this distinction matters:
    a real caller sent ``pattern="merchant_auto_suspend|third monday|third Monday|auto
    suspend"`` expecting an alternation and got a silent ``NO_MATCH`` with zero reader
    calls, because the four-clause string is not a substring of any line. The fix is a new
    field, not a smarter parse of the old one.
    """
    patterns = selector.get("patterns")
    if patterns is None:
        patterns = [selector["pattern"]]
    limit = int(selector["max_matches"])
    hits: list[int] = []
    for ordinal in range(1, snapshot.line_count + 1):
        try:
            line = snapshot.line_index.line_text(ordinal)
        except UnicodeDecodeError:
            continue
        if any(p in line for p in patterns):
            hits.append(ordinal)
            if len(hits) >= limit:
                break
    if not hits:
        return {"kind": "lines", "start": 1, "end": 0}
    return {"kind": "lines", "start": min(hits), "end": max(hits)}
