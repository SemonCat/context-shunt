"""Optional Suma oversized-result mode: pure spill and pointer.

This engine never summarizes. It has no model bridge at all, which is the point: the old
heuristic compactor is replaced, not chained. An oversized result is written to a private
cache, read back and hash-verified, registered as an internal source, and only then
replaced with a pointer envelope. Reading it later goes through the question-driven
reader like any other source.

Failure never falls back to the raw payload. Quota exhaustion, a full disk, a permission
error, an unserializable value and a readback mismatch all produce the same bounded
``SPILL_FAILED`` envelope.

Enabling this on a host additionally requires proof that the host captures the complete
result before truncation and accepts a safe replacement before persistence and context
insertion. See ``capability.py``; on both supported hosts that proof does not exist and
the mode stays disabled.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import envelope as E
from .binaryguard import assert_supported_blocks
from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits
from .registry import SourceRegistry
from .snapshot import json_depth_and_nodes, snapshot_bytes

_DIR_MODE = 0o700
_FILE_MODE = 0o600


@dataclass(frozen=True)
class SpillOutcome:
    action: str  # passthrough | spill | blocked | error
    envelope: dict[str, Any] | None = None
    code: str | None = None
    bytes_measured: int = 0


class SpillStore:
    """Private, quota'd, TTL'd cache outside every workspace root."""

    def __init__(self, root: Path, limits: Limits = DEFAULT_LIMITS):
        self._root = Path(root)
        self._limits = limits
        self._lock = threading.RLock()
        self._used_by_session: dict[str, int] = {}

    @property
    def root(self) -> Path:
        return self._root

    def used_bytes(self, session_id: str) -> int:
        with self._lock:
            return self._used_by_session.get(session_id, 0)

    def seed_usage(self, session_id: str, used: int) -> None:
        with self._lock:
            self._used_by_session[session_id] = used

    def _session_dir(self, session_id: str) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        _assert_private_directory(self._root)
        safe = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        path = self._root / safe
        path.mkdir(parents=True, exist_ok=True)
        _assert_private_directory(path)
        if path.resolve(strict=True).parent != self._root.resolve(strict=True):
            raise ShuntError("SPILL_FAILED", "UNSAFE_CACHE_PATH", retryable=False)
        return path

    def write(self, session_id: str, data: bytes) -> Path:
        """Atomically publish, then read back and verify before the caller may use it."""
        with self._lock:
            used = self._used_by_session.get(session_id, 0)
            if used + len(data) > self._limits.session_spill_quota_bytes:
                raise ShuntError("SPILL_FAILED", "QUOTA_EXCEEDED", retryable=False)
            tmp_name: str | None = None
            try:
                directory = self._session_dir(session_id)
                digest = hashlib.sha256(data).hexdigest()
                final = directory / f"{digest}.spill"
                tmp_fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".part")
                with os.fdopen(tmp_fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp_name, _FILE_MODE)
                os.replace(tmp_name, final)
            except (OSError, NotImplementedError):
                if tmp_name is not None:
                    _unlink_quiet(Path(tmp_name))
                raise ShuntError("SPILL_FAILED", "WRITE_FAILED", retryable=False) from None
            try:
                readback = final.read_bytes()
            except OSError:
                _unlink_quiet(final)
                raise ShuntError("SPILL_FAILED", "READBACK_FAILED", retryable=False) from None
            if hashlib.sha256(readback).hexdigest() != digest:
                _unlink_quiet(final)
                raise ShuntError("SPILL_FAILED", "READBACK_MISMATCH", retryable=False)
            self._used_by_session[session_id] = used + len(data)
            return final

    def purge_session(self, session_id: str) -> int:
        """Remove a session's artifacts. This is deletion, not secure erasure."""
        with self._lock:
            directory = self._root / hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
            removed = 0
            try:
                root_stat = self._root.lstat()
            except OSError:
                self._used_by_session.pop(session_id, None)
                return 0
            if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
                self._used_by_session.pop(session_id, None)
                return 0
            try:
                directory_stat = directory.lstat()
            except OSError:
                self._used_by_session.pop(session_id, None)
                return 0
            if stat.S_ISLNK(directory_stat.st_mode):
                _unlink_quiet(directory)
                removed = 1
            elif stat.S_ISDIR(directory_stat.st_mode):
                try:
                    children = list(directory.iterdir())
                except OSError:
                    children = []
                for child in children:
                    try:
                        child_stat = child.lstat()
                    except OSError:
                        continue
                    if stat.S_ISREG(child_stat.st_mode) or stat.S_ISLNK(child_stat.st_mode):
                        _unlink_quiet(child)
                        removed += 1
                with contextlib.suppress(OSError):
                    # A concurrent write may repopulate it; a later teardown gets it.
                    directory.rmdir()
            self._used_by_session.pop(session_id, None)
        return removed


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _assert_private_directory(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except (OSError, NotImplementedError):
        raise ShuntError("SPILL_FAILED", "UNSAFE_CACHE_PATH", retryable=False) from None
    if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise ShuntError("SPILL_FAILED", "UNSAFE_CACHE_PATH", retryable=False)
    try:
        os.chmod(path, _DIR_MODE, follow_symlinks=False)
    except (OSError, NotImplementedError):
        raise ShuntError("SPILL_FAILED", "PERMISSION_FAILED", retryable=False) from None


class SumaSpillEngine:
    """Sizing and spilling for a complete tool/MCP result."""

    def __init__(
        self,
        store: SpillStore,
        registry: SourceRegistry,
        *,
        limits: Limits = DEFAULT_LIMITS,
        enabled: bool = False,
    ):
        self._store = store
        self._registry = registry
        self._limits = limits
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def evaluate(
        self,
        session_id: str,
        request_id: str,
        result: Any,
        *,
        internal_source_id: str | None = None,
    ) -> SpillOutcome:
        """Decide what a host should do with one complete tool result."""
        if not self._enabled:
            return SpillOutcome(action="passthrough")
        if internal_source_id and self._registry.is_internal(session_id, internal_source_id):
            # Registry-verified internal envelope: never spill our own pointer again.
            return SpillOutcome(action="passthrough")
        try:
            serialized = self._serialize(result)
        except ShuntError as exc:
            return SpillOutcome(
                action="blocked" if exc.code == "BINARY_UNSUPPORTED" else "error",
                envelope=E.error_envelope(request_id, exc),
                code=exc.code,
            )
        except Exception:
            safe = ShuntError("SPILL_FAILED", "SERIALIZE_FAILED", retryable=False)
            return SpillOutcome(
                action="error",
                envelope=E.error_envelope(request_id, safe),
                code=safe.code,
            )

        size = len(serialized)
        if size > self._limits.max_source_bytes:
            exc = ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", retryable=False)
            return SpillOutcome(
                action="error",
                envelope=E.error_envelope(request_id, exc),
                code=exc.code,
                bytes_measured=size,
            )
        if size <= self._limits.max_tool_result_bytes:
            return SpillOutcome(action="passthrough", bytes_measured=size)

        try:
            # Validate before persistence so a binary/secret/invalid payload cannot leave
            # an orphaned raw artifact after the operation is rejected.
            snapshot = snapshot_bytes(serialized, media_type_hint="text/plain", limits=self._limits)
            entry = self._registry.register(session_id, snapshot, internal=True)
            try:
                self._store.write(session_id, serialized)
            except Exception:
                self._registry.remove(session_id, entry.source_id)
                raise
        except ShuntError as exc:
            code = exc.code if exc.code in ("SPILL_FAILED", "UNSAFE_SOURCE") else "SPILL_FAILED"
            safe = ShuntError(code, exc.detail, retryable=False)
            return SpillOutcome(
                action="error",
                envelope=E.error_envelope(request_id, safe),
                code=code,
                bytes_measured=size,
            )
        except Exception:
            safe = ShuntError("SPILL_FAILED", "WRITE_FAILED", retryable=False)
            return SpillOutcome(
                action="error",
                envelope=E.error_envelope(request_id, safe),
                code=safe.code,
                bytes_measured=size,
            )

        pointer = {
            "source_id": entry.source_id,
            "snapshot_id": entry.snapshot.snapshot_id,
            "bytes": size,
            "expires_at": E.iso_expiry(entry.expires_at_epoch),
            "internal": True,
        }
        env = E.build(
            request_id=request_id,
            status="ok",
            code="SPILLED",
            coverage=E.Coverage(upstream_truncated=None),
            sources=[
                {
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "media_type": entry.snapshot.media_type,
                    "bytes": size,
                    "expires_at": pointer["expires_at"],
                }
            ],
            retryable=False,
            pointer=pointer,
            guidance=(
                "The tool result was too large for this conversation and was moved out of it. "
                "Ask the context-shunt reader a question about this pointer to get a cited answer."
            ),
        )
        return SpillOutcome(action="spill", envelope=env, code="SPILLED", bytes_measured=size)

    def _serialize(self, result: Any) -> bytes:
        """Deterministic serialization of string, object, array or content-block results."""
        if isinstance(result, (bytes, bytearray)):
            return bytes(result)
        if isinstance(result, str):
            return result.encode("utf-8")
        blocks = _content_blocks(result)
        if blocks is not None:
            assert_supported_blocks(blocks)
        json_depth_and_nodes(result, self._limits)
        try:
            text = json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_reject
            )
        except (TypeError, ValueError):
            raise ShuntError("LIMIT_EXCEEDED", "UNSERIALIZABLE_RESULT", retryable=False) from None
        return text.encode("utf-8")


def _content_blocks(result: Any) -> list[dict] | None:
    if isinstance(result, dict):
        blocks = result.get("content")
        if isinstance(blocks, list):
            return blocks
    if isinstance(result, list) and any(isinstance(b, dict) and "type" in b for b in result):
        return result
    return None


def _reject(_value: Any) -> Any:
    raise TypeError("unserializable value")
