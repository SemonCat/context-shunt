/**
 * unit store (TypeScript core) - the same properties the Python gate asserts.
 *
 * Everything here is adversarial against the hybrid store. The store is the only thing
 * standing between an expired or forged handle and a private payload, so each property is
 * asserted directly rather than through the reader. The last block is the one that only
 * exists because there are two implementations: a store written by the TypeScript core and
 * read by the Python core, and back again.
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { ShuntError } from "../src/errors.js";
import { DEFAULT_LIMITS as L, narrowLimits, storeDdl, storeDdlPath } from "../src/limits.js";
import {
  type Capture,
  type OperationRecord,
  ScopeIdentity,
  SnapshotStore,
  captureHash,
  snapshotIdOf,
} from "../src/store.js";

const here = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(here, "..", "..", "..");
const enc = (s: string) => new TextEncoder().encode(s);
const BODY = enc("alpha\nbeta\ngamma\n");

// The store loads node:sqlite through createRequire for bundler independence; these
// tests reach the same database the same way.
const { DatabaseSync } = createRequire(import.meta.url)("node:sqlite") as {
  DatabaseSync: new (path: string) => {
    prepare(sql: string): { run(...a: unknown[]): unknown; get(...a: unknown[]): unknown };
    close(): void;
  };
};

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "shunt-store-"));
}

function identity(session = "sess", generation = 1): ScopeIdentity {
  return new ScopeIdentity({
    host: "test-host", profile: "test", principal: "local", session, generation,
  });
}

function capture(data: Uint8Array = BODY, extra: Partial<Capture> = {}): Capture {
  return {
    data,
    mediaType: "text/plain",
    lineCount: data.filter((byte) => byte === 0x0a).length,
    ...extra,
  };
}

function store(dir = tmp(), limits = L, clock?: { now: number }): SnapshotStore {
  return clock
    ? new SnapshotStore(join(dir, "cache"), limits, () => clock.now)
    : new SnapshotStore(join(dir, "cache"), limits);
}

function blobFiles(root: string): string[] {
  const out: string[] = [];
  const blobs = join(root, "blobs");
  for (const a of readdirSync(blobs)) {
    for (const b of readdirSync(join(blobs, a))) {
      for (const name of readdirSync(join(blobs, a, b))) out.push(join(blobs, a, b, name));
    }
  }
  return out;
}

// -- DDL is normative -------------------------------------------------------

describe("the DDL is the contract", () => {
  it("comes from the shared file, not from the code", () => {
    const ddl = storeDdl();
    expect(storeDdlPath().endsWith("v1.sql")).toBe(true);
    for (const table of ["store_metadata", "scopes", "blobs", "handles", "disclosure_events"]) {
      expect(ddl).toContain(`CREATE TABLE IF NOT EXISTS ${table}`);
    }
    // Neither core embeds its own CREATE TABLE.
    expect(readFileSync(join(here, "..", "src", "store.ts"), "utf8")).not.toContain("CREATE TABLE");
  });

  it("matches the vendored copy the Python core reads", () => {
    expect(storeDdl()).toBe(readFileSync(join(REPO, "contracts", "store", "v1.sql"), "utf8"));
  });

  it("records the revision it was created with", () => {
    const s = store();
    s.openScope(identity());
    expect(s.ddlVersion()).toBe(L.storeDdlVersion);
  });
});

// -- scoping and replay -----------------------------------------------------

describe("handle scope and replay", () => {
  const variants: Array<[string, Partial<{ host: string; profile: string; principal: string; session: string; generation: number }>]> = [
    ["host", { host: "other-host" }],
    ["profile", { profile: "other-profile" }],
    ["principal", { principal: "someone-else" }],
    ["session", { session: "other-session" }],
    ["generation", { generation: 2 }],
  ];

  for (const [field, override] of variants) {
    it(`does not replay a handle into a different ${field}`, () => {
      const s = store();
      const mine = identity();
      const handle = s.publish(mine, [capture()])[0]!;
      const theirs = new ScopeIdentity({
        host: mine.host,
        profile: mine.profile,
        principal: mine.principal,
        session: mine.session,
        generation: mine.generation,
        ...override,
      });
      s.openScope(theirs);
      try {
        s.resolve(theirs, handle.handleId);
        throw new Error("expected rejection");
      } catch (err) {
        expect((err as ShuntError).code).toBe("SOURCE_EXPIRED");
        // A foreign scope learns nothing beyond "unknown".
        expect((err as ShuntError).detail).toBe("UNKNOWN_HANDLE");
      }
    });
  }

  it("makes expiry a predicate, not a file deletion", () => {
    const clock = { now: 1_700_000_000_000 };
    const s = store(tmp(), L, clock);
    const scope = identity();
    const handle = s.publish(scope, [capture()])[0]!;
    clock.now += (L.storeHandleTtlSeconds + 1) * 1000;
    expect(() => s.resolve(scope, handle.handleId)).toThrowError(ShuntError);
    // The physical file is still there; readability was decided in SQL.
    expect(blobFiles(s.root).length).toBe(1);
    expect(s.sweep().expiredHandles).toBe(1);
    expect(blobFiles(s.root).length).toBe(0);
  });

  it("makes a closed scope unreadable before the sweep", () => {
    const s = store();
    const scope = identity();
    const handle = s.publish(scope, [capture()])[0]!;
    s.closeScope(scope, false);
    try {
      s.resolve(scope, handle.handleId);
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).detail).toBe("SCOPE_CLOSED");
    }
  });

  it("resolves a forged handle id to nothing", () => {
    const s = store();
    const scope = identity();
    s.publish(scope, [capture()]);
    expect(() => s.resolve(scope, "src_" + "f".repeat(16))).toThrowError(ShuntError);
  });
});

// -- atomic publication -----------------------------------------------------

describe("publication is all-or-none", () => {
  it("publishes a whole multi-source batch or nothing", () => {
    const s = store();
    const scope = identity();
    expect(s.publish(scope, [capture(enc("one\n")), capture(enc("two\n")), capture(enc("three\n"))]).length)
      .toBe(3);
    expect(s.stats().handles).toBe(3);

    const oversized = capture(new Uint8Array(L.maxSourceBytes + 1));
    expect(() => s.publish(scope, [capture(enc("four\n")), oversized])).toThrowError(ShuntError);
    // Neither handle from the refused batch exists.
    expect(s.stats().handles).toBe(3);
  });

  it("leaves no usable handle when the publishing transaction fails", () => {
    const s = store();
    const scope = identity();
    // Simulated crash after the files are in place but before the commit.
    const original = (s as unknown as { writeTxn: unknown }).writeTxn;
    let published = false;
    (s as unknown as { writeTxn: (db: unknown, body: () => unknown) => unknown }).writeTxn = function (
      db: unknown,
      body: () => unknown,
    ) {
      if (published) throw new Error("simulated crash");
      const result = (original as (db: unknown, body: () => unknown) => unknown).call(this, db, body);
      return result;
    };
    published = true;
    expect(() => s.publish(scope, [capture(enc("never published\n"))])).toThrowError(ShuntError);
    (s as unknown as { writeTxn: unknown }).writeTxn = original;

    expect(s.stats().handles).toBe(0);
    // The orphan content file has no row; recovery collects it.
    expect(s.recover().orphanBlobFiles).toBeGreaterThanOrEqual(0);
    expect(blobFiles(s.root).length).toBe(0);
  });

  it("clears staged temp files on recovery", () => {
    const s = store();
    s.openScope(identity());
    const stray = join(s.root, "tmp", "a".repeat(64) + ".deadbeef.part");
    writeFileSync(stray, "half-written payload");
    expect(s.recover().removedTemps).toBe(1);
    expect(existsSync(stray)).toBe(false);
  });
});

// -- dedupe, refcounts and corruption ---------------------------------------

describe("content addressing", () => {
  it("stores identical content once and refcounts it", () => {
    const s = store();
    const scope = identity();
    const first = s.publish(scope, [capture()])[0]!;
    const second = s.publish(scope, [capture()])[0]!;
    expect(first.blobHash).toBe(second.blobHash);
    expect(first.handleId).not.toBe(second.handleId);
    expect(s.stats().blobs).toBe(1);

    // Dropping one handle must not delete content the other still references.
    s.revoke(scope, first.handleId);
    expect(s.loadPayload(s.resolve(scope, second.handleId))).toEqual(BODY);
    s.revoke(scope, second.handleId);
    expect(blobFiles(s.root).length).toBe(0);
  });

  it("keeps content readable when a republish races a deletion", () => {
    const s = store();
    const scope = identity();
    const handle = s.publish(scope, [capture()])[0]!;
    s.revoke(scope, handle.handleId); // marks pending_delete and unlinks
    // A later capture of the same bytes rewrites the content-addressed file.
    const republished = s.publish(scope, [capture()])[0]!;
    expect(s.loadPayload(s.resolve(scope, republished.handleId))).toEqual(BODY);
  });

  it("fails closed on a content mismatch without deleting anything", () => {
    const s = store();
    const scope = identity();
    const handle = s.publish(scope, [capture()])[0]!;
    const blob = blobFiles(s.root)[0]!;
    writeFileSync(blob, "corrupted");
    expect(() => s.loadPayload(s.resolve(scope, handle.handleId))).toThrowError(ShuntError);
    // Corruption is reported, never "repaired" by deleting content other handles share.
    expect(existsSync(blob)).toBe(true);
  });

  it("refuses a symlink where a blob belongs", () => {
    const dir = tmp();
    const s = store(dir);
    const scope = identity();
    const payload = capture(enc("target content\n"));
    // Recompute the digest the same way the store does.
    const hash = createHash("sha256").update(payload.data).digest("hex");
    const blobDir = join(s.root, "blobs", hash.slice(0, 2), hash.slice(2, 4));
    mkdirSync(blobDir, { recursive: true });
    const outside = join(dir, "outside.txt");
    writeFileSync(outside, "attacker controlled\n");
    symlinkSync(outside, join(blobDir, `${hash}.bin`));
    try {
      s.publish(scope, [payload]);
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).code).toBe("STORE_FAILED");
      expect((err as ShuntError).detail).toBe("UNSAFE_BLOB_PATH");
    }
    expect(s.stats().handles).toBe(0);
  });

  it("keeps directories and files private", () => {
    const s = store();
    s.publish(identity(), [capture()]);
    expect(statSync(s.root).mode & 0o777).toBe(0o700);
    expect(statSync(join(s.root, "blobs")).mode & 0o777).toBe(0o700);
    for (const blob of blobFiles(s.root)) {
      expect(statSync(blob).mode & 0o777).toBe(0o600);
      expect(statSync(dirname(blob)).mode & 0o777).toBe(0o700);
    }
  });
});

// -- quotas and disclosure --------------------------------------------------

describe("quotas and the disclosure ceiling", () => {
  it("refuses rather than evicting a live handle at the entry quota", () => {
    const s = store(tmp(), narrowLimits(L, { storeMaxEntries: 2 }));
    const scope = identity();
    s.publish(scope, [capture(enc("one\n")), capture(enc("two\n"))]);
    try {
      s.publish(scope, [capture(enc("three\n"))]);
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).detail).toBe("STORE_ENTRY_QUOTA");
    }
    expect(s.stats().handles).toBe(2);
  });

  it("charges disclosure before bytes are returned", () => {
    const s = store(tmp(), narrowLimits(L, { disclosureMaxPerSourceBytes: 100 }));
    const scope = identity();
    const handle = s.publish(scope, [capture()])[0]!;
    const first = s.chargeDisclosure(scope, handle.handleId, "lines", 60);
    expect(first.granted).toBe(true);
    expect(first.disclosedBytesSource).toBe(60);
    // 60 + 60 exceeds the ceiling, so nothing is charged and nothing may be disclosed.
    const second = s.chargeDisclosure(scope, handle.handleId, "lines", 60);
    expect(second.granted).toBe(false);
    expect(second.chargedBytes).toBe(0);
    expect(s.disclosureAllowance(scope, handle.handleId).perSourceRemaining).toBe(40);
  });

  it("binds the session ceiling across separate handles", () => {
    const s = store(tmp(), narrowLimits(L, { disclosureMaxPerSessionBytes: 100 }));
    const scope = identity();
    const [a, b] = s.publish(scope, [capture(enc("one\n")), capture(enc("two\n"))]);
    expect(s.chargeDisclosure(scope, a!.handleId, "lines", 80).granted).toBe(true);
    // Paging a *different* handle cannot escape the session budget.
    expect(s.chargeDisclosure(scope, b!.handleId, "lines", 80).granted).toBe(false);
  });

  it("credits the baseline exactly once", () => {
    const s = store();
    const scope = identity();
    const handle = s.publish(scope, [capture()])[0]!;
    expect(s.creditBaseline(scope, handle.handleId)).toBe(true);
    for (let i = 0; i < 5; i += 1) {
      expect(s.creditBaseline(scope, handle.handleId)).toBe(false);
    }
  });
});

// -- accounting persistence -------------------------------------------------

describe("accounting persistence", () => {
  function record(overrides: Partial<OperationRecord> = {}): OperationRecord {
    return {
      operationId: "acc_" + "1".repeat(16),
      kind: "read",
      status: "ok",
      code: "ANSWERED",
      rawInputBytes: 2048,
      rawInputBaselineTokens: 512,
      baselineKind: "full_payload_counterfactual",
      baselineMethod: "bytes_div_4",
      baselineCreditTokens: 512,
      mainModelEnvelopeBytes: 400,
      mainModelEnvelopeTokens: 100,
      envelopeTokenMethod: "bytes_div_4",
      readerInputTokens: undefined,
      readerOutputTokens: undefined,
      readerCacheTokens: undefined,
      readerTokenMethod: "unknown",
      attemptsStarted: 1,
      attemptsUsageComplete: 0,
      deliveryBoundary: "envelope",
      mainContextTokensSaved: 412,
      netTokensSaved: 412,
      ...overrides,
    };
  }

  it("round-trips a record with its nulls intact", () => {
    const s = store();
    const scope = identity();
    s.recordOperation(scope, record());
    const [readBack] = s.operationPage(scope, { page: 1, pageSize: 8 });
    // "Not reported" survives the round trip as undefined, never as zero.
    expect(readBack!.readerInputTokens).toBeUndefined();
    expect(readBack!.readerOutputTokens).toBeUndefined();
    expect(readBack!.mainContextTokensSaved).toBe(412);
    const totals = s.operationTotals(scope);
    expect(totals.readerInputTokens).toBeUndefined();
    expect(totals.baselineCreditTokens).toBe(512);
  });

  it("never reaches another scope", () => {
    const s = store();
    const mine = identity("mine");
    const theirs = identity("theirs");
    s.recordOperation(mine, record({ operationId: "acc_" + "a".repeat(16) }));
    s.recordOperation(theirs, record({ operationId: "acc_" + "b".repeat(16) }));
    expect(s.operationCount(mine)).toBe(1);
    expect(s.operationPage(mine, { page: 1, pageSize: 8 }).map((r) => r.operationId))
      .toEqual(["acc_" + "a".repeat(16)]);
  });

  it("pages an append-only log without skew", () => {
    const s = store();
    const scope = identity();
    for (let i = 0; i < 12; i += 1) {
      s.recordOperation(scope, record({ operationId: `acc_${String(i).padStart(16, "0")}` }));
    }
    const first = s.operationPage(scope, { page: 1, pageSize: 8 });
    // A record written between two page reads lands at the end, never shifting page 1.
    s.recordOperation(scope, record({ operationId: "acc_" + "f".repeat(16) }));
    const second = s.operationPage(scope, { page: 2, pageSize: 8 });
    const firstIds = new Set(first.map((r) => r.operationId));
    expect(second.every((r) => !firstIds.has(r.operationId))).toBe(true);
  });
});

// -- cross-language interoperability ----------------------------------------

describe("cross-language interoperability", () => {
  const python = join(REPO, ".venv", "bin", "python");

  it("reads a store the Python core wrote, and is read back by it", () => {
    if (!existsSync(python)) {
      // Both cores are required for this one; without the Python venv it is not a pass.
      throw new Error("python venv missing: cannot prove cross-language interoperability");
    }
    const dir = tmp();
    const root = join(dir, "cache");
    const program = [
      "import sys, json",
      `sys.path.insert(0, ${JSON.stringify(join(REPO, "packages", "core-py", "src"))})`,
      "from context_shunt.store import SnapshotStore, ScopeIdentity, Capture",
      `s = SnapshotStore(${JSON.stringify(root)})`,
      "i = ScopeIdentity(host='test-host', profile='test', principal='local', session='shared')",
      "h = s.publish(i, [Capture(data=b'written by python\\n', media_type='text/plain', line_count=1)])[0]",
      "print(json.dumps({'handle': h.handle_id, 'snapshot': h.snapshot_id}))",
    ].join(";");
    const written = JSON.parse(
      execFileSync(python, ["-c", program], { encoding: "utf8" }).trim(),
    ) as { handle: string; snapshot: string };

    // The TypeScript core opens the same file and authorizes the same handle.
    const s = new SnapshotStore(root, L);
    const scope = identity("shared");
    const handle = s.resolve(scope, written.handle);
    expect(snapshotIdOf(handle)).toBe(written.snapshot);
    expect(new TextDecoder().decode(s.loadPayload(handle))).toBe("written by python\n");

    // ...and a handle this core publishes resolves back in the Python core.
    const mine = s.publish(scope, [capture(enc("written by typescript\n"))])[0]!;
    s.close();
    const readBack = [
      "import sys, json",
      `sys.path.insert(0, ${JSON.stringify(join(REPO, "packages", "core-py", "src"))})`,
      "from context_shunt.store import SnapshotStore, ScopeIdentity",
      `s = SnapshotStore(${JSON.stringify(root)})`,
      "i = ScopeIdentity(host='test-host', profile='test', principal='local', session='shared')",
      `h = s.resolve(i, ${JSON.stringify(mine.handleId)})`,
      "print(json.dumps({'snapshot': h.snapshot_id, 'text': s.load_payload(h).decode()}))",
    ].join(";");
    const echoed = JSON.parse(
      execFileSync(python, ["-c", readBack], { encoding: "utf8" }).trim(),
    ) as { snapshot: string; text: string };
    expect(echoed.snapshot).toBe(snapshotIdOf(mine));
    expect(echoed.text).toBe("written by typescript\n");
  });
});

// -- the deletion race, at the interleaving that actually loses data ----------

describe("store integrity under concurrent publish and sweep", () => {
  /**
   * `stageBlob` skips writing when the content file is present, and the sweeper used to
   * unlink outside its transaction. So a publisher could dedupe onto a file the sweeper
   * then removed, and commit a handle whose payload does not exist.
   */
  it("never lands a deduped publish on a swept file", () => {
    const dir = tmp();
    const s = store(dir);
    const scope = identity();
    const first = s.publish(scope, [capture()])[0]!;

    const db = new DatabaseSync(join(dir, "cache", "store.sqlite3"));
    db.prepare("UPDATE blobs SET refcount = 0, pending_delete = 1 WHERE hash = ?")
      .run(first.blobHash);
    db.close();

    // Force the interleaving: the sweeper runs between the dedupe decision and the commit.
    const inner = s as unknown as {
      stageBlob(c: Capture, h: string): string | undefined;
      collectPendingBlobs(): number;
    };
    const realStage = inner.stageBlob.bind(inner);
    inner.stageBlob = (c: Capture, h: string) => {
      const staged = realStage(c, h);
      inner.collectPendingBlobs();
      return staged;
    };

    const republished = s.publish(scope, [capture()])[0]!;
    expect(s.loadPayload(s.resolve(scope, republished.handleId))).toEqual(BODY);
  });

  it("leaves no orphan file or row when a publish is refused", () => {
    const dir = tmp();
    const s = store(dir, narrowLimits(L, { storeMaxBytes: BODY.length * 2 }));
    const scope = identity();
    s.publish(scope, [capture()]);
    const before = blobFiles(join(dir, "cache")).sort();

    expect(() => s.publish(scope, [capture(enc("x".repeat(4096)))])).toThrow(ShuntError);

    expect(blobFiles(join(dir, "cache")).sort()).toEqual(before);
    const db = new DatabaseSync(join(dir, "cache", "store.sqlite3"));
    const temps = db.prepare("SELECT COUNT(*) AS n FROM orphan_temps").get() as { n: number };
    db.close();
    expect(Number(temps.n)).toBe(0);
  });

  it("refuses identical bytes carrying different media metadata", () => {
    const dir = tmp();
    const s = store(dir);
    const scope = identity();
    const body = enc('{"a":1}\n');
    const first = s.publish(scope, [capture(body, { mediaType: "text/plain" })])[0]!;
    expect(first.mediaType).toBe("text/plain");

    expect(() => s.publish(scope, [capture(body, { mediaType: "application/json" })]))
      .toThrow(/BLOB_METADATA_CONFLICT/);
    // The original handle is untouched by the refusal.
    expect(s.loadPayload(s.resolve(scope, first.handleId))).toEqual(body);
  });
});

