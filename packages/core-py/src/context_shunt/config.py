"""Configuration.

Rules that matter more than the rest:

* ``writer.enabled = true`` is refused at load time. There is no writer, so accepting the
  flag and quietly ignoring it would turn a missing feature into a hidden one.
* ``tool_result_capture.enabled`` defaults to ``false`` and, even when set, only takes
  effect if the adapter's capability probe reports the mode supported - which additionally
  requires ``host_ordering_verified_locally``, an explicit operator attestation this code
  does not and cannot prove for itself. The deprecated ``suma_post_tool`` key is still
  accepted as an alias; setting both to disagreeing values is refused rather than guessed.
* ``artifact_import`` defaults to disabled with no roots. Enabling it needs at least one
  explicit import root *and* an explicitly allowlisted manifest schema: an artifact
  producer is a trust decision, so neither the roots nor the accepted producer shapes have
  a permissive default. Import roots are canonicalized, refused when they contain the
  private cache, and are a separate allowlist from ``workspace_roots`` - a deployment can
  broker external artifacts without widening what the pre-read gate may capture.
* A cap may be narrowed, never widened.

Changed in 1.1: reader model and provider are configurable
----------------------------------------------------------
Revision 1.0 refused any ``reader.model`` other than ``gpt-5.6-luna``. That was a
single-model contract, and it is deliberately relaxed here: ``gpt-5.6-luna`` remains the
**default**, and a deployment may point the reader at another model or provider. This is a
real behaviour change, not a quiet loosening - what replaces the old hard refusal is
truthful provenance. Every envelope states the requested model, whatever the host
reported, and how strongly the attribution can be believed, so a different model can never
be passed off as the default one.

``reader.attribution_policy`` decides what happens when attribution cannot be proven:

``allow_unverified`` (default)
    Publish the answer with ``attribution_status = unverified``. Chosen as the default
    because on a host whose plugin LLM facade cannot distinguish a provider report from an
    echo of the request, ``require_match`` disables the reader entirely.
``require_match``
    Refuse to answer unless the host reported a selection that agrees with the request.

A contradiction is refused under either policy: a mismatch is a wrong answer, not a weak
one.

``reader.fallback_chain`` is availability-only. Every entry keeps its own reported
provenance and usage, and reaching one is recorded as ``fallback_used``. It is never a
remedy for a poor-quality answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ShuntError
from .limits import DEFAULT_LIMITS, POSITIVE_LIMITS, Limits
from .paths import PathPolicy
from .provenance import AttributionPolicy

_NARROWABLE = frozenset(
    key
    for key in DEFAULT_LIMITS.__dataclass_fields__
    if isinstance(getattr(DEFAULT_LIMITS, key), int)
)
_CONFIG_KEYS = {
    "workspace_roots",
    "artifact_import",
    "spill_dir",
    "cache_dir",
    "denylist",
    "gate_enabled",
    "reader",
    "inspect",
    "stats",
    "tool_result_capture",
    #: Deprecated alias for `tool_result_capture`, accepted so an existing config file
    #: keeps working unchanged. See `_read_tool_result_capture`.
    "suma_post_tool",
    "writer",
    "operations",
    "limits",
}
_READER_KEYS = {
    "enabled",
    "model",
    "provider",
    "attribution_policy",
    "fallback_chain",
    "automatic_extract",
    "fallback_max_bytes",
    "legacy_compaction",
    "legacy_compaction_max_chars",
}
_ARTIFACT_IMPORT_KEYS = {"enabled", "roots", "accepted_manifest_schemas"}
_ENABLED_SECTION_KEYS = {"enabled"}
_TOOL_RESULT_CAPTURE_KEYS = {"enabled", "host_ordering_verified_locally"}
_MAX_FALLBACK_ENTRIES = 4
_MAX_MODEL_REF_BYTES = 128


@dataclass(frozen=True)
class ToolResultCaptureConfig:
    """The optional oversized-tool-result capture mode.

    Capture at the host's transform_tool_result-shaped hook has been directly verified
    feasible on one specific operator's live host (read-only inspection, dated and cited in
    docs/capability-matrix.md) - but that is evidence about one running instance, not a
    reproducible, version-independent proof this adapter can make about every host it might
    be installed against. `enabled` alone is therefore never enough to turn the mode on:
    the capability probe additionally requires `host_ordering_verified_locally`, an
    explicit **operator attestation** (this code does not and cannot prove it for itself)
    that the operator personally confirmed the ordering on their own installed host.
    Without both, the mode is reported unsupported and stays off regardless of what a
    config file requests - the same discipline every other mode in this project follows.
    """

    enabled: bool = False
    host_ordering_verified_locally: bool = False


#: Deprecated alias kept for existing imports. `suma_post_tool` was never a product name,
#: only this project's internal shorthand for "the optional oversized post-tool mode", and
#: is retired as public vocabulary in favor of a name that says what the mode does.
SumaConfig = ToolResultCaptureConfig


@dataclass(frozen=True)
class ArtifactImportConfig:
    """The external-artifact import boundary.

    ``roots`` is an allowlist of canonical directories an artifact may live under, held
    separately from ``workspace_roots`` so brokering a producer's artifacts never widens
    what an ordinary read may capture. ``accepted_manifest_schemas`` is an allowlist of
    producer manifest shapes: a manifest declaring a schema outside it is refused even
    when a translation profile for that schema exists, because knowing how to read a
    producer's manifest is not the same as being authorized to.
    """

    enabled: bool = False
    roots: tuple[str, ...] = ()
    accepted_manifest_schemas: tuple[str, ...] = ()

    def path_policy(self, denylist: tuple[str, ...] = ()) -> PathPolicy:
        """The import-root policy. Reuses the same canonicalization the gate uses."""
        return PathPolicy.from_config(list(self.roots), list(denylist))


@dataclass(frozen=True)
class ProviderRef:
    """One availability target: a model, optionally pinned to a provider."""

    model: str
    provider: str = ""


@dataclass(frozen=True)
class ReaderConfig:
    enabled: bool = True
    model: str = DEFAULT_LIMITS.reader_model
    provider: str = ""
    attribution_policy: AttributionPolicy = AttributionPolicy.ALLOW_UNVERIFIED
    fallback_chain: tuple[ProviderRef, ...] = ()
    automatic_extract: bool = True
    fallback_max_bytes: int = 2048
    #: The broader deterministic fallback: a ported heuristic compaction of the source,
    #: covering reader outcomes automatic_extract was never asked to (malformed output,
    #: citation-empty, or an availability failure when automatic_extract is off). See
    #: `session.py`'s `_LEGACY_COMPACTION_TRIGGER_CODES` for the exact ordering.
    legacy_compaction: bool = True
    #: Character budget handed to the compaction algorithm before the envelope's own byte
    #: cap is enforced. Kept below `limits.max_extraction_bytes` (16 KiB) by default so the
    #: algorithm's own cap is normally what fires, not the safety truncation behind it.
    legacy_compaction_max_chars: int = 16_000


@dataclass(frozen=True)
class ToolConfig:
    """Which read-only escape hatches are registered. None of them can retrieve a payload."""

    inspect_enabled: bool = True
    stats_enabled: bool = True


@dataclass(frozen=True)
class Config:
    workspace_roots: tuple[str, ...]
    spill_dir: Path
    denylist: tuple[str, ...] = ()
    gate_enabled: bool = True
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
    tool_result_capture: ToolResultCaptureConfig = field(default_factory=ToolResultCaptureConfig)
    artifact_import: ArtifactImportConfig = field(default_factory=ArtifactImportConfig)
    limits: Limits = DEFAULT_LIMITS

    @property
    def suma_post_tool(self) -> ToolResultCaptureConfig:
        """Deprecated alias for :attr:`tool_result_capture`. See ``SumaConfig``."""
        return self.tool_result_capture

    @property
    def cache_root(self) -> Path:
        """Private cache root: SQLite metadata plus content-addressed payload files."""
        return self.spill_dir

    def path_policy(self) -> PathPolicy:
        return PathPolicy.from_config(list(self.workspace_roots), list(self.denylist))

    def import_path_policy(self) -> PathPolicy:
        """The import-root policy, sharing this deployment's administrator denylist."""
        return self.artifact_import.path_policy(self.denylist)


