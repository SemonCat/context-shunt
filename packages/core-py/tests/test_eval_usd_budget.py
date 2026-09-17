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


def test_retry_reserves_again_and_cannot_cross_ten_dollars(tmp_path):
    from bridges._usd_budget import BudgetError, UsdBudgetLedger

    ledger = UsdBudgetLedger(tmp_path / "budget.sqlite3", _pricing(input_rate="3000000"))
    first = ledger.reserve(input_token_upper_bound=1, max_output_tokens=1)
    assert first.reserved_usd > Decimal("5")
    with pytest.raises(BudgetError, match="USD 10"):
        ledger.reserve(input_token_upper_bound=1, max_output_tokens=1)
    summary = ledger.summary()
    assert summary["reservations"] == 1
    assert Decimal(summary["reserved_usd"]) <= Decimal("10")


def test_concurrent_reservations_are_serialized_under_the_cap(tmp_path):
    from bridges._usd_budget import BudgetError, UsdBudgetLedger

    path = tmp_path / "budget.sqlite3"
    pricing = _pricing(input_rate="700000")

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
    assert Decimal(summary["reserved_usd"]) <= Decimal("10")


def test_approved_limit_increase_is_atomic_audited_and_preserves_rows(tmp_path):
    import sqlite3

    from bridges._usd_budget import (
        LIMIT_INCREASE_AUTHORIZATION,
        UsdBudgetLedger,
    )

    path = tmp_path / "budget.sqlite3"
    ledger = UsdBudgetLedger(path, _pricing())
    reservation = ledger.reserve(input_token_upper_bound=10_000, max_output_tokens=512)
    ledger.record_result(
        reservation,
        status="completed",
        reported_input_tokens=1_000,
        reported_output_tokens=20,
    )
    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT * FROM reservations ORDER BY reservation_id"
        ).fetchall()
        connection.execute(
            "UPDATE metadata SET value='2500000000' WHERE key='limit_nano_usd'"
        )
        connection.execute("DELETE FROM limit_history")
        connection.execute(
            """INSERT INTO limit_history(
                   changed_unix_ms, from_nano_usd, to_nano_usd, authorization
               ) VALUES (?, ?, ?, ?)""",
            (
                1,
                2_000_000_000,
                2_500_000_000,
                "mattermost:3feushz5if84uf8irw7tkx1k9r:2026-09-17",
            ),
        )

    summary = UsdBudgetLedger(path, _pricing()).summary()
    with sqlite3.connect(path) as connection:
        after = connection.execute(
            "SELECT * FROM reservations ORDER BY reservation_id"
        ).fetchall()
    assert after == before
    assert summary["limit_usd"] == "10"
    assert summary["accounted_usd"] == "0.00212"
    assert summary["limit_history"] == [
        {
            "changed_unix_ms": 1,
            "from_usd": "2",
            "to_usd": "2.5",
            "authorization": "mattermost:3feushz5if84uf8irw7tkx1k9r:2026-09-17",
        },
        {
            "changed_unix_ms": summary["limit_history"][1]["changed_unix_ms"],
            "from_usd": "2.5",
            "to_usd": "10",
            "authorization": LIMIT_INCREASE_AUTHORIZATION,
        }
    ]


def test_unapproved_limit_change_fails_closed(tmp_path):
    import sqlite3

    from bridges._usd_budget import BudgetError, UsdBudgetLedger

    path = tmp_path / "budget.sqlite3"
    UsdBudgetLedger(path, _pricing())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE metadata SET value='1500000000' WHERE key='limit_nano_usd'"
        )
    with pytest.raises(BudgetError, match="budget policy"):
        UsdBudgetLedger(path, _pricing())


def test_unsettled_reservation_survives_resume_and_keeps_its_full_hold(tmp_path):
    from bridges._usd_budget import UsdBudgetLedger

    path = tmp_path / "budget.sqlite3"
    first = UsdBudgetLedger(path, _pricing())
    reservation = first.reserve(input_token_upper_bound=1000, max_output_tokens=20)
    resumed = UsdBudgetLedger(path, _pricing())
    summary = resumed.summary()
    assert summary["reservations"] == 1
    assert summary["statuses"] == {"reserved": 1}
    assert summary["reserved_usd"] == format(reservation.reserved_usd, "f")


