"""The external-artifact import boundary.

Why this exists
---------------
The oversized context problem that actually costs a session is not a file read - it is a
*tool result*: a log query, a cloud journal page, an issue tracker export, a wiki page.
Holding one of those out of the main context requires intercepting the result before the
host truncates it and persists it, and no supported host offers that ordering (see
``capability.py`` and ``docs/capability-matrix.md``). The mode stays off.

What a host *can* be given is an artifact somebody else already wrote down. A compactor
or spooler that persists an oversized tool result to a file, and describes it with a
manifest, has already done the capture. This module adopts that artifact: it proves the
file is what the manifest says it is, converts it into a context-shunt-owned immutable
snapshot, and hands back an opaque handle. From that point the artifact is
indistinguishable from any other handle - the same reader, the same deterministic
inspect, the same citation verification, the same TTL, the same accounting.

Trust model
-----------
**Every field of the manifest, and every path inside it, is untrusted input.** The
manifest is a set of *claims*; nothing in it is believed until it has been re-proven:

* the manifest file itself is authorized through the same path policy as the artifact, so
  it cannot be a symlink out of the import roots or a device node;
* the artifact path must canonicalize inside a configured import root, be a regular
  non-symlinked un-hardlinked file, and survive the secret-path and denylist policy;
* the bytes are read through a pinned descriptor with ``O_NOFOLLOW`` and an identity
  re-check on both sides of the read, so a swap mid-read fails closed rather than mixing
  two versions;
* the declared size and digest are compared against the bytes *actually read*, and a
  disagreement refuses the import rather than trusting either side;
* the payload must be UTF-8 text or parseable JSON and must survive the content secret
  policy, exactly like an ordinary capture.

None of that logic is re-implemented here. This module is a sequencer over
``paths.authorize``, ``snapshot.snapshot_file`` and the store; its own contribution is
the manifest contract, the producer-profile translation, and the ordering guarantee that
nothing is published until every check has passed.

Producer independence
---------------------
``contracts/v1/artifact-import.schema.json`` is the only manifest shape the core owns. A
foreign producer's manifest reaches it through a :class:`ProducerProfile` in
:data:`PRODUCER_PROFILES` - a lookup table of *data*, so no particular producer's
identifiers appear in the core API or in any signature. A profile is a translator, not an
authorization: a manifest whose declared schema is absent from
``artifact_import.accepted_manifest_schemas`` is refused even when a profile for it
exists.

Failure never widens
--------------------
Every refusal is a bounded :class:`~context_shunt.errors.ShuntError` with a fixed token
detail. No refusal carries the manifest, the path, the payload or any producer-supplied
text, and no refusal returns the artifact contents. There is no partial import: either one
handle exists, or none does.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator

from .binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE
from .errors import ShuntError
from .limits import CONTRACTS_DIR, DEFAULT_LIMITS, Limits, raw_limits
from .paths import PathPolicy, assert_no_secret, authorize
from .registry import RegisteredSource, SourceRegistry
from .snapshot import snapshot_file

#: The manifest shape this core owns. Every accepted document is either already in this
#: shape or normalized into it by a profile before it is validated.
NATIVE_IMPORT_CONTRACT = "context_shunt.artifact_import.v1"

#: Keys a document may declare its own shape with. Checked in order; the first present
#: wins. ``import_contract`` is this core's own discriminator, ``schema`` is the
#: convention foreign producers use.
_DISCRIMINATORS = ("import_contract", "schema")

#: Media types an imported artifact may declare. Kept identical to what the snapshot
#: layer can index, so an import can never introduce a media type the reader, the
#: citation verifier or the inspector would not understand.
_SUPPORTED_MEDIA_TYPES = (TEXT_MEDIA_TYPE, JSON_MEDIA_TYPE)


@cache
def manifest_validator() -> Draft202012Validator:
    with (CONTRACTS_DIR / "artifact-import.schema.json").open("rb") as fh:
        return Draft202012Validator(json.load(fh))


def max_manifest_bytes() -> int:
    return int(raw_limits()["artifact_import"]["max_manifest_bytes"])


# -- the normalized manifest ------------------------------------------------


@dataclass(frozen=True)
class ArtifactManifest:
    """One validated, normalized import manifest. Still only *claims* about a file."""

    producer_id: str
    manifest_schema: str
    artifact_path: str
    declared_bytes: int
    declared_sha256: str
    media_type: str
    origin_tool: str | None
    upstream_truncated: bool

    @classmethod
    def from_native(cls, document: Mapping[str, Any]) -> ArtifactManifest:
        producer = document["producer"]
        artifact = document["artifact"]
        origin = document.get("origin") or {}
        return cls(
            producer_id=str(producer["id"]),
            manifest_schema=str(producer["manifest_schema"]),
            artifact_path=str(artifact["path"]),
            declared_bytes=int(artifact["bytes"]),
            declared_sha256=str(artifact["sha256"]),
            media_type=str(artifact["media_type"]),
            origin_tool=str(origin["tool"]) if origin.get("tool") else None,
            upstream_truncated=bool(origin.get("upstream_truncated", False)),
        )

    def receipt(self, *, artifact_sha256: str, byte_count: int) -> dict[str, Any]:
        """The envelope's import receipt.

        ``artifact_sha256`` and ``bytes`` are the values derived from the bytes actually
        read, never the ones the manifest claimed - by the time this is built the two have
        been proven equal, and recording the derived side keeps that true if the proof
        ever moves.
        """
        receipt: dict[str, Any] = {
            "producer": self.producer_id,
            "manifest_schema": self.manifest_schema,
            "artifact_sha256": artifact_sha256,
            "bytes": byte_count,
            "upstream_truncated": self.upstream_truncated,
        }
        if self.origin_tool:
            receipt["origin_tool"] = self.origin_tool
        return receipt


# -- producer profiles ------------------------------------------------------

#: A profile turns one foreign manifest shape into a native document. It is pure: it
#: reads a mapping and returns a mapping, touches no filesystem, and makes no
#: authorization decision.
ProducerProfile = Callable[[Mapping[str, Any]], dict[str, Any]]


def _native_profile(document: Mapping[str, Any]) -> dict[str, Any]:
    return dict(document)


def _artifact_uri_to_path(value: Any) -> str:
    """Accept a ``file://`` URI or a plain path, and never invent one.

    A non-``file`` scheme is refused rather than reinterpreted as a relative path: a
    producer pointing at ``https://`` means something this boundary does not do, and
    silently treating the string as a filename would be the wrong kind of forgiving. The
    percent-decoded result is returned as-is; whether it is absolute and inside an import
    root is decided later by the path policy, not here.
    """
    if not isinstance(value, str) or not value:
        raise ShuntError("INVALID_REQUEST", "MANIFEST_SCHEMA_VIOLATION", retryable=False)
    if "://" not in value:
        return value
    parts = urlsplit(value)
    if parts.scheme != "file" or parts.netloc not in ("", "localhost") or parts.query or parts.fragment:
        raise ShuntError("INVALID_REQUEST", "MANIFEST_UNSUPPORTED_URI", retryable=False)
    return unquote(parts.path)


def _strip_digest_prefix(value: Any) -> Any:
    """``sha256:<hex>`` and bare ``<hex>`` are the same claim; the contract stores hex."""
    if isinstance(value, str) and value.startswith("sha256:"):
        return value[len("sha256:") :]
    return value


def _tool_result_artifact_profile(document: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a producer that describes a persisted tool result.

    This is the shape a tool-result compactor/spooler emits: an artifact locator, a size,
    a digest, a content type, and which upstream tool produced the payload. The schema
    identifier it is registered under is producer-supplied *data* in
    :data:`PRODUCER_PROFILES`, not part of any signature here, and registering a
    translator grants nothing - the deployment still has to allowlist the schema.
    """
    artifact = document.get("artifact")
    source = document.get("source") or {}
    if not isinstance(artifact, Mapping) or not isinstance(source, Mapping):
        raise ShuntError("INVALID_REQUEST", "MANIFEST_SCHEMA_VIOLATION", retryable=False)
    native: dict[str, Any] = {
        "import_contract": NATIVE_IMPORT_CONTRACT,
        "producer": {
            "id": document.get("producer_id", "tool-result-compactor"),
            "manifest_schema": document.get("schema"),
        },
        "artifact": {
            "path": _artifact_uri_to_path(artifact.get("uri", artifact.get("path"))),
            "bytes": artifact.get("size_bytes", artifact.get("bytes")),
            "sha256": _strip_digest_prefix(artifact.get("digest", artifact.get("sha256"))),
            "media_type": artifact.get("content_type", artifact.get("media_type")),
        },
    }
    tool = source.get("tool_name", source.get("tool"))
    if tool is not None:
        native["origin"] = {
            "tool": tool,
            "upstream_truncated": bool(source.get("truncated", False)),
        }
    created = document.get("created_at")
    if created is not None:
        native["created_at"] = created
    return native


