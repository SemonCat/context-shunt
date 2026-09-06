"""Scope-bound source registry over the hybrid store.

A ``source_id`` is an opaque capability, not an address: it is minted by the store, it
carries the authorization decision that was made when the source was captured, and it
resolves only inside the trusted (host, profile, principal, session, generation) scope
that created it. Expired, revoked, closed-scope and stale-generation handles are refused
rather than silently re-fetching the underlying file - the source may have changed, and
answering from a newer version of it under an older snapshot hash would be a lie.

This layer adds one thing to the store: rehydrating a payload into a
:class:`~context_shunt.snapshot.Snapshot` with its line and record indexes. The payload is
hash-verified on every load, and the rehydrated snapshot is cached per handle because it
is immutable by construction. The cache is bounded so a session cannot pin more than a
handful of payloads in memory.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits
from .snapshot import Snapshot, snapshot_bytes
from .store import Capture, PublishedHandle, ScopeIdentity, SnapshotStore


@dataclass(frozen=True)
class RegisteredSource:
    source_id: str
    session_id: str
    snapshot: Snapshot
    expires_at_epoch: float
    internal: bool = False
    kind: str = "shunted_read"


class SourceRegistry:
    """Thread-safe, scope-bound view of the store."""

    #: How many rehydrated payloads one session may keep resident. One request may name
    #: up to ``max_sources_per_request`` sources, so the cache holds at least that many.
    _CACHE_ENTRIES = 8

    def __init__(
        self,
        store: SnapshotStore,
        identity: ScopeIdentity,
        limits: Limits = DEFAULT_LIMITS,
    ):
        self._store = store
        self._identity = identity
        self._limits = limits
        self._lock = threading.RLock()
        self._cache: OrderedDict[str, Snapshot] = OrderedDict()

    @property
    def identity(self) -> ScopeIdentity:
        return self._identity

    @property
    def session_id(self) -> str:
        return self._identity.session

    @property
    def store(self) -> SnapshotStore:
        return self._store

    # -- capture -----------------------------------------------------------

    def register(
        self,
        session_id: str,
        snapshot: Snapshot,
        *,
        internal: bool = False,
        kind: str = "shunted_read",
    ) -> RegisteredSource:
        """Publish one snapshot. Convenience wrapper over the all-or-none batch path."""
        return self.register_batch(session_id, [snapshot], internal=internal, kind=kind)[0]

    def register_batch(
        self,
        session_id: str,
        snapshots: Sequence[Snapshot],
        *,
        internal: bool = False,
        kind: str = "shunted_read",
    ) -> list[RegisteredSource]:
        """Publish a batch. Every handle appears or none does.

        A multi-source capture that half-succeeded would leave the caller holding handles
        for part of a request it will be told was refused, so the store commits the whole
        batch in one transaction.
        """
        self._assert_session(session_id)
        captures = [
            Capture(
                data=snapshot.data,
                media_type=snapshot.media_type,
                line_count=snapshot.line_count,
                kind=kind,
                internal=internal,
            )
            for snapshot in snapshots
        ]
        published = self._store.publish(self._identity, captures)
        out: list[RegisteredSource] = []
        for handle, snapshot in zip(published, snapshots, strict=True):
            if handle.snapshot_id != snapshot.snapshot_id:
                # Content addressing guarantees this; asserting it makes a future change
                # to either side fail loudly instead of publishing a mislabelled handle.
                raise ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", retryable=False)
            self._remember(handle.handle_id, snapshot)
            out.append(self._entry(handle, snapshot))
        return out

    # -- resolution --------------------------------------------------------

    def resolve(self, session_id: str, source_id: str) -> RegisteredSource:
        self._assert_session(session_id)
        handle = self._store.resolve(self._identity, source_id)
        return self._entry(handle, self._snapshot_for(handle))

    def handle(self, session_id: str, source_id: str) -> PublishedHandle:
        """Authorize without rehydrating the payload."""
        self._assert_session(session_id)
        return self._store.resolve(self._identity, source_id)

    def is_internal(self, session_id: str, source_id: str) -> bool:
        """Store-verified recursion guard. A payload claiming ``internal`` proves nothing."""
        try:
            return self.handle(session_id, source_id).internal
        except ShuntError:
            return False

    def remove(self, session_id: str, source_id: str) -> bool:
        self._assert_session(session_id)
        with self._lock:
            self._cache.pop(source_id, None)
        return self._store.revoke(self._identity, source_id)

    def expire_session(self, session_id: str) -> int:
        """Revoke every handle in this scope. Only a real session boundary calls this."""
        self._assert_session(session_id)
        with self._lock:
            self._cache.clear()
        return self._store.close_scope(self._identity)

    def sweep(self) -> int:
        return self._store.sweep().expired_handles

    def count(self, session_id: str | None = None) -> int:
        if session_id is not None:
            self._assert_session(session_id)
        return self._store.stats().handles

    # -- internals ---------------------------------------------------------

    def _assert_session(self, session_id: str) -> None:
        """A registry is bound to one scope; another session's id is not resolvable here."""
        if session_id and session_id != self._identity.session:
            raise ShuntError("SOURCE_EXPIRED", "UNKNOWN_HANDLE")

    def _entry(self, handle: PublishedHandle, snapshot: Snapshot) -> RegisteredSource:
        return RegisteredSource(
            source_id=handle.handle_id,
            session_id=self._identity.session,
            snapshot=snapshot,
            expires_at_epoch=handle.expires_at_epoch,
            internal=handle.internal,
            kind=handle.kind,
        )

    def _snapshot_for(self, handle: PublishedHandle) -> Snapshot:
        with self._lock:
            cached = self._cache.get(handle.handle_id)
            if cached is not None and cached.snapshot_id == handle.snapshot_id:
                self._cache.move_to_end(handle.handle_id)
                return cached
        data = self._store.load_payload(handle)
        snapshot = snapshot_bytes(data, media_type_hint=handle.media_type, limits=self._limits)
        if snapshot.snapshot_id != handle.snapshot_id:
            raise ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", retryable=False)
        self._remember(handle.handle_id, snapshot)
        return snapshot

    def _remember(self, handle_id: str, snapshot: Snapshot) -> None:
        with self._lock:
            self._cache[handle_id] = snapshot
            self._cache.move_to_end(handle_id)
            while len(self._cache) > self._CACHE_ENTRIES:
                self._cache.popitem(last=False)


__all__ = ["RegisteredSource", "SourceRegistry"]
