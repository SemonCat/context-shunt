/** Bounded deterministic aggregation over an already-validated JSON snapshot. */
import { ShuntError } from "./errors.js";
import { type Extraction, escapedJsonCost, segmentWireOverhead } from "./inspect.js";
import { type Limits } from "./limits.js";
import { canonicalJson, jsonDepthAndNodes, resolvePointer, type Snapshot } from "./snapshot.js";

const MAX_RETURNED_VALUES = 200;
const MAX_RETURNED_GROUPS = 200;
const MAX_SCALAR_BYTES = 512;
const MISSING = Symbol("missing");
const MISSING_OUTPUT = Object.freeze({ missing: true as const });
type Scalar = string | number | boolean | null;
type GroupValue = Scalar | typeof MISSING_OUTPUT;

function optionalPointer(value: unknown, pointer: string): unknown | typeof MISSING {
  try {
    return resolvePointer(value, pointer);
  } catch (err) {
    if (err instanceof ShuntError && err.detail === "POINTER_NOT_FOUND") return MISSING;
    throw err;
  }
}

function scalar(value: unknown): Scalar | typeof MISSING {
  if (value === MISSING) return MISSING;
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new ShuntError("INVALID_REQUEST", "BAD_SELECTOR", false);
    }
    return value;
  }
  if (value === null || ["string", "boolean"].includes(typeof value)) {
    return value as string | boolean | null;
  }
  throw new ShuntError("INVALID_REQUEST", "BAD_SELECTOR", false);
}

function scalarKey(value: Scalar): string {
  return canonicalJson(value);
}

function canonicalOrder(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
}

function outputScalar(value: GroupValue): boolean {
  return Buffer.byteLength(canonicalJson(value), "utf8") <= MAX_SCALAR_BYTES;
}

export interface AggregateSelector {
  records_pointer: string;
  expand_pointer?: string;
  record_pointer?: string;
  parse_json?: boolean;
  filter?: { pointer: string; equals?: unknown; contains?: string };
  group_by?: string[];
  distinct?: string[];
}

/**
 * Aggregate every selected record in one pass. The source JSON has already passed the
 * snapshot depth/node caps; embedded JSON strings are independently checked and share the
 * same cumulative node and byte ceilings, so minified Loki payloads cannot amplify work.
 */
