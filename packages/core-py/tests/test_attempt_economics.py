"""Synthetic runtime fixtures only; no production content or identity strings."""

import json
from dataclasses import replace
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
from context_shunt.attempts import (
    MAX_ATTEMPTS,
    MAX_OPERATIONS,
    identity_key,
    observations,
)
from context_shunt.economics import attempt_economics_report as report
from context_shunt.economics import configured_rates, price_observation
from context_shunt.errors import ShuntError
from context_shunt.provenance import Attribution, ModelIdentity, TokenMethod, Usage
from context_shunt.provider import CallIdentity, FallbackChainProvider, HostBridgeProvider
from context_shunt.store import ScopeIdentity, SnapshotStore
from tests.test_gate_accounting import _big_source, _read_request, _session, _stats

pytestmark = pytest.mark.gate_accounting
IDENTITY = ModelIdentity("fixture-provider", "fixture-model")


def rates(**overrides):
    return configured_rates(
        [
            dict(
                provider=IDENTITY.provider,
                model=IDENTITY.model,
                source="synthetic-test",
                as_of="2026-09-27",
                input_tokens="2",
                output_tokens="10",
                cache_tokens="0.2",
                cache_write_5m_tokens="2.5",
                cache_write_1h_tokens="4",
                **overrides,
            )
        ]
    )


def call(**overrides):
    usage = dict(
        input_tokens=100,
        output_tokens=20,
        cache_tokens=10,
        cache_write_5m_tokens=5,
        cache_write_1h_tokens=2,
        input_includes_cache=True,
        method=TokenMethod.EXACT,
    )
    usage.update(overrides)
    return CallIdentity(
        requested=IDENTITY,
        resolved=IDENTITY,
        reported=IDENTITY,
        attribution=Attribution.ACTUAL,
        usage=Usage(**usage),
    )


def test_prices_all_cache_buckets_with_runtime_inclusion():
    record = observations((call(),), 1)[0]
    amount, key = price_observation(record, rates())
    assert amount == Decimal("0.0003885")  # 83*2 + 20*10 + 10*.2 + 5*2.5 + 2*4
    assert key == identity_key(IDENTITY)
    disjoint = observations((call(input_includes_cache=False),), 1)[0]
    assert price_observation(disjoint, rates())[0] == Decimal("0.0004225")


@pytest.mark.parametrize(
    "field",
    [
        "input_tokens",
        "output_tokens",
        "cache_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
    ],
)
def test_missing_usage_is_unknown_even_without_rate(field):
    obs = observations((call(**{field: None}),), 1)[0]
    assert price_observation(obs, rates()) == (None, "usage_unknown")


def test_unknown_semantics_identity_and_rates_are_never_zero():
    obs = observations((call(input_includes_cache=None),), 1)[0]
    assert price_observation(obs, rates()) == (None, "cache_inclusion_unknown")
    obs = observations((replace(call(), attribution=Attribution.UNVERIFIED),), 1)[0]
    assert price_observation(obs, rates()) == (None, "identity_unknown")
    assert price_observation(observations((call(),), 1)[0], {}) == (None, "rate_unknown")
    assert price_observation(observations((call(input_tokens=1),), 1)[0], rates()) == (
        None,
        "cache_exceeds_input",
    )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1e-999999", True, "1" * 100])
def test_rates_reject_nonfinite_negative_and_unbounded(value):
    raw = dict(provider="p", model="m", source="fixture", as_of="2026-09-27", input_tokens=value)
    with pytest.raises(ValueError, match="BAD_ECONOMICS_RATES"):
        configured_rates([raw])


def test_observation_security_and_bounds():
    private = "PRIVATE BODY\nnot an identity"
    record = replace(call(), requested=ModelIdentity(private, private))
    encoded = json.dumps(observations((record,) * (MAX_ATTEMPTS + 1), MAX_ATTEMPTS + 1))
    assert private not in encoded and "PRIVATE" not in encoded
    assert len(json.loads(encoded)) == MAX_ATTEMPTS
    assert len(encoded) < 262144
    for value in (-1, True, 2**64):
        assert observations((call(input_tokens=value),), 1)[0]["usage"]["input_tokens"] is None


