"""Price-weighted reader economics: honest, explicit, never zero-for-unknown.

This is a reporting layer on top of the existing token seam (:mod:`accounting`), not a
new accounting model. It answers one question the token counts alone cannot: "what did
the reader actually cost in dollars", given prices the caller supplies.

Four rules, matched to the ones in :mod:`accounting`:

**Unknown usage or an unknown rate is never priced as zero.** A missing token count or a
provider/model with no entry in the caller's :class:`RateTable` produces a :class:`Priced`
with ``usd=None`` and a ``reason``, not a zero-dollar line. Zero dollars is reserved for a
token count that is genuinely, reportedly, zero.

**No default rates.** :class:`RateTable` holds only what the caller supplies, each entry
carrying its own ``as_of`` date and ``source`` string. There is no built-in price list and
none is invented here; the caller is responsible for supplying real, current rates.

**The gross main-context counterfactual is priced separately, never as reader cost.**
``gross_main_context_counterfactual_tokens`` (summed from ``baseline_credit_tokens``,
before the reader's own envelope is subtracted) is carried in tokens, and optionally
priced against a caller-supplied *main-model* rate as
``main_context_counterfactual_opportunity_usd`` - an opportunity figure, kept entirely
apart from ``reader_cost_usd_known_subset``. Conflating "tokens the main model didn't
have to hold" with "dollars the reader spent" is exactly the 1:1 substitution this task
was told never to make; ``net_tokens_saved`` (already net of both the envelope and the
reader's own spend) is never priced or summed here at all.

**A cost comparison against an old baseline is a cost comparison, not a savings claim.**
``old_compactor_baseline_usd`` is accepted only as an explicit caller input (never
computed here), and ``baseline_delta_usd`` is populated only when both the reader's total
cost and the baseline are fully known - otherwise it stays ``None`` rather than mixing a
known figure with an assumed one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .accounting import ReaderCost
from .attempts import COMPONENTS, identity_key
from .provenance import ModelIdentity, TokenMethod

TOKENS_PER_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class ProviderRate:
    """One provider/model's verified per-million-token USD rates.

    ``cached_input_usd_per_million`` is ``None`` when the caller has not supplied a
    cache rate for this provider/model - reported cache tokens are then priced as
    unknown rather than assumed to cost the same as an uncached input token.

    ``input_includes_cached`` states whether this provider's reported ``input_tokens``
    already counts cache-read tokens once (some providers bill cache reads as a strict
    subset of input; others report them as a separate, additive figure). It is ``None``
    when the caller has not confirmed which convention this provider/model uses - a
    positive cache-token count is then priced as unknown rather than silently guessed
    as additive (double-counting spend) or as already-included (undercounting it).
    """

    provider: str
    model: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    as_of: str
    source: str
    cached_input_usd_per_million: Decimal | None = None
    input_includes_cached: bool | None = None


class RateTable:
    """Caller-supplied provider/model rates. Holds no defaults of its own."""

    def __init__(self, rates: list[ProviderRate]) -> None:
        by_identity: dict[tuple[str, str], ProviderRate] = {}
        for rate in rates:
            key = (rate.provider, rate.model)
            if key in by_identity:
                raise ValueError(
                    f"duplicate rate for provider={rate.provider!r} model={rate.model!r}"
                )
            by_identity[key] = rate
        self._by_identity = by_identity

    def lookup(self, identity: ModelIdentity) -> ProviderRate | None:
        """The rate for an identity, or ``None`` if the caller never supplied one."""
        if not identity.provider or not identity.model:
            return None
        return self._by_identity.get((identity.provider, identity.model))


@dataclass(frozen=True)
class Priced:
    """A dollar amount that is either known, or explicitly not - never silently zero."""

    usd: Decimal | None
    reason: str | None

    @classmethod
    def known(cls, usd: Decimal) -> Priced:
        return cls(usd=usd, reason=None)

    @classmethod
    def unknown(cls, reason: str) -> Priced:
        return cls(usd=None, reason=reason)

    @property
    def is_known(self) -> bool:
        return self.usd is not None


@dataclass(frozen=True)
class ReaderCostPricing:
    """A priced breakdown of one :class:`~context_shunt.accounting.ReaderCost`.

    ``estimated_total`` sums the three components whenever each individually prices
    (regardless of how reliable the underlying usage is). ``total`` is the stricter
    figure meant for a "known cost" aggregate: it equals ``estimated_total`` only when
    the usage behind it is exact provider-reported usage from every attempt this
    operation started (``method == TokenMethod.EXACT`` and
    ``attempts_usage_complete == attempts_started``) - or the operation made zero
    attempts at all (a deterministic aggregate/decode_pointer success, genuinely free).
    A ``bytes_div_4`` estimate or a partially-measured attempt still surfaces in
    ``estimated_total`` for visibility, but never in ``total``, so an estimate can never
    be silently folded into a sum a caller reads as a real dollar figure.
    """

    input: Priced
    cached_input: Priced
    output: Priced
    estimated_total: Priced
    total: Priced
    rate: ProviderRate | None


def _price_tokens(
    tokens: int | None, rate_per_million: Decimal | None, *, unknown_reason: str
) -> Priced:
    if tokens is None:
        return Priced.unknown("usage_unknown")
    if tokens == 0:
        # A reported zero is a fact, not an absence - price it without needing a rate.
        return Priced.known(Decimal(0))
    if rate_per_million is None:
        return Priced.unknown(unknown_reason)
    return Priced.known((Decimal(tokens) * rate_per_million) / TOKENS_PER_MILLION)


def _price_cache_tokens(tokens: int | None, rate_per_million: Decimal | None) -> Priced:
    """Cache pricing is opt-in per rate entry, not inferred from the input rate.

    When the caller's rate for this provider/model carries no cache price at all, caching
    is simply outside what this rate prices - a reported zero or unreported cache-token
    count then contributes nothing, the same as a dimension the provider doesn't have.
    A *positive* reported cache-token count with no cache rate is a different case: real
    spend happened in a bucket this rate table cannot price, so that must stay unknown
    rather than silently drop to zero.
    """
    if rate_per_million is None:
        if tokens is None or tokens == 0:
            return Priced.known(Decimal(0))
        return Priced.unknown("cache_rate_unknown")
    return _price_tokens(tokens, rate_per_million, unknown_reason="cache_rate_unknown")


def _sum_components(components: tuple[Priced, ...]) -> Priced:
    if all(component.is_known for component in components):
        return Priced.known(sum((component.usd for component in components), Decimal(0)))
    first_unknown = next(component for component in components if not component.is_known)
    return Priced.unknown(first_unknown.reason or "unknown")


def price_reader_cost(
    cost: ReaderCost, identity: ModelIdentity, rates: RateTable
) -> ReaderCostPricing:
    """Price one chunk/answer's reader spend against the caller's rate table.

    ``identity`` is a per-call input, not read off ``cost`` - :class:`ReaderCost` carries
    no provider/model of its own (see :mod:`accounting`), so the caller names which
    provider actually served the tokens being priced. A provider-fallback chain that
    serves different chunks from different providers, or an operation whose calls
    disagreed on identity, must be priced per its own identity - never the primary's
    rate applied uniformly. (``reader.py`` already collapses a disagreeing operation's
    ``requested`` identity to unknown before this module ever sees it - see
    ``_agreed_identity`` - so that case reaches here as ``identity.known is False`` and
    prices as ``identity_unknown`` on its own, without any special-casing here.)
    """
    if cost.attempts_started == 0:
        # No model call was made at all (a deterministic aggregate/decode_pointer
        # success) - genuinely free, not merely unreported, and needs no rate to say so.
        free = Priced.known(Decimal(0))
        return ReaderCostPricing(
            input=free, cached_input=free, output=free, estimated_total=free, total=free, rate=None
        )

    rate = rates.lookup(identity)
    if rate is None:
        reason = "rate_unknown" if identity.known else "identity_unknown"
        unpriced = Priced.unknown(reason)
        return ReaderCostPricing(
            input=unpriced,
            cached_input=unpriced,
            output=unpriced,
            estimated_total=unpriced,
            total=unpriced,
            rate=None,
        )

    input_priced = _price_tokens(
        cost.input_tokens, rate.input_usd_per_million, unknown_reason="rate_unknown"
    )
    cached_priced = _price_cache_tokens(cost.cache_tokens, rate.cached_input_usd_per_million)
    output_priced = _price_tokens(
        cost.output_tokens, rate.output_usd_per_million, unknown_reason="rate_unknown"
    )

    cache_present = cost.cache_tokens is not None and cost.cache_tokens > 0
    cache_rate_configured = rate.cached_input_usd_per_million is not None
    if cache_present and cache_rate_configured and rate.input_includes_cached is None:
        # The caller hasn't said whether this provider's input_tokens already counts
        # cache reads - summing input + cache blindly here would either double-count
        # (additive convention) or silently overstate the true price (already-included
        # convention). Real spend happened; it just cannot be priced without that fact.
        estimated_total = Priced.unknown("cache_inclusion_unknown")
    elif cache_present and cache_rate_configured and rate.input_includes_cached is True:
        # Cache reads are a subset of input_tokens, billed at the discounted cache rate;
        # only the uncached remainder is billed at the full input rate. Charging all of
        # input_tokens at the full rate on top of the cache component would double-count
        # the cache-read tokens' cost.
        if cost.input_tokens is None:
            input_priced = Priced.unknown("usage_unknown")
            estimated_total = Priced.unknown("usage_unknown")
        elif cost.cache_tokens > cost.input_tokens:
            # Cache reads can never exceed total input under this convention - the usage
            # report is internally inconsistent, so refuse to price it either way.
            input_priced = Priced.unknown("cache_exceeds_input")
            estimated_total = Priced.unknown("cache_exceeds_input")
        else:
            uncached_tokens = cost.input_tokens - cost.cache_tokens
            input_priced = _price_tokens(
                uncached_tokens, rate.input_usd_per_million, unknown_reason="rate_unknown"
            )
            estimated_total = _sum_components((input_priced, cached_priced, output_priced))
    else:
        estimated_total = _sum_components((input_priced, cached_priced, output_priced))

    usage_reliable = (
        cost.method is TokenMethod.EXACT and cost.attempts_usage_complete == cost.attempts_started
    )
    if not estimated_total.is_known:
        total = estimated_total
    elif not usage_reliable:
        reason = (
            "usage_incomplete"
            if cost.attempts_usage_complete < cost.attempts_started
            else "usage_estimated"
        )
        total = Priced.unknown(reason)
    else:
        total = estimated_total

    return ReaderCostPricing(
        input=input_priced,
        cached_input=cached_priced,
        output=output_priced,
        estimated_total=estimated_total,
        total=total,
        rate=rate,
    )


@dataclass(frozen=True)
class ReportLine:
    """One operation's reader spend plus its already-computed token accounting.

    ``baseline_credit_tokens``, ``main_context_tokens_saved`` and ``net_tokens_saved``
    are copied verbatim from the matching :class:`~context_shunt.store.OperationRecord` -
    this module never recomputes them, so the "baseline credited once" rule already
    enforced there (a repeat recovery of the same snapshot records a zero credit)
    carries through unchanged: summing these fields across operations cannot
    manufacture a second credit for the same snapshot. ``main_context_tokens_saved``
    and ``net_tokens_saved`` are carried for reference/token-only reporting only - this
    module never prices or sums ``net_tokens_saved``, since that would dress the
    forbidden 1:1 token subtraction up as a dollar figure.
    """

    operation_id: str
    identity: ModelIdentity
    reader_cost: ReaderCost
    baseline_credit_tokens: int
    main_context_tokens_saved: int
    net_tokens_saved: int

    @classmethod
    def from_record(cls, record: Any, identity: ModelIdentity) -> ReportLine:
        """Build a line from a real :class:`~context_shunt.store.OperationRecord`.

        Reconstructs the :class:`ReaderCost` from the record's already-computed
        ``reader_*``/``attempts_*`` fields rather than asking the caller to duplicate
        them, so a report built from real operations reflects exactly what
        :func:`~context_shunt.accounting.compose` recorded.
        """
        reader_cost = ReaderCost(
            input_tokens=record.reader_input_tokens,
            output_tokens=record.reader_output_tokens,
            cache_tokens=record.reader_cache_tokens,
            method=TokenMethod(record.reader_token_method),
            attempts_started=record.attempts_started,
            attempts_usage_complete=record.attempts_usage_complete,
        )
        return cls(
            operation_id=record.operation_id,
            identity=identity,
            reader_cost=reader_cost,
            baseline_credit_tokens=record.baseline_credit_tokens,
            main_context_tokens_saved=record.main_context_tokens_saved,
            net_tokens_saved=record.net_tokens_saved,
        )


@dataclass(frozen=True)
class EconomicsReport:
    """A priced, honesty-preserving summary over a set of reader operations.

    ``gross_main_context_counterfactual_tokens`` sums ``baseline_credit_tokens`` - the
    gross bytes/tokens withheld from the main model's context before the reader's own
    envelope is subtracted - never ``main_context_tokens_saved`` (which is already net
    of the envelope) and never ``net_tokens_saved`` (which is already net of the
    reader's own token spend too). ``main_context_counterfactual_opportunity_usd``
    prices that gross figure against a caller-supplied *main-model* rate, kept entirely
    separate from the reader's own priced cost: this is what the main model's context
    would have been worth holding those tokens, an opportunity figure, never a claim
    about the actual reader bill or an actual dollar saving.

    ``main_context_counterfactual_opportunity_usd`` is reported as :class:`Priced` even
    when it prices successfully - but unlike ``reader_cost_usd_known_subset`` (gated on
    exact, complete provider usage), the token count behind it,
    ``baseline_credit_tokens``, always comes from
    :meth:`~context_shunt.accounting.Baseline.withheld_payload`'s ``bytes / 4`` estimate,
    never from exact usage. Treat a "known" opportunity figure as an estimate priced at
    an exact rate, not as an exact dollar amount the same way a fully-priced reader cost
    is.
    """

    operations: int
    priced_operations: int
    unpriced_operations: int
    unpriced_reasons: dict[str, int]
    reader_cost_usd_known_subset: Decimal | None
    reader_cost_complete: bool
    gross_main_context_counterfactual_tokens: int
    main_context_counterfactual_opportunity_usd: Priced
    old_compactor_baseline_usd: Decimal | None
    baseline_delta_usd: Decimal | None


def build_economics_report(
    lines: list[ReportLine],
    rates: RateTable,
    *,
    old_compactor_baseline_usd: Decimal | None = None,
    main_model_identity: ModelIdentity | None = None,
    main_model_rates: RateTable | None = None,
) -> EconomicsReport:
    """Aggregate priced reader cost across operations without inventing a total.

    ``reader_cost_usd_known_subset`` is the sum of only the operations that priced
    completely; it is ``None`` (not ``Decimal("0")``) when nothing priced, and it is
    always paired with ``reader_cost_complete`` and ``unpriced_operations`` so a caller
    cannot mistake a partial sum for the whole. ``baseline_delta_usd`` - a cost-to-cost
    comparison against a caller-supplied historical baseline, never an "amount saved" -
    is populated only when both sides are fully known.

    ``main_model_identity``/``main_model_rates`` are both optional and independent of
    the reader's own rate table: passing neither leaves
    ``main_context_counterfactual_opportunity_usd`` explicitly unpriced rather than
    silently reusing a reader rate for a different model.
    """
    priced = [price_reader_cost(line.reader_cost, line.identity, rates) for line in lines]
    known_totals = [item.total.usd for item in priced if item.total.is_known]
    unpriced_reasons: dict[str, int] = {}
    for item in priced:
        if not item.total.is_known:
            reason = item.total.reason or "unknown"
            unpriced_reasons[reason] = unpriced_reasons.get(reason, 0) + 1

    known_subset = sum(known_totals, Decimal(0)) if known_totals else None
    complete = not unpriced_reasons

    baseline_delta = None
    if complete and known_subset is not None and old_compactor_baseline_usd is not None:
        baseline_delta = known_subset - old_compactor_baseline_usd

    gross_tokens = sum(line.baseline_credit_tokens for line in lines)
    if main_model_identity is None or main_model_rates is None:
        opportunity = Priced.unknown("main_model_rate_not_supplied")
    else:
        main_rate = main_model_rates.lookup(main_model_identity)
        if main_rate is None:
            reason = "rate_unknown" if main_model_identity.known else "identity_unknown"
            opportunity = Priced.unknown(reason)
        else:
            opportunity = _price_tokens(
                gross_tokens, main_rate.input_usd_per_million, unknown_reason="rate_unknown"
            )

    return EconomicsReport(
        operations=len(lines),
        priced_operations=len(known_totals),
        unpriced_operations=len(lines) - len(known_totals),
        unpriced_reasons=unpriced_reasons,
        reader_cost_usd_known_subset=known_subset,
        reader_cost_complete=complete,
        gross_main_context_counterfactual_tokens=gross_tokens,
        main_context_counterfactual_opportunity_usd=opportunity,
        old_compactor_baseline_usd=old_compactor_baseline_usd,
        baseline_delta_usd=baseline_delta,
    )


__all__ = [
    "EconomicsReport",
    "Priced",
    "ProviderRate",
    "RateTable",
    "ReaderCostPricing",
    "ReportLine",
    "build_economics_report",
    "price_reader_cost",
]


# Runtime pricing is strictly per attempt, including both cache-write buckets.
def configured_rates(raw: Any) -> dict:
    """Validate trusted configuration. No inferred/default prices or raw error echo."""
    if not isinstance(raw, list) or len(raw) > 32:
        raise ValueError("BAD_ECONOMICS_RATES")
    rates = {}
    allowed = {"provider", "model", "source", "as_of", *COMPONENTS}
    for item in raw:
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError("BAD_ECONOMICS_RATES")
        if any(
            not isinstance(item.get(k), str)
            or not 1 <= len(item[k]) <= 128
            or any(ord(c) < 32 for c in item[k])
            for k in ("provider", "model", "source", "as_of")
        ):
            raise ValueError("BAD_ECONOMICS_RATES")
        key = identity_key(ModelIdentity(item["provider"], item["model"]))
        if key in rates:
            raise ValueError("BAD_ECONOMICS_RATES")
        entry = {"source": item["source"], "as_of": item["as_of"]}
        for component in COMPONENTS:
            value = item.get(component)
            if value is not None and (isinstance(value, bool) or len(str(value)) > 32):
                raise ValueError("BAD_ECONOMICS_RATES")
            try:
                number = Decimal(str(value)) if value is not None else None
            except InvalidOperation:
                raise ValueError("BAD_ECONOMICS_RATES") from None
            if number is not None and (
                not number.is_finite()
                or number < 0
                or number > Decimal("1000000")
                or number.as_tuple().exponent < -12
            ):
                raise ValueError("BAD_ECONOMICS_RATES")
            entry[component] = number
        rates[key] = entry
    return rates


def price_observation(record: dict, rates: dict) -> tuple[Decimal | None, str | None]:
    status = record["attribution"]
    key = (
        record["reported"]
        if status == "actual"
        else record["resolved"]
        if status == "resolved"
        else None
    )
    if key is None:
        return None, "identity_unknown"
    rate = rates.get(key)
    if rate is None:
        return None, "rate_unknown"
    usage = record["usage"]
    if usage.get("cache_write_unclassified_tokens"):
        # Real cache-write spend whose TTL bucket the host did not report. Neither write
        # rate can be applied without guessing, so the attempt stays unpriced.
        return None, "cache_write_ttl_unknown"
    if usage["method"] != "exact" or any(usage[k] is None for k in COMPONENTS):
        return None, "usage_unknown"
    cached = sum(usage[k] for k in COMPONENTS[2:])
    included = usage["input_includes_cache"]
    if cached and included is None:
        return None, "cache_inclusion_unknown"
    uncached = usage["input_tokens"] - (cached if included else 0)
    if uncached < 0:
        return None, "cache_exceeds_input"
    total = Decimal(0)
    for component in COMPONENTS:
        tokens = uncached if component == "input_tokens" else usage[component]
        if tokens and rate[component] is None:
            return None, "rate_unknown"
        if tokens:
            total += Decimal(tokens) * rate[component] / Decimal(1000000)
    return total, key


def attempt_economics_report(records: list, observations_by_operation: dict, rates: dict) -> dict:
    started = sum(r.attempts_started for r in records)
    known = 0
    amount = Decimal(0)
    reasons: dict[str, int] = {}
    used = set()
    observed = 0
    for operation in records:
        attempts = observations_by_operation.get(operation.operation_id, [])
        observed += len(attempts)
        for attempt in attempts:
            price, reason = price_observation(attempt, rates)
            if price is None:
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                known += 1
                amount += price
                used.add(reason)
    missing = max(0, started - observed)
    if missing:
        reasons["observations_unavailable"] = missing
    return {
        "scope": "page",
        "attempts_started": started,
        "attempts_priced": known,
        "reader_cost_usd_known_subset": format(amount, "f") if known or not started else None,
        "reader_cost_usd_total": format(amount, "f") if known == started else None,
        "unknown_reasons": reasons,
        "rates": [
            {
                "identity_sha256": key,
                "source": rates[key]["source"],
                "as_of": rates[key]["as_of"],
                "usd_per_million": {
                    c: format(rates[key][c], "f") if rates[key][c] is not None else None
                    for c in COMPONENTS
                },
            }
            for key in sorted(used)
        ],
        "net_savings_usd": None,
    }
