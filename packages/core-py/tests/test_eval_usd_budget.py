"""Dollar-budget regressions for the opt-in real-reader evaluation."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest


def _pricing(*, input_rate: str = "1", output_rate: str = "6"):
    from bridges._usd_budget import RoutePricing

    return RoutePricing.from_host_identity(
        {
            "pricing": {
                "route": "sub2api-openai/gpt-5.6-luna",
                "currency": "USD",
                "unit": "per_million_tokens",
                "source": "openclaw.resolveModelCostConfig",
                "fingerprint": "a" * 64,
                "rates": {
                    "input": input_rate,
                    "output": output_rate,
                    "cacheRead": "0.1",
                    "cacheWrite": "0",
                },
            }
        },
        expected_route="sub2api-openai/gpt-5.6-luna",
    )


def test_unknown_or_zero_pricing_fails_closed():
    from bridges._usd_budget import BudgetError, RoutePricing

    with pytest.raises(BudgetError, match="pricing"):
        RoutePricing.from_host_identity({}, expected_route="p/m")
    with pytest.raises(BudgetError, match="positive"):
        RoutePricing.from_host_identity(
            {
                "pricing": {
                    "route": "p/m",
                    "currency": "USD",
                    "unit": "per_million_tokens",
                    "source": "openclaw.resolveModelCostConfig",
                    "fingerprint": "b" * 64,
                    "rates": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                    },
                }
            },
            expected_route="p/m",
        )


def test_retry_reserves_again_and_cannot_cross_two_dollars(tmp_path):
    from bridges._usd_budget import BudgetError, UsdBudgetLedger

    ledger = UsdBudgetLedger(tmp_path / "budget.sqlite3", _pricing(input_rate="500000"))
    first = ledger.reserve(input_token_upper_bound=1, max_output_tokens=1)
    assert first.reserved_usd > Decimal("1")
    with pytest.raises(BudgetError, match="USD 2"):
        ledger.reserve(input_token_upper_bound=1, max_output_tokens=1)
    summary = ledger.summary()
    assert summary["reservations"] == 1
    assert Decimal(summary["reserved_usd"]) <= Decimal("2")


def test_concurrent_reservations_are_serialized_under_the_cap(tmp_path):
    from bridges._usd_budget import BudgetError, UsdBudgetLedger

    path = tmp_path / "budget.sqlite3"
    pricing = _pricing(input_rate="200000")

    def reserve_once() -> bool:
        try:
            UsdBudgetLedger(path, pricing).reserve(
                input_token_upper_bound=1, max_output_tokens=1
            )
            return True
        except BudgetError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda _index: reserve_once(), range(12)))
    summary = UsdBudgetLedger(path, pricing).summary()
    assert 0 < sum(accepted) < len(accepted)
    assert summary["reservations"] == sum(accepted)
    assert Decimal(summary["reserved_usd"]) <= Decimal("2")


def test_unsettled_reservation_survives_resume_and_is_not_refunded(tmp_path):
    from bridges._usd_budget import UsdBudgetLedger

    path = tmp_path / "budget.sqlite3"
    first = UsdBudgetLedger(path, _pricing())
    reservation = first.reserve(input_token_upper_bound=1000, max_output_tokens=20)
    resumed = UsdBudgetLedger(path, _pricing())
    summary = resumed.summary()
    assert summary["reservations"] == 1
    assert summary["statuses"] == {"reserved": 1}
    assert summary["reserved_usd"] == format(reservation.reserved_usd, "f")


def test_pricing_change_on_an_existing_ledger_fails_closed(tmp_path):
    from bridges._usd_budget import BudgetError, UsdBudgetLedger

    path = tmp_path / "budget.sqlite3"
    UsdBudgetLedger(path, _pricing()).reserve(
        input_token_upper_bound=100, max_output_tokens=10
    )
    with pytest.raises(BudgetError, match="pricing"):
        UsdBudgetLedger(path, _pricing(output_rate="7"))


@pytest.mark.parametrize("input_bound, output_bound", [(0, 1), (1, 0), (-1, 1)])
def test_missing_or_invalid_usage_bounds_fail_before_reservation(
    tmp_path, input_bound, output_bound
):
    from bridges._usd_budget import BudgetError, UsdBudgetLedger

    ledger = UsdBudgetLedger(tmp_path / "budget.sqlite3", _pricing())
    with pytest.raises(BudgetError, match="bound"):
        ledger.reserve(
            input_token_upper_bound=input_bound,
            max_output_tokens=output_bound,
        )
    assert ledger.summary()["reservations"] == 0


def test_source_manifest_excludes_only_named_generated_evidence(tmp_path):
    from evidence_binding import non_evidence_dirty_paths, source_manifest_sha256

    (tmp_path / "evals/intent-reader-audit").mkdir(parents=True)
    source = tmp_path / "runtime.py"
    evidence = tmp_path / "evals/intent-reader-audit/real-luna-latest.json"
    source.write_text("BOUND = True\n")
    evidence.write_text("{\"old\": true}\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Context Shunt Test",
            "-c",
            "user.email=context-shunt@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    before = source_manifest_sha256(tmp_path)

    evidence.write_text("{\"new\": true}\n")
    assert source_manifest_sha256(tmp_path) == before
    assert non_evidence_dirty_paths(tmp_path) == []

    source.write_text("BOUND = False\n")
    assert source_manifest_sha256(tmp_path) != before
    assert non_evidence_dirty_paths(tmp_path) == ["runtime.py"]
