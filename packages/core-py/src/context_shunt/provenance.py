"""Reader provenance: what produced this envelope, stated truthfully.

The rule this module exists to enforce is narrow and absolute: **``actual_model`` is never
synthesized from ``requested_model``**. Three separate facts are kept apart, because on
real hosts they are genuinely different things:

``requested_provider`` / ``requested_model``
    What the adapter asked the host for. Always known.

``resolved_provider`` / ``resolved_model``
    What the host says it selected after its own policy and routing. Known only when the
    host exposes its selection. ``None`` otherwise - never back-filled from the request.

``reported_provider`` / ``reported_model``
    What the *provider* says generated the tokens. Known only when the provider reports
    it and the host passes it through. ``None`` otherwise.

:class:`Attribution` then names the strongest thing that can be proven:

``actual``
    The provider reported the generating model and it agrees with the request.
``resolved``
    The host reported its own post-policy selection and it agrees with the request. That
    is a routing fact, not a provider confirmation.
``unverified``
    A value came back, but the host surface cannot distinguish a provider report from an
    echo of what we asked for. This is the honest ceiling on a host whose plugin LLM
    facade records the request when no provider value is available.
``mismatch``
    Something concrete came back and it contradicts the request.
``unknown``
    Nothing came back.
``not_applicable``
    No model call was made at all - a gate decision, a deterministic extraction, stats.

Policy
------
:class:`AttributionPolicy` decides what to do with an unprovable attribution.
``ALLOW_UNVERIFIED`` (the default) publishes the truthful ``unverified`` label; a
deployment that would rather fail closed sets ``REQUIRE_MATCH`` and gets
``PROVENANCE_UNAVAILABLE`` instead of an answer. Either way the envelope says which
policy was in force, so a reader of the envelope never has to guess.

Usage completeness
------------------
:class:`Usage` distinguishes *exact* provider-reported counts from a named deterministic
estimate from *unknown*. ``None`` means "not reported" and must never be rendered as
zero; ``usage_complete`` is true only when every started attempt came back with usage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import ShuntError


class Attribution(str, Enum):
    ACTUAL = "actual"
    RESOLVED = "resolved"
    UNVERIFIED = "unverified"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class AttributionPolicy(str, Enum):
    #: Publish the truthful label even when attribution cannot be proven.
    ALLOW_UNVERIFIED = "allow_unverified"
    #: Refuse to answer unless the reported or resolved model agrees with the request.
    REQUIRE_MATCH = "require_match"
    #: No model call was involved.
    NOT_APPLICABLE = "not_applicable"


class ResultKind(str, Enum):
    MODEL_DERIVED = "model_derived"
    DETERMINISTIC_EXTRACTION = "deterministic_extraction"
    GATE_DECISION = "gate_decision"
    POINTER = "pointer"
    STATS = "stats"
    FAILURE = "failure"


class ProvenanceLabel(str, Enum):
    MODEL_GENERATED_ANSWER = "model_generated_answer"
    DETERMINISTIC_EXTRACTION = "deterministic_extraction"
    GATE_DECISION = "gate_decision"
    POINTER_ONLY = "pointer_only"
    SESSION_METRICS = "session_metrics"
    NO_MODEL_OUTPUT = "no_model_output"


class TokenMethod(str, Enum):
    EXACT = "exact"
    BYTES_DIV_4 = "bytes_div_4"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Usage:
    """One attempt's token usage. ``None`` is "not reported", never zero."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_tokens: int | None = None
    method: TokenMethod = TokenMethod.UNKNOWN

    @property
    def complete(self) -> bool:
        """True only when the provider reported both directions exactly."""
        return (
            self.method is TokenMethod.EXACT
            and self.input_tokens is not None
            and self.output_tokens is not None
        )

    def merge(self, other: Usage) -> Usage:
        """Sum two attempts. Unknown plus anything stays unknown for that direction."""
        return Usage(
            input_tokens=_add_optional(self.input_tokens, other.input_tokens),
            output_tokens=_add_optional(self.output_tokens, other.output_tokens),
            cache_tokens=_add_optional(self.cache_tokens, other.cache_tokens),
            method=_merge_method(self.method, other.method),
        )


def _add_optional(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _merge_method(left: TokenMethod, right: TokenMethod) -> TokenMethod:
    if left is TokenMethod.NOT_APPLICABLE:
        return right
    if right is TokenMethod.NOT_APPLICABLE:
        return left
    if left is right:
        return left
    # Mixing exact and estimated counts yields an estimate, not an exact total.
    if TokenMethod.UNKNOWN in (left, right):
        return TokenMethod.UNKNOWN
    return TokenMethod.BYTES_DIV_4


@dataclass(frozen=True)
class ModelIdentity:
    """One side of the provenance triple. Empty strings are never invented."""

    provider: str | None = None
    model: str | None = None

    @property
    def known(self) -> bool:
        return bool(self.provider) or bool(self.model)

    @property
    def identifies_model(self) -> bool:
        """Whether this side actually names a model, rather than only a provider."""
        return bool(self.model)


@dataclass
class Provenance:
    """The provenance block published in a 1.1 envelope."""

    derived: bool
    label: ProvenanceLabel
    attribution_status: Attribution = Attribution.NOT_APPLICABLE
    attribution_confidence: Confidence = Confidence.NONE
    attribution_policy: AttributionPolicy = AttributionPolicy.NOT_APPLICABLE
    attempts_started: int = 0
    usage_complete: bool = True
    citations_mechanically_verified: bool = True
    requested: ModelIdentity = field(default_factory=ModelIdentity)
    resolved: ModelIdentity = field(default_factory=ModelIdentity)
    reported: ModelIdentity = field(default_factory=ModelIdentity)
    fallback_used: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "derived": self.derived,
            "label": self.label.value,
            "attribution_status": self.attribution_status.value,
            "attribution_confidence": self.attribution_confidence.value,
            "attribution_policy": self.attribution_policy.value,
            "attempts_started": self.attempts_started,
            "usage_complete": self.usage_complete,
            "citations_mechanically_verified": self.citations_mechanically_verified,
        }
        if self.requested.provider is not None:
            out["requested_provider"] = self.requested.provider
        if self.requested.model is not None:
            out["requested_model"] = self.requested.model
        if self.attribution_status is not Attribution.NOT_APPLICABLE:
            # Present-but-null is meaningful here: it says "the host does not expose this",
            # which is different from the field being absent because it never applied.
            out["resolved_provider"] = self.resolved.provider
            out["resolved_model"] = self.resolved.model
            out["reported_provider"] = self.reported.provider
            out["reported_model"] = self.reported.model
        if self.fallback_used is not None:
            out["fallback_used"] = self.fallback_used
        return out