// -- disclosure and baseline follow the content, not the handle --------------

describe("recapture accounting", () => {
  it("does not reset the per-source disclosure ceiling", () => {
    const cap = 100;
    const s = store(tmp(), narrowLimits(L, { disclosureMaxPerSourceBytes: cap }));
    const scope = identity();
    const first = s.publish(scope, [capture()])[0]!;
    expect(s.chargeDisclosure(scope, first.handleId, "bytes", cap).granted).toBe(true);

    const second = s.publish(scope, [capture()])[0]!;
    expect(second.handleId).not.toBe(first.handleId);
    expect(second.blobHash).toBe(first.blobHash);

    expect(s.disclosureAllowance(scope, second.handleId).perSourceRemaining).toBe(0);
    const refused = s.chargeDisclosure(scope, second.handleId, "bytes", 1);
    expect(refused.granted).toBe(false);
    expect(refused.limitReached).toBe(true);
  });

  it("claims the baseline once per source, not once per handle", () => {
    const s = store();
    const scope = identity();
    const first = s.publish(scope, [capture()])[0]!;
    expect(s.creditBaseline(scope, first.handleId)).toBe(true);
    expect(s.creditBaseline(scope, first.handleId)).toBe(false);

    const second = s.publish(scope, [capture()])[0]!;
    expect(s.creditBaseline(scope, second.handleId)).toBe(false);

    const other = s.publish(scope, [capture(enc("different bytes\n"))])[0]!;
    expect(s.creditBaseline(scope, other.handleId)).toBe(true);
  });

  it("keeps disclosure and baseline separate per scope", () => {
    const cap = 100;
    const dir = tmp();
    const s = store(dir, narrowLimits(L, { disclosureMaxPerSourceBytes: cap }));
    const one = identity("a");
    const two = identity("b");
    const first = s.publish(one, [capture()])[0]!;
    s.chargeDisclosure(one, first.handleId, "bytes", cap);
    expect(s.creditBaseline(one, first.handleId)).toBe(true);

    const second = s.publish(two, [capture()])[0]!;
    expect(s.disclosureAllowance(two, second.handleId).perSourceRemaining).toBe(cap);
    expect(s.creditBaseline(two, second.handleId)).toBe(true);
  });
});

