/** unit permissions (TypeScript core) - what may become a source. */
import { linkSync, mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { ShuntError } from "../src/errors.js";
import { authorize, pathPolicy } from "../src/paths.js";
import { DEFAULT_LIMITS } from "../src/limits.js";
import { SourceRegistry } from "../src/registry.js";
import { assertNoSecret, assertSupportedBlocks, assertText, looksBinary, snapshotBytes } from "../src/snapshot.js";
import { ScopeIdentity, SnapshotStore } from "../src/store.js";
import { makeIdentity, makeRegistry } from "./support.js";

const enc = (s: string) => new TextEncoder().encode(s);

function workspace() {
  const dir = mkdtempSync(join(tmpdir(), "shunt-perm-"));
  const root = join(dir, "ws");
  mkdirSync(root);
  writeFileSync(join(root, "ok.txt"), "alpha\n");
  const outside = join(dir, "outside");
  mkdirSync(outside);
  writeFileSync(join(outside, "secret.txt"), "classified\n");
  return { dir, root, outside };
}

const policy = (root: string, denylist: string[] = []) => pathPolicy([root], denylist);

describe("path authorization", () => {
  it("accepts a regular file inside a root", () => {
    const { root } = workspace();
    expect(authorize(join(root, "ok.txt"), policy(root)).real).toContain("ok.txt");
  });

  it("rejects a relative path", () => {
    const { root } = workspace();
    expect(() => authorize("ok.txt", policy(root))).toThrowError(ShuntError);
  });

  it("rejects traversal outside the root", () => {
    const { root, outside } = workspace();
    expect(() => authorize(join(root, "..", "outside", "secret.txt"), policy(root))).toThrowError(
      ShuntError,
    );
    expect(() => authorize(join(outside, "secret.txt"), policy(root))).toThrowError(ShuntError);
  });

  it("rejects a symlink even when its target is inside", () => {
    const { root } = workspace();
    const link = join(root, "link.txt");
    symlinkSync(join(root, "ok.txt"), link);
    try {
      authorize(link, policy(root));
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).detail).toBe("SYMLINK");
    }
  });

  it("rejects a hardlink", () => {
    const { root } = workspace();
    const target = join(root, "hard.txt");
    linkSync(join(root, "ok.txt"), target);
    try {
      authorize(target, policy(root));
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).detail).toBe("HARDLINKED");
    }
  });

  it("rejects a directory and a fifo", () => {
    const { root } = workspace();
    mkdirSync(join(root, "sub"));
    try {
      authorize(join(root, "sub"), policy(root));
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).detail).toBe("NOT_REGULAR_FILE");
    }
    const fifo = join(root, "pipe");
    execFileSync("mkfifo", [fifo]);
    expect(() => authorize(fifo, policy(root))).toThrowError(ShuntError);
  });

  for (const name of [".env", ".env.production", "id_rsa", "server.pem", "auth.json", "credentials"]) {
    it(`denies the secret path ${name}`, () => {
      const { root } = workspace();
      const path = join(root, name);
      writeFileSync(path, "value\n");
      try {
        authorize(path, policy(root));
        throw new Error("expected rejection");
      } catch (err) {
        const shunt = err as ShuntError;
        expect(shunt.detail).toBe("SECRET_PATH");
        expect(shunt.safeMessage()).not.toContain(name);
      }
    });
  }

  it("applies the administrator denylist", () => {
    const { root } = workspace();
    mkdirSync(join(root, "vault"));
    writeFileSync(join(root, "vault", "notes.txt"), "x\n");
    expect(() => authorize(join(root, "vault", "notes.txt"), policy(root, ["vault/*"]))).toThrowError(
      ShuntError,
    );
  });

  it("exposes no path or value in a rejection", () => {
    const { root, outside } = workspace();
    try {
      authorize(join(outside, "secret.txt"), policy(root));
      throw new Error("expected rejection");
    } catch (err) {
      const blob = JSON.stringify({ m: (err as ShuntError).safeMessage(), d: (err as ShuntError).detail });
      expect(blob).not.toContain("secret.txt");
      expect(blob).not.toContain(outside);
    }
  });
});

