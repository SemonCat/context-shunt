/**
 * Deterministic, bounded TypeScript port of the incumbent legacy tool-result compactor.
 *
 * This module is deliberately pure: it has no store access, no model bridge and no
 * filesystem I/O. It shapes a safe heuristic summary for the session fallback path after
 * exhausted reader availability. The caller owns persistence and envelope guards.
 *
 * The text-shaping rules mirror `packages/core-py/src/context_shunt/legacy_compact.py`:
 * signal-line detection, head/tail samples, repeated-line counts, JSON structure and
 * interesting-field extraction, representative JSON snippets, and inline secret redaction.
 * The result is always bounded by `hardChars`; it is a heuristic summary and never claims
 * to be complete source bytes or model-derived prose.
 */

const SIGNAL_RE =
  /\b(error|exception|fail(?:ed|ure|ing)?|timeout|traceback)\b|status(?:_code|Code|\s+code)?\s*(?::|=|\s)\s*5\d\d\b|\bhttp[/ ]\S*\s+5\d\d\b/i;
const INTERESTING_KEY_RE =
  /(query|logql|expr|expression|start|end|from|to|time|timestamp|count|total|limit|status|statuscode|status_code|resulttype|datasource)/i;
const SECRET_VALUE_RE =
  /(bearer\s+)[A-Za-z0-9._~+/=-]{20,}|\b(sk-[A-Za-z0-9][A-Za-z0-9_-]{16,})\b|\b(xox[baprs]-[A-Za-z0-9-]{20,})\b/gi;

const MAX_SIGNAL_LINES = 80;
const MAX_SAMPLE_LINES = 24;
const MAX_REPEATED_LINES = 24;
const MAX_JSON_PATHS = 80;
const MAX_JSON_STRINGS = 40;
const MAX_LINE_CHARS = 4_000;
export const DEFAULT_LEGACY_HARD_CHARS = 60_000;
/** Session fallback default, matching the Python reader configuration. */
export const DEFAULT_LEGACY_SESSION_HARD_CHARS = 16_000;

type IndexedLine = readonly [number, string];

function charLength(text: string): number {
  // Python's source implementation measures Unicode code points. Array.from keeps the
  // port's hard cap and line previews from splitting a surrogate pair.
  return Array.from(text).length;
}

function takeChars(text: string, count: number): string {
  return Array.from(text).slice(0, Math.max(0, count)).join("");
}

function tailChars(text: string, count: number): string {
  const chars = Array.from(text);
  return chars.slice(Math.max(0, chars.length - Math.max(0, count))).join("");
}

function redactSecretValues(text: string): string {
  return text.replace(SECRET_VALUE_RE, (_match: string, bearerPrefix?: string) =>
    `${bearerPrefix ?? ""}[redacted secret]`);
}

function linePreview(line: string, maxChars = MAX_LINE_CHARS): string {
  if (charLength(line) <= maxChars) return line;
  const head = Math.floor(maxChars / 2);
  const tail = maxChars - head;
  return `${takeChars(line, head)}\n[... line truncated from ${charLength(line)} chars ...]\n${tailChars(line, tail)}`;
}

function numberedLines(lines: readonly IndexedLine[], maxLines: number): string[] {
  const output: string[] = [];
  for (const [lineNo, line] of lines.slice(0, maxLines)) {
    output.push(`L${lineNo}: ${linePreview(line)}`);
  }
  if (lines.length > maxLines) {
    output.push(`... ${lines.length - maxLines} more matching lines omitted ...`);
  }
  return output;
}

function formatCollapsedLine(lineNo: number, line: string, repeat: number): string {
  const suffix = repeat > 1 ? ` [repeated ${repeat}x]` : "";
  return `L${lineNo}: ${linePreview(line)}${suffix}`;
}

function collapseConsecutive(lines: readonly IndexedLine[]): string[] {
  if (lines.length === 0) return [];
  const output: string[] = [];
  let [startNo, current] = lines[0] as [number, string];
  let repeat = 1;
  for (const [lineNo, line] of lines.slice(1)) {
    if (line === current) {
      repeat += 1;
      continue;
    }
    output.push(formatCollapsedLine(startNo, current, repeat));
    startNo = lineNo;
    current = line;
    repeat = 1;
  }
  output.push(formatCollapsedLine(startNo, current, repeat));
  return output;
}

function splitLines(text: string): string[] {
  if (text.length === 0) return [];
  // This is the set of line boundaries recognized by Python str.splitlines(). The final
  // empty item is removed because splitlines() does not manufacture a line after a
  // terminating boundary.
  const lines = text.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/u);
  if (/[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]$/u.test(text)) lines.pop();
  return lines;
}

function sampleLines(lines: readonly string[]): { first: IndexedLine[]; last: IndexedLine[] } {
  const indexed: IndexedLine[] = lines.map((line, index) => [index + 1, line]);
  if (indexed.length <= MAX_SAMPLE_LINES * 2) return { first: indexed, last: [] };
  return {
    first: indexed.slice(0, MAX_SAMPLE_LINES),
    last: indexed.slice(-MAX_SAMPLE_LINES),
  };
}