#: Schema identifier -> translator. Data, not API: adding a producer here teaches the
#: core to *read* that shape and nothing more. Authorization is
#: ``artifact_import.accepted_manifest_schemas``, which defaults to empty.
PRODUCER_PROFILES: dict[str, ProducerProfile] = {
    NATIVE_IMPORT_CONTRACT: _native_profile,
    "hermes.tool_result_artifact_manifest.v1": _tool_result_artifact_profile,
}


def declared_schema(document: Any) -> str:
    """The schema a document claims to be, as a bounded string, or a refusal."""
    if not isinstance(document, Mapping):
        raise ShuntError("INVALID_REQUEST", "MANIFEST_NOT_OBJECT", retryable=False)
    for key in _DISCRIMINATORS:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            if len(value.encode("utf-8")) > 128:
                raise ShuntError("INVALID_REQUEST", "MANIFEST_SCHEMA_VIOLATION", retryable=False)
            return value.strip()
    raise ShuntError("INVALID_REQUEST", "MANIFEST_SCHEMA_UNDECLARED", retryable=False)


def normalize_manifest(document: Any, *, accepted_schemas: tuple[str, ...]) -> ArtifactManifest:
    """Validate and normalize one untrusted manifest document.

    Order is deliberate. The document says what it is; a shape with no registered
    translator is refused before anything reads its fields, and a shape this deployment
    has not allowlisted is refused before its translator runs - reading a producer's
    manifest is a capability, not a courtesy.
    """
    schema = declared_schema(document)
    profile = PRODUCER_PROFILES.get(schema)
    if profile is None:
        raise ShuntError("INVALID_REQUEST", "MANIFEST_SCHEMA_UNKNOWN", retryable=False)
    if schema not in accepted_schemas:
        raise ShuntError("INVALID_REQUEST", "MANIFEST_SCHEMA_NOT_ALLOWED", retryable=False)

    native = profile(document)
    if not manifest_validator().is_valid(native):
        raise ShuntError("INVALID_REQUEST", "MANIFEST_SCHEMA_VIOLATION", retryable=False)
    manifest = ArtifactManifest.from_native(native)
    if manifest.media_type not in _SUPPORTED_MEDIA_TYPES:
        # Unreachable through the schema's enum today; asserted so widening the enum
        # cannot quietly introduce a media type the snapshot layer cannot index.
        raise ShuntError("INVALID_REQUEST", "MANIFEST_SCHEMA_VIOLATION", retryable=False)
    # The translated schema identifier has to be the one that was authorized. A profile
    # that rewrote it could otherwise launder an unallowlisted shape into the receipt.
    if manifest.manifest_schema != schema:
        raise ShuntError("INVALID_REQUEST", "MANIFEST_SCHEMA_VIOLATION", retryable=False)
    return manifest


