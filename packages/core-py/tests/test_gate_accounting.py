"""unit accounting: signed savings, one-time credit, and never zero-for-unknown.

The arithmetic is small; the discipline is the point. This gate pins the two formulas,
proves the baseline is credited once per snapshot, proves an unreported token count stays
null rather than becoming a zero, and proves the stats surface cannot be turned into a
reset switch, a cross-session read or a content channel.
"""

from __future__ import annotations

import json

import pytest

from context_shunt.accounting import (
    Baseline,
    BaselineKind,
    DeliveryBoundary,
    Egress,
    OperationKind,
    ReaderCost,
    compose,
    estimate_tokens,
)
from context_shunt.clock import Deadline, FakeClock
from context_shunt.errors import ShuntError
from context_shunt.limits import BASELINE_ESTIMATE_METHOD, DEFAULT_LIMITS, EMITTED_SCHEMA_VERSION
from context_shunt.metrics import ALLOWED_LABEL_KEYS, InMemoryMetrics, MetricsError
from context_shunt.provenance import ModelIdentity, TokenMethod, Usage
from context_shunt.provider import ModelResponse, ProviderTarget
from context_shunt.reader import Reader
from context_shunt.session import ShuntSession
from context_shunt.snapshot import snapshot_bytes
from tests.support import FakeLuna, answer_json, make_capability, make_config, make_registry

pytestmark = pytest.mark.gate_accounting

L = DEFAULT_LIMITS
CANARY = "ACCOUNTING-CANARY-51ee7a"


def _session(tmp_path, provider=None, **overrides):
    config = make_config(tmp_path, **overrides)
    return ShuntSession("sess", config, make_capability(), provider=provider or FakeLuna())


def _big_source(tmp_path, session, marker: str = CANARY):
    path = tmp_path / "ws" / "big.txt"
    path.write_text(
        f"line 0001 {marker}\n" + "".join(f"line {i:04d} value-{i}\n" for i in range(2, 2001))
    )
    return session.register_path(str(path))


def _read_request(entry, question="What values are configured?", refined=False):
    request = {
        "schema_version": EMITTED_SCHEMA_VERSION,
        "request_id": "req_a1",
        "operation": "read",
        "question": question,
        "sources": [
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
        ],
        "budgets": {"max_chunks": 2, "max_answer_bytes": 4096, "deadline_ms": 60000},
    }
    if refined:
        request["refined"] = True
    return request


def _stats(session, **kw):
    return session.stats(
        {
            "schema_version": EMITTED_SCHEMA_VERSION,
            "request_id": "req_stats",
            "operation": "stats",
            **kw,
        }
    )


# -- the formulas -----------------------------------------------------------


def test_the_two_formulas_are_exactly_as_specified():
    record = compose(
        operation_id="acc_" + "1" * 16,
        kind=OperationKind.READ,
        status="ok",
        code="ANSWERED",
        baseline=Baseline.withheld_payload(400_000),
        baseline_credited=True,
        reader=ReaderCost(
            input_tokens=6100,
            output_tokens=420,
            method=TokenMethod.EXACT,
            attempts_started=1,
            attempts_usage_complete=1,
        ),
        egress=Egress(boundary=DeliveryBoundary.ENVELOPE, byte_count=2048),
    )
    baseline_tokens = estimate_tokens(400_000)
    envelope_tokens = estimate_tokens(2048)
    assert record.baseline_credit_tokens == baseline_tokens
    assert record.main_model_envelope_tokens == envelope_tokens
    assert record.main_context_tokens_saved == baseline_tokens - envelope_tokens
    assert record.net_tokens_saved == record.main_context_tokens_saved - 6100 - 420


def test_both_savings_are_signed_and_go_negative_when_they_should():
    """An inspect page or a refined question costs context and withholds nothing new."""
    record = compose(
        operation_id="acc_" + "2" * 16,
        kind=OperationKind.INSPECT,
        status="ok",
        code="EXTRACTED",
        baseline=Baseline.none(),
        baseline_credited=False,
        reader=ReaderCost.none(),
        egress=Egress(boundary=DeliveryBoundary.EXTRACTION, byte_count=16384),
    )
    assert record.main_context_tokens_saved == -estimate_tokens(16384)
    assert record.net_tokens_saved == record.main_context_tokens_saved
    assert record.baseline_kind == BaselineKind.NONE.value
    assert record.raw_input_baseline_tokens is None


def test_an_unreported_usage_is_a_named_estimate_never_a_zero():
    reader = ReaderCost.estimated_from_bytes(
        prompt_bytes=8000, completion_bytes=400, attempts_started=1
    )
    assert reader.method is TokenMethod.BYTES_DIV_4
    assert reader.method.value == BASELINE_ESTIMATE_METHOD
    assert reader.input_tokens == estimate_tokens(8000)
    assert reader.attempts_usage_complete == 0
    # ReaderCost.none() means "no attempt", which is a different fact from "unreported".
    assert ReaderCost.none().input_tokens is None
    assert ReaderCost.none().method is TokenMethod.NOT_APPLICABLE


def test_exact_provider_usage_wins_over_the_estimate():
    exact = ReaderCost(
        input_tokens=11,
        output_tokens=7,
        method=TokenMethod.EXACT,
        attempts_started=1,
        attempts_usage_complete=1,
    )
    record = compose(
        operation_id="acc_" + "3" * 16,
        kind=OperationKind.READ,
        status="ok",
        code="ANSWERED",
        baseline=Baseline.withheld_payload(4096),
        baseline_credited=True,
        reader=exact,
        egress=Egress(boundary=DeliveryBoundary.ENVELOPE, byte_count=512),
    )
    assert record.reader_token_method == "exact"
    assert record.reader_input_tokens == 11 and record.reader_output_tokens == 7


