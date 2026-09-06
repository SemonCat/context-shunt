/**
 * Hybrid snapshot store: SQLite owns authorization, private files own the bytes.
 *
 * This is the TypeScript half of a two-language implementation of one store. It is not a
 * reimplementation of the schema: both cores execute `contracts/store/v1.sql` verbatim,
 * which is what makes the DDL normative and lets a cross-language interoperability test
 * open the same store file with both implementations.
 *
 * SQLite holds only authorization and accounting state: opaque handle identity, session
 * scope and generation, TTL, quotas, content refcounts, disclosure totals, cleanup state
 * and bounded operation metrics. The immutable raw payload lives in a content-addressed
 * private file whose location is *derived* from the SHA-256 internally. No source path, no
 * question, no answer, no payload preview and no provider error body is ever written to the
 * database, and no filesystem path is stored or exposed.
 *
 * Publication order, for a capture batch, in this exact order:
 *
 * 1. the caller validates and authorizes the whole request and captures bounded bytes;
 * 2. every payload is written to a fresh temp file with `O_CREAT|O_EXCL|O_NOFOLLOW`,
 *    `fsync`-ed, `chmod` 0600, and atomically renamed into its content-addressed home;
 * 3. one SQLite transaction publishes every handle and takes every refcount together.
 *
 * No handle is usable before both the payload and its metadata are durable, and a batch
 * publishes all of its handles or none of them. A crash between (2) and (3) leaves an
 * orphan blob file with no row, which the sweep collects; it never leaves a usable handle.
 *
 * Readability is a SQL predicate, never file existence, so an expired, revoked,
 * closed-scope or stale-generation handle is unreadable the instant the predicate stops
 * holding. A hash collision or content mismatch raises `STORE_FAILED` and never deletes
 * the file: the mismatch may be a shared blob other live handles reference, and deleting it
 * would turn one corruption into many.
 *
 * `expires_at_ms` is wall-clock UTC milliseconds, which is what a second process and a
 * restart can compare. A clock rollback cannot revive an expired handle: every reading
 * takes `max(wallClock, clock_high_water_ms)`.
 *
 * The pre-1.1 spill layout wrote `<root>/<32-hex>/<digest>.spill`. Those files are *never*
 * imported as authorized handles - an unauthenticated file on disk is not a capability.
 */