export function aggregateSnapshot(
  snapshot: Snapshot,
  selector: AggregateSelector,
  opts: { maxResultBytes: number; maxWireBytes: number; maxRecords: number; limits: Limits },
): Extraction {
  let root = snapshot.jsonValue;
  if (root === undefined) {
    if (snapshot.mediaType !== "text/plain") {
      throw new ShuntError("INVALID_REQUEST", "BAD_SELECTOR", false);
    }
    try {
      root = JSON.parse(Buffer.from(snapshot.data).toString("utf8"));
    } catch {
      throw new ShuntError("INVALID_REQUEST", "BAD_JSON", false);
    }
    jsonDepthAndNodes(root, opts.limits);
  }
  const outer = resolvePointer(root, selector.records_pointer);
  if (!Array.isArray(outer)) {
    throw new ShuntError("INVALID_REQUEST", "BAD_SELECTOR", false);
  }

  const records: unknown[] = [];
  let workUnits = 0;
  for (const item of outer) {
    workUnits += 1;
    if (workUnits > opts.maxRecords) {
      throw new ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", false);
    }
    const expanded = selector.expand_pointer === undefined
      ? [item]
      : resolvePointer(item, selector.expand_pointer);
    if (!Array.isArray(expanded)) {
      throw new ShuntError("INVALID_REQUEST", "BAD_SELECTOR", false);
    }
    if (selector.expand_pointer !== undefined) workUnits += expanded.length;
    if (workUnits > opts.maxRecords) {
      throw new ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", false);
    }
    records.push(...expanded);
  }

  const distinctPaths = selector.distinct ?? [];
  const groupPaths = selector.group_by ?? [];
  const distinct = new Map<string, Map<string, Scalar>>(
    distinctPaths.map((path) => [path, new Map()]),
  );
  const groups = new Map<string, { key: GroupValue[]; count: number }>();
  let matched = 0;
  let parsedBytes = 0;
  let parsedNodes = 0;
  const filter = selector.filter;
  let filterExpectedKey: string | undefined;
  if (filter !== undefined && Object.prototype.hasOwnProperty.call(filter, "equals")) {
    const expected = scalar(filter.equals);
    if (expected === MISSING || !outputScalar(expected)) {
      throw new ShuntError("INVALID_REQUEST", "BAD_SELECTOR", false);
    }
    filterExpectedKey = canonicalJson(expected);
  }

  for (const raw of records) {
    let record = selector.record_pointer === undefined
      ? raw
      : resolvePointer(raw, selector.record_pointer);
    if (selector.parse_json) {
      if (typeof record !== "string") {
        throw new ShuntError("INVALID_REQUEST", "BAD_SELECTOR", false);
      }
      parsedBytes += Buffer.byteLength(record, "utf8");
      if (parsedBytes > opts.limits.maxSourceBytes) {
        throw new ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", false);
      }
      try {
        record = JSON.parse(record);
      } catch {
        throw new ShuntError("INVALID_REQUEST", "BAD_JSON", false);
      }
      parsedNodes += jsonDepthAndNodes(record, opts.limits).nodes;
      if (parsedNodes > opts.limits.jsonMaxNodes) {
        throw new ShuntError("LIMIT_EXCEEDED", "JSON_TOO_MANY_NODES", false);
      }
    }

    if (filter !== undefined) {
      const candidate = optionalPointer(record, filter.pointer);
      if (candidate === MISSING) continue;
      if (Object.prototype.hasOwnProperty.call(filter, "equals")) {
        if (candidate !== null && !["string", "number", "boolean"].includes(typeof candidate)) {
          continue;
        }
        const normalizedCandidate = scalar(candidate);
        if (normalizedCandidate === MISSING || !outputScalar(normalizedCandidate)
          || canonicalJson(normalizedCandidate) !== filterExpectedKey) continue;
      } else if (typeof candidate !== "string" || !candidate.includes(filter.contains ?? "")) {
        continue;
      }
    }
    matched += 1;

    for (const path of distinctPaths) {
      const value = scalar(optionalPointer(record, path));
      if (value !== MISSING) distinct.get(path)?.set(scalarKey(value), value);
    }
    if (groupPaths.length > 0) {
      const key = groupPaths.map((path) => {
        const value = scalar(optionalPointer(record, path));
        return value === MISSING ? MISSING_OUTPUT : value;
      });
      const encoded = canonicalJson(key);
      const prior = groups.get(encoded);
      if (prior) prior.count += 1;
      else groups.set(encoded, { key, count: 1 });
    }
  }

  const distinctRows = distinctPaths.map((path) => {
    const all = [...(distinct.get(path)?.entries() ?? [])].sort(([a], [b]) => canonicalOrder(a, b));
    const values = all.filter(([, value]) => outputScalar(value))
      .slice(0, MAX_RETURNED_VALUES).map(([, value]) => value);
    return { path, count: all.length, values, values_complete: values.length === all.length };
  });
  const allGroups = [...groups.entries()].sort(([a], [b]) => canonicalOrder(a, b));
  let groupRows = allGroups.filter(([, row]) => row.key.every(outputScalar))
    .slice(0, MAX_RETURNED_GROUPS).map(([, row]) => row);
  let groupsComplete = groupRows.length === allGroups.length;

  const build = (): string => canonicalJson({
    distinct: distinctRows,
    group_by: groupPaths,
    group_count: allGroups.length,
    groups: groupRows,
    groups_complete: groupsComplete,
    matched_count: matched,
    records_scanned: records.length,
    schema: "context_shunt.aggregate.v1",
  });
  let text = build();
  const fits = (): boolean => {
    const bytes = Buffer.byteLength(text, "utf8");
    return bytes <= opts.maxResultBytes
      && segmentWireOverhead("aggregate", 0, records.length) + escapedJsonCost(text)
        <= opts.maxWireBytes;
  };
  while (!fits() && groupRows.length > 0) {
    groupRows = groupRows.slice(0, -1);
    groupsComplete = false;
    text = build();
  }
  while (!fits() && distinctRows.some((row) => row.values.length > 0)) {
    const row = [...distinctRows].reverse().find((candidate) => candidate.values.length > 0)!;
    row.values = row.values.slice(0, -1);
    row.values_complete = false;
    text = build();
  }
  if (!fits()) throw new ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_PAGE_BUDGET", false);
  const resultBytes = Buffer.byteLength(text, "utf8");
  return {
    mode: "aggregate",
    segments: [{ kind: "aggregate", start: 0, end: records.length, text }],
    resultBytes,
    complete: true,
    nextCursorState: undefined,
    // Aggregate traverses parsed records, not the text line index.
    linesScanned: 0,
    recordsScanned: records.length,
    recordsMatched: matched,
    scanBudgetExhausted: false,
    matchesFound: undefined,
    stalled: false,
    stallReason: "content",
  };
}
