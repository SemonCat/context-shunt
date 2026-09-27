"""unit economics: priced reader cost is honest, never zero-for-unknown.

Fixture rates below are fabricated for these tests only (``test-cheap-reader`` etc.) -
they are not real provider prices and must never be read as defaults; the module under
test accepts no built-in rates at all (see the module docstring in ``economics.py``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from context_shunt.accounting import (
    Baseline,
    DeliveryBoundary,
    Egress,
    OperationKind,
    ReaderCost,
    compose,
)
from context_shunt.economics import (
    ProviderRate,
    RateTable,
    ReportLine,
    build_economics_report,
    price_reader_cost,
)
from context_shunt.provenance import ModelIdentity, TokenMethod

pytestmark = pytest.mark.gate_reader

CHEAP = ModelIdentity(provider="test-cheap-reader", model="nano-v1")
EXPENSIVE = ModelIdentity(provider="test-expensive-reader", model="mega-v1")
UNPRICED = ModelIdentity(provider="test-unlisted-reader", model="ghost-v1")
MAIN_MODEL = ModelIdentity(provider="test-main-model", model="orchestrator-v1")

RATES = RateTable(
    [
        ProviderRate(
            provider="test-cheap-reader",
            model="nano-v1",
            input_usd_per_million=Decimal("0.10"),
            output_usd_per_million=Decimal("0.40"),
            cached_input_usd_per_million=Decimal("0.025"),
            input_includes_cached=False,
            as_of="2026-09-27",
            source="fixture, not a real provider price",
        ),
        ProviderRate(
            provider="test-expensive-reader",
            model="mega-v1",
            input_usd_per_million=Decimal("5.00"),
            output_usd_per_million=Decimal("15.00"),
            as_of="2026-09-27",
            source="fixture, not a real provider price",
        ),
    ]
)

MAIN_RATES = RateTable(
    [
        ProviderRate(
            provider="test-main-model",
            model="orchestrator-v1",
            input_usd_per_million=Decimal("3.00"),
            output_usd_per_million=Decimal("15.00"),
            as_of="2026-09-27",
            source="fixture, not a real provider price",
        ),
    ]
)


def test_cheap_reader_can_be_cost_efficient_despite_a_negative_net_tokens_saved():
    """The audit's actual shape: a reader that spent more tokens than it withheld from
    the main context (``net_tokens_saved < 0``) can still cost far less than what those
    withheld tokens would have been worth in the main model's own context - because
    token count and price-per-token are independent axes. Built from a real
    :func:`~context_shunt.accounting.compose` call, not hand-fed numbers, so the
    ``baseline_credit_tokens``/``net_tokens_saved`` relationship is the real one."""
    baseline = Baseline.withheld_payload(raw_input_bytes=80_000)  # ~20,000 bytes/4 tokens
    egress = Egress(boundary=DeliveryBoundary.ENVELOPE, byte_count=400)
    reader_cost = ReaderCost(
        input_tokens=50_000,
        output_tokens=5_000,
        cache_tokens=0,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    record = compose(
        operation_id="op-1",
        kind=OperationKind.READ,
        status="complete",
        code="ANSWERED",
        baseline=baseline,
        baseline_credited=True,
        reader=reader_cost,
        egress=egress,
    )
    # More reader tokens spent (55,000) than the main context saved (~19,900): a real,
    # negative net - the audit's own shape, not a contrived one.
    assert record.net_tokens_saved < 0

    line = ReportLine.from_record(record, CHEAP)
    report = build_economics_report(
        [line], RATES, main_model_identity=MAIN_MODEL, main_model_rates=MAIN_RATES
    )

    # 50,000 * 0.10/1e6 + 5,000 * 0.40/1e6 = 0.005 + 0.002
    assert report.reader_cost_usd_known_subset == Decimal("0.007")

    opportunity = report.main_context_counterfactual_opportunity_usd
    assert opportunity.is_known
    assert opportunity.usd == (Decimal(record.baseline_credit_tokens) * Decimal("3.00")) / Decimal(
        1_000_000
    )

    # The cheap reader's actual dollar cost is below what holding those withheld tokens
    # in the main model's own context would have been worth - even though it spent more
    # tokens than it saved. The two figures stay separate fields; neither is a "savings".
    assert report.reader_cost_usd_known_subset < opportunity.usd


def test_missing_usage_is_unknown_not_zero():
    """Unreported token counts must never be priced as free."""
    cost = ReaderCost(
        input_tokens=None,
        output_tokens=None,
        method=TokenMethod.UNKNOWN,
        attempts_started=1,
        attempts_usage_complete=0,
    )
    priced = price_reader_cost(cost, CHEAP, RATES)
    assert priced.total.usd is None
    assert priced.total.reason == "usage_unknown"
    assert priced.input.reason == "usage_unknown"
    assert priced.output.reason == "usage_unknown"


def test_zero_attempt_operation_prices_as_a_real_known_zero():
    """A deterministic aggregate/decode_pointer success makes no model call at all - the
    audit's 35 zero-model-call successes. That is a genuine, known $0, not "unknown",
    and needs no identity or rate to say so."""
    cost = ReaderCost.none()
    assert cost.attempts_started == 0
    priced = price_reader_cost(cost, ModelIdentity(), RATES)
    assert priced.total.is_known
    assert priced.total.usd == Decimal(0)
    assert priced.rate is None


def test_reported_zero_usage_prices_as_a_real_zero():
    """A genuinely-reported zero token count is a fact, distinguishable from unknown."""
    cost = ReaderCost(
        input_tokens=0,
        output_tokens=0,
        cache_tokens=0,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    priced = price_reader_cost(cost, CHEAP, RATES)
    assert priced.total.is_known
    assert priced.total.usd == Decimal(0)


def test_unpriced_identity_is_unknown_not_zero():
    """A provider/model absent from the caller's rate table must not be treated as free."""
    cost = ReaderCost(
        input_tokens=1000,
        output_tokens=200,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    priced = price_reader_cost(cost, UNPRICED, RATES)
    assert priced.total.usd is None
    assert priced.total.reason == "rate_unknown"

    unknown_identity_cost = price_reader_cost(cost, ModelIdentity(), RATES)
    assert unknown_identity_cost.total.usd is None
    assert unknown_identity_cost.total.reason == "identity_unknown"


def test_cache_price_semantics_require_an_explicit_cache_rate():
    """Cache tokens price at the cache rate, and are unknown - not input-rate-priced - when absent."""
    with_cache_rate = ReaderCost(
        input_tokens=1000,
        output_tokens=0,
        cache_tokens=4000,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    priced = price_reader_cost(with_cache_rate, CHEAP, RATES)
    assert priced.cached_input.is_known
    assert priced.cached_input.usd == (Decimal(4000) * Decimal("0.025")) / Decimal(1_000_000)
    assert priced.total.is_known

    no_cache_rate_configured = ReaderCost(
        input_tokens=1000,
        output_tokens=0,
        cache_tokens=4000,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    priced_without_rate = price_reader_cost(no_cache_rate_configured, EXPENSIVE, RATES)
    assert priced_without_rate.cached_input.usd is None
    assert priced_without_rate.cached_input.reason == "cache_rate_unknown"
    # A cache rate gap must not fall back to the (cheaper) input rate for those tokens.
    assert priced_without_rate.total.usd is None
    assert priced_without_rate.total.reason == "cache_rate_unknown"


def test_cache_inclusion_ambiguity_is_unknown_not_guessed():
    """A cache rate can exist while it's still unknown whether input_tokens already
    counts those cache reads once. Guessing either way (additive -> double-counts,
    already-included -> undercounts) would misprice real spend, so it must stay unknown."""
    ambiguous_rate = RateTable(
        [
            ProviderRate(
                provider="test-cheap-reader",
                model="nano-v1",
                input_usd_per_million=Decimal("0.10"),
                output_usd_per_million=Decimal("0.40"),
                cached_input_usd_per_million=Decimal("0.025"),
                # input_includes_cached left unset (None) - the ambiguous case.
                as_of="2026-09-27",
                source="fixture, not a real provider price",
            ),
        ]
    )
    cost = ReaderCost(
        input_tokens=1000,
        output_tokens=0,
        cache_tokens=500,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    priced = price_reader_cost(cost, CHEAP, ambiguous_rate)
    assert priced.total.usd is None
    assert priced.total.reason == "cache_inclusion_unknown"

    # No cache tokens reported at all -> the ambiguity is moot, pricing proceeds.
    no_cache = ReaderCost(
        input_tokens=1000,
        output_tokens=0,
        cache_tokens=0,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    priced_no_cache = price_reader_cost(no_cache, CHEAP, ambiguous_rate)
    assert priced_no_cache.total.is_known


def test_input_includes_cached_true_does_not_double_count():
    """When a rate states cache reads are a subset of input_tokens (billed at the
    discounted cache rate), only the *uncached remainder* of input_tokens is billed at
    the full input rate - charging all of input_tokens at the full rate on top of the
    cache component would double-count the cache-read tokens' cost."""
    already_included_rate = RateTable(
        [
            ProviderRate(
                provider="test-cheap-reader",
                model="nano-v1",
                input_usd_per_million=Decimal("0.10"),
                output_usd_per_million=Decimal("0.40"),
                cached_input_usd_per_million=Decimal("0.025"),
                input_includes_cached=True,
                as_of="2026-09-27",
                source="fixture, not a real provider price",
            ),
        ]
    )
    cost = ReaderCost(
        input_tokens=1000,
        output_tokens=0,
        cache_tokens=500,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    priced = price_reader_cost(cost, CHEAP, already_included_rate)
    assert priced.total.is_known
    # Uncached remainder (500 tokens) at the input rate, plus the 500 cached tokens at
    # the discounted cache rate: 500 * 0.10/1e6 + 500 * 0.025/1e6.
    expected_input = (Decimal(500) * Decimal("0.10")) / Decimal(1_000_000)
    expected_cache = (Decimal(500) * Decimal("0.025")) / Decimal(1_000_000)
    assert priced.input.usd == expected_input
    assert priced.cached_input.usd == expected_cache
    assert priced.total.usd == expected_input + expected_cache


def test_input_includes_cached_true_with_cache_exceeding_input_is_unknown():
    """``cache_tokens > input_tokens`` is internally inconsistent under the
    already-included convention (cache reads can only be a subset of input) - refuse to
    price it either way rather than producing a negative uncached-token count."""
    already_included_rate = RateTable(
        [
            ProviderRate(
                provider="test-cheap-reader",
                model="nano-v1",
                input_usd_per_million=Decimal("0.10"),
                output_usd_per_million=Decimal("0.40"),
                cached_input_usd_per_million=Decimal("0.025"),
                input_includes_cached=True,
                as_of="2026-09-27",
                source="fixture, not a real provider price",
            ),
        ]
    )
    cost = ReaderCost(
        input_tokens=100,
        output_tokens=0,
        cache_tokens=500,  # exceeds input_tokens - impossible under this convention
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    priced = price_reader_cost(cost, CHEAP, already_included_rate)
    assert priced.total.usd is None
    assert priced.total.reason == "cache_exceeds_input"
    assert priced.input.reason == "cache_exceeds_input"


def test_output_tokens_are_weighted_by_their_own_rate_not_the_input_rate():
    """Output is priced at output_usd_per_million even when it differs sharply from input."""
    cost = ReaderCost(
        input_tokens=1000,
        output_tokens=1000,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    priced = price_reader_cost(cost, CHEAP, RATES)
    assert priced.input.usd == Decimal("0.0001")  # 1000 * 0.10 / 1e6
    assert priced.output.usd == Decimal("0.0004")  # 1000 * 0.40 / 1e6
    assert priced.output.usd > priced.input.usd


def test_estimated_usage_prices_as_estimate_never_as_a_known_total():
    """A bytes_div_4 estimate (usage was never reported) must not be folded into the
    known-cost total, even though every field can technically be multiplied out."""
    estimated = ReaderCost.estimated_from_bytes(
        prompt_bytes=4000, completion_bytes=400, attempts_started=1
    )
    assert estimated.method == TokenMethod.BYTES_DIV_4
    # EXPENSIVE has no cache rate configured, so estimated_from_bytes's cache_tokens=None
    # prices as a real $0 (no cache dimension), not "unknown" - only input/output/method
    # gate the total here.
    priced = price_reader_cost(estimated, EXPENSIVE, RATES)
    assert priced.estimated_total.is_known  # visible for transparency...
    assert priced.total.usd is None  # ...but never counted as a known dollar figure
    # estimated_from_bytes always reports attempts_usage_complete=0, so the "no attempts
    # had verified usage" reason applies here alongside the non-exact method.
    assert priced.total.reason == "usage_incomplete"


def test_incomplete_attempt_usage_prices_as_estimate_not_known():
    """The audit's own ratio: 109 attempts started, 95 with complete usage. A reader
    cost built from that shape must not be priced as a fully known total."""
    partial = ReaderCost(
        input_tokens=961_248,
        output_tokens=99_614,
        cache_tokens=0,
        method=TokenMethod.EXACT,
        attempts_started=109,
        attempts_usage_complete=95,
    )
    priced = price_reader_cost(partial, CHEAP, RATES)
    assert priced.estimated_total.is_known
    assert priced.total.usd is None
    assert priced.total.reason == "usage_incomplete"


def test_provider_fallback_prices_each_operations_own_identity():
    """A fallback chain that serves different chunks from different providers must not be
    priced as though every chunk used the primary's rate."""
    served_by_primary = ReaderCost(
        input_tokens=1000,
        output_tokens=200,
        cache_tokens=0,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    served_by_alternate = ReaderCost(
        input_tokens=1000,
        output_tokens=200,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )

    primary_priced = price_reader_cost(served_by_primary, CHEAP, RATES)
    alternate_priced = price_reader_cost(served_by_alternate, EXPENSIVE, RATES)

    assert primary_priced.total.is_known and alternate_priced.total.is_known
    # Identical token counts, different provider -> different price, proving no shared rate leaked.
    assert primary_priced.total.usd != alternate_priced.total.usd

    unpriced_alternate = ReaderCost(
        input_tokens=1000,
        output_tokens=200,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    report = build_economics_report(
        [
            ReportLine(
                operation_id="op-primary",
                identity=CHEAP,
                reader_cost=served_by_primary,
                baseline_credit_tokens=500,
                main_context_tokens_saved=500,
                net_tokens_saved=490,
            ),
            ReportLine(
                operation_id="op-fallback-unpriced",
                identity=UNPRICED,
                reader_cost=unpriced_alternate,
                baseline_credit_tokens=300,
                main_context_tokens_saved=300,
                net_tokens_saved=280,
            ),
        ],
        RATES,
    )
    assert report.operations == 2
    assert report.priced_operations == 1
    assert report.unpriced_operations == 1
    assert report.unpriced_reasons == {"rate_unknown": 1}
    assert report.reader_cost_complete is False
    # The known subset must still be surfaced (not withheld), but never mistaken for the total.
    assert report.reader_cost_usd_known_subset == primary_priced.total.usd


def test_mixed_provider_operation_with_disagreeing_calls_prices_as_unknown():
    """``reader.py`` collapses an operation's ``requested`` identity to unknown when its
    answering calls disagreed on who served them (``_agreed_identity`` returns ``None``
    for a mixed-provider fallback within one answer) - never to the primary's identity.
    This module must price that operation as unknown, not silently at the primary's rate."""
    mixed_operation_cost = ReaderCost(
        input_tokens=2000,
        output_tokens=400,
        method=TokenMethod.EXACT,
        attempts_started=2,
        attempts_usage_complete=2,
    )
    # ModelIdentity() with neither field set is exactly what a disagreeing operation's
    # `requested` collapses to (reader.py's UNKNOWN sentinel) - not CHEAP, the primary.
    priced = price_reader_cost(mixed_operation_cost, ModelIdentity(), RATES)
    assert priced.total.usd is None
    assert priced.total.reason == "identity_unknown"


def test_report_does_not_manufacture_repeated_turn_credits():
    """Summing report lines must not invent a second baseline credit for one snapshot.

    Uses two real :func:`~context_shunt.accounting.compose` calls (not hand-fed numbers)
    over the same baseline with ``baseline_credited`` true only the first time - exactly
    the "credited once" rule this report must carry through unchanged.
    """
    baseline = Baseline.withheld_payload(raw_input_bytes=8000)
    egress = Egress(boundary=DeliveryBoundary.ENVELOPE, byte_count=400)
    reader_cost = ReaderCost(
        input_tokens=1000,
        output_tokens=100,
        cache_tokens=0,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )

    first_record = compose(
        operation_id="op-1",
        kind=OperationKind.READ,
        status="complete",
        code="ANSWERED",
        baseline=baseline,
        baseline_credited=True,
        reader=reader_cost,
        egress=egress,
    )
    repeat_record = compose(
        operation_id="op-2",
        kind=OperationKind.READ,
        status="complete",
        code="ANSWERED",
        baseline=baseline,
        baseline_credited=False,  # same snapshot already credited by op-1
        reader=reader_cost,
        egress=egress,
    )
    assert first_record.baseline_credit_tokens > 0
    assert repeat_record.baseline_credit_tokens == 0

    lines = [
        ReportLine.from_record(first_record, CHEAP),
        ReportLine.from_record(repeat_record, CHEAP),
    ]
    report = build_economics_report(lines, RATES)
    assert report.gross_main_context_counterfactual_tokens == first_record.baseline_credit_tokens
    # Priced cost still accrues on both operations even though the credit did not repeat.
    assert report.priced_operations == 2


def test_main_context_counterfactual_opportunity_is_separate_from_reader_cost():
    """The gross main-context counterfactual can be priced against a main-model rate,
    entirely independent of the reader's own rate table and cost."""
    line = ReportLine(
        operation_id="op-1",
        identity=CHEAP,
        reader_cost=ReaderCost(
            input_tokens=1000,
            output_tokens=100,
            cache_tokens=0,
            method=TokenMethod.EXACT,
            attempts_started=1,
            attempts_usage_complete=1,
        ),
        baseline_credit_tokens=2000,
        main_context_tokens_saved=1900,
        net_tokens_saved=800,
    )

    without_main_rate = build_economics_report([line], RATES)
    assert without_main_rate.main_context_counterfactual_opportunity_usd.usd is None
    assert (
        without_main_rate.main_context_counterfactual_opportunity_usd.reason
        == "main_model_rate_not_supplied"
    )

    with_main_rate = build_economics_report(
        [line], RATES, main_model_identity=MAIN_MODEL, main_model_rates=MAIN_RATES
    )
    opportunity = with_main_rate.main_context_counterfactual_opportunity_usd
    assert opportunity.is_known
    assert opportunity.usd == (Decimal(2000) * Decimal("3.00")) / Decimal(1_000_000)
    # Never conflated with the reader's own priced cost.
    assert opportunity.usd != with_main_rate.reader_cost_usd_known_subset


def test_baseline_delta_only_populates_when_both_sides_are_fully_known():
    """A cost-to-cost delta against a caller-supplied baseline, never an invented saving."""
    line = ReportLine(
        operation_id="op-1",
        identity=CHEAP,
        reader_cost=ReaderCost(
            input_tokens=1000,
            output_tokens=100,
            cache_tokens=0,
            method=TokenMethod.EXACT,
            attempts_started=1,
            attempts_usage_complete=1,
        ),
        baseline_credit_tokens=2000,
        main_context_tokens_saved=2000,
        net_tokens_saved=1900,
    )
    priced = price_reader_cost(line.reader_cost, CHEAP, RATES)

    with_baseline = build_economics_report(
        [line], RATES, old_compactor_baseline_usd=Decimal("1.00")
    )
    assert with_baseline.baseline_delta_usd == priced.total.usd - Decimal("1.00")

    without_baseline = build_economics_report([line], RATES)
    assert without_baseline.old_compactor_baseline_usd is None
    assert without_baseline.baseline_delta_usd is None

    unpriced_line = ReportLine(
        operation_id="op-2",
        identity=UNPRICED,
        reader_cost=ReaderCost(
            input_tokens=1000,
            output_tokens=100,
            method=TokenMethod.EXACT,
            attempts_started=1,
            attempts_usage_complete=1,
        ),
        baseline_credit_tokens=0,
        main_context_tokens_saved=0,
        net_tokens_saved=-50,
    )
    incomplete_with_baseline = build_economics_report(
        [line, unpriced_line], RATES, old_compactor_baseline_usd=Decimal("1.00")
    )
    # One operation's cost is unknown, so no delta may be claimed even though a baseline was given.
    assert incomplete_with_baseline.baseline_delta_usd is None


def test_no_priced_operations_reports_unknown_subset_not_a_fabricated_zero():
    report = build_economics_report(
        [
            ReportLine(
                operation_id="op-1",
                identity=UNPRICED,
                reader_cost=ReaderCost(
                    input_tokens=1000,
                    output_tokens=100,
                    method=TokenMethod.EXACT,
                    attempts_started=1,
                    attempts_usage_complete=1,
                ),
                baseline_credit_tokens=0,
                main_context_tokens_saved=0,
                net_tokens_saved=-50,
            )
        ],
        RATES,
    )
    assert report.reader_cost_usd_known_subset is None
    assert report.reader_cost_complete is False


def test_observed_production_rates_scenario_stays_unknown_on_cache_and_usage_gaps():
    """The parent's separately-researched observed rates (sub2api-upstream usage_logs,
    2026-09-27 window, service-wide internal metered accounting - NOT Shunt-marginal
    cost, and never to be read as universal default rates: hence built only inline here,
    never added to a module default) applied to the audit's own reader usage shape
    (input=961,248, output=99,614, cache_tokens unknown/NULL - not zero, 109 attempts
    started vs. 95 with complete usage).

    This is a scenario check, not a bound or a measured saving: it confirms the honest
    gating rules (cache count truly unknown must stay unknown even when a cache rate
    exists; incomplete usage must keep ``total`` unknown) hold under real observed
    numbers, and that ``estimated_total`` - visible but never counted as known cost -
    can still line up with an external back-of-envelope figure when the provider has no
    cache pricing recorded for this window at all.
    """
    audit_reader_cost = ReaderCost(
        input_tokens=961_248,
        output_tokens=99_614,
        cache_tokens=None,  # NULL in accounting_events: unreported, not a reported zero
        method=TokenMethod.EXACT,
        attempts_started=109,
        attempts_usage_complete=95,
    )

    # gpt-5.6-luna: OpenAI-style input_tokens already includes cache reads, and this
    # window's usage_logs give it a real (non-null) cache rate - so an unreported cache
    # count cannot be priced as "no cache dimension", it must stay unknown.
    luna_rates = RateTable(
        [
            ProviderRate(
                provider="codex-official",
                model="gpt-5.6-luna",
                input_usd_per_million=Decimal("0.2"),
                cached_input_usd_per_million=Decimal("0.02"),
                output_usd_per_million=Decimal("1.2"),
                input_includes_cached=True,
                as_of="2026-09-27",
                source=(
                    "sub2api-upstream-postgres usage_logs, service-wide today window, "
                    "summed component cost / summed component tokens - internal metered "
                    "accounting, not proof of marginal upstream cash cost; not a default rate"
                ),
            )
        ]
    )
    luna_identity = ModelIdentity(provider="codex-official", model="gpt-5.6-luna")
    luna_priced = price_reader_cost(audit_reader_cost, luna_identity, luna_rates)
    # Real cache spend may have happened; the count just was never reported - not zero.
    assert luna_priced.total.usd is None
    assert luna_priced.estimated_total.usd is None
    assert luna_priced.estimated_total.reason == "usage_unknown"

    # claude-sonnet-5: this window's usage_logs recorded no cached reads at all, so no
    # cache rate was derived for it - a null cache count here means "no cache dimension
    # observed", genuinely priced at $0, not "unknown".
    sonnet_rates = RateTable(
        [
            ProviderRate(
                provider="anthropic",
                model="claude-sonnet-5",
                input_usd_per_million=Decimal("2"),
                output_usd_per_million=Decimal("10"),
                as_of="2026-09-27",
                source=(
                    "sub2api-upstream-postgres usage_logs, service-wide today window; "
                    "no cached reads observed this window - not a default rate"
                ),
            )
        ]
    )
    sonnet_identity = ModelIdentity(provider="anthropic", model="claude-sonnet-5")
    sonnet_priced = price_reader_cost(audit_reader_cost, sonnet_identity, sonnet_rates)
    # 109 started vs. 95 usage-complete keeps `total` unknown either way...
    assert sonnet_priced.total.usd is None
    assert sonnet_priced.total.reason == "usage_incomplete"
    # ...but the visible estimate matches the source document's own scenario arithmetic
    # (961,248 * 2 + 99,614 * 10, over 1e6) - not asserted as a measured dollar figure.
    assert sonnet_priced.estimated_total.usd == Decimal("2.918636")

    # Sol (gpt-5.6-sol) as the main model: the gross withheld-context figure priced
    # against Sol's observed uncached input rate, kept entirely separate from reader
    # cost - an opportunity scenario, not a claimed avoided bill (long-context brackets,
    # repeated exposure and cache behavior are all unresolved, per the source document).
    sol_rates = RateTable(
        [
            ProviderRate(
                provider="codex-official",
                model="gpt-5.6-sol",
                input_usd_per_million=Decimal("5"),
                cached_input_usd_per_million=Decimal("0.5"),
                output_usd_per_million=Decimal("30"),
                as_of="2026-09-27",
                source=(
                    "sub2api-upstream-postgres usage_logs, service-wide today window - "
                    "not a default rate, not per-attempt Shunt attribution"
                ),
            )
        ]
    )
    line = ReportLine(
        operation_id="scenario-audit-window",
        identity=sonnet_identity,
        reader_cost=audit_reader_cost,
        # The source document's own scenario figure, used here only as an illustrative
        # gross-tokens proxy - not a claim that this equals `baseline_credit_tokens`
        # field-for-field from a real ledger read this session.
        baseline_credit_tokens=1_215_287,
        main_context_tokens_saved=1_215_287,
        net_tokens_saved=1_215_287 - 961_248 - 99_614,
    )
    report = build_economics_report(
        [line],
        sonnet_rates,
        main_model_identity=ModelIdentity(provider="codex-official", model="gpt-5.6-sol"),
        main_model_rates=sol_rates,
    )
    opportunity = report.main_context_counterfactual_opportunity_usd
    assert opportunity.is_known
    assert opportunity.usd == Decimal("6.076435")
    # Never conflated with, or summed into, the reader's own (here, unknown) cost total.
    assert report.reader_cost_usd_known_subset is None
