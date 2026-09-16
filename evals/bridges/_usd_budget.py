"""Durable fail-closed USD reservations for the opt-in Luna evaluation.

The ledger reserves the conservative upper bound before a physical provider request.
A completed call with exact input and output usage is settled to a conservative upper
cost using the most expensive verified prompt and output rates.  A timeout, crash, lost
response, malformed usage block, or retry keeps its full reservation because it can still
have been billed.  SQLite's ``BEGIN IMMEDIATE`` makes reserve and settle operations atomic
across concurrent and resumed evaluator processes.

No prompt, completion, credential, or source text is stored.  The database contains only
the route/pricing binding, numeric bounds, reservation ids, and terminal status.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any

MAX_EVALUATION_USD = Decimal("2")
NANO_USD_PER_USD = Decimal("1000000000")
TOKENS_PER_MILLION = Decimal("1000000")
LEDGER_SCHEMA_VERSION = "2"
RESERVATION_POLICY = "pre_dispatch_reserve_exact_usage_upper_settlement_v2"
LEGACY_RESERVATION_POLICY = "pre_dispatch_upper_bound_never_refunded_v1"


class BudgetError(RuntimeError):
    """The evaluator cannot prove that another request stays under its USD ceiling."""


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise BudgetError(f"{label} pricing is not numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BudgetError(f"{label} pricing is not numeric") from None
    if not parsed.is_finite() or parsed < 0:
        raise BudgetError(f"{label} pricing must be finite and non-negative")
    return parsed


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class RoutePricing:
    """Verified route prices in USD per million tokens."""

    route: str
    fingerprint: str
    source: str
    rates: tuple[tuple[str, Decimal], ...]
    tier_rates: tuple[tuple[tuple[str, Decimal], ...], ...] = ()

    @classmethod
    def from_host_identity(
        cls, identity: dict[str, Any], *, expected_route: str
    ) -> RoutePricing:
        raw = identity.get("pricing")
        if not isinstance(raw, dict):
            raise BudgetError("host returned no verified route pricing")
        if raw.get("route") != expected_route:
            raise BudgetError("host pricing route does not match the requested route")
        if raw.get("currency") != "USD" or raw.get("unit") != "per_million_tokens":
            raise BudgetError("host pricing unit is unknown")
        source = raw.get("source")
        fingerprint = raw.get("fingerprint")
        if source != "openclaw.resolveModelCostConfig":
            raise BudgetError("host pricing source is not the supported OpenClaw resolver")
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(ch not in "0123456789abcdef" for ch in fingerprint)
        ):
            raise BudgetError("host pricing fingerprint is invalid")
        rate_fields = ("input", "output", "cacheRead", "cacheWrite")

        def parse_rates(value: object, label: str) -> tuple[tuple[str, Decimal], ...]:
            if not isinstance(value, dict) or any(field not in value for field in rate_fields):
                raise BudgetError(f"{label} pricing is incomplete")
            return tuple((field, _decimal(value[field], f"{label}.{field}")) for field in rate_fields)

        rates = parse_rates(raw.get("rates"), "route")
        raw_tiers = raw.get("tieredPricing", [])
        if not isinstance(raw_tiers, list):
            raise BudgetError("tiered pricing is malformed")
        tiers: list[tuple[tuple[str, Decimal], ...]] = []
        for index, tier in enumerate(raw_tiers):
            if not isinstance(tier, dict):
                raise BudgetError("tiered pricing is malformed")
            tiers.append(parse_rates(tier.get("cost"), f"tier[{index}]"))
        all_rates = [dict(rates), *(dict(tier) for tier in tiers)]
        if not any(rate["input"] > 0 or rate["output"] > 0 for rate in all_rates):
            raise BudgetError("route pricing must contain a positive input or output rate")
        return cls(
            route=expected_route,
            fingerprint=fingerprint,
            source=source,
            rates=rates,
            tier_rates=tuple(tiers),
        )

    def _max_rate(self, *names: str) -> Decimal:
        schedules = (self.rates, *self.tier_rates)
        return max(dict(schedule)[name] for schedule in schedules for name in names)

    @property
    def max_input_rate(self) -> Decimal:
        # A provider may classify prompt tokens as uncached, cache-read, or cache-write.
        # Reserving at the most expensive prompt bucket is safe for every classification.
        schedules = (self.rates, *self.tier_rates)
        return max(
            max(
                rates["input"],
                rates["cacheRead"],
                rates["cacheWrite"],
                # OpenClaw prices one-hour cache writes at twice the input rate.
                rates["input"] * 2,
            )
            for rates in (dict(schedule) for schedule in schedules)
        )

    @property
    def max_output_rate(self) -> Decimal:
        return self._max_rate("output")

    def public_record(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "currency": "USD",
            "unit": "per_million_tokens",
            "source": self.source,
            "fingerprint": self.fingerprint,
            "rates": {key: format(value, "f") for key, value in self.rates},
            "tiered_rates": [
                {key: format(value, "f") for key, value in tier}
                for tier in self.tier_rates
            ],
            "worst_input_rate": format(self.max_input_rate, "f"),
            "worst_output_rate": format(self.max_output_rate, "f"),
        }

    @property
    def binding_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.public_record()).encode()).hexdigest()


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    reserved_nano_usd: int

    @property
    def reserved_usd(self) -> Decimal:
        return Decimal(self.reserved_nano_usd) / NANO_USD_PER_USD


class UsdBudgetLedger:
    """A cumulative USD ceiling shared by all calls using one ledger path."""

    def __init__(self, path: Path, pricing: RoutePricing) -> None:
        self.path = Path(path)
        if self.path.exists() and self.path.is_symlink():
            raise BudgetError("USD budget ledger must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pricing = pricing
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _metadata(self) -> dict[str, str]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "limit_nano_usd": str(int(MAX_EVALUATION_USD * NANO_USD_PER_USD)),
            "route": self.pricing.route,
            "pricing_fingerprint": self.pricing.fingerprint,
            "pricing_binding_sha256": self.pricing.binding_sha256,
            "pricing": _canonical_json(self.pricing.public_record()),
            "reservation_policy": RESERVATION_POLICY,
        }

    def _cost_nano_usd(self, input_tokens: int, output_tokens: int) -> int:
        """Conservative charge for known token counts, rounded upward."""
        cost = (
            Decimal(input_tokens) * self.pricing.max_input_rate
            + Decimal(output_tokens) * self.pricing.max_output_rate
        ) / TOKENS_PER_MILLION
        return int((cost * NANO_USD_PER_USD).to_integral_value(rounding=ROUND_CEILING))

    def _initialize(self) -> None:
        expected = self._metadata()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    created_unix_ms INTEGER NOT NULL,
                    input_token_upper_bound INTEGER NOT NULL,
                    output_token_upper_bound INTEGER NOT NULL,
                    reserved_nano_usd INTEGER NOT NULL,
                    accounted_nano_usd INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reported_input_tokens INTEGER,
                    reported_output_tokens INTEGER
                )"""
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(reservations)")
            }
            if "accounted_nano_usd" not in columns:
                connection.execute(
                    "ALTER TABLE reservations ADD COLUMN accounted_nano_usd INTEGER"
                )
            existing = dict(connection.execute("SELECT key, value FROM metadata"))
            if not existing:
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)", expected.items()
                )
            elif self._is_legacy_metadata(existing, expected):
                self._migrate_legacy_reservations(connection)
                connection.execute("DELETE FROM metadata")
                connection.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)", expected.items()
                )
            elif existing != expected:
                connection.rollback()
                differing = "pricing" if any(
                    existing.get(key) != expected[key]
                    for key in ("route", "pricing_fingerprint", "pricing_binding_sha256", "pricing")
                ) else "budget policy"
                raise BudgetError(f"existing USD ledger {differing} does not match this run")
            connection.execute(
                "UPDATE reservations SET accounted_nano_usd=reserved_nano_usd "
                "WHERE accounted_nano_usd IS NULL"
            )
            connection.commit()

    @staticmethod
    def _is_legacy_metadata(existing: dict[str, str], expected: dict[str, str]) -> bool:
        """Only the exact v1 policy may be upgraded in place."""
        return (
            existing.get("schema_version") == "1"
            and existing.get("reservation_policy") == LEGACY_RESERVATION_POLICY
            and all(
                existing.get(key) == expected[key]
                for key in (
                    "limit_nano_usd",
                    "route",
                    "pricing_fingerprint",
                    "pricing_binding_sha256",
                    "pricing",
                )
            )
            and set(existing) == set(expected)
        )

    def _migrate_legacy_reservations(self, connection: sqlite3.Connection) -> None:
        """Settle only v1 rows that already recorded complete exact usage."""
        rows = connection.execute(
            """SELECT reservation_id, reserved_nano_usd, status,
                      reported_input_tokens, reported_output_tokens
               FROM reservations"""
        ).fetchall()
        for reservation_id, reserved, status, input_tokens, output_tokens in rows:
            accounted = int(reserved)
            if (
                status == "completed"
                and isinstance(input_tokens, int)
                and input_tokens >= 0
                and isinstance(output_tokens, int)
                and output_tokens >= 0
            ):
                settled = self._cost_nano_usd(input_tokens, output_tokens)
                if settled > accounted:
                    raise BudgetError(
                        "legacy USD ledger usage exceeded its pre-dispatch reservation"
                    )
                accounted = settled
            connection.execute(
                "UPDATE reservations SET accounted_nano_usd=? WHERE reservation_id=?",
                (accounted, reservation_id),
            )

    def reserve(
        self, *, input_token_upper_bound: int, max_output_tokens: int
    ) -> Reservation:
        for value, label in (
            (input_token_upper_bound, "input bound"),
            (max_output_tokens, "output bound"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise BudgetError(f"{label} must be a positive integer")
        nano = self._cost_nano_usd(input_token_upper_bound, max_output_tokens)
        if nano <= 0:
            raise BudgetError("request reservation rounded to a non-positive amount")
        limit = int(MAX_EVALUATION_USD * NANO_USD_PER_USD)
        reservation_id = str(uuid.uuid4())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            used = int(
                connection.execute(
                    "SELECT COALESCE(SUM(accounted_nano_usd), 0) FROM reservations"
                ).fetchone()[0]
            )
            if used + nano > limit:
                connection.rollback()
                raise BudgetError(
                    "provider request refused: conservative reservation would exceed "
                    f"the cumulative USD 2 ceiling (reserved={Decimal(used) / NANO_USD_PER_USD}, "
                    f"request={Decimal(nano) / NANO_USD_PER_USD})"
                )
            connection.execute(
                """INSERT INTO reservations(
                    reservation_id, created_unix_ms, input_token_upper_bound,
                    output_token_upper_bound, reserved_nano_usd, accounted_nano_usd, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'reserved')""",
                (
                    reservation_id,
                    int(time.time() * 1000),
                    input_token_upper_bound,
                    max_output_tokens,
                    nano,
                    nano,
                ),
            )
            connection.commit()
        return Reservation(reservation_id, nano)

    def record_result(
        self,
        reservation: Reservation,
        *,
        status: str,
        reported_input_tokens: int | None = None,
        reported_output_tokens: int | None = None,
    ) -> None:
        if status not in {"completed", "usage_unknown", "bound_breach"}:
            raise BudgetError("invalid reservation terminal status")
        for value, label in (
            (reported_input_tokens, "reported input tokens"),
            (reported_output_tokens, "reported output tokens"),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise BudgetError(f"{label} must be a non-negative integer or absent")
        if status == "completed" and (
            reported_input_tokens is None or reported_output_tokens is None
        ):
            raise BudgetError("completed reservation requires exact input and output usage")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT reserved_nano_usd, input_token_upper_bound,
                          output_token_upper_bound
                   FROM reservations
                   WHERE reservation_id=? AND status='reserved'""",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise BudgetError("reservation was missing or already settled")
            reserved_nano, input_bound, output_bound = map(int, row)
            accounted_nano = reserved_nano
            if reported_input_tokens is not None and reported_output_tokens is not None:
                settled_nano = self._cost_nano_usd(
                    reported_input_tokens, reported_output_tokens
                )
                breached = (
                    reported_input_tokens > input_bound
                    or reported_output_tokens > output_bound
                    or settled_nano > reserved_nano
                )
                if status == "completed" and breached:
                    connection.rollback()
                    raise BudgetError(
                        "completed usage exceeded its pre-dispatch reservation"
                    )
                if status == "completed":
                    accounted_nano = settled_nano
                elif status == "bound_breach":
                    accounted_nano = max(reserved_nano, settled_nano)
            cursor = connection.execute(
                """UPDATE reservations
                   SET status=?, reported_input_tokens=?, reported_output_tokens=?,
                       accounted_nano_usd=?
                   WHERE reservation_id=? AND status='reserved'""",
                (
                    status,
                    reported_input_tokens,
                    reported_output_tokens,
                    accounted_nano,
                    reservation.reservation_id,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise BudgetError("reservation was missing or already settled")
            connection.commit()

    def summary(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            accounted, gross_reserved, active_reserved, settled, count = connection.execute(
                """SELECT COALESCE(SUM(accounted_nano_usd), 0),
                          COALESCE(SUM(reserved_nano_usd), 0),
                          COALESCE(SUM(CASE WHEN status IN ('reserved', 'usage_unknown')
                                           THEN accounted_nano_usd ELSE 0 END), 0),
                          COALESCE(SUM(CASE WHEN status='completed'
                                           THEN accounted_nano_usd ELSE 0 END), 0),
                          COUNT(*)
                   FROM reservations"""
            ).fetchone()
            statuses = {
                row[0]: int(row[1])
                for row in connection.execute(
                    "SELECT status, COUNT(*) FROM reservations GROUP BY status ORDER BY status"
                )
            }
        accounted = int(accounted)
        gross_reserved = int(gross_reserved)
        active_reserved = int(active_reserved)
        settled = int(settled)
        limit = int(MAX_EVALUATION_USD * NANO_USD_PER_USD)
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "limit_usd": format(MAX_EVALUATION_USD, "f"),
            # Kept for report-schema compatibility: this is the cumulative amount that
            # currently counts against the cap, not the historical sum of peak holds.
            "reserved_usd": format(Decimal(accounted) / NANO_USD_PER_USD, "f"),
            "accounted_usd": format(Decimal(accounted) / NANO_USD_PER_USD, "f"),
            "gross_reserved_usd": format(
                Decimal(gross_reserved) / NANO_USD_PER_USD, "f"
            ),
            "active_or_unknown_reserved_usd": format(
                Decimal(active_reserved) / NANO_USD_PER_USD, "f"
            ),
            "settled_usage_upper_usd": format(
                Decimal(settled) / NANO_USD_PER_USD, "f"
            ),
            "remaining_usd": format(Decimal(limit - accounted) / NANO_USD_PER_USD, "f"),
            "reservations": int(count),
            "statuses": statuses,
            "pricing": self.pricing.public_record(),
            "pricing_binding_sha256": self.pricing.binding_sha256,
            "reservation_policy": RESERVATION_POLICY,
            "contains_prompt_or_completion": False,
        }


def upper_bound_input_tokens(system: str, user: str) -> int:
    """Conservative token ceiling for the verified two-message, zero-tool route.

    Byte-level model tokenizers cannot emit more content tokens than UTF-8 bytes.  The
    exact host source supplies only these two strings and an empty tool list; 512 more
    tokens conservatively cover the fixed role/message framing.  The bridge contract gate
    pins that source shape.  Any provider-reported breach is terminal and retained.
    """

    content_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
    if content_bytes <= 0:
        raise BudgetError("input bound requires non-empty role content")
    return content_bytes + 512