def test_a_host_truncated_baseline_is_labelled_separately(tmp_path):
    """Crediting the full payload when the host already truncated would be a fabrication."""
    counterfactual = Baseline.withheld_payload(1_000_000)
    observed = Baseline.host_truncated(16_384)
    assert counterfactual.kind is BaselineKind.FULL_PAYLOAD_COUNTERFACTUAL
    assert observed.kind is BaselineKind.HOST_TRUNCATED_OBSERVED
    assert observed.tokens is not None and observed.tokens < counterfactual.tokens


# -- one-time credit --------------------------------------------------------


def test_the_baseline_is_credited_once_and_recovery_only_adds_cost(tmp_path):
    reply = answer_json(
        f"The first line carries {CANARY} [c1].",
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": CANARY}],
    )
    luna = FakeLuna(default_reply=reply)
    session = _session(tmp_path, provider=luna)
    entry = _big_source(tmp_path, session)

    first = session.read(_read_request(entry))
    assert first["code"] == "ANSWERED"
    refined = session.read(_read_request(entry, "And the second line?", refined=True))
    assert refined["status"] in ("ok", "partial")

    records = {r["operation_id"]: r for r in _stats(session)["stats"]["records"]}
    reads = [r for r in records.values() if r["kind"] in ("read", "refined_read")]
    assert len(reads) == 2
    credited = [r for r in reads if r["baseline_credit_tokens"] > 0]
    refinements = [r for r in reads if r["kind"] == "refined_read"]
    assert len(credited) == 1, "the withheld-source baseline was credited more than once"
    assert refinements[0]["baseline_credit_tokens"] == 0
    # The refinement still reports what the payload measures, so the zero credit is
    # visibly a policy decision rather than a missing measurement.
    assert refinements[0]["raw_input_baseline_tokens"] > 0
    assert refinements[0]["main_context_tokens_saved"] < 0


def test_a_failed_retry_adds_overhead_without_claiming_a_saving(tmp_path):
    from context_shunt.provider import TransientProviderError

    luna = FakeLuna(default_reply=TransientProviderError("PROVIDER_CALL_FAILED"))
    session = _session(tmp_path, provider=luna)
    entry = _big_source(tmp_path, session)
    session.read(_read_request(entry))
    failed = session.read(_read_request(entry, "try again", refined=True))
    records = _stats(session)["stats"]["records"]
    refinement = next(r for r in records if r["kind"] == "refined_read")
    assert refinement["baseline_credit_tokens"] == 0
    assert refinement["attempts_started"] >= 1
    assert failed["coverage"]["complete"] is False


def test_inspect_pages_accumulate_cost_against_one_credited_baseline(tmp_path):
    session = _session(tmp_path)
    entry = _big_source(tmp_path, session)
    session.read(_read_request(entry))
    for _ in range(3):
        session.inspect(
            {
                "schema_version": EMITTED_SCHEMA_VERSION,
                "request_id": "req_i",
                "operation": "inspect",
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": {"kind": "lines", "start": 1, "end": 40},
                "budgets": {"max_result_bytes": 1024, "max_scan_lines": 100},
            }
        )
    stats = _stats(session, page_size=8)["stats"]
    inspects = [r for r in stats["records"] if r["kind"] == "inspect"]
    assert len(inspects) == 3
    assert all(r["baseline_credit_tokens"] == 0 for r in inspects)
    assert all(r["main_context_tokens_saved"] < 0 for r in inspects)
    assert all(r["delivery_boundary"] == "extraction" for r in inspects)


# -- egress measurement -----------------------------------------------------


def test_egress_is_measured_on_the_final_encoded_envelope(tmp_path):
    """The envelope carries only the opaque id, so the measurement is not self-referential."""
    session = _session(tmp_path)
    entry = _big_source(tmp_path, session)
    envelope = session.read(_read_request(entry))
    exact_bytes = len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())
    record = next(
        r
        for r in _stats(session)["stats"]["records"]
        if r["operation_id"] == envelope["accounting_id"]
    )
    assert record["main_model_envelope_bytes"] == exact_bytes
    assert record["main_model_envelope_tokens"] == estimate_tokens(exact_bytes)
    assert record["envelope_token_method"] == BASELINE_ESTIMATE_METHOD


def test_no_record_carries_a_monetary_value(tmp_path):
    session = _session(tmp_path)
    entry = _big_source(tmp_path, session)
    session.read(_read_request(entry))
    blob = json.dumps(_stats(session)).lower()
    for money in ("usd", "cost", "price", "dollar", "cents"):
        assert money not in blob


# -- the stats surface ------------------------------------------------------


def test_stats_is_read_only_and_scoped_to_this_session(tmp_path):
    session = _session(tmp_path)
    entry = _big_source(tmp_path, session)
    session.read(_read_request(entry))
    before = _stats(session)["stats"]["total_records"]

    # There is no parameter that could reset a counter or name another session; an
    # unknown field is rejected rather than ignored.
    rejected = session.stats(
        {
            "schema_version": EMITTED_SCHEMA_VERSION,
            "request_id": "req_stats",
            "operation": "stats",
            "reset": True,
        }
    )
    assert rejected["status"] == "error" and rejected["code"] == "INVALID_REQUEST"

    foreign = session.stats(
        {
            "schema_version": EMITTED_SCHEMA_VERSION,
            "request_id": "req_stats",
            "operation": "stats",
            "session_id": "someone-else",
        }
    )
    assert foreign["status"] == "error"
    # Reading stats records its own operation and destroys nothing.
    assert _stats(session)["stats"]["total_records"] > before


def test_stats_pages_are_bounded_and_reveal_no_content(tmp_path):
    session = _session(tmp_path)
    entry = _big_source(tmp_path, session)
    for _ in range(12):
        session.read(_read_request(entry))
    stats = _stats(session, page=1, page_size=8)["stats"]
    assert len(stats["records"]) <= L.stats_max_records_per_page
    assert stats["next_page"] == 2
    page_two = _stats(session, page=2, page_size=8)["stats"]
    assert {r["operation_id"] for r in stats["records"]}.isdisjoint(
        {r["operation_id"] for r in page_two["records"]}
    )
    blob = json.dumps(_stats(session, page=1, page_size=8))
    assert CANARY not in blob
    assert str(tmp_path) not in blob
    assert "What values are configured?" not in blob


