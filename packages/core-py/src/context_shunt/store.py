"""Hybrid snapshot store: SQLite owns authorization, private files own the bytes.

Split of responsibility
-----------------------
SQLite holds only authorization and accounting state: opaque handle identity, session
scope and generation, TTL, quotas, content refcounts, disclosure totals, cleanup state
and bounded operation metrics. The immutable raw payload lives in a content-addressed
private file whose location is *derived* from the SHA-256 internally. No source path, no
question, no answer, no payload preview and no provider error body is ever written to the
database, and no filesystem path is stored or exposed.

The schema is not written here. Both language cores execute ``contracts/store/v1.sql``
verbatim, which is what makes the DDL normative and lets the cross-language
interoperability test open one store file with both implementations.

Publication order
-----------------
For a capture batch, in this exact order:

1. the caller validates and authorizes the whole request and captures bounded bytes;
2. every payload is written to a fresh temp file with ``O_CREAT|O_EXCL|O_NOFOLLOW``,
   ``fsync``-ed, ``chmod`` 0600, and atomically renamed into its content-addressed home;
3. one SQLite transaction publishes every handle and takes every refcount together.

No handle is usable before both the payload and its metadata are durable, and a batch
publishes all of its handles or none of them. A crash between (2) and (3) leaves an
orphan blob file with no row, which the sweep collects; it never leaves a usable handle.

Readability is a SQL predicate, never file existence::

    revoked = 0 AND expires_at_ms > :now
    AND scope.closed_at_ms IS NULL AND scope.generation = :generation

so an expired, revoked, closed-scope or stale-generation handle is unreadable the instant
the predicate stops holding, whether or not physical cleanup has run.

Failing closed
--------------
A hash collision or a content mismatch on a shared blob raises ``STORE_FAILED``; it never
deletes the file, because the mismatch may be a shared blob other live handles still
reference and deleting it would turn one corruption into many. Deletion is always
mark-then-sweep against ``refcount = 0``.

Clocks
------
``expires_at_ms`` is wall-clock UTC milliseconds, which is what a second process and a
restart can compare. A clock rollback cannot revive an expired handle: every reading
takes ``max(wall_clock, clock_high_water_ms)``, and the high-water mark is persisted
inside the write transactions the store is already taking. The request-scoped
``MonotonicClock``/``Deadline`` pair is unrelated and stays process-local.

Legacy artifacts
----------------
The pre-1.1 spill layout wrote ``<root>/<32-hex>/<digest>.spill``. Those files are *never*
imported as authorized handles - an unauthenticated file on disk is not a capability.
:meth:`SnapshotStore.legacy_artifact_count` reports them and
:meth:`SnapshotStore.purge_legacy_artifacts` removes them, both only when a caller asks.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits, store_ddl

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_DB_NAME = "store.sqlite3"

#: Opening a brand-new store races on the WAL transition, which is exclusive and answers
#: a competitor with SQLITE_BUSY immediately. The transition happens once, so a handful of
#: short retries is enough; this is not a substitute for `busy_timeout`, which covers the
#: ordinary write contention that follows.
_OPEN_ATTEMPTS = 6
_OPEN_BACKOFF_S = 0.02
_BLOB_DIR = "blobs"
_TMP_DIR = "tmp"
_READ_CHUNK = 256 * 1024

#: Blob file names are ``<hash>.bin``; legacy spill artifacts ended in ``.spill``.
_BLOB_SUFFIX = ".bin"
_LEGACY_SUFFIX = ".spill"

#: How far the clock may advance before the high-water mark is written back. Small enough
#: that a restart after a rollback loses at most this much protection, large enough that a
#: read-heavy session is not turned into a write-heavy one.
_HIGH_WATER_GRANULARITY_MS = 1000

HANDLE_KINDS = frozenset({"shunted_read", "spilled_tool"})
DISCLOSURE_KINDS = frozenset({"lines", "bytes", "search"})


# -- identities -------------------------------------------------------------


@dataclass(frozen=True)
class ScopeIdentity:
    """The trusted identity a handle is scoped to.

    Every component comes from the host, never from a payload or a tool argument. The
    components are digested before storage so no session name, account id or profile
    label is retained. ``generation`` makes a stale or foreign handle unreplayable: a
    reset bumps the generation and every earlier handle stops matching the predicate.
    """

    host: str
    profile: str
    principal: str
    session: str
    generation: int = 1

    def __post_init__(self) -> None:
        if not self.host or not self.session:
            raise ShuntError("STORE_FAILED", "SCOPE_INCOMPLETE", retryable=False)
        if not isinstance(self.generation, int) or isinstance(self.generation, bool):
            raise ShuntError("STORE_FAILED", "SCOPE_INCOMPLETE", retryable=False)
        if self.generation < 1:
            raise ShuntError("STORE_FAILED", "SCOPE_INCOMPLETE", retryable=False)

    @property
    def scope_id(self) -> str:
        return "scp_" + self._digest(self._material())[:32]

    def _material(self) -> str:
        return "\x1f".join(
            (
                self.host,
                self.profile,
                self.principal,
                self.session,
                str(self.generation),
            )
        )

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def columns(self) -> tuple[str, str, str, str, int]:
        return (
            self._digest(self.host),
            self._digest(self.profile),
            self._digest(self.principal),
            self._digest(self.session),
            self.generation,
        )


@dataclass(frozen=True)
class Capture:
    """One payload offered for publication.

    ``data`` is already validated, already bounded and already proven text by the caller.
    The store checks size against the configured caps and nothing else about its meaning.
    """

    data: bytes
    media_type: str
    line_count: int
    kind: str = "shunted_read"
    internal: bool = False

    def __post_init__(self) -> None:
        if self.kind not in HANDLE_KINDS:
            raise ShuntError("STORE_FAILED", "BAD_HANDLE_KIND", retryable=False)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class PublishedHandle:
    handle_id: str
    scope_id: str
    blob_hash: str
    media_type: str
    bytes_len: int
    line_count: int
    kind: str
    internal: bool
    created_at_ms: int
    expires_at_ms: int

    @property
    def snapshot_id(self) -> str:
        return f"sha256:{self.blob_hash}"

    @property
    def expires_at_epoch(self) -> float:
        return self.expires_at_ms / 1000.0


@dataclass(frozen=True)
class DisclosureAllowance:
    per_source_remaining: int
    per_session_remaining: int

    @property
    def remaining(self) -> int:
        return max(0, min(self.per_source_remaining, self.per_session_remaining))

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


@dataclass(frozen=True)
class DisclosureCharge:
    granted: bool
    charged_bytes: int
    disclosed_bytes_source: int
    disclosed_bytes_session: int
    limit_reached: bool


@dataclass(frozen=True)
class OperationRecord:
    """Bounded non-content metadata for one shunt operation.

    Every field is a closed enum, a byte count or a token count. ``None`` in a token
    column means "not reported" and is stored as SQL NULL so it can never be read back
    as zero.
    """

    operation_id: str
    kind: str
    status: str
    code: str
    raw_input_bytes: int
    raw_input_baseline_tokens: int | None
    baseline_kind: str
    baseline_method: str
    baseline_credit_tokens: int
    main_model_envelope_bytes: int
    main_model_envelope_tokens: int
    envelope_token_method: str
    reader_input_tokens: int | None
    reader_output_tokens: int | None
    reader_cache_tokens: int | None
    reader_token_method: str
    attempts_started: int
    attempts_usage_complete: int
    delivery_boundary: str
    main_context_tokens_saved: int
    net_tokens_saved: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "kind": self.kind,
            "status": self.status,
            "code": self.code,
            "raw_input_bytes": self.raw_input_bytes,
            "raw_input_baseline_tokens": self.raw_input_baseline_tokens,
            "baseline_kind": self.baseline_kind,
            "baseline_method": self.baseline_method,
            "baseline_credit_tokens": self.baseline_credit_tokens,
            "main_model_envelope_bytes": self.main_model_envelope_bytes,
            "main_model_envelope_tokens": self.main_model_envelope_tokens,
            "envelope_token_method": self.envelope_token_method,
            "reader_input_tokens": self.reader_input_tokens,
            "reader_output_tokens": self.reader_output_tokens,
            "reader_cache_tokens": self.reader_cache_tokens,
            "reader_token_method": self.reader_token_method,
            "attempts_started": self.attempts_started,
            "attempts_usage_complete": self.attempts_usage_complete,
            "delivery_boundary": self.delivery_boundary,
            "main_context_tokens_saved": self.main_context_tokens_saved,
            "net_tokens_saved": self.net_tokens_saved,
        }


@dataclass(frozen=True)
class SweepReport:
    expired_handles: int = 0
    deleted_blobs: int = 0
    removed_temps: int = 0
    orphan_blob_files: int = 0


@dataclass
class StoreStats:
    handles: int = 0
    blobs: int = 0
    bytes: int = 0


def new_operation_id() -> str:
    return "acc_" + secrets.token_hex(8)


def _mint_handle_id() -> str:
    return "src_" + secrets.token_hex(8)


# -- the store --------------------------------------------------------------


class SnapshotStore:
    """Cross-process safe hybrid store. One instance per process per cache root."""

    def __init__(
        self,
        root: Path | str,
        limits: Limits = DEFAULT_LIMITS,
        *,
        wall_clock_ms=None,
    ):
        self._root = Path(root)
        self._limits = limits
        self._wall = wall_clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._high_water = 0
        self._persisted_high_water = 0
        self._prepare_directories()

    # -- lifecycle ---------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
                self._conn = None

    def _prepare_directories(self) -> None:
        for path in (self._root, self._root / _BLOB_DIR, self._root / _TMP_DIR):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise ShuntError("STORE_FAILED", "UNSAFE_CACHE_PATH", retryable=False) from None
            _assert_private_directory(path)

    def _connect(self) -> sqlite3.Connection:
        """Open the store, tolerating another process opening it at the same moment.

        The normative DDL sets ``PRAGMA journal_mode = WAL``, and it runs on every fresh
        connection. Switching a database into WAL needs a brief *exclusive* lock, and
        SQLite answers a competing attempt with ``SQLITE_BUSY`` straight away -
        ``busy_timeout`` does not cover the journal-mode transition. Two processes opening
        a new store together therefore raced, and the loser failed the whole operation
        with ``OPEN_FAILED``.

        The transition only has to happen once: whoever wins leaves the database in WAL,
        and every later connection finds it already there and needs no exclusive lock. So
        the contention is genuinely transient and a bounded retry resolves it, without
        weakening the DDL or moving the pragma out of the contract.
        """
        if self._conn is not None:
            return self._conn
        last: Exception | None = None
        for attempt in range(_OPEN_ATTEMPTS):
            conn = None
            try:
                conn = sqlite3.connect(
                    self._root / _DB_NAME,
                    timeout=self._limits.store_busy_timeout_ms / 1000.0,
                    isolation_level=None,  # explicit transactions only
                    check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
                conn.execute(f"PRAGMA busy_timeout = {int(self._limits.store_busy_timeout_ms)}")
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA synchronous = FULL")
                # Before the DDL, not after: revision 2 indexes a column revision 1 does
                # not have, so executing the script first fails on a store that still
                # needs the column added.
                self._migrate_schema(conn)
                conn.executescript(store_ddl())
                with contextlib.suppress(OSError):
                    os.chmod(self._root / _DB_NAME, _FILE_MODE)
            except sqlite3.OperationalError as exc:
                last = exc
                if conn is not None:
                    with contextlib.suppress(sqlite3.Error):
                        conn.close()
                if attempt + 1 == _OPEN_ATTEMPTS:
                    raise ShuntError("STORE_FAILED", "OPEN_FAILED", retryable=False) from None
                time.sleep(_OPEN_BACKOFF_S * (attempt + 1))
                continue
            except (sqlite3.Error, OSError):
                if conn is not None:
                    with contextlib.suppress(sqlite3.Error):
                        conn.close()
                raise ShuntError("STORE_FAILED", "OPEN_FAILED", retryable=False) from None
            self._conn = conn
            self._bootstrap_metadata(conn)
            return conn
        raise ShuntError("STORE_FAILED", "OPEN_FAILED", retryable=False) from last

    def _bootstrap_metadata(self, conn: sqlite3.Connection) -> None:
        """Seed the singletons and refuse a store written by an incompatible revision."""
        try:
            with _write_txn(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO store_metadata (key, value) VALUES (?, ?)",
                    ("ddl_version", str(self._limits.store_ddl_version)),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO store_metadata (key, value) VALUES (?, ?)",
                    ("store_id", secrets.token_hex(16)),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO store_metadata (key, value) VALUES (?, ?)",
                    ("clock_high_water_ms", "0"),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO store_metadata (key, value) VALUES (?, ?)",
                    ("cursor_key", secrets.token_hex(32)),
                )
        except sqlite3.Error:
            raise ShuntError("STORE_FAILED", "MIGRATION_FAILED", retryable=False) from None
        found = self._metadata(conn, "ddl_version")
        if found != str(self._limits.store_ddl_version):
            if found != "1" or str(self._limits.store_ddl_version) != "2":
                # A store from an unknown revision is refused rather than migrated in
                # place by guesswork; docs/install.md documents the supported path.
                raise ShuntError("STORE_FAILED", "DDL_VERSION_MISMATCH", retryable=False)
            try:
                with _write_txn(conn):
                    conn.execute(
                        "UPDATE store_metadata SET value = ? WHERE key = 'ddl_version'",
                        (str(self._limits.store_ddl_version),),
                    )
            except sqlite3.Error:
                raise ShuntError("STORE_FAILED", "MIGRATION_FAILED", retryable=False) from None
        self._high_water = int(self._metadata(conn, "clock_high_water_ms") or "0")
        self._persisted_high_water = self._high_water

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Add what a newer revision needs, before the DDL script runs. Never destructive.

        Only additive steps, and only between revisions this core knows how to bridge.
        Revision 1 to 2 adds the durable disclosure identity: ``disclosure_events`` gains a
        nullable ``blob_hash``. The contract DDL creates that column for a *new* store and
        also indexes it, which is why this has to run first - the index cannot be built on
        a table that still lacks the column.

        A revision-1 row keeps a null ``blob_hash``. That is honest rather than convenient:
        the content it disclosed is genuinely unattributable now, so it still counts toward
        the session ceiling - where it was always counted - and is never credited to a
        specific source's ceiling, which would mean inventing the identity it lacks.
        """
        try:
            has_metadata = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_metadata'"
            ).fetchone()
            if has_metadata is None:
                return  # A brand-new store; the DDL builds revision 2 directly.
            found = self._metadata(conn, "ddl_version")
            if found != "1" or str(self._limits.store_ddl_version) != "2":
                return  # Not a bridge this core knows; `_bootstrap_metadata` decides.
            columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(disclosure_events)")
            }
            if columns and "blob_hash" not in columns:
                with _write_txn(conn):
                    conn.execute("ALTER TABLE disclosure_events ADD COLUMN blob_hash TEXT")
        except sqlite3.Error:
            raise ShuntError("STORE_FAILED", "MIGRATION_FAILED", retryable=False) from None

    @staticmethod
    def _metadata(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM store_metadata WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def ddl_version(self) -> int:
        with self._lock:
            return int(self._metadata(self._connect(), "ddl_version") or "0")

    def cursor_key(self) -> bytes:
        """Per-store secret used to authenticate continuation cursors."""
        with self._lock:
            value = self._metadata(self._connect(), "cursor_key")
        if not value:
            raise ShuntError("STORE_FAILED", "CURSOR_KEY_MISSING", retryable=False)
        return bytes.fromhex(value)

    # -- clock -------------------------------------------------------------

    def now_ms(self) -> int:
        """Non-decreasing wall-clock milliseconds.

        A backwards clock yields the high-water mark instead, so a handle that has expired
        stays expired across a rollback, a restart or a second process.
        """
        return self._tick()

    def _tick(self) -> int:
        """Read the clock and keep the persisted high-water mark roughly current.

        Read paths take no write transaction of their own, so without this a long run of
        pure reads would leave the persisted mark far behind, and a restart after a clock
        rollback could revive an expired handle. Persisting is amortised: it happens only
        once the mark has advanced past ``_HIGH_WATER_GRANULARITY_MS``, so an ordinary
        read stays a read.
        """
        with self._lock:
            conn = self._connect()
            now = self._now_locked(conn)
            if now >= self._persisted_high_water + _HIGH_WATER_GRANULARITY_MS:
                try:
                    with _write_txn(conn):
                        self._bump_high_water(conn, now)
                except sqlite3.Error:
                    # A busy store just means another writer is ahead of us; the in-memory
                    # mark still protects this process and the next writer persists it.
                    pass
            return now

    def _now_locked(self, conn: sqlite3.Connection) -> int:
        stored = int(self._metadata(conn, "clock_high_water_ms") or "0")
        self._high_water = max(self._high_water, stored)
        self._persisted_high_water = max(self._persisted_high_water, stored)
        return max(int(self._wall()), self._high_water)

    def _bump_high_water(self, conn: sqlite3.Connection, now: int) -> None:
        """Persist the mark. Must be called inside a write transaction."""
        if now > self._persisted_high_water:
            self._high_water = max(self._high_water, now)
            self._persisted_high_water = now
            conn.execute(
                "UPDATE store_metadata SET value = ? WHERE key = ?",
                (str(now), "clock_high_water_ms"),
            )

    # -- scopes ------------------------------------------------------------

    def open_scope(self, identity: ScopeIdentity) -> str:
        """Create or reuse the scope row for this identity and generation."""
        with self._lock:
            conn = self._connect()
            host, profile, principal, session, generation = identity.columns()
            try:
                with _write_txn(conn):
                    now = self._now_locked(conn)
                    self._bump_high_water(conn, now)
                    conn.execute(
                        "INSERT OR IGNORE INTO scopes "
                        "(scope_id, host, profile, principal, session, generation, created_at_ms) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            identity.scope_id,
                            host,
                            profile,
                            principal,
                            session,
                            generation,
                            now,
                        ),
                    )
                    # Reopening an identity that was closed earlier in the same
                    # generation is a resume, not a new scope: clear the close marker.
                    conn.execute(
                        "UPDATE scopes SET closed_at_ms = NULL WHERE scope_id = ?",
                        (identity.scope_id,),
                    )
            except sqlite3.Error:
                raise ShuntError("STORE_FAILED", "SCOPE_OPEN_FAILED", retryable=False) from None
            return identity.scope_id

    def close_scope(self, identity: ScopeIdentity, *, revoke: bool = True) -> int:
        """Close a scope. With ``revoke`` its handles become unreadable immediately.

        Ordinary per-turn events must not call this: on Hermes ``on_session_end`` fires at
        the end of every ``run_conversation`` call, so closing there would destroy the
        recovery handles the next turn needs. Only a real finalize/reset boundary closes a
        scope; everything else relies on TTL.
        """
        with self._lock:
            conn = self._connect()
            try:
                with _write_txn(conn):
                    now = self._now_locked(conn)
                    self._bump_high_water(conn, now)
                    conn.execute(
                        "UPDATE scopes SET closed_at_ms = ? WHERE scope_id = ? "
                        "AND closed_at_ms IS NULL",
                        (now, identity.scope_id),
                    )
                    if not revoke:
                        return 0
                    cursor = conn.execute(
                        "UPDATE handles SET revoked = 1 WHERE scope_id = ? AND revoked = 0",
                        (identity.scope_id,),
                    )
                    revoked = cursor.rowcount or 0
                    self._release_refcounts_locked(conn, identity.scope_id)
            except sqlite3.Error:
                raise ShuntError("STORE_FAILED", "SCOPE_CLOSE_FAILED", retryable=False) from None
        self._collect_pending_blobs()
        return revoked

    def _release_refcounts_locked(self, conn: sqlite3.Connection, scope_id: str) -> None:
        """Drop the refcount each revoked handle held, inside the caller's transaction."""
        rows = conn.execute(
            "SELECT blob_hash, COUNT(*) AS n FROM handles WHERE scope_id = ? AND revoked = 1 "
            "GROUP BY blob_hash",
            (scope_id,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE blobs SET refcount = MAX(0, refcount - ?) WHERE hash = ?",
                (int(row["n"]), row["blob_hash"]),
            )
        conn.execute("DELETE FROM handles WHERE scope_id = ? AND revoked = 1", (scope_id,))
        conn.execute(
            "UPDATE blobs SET pending_delete = 1 WHERE refcount = 0 AND pending_delete = 0"
        )

    # -- publication -------------------------------------------------------

    def publish(
        self, identity: ScopeIdentity, captures: Sequence[Capture]
    ) -> list[PublishedHandle]:
        """Publish a capture batch. Every handle appears, or none does.

        Storage failure never degrades into raw passthrough: the caller's operation stays
        blocked and a bounded ``STORE_FAILED`` is raised with no handle.
        """
        if not captures:
            return []
        if len(captures) > self._limits.max_sources_per_request:
            raise ShuntError("STORE_FAILED", "BATCH_TOO_LARGE", retryable=False)
        for capture in captures:
            if len(capture.data) > self._limits.max_source_bytes:
                raise ShuntError("LIMIT_EXCEEDED", "SOURCE_OVER_BYTE_CAP", retryable=False)

        scope_id = self.open_scope(identity)
        staged: list[tuple[Capture, Path, str]] = []
        temp_ids: list[str] = []
        try:
            for capture in captures:
                digest = capture.hash
                final = self._blob_path(digest)
                temp_id = self._stage_blob(capture, digest, final)
                if temp_id is not None:
                    temp_ids.append(temp_id)
                staged.append((capture, final, digest))
        except ShuntError:
            self._discard_temp_ids(temp_ids)
            raise
        except OSError:
            self._discard_temp_ids(temp_ids)
            raise ShuntError("STORE_FAILED", "WRITE_FAILED", retryable=False) from None

        with self._lock:
            conn = self._connect()
            try:
                with _write_txn(conn):
                    now = self._now_locked(conn)
                    self._bump_high_water(conn, now)
                    self._assert_scope_open_locked(conn, identity)
                    self._assert_capacity_locked(conn, staged)
                    self._assert_blob_metadata_agrees_locked(conn, staged)
                    expires = now + self._limits.store_handle_ttl_seconds * 1000
                    published: list[PublishedHandle] = []
                    for capture, _final, digest in staged:
                        conn.execute(
                            "INSERT INTO blobs "
                            "(hash, bytes, media_type, line_count, refcount, pending_delete, "
                            " created_at_ms) VALUES (?, ?, ?, ?, 0, 0, ?) "
                            "ON CONFLICT(hash) DO NOTHING",
                            (
                                digest,
                                len(capture.data),
                                capture.media_type,
                                capture.line_count,
                                now,
                            ),
                        )
                        conn.execute(
                            "UPDATE blobs SET refcount = refcount + 1, pending_delete = 0 "
                            "WHERE hash = ?",
                            (digest,),
                        )
                        handle_id = _mint_handle_id()
                        conn.execute(
                            "INSERT INTO handles "
                            "(handle_id, scope_id, blob_hash, kind, internal, generation, "
                            " created_at_ms, expires_at_ms, revoked, baseline_credited, "
                            " disclosed_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0)",
                            (
                                handle_id,
                                scope_id,
                                digest,
                                capture.kind,
                                1 if capture.internal else 0,
                                identity.generation,
                                now,
                                expires,
                            ),
                        )
                        published.append(
                            PublishedHandle(
                                handle_id=handle_id,
                                scope_id=scope_id,
                                blob_hash=digest,
                                media_type=capture.media_type,
                                bytes_len=len(capture.data),
                                line_count=capture.line_count,
                                kind=capture.kind,
                                internal=capture.internal,
                                created_at_ms=now,
                                expires_at_ms=expires,
                            )
                        )
                    for temp_id in temp_ids:
                        conn.execute("DELETE FROM orphan_temps WHERE temp_id = ?", (temp_id,))
            except ShuntError:
                # A refused publish - capacity, a closed scope, conflicting metadata -
                # must not leave the content it staged behind. The rows roll back with the
                # transaction, but the blobs were renamed into place before it opened, so
                # they are discarded explicitly. Only this batch's own temps are dropped:
                # a digest another handle already references is left alone.
                self._discard_temp_ids(temp_ids)
                raise
            except sqlite3.Error:
                # Nothing committed, so no handle exists. The renamed blob files carry no
                # row and the sweep collects them; a partially published batch is
                # impossible by construction.
                self._discard_temp_ids(temp_ids)
                raise ShuntError("STORE_FAILED", "PUBLISH_FAILED", retryable=False) from None
        return published

    def _assert_blob_metadata_agrees_locked(
        self, conn: sqlite3.Connection, staged: list[tuple[Capture, Path, str]]
    ) -> None:
        """Dedupe shares a row; the metadata on it must describe the capture too.

        Blobs are keyed by content hash, but ``media_type`` and ``line_count`` are not part
        of the content. ``INSERT ... ON CONFLICT(hash) DO NOTHING`` therefore kept the
        first publisher's metadata, so identical bytes published as JSON after being
        published as text came back labelled ``text/plain`` - and a records selector over
        that handle is then refused as "not JSON". Serving the wrong media type silently is
        worse than refusing, and the store already fails closed on a content mismatch, so
        this does the same for a metadata mismatch.
        """
        # Two entries in *this* batch can disagree with each other before either is
        # committed. The committed-row check alone let that through: both handles were
        # returned, the row kept the first entry's media type, and every later resolve of
        # the second handle reported metadata its caller never asked for.
        within_batch: dict[str, tuple[str, int]] = {}
        for capture, _final, digest in staged:
            declared = (capture.media_type, capture.line_count)
            seen = within_batch.setdefault(digest, declared)
            if seen != declared:
                raise ShuntError("STORE_FAILED", "BLOB_METADATA_CONFLICT", retryable=False)
            row = conn.execute(
                "SELECT media_type, line_count FROM blobs WHERE hash = ?", (digest,)
            ).fetchone()
            if row is None:
                continue
            if (
                str(row["media_type"]) != capture.media_type
                or int(row["line_count"]) != capture.line_count
            ):
                raise ShuntError("STORE_FAILED", "BLOB_METADATA_CONFLICT", retryable=False)

    def _stage_blob(self, capture: Capture, digest: str, final: Path) -> str | None:
        """Write and rename one payload. Returns the temp id, or ``None`` when deduped."""
        existing = _hash_file(final)
        if existing is not None:
            if existing != digest:
                # Content-addressed storage says this file must hash to `digest`. It does
                # not. Fail closed and leave it alone: other live handles may reference
                # it, and deleting it would widen the damage.
                raise ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", retryable=False)
            return None

        final.parent.mkdir(parents=True, exist_ok=True)
        _assert_private_directory(final.parent)
        temp_id = f"{digest}.{secrets.token_hex(8)}"
        temp = self._root / _TMP_DIR / f"{temp_id}.part"
        self._record_temp(temp_id, digest)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(temp, flags, _FILE_MODE)
        try:
            written = 0
            view = memoryview(capture.data)
            while written < len(view):
                written += os.write(fd, view[written : written + _READ_CHUNK])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(temp, _FILE_MODE)
        os.replace(temp, final)
        _fsync_dir(final.parent)
        return temp_id

    def _record_temp(self, temp_id: str, digest: str) -> None:
        with self._lock:
            conn = self._connect()
            try:
                with _write_txn(conn):
                    now = self._now_locked(conn)
                    conn.execute(
                        "INSERT OR REPLACE INTO orphan_temps (temp_id, blob_hash, created_at_ms) "
                        "VALUES (?, ?, ?)",
                        (temp_id, digest, now),
                    )
            except sqlite3.Error:
                raise ShuntError("STORE_FAILED", "TEMP_RECORD_FAILED", retryable=False) from None

    def _discard_temp_ids(self, temp_ids: Sequence[str]) -> None:
        """Undo staging for a batch that will not be published.

        Staging renames the payload to its final content-addressed path *before* the
        publish transaction opens, so unlinking only the ``.part`` file - which no longer
        exists by then - left the content behind on every refused publish. The final file
        is dropped too, but only when no row references the digest: a concurrent publisher
        may legitimately have taken a reference to the very same content while this batch
        was being refused, and its payload must survive.
        """
        if not temp_ids:
            return
        for temp_id in temp_ids:
            _unlink_quiet(self._root / _TMP_DIR / f"{temp_id}.part")
        with self._lock, contextlib.suppress(sqlite3.Error):
            conn = self._connect()
            with _write_txn(conn):
                conn.executemany(
                    "DELETE FROM orphan_temps WHERE temp_id = ?", [(t,) for t in temp_ids]
                )
                # A temp id is `<digest>.<nonce>`, so the digest this batch wrote is
                # recoverable without threading more state through the failure paths.
                for digest in {temp_id.split(".", 1)[0] for temp_id in temp_ids}:
                    referenced = conn.execute(
                        "SELECT 1 FROM blobs WHERE hash = ?", (digest,)
                    ).fetchone()
                    if referenced is None:
                        _unlink_quiet(self._blob_path(digest))

    def _assert_scope_open_locked(self, conn: sqlite3.Connection, identity: ScopeIdentity) -> None:
        row = conn.execute(
            "SELECT generation, closed_at_ms FROM scopes WHERE scope_id = ?",
            (identity.scope_id,),
        ).fetchone()
        if row is None or row["closed_at_ms"] is not None:
            raise ShuntError("SOURCE_EXPIRED", "SCOPE_CLOSED", retryable=False)
        if int(row["generation"]) != identity.generation:
            raise ShuntError("SOURCE_EXPIRED", "STALE_GENERATION", retryable=False)

    def _assert_capacity_locked(
        self, conn: sqlite3.Connection, staged: Sequence[tuple[Capture, Path, str]]
    ) -> None:
        row = conn.execute("SELECT COUNT(*) AS handles FROM handles WHERE revoked = 0").fetchone()
        blob_row = conn.execute(
            "SELECT COALESCE(SUM(bytes), 0) AS total FROM blobs WHERE pending_delete = 0"
        ).fetchone()
        handles = int(row["handles"]) + len(staged)
        if handles > self._limits.store_max_entries:
            raise ShuntError("LIMIT_EXCEEDED", "STORE_ENTRY_QUOTA", retryable=False)
        known = {r["hash"] for r in conn.execute("SELECT hash FROM blobs").fetchall()}
        added = sum(len(c.data) for c, _p, d in staged if d not in known)
        if int(blob_row["total"]) + added > self._limits.store_max_bytes:
            raise ShuntError("LIMIT_EXCEEDED", "STORE_BYTE_QUOTA", retryable=False)

    # -- authorization -----------------------------------------------------

    def resolve(
        self, identity: ScopeIdentity, handle_id: str, *, snapshot_id: str | None = None
    ) -> PublishedHandle:
        """Authorize one handle. Raises rather than re-reading the underlying source.

        A handle from another scope, another generation, a closed scope or past its TTL is
        indistinguishable from an unknown one, which is deliberate: cross-session probing
        learns nothing.
        """
        now = self._tick()
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT h.handle_id, h.scope_id, h.blob_hash, h.kind, h.internal, "
                "       h.created_at_ms, h.expires_at_ms, b.bytes, b.media_type, b.line_count "
                "  FROM handles h "
                "  JOIN scopes s ON s.scope_id = h.scope_id "
                "  JOIN blobs  b ON b.hash     = h.blob_hash "
                " WHERE h.handle_id = ? AND h.scope_id = ? AND h.revoked = 0 "
                "   AND h.expires_at_ms > ? AND s.closed_at_ms IS NULL AND s.generation = ?",
                (handle_id, identity.scope_id, now, identity.generation),
            ).fetchone()
        if row is None:
            raise ShuntError("SOURCE_EXPIRED", self._refusal_detail(identity, handle_id, now))
        handle = PublishedHandle(
            handle_id=str(row["handle_id"]),
            scope_id=str(row["scope_id"]),
            blob_hash=str(row["blob_hash"]),
            media_type=str(row["media_type"]),
            bytes_len=int(row["bytes"]),
            line_count=int(row["line_count"]),
            kind=str(row["kind"]),
            internal=bool(row["internal"]),
            created_at_ms=int(row["created_at_ms"]),
            expires_at_ms=int(row["expires_at_ms"]),
        )
        if snapshot_id is not None and snapshot_id != handle.snapshot_id:
            raise ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH")
        return handle

    def _refusal_detail(self, identity: ScopeIdentity, handle_id: str, now: int) -> str:
        """Why a handle was refused - accurately, but only about our own scope.

        The lookup is scoped to ``identity.scope_id``, so a handle belonging to another
        session, principal or generation is reported as ``UNKNOWN_HANDLE`` and cross-scope
        probing still learns nothing. Inside the caller's own scope there is nothing to
        protect - they already hold the handle - so the honest reason is returned and the
        citation verifier can distinguish an expiry from a typo.
        """
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT h.revoked, h.expires_at_ms, s.closed_at_ms, s.generation "
                "  FROM handles h JOIN scopes s ON s.scope_id = h.scope_id "
                " WHERE h.handle_id = ? AND h.scope_id = ?",
                (handle_id, identity.scope_id),
            ).fetchone()
        if row is None:
            return "UNKNOWN_HANDLE"
        if int(row["revoked"]):
            return "REVOKED"
        if row["closed_at_ms"] is not None:
            return "SCOPE_CLOSED"
        if int(row["generation"]) != identity.generation:
            return "STALE_GENERATION"
        if int(row["expires_at_ms"]) <= now:
            return "TTL_ELAPSED"
        return "UNKNOWN_HANDLE"

    def is_internal(self, identity: ScopeIdentity, handle_id: str) -> bool:
        """Store-verified recursion guard. A payload claiming ``internal`` proves nothing."""
        try:
            return self.resolve(identity, handle_id).internal
        except ShuntError:
            return False

    def load_payload(self, handle: PublishedHandle) -> bytes:
        """Read the immutable payload and re-verify it against the handle's hash."""
        path = self._blob_path(handle.blob_hash)
        try:
            data = _read_private(path, handle.bytes_len)
        except FileNotFoundError:
            raise ShuntError("STORE_FAILED", "BLOB_MISSING", retryable=False) from None
        except OSError:
            raise ShuntError("STORE_FAILED", "BLOB_READ_FAILED", retryable=False) from None
        if hashlib.sha256(data).hexdigest() != handle.blob_hash:
            raise ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", retryable=False)
        return data

    def revoke(self, identity: ScopeIdentity, handle_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                with _write_txn(conn):
                    row = conn.execute(
                        "SELECT blob_hash FROM handles WHERE handle_id = ? AND scope_id = ?",
                        (handle_id, identity.scope_id),
                    ).fetchone()
                    if row is None:
                        return False
                    conn.execute(
                        "DELETE FROM handles WHERE handle_id = ? AND scope_id = ?",
                        (handle_id, identity.scope_id),
                    )
                    conn.execute(
                        "UPDATE blobs SET refcount = MAX(0, refcount - 1) WHERE hash = ?",
                        (row["blob_hash"],),
                    )
                    conn.execute(
                        "UPDATE blobs SET pending_delete = 1 WHERE hash = ? AND refcount = 0",
                        (row["blob_hash"],),
                    )
            except sqlite3.Error:
                raise ShuntError("STORE_FAILED", "REVOKE_FAILED", retryable=False) from None
        self._collect_pending_blobs()
        return True

    # -- baseline credit ---------------------------------------------------

    def credit_baseline(self, identity: ScopeIdentity, handle_id: str) -> bool:
        """Claim the one-time withheld-source baseline for this content.

        Returns ``True`` exactly once per (scope, content). Refined questions, failed
        retries and inspect pages therefore add their own overhead without re-claiming the
        saving - and neither does a *recapture*: the saving is a property of the content
        that was withheld, not of the handle that happens to address it. Keyed by handle,
        re-registering the same file claimed the saving again and inflated "tokens saved"
        without withholding anything new.
        """
        with self._lock:
            conn = self._connect()
            try:
                with _write_txn(conn):
                    now = self._now_locked(conn)
                    self._bump_high_water(conn, now)
                    # The claim is recorded against the *content*, in a row that outlives
                    # the handle. Kept on `handles.baseline_credited` it died with the
                    # handle, so revoking and recapturing the same bytes claimed the
                    # saving again - and a `handles` row cannot be retained as a
                    # tombstone, because its foreign key to `blobs` would pin the payload
                    # row and stop revoked content being collected at all.
                    row = conn.execute(
                        "SELECT blob_hash FROM handles "
                        " WHERE handle_id = ? AND scope_id = ? AND revoked = 0 "
                        "   AND expires_at_ms > ?",
                        (handle_id, identity.scope_id, now),
                    ).fetchone()
                    if row is None:
                        return False
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO source_credits "
                        "(scope_id, blob_hash, credited_at_ms) VALUES (?, ?, ?)",
                        (identity.scope_id, str(row["blob_hash"]), now),
                    )
                    credited = bool(cursor.rowcount)
                    if credited:
                        # Kept in step so the legacy column still reads truthfully for
                        # anything inspecting a live handle.
                        conn.execute(
                            "UPDATE handles SET baseline_credited = 1 "
                            " WHERE handle_id = ? AND scope_id = ?",
                            (handle_id, identity.scope_id),
                        )
                    return credited
            except sqlite3.Error:
                raise ShuntError("STORE_FAILED", "BASELINE_FAILED", retryable=False) from None

    # -- disclosure --------------------------------------------------------

    def disclosure_allowance(self, identity: ScopeIdentity, handle_id: str) -> DisclosureAllowance:
        now = self._tick()
        with self._lock:
            conn = self._connect()
            source = conn.execute(
                "SELECT disclosed_bytes FROM handles "
                " WHERE handle_id = ? AND scope_id = ? AND revoked = 0 AND expires_at_ms > ?",
                (handle_id, identity.scope_id, now),
            ).fetchone()
            if source is not None:
                # The ceiling is per *source*, and a recapture of the same bytes is the
                # same source. Summing this scope's disclosure events for every handle
                # that addresses this content stops a caller resetting the ceiling by
                # re-registering the file. See `charge_disclosure`.
                spent = conn.execute(
                    "SELECT COALESCE(SUM(bytes), 0) AS total FROM disclosure_events "
                    " WHERE scope_id = ? AND blob_hash IS NOT NULL "
                    "   AND blob_hash = (SELECT blob_hash FROM handles "
                    "                      WHERE handle_id = ? AND scope_id = ?)",
                    (identity.scope_id, handle_id, identity.scope_id),
                ).fetchone()
                source = {"disclosed_bytes": int(spent["total"])}
            if source is None:
                raise ShuntError("SOURCE_EXPIRED", "UNKNOWN_HANDLE")
            session = conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS total FROM disclosure_events "
                " WHERE scope_id = ?",
                (identity.scope_id,),
            ).fetchone()
        return DisclosureAllowance(
            per_source_remaining=max(
                0, self._limits.disclosure_max_per_source_bytes - int(source["disclosed_bytes"])
            ),
            per_session_remaining=max(
                0, self._limits.disclosure_max_per_session_bytes - int(session["total"])
            ),
        )

    def charge_disclosure(
        self, identity: ScopeIdentity, handle_id: str, kind: str, want_bytes: int
    ) -> DisclosureCharge:
        """Check and increment the disclosure counters in one transaction.

        This runs *before* any byte is returned, so two concurrent inspects cannot
        overshoot the ceiling between them. The exact byte count the caller is about to
        emit is charged; if the allowance no longer covers it, nothing is charged and
        nothing is disclosed.
        """
        if kind not in DISCLOSURE_KINDS:
            raise ShuntError("STORE_FAILED", "BAD_DISCLOSURE_KIND", retryable=False)
        if want_bytes < 0:
            raise ShuntError("STORE_FAILED", "BAD_DISCLOSURE_BYTES", retryable=False)
        with self._lock:
            conn = self._connect()
            try:
                with _write_txn(conn):
                    now = self._now_locked(conn)
                    self._bump_high_water(conn, now)
                    source = conn.execute(
                        "SELECT disclosed_bytes FROM handles "
                        " WHERE handle_id = ? AND scope_id = ? AND revoked = 0 "
                        "   AND expires_at_ms > ?",
                        (handle_id, identity.scope_id, now),
                    ).fetchone()
                    if source is None:
                        raise ShuntError("SOURCE_EXPIRED", "UNKNOWN_HANDLE")
                    # Per *source*, not per handle: a recapture of the same bytes is the
                    # same source, and reading one handle's column let a caller disclose
                    # the cap, re-register, and disclose it again without limit. The
                    # session total was never affected - it already sums scope-wide.
                    spent = conn.execute(
                        "SELECT COALESCE(SUM(bytes), 0) AS total FROM disclosure_events "
                        " WHERE scope_id = ? AND blob_hash IS NOT NULL "
                        "   AND blob_hash = (SELECT blob_hash FROM handles "
                        "                      WHERE handle_id = ? AND scope_id = ?)",
                        (identity.scope_id, handle_id, identity.scope_id),
                    ).fetchone()
                    used_source = int(spent["total"])
                    session_row = conn.execute(
                        "SELECT COALESCE(SUM(bytes), 0) AS total FROM disclosure_events "
                        " WHERE scope_id = ?",
                        (identity.scope_id,),
                    ).fetchone()
                    used_session = int(session_row["total"])
                    source_cap = self._limits.disclosure_max_per_source_bytes
                    session_cap = self._limits.disclosure_max_per_session_bytes
                    fits = (
                        used_source + want_bytes <= source_cap
                        and used_session + want_bytes <= session_cap
                    )
                    if not fits:
                        return DisclosureCharge(
                            granted=False,
                            charged_bytes=0,
                            disclosed_bytes_source=used_source,
                            disclosed_bytes_session=used_session,
                            limit_reached=True,
                        )
                    if want_bytes > 0:
                        # A page that disclosed nothing has nothing to account for. Writing
                        # a zero row anyway would let a caller paging a fruitless search
                        # grow an uncapped table without ever disclosing a byte.
                        conn.execute(
                            "UPDATE handles SET disclosed_bytes = disclosed_bytes + ? "
                            " WHERE handle_id = ? AND scope_id = ?",
                            (want_bytes, handle_id, identity.scope_id),
                        )
                        conn.execute(
                            # The content, not just the handle: a handle is deleted by
                            # revocation or expiry, and the per-source ceiling has to
                            # outlive both or a recapture resets it.
                            "INSERT INTO disclosure_events "
                            "(scope_id, handle_id, blob_hash, kind, bytes, at_ms) "
                            "VALUES (?, ?, (SELECT blob_hash FROM handles "
                            "                 WHERE handle_id = ? AND scope_id = ?), ?, ?, ?)",
                            (
                                identity.scope_id,
                                handle_id,
                                handle_id,
                                identity.scope_id,
                                kind,
                                want_bytes,
                                now,
                            ),
                        )
                    return DisclosureCharge(
                        granted=True,
                        charged_bytes=want_bytes,
                        disclosed_bytes_source=used_source + want_bytes,
                        disclosed_bytes_session=used_session + want_bytes,
                        limit_reached=(
                            used_source + want_bytes >= source_cap
                            or used_session + want_bytes >= session_cap
                        ),
                    )
            except ShuntError:
                raise
            except sqlite3.Error:
                raise ShuntError("STORE_FAILED", "DISCLOSURE_FAILED", retryable=False) from None

    def disclosed_bytes(self, identity: ScopeIdentity) -> int:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS total FROM disclosure_events WHERE scope_id = ?",
                (identity.scope_id,),
            ).fetchone()
        return int(row["total"])

    # -- accounting --------------------------------------------------------

    def record_operation(self, identity: ScopeIdentity, record: OperationRecord) -> None:
        scope_id = self.open_scope(identity)
        with self._lock:
            conn = self._connect()
            try:
                with _write_txn(conn):
                    now = self._now_locked(conn)
                    self._bump_high_water(conn, now)
                    conn.execute(
                        "INSERT OR REPLACE INTO accounting_events ("
                        " operation_id, scope_id, kind, status, code, raw_input_bytes,"
                        " raw_input_baseline_tokens, baseline_kind, baseline_method,"
                        " baseline_credit_tokens, main_model_envelope_bytes,"
                        " main_model_envelope_tokens, envelope_token_method,"
                        " reader_input_tokens, reader_output_tokens, reader_cache_tokens,"
                        " reader_token_method, attempts_started, attempts_usage_complete,"
                        " delivery_boundary, main_context_tokens_saved, net_tokens_saved, at_ms"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.operation_id,
                            scope_id,
                            record.kind,
                            record.status,
                            record.code,
                            record.raw_input_bytes,
                            record.raw_input_baseline_tokens,
                            record.baseline_kind,
                            record.baseline_method,
                            record.baseline_credit_tokens,
                            record.main_model_envelope_bytes,
                            record.main_model_envelope_tokens,
                            record.envelope_token_method,
                            record.reader_input_tokens,
                            record.reader_output_tokens,
                            record.reader_cache_tokens,
                            record.reader_token_method,
                            record.attempts_started,
                            record.attempts_usage_complete,
                            record.delivery_boundary,
                            record.main_context_tokens_saved,
                            record.net_tokens_saved,
                            now,
                        ),
                    )
            except sqlite3.Error:
                raise ShuntError("STORE_FAILED", "ACCOUNTING_FAILED", retryable=False) from None

    def operation_count(self, identity: ScopeIdentity) -> int:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM accounting_events WHERE scope_id = ?",
                (identity.scope_id,),
            ).fetchone()
        return int(row["n"])

    def operation_page(
        self, identity: ScopeIdentity, *, page: int, page_size: int
    ) -> list[OperationRecord]:
        """One bounded page of this scope's own records, oldest first.

        Ordering is ascending on ``(at_ms, operation_id)`` deliberately. The log is
        append-only, so ascending order means a record written *between* two page reads
        lands at the end and never shifts a page the caller already walked. Newest-first
        ordering would skew every offset each time a new operation was recorded - and
        reading stats records an operation of its own, so that skew is guaranteed rather
        than hypothetical. ``operation_id`` breaks ties within the same millisecond, which
        keeps the order total.
        """
        page = max(1, min(page, self._limits.stats_max_pages))
        page_size = max(1, min(page_size, self._limits.stats_max_records_per_page))
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM accounting_events WHERE scope_id = ? "
                " ORDER BY at_ms ASC, operation_id ASC LIMIT ? OFFSET ?",
                (identity.scope_id, page_size, (page - 1) * page_size),
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def operation_totals(self, identity: ScopeIdentity) -> dict[str, Any]:
        """Signed aggregates for this scope. A NULL column stays ``None``, never 0."""
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT COUNT(*) AS operations,"
                "       COALESCE(SUM(raw_input_bytes), 0) AS raw_input_bytes,"
                "       COALESCE(SUM(baseline_credit_tokens), 0) AS baseline_credit_tokens,"
                "       COALESCE(SUM(main_model_envelope_tokens), 0) AS main_model_envelope_tokens,"
                "       SUM(reader_input_tokens) AS reader_input_tokens,"
                "       SUM(reader_output_tokens) AS reader_output_tokens,"
                "       SUM(reader_cache_tokens) AS reader_cache_tokens,"
                "       COALESCE(SUM(main_context_tokens_saved), 0) AS main_context_tokens_saved,"
                "       COALESCE(SUM(net_tokens_saved), 0) AS net_tokens_saved,"
                "       COALESCE(SUM(attempts_started), 0) AS attempts_started,"
                "       COALESCE(SUM(attempts_usage_complete), 0) AS attempts_usage_complete"
                "  FROM accounting_events WHERE scope_id = ?",
                (identity.scope_id,),
            ).fetchone()
        totals = dict(zip(row.keys(), tuple(row), strict=True))
        totals["disclosed_bytes"] = self.disclosed_bytes(identity)
        return totals

    # -- maintenance -------------------------------------------------------

    def recover(self) -> SweepReport:
        """Startup recovery: clear staged temps, then sweep.

        No live handle ever references a file under ``tmp/``, so every file there is by
        definition the residue of a transaction that did not commit and is safe to remove.
        """
        removed = 0
        tmp_dir = self._root / _TMP_DIR
        try:
            children = list(tmp_dir.iterdir())
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
        with self._lock, contextlib.suppress(sqlite3.Error):
            conn = self._connect()
            with _write_txn(conn):
                conn.execute("DELETE FROM orphan_temps")
        report = self.sweep()
        return SweepReport(
            expired_handles=report.expired_handles,
            deleted_blobs=report.deleted_blobs,
            removed_temps=removed,
            orphan_blob_files=report.orphan_blob_files,
        )

    def sweep(self) -> SweepReport:
        """Expire handles past their TTL, then collect unreferenced content."""
        with self._lock:
            conn = self._connect()
            try:
                with _write_txn(conn):
                    now = self._now_locked(conn)
                    self._bump_high_water(conn, now)
                    rows = conn.execute(
                        "SELECT blob_hash, COUNT(*) AS n FROM handles "
                        " WHERE expires_at_ms <= ? OR revoked = 1 GROUP BY blob_hash",
                        (now,),
                    ).fetchall()
                    expired = sum(int(r["n"]) for r in rows)
                    for row in rows:
                        conn.execute(
                            "UPDATE blobs SET refcount = MAX(0, refcount - ?) WHERE hash = ?",
                            (int(row["n"]), row["blob_hash"]),
                        )
                    conn.execute(
                        "DELETE FROM handles WHERE expires_at_ms <= ? OR revoked = 1",
                        (now,),
                    )
                    conn.execute(
                        "UPDATE blobs SET pending_delete = 1 "
                        " WHERE refcount = 0 AND pending_delete = 0"
                    )
            except sqlite3.Error:
                raise ShuntError("STORE_FAILED", "SWEEP_FAILED", retryable=False) from None
        deleted = self._collect_pending_blobs()
        orphans = self._collect_orphan_blob_files()
        return SweepReport(
            expired_handles=expired, deleted_blobs=deleted, orphan_blob_files=orphans
        )

    def _collect_pending_blobs(self) -> int:
        """Delete the row and unlink its file under one lock, never separately.

        This used to unlink outside every lock and then re-verify
        ``refcount = 0 AND pending_delete = 1`` before deleting the row, on the reasoning
        that a publisher which took a reference meanwhile would keep its row "and its
        ``_stage_blob`` re-writes the content". It does not: ``_stage_blob`` *skips*
        writing whenever the file is already present, so the losing interleaving is

          1. publisher sees the file and dedupes, writing nothing;
          2. sweeper unlinks the file and deletes the row, refcount still 0;
          3. publisher commits, re-inserting the row and minting a handle.

        which leaves a handle whose payload does not exist. Holding the lock across the
        check, the unlink and the delete removes the window: a publisher either commits
        its refcount before the sweep starts - and the ``refcount = 0`` guard then spares
        the blob - or it runs after the row is gone and re-stages the content itself.
        """
        deleted = 0
        with self._lock:
            conn = self._connect()
            candidates = [
                str(row["hash"])
                for row in conn.execute(
                    "SELECT hash FROM blobs WHERE pending_delete = 1 AND refcount = 0"
                ).fetchall()
            ]
            for digest in candidates:
                try:
                    with _write_txn(conn):
                        cursor = conn.execute(
                            "DELETE FROM blobs WHERE hash = ? AND refcount = 0 "
                            "  AND pending_delete = 1",
                            (digest,),
                        )
                        removed = cursor.rowcount or 0
                        # Inside the transaction: if it rolls back, the file is still
                        # referenced by the row that survived and must stay.
                        if removed:
                            _unlink_quiet(self._blob_path(digest))
                        deleted += removed
                except sqlite3.Error:
                    continue
        return deleted

    def _collect_orphan_blob_files(self) -> int:
        """Remove content files with no row - the residue of a crash before commit."""
        with self._lock:
            conn = self._connect()
            known = {str(row["hash"]) for row in conn.execute("SELECT hash FROM blobs").fetchall()}
        removed = 0
        for path in self._iter_blob_files():
            digest = path.name[: -len(_BLOB_SUFFIX)]
            if digest not in known:
                _unlink_quiet(path)
                removed += 1
        return removed

    def _iter_blob_files(self) -> Iterator[Path]:
        root = self._root / _BLOB_DIR
        if not root.is_dir():
            return
        for path in root.rglob(f"*{_BLOB_SUFFIX}"):
            try:
                if stat.S_ISREG(path.lstat().st_mode) and len(path.name) == 64 + len(_BLOB_SUFFIX):
                    yield path
            except OSError:
                continue

    def stats(self) -> StoreStats:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT (SELECT COUNT(*) FROM handles WHERE revoked = 0) AS handles,"
                "       (SELECT COUNT(*) FROM blobs) AS blobs,"
                "       (SELECT COALESCE(SUM(bytes), 0) FROM blobs) AS bytes"
            ).fetchone()
        return StoreStats(
            handles=int(row["handles"]), blobs=int(row["blobs"]), bytes=int(row["bytes"])
        )

    # -- legacy artifacts --------------------------------------------------

    def legacy_artifact_count(self) -> int:
        """Count pre-1.1 ``*.spill`` files. They are never imported as handles."""
        return sum(1 for _ in self._iter_legacy_artifacts())

    def purge_legacy_artifacts(self) -> int:
        """Remove pre-1.1 spill artifacts. Deletion, not secure erasure."""
        removed = 0
        for path in list(self._iter_legacy_artifacts()):
            _unlink_quiet(path)
            removed += 1
        return removed

    def _iter_legacy_artifacts(self) -> Iterator[Path]:
        if not self._root.is_dir():
            return
        for path in self._root.glob(f"*/*{_LEGACY_SUFFIX}"):
            with contextlib.suppress(OSError):
                if stat.S_ISREG(path.lstat().st_mode):
                    yield path

    # -- paths -------------------------------------------------------------

    def _blob_path(self, digest: str) -> Path:
        """Location derived from the hash. Never stored, never exposed to a caller."""
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ShuntError("STORE_FAILED", "BAD_BLOB_HASH", retryable=False)
        return self._root / _BLOB_DIR / digest[:2] / digest[2:4] / f"{digest}{_BLOB_SUFFIX}"