def load(raw: dict[str, Any] | None, *, default_spill_dir: Path) -> Config:
    if raw is not None and not isinstance(raw, dict):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    raw = dict(raw or {})
    if set(raw) - _CONFIG_KEYS:
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)

    for key in (
        "reader",
        "inspect",
        "stats",
        "tool_result_capture",
        "suma_post_tool",
        "artifact_import",
        "writer",
        "limits",
    ):
        if key in raw and not isinstance(raw[key], dict):
            raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    operations = raw.get("operations")
    if operations is not None and (
        not isinstance(operations, list) or any(not isinstance(item, str) for item in operations)
    ):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    reader_raw = raw.get("reader") or {}
    if set(reader_raw) - _READER_KEYS:
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    for key in ("inspect", "stats", "writer"):
        if set(raw.get(key) or {}) - _ENABLED_SECTION_KEYS:
            raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    tool_result_capture_raw = _merge_tool_result_capture_raw(raw)
    if set(tool_result_capture_raw) - _TOOL_RESULT_CAPTURE_KEYS:
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    import_raw = raw.get("artifact_import") or {}
    if set(import_raw) - _ARTIFACT_IMPORT_KEYS:
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    for value in (
        raw.get("gate_enabled"),
        reader_raw.get("enabled"),
        reader_raw.get("automatic_extract"),
        (raw.get("inspect") or {}).get("enabled"),
        (raw.get("stats") or {}).get("enabled"),
        tool_result_capture_raw.get("enabled"),
        tool_result_capture_raw.get("host_ordering_verified_locally"),
        import_raw.get("enabled"),
        (raw.get("writer") or {}).get("enabled"),
    ):
        if value is not None and not isinstance(value, bool):
            raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)

    writer = raw.get("writer") or {}
    if writer.get("enabled"):
        # Not "not implemented yet" - refused, so it cannot become a hidden capability.
        raise ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", retryable=False)
    if "operations" in raw and "propose_patch" in (raw.get("operations") or []):
        raise ShuntError("INVALID_REQUEST", "WRITER_UNSUPPORTED_CONFIGURATION", retryable=False)

    roots_raw = raw.get("workspace_roots")
    if (
        not isinstance(roots_raw, list)
        or not roots_raw
        or any(not isinstance(root, str) or not root for root in roots_raw)
    ):
        raise ShuntError("UNSAFE_SOURCE", "NO_WORKSPACE_ROOT", retryable=False)
    roots = tuple(str(Path(root).expanduser().resolve(strict=False)) for root in roots_raw)

    spill_value = raw.get("cache_dir", raw.get("spill_dir"))
    if spill_value is not None and not isinstance(spill_value, str):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)

    reader = _read_reader(reader_raw)
    limits = _read_limits(raw.get("limits") or {})
    artifact_import = _read_artifact_import(import_raw, limits)

    spill_dir = Path(spill_value or default_spill_dir).expanduser().resolve(strict=False)
    if any(spill_dir == Path(root) or Path(root) in spill_dir.parents for root in roots):
        raise ShuntError("UNSAFE_SOURCE", "SPILL_INSIDE_WORKSPACE", retryable=False)
    for root in artifact_import.roots:
        # An import root containing the private cache would let a manifest name one of
        # our own immutable blobs as if it were a producer artifact.
        if spill_dir == Path(root) or Path(root) in spill_dir.parents:
            raise ShuntError("UNSAFE_SOURCE", "CACHE_INSIDE_IMPORT_ROOT", retryable=False)

    return Config(
        workspace_roots=roots,
        spill_dir=spill_dir,
        denylist=_read_denylist(raw.get("denylist")),
        gate_enabled=raw.get("gate_enabled", True),
        reader=reader,
        tools=ToolConfig(
            inspect_enabled=(raw.get("inspect") or {}).get("enabled", True),
            stats_enabled=(raw.get("stats") or {}).get("enabled", True),
        ),
        tool_result_capture=ToolResultCaptureConfig(
            enabled=tool_result_capture_raw.get("enabled", False),
            host_ordering_verified_locally=tool_result_capture_raw.get(
                "host_ordering_verified_locally", False
            ),
        ),
        artifact_import=artifact_import,
        limits=limits,
    )