def test_stats_records_carry_only_closed_enums_and_counters(tmp_path):
    session = _session(tmp_path)
    entry = _big_source(tmp_path, session)
    session.read(_read_request(entry))
    for record in _stats(session)["stats"]["records"]:
        assert record["kind"] in {
            "gate_block",
            "capture",
            "read",
            "refined_read",
            "inspect",
            "stats",
            "spill",
        }
        assert record["status"] in {"ok", "partial", "blocked", "error"}
        assert record["baseline_method"] in {"exact", "bytes_div_4", "unknown"}
        assert record["delivery_boundary"] in {
            "envelope",
            "extraction",
            "pointer",
            "block_message",
            "none",
        }
        # No model or provider name anywhere in a record.
        assert "luna" not in json.dumps(record).lower()


def test_metric_labels_are_a_closed_enum_and_reject_high_cardinality():
    metrics = InMemoryMetrics()
    assert "model" not in ALLOWED_LABEL_KEYS
    assert "provider" not in ALLOWED_LABEL_KEYS
    assert "path" not in ALLOWED_LABEL_KEYS
    for bad in ({"model": "gpt-5.6-luna"}, {"provider": "openai"}, {"request_id": "req_1"}):
        with pytest.raises(MetricsError):
            metrics.count("reader_model_calls", bad)
    # An allowed key still refuses a value that is not a bounded token.
    with pytest.raises(MetricsError):
        metrics.count("gate_decision", {"reason": "path /tmp/secret with spaces"})


def test_totals_preserve_null_for_an_unreported_direction(tmp_path):
    """A session where no provider reported usage shows null, not zero."""
    luna = FakeLuna(usage_exact=False, default_reply=answer_json("", []))
    session = _session(tmp_path, provider=luna)
    entry = _big_source(tmp_path, session)
    session.read(_read_request(entry))
    totals = _stats(session)["stats"]["totals"]
    # The reader cost is a named estimate, so the totals are populated but the method on
    # each record says how. Cache tokens were never reported at all and stay null.
    assert totals["reader_cache_tokens"] is None
    record = next(r for r in _stats(session)["stats"]["records"] if r["kind"] == "read")
    assert record["reader_token_method"] == "bytes_div_4"
    assert record["reader_cache_tokens"] is None
    assert record["attempts_usage_complete"] == 0


def test_exact_provider_usage_survives_into_the_reader_cost(tmp_path):
    """The whole point of `usage_exact` is that it is not an estimate.

    `ChunkOutcome.usage` started at `Usage()`, whose method is `UNKNOWN` - correct for a
    bridge that reported no counts, wrong for an empty accumulator. `UNKNOWN + EXACT` is
    `UNKNOWN`, so the provider's exact counts were merged away and every request fell back
    to the byte estimate. The distinction the accounting layer exists to make never
    reached a single envelope.
    """
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"mode = fast\n"))
    luna = FakeLuna(default_reply=answer_json("mode = fast [c1]", [(1, 1, "mode = fast")]))
    result = Reader(registry, luna).answer("sess", _read_request(entry))

    assert result.cost.method is TokenMethod.EXACT
    assert result.cost.attempts_started == 1
    assert result.cost.attempts_usage_complete == 1
    # The provider's own numbers, not `len(bytes) / 4`.
    assert result.cost.input_tokens == 10
    assert result.cost.output_tokens == 5

    # A bridge that reports nothing still yields the named estimate, not a false `exact`.
    silent = FakeLuna(
        default_reply=answer_json("mode = fast [c1]", [(1, 1, "mode = fast")]),
        usage_exact=False,
    )
    estimated = Reader(registry, silent).answer("sess", _read_request(entry))
    assert estimated.cost.method is TokenMethod.BYTES_DIV_4
    assert estimated.cost.attempts_usage_complete == 0


def test_a_mixed_source_read_credits_only_the_newly_withheld_source(tmp_path):
    """The baseline is the saving from withholding *this* source, once.

    `_baseline_for` summed the bytes of every selected source but folded the per-source
    credit results into a single OR, so a second read that mixed an already-credited
    source with a new one credited both again - the previously withheld source's bytes
    were counted a second time and the reported saving was inflated.
    """
    session = _session(tmp_path)
    ws = tmp_path / "ws"
    (ws / "one.txt").write_text("".join(f"one {i:04d} value\n" for i in range(1, 2001)))
    (ws / "two.txt").write_text("".join(f"two {i:04d} value\n" for i in range(1, 3001)))
    first = session.register_path(str(ws / "one.txt"))
    second = session.register_path(str(ws / "two.txt"))

    def request(*entries):
        base = _read_request(first)
        base["sources"] = [
            {
                "source_id": e.source_id,
                "snapshot_id": e.snapshot.snapshot_id,
                "selector": {"kind": "all"},
            }
            for e in entries
        ]
        return base

    session.read(request(first))
    after_first = _stats(session)["stats"]["totals"]["baseline_credit_tokens"]
    assert after_first == estimate_tokens(first.snapshot.bytes_len)

    # The second read re-uses `first` and adds `second`; only `second` is newly withheld.
    session.read(request(first, second))
    after_second = _stats(session)["stats"]["totals"]["baseline_credit_tokens"]

    added = after_second - after_first
    only_second = estimate_tokens(second.snapshot.bytes_len)
    assert added == only_second, (
        f"credited {added} tokens for the second read, but only {only_second} were newly withheld"
    )