# -- helpers ----------------------------------------------------------------


@contextlib.contextmanager
def _write_txn(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE so a writer conflict surfaces as busy-timeout, not as a late abort."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _record_from_row(row: sqlite3.Row) -> OperationRecord:
    return OperationRecord(
        operation_id=str(row["operation_id"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        code=str(row["code"]),
        raw_input_bytes=int(row["raw_input_bytes"]),
        raw_input_baseline_tokens=_optional_int(row["raw_input_baseline_tokens"]),
        baseline_kind=str(row["baseline_kind"]),
        baseline_method=str(row["baseline_method"]),
        baseline_credit_tokens=int(row["baseline_credit_tokens"]),
        main_model_envelope_bytes=int(row["main_model_envelope_bytes"]),
        main_model_envelope_tokens=int(row["main_model_envelope_tokens"]),
        envelope_token_method=str(row["envelope_token_method"]),
        reader_input_tokens=_optional_int(row["reader_input_tokens"]),
        reader_output_tokens=_optional_int(row["reader_output_tokens"]),
        reader_cache_tokens=_optional_int(row["reader_cache_tokens"]),
        reader_token_method=str(row["reader_token_method"]),
        attempts_started=int(row["attempts_started"]),
        attempts_usage_complete=int(row["attempts_usage_complete"]),
        delivery_boundary=str(row["delivery_boundary"]),
        main_context_tokens_saved=int(row["main_context_tokens_saved"]),
        net_tokens_saved=int(row["net_tokens_saved"]),
    )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _hash_file(path: Path) -> str | None:
    """SHA-256 of an existing regular file, or ``None`` when it is absent.

    A symlink, FIFO, directory or device where a blob should be is a path-replacement
    attempt: it fails closed rather than being followed.
    """
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ShuntError("STORE_FAILED", "BLOB_STAT_FAILED", retryable=False) from None
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink > 1:
        raise ShuntError("STORE_FAILED", "UNSAFE_BLOB_PATH", retryable=False)
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise ShuntError("STORE_FAILED", "UNSAFE_BLOB_PATH", retryable=False) from None
    try:
        while True:
            block = os.read(fd, _READ_CHUNK)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _read_private(path: Path, expected_bytes: int) -> bytes:
    """Read a blob through a validated descriptor, refusing past the expected size."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ShuntError("STORE_FAILED", "UNSAFE_BLOB_PATH", retryable=False)
        out = bytearray()
        while True:
            block = os.read(fd, _READ_CHUNK)
            if not block:
                break
            if len(out) + len(block) > expected_bytes:
                raise ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", retryable=False)
            out.extend(block)
        if len(out) != expected_bytes:
            raise ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", retryable=False)
        return bytes(out)
    finally:
        os.close(fd)


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _fsync_dir(path: Path) -> None:
    """Make a rename durable. Not every platform supports it; absence is not a failure."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _assert_private_directory(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except (OSError, NotImplementedError):
        raise ShuntError("STORE_FAILED", "UNSAFE_CACHE_PATH", retryable=False) from None
    if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise ShuntError("STORE_FAILED", "UNSAFE_CACHE_PATH", retryable=False)
    try:
        os.chmod(path, _DIR_MODE, follow_symlinks=False)
    except (OSError, NotImplementedError):
        raise ShuntError("STORE_FAILED", "PERMISSION_FAILED", retryable=False) from None


__all__ = [
    "Capture",
    "DisclosureAllowance",
    "DisclosureCharge",
    "OperationRecord",
    "PublishedHandle",
    "ScopeIdentity",
    "SnapshotStore",
    "StoreStats",
    "SweepReport",
    "new_operation_id",
]