describe("content policy", () => {
  it("rejects secret content without echoing the value", () => {
    const payload = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n";
    try {
      assertNoSecret(payload, "SOURCE");
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).safeMessage()).not.toContain("MIIabc");
    }
    expect(() => snapshotBytes(enc(payload))).toThrowError(ShuntError);
  });

  it("rejects a secret in the question", () => {
    expect(() => assertNoSecret("my key is ghp_abcdefghijklmnop", "QUESTION")).toThrowError(ShuntError);
  });

  for (const payload of ["\x7fELF\x02\x01", "%PDF-1.7\n", "abc\x00def"]) {
    it(`detects binary content by sniffing ${JSON.stringify(payload.slice(0, 6))}`, () => {
      expect(looksBinary(enc(payload))).toBe(true);
      try {
        assertText(enc(payload));
        throw new Error("expected rejection");
      } catch (err) {
        expect((err as ShuntError).code).toBe("BINARY_UNSUPPORTED");
      }
    });
  }

  it("rejects invalid encoding", () => {
    expect(() => assertText(new Uint8Array([0x76, 0x61, 0xff, 0xfe]))).toThrowError(ShuntError);
  });

  it("rejects a whole result containing one unsupported block", () => {
    expect(() =>
      assertSupportedBlocks([{ type: "text", text: "ok" }, { type: "image", data: "x" }]),
    ).toThrowError(ShuntError);
  });
});

describe("handle isolation", () => {
  function scopedStore(sessionId: string, clock?: { now: number }) {
    const dir = mkdtempSync(join(tmpdir(), "shunt-scope-"));
    const store = clock
      ? new SnapshotStore(join(dir, "cache"), DEFAULT_LIMITS, () => clock.now)
      : new SnapshotStore(join(dir, "cache"));
    const identity = makeIdentity(sessionId);
    store.openScope(identity);
    return { store, identity, registry: new SourceRegistry(store, identity) };
  }

  it("does not resolve a handle across sessions", () => {
    const mine = scopedStore("sess_a");
    const entry = mine.registry.register("sess_a", snapshotBytes(enc("alpha\n")));
    expect(mine.registry.resolve("sess_a", entry.sourceId).sourceId).toBe(entry.sourceId);

    // A second scope over the same store: same file, different trusted identity.
    const theirs = new ScopeIdentity({
      host: "test-host", profile: "test", principal: "local", session: "sess_b",
    });
    mine.store.openScope(theirs);
    const other = new SourceRegistry(mine.store, theirs);
    try {
      other.resolve("sess_b", entry.sourceId);
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).code).toBe("SOURCE_EXPIRED");
      // A foreign scope learns nothing beyond "unknown".
      expect((err as ShuntError).detail).toBe("UNKNOWN_HANDLE");
    }
  });

  it("does not resolve a handle across session generations", () => {
    const first = scopedStore("sess");
    const entry = first.registry.register("sess", snapshotBytes(enc("alpha\n")));
    const next = first.identity.withGeneration(2);
    first.store.openScope(next);
    try {
      new SourceRegistry(first.store, next).resolve("sess", entry.sourceId);
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).code).toBe("SOURCE_EXPIRED");
    }
  });

  it("refuses an expired handle instead of re-fetching", () => {
    const clock = { now: 1_700_000_000_000 };
    const scoped = scopedStore("sess", clock);
    const entry = scoped.registry.register("sess", snapshotBytes(enc("alpha\n")));
    clock.now += (DEFAULT_LIMITS.storeHandleTtlSeconds + 1) * 1000;
    try {
      scoped.registry.resolve("sess", entry.sourceId);
      throw new Error("expected rejection");
    } catch (err) {
      expect((err as ShuntError).detail).toBe("TTL_ELAPSED");
    }
  });

  it("does not let a clock rollback revive an expired handle", () => {
    const clock = { now: 1_700_000_000_000 };
    const scoped = scopedStore("sess", clock);
    const entry = scoped.registry.register("sess", snapshotBytes(enc("alpha\n")));
    const ttlMs = (DEFAULT_LIMITS.storeHandleTtlSeconds + 1) * 1000;
    clock.now += ttlMs;
    expect(() => scoped.registry.resolve("sess", entry.sourceId)).toThrowError(ShuntError);
    // Winding the wall clock back must not make the handle readable again.
    clock.now -= ttlMs;
    expect(() => scoped.registry.resolve("sess", entry.sourceId)).toThrowError(ShuntError);
  });

  it("drops every handle when the session ends", () => {
    const registry = makeRegistry(mkdtempSync(join(tmpdir(), "shunt-end-")), { sessionId: "sess" });
    registry.register("sess", snapshotBytes(enc("a\n")));
    registry.register("sess", snapshotBytes(enc("b\n")));
    expect(registry.expireSession("sess")).toBe(2);
    expect(registry.count("sess")).toBe(0);
  });
});
