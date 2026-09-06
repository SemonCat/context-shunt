"""Immutable snapshots and their line / record indexes.

A snapshot binds a SHA-256 to the exact bytes a citation was taken from. Reading is
streamed and capped at ``max_source_bytes`` so an oversized source is refused instead of
buffered, and file identity is re-checked after the read: if the file was swapped
mid-read the snapshot is inconsistent and the request fails with ``SOURCE_CHANGED``
rather than mixing two versions.

JSON sources are indexed by RFC 6901 pointer with a stable record ordinal: element *n*
of an array has ordinal *n+1*, object keys are ordered by a fixed sort, and a scalar is
addressed by a pointer to itself with ordinal 1. Pretty-printed line numbers are never
used as citations for a JSON source.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .binaryguard import JSON_MEDIA_TYPE, TEXT_MEDIA_TYPE, assert_text
from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits
from .paths import AuthorizedPath, assert_no_secret
from .textindex import LineIndex

_READ_CHUNK = 256 * 1024


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    media_type: str
    data: bytes
    line_index: LineIndex
    json_value: Any | None = None

    @property
    def bytes_len(self) -> int:
        return len(self.data)

    @property
    def line_count(self) -> int:
        return self.line_index.line_count


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_bounded(path: Path, max_bytes: int) -> bytes:
    """Stream a file, refusing at the cap instead of allocating past it."""
    out = bytearray()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_READ_CHUNK)
            if not block:
                break
            if len(out) + len(block) > max_bytes:
                raise ShuntError("LIMIT_EXCEEDED", "SOURCE_OVER_BYTE_CAP")
            out.extend(block)
    return bytes(out)


def _read_authorized(authorized: AuthorizedPath, max_bytes: int) -> bytes:
    """Open once, validate the descriptor, then read only that descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(authorized.real, flags)
    except OSError:
        raise ShuntError("SOURCE_CHANGED", "OPEN_FAILED") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
            raise ShuntError("SOURCE_CHANGED", "IDENTITY_CHANGED")
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if before_identity != authorized.identity():
            raise ShuntError("SOURCE_CHANGED", "IDENTITY_CHANGED")

        out = bytearray()
        while True:
            block = os.read(fd, _READ_CHUNK)
            if not block:
                break
            if len(out) + len(block) > max_bytes:
                raise ShuntError("LIMIT_EXCEEDED", "SOURCE_OVER_BYTE_CAP")
            out.extend(block)

        after = os.fstat(fd)
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if after_identity != authorized.identity() or after.st_size != len(out):
            raise ShuntError("SOURCE_CHANGED", "MODIFIED_DURING_READ")
        return bytes(out)
    finally:
        os.close(fd)


def snapshot_file(
    authorized: AuthorizedPath,
    *,
    limits: Limits = DEFAULT_LIMITS,
    media_type_hint: str | None = None,
) -> Snapshot:
    data = _read_authorized(authorized, limits.max_source_bytes)
    return snapshot_bytes(data, media_type_hint=media_type_hint, limits=limits)


def snapshot_bytes(
    data: bytes,
    *,
    media_type_hint: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> Snapshot:
    if len(data) > limits.max_source_bytes:
        raise ShuntError("LIMIT_EXCEEDED", "SOURCE_OVER_BYTE_CAP", retryable=False)
    text = assert_text(data)
    assert_no_secret(data, "SOURCE")
    media_type = media_type_hint or TEXT_MEDIA_TYPE
    json_value: Any | None = None
    if media_type == JSON_MEDIA_TYPE:
        try:
            json_value = json.loads(text)
        except (ValueError, RecursionError):
            raise ShuntError("UNSAFE_SOURCE", "INVALID_JSON") from None
        json_depth_and_nodes(json_value, limits)
    return Snapshot(
        snapshot_id=_digest(data),
        media_type=media_type,
        data=data,
        line_index=LineIndex(data),
        json_value=json_value,
    )


# -- JSON record addressing ------------------------------------------------


def unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_pointer(value: Any, pointer: str) -> Any:
    """RFC 6901 resolution. Raises ``POINTER_NOT_FOUND`` rather than returning a default."""
    if pointer in ("", "/") and pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ShuntError("INVALID_REQUEST", "BAD_POINTER")
    current = value
    for raw in pointer.split("/")[1:]:
        token = unescape_pointer_token(raw)
        if isinstance(current, dict):
            if token not in current:
                raise ShuntError("INVALID_REQUEST", "POINTER_NOT_FOUND")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                raise ShuntError("INVALID_REQUEST", "POINTER_NOT_FOUND")
            idx = int(token)
            if idx >= len(current):
                raise ShuntError("INVALID_REQUEST", "POINTER_NOT_FOUND")
            current = current[idx]
        else:
            raise ShuntError("INVALID_REQUEST", "POINTER_NOT_FOUND")
    return current


def canonical_json(value: Any) -> str:
    """Deterministic serialization: sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_count(node: Any) -> int:
    if isinstance(node, list):
        return len(node)
    if isinstance(node, dict):
        return len(node)
    return 1


def record_at(node: Any, ordinal: int) -> Any:
    """1-based record addressing: array element n has ordinal n+1; object keys sort stably."""
    if ordinal < 1:
        raise ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE")
    if isinstance(node, list):
        if ordinal > len(node):
            raise ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE")
        return node[ordinal - 1]
    if isinstance(node, dict):
        keys = sorted(node.keys())
        if ordinal > len(keys):
            raise ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE")
        key = keys[ordinal - 1]
        return {key: node[key]}
    if ordinal != 1:
        raise ShuntError("INVALID_REQUEST", "RECORD_OUT_OF_RANGE")
    return node


def json_depth_and_nodes(value: Any, limits: Limits = DEFAULT_LIMITS) -> tuple[int, int]:
    """Bounded structural walk. Cycles and oversized structures raise LIMIT_EXCEEDED."""
    max_depth = 0
    nodes = 0
    seen: set[int] = set()
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        max_depth = max(max_depth, depth)
        if depth > limits.json_max_depth:
            raise ShuntError("LIMIT_EXCEEDED", "JSON_TOO_DEEP")
        if nodes > limits.json_max_nodes:
            raise ShuntError("LIMIT_EXCEEDED", "JSON_TOO_MANY_NODES")
        if isinstance(node, (dict, list)):
            ident = id(node)
            if ident in seen:
                raise ShuntError("LIMIT_EXCEEDED", "JSON_CYCLE")
            seen.add(ident)
            if isinstance(node, dict) and any(not isinstance(key, str) for key in node):
                raise ShuntError("LIMIT_EXCEEDED", "JSON_UNSUPPORTED_VALUE")
            children = node.values() if isinstance(node, dict) else node
            for child in children:
                stack.append((child, depth + 1))
        elif (isinstance(node, float) and not math.isfinite(node)) or not isinstance(
            node, (str, int, float, bool, type(None))
        ):
            raise ShuntError("LIMIT_EXCEEDED", "JSON_UNSUPPORTED_VALUE")
    return max_depth, nodes