function topRepeatedLines(lines: readonly string[]): string[] {
  const counts = new Map<string, number>();
  for (const line of lines) {
    if (line.trim().length === 0) continue;
    counts.set(line, (counts.get(line) ?? 0) + 1);
  }
  const repeated = [...counts.entries()]
    .filter(([, count]) => count > 1)
    // Array#sort is stable on supported Node versions. The original Map order is the
    // Counter tie-breaker used by Python, so equal counts retain first occurrence order.
    .sort((left, right) => right[1] - left[1]);
  const output = repeated.slice(0, MAX_REPEATED_LINES)
    .map(([line, count]) => `${count}x: ${linePreview(line)}`);
  if (repeated.length > MAX_REPEATED_LINES) {
    output.push(`... ${repeated.length - MAX_REPEATED_LINES} more repeated lines omitted ...`);
  }
  return output;
}

function compactLogText(text: string): string {
  const lines = splitLines(text);
  const signals: IndexedLine[] = lines
    .map((line, index) => [index + 1, line] as IndexedLine)
    .filter(([, line]) => SIGNAL_RE.test(line));
  // RegExp#test with a non-global expression is stateless, but explicitly clear lastIndex
  // in case the expression is edited to add a flag later.
  SIGNAL_RE.lastIndex = 0;
  const { first, last } = sampleLines(lines);
  const repeated = topRepeatedLines(lines);

  const sections: string[] = [
    `Line count: ${lines.length}`,
    `High-signal line count: ${signals.length}`,
  ];
  if (signals.length > 0) {
    sections.push("", "High-signal exact lines:", ...numberedLines(signals, MAX_SIGNAL_LINES));
  }
  sections.push("", "First sample lines:", ...collapseConsecutive(first));
  if (last.length > 0) {
    sections.push("", "Last sample lines:", ...collapseConsecutive(last));
  }
  if (repeated.length > 0) sections.push("", "Repeated exact lines:", ...repeated);
  return sections.join("\n");
}

function jsonType(value: unknown): string {
  if (Array.isArray(value)) return `array(${value.length} items)`;
  if (typeof value === "object" && value !== null) {
    return `object(${Object.keys(value as Record<string, unknown>).length} keys)`;
  }
  if (typeof value === "string") return `string(${charLength(value)} chars)`;
  if (value === null) return "null";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return Number.isInteger(value) ? "int" : "float";
  return typeof value;
}