def test_an_over_cap_bridge_reply_still_reports_the_usage_it_was_billed(tmp_path):
    """Rejecting the text must not discard the token counts that came with it.

    The bridge validates the returned text against the output cap *before* it unpacks
    usage, so an over-cap reply raised a sanitized validation error and the exact counts
    the provider reported went with it. The call happened and was billed either way; only
    the text is unusable.
    """
    from context_shunt.limits import DEFAULT_LIMITS
    from context_shunt.provider import HostBridgeProvider

    def oversized(**_kwargs):
        return {
            "text": "x" * (DEFAULT_LIMITS.max_tool_result_bytes + 10),
            "input_tokens": 23,
            "output_tokens": 11,
            "usage_exact": True,
        }

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"mode = fast\n"))
    result = Reader(registry, HostBridgeProvider(oversized)).answer("sess", _read_request(entry))

    assert result.envelope["code"] in ("NO_MATCH", "INVALID_MODEL_OUTPUT")
    assert result.cost.attempts_started >= 1
    assert result.cost.method is TokenMethod.EXACT
    assert result.cost.input_tokens == 23
    assert result.cost.output_tokens == 11


# --- estimates cover every physical attempt -----------------------------------------
#
# Parity with the TypeScript `estimates cover every physical attempt` suite: a fallback
# chain re-sends the prompt to every candidate and each candidate is billed, so the
# estimate has to charge for every physical call exactly once - and no constituent
# claim may exceed the fixed per-call cap.

_ANSWER_TEXT = answer_json(
    "mode = fast [c1]",
    [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "mode = fast"}],
)


def _plain_unavailable(**_kwargs):
    raise ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)


def _billed_unavailable(input_tokens: int, output_tokens: int):
    def call(**_kwargs):
        exc = ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)
        exc.billed_usage = Usage(
            input_tokens=input_tokens, output_tokens=output_tokens, method=TokenMethod.EXACT
        )
        raise exc

    return call


def _measured(tmp_path, first, second):
    """Run a two-candidate chain and report the prompt bytes actually transmitted."""
    from context_shunt.provider import FallbackChainProvider, HostBridgeProvider

    seen = {"calls": 0, "prompt_bytes": 0}

    def count(bridge):
        def call(*, system, user, **kwargs):
            seen["calls"] += 1
            seen["prompt_bytes"] += len(system.encode("utf-8")) + len(user.encode("utf-8"))
            return bridge(system=system, user=user, **kwargs)

        return call

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"mode = fast\n"))
    chain = FallbackChainProvider(
        HostBridgeProvider(count(first), L, provider="openai"),
        [HostBridgeProvider(count(second), L, "f0", provider="openai")],
    )
    result = Reader(registry, chain).answer("sess", _read_request(entry))
    return seen, result


def test_the_estimate_charges_for_every_attempt_prompt_on_an_unreported_failure(tmp_path):
    """The prompt is re-sent by every candidate, but was charged once per invocation.

    A two-candidate chain therefore estimated four physical calls' input from one call's
    bytes and reported roughly half of what was really transmitted.
    """
    seen, result = _measured(tmp_path, _plain_unavailable, _plain_unavailable)

    assert seen["calls"] == 4
    assert result.cost.attempts_started == seen["calls"]
    assert result.cost.method is TokenMethod.BYTES_DIV_4
    assert result.cost.input_tokens == -(-seen["prompt_bytes"] // 4)


def test_the_estimate_charges_for_every_attempt_prompt_on_an_eventual_success(tmp_path):
    seen, result = _measured(tmp_path, _plain_unavailable, lambda **_k: {"text": _ANSWER_TEXT})

    assert seen["calls"] == 2
    assert result.envelope["code"] == "ANSWERED"
    assert result.cost.attempts_started == seen["calls"]
    assert result.cost.input_tokens == -(-seen["prompt_bytes"] // 4)


def test_the_estimate_includes_billed_output_from_attempts_never_seen(tmp_path):
    """Output the reader never read is still output that was billed."""
    seen, result = _measured(tmp_path, _billed_unavailable(5, 3), _plain_unavailable)

    assert seen["calls"] == 4
    assert result.cost.method is TokenMethod.BYTES_DIV_4
    # Two of the four attempts reported three output tokens each.
    assert result.cost.output_tokens == 6


def test_a_billed_failure_claim_no_single_call_could_have_produced_is_refused(tmp_path):
    """Bounding the sum cannot prove each constituent respected the per-call cap.

    A `ShuntError` the host raises itself never passes the bridge's usage validation, so a
    failure reporting one token over the cap plus a winner reporting one stayed under the
    two-attempt ceiling and was published as exact.
    """
    over_cap = _billed_unavailable(1, L.max_output_tokens_per_call + 1)

    def tiny_winner(**_kwargs):
        return {"text": _ANSWER_TEXT, "input_tokens": 1, "output_tokens": 1, "usage_exact": True}

    seen, result = _measured(tmp_path, over_cap, tiny_winner)

    assert seen["calls"] == 2
    # The call still failed the way it failed, so the chain still advanced and answered.
    assert result.envelope["code"] == "ANSWERED"
    # But the impossible claim is not evidence, so the total is not exact and never
    # carries the over-cap number.
    assert result.cost.method is not TokenMethod.EXACT
    assert result.cost.output_tokens < L.max_output_tokens_per_call


def test_a_billed_failure_claim_that_respects_the_per_call_cap_is_still_accepted(tmp_path):
    at_cap = _billed_unavailable(1, L.max_output_tokens_per_call)
    _seen, result = _measured(tmp_path, at_cap, _plain_unavailable)

    # Exactly at the ceiling is legal, and two attempts reported it.
    assert result.cost.output_tokens == L.max_output_tokens_per_call * 2


def test_a_billed_loser_and_an_exact_winner_are_each_counted_exactly_once(tmp_path):
    """The other branch of the same rule.

    A billed failure's usage is recorded in *both* the outcome's usage and its unseen
    usage. Only one is ever read: the exact branch reads the usage, the estimate branch
    discards it and reads the unseen total, so the loser's output is charged once either
    way.

    Both attempts reported complete usage, so the chain says so and the total is exact -
    the same numbers TypeScript produces for the same schedule. Python used to report an
    estimate here because its chain kept the loser's claim out of the winner's usage and
    could therefore never reach a full usage-complete count; folding it in closes that
    divergence rather than justifying it.
    """

    def exact_winner(**_kwargs):
        return {"text": _ANSWER_TEXT, "input_tokens": 9, "output_tokens": 11, "usage_exact": True}

    seen, result = _measured(tmp_path, _billed_unavailable(1, 7), exact_winner)

    assert seen["calls"] == 2
    assert result.envelope["code"] == "ANSWERED"
    assert result.cost.attempts_started == 2
    assert result.cost.attempts_usage_complete == 2
    assert result.cost.method is TokenMethod.EXACT
    # One loser at 1/7 and one winner at 9/11, each counted once.
    assert result.cost.input_tokens == 10
    assert result.cost.output_tokens == 18


# --- every physical attempt is accounted for once, on every path ---------------------
#
# The ordinary success and failure paths were the first half of this rule. These are the
# paths that bypassed them: a stable provider error re-read across the reader's outer
# retry, a response that arrived after the deadline, a cancellation that raced the
# provider, and a plain `ReaderProvider` whose usage no bridge ever bounded.

_PLAIN = ProviderTarget(model=L.reader_model, provider="plain")


def _billed_error(input_tokens: int, output_tokens: int) -> ShuntError:
    error = ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)
    error.billed_usage = Usage(
        input_tokens=input_tokens, output_tokens=output_tokens, method=TokenMethod.EXACT
    )
    return error


def _fixture(tmp_path, provider, *, clock=None):
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"mode = fast\n"))
    return Reader(registry, provider, clock=clock), _read_request(entry, "What is the mode?")