import { createHash, randomBytes } from "node:crypto";
import {
  chmodSync,
  closeSync,
  constants as fsConstants,
  fsyncSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readSync,
  renameSync,
  statSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { join } from "node:path";
import { createRequire } from "node:module";
import type { DatabaseSync as DatabaseSyncType, StatementSync } from "node:sqlite";

import { ShuntError } from "./errors.js";
import { DEFAULT_LIMITS, Limits, storeDdl } from "./limits.js";
import type { TokenMethod } from "./provenance.js";

/**
 * `node:sqlite` is loaded through `createRequire` rather than a static import.
 *
 * It is a synchronous API, so a dynamic `import()` is not an option, and it is newer than
 * the builtin list some bundlers ship - a static `import` makes them try to resolve a
 * package called `sqlite` and fail. Requiring it at call time keeps the module opaque to
 * every bundler while staying exactly as synchronous as the store needs. The type-only
 * import above is erased at compile time and costs nothing at runtime.
 */
const nodeRequire = createRequire(import.meta.url);
type SqliteModule = { DatabaseSync: new (path: string) => DatabaseSyncType };
let sqliteModule: SqliteModule | undefined;

function openDatabase(path: string): DatabaseSyncType {
  sqliteModule ??= nodeRequire("node:sqlite") as SqliteModule;
  return new sqliteModule.DatabaseSync(path);
}

const DIR_MODE = 0o700;
const FILE_MODE = 0o600;
const DB_NAME = "store.sqlite3";
const BLOB_DIR = "blobs";
const TMP_DIR = "tmp";
const BLOB_SUFFIX = ".bin";
const LEGACY_SUFFIX = ".spill";
const READ_CHUNK = 256 * 1024;

/**
 * How far the clock may advance before the high-water mark is written back. Small enough
 * that a restart after a rollback loses at most this much protection, large enough that a
 * read-heavy session is not turned into a write-heavy one.
 */
const HIGH_WATER_GRANULARITY_MS = 1000;

export const HANDLE_KINDS: ReadonlySet<string> = new Set(["shunted_read", "spilled_tool"]);
export const DISCLOSURE_KINDS: ReadonlySet<string> = new Set(["lines", "bytes", "search"]);

// -- identities -------------------------------------------------------------

/**
 * The trusted identity a handle is scoped to.
 *
 * Every component comes from the host, never from a payload or a tool argument. The
 * components are digested before storage so no session name, account id or profile label is
 * retained. `generation` makes a stale or foreign handle unreplayable: a reset bumps the
 * generation and every earlier handle stops matching the predicate.
 */
export class ScopeIdentity {
  readonly host: string;
  readonly profile: string;
  readonly principal: string;
  readonly session: string;
  readonly generation: number;

  constructor(opts: {
    host: string;
    profile?: string;
    principal?: string;
    session: string;
    generation?: number;
  }) {
    this.host = opts.host;
    this.profile = opts.profile ?? "";
    this.principal = opts.principal ?? "";
    this.session = opts.session;
    this.generation = opts.generation ?? 1;
    if (!this.host || !this.session) {
      throw new ShuntError("STORE_FAILED", "SCOPE_INCOMPLETE", false);
    }
    if (!Number.isSafeInteger(this.generation) || this.generation < 1) {
      throw new ShuntError("STORE_FAILED", "SCOPE_INCOMPLETE", false);
    }
  }

  get scopeId(): string {
    return "scp_" + digest(this.material()).slice(0, 32);
  }

  private material(): string {
    return [this.host, this.profile, this.principal, this.session, String(this.generation)]
      .join("");
  }

  columns(): [string, string, string, string, number] {
    return [
      digest(this.host),
      digest(this.profile),
      digest(this.principal),
      digest(this.session),
      this.generation,
    ];
  }

  withGeneration(generation: number): ScopeIdentity {
    return new ScopeIdentity({
      host: this.host,
      profile: this.profile,
      principal: this.principal,
      session: this.session,
      generation,
    });
  }
}

function digest(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

/**
 * One payload offered for publication. `data` is already validated, already bounded and
 * already proven text by the caller; the store checks size against the configured caps and
 * nothing else about its meaning.
 */
export interface Capture {
  readonly data: Uint8Array;
  readonly mediaType: string;
  readonly lineCount: number;
  readonly kind?: string;
  readonly internal?: boolean;
}

export function captureHash(capture: Capture): string {
  return createHash("sha256").update(capture.data).digest("hex");
}

export interface PublishedHandle {
  readonly handleId: string;
  readonly scopeId: string;
  readonly blobHash: string;
  readonly mediaType: string;
  readonly bytesLen: number;
  readonly lineCount: number;
  readonly kind: string;
  readonly internal: boolean;
  readonly createdAtMs: number;
  readonly expiresAtMs: number;
}

export function snapshotIdOf(handle: PublishedHandle): string {
  return `sha256:${handle.blobHash}`;
}

export function expiresAtEpochOf(handle: PublishedHandle): number {
  return handle.expiresAtMs / 1000;
}

export interface DisclosureAllowance {
  readonly perSourceRemaining: number;
  readonly perSessionRemaining: number;
}

export function allowanceRemaining(allowance: DisclosureAllowance): number {
  return Math.max(0, Math.min(allowance.perSourceRemaining, allowance.perSessionRemaining));
}

export interface DisclosureCharge {
  readonly granted: boolean;
  readonly chargedBytes: number;
  readonly disclosedBytesSource: number;
  readonly disclosedBytesSession: number;
  readonly limitReached: boolean;
}

/**
 * Bounded non-content metadata for one shunt operation. Every field is a closed enum, a
 * byte count or a token count. `undefined` in a token column means "not reported" and is
 * stored as SQL NULL so it can never be read back as zero.
 */
export interface OperationRecord {
  readonly operationId: string;
  readonly kind: string;
  readonly status: string;
  readonly code: string;
  readonly rawInputBytes: number;
  readonly rawInputBaselineTokens: number | undefined;
  readonly baselineKind: string;
  readonly baselineMethod: TokenMethod | string;
  readonly baselineCreditTokens: number;
  readonly mainModelEnvelopeBytes: number;
  readonly mainModelEnvelopeTokens: number;
  readonly envelopeTokenMethod: TokenMethod | string;
  readonly readerInputTokens: number | undefined;
  readonly readerOutputTokens: number | undefined;
  readonly readerCacheTokens: number | undefined;
  readonly readerTokenMethod: TokenMethod | string;
  readonly attemptsStarted: number;
  readonly attemptsUsageComplete: number;
  readonly deliveryBoundary: string;
  readonly mainContextTokensSaved: number;
  readonly netTokensSaved: number;
}

export interface SweepReport {
  readonly expiredHandles: number;
  readonly deletedBlobs: number;
  readonly removedTemps: number;
  readonly orphanBlobFiles: number;
}

export interface StoreStats {
  readonly handles: number;
  readonly blobs: number;
  readonly bytes: number;
}

function mintHandleId(): string {
  return "src_" + randomBytes(8).toString("hex");
}

type Row = Record<string, unknown>;

// -- the store --------------------------------------------------------------

export class SnapshotStore {
  private db: DatabaseSyncType | undefined;
  private highWater = 0;
  private persistedHighWater = 0;

  constructor(
    readonly root: string,
    private readonly limits: Limits = DEFAULT_LIMITS,
    private readonly wallClockMs: () => number = () => Date.now(),
  ) {
    this.prepareDirectories();
  }

  close(): void {
    try {
      this.db?.close();
    } catch {
      // A store that is already closed is closed.
    }
    this.db = undefined;
  }

  private prepareDirectories(): void {
    for (const path of [this.root, join(this.root, BLOB_DIR), join(this.root, TMP_DIR)]) {
      try {
        mkdirSync(path, { recursive: true, mode: DIR_MODE });
      } catch {
        throw new ShuntError("STORE_FAILED", "UNSAFE_CACHE_PATH", false);
      }
      assertPrivateDirectory(path);
    }
  }

  private connect(): DatabaseSyncType {
    if (this.db) return this.db;
    let db: DatabaseSyncType;
    try {
      db = openDatabase(join(this.root, DB_NAME));
      db.exec(`PRAGMA busy_timeout = ${Math.trunc(this.limits.storeBusyTimeoutMs)}`);
      db.exec("PRAGMA foreign_keys = ON");
      db.exec("PRAGMA synchronous = FULL");
      db.exec(storeDdl());
      try {
        chmodSync(join(this.root, DB_NAME), FILE_MODE);
      } catch {
        // Not every filesystem honours the mode; the directory is already 0700.
      }
    } catch {
      throw new ShuntError("STORE_FAILED", "OPEN_FAILED", false);
    }
    this.db = db;
    this.bootstrapMetadata(db);
    return db;
  }

  /** Seed the singletons and refuse a store written by an incompatible revision. */
  private bootstrapMetadata(db: DatabaseSyncType): void {
    try {
      this.writeTxn(db, () => {
        const insert = db.prepare(
          "INSERT OR IGNORE INTO store_metadata (key, value) VALUES (?, ?)",
        );
        insert.run("ddl_version", String(this.limits.storeDdlVersion));
        insert.run("store_id", randomBytes(16).toString("hex"));
        insert.run("clock_high_water_ms", "0");
        insert.run("cursor_key", randomBytes(32).toString("hex"));
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "MIGRATION_FAILED", false);
    }
    if (this.metadata(db, "ddl_version") !== String(this.limits.storeDdlVersion)) {
      // A store from a different DDL revision is refused rather than migrated in place by
      // guesswork; docs/install.md documents the supported path.
      throw new ShuntError("STORE_FAILED", "DDL_VERSION_MISMATCH", false);
    }
    this.highWater = Number(this.metadata(db, "clock_high_water_ms") ?? "0");
    this.persistedHighWater = this.highWater;
  }

  private metadata(db: DatabaseSyncType, key: string): string | undefined {
    const row = db.prepare("SELECT value FROM store_metadata WHERE key = ?").get(key) as
      | Row
      | undefined;
    return row === undefined ? undefined : String(row["value"]);
  }

  ddlVersion(): number {
    return Number(this.metadata(this.connect(), "ddl_version") ?? "0");
  }

  /** Per-store secret used to authenticate continuation cursors. */
  cursorKey(): Uint8Array {
    const value = this.metadata(this.connect(), "cursor_key");
    if (!value) throw new ShuntError("STORE_FAILED", "CURSOR_KEY_MISSING", false);
    return Uint8Array.from(Buffer.from(value, "hex"));
  }

  // -- clock -----------------------------------------------------------------

  /**
   * Non-decreasing wall-clock milliseconds. A backwards clock yields the high-water mark
   * instead, so a handle that has expired stays expired across a rollback, a restart or a
   * second process.
   */
  nowMs(): number {
    return this.tick();
  }

  /**
   * Read the clock and keep the persisted high-water mark roughly current. Read paths take
   * no write transaction of their own, so without this a long run of pure reads would leave
   * the persisted mark far behind and a restart after a rollback could revive an expired
   * handle. Persisting is amortised past `HIGH_WATER_GRANULARITY_MS`.
   */
  private tick(): number {
    const db = this.connect();
    const now = this.nowLocked(db);
    if (now >= this.persistedHighWater + HIGH_WATER_GRANULARITY_MS) {
      try {
        this.writeTxn(db, () => this.bumpHighWater(db, now));
      } catch {
        // A busy store just means another writer is ahead of us.
      }
    }
    return now;
  }

  private nowLocked(db: DatabaseSyncType): number {
    const stored = Number(this.metadata(db, "clock_high_water_ms") ?? "0");
    this.highWater = Math.max(this.highWater, stored);
    this.persistedHighWater = Math.max(this.persistedHighWater, stored);
    return Math.max(Math.trunc(this.wallClockMs()), this.highWater);
  }

  /** Persist the mark. Must be called inside a write transaction. */
  private bumpHighWater(db: DatabaseSyncType, now: number): void {
    if (now > this.persistedHighWater) {
      this.highWater = Math.max(this.highWater, now);
      this.persistedHighWater = now;
      db.prepare("UPDATE store_metadata SET value = ? WHERE key = ?").run(
        String(now),
        "clock_high_water_ms",
      );
    }
  }

  /** BEGIN IMMEDIATE so a writer conflict surfaces as busy-timeout, not a late abort. */
  private writeTxn<T>(db: DatabaseSyncType, body: () => T): T {
    db.exec("BEGIN IMMEDIATE");
    let result: T;
    try {
      result = body();
    } catch (err) {
      try {
        db.exec("ROLLBACK");
      } catch {
        // Rolling back a transaction the engine already aborted is a no-op.
      }
      throw err;
    }
    db.exec("COMMIT");
    return result;
  }

  // -- scopes ----------------------------------------------------------------

  /** Create or reuse the scope row for this identity and generation. */
  openScope(identity: ScopeIdentity): string {
    const db = this.connect();
    const [host, profile, principal, session, generation] = identity.columns();
    try {
      this.writeTxn(db, () => {
        const now = this.nowLocked(db);
        this.bumpHighWater(db, now);
        db.prepare(
          "INSERT OR IGNORE INTO scopes "
            + "(scope_id, host, profile, principal, session, generation, created_at_ms) "
            + "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ).run(identity.scopeId, host, profile, principal, session, generation, now);
        // Reopening an identity that was closed earlier in the same generation is a
        // resume, not a new scope: clear the close marker.
        db.prepare("UPDATE scopes SET closed_at_ms = NULL WHERE scope_id = ?")
          .run(identity.scopeId);
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "SCOPE_OPEN_FAILED", false);
    }
    return identity.scopeId;
  }

  /**
   * Close a scope. With `revoke` its handles become unreadable immediately.
   *
   * Ordinary per-turn events must not call this: on Hermes `on_session_end` fires at the
   * end of every `run_conversation` call and OpenClaw fires `session_end` with
   * `reason: "compaction"` mid-conversation, so closing there would destroy the recovery
   * handles the next turn needs. Only a real finalize/reset boundary closes a scope.
   */
  closeScope(identity: ScopeIdentity, revoke = true): number {
    const db = this.connect();
    let revoked = 0;
    try {
      revoked = this.writeTxn(db, () => {
        const now = this.nowLocked(db);
        this.bumpHighWater(db, now);
        db.prepare(
          "UPDATE scopes SET closed_at_ms = ? WHERE scope_id = ? AND closed_at_ms IS NULL",
        ).run(now, identity.scopeId);
        if (!revoke) return 0;
        const result = db.prepare(
          "UPDATE handles SET revoked = 1 WHERE scope_id = ? AND revoked = 0",
        ).run(identity.scopeId);
        const count = Number(result.changes ?? 0);
        this.releaseRefcounts(db, identity.scopeId);
        return count;
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "SCOPE_CLOSE_FAILED", false);
    }
    this.collectPendingBlobs();
    return revoked;
  }

  /** Drop the refcount each revoked handle held, inside the caller's transaction. */
  private releaseRefcounts(db: DatabaseSyncType, scopeId: string): void {
    const rows = db.prepare(
      "SELECT blob_hash, COUNT(*) AS n FROM handles WHERE scope_id = ? AND revoked = 1 "
        + "GROUP BY blob_hash",
    ).all(scopeId) as Row[];
    const decrement = db.prepare(
      "UPDATE blobs SET refcount = MAX(0, refcount - ?) WHERE hash = ?",
    );
    for (const row of rows) decrement.run(Number(row["n"]), String(row["blob_hash"]));
    db.prepare("DELETE FROM handles WHERE scope_id = ? AND revoked = 1").run(scopeId);
    db.exec("UPDATE blobs SET pending_delete = 1 WHERE refcount = 0 AND pending_delete = 0");
  }

  // -- publication -----------------------------------------------------------

  /**
   * Publish a capture batch. Every handle appears, or none does.
   *
   * Storage failure never degrades into raw passthrough: the caller's operation stays
   * blocked and a bounded `STORE_FAILED` is thrown with no handle.
   */
  publish(identity: ScopeIdentity, captures: readonly Capture[]): PublishedHandle[] {
    if (captures.length === 0) return [];
    if (captures.length > this.limits.maxSourcesPerRequest) {
      throw new ShuntError("STORE_FAILED", "BATCH_TOO_LARGE", false);
    }
    for (const capture of captures) {
      if (capture.data.length > this.limits.maxSourceBytes) {
        throw new ShuntError("LIMIT_EXCEEDED", "SOURCE_OVER_BYTE_CAP", false);
      }
      if (capture.kind !== undefined && !HANDLE_KINDS.has(capture.kind)) {
        throw new ShuntError("STORE_FAILED", "BAD_HANDLE_KIND", false);
      }
    }

    const scopeId = this.openScope(identity);
    const staged: Array<{ capture: Capture; hash: string }> = [];
    const tempIds: string[] = [];
    try {
      for (const capture of captures) {
        const hash = captureHash(capture);
        const tempId = this.stageBlob(capture, hash);
        if (tempId !== undefined) tempIds.push(tempId);
        staged.push({ capture, hash });
      }
    } catch (err) {
      this.discardTempIds(tempIds);
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "WRITE_FAILED", false);
    }

    const db = this.connect();
    try {
      return this.writeTxn(db, () => {
        const now = this.nowLocked(db);
        this.bumpHighWater(db, now);
        this.assertScopeOpen(db, identity);
        this.assertCapacity(db, staged);
        const expires = now + this.limits.storeHandleTtlSeconds * 1000;
        const published: PublishedHandle[] = [];
        const insertBlob = db.prepare(
          "INSERT INTO blobs (hash, bytes, media_type, line_count, refcount, pending_delete, "
            + "created_at_ms) VALUES (?, ?, ?, ?, 0, 0, ?) ON CONFLICT(hash) DO NOTHING",
        );
        const bumpBlob = db.prepare(
          "UPDATE blobs SET refcount = refcount + 1, pending_delete = 0 WHERE hash = ?",
        );
        const insertHandle = db.prepare(
          "INSERT INTO handles (handle_id, scope_id, blob_hash, kind, internal, generation, "
            + "created_at_ms, expires_at_ms, revoked, baseline_credited, disclosed_bytes) "
            + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0)",
        );
        for (const { capture, hash } of staged) {
          insertBlob.run(
            hash,
            capture.data.length,
            capture.mediaType,
            capture.lineCount,
            now,
          );
          bumpBlob.run(hash);
          const handleId = mintHandleId();
          const kind = capture.kind ?? "shunted_read";
          insertHandle.run(
            handleId,
            scopeId,
            hash,
            kind,
            capture.internal ? 1 : 0,
            identity.generation,
            now,
            expires,
          );
          published.push({
            handleId,
            scopeId,
            blobHash: hash,
            mediaType: capture.mediaType,
            bytesLen: capture.data.length,
            lineCount: capture.lineCount,
            kind,
            internal: Boolean(capture.internal),
            createdAtMs: now,
            expiresAtMs: expires,
          });
        }
        const dropTemp = db.prepare("DELETE FROM orphan_temps WHERE temp_id = ?");
        for (const tempId of tempIds) dropTemp.run(tempId);
        return published;
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      // Nothing committed, so no handle exists. The renamed blob files carry no row and
      // the sweep collects them; a partially published batch is impossible by construction.
      throw new ShuntError("STORE_FAILED", "PUBLISH_FAILED", false);
    }
  }

  /** Write and rename one payload. Returns the temp id, or `undefined` when deduped. */
  private stageBlob(capture: Capture, hash: string): string | undefined {
    const final = this.blobPath(hash);
    const existing = hashFile(final);
    if (existing !== undefined) {
      if (existing !== hash) {
        // Content-addressed storage says this file must hash to `hash`. It does not. Fail
        // closed and leave it alone: other live handles may reference it, and deleting it
        // would widen the damage.
        throw new ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", false);
      }
      return undefined;
    }

    const parent = final.slice(0, final.lastIndexOf("/"));
    mkdirSync(parent, { recursive: true, mode: DIR_MODE });
    assertPrivateDirectory(parent);
    const tempId = `${hash}.${randomBytes(8).toString("hex")}`;
    const temp = join(this.root, TMP_DIR, `${tempId}.part`);
    this.recordTemp(tempId, hash);
    const flags =
      fsConstants.O_WRONLY
      | fsConstants.O_CREAT
      | fsConstants.O_EXCL
      | (fsConstants.O_NOFOLLOW ?? 0);
    const fd = openSync(temp, flags, FILE_MODE);
    try {
      let written = 0;
      while (written < capture.data.length) {
        const end = Math.min(capture.data.length, written + READ_CHUNK);
        written += writeSync(fd, capture.data, written, end - written);
      }
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    chmodSync(temp, FILE_MODE);
    renameSync(temp, final);
    fsyncDir(parent);
    return tempId;
  }

  private recordTemp(tempId: string, hash: string): void {
    const db = this.connect();
    try {
      this.writeTxn(db, () => {
        const now = this.nowLocked(db);
        db.prepare(
          "INSERT OR REPLACE INTO orphan_temps (temp_id, blob_hash, created_at_ms) "
            + "VALUES (?, ?, ?)",
        ).run(tempId, hash, now);
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "TEMP_RECORD_FAILED", false);
    }
  }

  private discardTempIds(tempIds: readonly string[]): void {
    for (const tempId of tempIds) unlinkQuiet(join(this.root, TMP_DIR, `${tempId}.part`));
    if (tempIds.length === 0) return;
    try {
      const db = this.connect();
      this.writeTxn(db, () => {
        const remove = db.prepare("DELETE FROM orphan_temps WHERE temp_id = ?");
        for (const tempId of tempIds) remove.run(tempId);
      });
    } catch {
      // The startup sweep clears anything left behind.
    }
  }

  private assertScopeOpen(db: DatabaseSyncType, identity: ScopeIdentity): void {
    const row = db.prepare(
      "SELECT generation, closed_at_ms FROM scopes WHERE scope_id = ?",
    ).get(identity.scopeId) as Row | undefined;
    if (row === undefined || row["closed_at_ms"] !== null) {
      throw new ShuntError("SOURCE_EXPIRED", "SCOPE_CLOSED", false);
    }
    if (Number(row["generation"]) !== identity.generation) {
      throw new ShuntError("SOURCE_EXPIRED", "STALE_GENERATION", false);
    }
  }

  private assertCapacity(
    db: DatabaseSyncType,
    staged: ReadonlyArray<{ capture: Capture; hash: string }>,
  ): void {
    const handleRow = db.prepare(
      "SELECT COUNT(*) AS handles FROM handles WHERE revoked = 0",
    ).get() as Row;
    const blobRow = db.prepare(
      "SELECT COALESCE(SUM(bytes), 0) AS total FROM blobs WHERE pending_delete = 0",
    ).get() as Row;
    const handles = Number(handleRow["handles"]) + staged.length;
    if (handles > this.limits.storeMaxEntries) {
      throw new ShuntError("LIMIT_EXCEEDED", "STORE_ENTRY_QUOTA", false);
    }
    const known = new Set(
      (db.prepare("SELECT hash FROM blobs").all() as Row[]).map((row) => String(row["hash"])),
    );
    const added = staged
      .filter(({ hash }) => !known.has(hash))
      .reduce((total, { capture }) => total + capture.data.length, 0);
    if (Number(blobRow["total"]) + added > this.limits.storeMaxBytes) {
      throw new ShuntError("LIMIT_EXCEEDED", "STORE_BYTE_QUOTA", false);
    }
  }

  // -- authorization ---------------------------------------------------------

  /**
   * Authorize one handle. Throws rather than re-reading the underlying source.
   *
   * A handle from another scope, another generation, a closed scope or past its TTL is
   * indistinguishable from an unknown one, which is deliberate: cross-scope probing learns
   * nothing.
   */
  resolve(identity: ScopeIdentity, handleId: string, snapshotId?: string): PublishedHandle {
    const now = this.tick();
    const db = this.connect();
    const row = db.prepare(
      "SELECT h.handle_id, h.scope_id, h.blob_hash, h.kind, h.internal, h.created_at_ms, "
        + "       h.expires_at_ms, b.bytes, b.media_type, b.line_count "
        + "  FROM handles h "
        + "  JOIN scopes s ON s.scope_id = h.scope_id "
        + "  JOIN blobs  b ON b.hash     = h.blob_hash "
        + " WHERE h.handle_id = ? AND h.scope_id = ? AND h.revoked = 0 "
        + "   AND h.expires_at_ms > ? AND s.closed_at_ms IS NULL AND s.generation = ?",
    ).get(handleId, identity.scopeId, now, identity.generation) as Row | undefined;
    if (row === undefined) {
      throw new ShuntError("SOURCE_EXPIRED", this.refusalDetail(identity, handleId, now));
    }
    const handle: PublishedHandle = {
      handleId: String(row["handle_id"]),
      scopeId: String(row["scope_id"]),
      blobHash: String(row["blob_hash"]),
      mediaType: String(row["media_type"]),
      bytesLen: Number(row["bytes"]),
      lineCount: Number(row["line_count"]),
      kind: String(row["kind"]),
      internal: Boolean(Number(row["internal"])),
      createdAtMs: Number(row["created_at_ms"]),
      expiresAtMs: Number(row["expires_at_ms"]),
    };
    if (snapshotId !== undefined && snapshotId !== snapshotIdOf(handle)) {
      throw new ShuntError("SOURCE_CHANGED", "SNAPSHOT_MISMATCH");
    }
    return handle;
  }

  /**
   * Why a handle was refused - accurately, but only about our own scope.
   *
   * The lookup is scoped to `identity.scopeId`, so a handle belonging to another session,
   * principal or generation is reported as `UNKNOWN_HANDLE` and cross-scope probing still
   * learns nothing. Inside the caller's own scope there is nothing to protect - they
   * already hold the handle - so the honest reason is returned and the citation verifier
   * can distinguish an expiry from a typo.
   */
  private refusalDetail(identity: ScopeIdentity, handleId: string, now: number): string {
    const db = this.connect();
    const row = db.prepare(
      "SELECT h.revoked, h.expires_at_ms, s.closed_at_ms, s.generation "
        + "  FROM handles h JOIN scopes s ON s.scope_id = h.scope_id "
        + " WHERE h.handle_id = ? AND h.scope_id = ?",
    ).get(handleId, identity.scopeId) as Row | undefined;
    if (row === undefined) return "UNKNOWN_HANDLE";
    if (Number(row["revoked"])) return "REVOKED";
    if (row["closed_at_ms"] !== null) return "SCOPE_CLOSED";
    if (Number(row["generation"]) !== identity.generation) return "STALE_GENERATION";
    if (Number(row["expires_at_ms"]) <= now) return "TTL_ELAPSED";
    return "UNKNOWN_HANDLE";
  }

  /** Store-verified recursion guard. A payload claiming `internal` proves nothing. */
  isInternal(identity: ScopeIdentity, handleId: string): boolean {
    try {
      return this.resolve(identity, handleId).internal;
    } catch {
      return false;
    }
  }

  /** Read the immutable payload and re-verify it against the handle's hash. */
  loadPayload(handle: PublishedHandle): Uint8Array {
    const data = readPrivate(this.blobPath(handle.blobHash), handle.bytesLen);
    if (createHash("sha256").update(data).digest("hex") !== handle.blobHash) {
      throw new ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", false);
    }
    return data;
  }

  revoke(identity: ScopeIdentity, handleId: string): boolean {
    const db = this.connect();
    let removed = false;
    try {
      removed = this.writeTxn(db, () => {
        const row = db.prepare(
          "SELECT blob_hash FROM handles WHERE handle_id = ? AND scope_id = ?",
        ).get(handleId, identity.scopeId) as Row | undefined;
        if (row === undefined) return false;
        const hash = String(row["blob_hash"]);
        db.prepare("DELETE FROM handles WHERE handle_id = ? AND scope_id = ?")
          .run(handleId, identity.scopeId);
        db.prepare("UPDATE blobs SET refcount = MAX(0, refcount - 1) WHERE hash = ?").run(hash);
        db.prepare("UPDATE blobs SET pending_delete = 1 WHERE hash = ? AND refcount = 0")
          .run(hash);
        return true;
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "REVOKE_FAILED", false);
    }
    this.collectPendingBlobs();
    return removed;
  }

  // -- baseline credit -------------------------------------------------------

  /**
   * Claim the one-time withheld-source baseline for this snapshot. Returns `true` exactly
   * once per handle, so refined questions, failed retries and inspect pages add their own
   * overhead without re-claiming the saving.
   */
  creditBaseline(identity: ScopeIdentity, handleId: string): boolean {
    const db = this.connect();
    try {
      return this.writeTxn(db, () => {
        const now = this.nowLocked(db);
        this.bumpHighWater(db, now);
        const result = db.prepare(
          "UPDATE handles SET baseline_credited = 1 "
            + " WHERE handle_id = ? AND scope_id = ? AND baseline_credited = 0 "
            + "   AND revoked = 0 AND expires_at_ms > ?",
        ).run(handleId, identity.scopeId, now);
        return Number(result.changes ?? 0) > 0;
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "BASELINE_FAILED", false);
    }
  }

  // -- disclosure ------------------------------------------------------------

  disclosureAllowance(identity: ScopeIdentity, handleId: string): DisclosureAllowance {
    const now = this.tick();
    const db = this.connect();
    const source = db.prepare(
      "SELECT disclosed_bytes FROM handles "
        + " WHERE handle_id = ? AND scope_id = ? AND revoked = 0 AND expires_at_ms > ?",
    ).get(handleId, identity.scopeId, now) as Row | undefined;
    if (source === undefined) throw new ShuntError("SOURCE_EXPIRED", "UNKNOWN_HANDLE");
    const session = db.prepare(
      "SELECT COALESCE(SUM(bytes), 0) AS total FROM disclosure_events WHERE scope_id = ?",
    ).get(identity.scopeId) as Row;
    return {
      perSourceRemaining: Math.max(
        0,
        this.limits.disclosureMaxPerSourceBytes - Number(source["disclosed_bytes"]),
      ),
      perSessionRemaining: Math.max(
        0,
        this.limits.disclosureMaxPerSessionBytes - Number(session["total"]),
      ),
    };
  }

  /**
   * Check and increment the disclosure counters in one transaction.
   *
   * This runs *before* any byte is returned, so two concurrent inspects cannot overshoot
   * the ceiling between them. The exact byte count the caller is about to emit is charged;
   * if the allowance no longer covers it, nothing is charged and nothing is disclosed.
   */
  chargeDisclosure(
    identity: ScopeIdentity,
    handleId: string,
    kind: string,
    wantBytes: number,
  ): DisclosureCharge {
    if (!DISCLOSURE_KINDS.has(kind)) {
      throw new ShuntError("STORE_FAILED", "BAD_DISCLOSURE_KIND", false);
    }
    if (wantBytes < 0) throw new ShuntError("STORE_FAILED", "BAD_DISCLOSURE_BYTES", false);
    const db = this.connect();
    try {
      return this.writeTxn(db, () => {
        const now = this.nowLocked(db);
        this.bumpHighWater(db, now);
        const source = db.prepare(
          "SELECT disclosed_bytes FROM handles "
            + " WHERE handle_id = ? AND scope_id = ? AND revoked = 0 AND expires_at_ms > ?",
        ).get(handleId, identity.scopeId, now) as Row | undefined;
        if (source === undefined) throw new ShuntError("SOURCE_EXPIRED", "UNKNOWN_HANDLE");
        const usedSource = Number(source["disclosed_bytes"]);
        const sessionRow = db.prepare(
          "SELECT COALESCE(SUM(bytes), 0) AS total FROM disclosure_events WHERE scope_id = ?",
        ).get(identity.scopeId) as Row;
        const usedSession = Number(sessionRow["total"]);
        const sourceCap = this.limits.disclosureMaxPerSourceBytes;
        const sessionCap = this.limits.disclosureMaxPerSessionBytes;
        const fits =
          usedSource + wantBytes <= sourceCap && usedSession + wantBytes <= sessionCap;
        if (!fits) {
          return {
            granted: false,
            chargedBytes: 0,
            disclosedBytesSource: usedSource,
            disclosedBytesSession: usedSession,
            limitReached: true,
          };
        }
        if (wantBytes > 0) {
          // A page that disclosed nothing has nothing to account for. Writing a zero row
          // anyway would let a caller paging a fruitless search grow an uncapped table
          // without ever disclosing a byte.
          db.prepare(
            "UPDATE handles SET disclosed_bytes = disclosed_bytes + ? "
              + " WHERE handle_id = ? AND scope_id = ?",
          ).run(wantBytes, handleId, identity.scopeId);
          db.prepare(
            "INSERT INTO disclosure_events (scope_id, handle_id, kind, bytes, at_ms) "
              + "VALUES (?, ?, ?, ?, ?)",
          ).run(identity.scopeId, handleId, kind, wantBytes, now);
        }
        return {
          granted: true,
          chargedBytes: wantBytes,
          disclosedBytesSource: usedSource + wantBytes,
          disclosedBytesSession: usedSession + wantBytes,
          limitReached:
            usedSource + wantBytes >= sourceCap || usedSession + wantBytes >= sessionCap,
        };
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "DISCLOSURE_FAILED", false);
    }
  }

  disclosedBytes(identity: ScopeIdentity): number {
    const row = this.connect().prepare(
      "SELECT COALESCE(SUM(bytes), 0) AS total FROM disclosure_events WHERE scope_id = ?",
    ).get(identity.scopeId) as Row;
    return Number(row["total"]);
  }

  // -- accounting ------------------------------------------------------------

  recordOperation(identity: ScopeIdentity, record: OperationRecord): void {
    const scopeId = this.openScope(identity);
    const db = this.connect();
    try {
      this.writeTxn(db, () => {
        const now = this.nowLocked(db);
        this.bumpHighWater(db, now);
        db.prepare(
          "INSERT OR REPLACE INTO accounting_events ("
            + " operation_id, scope_id, kind, status, code, raw_input_bytes,"
            + " raw_input_baseline_tokens, baseline_kind, baseline_method,"
            + " baseline_credit_tokens, main_model_envelope_bytes,"
            + " main_model_envelope_tokens, envelope_token_method,"
            + " reader_input_tokens, reader_output_tokens, reader_cache_tokens,"
            + " reader_token_method, attempts_started, attempts_usage_complete,"
            + " delivery_boundary, main_context_tokens_saved, net_tokens_saved, at_ms"
            + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ).run(
          record.operationId,
          scopeId,
          record.kind,
          record.status,
          record.code,
          record.rawInputBytes,
          record.rawInputBaselineTokens ?? null,
          record.baselineKind,
          String(record.baselineMethod),
          record.baselineCreditTokens,
          record.mainModelEnvelopeBytes,
          record.mainModelEnvelopeTokens,
          String(record.envelopeTokenMethod),
          record.readerInputTokens ?? null,
          record.readerOutputTokens ?? null,
          record.readerCacheTokens ?? null,
          String(record.readerTokenMethod),
          record.attemptsStarted,
          record.attemptsUsageComplete,
          record.deliveryBoundary,
          record.mainContextTokensSaved,
          record.netTokensSaved,
          now,
        );
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "ACCOUNTING_FAILED", false);
    }
  }

  operationCount(identity: ScopeIdentity): number {
    const row = this.connect().prepare(
      "SELECT COUNT(*) AS n FROM accounting_events WHERE scope_id = ?",
    ).get(identity.scopeId) as Row;
    return Number(row["n"]);
  }

  /**
   * One bounded page of this scope's own records, oldest first.
   *
   * Ordering is ascending on `(at_ms, operation_id)` deliberately. The log is append-only,
   * so ascending order means a record written *between* two page reads lands at the end and
   * never shifts a page the caller already walked. Newest-first ordering would skew every
   * offset each time a new operation was recorded - and reading stats records an operation
   * of its own, so that skew is guaranteed rather than hypothetical.
   */
  operationPage(
    identity: ScopeIdentity,
    opts: { page: number; pageSize: number },
  ): OperationRecord[] {
    const page = Math.max(1, Math.min(opts.page, this.limits.statsMaxPages));
    const pageSize = Math.max(1, Math.min(opts.pageSize, this.limits.statsMaxRecordsPerPage));
    const rows = this.connect().prepare(
      "SELECT * FROM accounting_events WHERE scope_id = ? "
        + " ORDER BY at_ms ASC, operation_id ASC LIMIT ? OFFSET ?",
    ).all(identity.scopeId, pageSize, (page - 1) * pageSize) as Row[];
    return rows.map(recordFromRow);
  }

  /** Signed aggregates for this scope. A NULL column stays `undefined`, never 0. */
  operationTotals(identity: ScopeIdentity): {
    operations: number;
    rawInputBytes: number;
    baselineCreditTokens: number;
    mainModelEnvelopeTokens: number;
    readerInputTokens: number | undefined;
    readerOutputTokens: number | undefined;
    readerCacheTokens: number | undefined;
    mainContextTokensSaved: number;
    netTokensSaved: number;
    attemptsStarted: number;
    attemptsUsageComplete: number;
    disclosedBytes: number;
  } {
    const row = this.connect().prepare(
      "SELECT COUNT(*) AS operations,"
        + "       COALESCE(SUM(raw_input_bytes), 0) AS raw_input_bytes,"
        + "       COALESCE(SUM(baseline_credit_tokens), 0) AS baseline_credit_tokens,"
        + "       COALESCE(SUM(main_model_envelope_tokens), 0) AS main_model_envelope_tokens,"
        + "       SUM(reader_input_tokens) AS reader_input_tokens,"
        + "       SUM(reader_output_tokens) AS reader_output_tokens,"
        + "       SUM(reader_cache_tokens) AS reader_cache_tokens,"
        + "       COALESCE(SUM(main_context_tokens_saved), 0) AS main_context_tokens_saved,"
        + "       COALESCE(SUM(net_tokens_saved), 0) AS net_tokens_saved,"
        + "       COALESCE(SUM(attempts_started), 0) AS attempts_started,"
        + "       COALESCE(SUM(attempts_usage_complete), 0) AS attempts_usage_complete"
        + "  FROM accounting_events WHERE scope_id = ?",
    ).get(identity.scopeId) as Row;
    return {
      operations: Number(row["operations"]),
      rawInputBytes: Number(row["raw_input_bytes"]),
      baselineCreditTokens: Number(row["baseline_credit_tokens"]),
      mainModelEnvelopeTokens: Number(row["main_model_envelope_tokens"]),
      readerInputTokens: optionalNumber(row["reader_input_tokens"]),
      readerOutputTokens: optionalNumber(row["reader_output_tokens"]),
      readerCacheTokens: optionalNumber(row["reader_cache_tokens"]),
      mainContextTokensSaved: Number(row["main_context_tokens_saved"]),
      netTokensSaved: Number(row["net_tokens_saved"]),
      attemptsStarted: Number(row["attempts_started"]),
      attemptsUsageComplete: Number(row["attempts_usage_complete"]),
      disclosedBytes: this.disclosedBytes(identity),
    };
  }

  // -- maintenance -----------------------------------------------------------

  /**
   * Startup recovery: clear staged temps, then sweep. No live handle ever references a
   * file under `tmp/`, so every file there is by definition the residue of a transaction
   * that did not commit and is safe to remove.
   */
  recover(): SweepReport {
    let removed = 0;
    const tmp = join(this.root, TMP_DIR);
    for (const name of safeReaddir(tmp)) {
      const child = join(tmp, name);
      const info = safeLstat(child);
      if (info?.isFile() || info?.isSymbolicLink()) {
        unlinkQuiet(child);
        removed += 1;
      }
    }
    try {
      const db = this.connect();
      this.writeTxn(db, () => db.exec("DELETE FROM orphan_temps"));
    } catch {
      // The next sweep tries again.
    }
    const report = this.sweep();
    return { ...report, removedTemps: removed };
  }

  /** Expire handles past their TTL, then collect unreferenced content. */
  sweep(): SweepReport {
    const db = this.connect();
    let expired = 0;
    try {
      expired = this.writeTxn(db, () => {
        const now = this.nowLocked(db);
        this.bumpHighWater(db, now);
        const rows = db.prepare(
          "SELECT blob_hash, COUNT(*) AS n FROM handles "
            + " WHERE expires_at_ms <= ? OR revoked = 1 GROUP BY blob_hash",
        ).all(now) as Row[];
        const count = rows.reduce((total, row) => total + Number(row["n"]), 0);
        const decrement = db.prepare(
          "UPDATE blobs SET refcount = MAX(0, refcount - ?) WHERE hash = ?",
        );
        for (const row of rows) decrement.run(Number(row["n"]), String(row["blob_hash"]));
        db.prepare("DELETE FROM handles WHERE expires_at_ms <= ? OR revoked = 1").run(now);
        db.exec("UPDATE blobs SET pending_delete = 1 WHERE refcount = 0 AND pending_delete = 0");
        return count;
      });
    } catch (err) {
      if (err instanceof ShuntError) throw err;
      throw new ShuntError("STORE_FAILED", "SWEEP_FAILED", false);
    }
    const deleted = this.collectPendingBlobs();
    const orphans = this.collectOrphanBlobFiles();
    return { expiredHandles: expired, deletedBlobs: deleted, removedTemps: 0, orphanBlobFiles: orphans };
  }

  /**
   * Mark-then-sweep deletion: unlink outside the transaction, then re-verify and delete.
   * The second transaction re-checks `refcount = 0 AND pending_delete = 1`, so a publisher
   * that took a reference while the file was being unlinked keeps its row - and its
   * `stageBlob` rewrites the content, which is safe precisely because the content is
   * addressed by its own hash.
   */
  private collectPendingBlobs(): number {
    const db = this.connect();
    const candidates = (db.prepare(
      "SELECT hash FROM blobs WHERE pending_delete = 1 AND refcount = 0",
    ).all() as Row[]).map((row) => String(row["hash"]));
    let deleted = 0;
    for (const hash of candidates) {
      unlinkQuiet(this.blobPath(hash));
      try {
        deleted += this.writeTxn(db, () => {
          const result = db.prepare(
            "DELETE FROM blobs WHERE hash = ? AND refcount = 0 AND pending_delete = 1",
          ).run(hash);
          return Number(result.changes ?? 0);
        });
      } catch {
        // A concurrent publisher took a reference; the row stays and so does the content.
      }
    }
    return deleted;
  }

  /** Remove content files with no row - the residue of a crash before commit. */
  private collectOrphanBlobFiles(): number {
    const known = new Set(
      (this.connect().prepare("SELECT hash FROM blobs").all() as Row[])
        .map((row) => String(row["hash"])),
    );
    let removed = 0;
    for (const path of this.blobFiles()) {
      const name = path.slice(path.lastIndexOf("/") + 1);
      const hash = name.slice(0, name.length - BLOB_SUFFIX.length);
      if (!known.has(hash)) {
        unlinkQuiet(path);
        removed += 1;
      }
    }
    return removed;
  }

  private blobFiles(): string[] {
    const out: string[] = [];
    const root = join(this.root, BLOB_DIR);
    for (const first of safeReaddir(root)) {
      const firstPath = join(root, first);
      for (const second of safeReaddir(firstPath)) {
        const secondPath = join(firstPath, second);
        for (const name of safeReaddir(secondPath)) {
          if (!name.endsWith(BLOB_SUFFIX) || name.length !== 64 + BLOB_SUFFIX.length) continue;
          const candidate = join(secondPath, name);
          if (safeLstat(candidate)?.isFile()) out.push(candidate);
        }
      }
    }
    return out;
  }

  stats(): StoreStats {
    const row = this.connect().prepare(
      "SELECT (SELECT COUNT(*) FROM handles WHERE revoked = 0) AS handles,"
        + "       (SELECT COUNT(*) FROM blobs) AS blobs,"
        + "       (SELECT COALESCE(SUM(bytes), 0) FROM blobs) AS bytes",
    ).get() as Row;
    return {
      handles: Number(row["handles"]),
      blobs: Number(row["blobs"]),
      bytes: Number(row["bytes"]),
    };
  }

  // -- legacy artifacts ------------------------------------------------------

  /** Count pre-1.1 `*.spill` files. They are never imported as handles. */
  legacyArtifactCount(): number {
    return this.legacyArtifacts().length;
  }

  /** Remove pre-1.1 spill artifacts. Deletion, not secure erasure. */
  purgeLegacyArtifacts(): number {
    const found = this.legacyArtifacts();
    for (const path of found) unlinkQuiet(path);
    return found.length;
  }

  private legacyArtifacts(): string[] {
    const out: string[] = [];
    for (const name of safeReaddir(this.root)) {
      if (name === BLOB_DIR || name === TMP_DIR) continue;
      const dir = join(this.root, name);
      if (!safeLstat(dir)?.isDirectory()) continue;
      for (const child of safeReaddir(dir)) {
        if (!child.endsWith(LEGACY_SUFFIX)) continue;
        const candidate = join(dir, child);
        if (safeLstat(candidate)?.isFile()) out.push(candidate);
      }
    }
    return out;
  }

  // -- paths -----------------------------------------------------------------

  /** Location derived from the hash. Never stored, never exposed to a caller. */
  private blobPath(hash: string): string {
    if (hash.length !== 64 || !/^[0-9a-f]{64}$/.test(hash)) {
      throw new ShuntError("STORE_FAILED", "BAD_BLOB_HASH", false);
    }
    return join(this.root, BLOB_DIR, hash.slice(0, 2), hash.slice(2, 4), `${hash}${BLOB_SUFFIX}`);
  }
}

// -- helpers ----------------------------------------------------------------

function optionalNumber(value: unknown): number | undefined {
  return value === null || value === undefined ? undefined : Number(value);
}

function recordFromRow(row: Row): OperationRecord {
  return {
    operationId: String(row["operation_id"]),
    kind: String(row["kind"]),
    status: String(row["status"]),
    code: String(row["code"]),
    rawInputBytes: Number(row["raw_input_bytes"]),
    rawInputBaselineTokens: optionalNumber(row["raw_input_baseline_tokens"]),
    baselineKind: String(row["baseline_kind"]),
    baselineMethod: String(row["baseline_method"]),
    baselineCreditTokens: Number(row["baseline_credit_tokens"]),
    mainModelEnvelopeBytes: Number(row["main_model_envelope_bytes"]),
    mainModelEnvelopeTokens: Number(row["main_model_envelope_tokens"]),
    envelopeTokenMethod: String(row["envelope_token_method"]),
    readerInputTokens: optionalNumber(row["reader_input_tokens"]),
    readerOutputTokens: optionalNumber(row["reader_output_tokens"]),
    readerCacheTokens: optionalNumber(row["reader_cache_tokens"]),
    readerTokenMethod: String(row["reader_token_method"]),
    attemptsStarted: Number(row["attempts_started"]),
    attemptsUsageComplete: Number(row["attempts_usage_complete"]),
    deliveryBoundary: String(row["delivery_boundary"]),
    mainContextTokensSaved: Number(row["main_context_tokens_saved"]),
    netTokensSaved: Number(row["net_tokens_saved"]),
  };
}

function safeReaddir(path: string): string[] {
  try {
    return readdirSync(path);
  } catch {
    return [];
  }
}

function safeLstat(path: string) {
  try {
    return lstatSync(path);
  } catch {
    return undefined;
  }
}

/**
 * SHA-256 of an existing regular file, or `undefined` when it is absent. A symlink, FIFO,
 * directory or device where a blob should be is a path-replacement attempt: it fails closed
 * rather than being followed.
 */
function hashFile(path: string): string | undefined {
  const info = safeLstat(path);
  if (info === undefined) return undefined;
  if (!info.isFile() || info.nlink > 1) {
    throw new ShuntError("STORE_FAILED", "UNSAFE_BLOB_PATH", false);
  }
  const hash = createHash("sha256");
  let fd: number;
  try {
    fd = openSync(path, fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0));
  } catch {
    throw new ShuntError("STORE_FAILED", "UNSAFE_BLOB_PATH", false);
  }
  try {
    const buffer = Buffer.allocUnsafe(READ_CHUNK);
    for (;;) {
      const read = readSync(fd, buffer, 0, buffer.length, null);
      if (read === 0) break;
      hash.update(buffer.subarray(0, read));
    }
  } finally {
    closeSync(fd);
  }
  return hash.digest("hex");
}

/** Read a blob through a validated descriptor, refusing past the expected size. */
function readPrivate(path: string, expectedBytes: number): Uint8Array {
  let fd: number;
  try {
    fd = openSync(path, fsConstants.O_RDONLY | (fsConstants.O_NOFOLLOW ?? 0));
  } catch (err) {
    const code = (err as { code?: string }).code;
    if (code === "ENOENT") throw new ShuntError("STORE_FAILED", "BLOB_MISSING", false);
    throw new ShuntError("STORE_FAILED", "BLOB_READ_FAILED", false);
  }
  try {
    const info = statSync(path);
    if (!info.isFile() || info.nlink > 1) {
      throw new ShuntError("STORE_FAILED", "UNSAFE_BLOB_PATH", false);
    }
    const out = Buffer.allocUnsafe(expectedBytes);
    let total = 0;
    for (;;) {
      if (total > expectedBytes) {
        throw new ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", false);
      }
      const read = readSync(fd, out, total, Math.max(0, expectedBytes - total), null);
      if (read === 0) break;
      total += read;
      if (total >= expectedBytes) {
        // One more read must return 0; anything else means the file grew.
        const probe = Buffer.allocUnsafe(1);
        if (readSync(fd, probe, 0, 1, null) !== 0) {
          throw new ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", false);
        }
        break;
      }
    }
    if (total !== expectedBytes) {
      throw new ShuntError("STORE_FAILED", "BLOB_CONTENT_MISMATCH", false);
    }
    return Uint8Array.from(out);
  } finally {
    closeSync(fd);
  }
}

function unlinkQuiet(path: string): void {
  try {
    unlinkSync(path);
  } catch {
    // Already gone, or never there.
  }
}

/** Make a rename durable. Not every platform supports it; absence is not a failure. */
function fsyncDir(path: string): void {
  let fd: number;
  try {
    fd = openSync(path, fsConstants.O_RDONLY | (fsConstants.O_DIRECTORY ?? 0));
  } catch {
    return;
  }
  try {
    fsyncSync(fd);
  } catch {
    // Directory fsync is advisory on some platforms.
  } finally {
    closeSync(fd);
  }
}

function assertPrivateDirectory(path: string): void {
  const info = safeLstat(path);
  if (info === undefined || !info.isDirectory() || info.isSymbolicLink()) {
    throw new ShuntError("STORE_FAILED", "UNSAFE_CACHE_PATH", false);
  }
  try {
    chmodSync(path, DIR_MODE);
  } catch {
    throw new ShuntError("STORE_FAILED", "PERMISSION_FAILED", false);
  }
}

export type { StatementSync };
