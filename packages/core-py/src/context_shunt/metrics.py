"""Non-content metrics.

Labels are bounded enums only. Source paths, questions, answers, quotes, payloads,
secrets and full request/source identifiers are never labels - a high-cardinality label
is both a cost problem and a leak channel. Correlation for diagnostics uses a short
random id recorded in a restricted event log, never a metric dimension.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Protocol

ALLOWED_LABEL_KEYS = frozenset(
    {"adapter", "mode", "reason", "status", "code", "form", "decision", "model", "result", "stage"}
)
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")


class MetricsError(ValueError):
    pass


def check_labels(labels: dict[str, Any] | None) -> dict[str, str]:
    if not labels:
        return {}
    out: dict[str, str] = {}
    for key, value in labels.items():
        if key not in ALLOWED_LABEL_KEYS:
            raise MetricsError(f"label key not allowed: {key}")
        text = str(value)
        if not _LABEL_VALUE.match(text):
            raise MetricsError(f"label value not a bounded enum token: {key}")
        out[key] = text
    return out


class MetricsSink(Protocol):
    def count(self, name: str, labels: dict[str, Any] | None = None, value: int = 1) -> None: ...
    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None: ...


class NullMetrics:
    def count(self, name: str, labels: dict[str, Any] | None = None, value: int = 1) -> None:
        check_labels(labels)

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        check_labels(labels)


class InMemoryMetrics:
    """Used by the gates to assert what is (and is not) recorded."""

    def __init__(self) -> None:
        self.counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self.observations: list[tuple[str, float, tuple[tuple[str, str], ...]]] = []

    def count(self, name: str, labels: dict[str, Any] | None = None, value: int = 1) -> None:
        self.counters[(name, tuple(sorted(check_labels(labels).items())))] += value

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        self.observations.append((name, float(value), tuple(sorted(check_labels(labels).items()))))

    def rendered(self) -> str:
        """Everything this sink would emit, for leak assertions."""
        parts = [f"{n}{dict(l)}={v}" for (n, l), v in self.counters.items()]
        parts += [f"{n}{dict(l)}={v}" for n, v, l in self.observations]
        return "\n".join(parts)