# -- the import itself ------------------------------------------------------


@dataclass(frozen=True)
class ImportOutcome:
    """What an import produced: one handle, plus the receipt describing where it came from."""

    source: RegisteredSource
    manifest: ArtifactManifest
    receipt: dict[str, Any]

    @property
    def byte_count(self) -> int:
        return self.source.snapshot.bytes_len


class ArtifactImporter:
    """Adopts an external producer's artifact as an immutable context-shunt snapshot."""

    def __init__(
        self,
        registry: SourceRegistry,
        *,
        policy: PathPolicy,
        accepted_schemas: tuple[str, ...],
        limits: Limits = DEFAULT_LIMITS,
    ):
        self._registry = registry
        self._policy = policy
        self._accepted = accepted_schemas
        self._limits = limits

    # -- manifest sources -------------------------------------------------

    def read_manifest_file(self, manifest_path: str) -> ArtifactManifest:
        """Read a manifest that lives on disk, under the same policy as the artifact.

        The manifest is the one file in this boundary a naive implementation would open
        directly, which is exactly why it goes through ``authorize`` and ``snapshot_file``
        first: an unauthorized ``open`` here would follow a symlink out of the import
        roots and read whatever it pointed at, before any of the artifact checks ran.
        """
        authorized = authorize(manifest_path, self._policy)
        manifest_limits = self._limits.narrow(
            max_source_bytes=min(self._limits.max_source_bytes, max_manifest_bytes())
        )
        try:
            snapshot = snapshot_file(
                authorized, limits=manifest_limits, media_type_hint=JSON_MEDIA_TYPE
            )
        except ShuntError as exc:
            if exc.code == "LIMIT_EXCEEDED" and exc.detail == "SOURCE_OVER_BYTE_CAP":
                raise ShuntError(
                    "LIMIT_EXCEEDED", "MANIFEST_OVER_BYTE_CAP", retryable=False
                ) from None
            if exc.code == "UNSAFE_SOURCE" and exc.detail == "INVALID_JSON":
                raise ShuntError(
                    "INVALID_REQUEST", "MANIFEST_NOT_JSON", retryable=False
                ) from None
            raise
        return normalize_manifest(snapshot.json_value, accepted_schemas=self._accepted)

    def normalize(self, document: Any) -> ArtifactManifest:
        """Normalize a manifest the caller already holds as a parsed document.

        The document form has to be held to the same standard as the file form, and one
        check is not automatic here: ``read_manifest_file`` runs the content secret policy
        over the manifest's bytes on its way through ``snapshot_file``, and a document that
        never touched the filesystem skipped it. The receipt's identifier patterns permit
        enough punctuation for a token-shaped ``producer`` id to be schema-valid, so the
        same policy runs over the serialized document before anything is normalized.
        """
        try:
            material = json.dumps(document, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            raise ShuntError("INVALID_REQUEST", "MANIFEST_NOT_JSON", retryable=False) from None
        assert_no_secret(material.encode("utf-8"), "MANIFEST")
        return normalize_manifest(document, accepted_schemas=self._accepted)

    # -- adoption ----------------------------------------------------------

    def adopt(self, session_id: str, manifest: ArtifactManifest) -> ImportOutcome:
        """Prove the artifact matches the manifest, then publish exactly one handle.

        Everything that could reject the artifact runs before publication, for the same
        reason the spill engine validates first: a rejected import must not leave an
        orphaned blob or a handle for a request the caller will be told was refused.
        """
        authorized = authorize(manifest.artifact_path, self._policy)
        if authorized.size != manifest.declared_bytes:
            # Cheap pre-check on the pinned stat, before reading up to 8 MiB. The
            # authoritative comparison is against the bytes actually read, below.
            raise ShuntError("SOURCE_CHANGED", "ARTIFACT_SIZE_MISMATCH", retryable=False)
        snapshot = snapshot_file(
            authorized, limits=self._limits, media_type_hint=manifest.media_type
        )
        if snapshot.bytes_len != manifest.declared_bytes:
            raise ShuntError("SOURCE_CHANGED", "ARTIFACT_SIZE_MISMATCH", retryable=False)
        derived = _bare_digest(snapshot.snapshot_id)
        if derived != manifest.declared_sha256:
            # The producer described different bytes than the ones on disk. Either the
            # artifact was rewritten or the manifest is wrong; neither is importable, and
            # answering from these bytes under that digest would be a lie.
            raise ShuntError("SOURCE_CHANGED", "ARTIFACT_HASH_MISMATCH", retryable=False)

        entry = self._registry.register(
            session_id,
            snapshot,
            # Same recursion guard the spill engine uses: an imported artifact is a
            # payload held *out* of the context, so a pointer to it must never itself be
            # re-imported or re-spilled. Store-verified, never trusted from a payload.
            internal=True,
            # The store's `kind` records the capture *category* - an oversized post-tool
            # payload withheld from the context - which is exactly what this is. Which
            # producer it came from is deliberately not stored: `contracts/store/v1.sql`
            # holds no producer identity by design, so that distinction travels in the
            # envelope receipt and in the `capture` accounting kind instead.
            kind="spilled_tool",
        )
        return ImportOutcome(
            source=entry,
            manifest=manifest,
            receipt=manifest.receipt(artifact_sha256=derived, byte_count=snapshot.bytes_len),
        )


def _bare_digest(snapshot_id: str) -> str:
    """``sha256:<hex>`` from a snapshot id, as the bare hex the manifest contract uses."""
    return snapshot_id.split(":", 1)[-1]


def import_policy(roots: tuple[str, ...], denylist: tuple[str, ...] = ()) -> PathPolicy:
    """Build the import-root policy, or refuse when a deployment configured none."""
    if not roots:
        raise ShuntError("INVALID_REQUEST", "NO_IMPORT_ROOT", retryable=False)
    return PathPolicy.from_config([str(Path(root)) for root in roots], list(denylist))


__all__ = [
    "NATIVE_IMPORT_CONTRACT",
    "PRODUCER_PROFILES",
    "ArtifactImporter",
    "ArtifactManifest",
    "ImportOutcome",
    "ProducerProfile",
    "declared_schema",
    "import_policy",
    "manifest_validator",
    "max_manifest_bytes",
    "normalize_manifest",
]