// -- accounting must survive revocation, and stay per source ----------------

describe("revocation-safe accounting", () => {
  it("does not reset the per-source ceiling when a handle is revoked", () => {
    const cap = 100;
    const s = store(tmp(), narrowLimits(L, { disclosureMaxPerSourceBytes: cap }));
    const scope = identity();
    const first = s.publish(scope, [capture()])[0]!;
    expect(s.chargeDisclosure(scope, first.handleId, "bytes", cap).granted).toBe(true);
    expect(s.creditBaseline(scope, first.handleId)).toBe(true);

    s.revoke(scope, first.handleId);
    const second = s.publish(scope, [capture()])[0]!;

    expect(s.disclosureAllowance(scope, second.handleId).perSourceRemaining).toBe(0);
    const refused = s.chargeDisclosure(scope, second.handleId, "bytes", 1);
    expect(refused.granted).toBe(false);
    expect(refused.limitReached).toBe(true);
    expect(s.creditBaseline(scope, second.handleId)).toBe(false);
  });

  it("clears the accounting when the scope closes", () => {
    const cap = 100;
    const dir = tmp();
    const s = store(dir, narrowLimits(L, { disclosureMaxPerSourceBytes: cap }));
    const scope = identity();
    const handle = s.publish(scope, [capture()])[0]!;
    s.chargeDisclosure(scope, handle.handleId, "bytes", cap);
    s.closeScope(scope, true);

    const fresh = identity("next");
    const reborn = s.publish(fresh, [capture()])[0]!;
    expect(s.disclosureAllowance(fresh, reborn.handleId).perSourceRemaining).toBe(cap);
  });

  it("refuses two media types for the same bytes inside one batch", () => {
    const s = store();
    const scope = identity();
    const body = enc('{"a":1}\n');
    expect(() =>
      s.publish(scope, [
        capture(body, { mediaType: "application/json" }),
        capture(body, { mediaType: "text/plain" }),
      ]),
    ).toThrow(/BLOB_METADATA_CONFLICT/);
    expect(s.stats().handles).toBe(0);
  });
});

