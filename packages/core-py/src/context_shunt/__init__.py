"""context-shunt core: read-only, question-driven, citation-verified.

It keeps large sources out of the main agent's context. It blocks oversized full reads
before they run, captures only what it withholds, answers questions about that immutable
snapshot through a configurable reader model, verifies every citation, and returns a
bounded envelope. It never writes to a source.

Revision 1.1 adds three things and takes nothing away:

* a hybrid local store - SQLite owns authorization, TTL, quotas, refcounts and accounting;
  immutable payloads live in content-addressed private files;
* two more read-only escape hatches - ``inspect`` for zero-model exact extraction under a
  cumulative disclosure ceiling, and ``stats`` for this session's own bounded metrics;
* truthful provenance and signed token accounting on every envelope.

The design is inspired by the Compress-Cache-Retrieve pattern popularized by Headroom. No
Headroom code is used, and one difference is deliberate: **no tool here ever retrieves the
full original payload into the main model context.** Everything the agent can reach is
either a cited model-derived answer or a capped, cumulatively-limited exact extract.
"""

from .accounting import Baseline, DeliveryBoundary, Egress, OperationKind, ReaderCost
from .capability import CapabilityReport, DisabledReason, ModeCapability, Support
from .config import Config, ProviderRef, ReaderConfig, ToolConfig
from .config import load as load_config
from .errors import ShuntError
from .gate import Decision, GateDecision, PreReadGate, ProbeResult, guidance_for
from .inspect import Inspector, decode_cursor, encode_cursor
from .limits import (
    BASELINE_ESTIMATE_METHOD,
    DEFAULT_LIMITS,
    EMITTED_SCHEMA_VERSION,
    READER_MODEL,
    SCHEMA_VERSION,
    SUPPORTED_REQUEST_VERSIONS,
    Limits,
    store_ddl,
)
from .provenance import (
    Attribution,
    AttributionPolicy,
    Confidence,
    ModelIdentity,
    Provenance,
    ProvenanceLabel,
    ResultKind,
    TokenMethod,
    Usage,
)
from .provider import FallbackChainProvider, HostBridgeProvider, UnavailableProvider
from .reader import Reader, ReaderResult
from .registry import RegisteredSource, SourceRegistry
from .session import ShuntSession, build_provider
from .spill import SpillEngine, SumaSpillEngine
from .store import Capture, OperationRecord, PublishedHandle, ScopeIdentity, SnapshotStore

__version__ = "1.1.0"

__all__ = [
    "Attribution",
    "AttributionPolicy",
    "BASELINE_ESTIMATE_METHOD",
    "Baseline",
    "CapabilityReport",
    "Capture",
    "Config",
    "Confidence",
    "DEFAULT_LIMITS",
    "Decision",
    "DeliveryBoundary",
    "DisabledReason",
    "EMITTED_SCHEMA_VERSION",
    "Egress",
    "FallbackChainProvider",
    "GateDecision",
    "HostBridgeProvider",
    "Inspector",
    "Limits",
    "ModeCapability",
    "ModelIdentity",
    "OperationKind",
    "OperationRecord",
    "PreReadGate",
    "ProbeResult",
    "Provenance",
    "ProvenanceLabel",
    "ProviderRef",
    "PublishedHandle",
    "READER_MODEL",
    "Reader",
    "ReaderResult",
    "ReaderConfig",
    "ReaderCost",
    "RegisteredSource",
    "ResultKind",
    "SCHEMA_VERSION",
    "SUPPORTED_REQUEST_VERSIONS",
    "ScopeIdentity",
    "ShuntError",
    "ShuntSession",
    "SnapshotStore",
    "SourceRegistry",
    "SpillEngine",
    "SumaSpillEngine",
    "Support",
    "TokenMethod",
    "ToolConfig",
    "UnavailableProvider",
    "Usage",
    "__version__",
    "build_provider",
    "decode_cursor",
    "encode_cursor",
    "guidance_for",
    "load_config",
    "store_ddl",
]