def deterministic(label: ProvenanceLabel = ProvenanceLabel.DETERMINISTIC_EXTRACTION) -> Provenance:
    """Provenance for a result no model touched."""
    return Provenance(derived=False, label=label)


def classify(
    *,
    requested: ModelIdentity,
    resolved: ModelIdentity,
    reported: ModelIdentity,
    provider_confirms_generation: bool,
) -> tuple[Attribution, Confidence]:
    """Name the strongest attribution the host's answer actually supports.

    ``provider_confirms_generation`` is the adapter's assertion that ``reported`` came
    from the provider's own report of what generated the tokens - not from the host
    echoing the request back. An adapter that cannot tell the two apart passes ``False``,
    and the result is ``unverified`` rather than ``actual``. That flag is the only place
    where "we know this is real" can be asserted, and it is asserted by the adapter that
    read the host's source, never inferred here.
    """
    if not resolved.known and not reported.known:
        return Attribution.UNKNOWN, Confidence.NONE

    if reported.known and _contradicts(requested, reported):
        return Attribution.MISMATCH, Confidence.HIGH
    if resolved.known and _contradicts(requested, resolved):
        return Attribution.MISMATCH, Confidence.MEDIUM

    # `ACTUAL` is a claim about which *model* generated the tokens, so a confirmation
    # that names only a provider cannot earn it - `known` is true for either half alone.
    if provider_confirms_generation and reported.identifies_model:
        return Attribution.ACTUAL, Confidence.HIGH
    if resolved.known:
        return Attribution.RESOLVED, Confidence.MEDIUM
    return Attribution.UNVERIFIED, Confidence.LOW


def _contradicts(requested: ModelIdentity, observed: ModelIdentity) -> bool:
    if requested.model and observed.model and not _model_agrees(requested.model, observed.model):
        return True
    return bool(
        requested.provider
        and observed.provider
        and requested.provider.strip().lower() != observed.provider.strip().lower()
    )


#: A trailing segment that decorates an id rather than renaming it: an ISO build date or
#: a numeric revision. Deliberately not an arbitrary word - `gpt-4` and `gpt-4-turbo` are
#: different models, and so are `gpt-5.6-luna` and `gpt-5.6-luna-evil`.
_DECORATION = re.compile(r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|v?[0-9]+(?:[.\-][0-9]+)*)$")


def _model_agrees(requested: str, observed: str) -> bool:
    """Providers often return a dated or namespaced variant of the requested id.

    ``gpt-5.6-luna`` vs ``openai/gpt-5.6-luna`` vs ``gpt-5.6-luna-2026-05-01`` all agree;
    a different family does not. Agreement is generous about *decoration* and strict about
    identity, because the alternative is a false ``mismatch`` on every provider that
    stamps a build date.

    Decoration used to be "the requested id plus a hyphen plus anything", which let a
    substituted model keep the prefix and pass as the requested one - ``gpt-5.6-luna-evil``
    was classified as ``gpt-5.6-luna`` and could be published under its name. The suffix
    must now look like a version, so a rename is a mismatch again.
    """
    left = _bare_model(requested)
    right = _bare_model(observed)
    if left == right:
        return True
    longer, shorter = (right, left) if len(right) > len(left) else (left, right)
    if not longer.startswith(f"{shorter}-"):
        return False
    return bool(_DECORATION.match(longer[len(shorter) + 1 :]))


def _bare_model(ref: str) -> str:
    return ref.strip().lower().rsplit("/", 1)[-1]


def enforce_policy(provenance: Provenance, policy: AttributionPolicy) -> None:
    """Apply the configured attribution policy, or raise a bounded failure.

    ``REQUIRE_MATCH`` refuses anything weaker than ``actual`` or ``resolved``: on a host
    that cannot prove attribution this disables the reader, which is a legitimate choice
    but never the silent default. ``ALLOW_UNVERIFIED`` refuses only an outright
    contradiction - a mismatch is a wrong answer, not a weak one.
    """
    status = provenance.attribution_status
    if status is Attribution.NOT_APPLICABLE:
        return
    if status is Attribution.MISMATCH:
        raise ShuntError("MODEL_ERROR", "MODEL_SUBSTITUTED", retryable=False)
    if policy is AttributionPolicy.REQUIRE_MATCH and status not in (
        Attribution.ACTUAL,
        Attribution.RESOLVED,
    ):
        raise ShuntError("PROVENANCE_UNAVAILABLE", "ATTRIBUTION_UNPROVEN", retryable=False)