def _plain_response(output_tokens: int) -> ModelResponse:
    identity = ModelIdentity(provider="plain", model=L.reader_model)
    return ModelResponse(
        text=_ANSWER_TEXT,
        requested=identity,
        resolved=identity,
        reported=identity,
        provider_confirms_generation=True,
        usage=Usage(input_tokens=1, output_tokens=output_tokens, method=TokenMethod.EXACT),
    )


def test_a_stable_provider_error_is_not_reingested_across_the_outer_retry(tmp_path):
    """A provider may raise one stable ``ShuntError`` instance for every call it fails.

    The chain used to write its aggregate onto that object and read the same field back on
    the reader's next outer attempt, so its own earlier total arrived as fresh evidence:
    four calls reporting 24/10 in total were published as 29/13.
    """
    from context_shunt.provider import FallbackChainProvider, HostBridgeProvider

    seen = {"calls": 0}

    def raising(error):
        def call(**_kwargs):
            seen["calls"] += 1
            raise error

        return HostBridgeProvider(call, L, provider="openai")

    chain = FallbackChainProvider(raising(_billed_error(5, 3)), [raising(_billed_error(7, 2))])
    reader, request = _fixture(tmp_path, chain)
    result = reader.answer("sess", request)

    assert seen["calls"] == 4
    assert result.cost.attempts_started == 4
    assert result.cost.attempts_usage_complete == 4
    assert result.cost.method is TokenMethod.EXACT
    assert result.cost.input_tokens == 24
    assert result.cost.output_tokens == 10


