"""context-shunt core: read-only, question-driven, citation-verified.

v1 keeps large sources out of the main agent's context. It blocks oversized full reads
before they run, answers questions about a source through a fixed cheap reader model,
verifies every citation against an immutable snapshot, and returns a bounded envelope.
It never writes to a source.
"""

from .capability import CapabilityReport, DisabledReason, ModeCapability, Support
from .config import Config
from .config import load as load_config
from .errors import ShuntError
from .gate import Decision, GateDecision, PreReadGate, ProbeResult, guidance_for
from .limits import DEFAULT_LIMITS, READER_MODEL, SCHEMA_VERSION, Limits
from .reader import Reader
from .registry import SourceRegistry
from .spill import SpillStore, SumaSpillEngine

__version__ = "1.0.0"

__all__ = [
    "CapabilityReport",
    "Config",
    "DEFAULT_LIMITS",
    "Decision",
    "DisabledReason",
    "GateDecision",
    "Limits",
    "ModeCapability",
    "PreReadGate",
    "ProbeResult",
    "READER_MODEL",
    "Reader",
    "SCHEMA_VERSION",
    "ShuntError",
    "SourceRegistry",
    "SpillStore",
    "SumaSpillEngine",
    "Support",
    "__version__",
    "guidance_for",
    "load_config",
]
