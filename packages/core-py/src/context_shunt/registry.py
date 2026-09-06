"""Session-scoped source registry.

A ``source_id`` is an opaque capability, not an address: it is minted per session, it
carries the authorization decision that was made when the source was registered, and it
never resolves in another session. Expired handles are refused rather than silently
re-fetching the underlying file.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from .errors import ShuntError
from .limits import DEFAULT_LIMITS, Limits
from .snapshot import Snapshot


@dataclass(frozen=True)
class RegisteredSource:
    source_id: str
    session_id: str
    snapshot: Snapshot
    expires_at_epoch: float
    internal: bool = False


def _mint_id() -> str:
    return "src_" + secrets.token_hex(8)


class SourceRegistry:
    """Thread-safe registry isolated per session/tenant."""

    def __init__(self, limits: Limits = DEFAULT_LIMITS, time_fn=time.time):
        self._limits = limits
        self._time = time_fn
        self._lock = threading.RLock()
        self._by_session: dict[str, dict[str, RegisteredSource]] = {}

    def register(
        self, session_id: str, snapshot: Snapshot, *, internal: bool = False
    ) -> RegisteredSource:
        if not session_id:
            raise ShuntError("UNSAFE_SOURCE", "NO_SESSION")
        entry = RegisteredSource(
            source_id=_mint_id(),
            session_id=session_id,
            snapshot=snapshot,
            expires_at_epoch=self._time() + self._limits.spill_ttl_seconds,
            internal=internal,
        )
        with self._lock:
            self._by_session.setdefault(session_id, {})[entry.source_id] = entry
        return entry

    def resolve(self, session_id: str, source_id: str) -> RegisteredSource:
        with self._lock:
            entry = self._by_session.get(session_id, {}).get(source_id)
        if entry is None:
            # A handle from another session is indistinguishable from an unknown one,
            # which is deliberate: cross-session probing learns nothing.
            raise ShuntError("SOURCE_EXPIRED", "UNKNOWN_HANDLE")
        if self._time() >= entry.expires_at_epoch:
            raise ShuntError("SOURCE_EXPIRED", "TTL_ELAPSED")
        return entry

    def is_internal(self, session_id: str, source_id: str) -> bool:
        """Registry-verified recursion guard. A payload claiming ``internal`` proves nothing."""
        try:
            return self.resolve(session_id, source_id).internal
        except ShuntError:
            return False

    def expire_session(self, session_id: str) -> int:
        with self._lock:
            removed = self._by_session.pop(session_id, {})
        return len(removed)

    def sweep(self) -> int:
        now = self._time()
        removed = 0
        with self._lock:
            for session_id, entries in list(self._by_session.items()):
                for source_id, entry in list(entries.items()):
                    if now >= entry.expires_at_epoch:
                        del entries[source_id]
                        removed += 1
                if not entries:
                    del self._by_session[session_id]
        return removed

    def count(self, session_id: str | None = None) -> int:
        with self._lock:
            if session_id is None:
                return sum(len(v) for v in self._by_session.values())
            return len(self._by_session.get(session_id, {}))