def test_a_late_composite_response_is_charged_for_every_prompt_and_unseen_token(tmp_path):
    """A response that arrives after the deadline is refused, but it was still paid for.

    The late branch recorded only the winner's own usage, so the prompt every earlier
    candidate re-sent and the output they reported disappeared.
    """
    from context_shunt.provider import FallbackChainProvider, HostBridgeProvider

    clock = FakeClock()
    seen = {"calls": 0, "prompt_bytes": 0}

    def count(body):
        def call(*, system, user, **kwargs):
            seen["calls"] += 1
            seen["prompt_bytes"] += len(system.encode("utf-8")) + len(user.encode("utf-8"))
            return body()

        return call

    def fail():
        raise _billed_error(5, 3)

    def late():
        clock.advance(70_000)
        return {"text": _ANSWER_TEXT}

    chain = FallbackChainProvider(
        HostBridgeProvider(count(fail), L, provider="openai"),
        [HostBridgeProvider(count(late), L, "fallback", provider="openai")],
    )
    reader, request = _fixture(tmp_path, chain, clock=clock)
    result = reader.answer("sess", request)

    assert result.envelope["code"] == "TIMEOUT"
    assert seen["calls"] == 2
    assert result.cost.attempts_started == 2
    assert result.cost.input_tokens == -(-seen["prompt_bytes"] // 4)
    assert result.cost.output_tokens == -(-len(_ANSWER_TEXT.encode("utf-8")) // 4) + 3


def test_a_billed_attempt_that_cancellation_raced_is_kept(tmp_path):
    """Cancellation can land between the provider failing and the reader reading it.

    The cancelled branch carried none of the call's metadata, so a call billed 5/3 was
    reported as zero usage-complete attempts and zero output tokens.
    """
    from context_shunt.provider import FallbackChainProvider, HostBridgeProvider

    clock = FakeClock()
    deadline = Deadline.start(clock, 60_000)
    seen = {"calls": 0}

    def first(**_kwargs):
        seen["calls"] += 1
        deadline.cancel()
        raise _billed_error(5, 3)

    def never(**_kwargs):
        seen["calls"] += 1
        raise _billed_error(7, 2)

    chain = FallbackChainProvider(
        HostBridgeProvider(first, L, provider="openai"),
        [HostBridgeProvider(never, L, "fallback", provider="openai")],
    )
    reader, request = _fixture(tmp_path, chain, clock=clock)
    result = reader.answer("sess", request, deadline=deadline)

    assert result.envelope["code"] == "CANCELLED"
    # Cancelling the primary must still not start the fallback.
    assert seen["calls"] == 1
    assert result.cost.attempts_started == 1
    assert result.cost.attempts_usage_complete == 1
    assert result.cost.output_tokens == 3


def test_a_plain_providers_billed_failure_is_capped_before_the_merge(tmp_path):
    """The chain accepts any ``ReaderProvider``, so a claim may never have been bounded.

    A plain provider's failed attempt claiming one token over the per-call cap, plus a
    winner claiming one, stayed under the two-attempt aggregate ceiling.
    """
    from context_shunt.provider import FallbackChainProvider

    class Failing:
        target = _PLAIN

        def complete(self, **_kwargs):
            raise _billed_error(1, L.max_output_tokens_per_call + 1)

    class Winner:
        target = _PLAIN

        def complete(self, **_kwargs):
            return _plain_response(1)

    reader, request = _fixture(tmp_path, FallbackChainProvider(Failing(), [Winner()]))
    result = reader.answer("sess", request)

    # The call still failed the way it failed, so the chain still advanced and answered.
    assert result.envelope["code"] == "ANSWERED"
    assert result.cost.method is not TokenMethod.EXACT
    assert result.cost.output_tokens < L.max_output_tokens_per_call


def test_a_plain_provider_that_wins_the_fallback_is_capped(tmp_path):
    """A winner is a constituent too, and an aggregate bound cannot vouch for it.

    Refusing it must not refuse what the chain already knew. The rejection used to raise a
    bare ``BAD_USAGE`` that carried none of the chain's state, so a two-call schedule whose
    first attempt was billed 5/3 was published as one attempt, zero usage-complete attempts
    and zero output tokens - the invalid claim was thrown out and the *earlier* attempt's
    real spend went with it. Only the unusable claim may be excluded.
    """
    from context_shunt.provider import FallbackChainProvider

    seen = {"calls": 0}

    class BilledFailure:
        target = _PLAIN

        def complete(self, **_kwargs):
            seen["calls"] += 1
            raise _billed_error(5, 3)

    class OverCap:
        target = _PLAIN

        def complete(self, **_kwargs):
            seen["calls"] += 1
            return _plain_response(L.max_output_tokens_per_call + 1)

    reader, request = _fixture(tmp_path, FallbackChainProvider(BilledFailure(), [OverCap()]))
    result = reader.answer("sess", request)

    assert result.envelope["code"] != "ANSWERED"
    assert result.envelope["answer"] == ""
    # Both calls were made and both were billed, whatever became of the second's claim.
    assert seen["calls"] == 2
    assert result.cost.attempts_started == 2
    # Exactly one of the two reported usage that could be believed.
    assert result.cost.attempts_usage_complete == 1
    # The first attempt's three billed output tokens survive the second's refusal.
    assert result.cost.output_tokens == 3


def test_a_host_bridge_that_wins_the_fallback_is_capped_the_same_way(tmp_path):
    """The parity case: the same schedule where the winner is a real bridge.

    A bridge rejects its own over-cap reply inside ``complete``, so the refusal reaches the
    chain as a caught failure rather than a returned response. Both routes have to publish
    the same accounting, or the boundary a claim happens to cross would decide what the
    session was charged.
    """
    from context_shunt.provider import FallbackChainProvider, HostBridgeProvider

    seen = {"calls": 0}

    def failing(**_kwargs):
        seen["calls"] += 1
        raise _billed_error(5, 3)

    def over_cap(**_kwargs):
        seen["calls"] += 1
        return {
            "text": _ANSWER_TEXT,
            "input_tokens": 1,
            "output_tokens": L.max_output_tokens_per_call + 1,
            "usage_exact": True,
        }

    chain = FallbackChainProvider(
        HostBridgeProvider(failing, L, provider="openai"),
        [HostBridgeProvider(over_cap, L, "fallback", provider="openai")],
    )
    reader, request = _fixture(tmp_path, chain)
    result = reader.answer("sess", request)

    assert result.envelope["code"] != "ANSWERED"
    assert result.envelope["answer"] == ""
    assert seen["calls"] == 2
    assert result.cost.attempts_started == 2
    assert result.cost.attempts_usage_complete == 1
    assert result.cost.output_tokens == 3


def test_a_legal_sum_above_the_single_call_ceiling_is_accepted(tmp_path):
    """The other direction, and the reason the aggregate bound scales with attempts.

    Two attempts of 1,536 output tokens are each legal and total 3,072 against a 2,048
    per-call ceiling. Re-applying the single-call ceiling to the sum refused a valid
    fallback outright.
    """
    from context_shunt.provider import FallbackChainProvider, HostBridgeProvider

    half = int(L.max_output_tokens_per_call * 0.75)

    def failing(**_kwargs):
        raise _billed_error(1, half)

    def winner(**_kwargs):
        return {
            "text": _ANSWER_TEXT,
            "input_tokens": 1,
            "output_tokens": half,
            "usage_exact": True,
        }

    chain = FallbackChainProvider(
        HostBridgeProvider(failing, L, provider="openai"),
        [HostBridgeProvider(winner, L, "fallback", provider="openai")],
    )
    reader, request = _fixture(tmp_path, chain)
    result = reader.answer("sess", request)

    assert result.envelope["code"] == "ANSWERED"
    assert result.cost.method is TokenMethod.EXACT
    assert result.cost.output_tokens == half * 2
    assert result.cost.output_tokens > L.max_output_tokens_per_call


# -- a refused usage claim does not erase measured bytes --------------------------------


class _BadUsageProvider:
    """Returns a real completion alongside a usage claim no call could have made."""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    @property
    def target(self) -> ProviderTarget:
        return ProviderTarget()

    def complete(self, *, system, user, max_output_tokens, timeout_ms):
        self.calls += 1
        return ModelResponse(
            text=self.text,
            requested=ModelIdentity(model="gpt-5.6-luna"),
            resolved=ModelIdentity(model="gpt-5.6-luna"),
            # Above every ceiling, so `_validate_model_response` refuses the claim.
            usage=Usage(input_tokens=10, output_tokens=10**9, method=TokenMethod.EXACT),
        )


def test_invalid_usage_metadata_does_not_erase_the_bytes_we_measured(tmp_path):
    """The release blocker: a refused usage claim took real completion bytes with it.

    The response reached the provider, transmitted the prompt and came back carrying
    completion bytes this core can measure itself. Rejecting the whole response for a
    malformed usage claim reported ``output_tokens: 0`` for a call that had produced
    hundreds of bytes - understating spend, which is the one direction this accounting
    must never err in. The claim is still refused; the measurement is kept.
    """
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"alpha = 1\nbeta = 2\n"))
    text = answer_json(
        "Alpha is one [c1].",
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "alpha = 1"}],
    )
    provider = _BadUsageProvider(text)
    result = Reader(registry, provider).answer(
        "sess",
        {
            "schema_version": "1.0",
            "request_id": "req_usage",
            "operation": "read",
            "question": "What is alpha?",
            "sources": [
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": {"kind": "all"},
                }
            ],
            "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
        },
    )
    assert provider.calls == 1
    cost = result.cost
    assert cost.attempts_started == 1
    # The provider's own numbers are refused, so no attempt counts as usage-complete and
    # the method says the totals are this core's estimate.
    assert cost.attempts_usage_complete == 0
    assert cost.method is TokenMethod.BYTES_DIV_4
    # And the completion bytes survive: the estimate is the text we actually received.
    assert cost.output_tokens == estimate_tokens(len(text.encode("utf-8")))
    assert cost.output_tokens > 0
    assert cost.input_tokens > 0
    # The refused reply is still refused - nothing from it is published.
    assert result.envelope["code"] in ("NO_MATCH", "INVALID_MODEL_OUTPUT")
    assert result.envelope.get("answer", "") == ""