// -- staging is a reservation other processes must honour -------------------

describe("cross-process staging reservations", () => {
  it("spares content another instance is staging", () => {
    const dir = tmp();
    const s = store(dir);
    const scope = identity();
    s.openScope(scope);
    const other = new SnapshotStore(join(dir, "cache"), L);
    other.openScope(scope);

    const c = capture();
    const inner = s as unknown as {
      stageBlob(c: Capture, h: string): string | undefined;
      blobPath(h: string): string;
    };
    const otherInner = other as unknown as { collectOrphanBlobFiles(): number };
    const hash = captureHash(c);
    expect(inner.stageBlob(c, hash)).toBeDefined();
    expect(existsSync(inner.blobPath(hash))).toBe(true);

    expect(otherInner.collectOrphanBlobFiles()).toBe(0);
    expect(existsSync(inner.blobPath(hash))).toBe(true);

    const handle = s.publish(scope, [c])[0]!;
    expect(s.loadPayload(s.resolve(scope, handle.handleId))).toEqual(c.data);
  });

  it("leaves a fresh reservation alone during recovery", () => {
    const dir = tmp();
    const s = store(dir);
    s.openScope(identity());
    const other = new SnapshotStore(join(dir, "cache"), L);

    const c = capture();
    const inner = s as unknown as {
      stageBlob(c: Capture, h: string): string | undefined;
      blobPath(h: string): string;
    };
    const hash = captureHash(c);
    expect(inner.stageBlob(c, hash)).toBeDefined();

    other.recover();
    expect(existsSync(inner.blobPath(hash))).toBe(true);
  });
});

