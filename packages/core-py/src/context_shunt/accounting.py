"""Token accounting: signed, one-time-credited, and never zero-for-unknown.

Definitions, verbatim from the contract::

    main_context_tokens_saved = baseline_credit_tokens - main_model_envelope_tokens
    net_tokens_saved          = main_context_tokens_saved
                              - reader_input_tokens - reader_output_tokens

Both results are **signed**. A refined question, a failed retry and an inspect page all
produce a negative net, and that is the correct answer: they cost context without
withholding anything new.

Three rules keep the numbers honest.

**Zero is never unknown.** A token count that the provider did not report is stored as
SQL NULL and rendered as ``null``. Where a number is still required for the arithmetic,
the estimate is derived from bytes we measured ourselves and the record says
``bytes_div_4`` rather than pretending the count was exact. ``unknown`` appears only when
no attempt was made at all.

**The baseline is credited once.** ``baseline_credit_tokens`` is non-zero only on the
operation that first withheld a given snapshot. Every later operation over the same
snapshot records a zero credit and still records its own envelope and reader overhead, so
repeated recovery shows up as accumulating cost, not as repeated savings.

**A counterfactual is labelled as one.** ``full_payload_counterfactual`` is what the whole
payload *would* have cost had it entered the conversation - we measured the payload, so
the number is real, but the saving is a counterfactual. ``host_truncated_observed`` is the
different, smaller baseline that applies when the host had already truncated the result
before we saw it: crediting the full payload there would be a fabrication.

Egress is measured after the envelope is complete. The envelope carries only the opaque
``accounting_id``; the record it points at is written afterwards from the exact serialized
bytes, so the measurement can never include itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .limits import DEFAULT_LIMITS, Limits
from .provenance import TokenMethod
from .store import OperationRecord, new_operation_id


class OperationKind(str, Enum):
    GATE_BLOCK = "gate_block"
    CAPTURE = "capture"
    READ = "read"
    REFINED_READ = "refined_read"
    INSPECT = "inspect"
    STATS = "stats"
    SPILL = "spill"


class BaselineKind(str, Enum):
    #: We measured the entire payload; the saving is what it would have cost.
    FULL_PAYLOAD_COUNTERFACTUAL = "full_payload_counterfactual"
    #: The host truncated before we saw it; only the truncated size is observable.
    HOST_TRUNCATED_OBSERVED = "host_truncated_observed"
    #: Nothing was withheld by this operation.
    NONE = "none"


class DeliveryBoundary(str, Enum):
    ENVELOPE = "envelope"
    EXTRACTION = "extraction"
    POINTER = "pointer"
    BLOCK_MESSAGE = "block_message"
    NONE = "none"


def estimate_tokens(byte_count: int, limits: Limits = DEFAULT_LIMITS) -> int:
    """The single deterministic estimator, named ``bytes_div_4`` in every record."""
    divisor = max(1, limits.bytes_per_token_estimate)
    return (max(0, byte_count) + divisor - 1) // divisor


@dataclass(frozen=True)
class Baseline:
    """What this operation withheld from the main context."""

    kind: BaselineKind
    raw_input_bytes: int
    tokens: int | None
    method: TokenMethod

    @classmethod
    def withheld_payload(cls, raw_input_bytes: int, *, limits: Limits = DEFAULT_LIMITS) -> Baseline:
        return cls(
            kind=BaselineKind.FULL_PAYLOAD_COUNTERFACTUAL,
            raw_input_bytes=raw_input_bytes,
            tokens=estimate_tokens(raw_input_bytes, limits),
            method=TokenMethod.BYTES_DIV_4,
        )

    @classmethod
    def host_truncated(cls, observed_bytes: int, *, limits: Limits = DEFAULT_LIMITS) -> Baseline:
        """A smaller, separately labelled baseline for an already-truncated upstream."""
        return cls(
            kind=BaselineKind.HOST_TRUNCATED_OBSERVED,
            raw_input_bytes=observed_bytes,
            tokens=estimate_tokens(observed_bytes, limits),
            method=TokenMethod.BYTES_DIV_4,
        )

    @classmethod
    def none(cls) -> Baseline:
        return cls(
            kind=BaselineKind.NONE,
            raw_input_bytes=0,
            tokens=None,
            method=TokenMethod.UNKNOWN,
        )


@dataclass(frozen=True)
class ReaderCost:
    """What the reader model spent. ``None`` means the provider did not report it."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_tokens: int | None = None
    method: TokenMethod = TokenMethod.NOT_APPLICABLE
    attempts_started: int = 0
    attempts_usage_complete: int = 0

    @classmethod
    def none(cls) -> ReaderCost:
        return cls()

    @classmethod
    def estimated_from_bytes(
        cls,
        *,
        prompt_bytes: int,
        completion_bytes: int,
        attempts_started: int,
        limits: Limits = DEFAULT_LIMITS,
    ) -> ReaderCost:
        """Deterministic fallback when usage was not reported.

        The byte counts come from the messages this core built and the text it received,
        so the estimate is reproducible - but the record still says ``bytes_div_4`` so
        nobody mistakes it for provider truth.
        """
        return cls(
            input_tokens=estimate_tokens(prompt_bytes, limits),
            output_tokens=estimate_tokens(completion_bytes, limits),
            cache_tokens=None,
            method=TokenMethod.BYTES_DIV_4,
            attempts_started=attempts_started,
            attempts_usage_complete=0,
        )

    @property
    def charged_input(self) -> int:
        return self.input_tokens or 0

    @property
    def charged_output(self) -> int:
        return self.output_tokens or 0