def test_a_valid_usage_claim_is_still_reported_exactly(tmp_path):
    """The control: a legal claim is used, and the record says so."""
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"alpha = 1\n"))
    reply = answer_json(
        "Alpha is one [c1].",
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "alpha = 1"}],
    )
    result = Reader(registry, FakeLuna(replies=[reply])).answer(
        "sess",
        {
            "schema_version": "1.0",
            "request_id": "req_usage_ok",
            "operation": "read",
            "question": "What is alpha?",
            "sources": [
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": {"kind": "all"},
                }
            ],
            "budgets": {"max_chunks": 8, "max_answer_bytes": 8192, "deadline_ms": 60000},
        },
    )
    assert result.cost.method is TokenMethod.EXACT
    assert result.cost.attempts_usage_complete == 1


# -- nested chains, bridge usage bytes, and per-call identity ----------------------------


def test_a_nested_chain_is_flattened_into_one_ordered_candidate_list():
    """The release blocker: a chain inside a chain broke every per-constituent invariant.

    Every rule `FallbackChainProvider` enforces is written per entry - one entry, one
    physical call. A nested chain made three physical calls behind one entry, so the outer
    `attempts` counted one, the shared input-token debit never charged the inner
    candidates, and the winner's aggregate reply was judged against a *single*-call
    ceiling. Flattening restores all three without changing the candidate order.
    """
    from context_shunt.provider import FallbackChainProvider, HostBridgeProvider

    def bridge(_name):
        def call(**_kwargs):
            raise ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)

        return call

    a = HostBridgeProvider(bridge("a"), L, "a", provider="openai")
    b = HostBridgeProvider(bridge("b"), L, "b", provider="openai")
    c = HostBridgeProvider(bridge("c"), L, "c", provider="openai")
    nested = FallbackChainProvider(a, [FallbackChainProvider(b, [c], L)], L)
    assert nested._chain == [a, b, c]
    # And recursively, however deep it was built.
    deeper = FallbackChainProvider(
        FallbackChainProvider(a, [FallbackChainProvider(b, [c], L)], L), [], L
    )
    assert deeper._chain == [a, b, c]
    # The head's target is still the first candidate's, so `target` is unchanged.
    assert nested.target.model == "a"


def test_a_nested_chain_built_with_other_limits_is_refused_not_widened():
    """Flattening makes the outer limits govern every candidate, so a mismatch is fatal.

    Silently widening a ceiling someone set deliberately is the one outcome worse than
    refusing the arrangement, so this fails closed at construction rather than at the
    first call that would have exceeded the inner cap.
    """
    from context_shunt.provider import FallbackChainProvider, HostBridgeProvider

    def call(**_kwargs):
        raise ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)

    inner_limits = L.narrow(max_output_tokens_per_call=64)
    inner = FallbackChainProvider(
        HostBridgeProvider(call, inner_limits, "b", provider="openai"), [], inner_limits
    )
    with pytest.raises(ShuntError) as caught:
        FallbackChainProvider(HostBridgeProvider(call, L, "a", provider="openai"), [inner], L)
    assert caught.value.detail == "NESTED_CHAIN_LIMITS_DIFFER"


def test_a_flattened_chain_never_transmits_more_prompts_than_the_budget_allows(tmp_path):
    """Physical calls stay inside `max_request_input_tokens`, nesting or not.

    Nesting used to hide candidates from the shared debit entirely: the inner chain
    received no `input_budget`, so its extra calls transmitted the whole prompt again
    against nobody's allowance. Flattened, every candidate past the first is debited, and
    the one the budget cannot afford is never started.
    """
    from context_shunt.provider import FallbackChainProvider, HostBridgeProvider

    seen = {"calls": 0}

    def unavailable(**_kwargs):
        seen["calls"] += 1
        raise ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"mode = fast\n"))
    question = "What is the mode?"
    request = _read_request(entry, question)
    # A budget for exactly two prompts, and no outer retry to confuse the count with.
    limits = L.narrow(
        max_request_input_tokens=_prompt_tokens(entry, question) * 2,
        max_transient_retries=0,
    )
    inner = FallbackChainProvider(
        HostBridgeProvider(unavailable, limits, "b", provider="openai"),
        [HostBridgeProvider(unavailable, limits, "c", provider="openai")],
        limits,
    )
    chain = FallbackChainProvider(
        HostBridgeProvider(unavailable, limits, "a", provider="openai"), [inner], limits
    )
    result = Reader(registry, chain, limits=limits).answer("sess", request)
    # Three candidates, a budget for two prompts: the third is never started. Before
    # flattening, the inner chain saw no budget at all and made both of its calls.
    assert seen["calls"] == 2
    assert result.cost.attempts_started == 2
    # Nothing was answered, and the envelope says the chunk was not covered rather than
    # reporting an answer the budget stopped it from producing.
    assert result.envelope.get("answer", "") == ""
    assert result.envelope["coverage"]["complete"] is False


