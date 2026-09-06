/**
 * Session-scoped source registry.
 *
 * A `sourceId` is an opaque capability, not an address: minted per session, carrying the
 * authorization decision made when the source was registered, and never resolving in
 * another session. Expired handles are refused rather than silently re-fetched.
 */
import { randomBytes } from "node:crypto";

import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits } from "./limits.js";
import { Snapshot } from "./snapshot.js";

export interface RegisteredSource {
  readonly sourceId: string;
  readonly sessionId: string;
  readonly snapshot: Snapshot;
  readonly expiresAtEpoch: number;
  readonly internal: boolean;
}

function mintId(): string {
  return "src_" + randomBytes(8).toString("hex");
}

export class SourceRegistry {
  private readonly bySession = new Map<string, Map<string, RegisteredSource>>();

  constructor(
    private readonly limits: Limits = DEFAULT_LIMITS,
    private timeFn: () => number = () => Date.now() / 1000,
  ) {}

  /** Test seam for TTL assertions; production always uses the wall clock. */
  setTimeFn(fn: () => number): void {
    this.timeFn = fn;
  }

  register(sessionId: string, snapshot: Snapshot, internal = false): RegisteredSource {
    if (!sessionId) throw new ShuntError("UNSAFE_SOURCE", "NO_SESSION");
    const entry: RegisteredSource = {
      sourceId: mintId(),
      sessionId,
      snapshot,
      expiresAtEpoch: this.timeFn() + this.limits.spillTtlSeconds,
      internal,
    };
    let bucket = this.bySession.get(sessionId);
    if (!bucket) {
      bucket = new Map();
      this.bySession.set(sessionId, bucket);
    }
    bucket.set(entry.sourceId, entry);
    return entry;
  }

  resolve(sessionId: string, sourceId: string): RegisteredSource {
    const entry = this.bySession.get(sessionId)?.get(sourceId);
    if (!entry) {
      // A handle from another session is indistinguishable from an unknown one, which is
      // deliberate: cross-session probing learns nothing.
      throw new ShuntError("SOURCE_EXPIRED", "UNKNOWN_HANDLE");
    }
    if (this.timeFn() >= entry.expiresAtEpoch) {
      throw new ShuntError("SOURCE_EXPIRED", "TTL_ELAPSED");
    }
    return entry;
  }

  /** Registry-verified recursion guard. A payload claiming `internal` proves nothing. */
  isInternal(sessionId: string, sourceId: string): boolean {
    try {
      return this.resolve(sessionId, sourceId).internal;
    } catch {
      return false;
    }
  }

  expireSession(sessionId: string): number {
    const removed = this.bySession.get(sessionId)?.size ?? 0;
    this.bySession.delete(sessionId);
    return removed;
  }

  count(sessionId?: string): number {
    if (sessionId === undefined) {
      let total = 0;
      for (const bucket of this.bySession.values()) total += bucket.size;
      return total;
    }
    return this.bySession.get(sessionId)?.size ?? 0;
  }
}