// -- a publisher must not commit a handle whose payload is gone -------------

describe("cross-process publication safety", () => {
  it("revalidates its content before committing", () => {
    const dir = tmp();
    const s = store(dir);
    const scope = identity();
    s.openScope(scope);
    const other = new SnapshotStore(join(dir, "cache"), L);
    other.openScope(scope);

    const c = capture();
    const hash = captureHash(c);
    const inner = s as unknown as {
      stageBlob(c: Capture, h: string): string | undefined;
      blobPath(h: string): string;
    };
    const otherInner = other as unknown as { stageBlob(c: Capture, h: string): string | undefined };
    otherInner.stageBlob(c, hash);
    expect(existsSync(inner.blobPath(hash))).toBe(true);

    // Another process removes the shared file between staging and the commit.
    const realStage = inner.stageBlob.bind(inner);
    inner.stageBlob = (cap: Capture, h: string) => {
      const staged = realStage(cap, h);
      rmSync(inner.blobPath(h), { force: true });
      return staged;
    };

    const handle = s.publish(scope, [c])[0]!;
    expect(s.loadPayload(s.resolve(scope, handle.handleId))).toEqual(c.data);
  });

  it("gives a deduping publisher a lease of its own", () => {
    const dir = tmp();
    const s = store(dir);
    const scope = identity();
    s.openScope(scope);
    const other = new SnapshotStore(join(dir, "cache"), L);
    other.openScope(scope);

    const c = capture();
    const hash = captureHash(c);
    const inner = s as unknown as {
      stageBlob(c: Capture, h: string): string | undefined;
      blobPath(h: string): string;
      discardTempIds(ids: readonly string[]): void;
    };
    const otherInner = other as unknown as { stageBlob(c: Capture, h: string): string | undefined };

    const mine = inner.stageBlob(c, hash);
    const theirs = otherInner.stageBlob(c, hash);
    expect(mine).toBeDefined();
    expect(theirs).toBeDefined();

    inner.discardTempIds([mine as string]);
    expect(existsSync(inner.blobPath(hash))).toBe(true);
  });
});