@dataclass(frozen=True)
class Egress:
    """The exact bytes that crossed into the main model context."""

    boundary: DeliveryBoundary
    byte_count: int
    method: TokenMethod = TokenMethod.BYTES_DIV_4

    def tokens(self, limits: Limits = DEFAULT_LIMITS) -> int:
        return estimate_tokens(self.byte_count, limits)


def compose(
    *,
    operation_id: str,
    kind: OperationKind,
    status: str,
    code: str,
    baseline: Baseline,
    baseline_credited: bool,
    reader: ReaderCost,
    egress: Egress,
    limits: Limits = DEFAULT_LIMITS,
    credited_bytes: int | None = None,
) -> OperationRecord:
    """Build the record for one operation.

    ``baseline_credited`` is the store's one-time answer for this snapshot. When it is
    false the credit is zero even though ``raw_input_baseline_tokens`` still reports what
    the payload measures, so the record shows both "this much was withheld overall" and
    "this operation claims none of that saving".

    ``credited_bytes`` separates the two for a *mixed* selection. The measurement covers
    every selected source, but the credit may cover only some of them: a read that reuses
    an already-credited source alongside a new one withholds nothing new for the former.
    Crediting the whole measured baseline whenever any part of it was new inflated the
    reported saving on every such read. When it is omitted the credit is the whole
    baseline, which is the single-source case and the previous behaviour.
    """
    envelope_tokens = egress.tokens(limits)
    if not baseline_credited or baseline.tokens is None:
        credit = 0
    elif credited_bytes is None:
        credit = baseline.tokens
    else:
        credit = estimate_tokens(credited_bytes, limits)
    main_saved = credit - envelope_tokens
    net_saved = main_saved - reader.charged_input - reader.charged_output
    return OperationRecord(
        operation_id=operation_id,
        kind=kind.value,
        status=status,
        code=code,
        raw_input_bytes=baseline.raw_input_bytes,
        raw_input_baseline_tokens=baseline.tokens,
        baseline_kind=baseline.kind.value,
        baseline_method=_baseline_method(baseline),
        baseline_credit_tokens=credit,
        main_model_envelope_bytes=egress.byte_count,
        main_model_envelope_tokens=envelope_tokens,
        envelope_token_method=egress.method.value,
        reader_input_tokens=reader.input_tokens,
        reader_output_tokens=reader.output_tokens,
        reader_cache_tokens=reader.cache_tokens,
        reader_token_method=reader.method.value,
        attempts_started=reader.attempts_started,
        attempts_usage_complete=reader.attempts_usage_complete,
        delivery_boundary=egress.boundary.value,
        main_context_tokens_saved=main_saved,
        net_tokens_saved=net_saved,
    )


def _baseline_method(baseline: Baseline) -> str:
    """The DDL admits exact/bytes_div_4/unknown for a baseline; not_applicable is not one."""
    if baseline.method is TokenMethod.NOT_APPLICABLE:
        return TokenMethod.UNKNOWN.value
    return baseline.method.value


def totals_to_dict(raw: dict[str, Any], *, operations: int | None = None) -> dict[str, Any]:
    """Shape the store's aggregate for the stats envelope, preserving nulls."""
    out = {
        "operations": int(raw.get("operations") or 0) if operations is None else operations,
        "raw_input_bytes": int(raw.get("raw_input_bytes") or 0),
        "baseline_credit_tokens": int(raw.get("baseline_credit_tokens") or 0),
        "main_model_envelope_tokens": int(raw.get("main_model_envelope_tokens") or 0),
        "main_context_tokens_saved": int(raw.get("main_context_tokens_saved") or 0),
        "net_tokens_saved": int(raw.get("net_tokens_saved") or 0),
        "attempts_started": int(raw.get("attempts_started") or 0),
        "attempts_usage_complete": int(raw.get("attempts_usage_complete") or 0),
        "disclosed_bytes": int(raw.get("disclosed_bytes") or 0),
    }
    for key in ("reader_input_tokens", "reader_output_tokens", "reader_cache_tokens"):
        value = raw.get(key)
        # A NULL sum means no operation reported this direction. It stays null.
        out[key] = None if value is None else int(value)
    return out


__all__ = [
    "Baseline",
    "BaselineKind",
    "DeliveryBoundary",
    "Egress",
    "OperationKind",
    "ReaderCost",
    "compose",
    "estimate_tokens",
    "new_operation_id",
    "totals_to_dict",
]