def _prompt_tokens(entry, question: str, limits=L) -> int:
    """What the reader debits for one physical call of this chunk's prompt."""
    from context_shunt.chunking import estimate_tokens as chunk_tokens
    from context_shunt.chunking import plan
    from context_shunt.provider import READER_SYSTEM_PROMPT, build_user_message

    chunk = plan(
        [(entry.source_id, entry.snapshot, {"kind": "all"})],
        max_chunks=8,
        limits=limits,
        question=question,
    ).chunks[0]
    return chunk_tokens(READER_SYSTEM_PROMPT, limits) + chunk_tokens(
        build_user_message(question, chunk.text, chunk.locator), limits
    )


class _BadBridgeUsage:
    """A host bridge whose reply is intact and whose usage claim is not."""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def __call__(self, **_kwargs):
        self.calls += 1
        # Negative is not a count. `_usage_value` refuses it before any response exists,
        # which is the path that used to lose the reply's measured bytes entirely.
        return {"text": self.text, "usage_exact": True, "input_tokens": 5, "output_tokens": -1}


def test_a_bridge_usage_claim_this_core_refuses_still_keeps_the_reply_bytes(tmp_path):
    """The release blocker: `_unpack` raised before the reply could be measured.

    `_usage_value` rejects a malformed count *while the usage object is being built*, so
    the `ModelResponse` was never constructed and the error carried nothing. The call had
    reached the provider, transmitted the prompt and returned hundreds of bytes of text -
    all of which was published as `output_tokens: 0`. The claim is still refused whole; the
    measurement this core made itself survives it.
    """
    from context_shunt.provider import HostBridgeProvider

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"alpha = 1\n"))
    text = answer_json(
        "Alpha is one [c1].",
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "alpha = 1"}],
    )
    bridge = _BadBridgeUsage(text)
    result = Reader(registry, HostBridgeProvider(bridge, L, provider="openai")).answer(
        "sess", _read_request(entry, "What is alpha?")
    )
    assert bridge.calls >= 1
    cost = result.cost
    assert cost.attempts_started >= 1
    # None of the host's numbers are believed.
    assert cost.attempts_usage_complete == 0
    assert cost.method is TokenMethod.BYTES_DIV_4
    # The bytes are: one refused claim must not read as a call that produced nothing.
    assert cost.output_tokens >= estimate_tokens(len(text.encode("utf-8")))
    assert cost.output_tokens > 0


class _Unavailable:
    """A candidate that always fails on availability, reporting no identity."""

    def __init__(self, name: str):
        self.name = name

    @property
    def target(self) -> ProviderTarget:
        return ProviderTarget(model=self.name, provider="openai")

    def complete(self, **_kwargs):
        raise ShuntError("MODEL_ERROR", "UNAVAILABLE", retryable=True)


def test_identity_is_recorded_per_physical_call_not_multiplied_by_the_attempt_count(tmp_path):
    """The release blocker: one identity times `attempts_started` certified every call.

    A run that fell back twice before answering made three physical calls, and only the
    third reported which model ran. Multiplying the winner's identity by the attempt count
    - the only arithmetic available without per-call records - certified all three, which
    is precisely the claim no evidence supports. There is now one record per call, and the
    two that observed nothing say so.
    """
    from context_shunt.provider import FallbackChainProvider

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"mode = fast\n"))
    reply = answer_json(
        "The mode is fast [c1].",
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "mode = fast"}],
    )
    chain = FallbackChainProvider(
        _Unavailable("down-a"), [_Unavailable("down-b"), FakeLuna(replies=[reply])], L
    )
    result = Reader(registry, chain).answer("sess", _read_request(entry, "What is the mode?"))
    assert result.envelope["code"] == "ANSWERED"
    records = result.provenance.call_identities
    # One record per physical call, which is what makes a count over them a call count.
    assert len(records) == result.cost.attempts_started == 3
    observed = [r.observed_model for r in records]
    # Exactly one call observed a model. The other two are unobserved, not the winner's.
    assert observed.count(L.reader_model) == 1
    assert observed.count("") == 2
    statuses = [r.attribution.value for r in records]
    assert statuses.count("unknown") == 2
    assert "actual" in statuses or "resolved" in statuses


def test_a_single_call_still_records_exactly_one_identity(tmp_path):
    """The ordinary case: one call, one record, and it describes that call."""
    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"mode = fast\n"))
    reply = answer_json(
        "The mode is fast [c1].",
        [{"id": "c1", "line_start": 1, "line_end": 1, "quote": "mode = fast"}],
    )
    result = Reader(registry, FakeLuna(replies=[reply])).answer(
        "sess", _read_request(entry, "What is the mode?")
    )
    records = result.provenance.call_identities
    assert len(records) == result.cost.attempts_started == 1
    assert records[0].observed_model == L.reader_model
    assert records[0].attribution.value in ("actual", "resolved")


def test_a_failed_request_still_accounts_for_every_call_it_made(tmp_path):
    """A chain that answered nothing made calls, and each is one record."""
    from context_shunt.provider import FallbackChainProvider

    registry = make_registry(tmp_path, session_id="sess")
    entry = registry.register("sess", snapshot_bytes(b"mode = fast\n"))
    chain = FallbackChainProvider(_Unavailable("down-a"), [_Unavailable("down-b")], L)
    result = Reader(registry, chain).answer("sess", _read_request(entry, "What is the mode?"))
    records = result.provenance.call_identities
    assert len(records) == result.cost.attempts_started
    assert result.cost.attempts_started >= 2
    assert all(r.observed_model == "" for r in records)
