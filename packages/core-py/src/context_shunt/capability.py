"""Capability detection and the startup report.

A mode is enabled only when the adapter can *prove* the host gives it what the mode
needs. Where the proof does not exist the mode is reported ``unsupported`` with a fixed
reason and stays off - never faked, never "probably fine".

For the optional Suma post-tool mode the required proof is two-part:

* complete capture of the result **before** any host truncation, and
* safe replacement **before** persistence and context insertion.

Neither supported host provides both today; see ``docs/capability-matrix.md`` for the
file-and-line evidence behind each ``DisabledReason``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Support(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    DISABLED_BY_CONFIG = "disabled_by_config"


class DisabledReason(str, Enum):
    HOOK_MISSING = "HOOK_MISSING"
    CAPTURE_AFTER_TRUNCATION = "CAPTURE_AFTER_TRUNCATION"
    REPLACEMENT_AFTER_PERSISTENCE = "REPLACEMENT_AFTER_PERSISTENCE"
    OBSERVE_ONLY_HOOK = "OBSERVE_ONLY_HOOK"
    HOST_FAIL_OPEN = "HOST_FAIL_OPEN"
    ORDERING_UNPROVEN = "ORDERING_UNPROVEN"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    UNSAFE_TRACING = "UNSAFE_TRACING"
    HOST_VERSION_UNVERIFIED = "HOST_VERSION_UNVERIFIED"
    CONFIG_DISABLED = "CONFIG_DISABLED"


@dataclass(frozen=True)
class ModeCapability:
    mode: str
    support: Support
    reasons: tuple[DisabledReason, ...] = ()
    evidence: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.support is Support.SUPPORTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "support": self.support.value,
            "enabled": self.enabled,
            "reasons": [r.value for r in self.reasons],
            "evidence": list(self.evidence),
        }


@dataclass
class CapabilityReport:
    adapter: str
    adapter_version: str
    host_name: str
    host_version: str
    contract_version: str
    reader_model: str
    tools_covered: tuple[str, ...] = ()
    modes: list[ModeCapability] = field(default_factory=list)
    tested_fixture_id: str = ""

    def mode(self, name: str) -> ModeCapability | None:
        return next((m for m in self.modes if m.mode == name), None)

    def enabled(self, name: str) -> bool:
        found = self.mode(name)
        return bool(found and found.enabled)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "host": {"name": self.host_name, "version": self.host_version},
            "contract_version": self.contract_version,
            "reader_model": self.reader_model,
            "tools_covered": list(self.tools_covered),
            "modes": [m.to_dict() for m in self.modes],
            "tested_fixture_id": self.tested_fixture_id,
        }


def unsupported(mode: str, *reasons: DisabledReason, evidence: tuple[str, ...] = ()) -> ModeCapability:
    return ModeCapability(mode=mode, support=Support.UNSUPPORTED, reasons=reasons, evidence=evidence)


def supported(mode: str, evidence: tuple[str, ...] = ()) -> ModeCapability:
    return ModeCapability(mode=mode, support=Support.SUPPORTED, evidence=evidence)


def disabled_by_config(mode: str) -> ModeCapability:
    return ModeCapability(
        mode=mode, support=Support.DISABLED_BY_CONFIG, reasons=(DisabledReason.CONFIG_DISABLED,)
    )
