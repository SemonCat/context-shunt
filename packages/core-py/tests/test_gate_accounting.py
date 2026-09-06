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
from context_shunt.limits import BASELINE_ESTIMATE_METHOD, DEFAULT_LIMITS, EMITTED_SCHEMA_VERSION
from context_shunt.metrics import ALLOWED_LABEL_KEYS, InMemoryMetrics, MetricsError
from context_shunt.provenance import TokenMethod
from context_shunt.session import ShuntSession
from tests.support import FakeLuna, answer_json, make_capability, make_config

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