def test_runtime_chain_keeps_failure_and_winner_usage_separate(tmp_path):
    failure = ShuntError("MODEL_ERROR", "PROVIDER_CALL_FAILED", retryable=True)
    failure.billed_usage = Usage(input_tokens=17, output_tokens=3, method=TokenMethod.EXACT)

    def fail(**kwargs):
        raise failure

    def succeed(**kwargs):
        return dict(
            text='{"claims":[],"citations":[]}',
            resolved_provider=IDENTITY.provider,
            resolved_model=IDENTITY.model,
            input_tokens=100,
            output_tokens=20,
            cache_tokens=10,
            cache_write_5m_tokens=5,
            cache_write_1h_tokens=2,
            input_includes_cache=True,
            usage_exact=True,
        )

    first = HostBridgeProvider(fail, model="unavailable-fixture", provider="fixture-provider")
    second = HostBridgeProvider(succeed, model=IDENTITY.model, provider=IDENTITY.provider)
    session = _session(tmp_path, FallbackChainProvider(first, [second]))
    session.config = replace(session.config, economics_rates=rates())
    entry = _big_source(tmp_path, session)
    envelope = session.read(_read_request(entry))
    rows = session._store.attempt_observations(session._identity, [envelope["accounting_id"]])
    attempts = rows[envelope["accounting_id"]]
    assert len(attempts) >= 2 and len(attempts) % 2 == 0
    for failed, winner in zip(attempts[::2], attempts[1::2], strict=True):
        assert failed["usage"]["input_tokens"] == 17
        assert winner["usage"]["input_tokens"] == 100
        assert failed["resolved"] is None
        assert winner["resolved"] == identity_key(IDENTITY)
    stats = _stats(session)
    assert stats["code"] == "STATS"
    econ = stats["stats"]["economics"]
    assert econ["attempts_priced"] == len(attempts) // 2
    assert econ["reader_cost_usd_total"] is None
    assert econ["net_savings_usd"] is None
    assert econ["rates"][0]["source"] == "synthetic-test"
    assert "fixture-model" not in json.dumps(rows)
    session._store._conn.close()
    session._store._conn = None
    assert (
        session._store.attempt_observations(session._identity, [envelope["accounting_id"]]) == rows
    )