def _merge_tool_result_capture_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge the canonical ``tool_result_capture`` key with the deprecated ``suma_post_tool``
    alias, preferring the canonical key and refusing a config that sets both to disagreeing
    values rather than silently picking a side of a half-migrated file.
    """
    current = raw.get("tool_result_capture")
    legacy = raw.get("suma_post_tool")
    if current is not None and legacy is not None and dict(current) != dict(legacy):
        raise ShuntError(
            "INVALID_REQUEST", "TOOL_RESULT_CAPTURE_CONFIG_CONFLICT", retryable=False
        )
    return dict((current if current is not None else legacy) or {})


def _read_reader(reader_raw: dict[str, Any]) -> ReaderConfig:
    model = reader_raw.get("model", DEFAULT_LIMITS.reader_model)
    provider = reader_raw.get("provider", "")
    for value in (model, provider):
        if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_MODEL_REF_BYTES:
            raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    if not model.strip():
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)

    policy_raw = reader_raw.get("attribution_policy", AttributionPolicy.ALLOW_UNVERIFIED.value)
    if policy_raw not in (
        AttributionPolicy.ALLOW_UNVERIFIED.value,
        AttributionPolicy.REQUIRE_MATCH.value,
    ):
        raise ShuntError("INVALID_REQUEST", "BAD_ATTRIBUTION_POLICY", retryable=False)

    chain_raw = reader_raw.get("fallback_chain", [])
    if not isinstance(chain_raw, list) or len(chain_raw) > _MAX_FALLBACK_ENTRIES:
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    chain: list[ProviderRef] = []
    for entry in chain_raw:
        if not isinstance(entry, dict) or set(entry) - {"model", "provider"}:
            raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
        entry_model = entry.get("model", "")
        entry_provider = entry.get("provider", "")
        if (
            not isinstance(entry_model, str)
            or not entry_model.strip()
            or not isinstance(entry_provider, str)
            or len(entry_model.encode("utf-8")) > _MAX_MODEL_REF_BYTES
            or len(entry_provider.encode("utf-8")) > _MAX_MODEL_REF_BYTES
        ):
            raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
        chain.append(ProviderRef(model=entry_model.strip(), provider=entry_provider.strip()))

    if "automatic_extract" in reader_raw and type(reader_raw["automatic_extract"]) is not bool:
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    fallback_bytes = reader_raw.get("fallback_max_bytes", 2048)
    if type(fallback_bytes) is not int or not 1 <= fallback_bytes <= 4096:
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    if "legacy_compaction" in reader_raw and type(reader_raw["legacy_compaction"]) is not bool:
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    legacy_compaction_max_chars = reader_raw.get("legacy_compaction_max_chars", 16_000)
    if (
        type(legacy_compaction_max_chars) is not int
        or not 1_000 <= legacy_compaction_max_chars <= 60_000
    ):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    return ReaderConfig(
        automatic_extract=reader_raw.get("automatic_extract", True),
        fallback_max_bytes=fallback_bytes,
        legacy_compaction=reader_raw.get("legacy_compaction", True),
        legacy_compaction_max_chars=legacy_compaction_max_chars,
        enabled=reader_raw.get("enabled", True),
        model=model.strip(),
        provider=provider.strip(),
        attribution_policy=AttributionPolicy(policy_raw),
        fallback_chain=tuple(chain),
    )


#: The one manifest shape this core owns. Any other accepted value names a *foreign*
#: producer schema that a translation profile normalizes into it; the profile table lives
#: in ``artifacts.py`` as data, so no producer's identifiers appear in the core API.
NATIVE_IMPORT_CONTRACT = "context_shunt.artifact_import.v1"
_MAX_MANIFEST_SCHEMA_BYTES = 128


def _read_artifact_import(raw_import: dict[str, Any], limits: Limits) -> ArtifactImportConfig:
    """Read the import boundary. Disabled with no roots unless a deployment says otherwise."""
    enabled = bool(raw_import.get("enabled", False))

    roots_raw = raw_import.get("roots", [])
    if (
        not isinstance(roots_raw, list)
        or len(roots_raw) > _import_root_cap(limits)
        or any(not isinstance(root, str) or not root.strip() for root in roots_raw)
    ):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    roots = tuple(str(Path(root).expanduser().resolve(strict=False)) for root in roots_raw)

    schemas_raw = raw_import.get("accepted_manifest_schemas", [])
    if not isinstance(schemas_raw, list):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    schemas: list[str] = []
    for entry in schemas_raw:
        if (
            not isinstance(entry, str)
            or not entry.strip()
            or len(entry.encode("utf-8")) > _MAX_MANIFEST_SCHEMA_BYTES
        ):
            raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
        schemas.append(entry.strip())

    if enabled and (not roots or not schemas):
        # Enabling the boundary without saying what it trusts would be an allow-all in
        # everything but name, so it is a configuration error rather than a wide default.
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)

    return ArtifactImportConfig(
        enabled=enabled,
        roots=roots,
        accepted_manifest_schemas=tuple(dict.fromkeys(schemas)),
    )


def _import_root_cap(limits: Limits) -> int:
    del limits  # the cap is normative, not narrowable per deployment
    return int(raw_import_limits()["max_import_roots"])


def raw_import_limits() -> dict[str, Any]:
    from .limits import raw_limits

    return dict(raw_limits()["artifact_import"])


def _read_limits(raw_limits: dict[str, Any]) -> Limits:
    unknown = set(raw_limits) - _NARROWABLE
    if unknown:
        raise ShuntError("INVALID_REQUEST", "UNKNOWN_LIMIT_OVERRIDE", retryable=False)
    overrides: dict[str, int] = {}
    for key, value in raw_limits.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or (value == 0 and key in POSITIVE_LIMITS)
        ):
            raise ShuntError("INVALID_REQUEST", "BAD_LIMIT_OVERRIDE", retryable=False)
        overrides[key] = value
    if not overrides:
        return DEFAULT_LIMITS
    try:
        return DEFAULT_LIMITS.narrow(**overrides)
    except ValueError:
        raise ShuntError("INVALID_REQUEST", "LIMIT_MAY_ONLY_NARROW", retryable=False) from None


def _read_denylist(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ShuntError("INVALID_REQUEST", "BAD_CONFIGURATION", retryable=False)
    return tuple(value)