/** Python json.dumps(..., ensure_ascii=False, sort_keys=True) for parsed JSON values. */
function jsonDump(value: unknown, sortKeys: boolean): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map((item) => jsonDump(item, sortKeys)).join(", ")}]`;
  const object = value as Record<string, unknown>;
  const keys = Object.keys(object);
  if (sortKeys) keys.sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}: ${jsonDump(object[key], sortKeys)}`).join(", ")}}`;
}

function jsonScalar(value: unknown): string {
  const encoded = jsonDump(value, true);
  if (charLength(encoded) <= 500) return encoded;
  return `${takeChars(encoded, 500)}... (${charLength(encoded)} chars total)`;
}

function* walkJson(value: unknown, path = "$"): Generator<[string, unknown]> {
  yield [path, value];
  if (Array.isArray(value)) {
    for (let index = 0; index < value.length; index += 1) {
      yield* walkJson(value[index], `${path}[${index}]`);
    }
  } else if (typeof value === "object" && value !== null) {
    for (const key of Object.keys(value as Record<string, unknown>)) {
      const safeKey = key.replaceAll("\\", "\\\\").replaceAll(".", "\\.");
      yield* walkJson((value as Record<string, unknown>)[key], `${path}.${safeKey}`);
    }
  }
}

function jsonStructureLines(parsed: unknown): string[] {
  const lines: string[] = [`Root: ${jsonType(parsed)}`];
  let count = 0;
  for (const [path, value] of walkJson(parsed)) {
    if (path === "$") continue;
    if (!Array.isArray(value) && (typeof value !== "object" || value === null)) continue;
    lines.push(`${path}: ${jsonType(value)}`);
    count += 1;
    if (count >= MAX_JSON_PATHS) {
      lines.push("... additional JSON containers omitted ...");
      break;
    }
  }
  return lines;
}

function jsonInterestingLines(parsed: unknown): string[] {
  const lines: string[] = [];
  for (const [path, value] of walkJson(parsed)) {
    const tail = path.slice(path.lastIndexOf(".") + 1);
    if (!INTERESTING_KEY_RE.test(tail)) continue;
    INTERESTING_KEY_RE.lastIndex = 0;
    if (Array.isArray(value) || (typeof value === "object" && value !== null)) {
      lines.push(`${path}: ${jsonType(value)}`);
    } else {
      lines.push(`${path}: ${jsonScalar(value)}`);
    }
    if (lines.length >= MAX_JSON_PATHS) {
      lines.push("... additional query/time/count/status fields omitted ...");
      break;
    }
  }
  return lines;
}

function jsonStringSamples(parsed: unknown): { signals: string[]; strings: string[] } {
  const strings: string[] = [];
  const signalStrings: string[] = [];
  for (const [path, value] of walkJson(parsed)) {
    if (typeof value !== "string" || value.length === 0) continue;
    const line = `${path}: ${value}`;
    if (SIGNAL_RE.test(value)) signalStrings.push(line);
    else if (strings.length < MAX_JSON_STRINGS) strings.push(line);
    SIGNAL_RE.lastIndex = 0;
    if (signalStrings.length >= MAX_SIGNAL_LINES && strings.length >= MAX_JSON_STRINGS) break;
  }
  return { signals: signalStrings, strings };
}

function jsonRepresentativeSnippets(parsed: unknown): string[] {
  const snippets: string[] = [];
  if (Array.isArray(parsed) && parsed.length > 0) {
    snippets.push(`First array item: ${takeChars(jsonDump(parsed[0], true), 2_000)}`);
    if (parsed.length > 1) {
      snippets.push(`Last array item: ${takeChars(jsonDump(parsed[parsed.length - 1], true), 2_000)}`);
    }
  } else if (typeof parsed === "object" && parsed !== null) {
    const object = parsed as Record<string, unknown>;
    const keys = Object.keys(object);
    snippets.push(`Top-level keys: ${keys.slice(0, 40).join(", ")}`);
    for (const key of keys.slice(0, 5)) {
      const value = object[key];
      if (Array.isArray(value) || (typeof value === "object" && value !== null)) {
        snippets.push(`$.${key}: ${takeChars(jsonDump(value, true), 2_000)}`);
      } else {
        snippets.push(`$.${key}: ${jsonScalar(value)}`);
      }
    }
  }
  return snippets;
}

function compactJsonText(text: string, parsed: unknown): string {
  const sections: string[] = ["JSON structure:", ...jsonStructureLines(parsed)];

  const interesting = jsonInterestingLines(parsed);
  if (interesting.length > 0) sections.push("", "Query/time/count/status fields:", ...interesting);

  const { signals, strings } = jsonStringSamples(parsed);
  if (signals.length > 0) {
    sections.push(
      "",
      "High-signal exact JSON string values:",
      ...numberedLines(signals.map((line, index) => [index + 1, line]), MAX_SIGNAL_LINES),
    );
  }
  if (strings.length > 0) {
    sections.push(
      "",
      "Representative exact JSON string values:",
      ...numberedLines(strings.map((line, index) => [index + 1, line]), MAX_JSON_STRINGS),
    );
  }

  const snippets = jsonRepresentativeSnippets(parsed);
  if (snippets.length > 0) {
    sections.push("", "Representative JSON snippets:", ...snippets.map((snippet) => linePreview(snippet, 2_500)));
  }

  const multiline = [...walkJson(parsed)]
    .filter(([, value]) => typeof value === "string" && value.includes("\n"))
    .map(([, value]) => value as string)
    .join("\n");
  if (multiline.length > 0) {
    sections.push("", "Embedded multiline text summary:", compactLogText(multiline));
  } else if (signals.length === 0 && strings.length === 0) {
    sections.push("", "Raw JSON prefix:", takeChars(text, 4_000));
  }
  return sections.join("\n");
}

function tryParseJson(text: string): unknown | undefined {
  const stripped = text.trim();
  if (stripped.length === 0 || (stripped[0] !== "[" && stripped[0] !== "{")) return undefined;
  try {
    return JSON.parse(stripped) as unknown;
  } catch {
    return undefined;
  }
}

function capText(text: string, hardChars: number): string {
  const maximum = Math.max(1, Number.isFinite(hardChars) ? Math.floor(hardChars) : DEFAULT_LEGACY_HARD_CHARS);
  if (charLength(text) <= maximum) return text;
  const notice = `\n\n[Compacted summary truncated to ${maximum} chars. Original source is larger.]`;
  // The Python incumbent is normally called with a cap large enough for the notice. Keep
  // the stronger invariant here for a narrowed/test cap: no result may exceed its bound.
  if (charLength(notice) >= maximum) return takeChars(notice, maximum);
  const keep = Math.max(0, maximum - charLength(notice));
  return `${takeChars(text, keep).trimEnd()}${notice}`;
}

export interface LegacyCompactOptions {
  readonly hardChars?: number;
}

/** Build a deterministic heuristic summary for any text or JSON-shaped input. */
export function compactToolResult(
  text: string,
  options: LegacyCompactOptions | number = {},
): string {
  const hardChars = typeof options === "number"
    ? options
    : options.hardChars ?? DEFAULT_LEGACY_HARD_CHARS;
  const redacted = redactSecretValues(text);
  const parsed = tryParseJson(redacted);
  const compacted = parsed === undefined
    ? compactLogText(redacted)
    : compactJsonText(redacted, parsed);
  return capText(compacted, hardChars);
}