def test_migration_legacy_scoping_retention_and_zero_calls(tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    scope = ScopeIdentity("host", "profile", "principal", "session", 1)
    foreign = ScopeIdentity("host", "profile", "principal", "other-session", 1)

    def record(i, attempts=1):
        return compose(
            operation_id=f"op_{i:032x}",
            kind=OperationKind.READ,
            status="ok",
            code="ANSWERED",
            baseline=Baseline.none(),
            baseline_credited=False,
            reader=ReaderCost(attempts_started=attempts),
            egress=Egress(DeliveryBoundary.ENVELOPE, 0),
        )

    old = record(0)
    store.record_operation(scope, old)
    store._conn.execute("DROP TABLE reader_attempt_observations")
    store._conn.close()
    store._conn = None
    assert store.attempt_observations(scope, [old.operation_id]) == {}
    assert report([old], {}, rates())["unknown_reasons"] == {"observations_unavailable": 1}
    for i in range(1, MAX_OPERATIONS + 2):
        store.record_operation(scope, record(i), call_identities=(call(),))
    assert (
        store._conn.execute("SELECT count(*) FROM reader_attempt_observations").fetchone()[0]
        == MAX_OPERATIONS
    )
    assert store.attempt_observations(scope, [record(1).operation_id]) == {}
    assert store.attempt_observations(foreign, [record(MAX_OPERATIONS + 1).operation_id]) == {}
    assert report([record(9999, 0)], {}, {})["reader_cost_usd_total"] == "0"


def test_configured_rate_loader_rejects_request_like_content(tmp_path):
    from context_shunt.config import load

    (tmp_path / "ws").mkdir()
    raw = {
        "workspace_roots": [str(tmp_path / "ws")],
        "economics_rates": [
            dict(provider="p", model="m", source="fixture", as_of="2026-09-27", input_tokens="0")
        ],
    }
    assert load(raw, default_spill_dir=tmp_path / "cache").economics_rates
    raw["economics_rates"][0]["body"] = "SYNTHETIC-PRIVATE-BODY"
    with pytest.raises(ShuntError, match="BAD_CONFIGURATION"):
        load(raw, default_spill_dir=tmp_path / "cache")


def test_adapter_logging_is_content_free_and_failure_isolated():
    from tests.test_gate_capability import _load_adapter

    adapter = _load_adapter()
    logged = []

    class Logger:
        def info(self, *args):
            logged.append(args)

    sink = adapter._ReaderMetrics(Logger())
    sink.event(dict(duration_ms=10, status="ok", code="ANSWERED", body="SYNTHETIC-PRIVATE-BODY"))
    sink.event(dict(duration_ms=float("nan"), status="PRIVATE", code="PRIVATE"))
    sink.event(dict(duration_ms=11, status="PRIVATE", code="PRIVATE"))
    assert "PRIVATE" not in json.dumps(logged)

    class Broken:
        def info(self, *args):
            raise RuntimeError("SYNTHETIC-PRIVATE-BODY")

    adapter._ReaderMetrics(Broken()).event(dict(duration_ms=10))


@pytest.mark.parametrize("text", [None, "x" * 20000])
def test_rejected_response_keeps_observed_identity_and_billing(text):
    bridge = HostBridgeProvider(
        lambda **kw: dict(
            text=text,
            resolved_provider=IDENTITY.provider,
            resolved_model=IDENTITY.model,
            input_tokens=17,
            output_tokens=3,
            usage_exact=True,
        ),
        model=IDENTITY.model,
        provider=IDENTITY.provider,
    )
    with pytest.raises(ShuntError) as caught:
        bridge.complete(system="fixture", user="fixture", max_output_tokens=100, timeout_ms=1000)
    record = caught.value.call_identities[0]
    assert record.resolved == IDENTITY
    assert record.usage.input_tokens == 17
    assert record.usage.output_tokens == 3


def test_non_enum_attribution_never_persists_arbitrary_content():
    record = replace(call(), attribution="SYNTHETIC-PRIVATE-BODY")
    assert observations((record,), 1)[0]["attribution"] == "unknown"


def test_ttl_less_cache_writes_are_persisted_but_never_priced_into_a_bucket():
    record = observations((call(cache_write_unclassified_tokens=9),), 1)[0]
    assert record["usage"]["cache_write_unclassified_tokens"] == 9
    assert price_observation(record, rates()) == (None, "cache_write_ttl_unknown")
    from types import SimpleNamespace

    operation = SimpleNamespace(operation_id="op_synthetic", attempts_started=1)
    summary = report([operation], {"op_synthetic": [record]}, rates())
    assert summary["unknown_reasons"] == {"cache_write_ttl_unknown": 1}
    assert summary["reader_cost_usd_total"] is None


def test_a_pre_upgrade_observation_without_the_unclassified_field_still_prices():
    record = observations((call(),), 1)[0]
    record["usage"].pop("cache_write_unclassified_tokens")
    assert price_observation(record, rates())[0] == Decimal("0.0003885")


def test_a_malformed_unclassified_write_makes_the_whole_claim_unknown():
    from context_shunt.provider import normalize_usage

    usage, well_formed = normalize_usage(Usage(input_tokens=1, cache_write_unclassified_tokens=-1))
    assert not well_formed and usage == Usage()
