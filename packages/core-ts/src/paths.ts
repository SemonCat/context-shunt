/**
 * Source authorization: roots, canonicalization, file identity and secret policy.
 *
 * Nothing becomes a source unless it canonicalizes inside a configured workspace root, is
 * a regular file, and survives the secret policy. Rejections carry a code and a bounded
 * token - never the path, and never the matched value.
 */
import { lstatSync, openSync, readSync, closeSync, realpathSync, statSync } from "node:fs";
import { basename, extname, isAbsolute, resolve, sep } from "node:path";

import { ShuntError } from "./errors.js";

const SECRET_NAMES = new Set([
  ".env", ".env.local", ".env.production", ".env.development", ".netrc", "_netrc",
  "credentials", "auth.json", ".htpasswd", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
  ".pgpass", "shadow", "master.key", "secrets.yaml", "secrets.yml", ".npmrc", ".pypirc",
  ".dockercfg",
]);
const SECRET_SUFFIXES = ["_rsa", "_dsa", "_ed25519"];
const SECRET_EXTENSIONS = new Set([".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk"]);
const SECRET_DIR_PARTS = new Set([".ssh", ".gnupg", ".aws", ".kube", ".docker"]);

export interface PathPolicy {
  readonly roots: readonly string[];
  readonly denylist: readonly string[];
}

export function pathPolicy(roots: readonly string[], denylist: readonly string[] = []): PathPolicy {
  if (roots.length === 0) throw new ShuntError("UNSAFE_SOURCE", "NO_WORKSPACE_ROOT");
  // Roots are canonicalized the same way sources are, so a root reached through a
  // symlinked prefix (macOS /var -> /private/var, for one) still contains its own files.
  return { roots: roots.map(canonicalRoot), denylist };
}

function canonicalRoot(root: string): string {
  try {
    return realpathSync(resolve(root));
  } catch {
    return resolve(root);
  }
}

export interface AuthorizedPath {
  readonly real: string;
  readonly dev: number;
  readonly ino: number;
  readonly size: number;
  readonly mtimeNs: bigint;
}

function isSecretPath(real: string, policy: PathPolicy): boolean {
  const name = basename(real).toLowerCase();
  if (SECRET_NAMES.has(name)) return true;
  if (SECRET_EXTENSIONS.has(extname(real).toLowerCase())) return true;
  if (SECRET_SUFFIXES.some((sfx) => name.endsWith(sfx))) return true;
  if (name.startsWith(".env")) return true;
  return real.split(sep).some((part) => SECRET_DIR_PARTS.has(part));
}

function matchesDenylist(real: string, policy: PathPolicy): boolean {
  for (const root of policy.roots) {
    if (!real.startsWith(root + sep)) continue;
    const rel = real.slice(root.length + 1);
    for (const pattern of policy.denylist) {
      const rx = new RegExp(
        "^" + pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, "[^/]*") + "$",
      );
      if (rx.test(rel)) return true;
      const dir = rel.split("/")[0];
      if (dir !== undefined && rx.test(`${dir}/*`)) return true;
    }
  }
  return false;
}

/** Canonicalize and authorize one source path, or throw a bounded ShuntError. */
export function authorize(path: string, policy: PathPolicy): AuthorizedPath {
  if (!isAbsolute(path)) throw new ShuntError("UNSAFE_SOURCE", "RELATIVE_PATH");
  let lst;
  try {
    lst = lstatSync(path, { bigint: true });
  } catch {
    throw new ShuntError("UNSAFE_SOURCE", "NOT_FOUND");
  }
  if (lst.isSymbolicLink()) throw new ShuntError("UNSAFE_SOURCE", "SYMLINK");

  let real: string;
  try {
    real = realpathSync(path);
  } catch {
    real = resolve(path);
  }
  const inRoot = policy.roots.some((root) => real === root || real.startsWith(root + sep));
  if (!inRoot) throw new ShuntError("UNSAFE_SOURCE", "OUTSIDE_WORKSPACE_ROOT");
  if (isSecretPath(real, policy) || matchesDenylist(real, policy)) {
    throw new ShuntError("UNSAFE_SOURCE", "SECRET_PATH");
  }

  let st;
  try {
    st = statSync(real, { bigint: true });
  } catch {
    throw new ShuntError("UNSAFE_SOURCE", "NOT_FOUND");
  }
  if (!st.isFile()) throw new ShuntError("UNSAFE_SOURCE", "NOT_REGULAR_FILE");
  if (st.nlink > 1n) {
    // A hardlinked file can be re-pointed outside the root between checks.
    throw new ShuntError("UNSAFE_SOURCE", "HARDLINKED");
  }
  return {
    real,
    dev: Number(st.dev),
    ino: Number(st.ino),
    size: Number(st.size),
    mtimeNs: st.mtimeNs,
  };
}

/** Stream a file, refusing at the cap instead of allocating past it. */
export function readBounded(path: string, maxBytes: number): Uint8Array {
  const CHUNK = 256 * 1024;
  const fd = openSync(path, "r");
  const parts: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const buf = Buffer.allocUnsafe(CHUNK);
      const read = readSync(fd, buf, 0, CHUNK, null);
      if (read === 0) break;
      if (total + read > maxBytes) throw new ShuntError("LIMIT_EXCEEDED", "SOURCE_OVER_BYTE_CAP");
      parts.push(buf.subarray(0, read));
      total += read;
    }
  } finally {
    closeSync(fd);
  }
  return Buffer.concat(parts, total);
}

/**
 * Snapshot a file and re-check identity afterwards: a file swapped mid-read yields
 * `SOURCE_CHANGED` rather than a snapshot mixing two versions.
 */
export function assertUnchanged(authorized: AuthorizedPath, readBytes: number): void {
  let after;
  try {
    after = statSync(authorized.real, { bigint: true });
  } catch {
    throw new ShuntError("SOURCE_CHANGED", "DISAPPEARED");
  }
  if (Number(after.dev) !== authorized.dev || Number(after.ino) !== authorized.ino) {
    throw new ShuntError("SOURCE_CHANGED", "IDENTITY_CHANGED");
  }
  if (Number(after.size) !== readBytes || after.mtimeNs !== authorized.mtimeNs) {
    throw new ShuntError("SOURCE_CHANGED", "MODIFIED_DURING_READ");
  }
}