def test_completed_exact_usage_settles_to_a_conservative_upper_cost(tmp_path):
    from bridges._usd_budget import UsdBudgetLedger

    ledger = UsdBudgetLedger(tmp_path / "budget.sqlite3", _pricing())
    reservation = ledger.reserve(input_token_upper_bound=10_000, max_output_tokens=512)
    held = reservation.reserved_usd
    ledger.record_result(
        reservation,
        status="completed",
        reported_input_tokens=1_000,
        reported_output_tokens=20,
    )
    summary = ledger.summary()
    # Prompt usage is still charged at the worst verified bucket (2x input), and output
    # at the worst verified output rate: 1000*2 + 20*6 micro-dollars.
    assert summary["accounted_usd"] == "0.00212"
    assert Decimal(summary["accounted_usd"]) < held
    assert summary["active_or_unknown_reserved_usd"] == "0"
    assert summary["settled_usage_upper_usd"] == "0.00212"


def test_unknown_usage_never_releases_the_reservation(tmp_path):
    from bridges._usd_budget import UsdBudgetLedger

    ledger = UsdBudgetLedger(tmp_path / "budget.sqlite3", _pricing())
    reservation = ledger.reserve(input_token_upper_bound=10_000, max_output_tokens=512)
    ledger.record_result(reservation, status="usage_unknown")
    summary = ledger.summary()
    assert summary["accounted_usd"] == format(reservation.reserved_usd, "f")
    assert summary["active_or_unknown_reserved_usd"] == format(
        reservation.reserved_usd, "f"
    )


def test_completed_usage_cannot_settle_above_its_reserved_bounds(tmp_path):
    from bridges._usd_budget import BudgetError, UsdBudgetLedger

    ledger = UsdBudgetLedger(tmp_path / "budget.sqlite3", _pricing())
    reservation = ledger.reserve(input_token_upper_bound=10, max_output_tokens=10)
    with pytest.raises(BudgetError, match="exceeded"):
        ledger.record_result(
            reservation,
            status="completed",
            reported_input_tokens=11,
            reported_output_tokens=1,
        )
    summary = ledger.summary()
    assert summary["statuses"] == {"reserved": 1}
    assert summary["accounted_usd"] == format(reservation.reserved_usd, "f")


def test_v1_completed_rows_migrate_to_safe_settlement_but_unknown_rows_do_not(
    tmp_path,
):
    import sqlite3

    from bridges._usd_budget import UsdBudgetLedger

    path = tmp_path / "budget.sqlite3"
    legacy = UsdBudgetLedger(path, _pricing())
    completed = legacy.reserve(input_token_upper_bound=10_000, max_output_tokens=512)
    unknown = legacy.reserve(input_token_upper_bound=2_000, max_output_tokens=64)
    # Recreate the exact v1 state without fabricating pricing or route metadata.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE reservations SET status='completed', reported_input_tokens=1000, "
            "reported_output_tokens=20 WHERE reservation_id=?",
            (completed.reservation_id,),
        )
        connection.execute(
            "UPDATE reservations SET status='usage_unknown' WHERE reservation_id=?",
            (unknown.reservation_id,),
        )
        connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        connection.execute(
            "UPDATE metadata SET value='pre_dispatch_upper_bound_never_refunded_v1' "
            "WHERE key='reservation_policy'"
        )
        connection.execute("ALTER TABLE reservations RENAME TO reservations_v2")
        connection.execute(
            """CREATE TABLE reservations (
                reservation_id TEXT PRIMARY KEY,
                created_unix_ms INTEGER NOT NULL,
                input_token_upper_bound INTEGER NOT NULL,
                output_token_upper_bound INTEGER NOT NULL,
                reserved_nano_usd INTEGER NOT NULL,
                status TEXT NOT NULL,
                reported_input_tokens INTEGER,
                reported_output_tokens INTEGER
            )"""
        )
        connection.execute(
            """INSERT INTO reservations
               SELECT reservation_id, created_unix_ms, input_token_upper_bound,
                      output_token_upper_bound, reserved_nano_usd, status,
                      reported_input_tokens, reported_output_tokens
               FROM reservations_v2"""
        )
        connection.execute("DROP TABLE reservations_v2")

    migrated = UsdBudgetLedger(path, _pricing()).summary()
    assert migrated["schema_version"] == "2"
    assert migrated["statuses"] == {"completed": 1, "usage_unknown": 1}
    assert migrated["settled_usage_upper_usd"] == "0.00212"
    assert migrated["active_or_unknown_reserved_usd"] == format(
        unknown.reserved_usd, "f"
    )


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
