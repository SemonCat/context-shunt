/**
 * Scope-bound source registry over the hybrid store.
 *
 * A `sourceId` is an opaque capability, not an address: it is minted by the store, it
 * carries the authorization decision that was made when the source was captured, and it
 * resolves only inside the trusted (host, profile, principal, session, generation) scope
 * that created it. Expired, revoked, closed-scope and stale-generation handles are refused
 * rather than silently re-fetching the underlying file - the source may have changed, and
 * answering from a newer version of it under an older snapshot hash would be a lie.
 *
 * This layer adds one thing to the store: rehydrating a payload into a `Snapshot` with its
 * line and record indexes. The payload is hash-verified on every load, and the rehydrated
 * snapshot is cached per handle because it is immutable by construction. The cache is
 * bounded so a session cannot pin more than a handful of payloads in memory.
 */
import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { Snapshot, snapshotBytes } from "./snapshot.js";
import {
  type Capture,
  type PublishedHandle,
  ScopeIdentity,
  SnapshotStore,
  expiresAtEpochOf,
  snapshotIdOf,
} from "./store.js";

export interface RegisteredSource {
  readonly sourceId: string;
  readonly sessionId: string;
  readonly snapshot: Snapshot;
  readonly expiresAtEpoch: number;
  readonly internal: boolean;
  readonly kind: string;
}

export class SourceRegistry {
  /**
   * How many rehydrated payloads one session may keep resident. One request may name up to
   * `maxSourcesPerRequest` sources, so the cache holds at least that many.
   */
  private static readonly CACHE_ENTRIES = 8;

  private readonly cache = new Map<string, Snapshot>();

  constructor(
    readonly store: SnapshotStore,
    readonly identity: ScopeIdentity,
    private readonly limits: Limits = DEFAULT_LIMITS,
  ) {}

  get sessionId(): string {
    return this.identity.session;
  }

  // -- capture ---------------------------------------------------------------

  /** Publish one snapshot. Convenience wrapper over the all-or-none batch path. */
  register(
    sessionId: string,
    snapshot: Snapshot,
    internal = false,
    kind = "shunted_read",
  ): RegisteredSource {
    return this.registerBatch(sessionId, [snapshot], internal, kind)[0] as RegisteredSource;
  }

  /**
   * Publish a batch. Every handle appears or none does: a multi-source capture that
   * half-succeeded would leave the caller holding handles for part of a request it will be
   * told was refused, so the store commits the whole batch in one transaction.
   */
  registerBatch(
    sessionId: string,
    snapshots: readonly Snapshot[],
    internal = false,
    kind = "shunted_read",
  ): RegisteredSource[] {
    this.assertSession(sessionId);
    const captures: Capture[] = snapshots.map((snapshot) => ({
      data: snapshot.data,
      mediaType: snapshot.mediaType,
      lineCount: snapshot.lineCount,
      kind,
      internal,
    }));
    const published = this.store.publish(this.identity, captures);
    return published.map((handle, index) => {
      const snapshot = snapshots[index] as Snapshot;
      if (snapshotIdOf(handle) !== snapshot.snapshotId) {
        // Content addressing guarantees this; asserting it makes a future change to either
        // side fail loudly instead of publishing a mislabelled handle.
        throw new ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", false);
      }
      this.remember(handle.handleId, snapshot);
      return this.entry(handle, snapshot);
    });
  }

  // -- resolution ------------------------------------------------------------

  resolve(sessionId: string, sourceId: string): RegisteredSource {
    this.assertSession(sessionId);
    const handle = this.store.resolve(this.identity, sourceId);
    return this.entry(handle, this.snapshotFor(handle));
  }

  /** Authorize without rehydrating the payload. */
  handle(sessionId: string, sourceId: string): PublishedHandle {
    this.assertSession(sessionId);
    return this.store.resolve(this.identity, sourceId);
  }

  /** Store-verified recursion guard. A payload claiming `internal` proves nothing. */
  isInternal(sessionId: string, sourceId: string): boolean {
    try {
      return this.handle(sessionId, sourceId).internal;
    } catch {
      return false;
    }
  }

  remove(sessionId: string, sourceId: string): boolean {
    this.assertSession(sessionId);
    this.cache.delete(sourceId);
    return this.store.revoke(this.identity, sourceId);
  }

  /** Revoke every handle in this scope. Only a real session boundary calls this. */
  expireSession(sessionId: string): number {
    this.assertSession(sessionId);
    this.cache.clear();
    return this.store.closeScope(this.identity);
  }

  sweep(): number {
    return this.store.sweep().expiredHandles;
  }

  count(sessionId?: string): number {
    if (sessionId !== undefined) this.assertSession(sessionId);
    return this.store.stats().handles;
  }

  // -- internals -------------------------------------------------------------

  /** A registry is bound to one scope; another session's id is not resolvable here. */
  private assertSession(sessionId: string): void {
    if (sessionId && sessionId !== this.identity.session) {
      throw new ShuntError("SOURCE_EXPIRED", "UNKNOWN_HANDLE");
    }
  }

  private entry(handle: PublishedHandle, snapshot: Snapshot): RegisteredSource {
    return {
      sourceId: handle.handleId,
      sessionId: this.identity.session,
      snapshot,
      expiresAtEpoch: expiresAtEpochOf(handle),
      internal: handle.internal,
      kind: handle.kind,
    };
  }

  private snapshotFor(handle: PublishedHandle): Snapshot {
    const cached = this.cache.get(handle.handleId);
    if (cached !== undefined && cached.snapshotId === snapshotIdOf(handle)) {
      // Refresh recency.
      this.cache.delete(handle.handleId);
      this.cache.set(handle.handleId, cached);
      return cached;
    }
    const data = this.store.loadPayload(handle);
    const snapshot = snapshotBytes(data, handle.mediaType, this.limits);
    if (snapshot.snapshotId !== snapshotIdOf(handle)) {
      throw new ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", false);
    }
    this.remember(handle.handleId, snapshot);
    return snapshot;
  }

  private remember(handleId: string, snapshot: Snapshot): void {
    this.cache.delete(handleId);
    this.cache.set(handleId, snapshot);
    while (this.cache.size > SourceRegistry.CACHE_ENTRIES) {
      const oldest = this.cache.keys().next();
      if (oldest.done) break;
      this.cache.delete(oldest.value);
    }
  }
}
