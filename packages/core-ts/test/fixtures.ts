import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { contractsDir } from "../src/limits.js";

export function conformance<T = any>(name: string): T {
  return JSON.parse(readFileSync(join(contractsDir(), "conformance", name), "utf8")) as T;
}

export function fixtureDocs(kind: "request" | "envelope", validity: "valid" | "invalid") {
  const dir = join(contractsDir(), "fixtures", kind, validity);
  const names = readdirSync(dir).filter((n) => n.endsWith(".json")).sort();
  if (names.length === 0) throw new Error(`fixture corpus ${kind}/${validity} is empty`);
  return names.map((name) => ({
    name,
    document: JSON.parse(readFileSync(join(dir, name), "utf8")).document as unknown,
  }));
}
