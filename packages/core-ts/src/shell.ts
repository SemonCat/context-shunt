/**
 * Classification of read-like shell commands.
 *
 * This is not a shell sandbox and does not claim to understand arbitrary scripts. It
 * recognises a small, explicitly enumerated set of provably bounded forms and refuses
 * everything else that touches a file: an unrecognised read-like command is
 * `UNCLASSIFIABLE_READ`, not "probably fine".
 *
 * A string regex is not the defence. The command is scanned into words with quote state
 * tracked, and any shell metacharacter (pipe, list, redirect, substitution, expansion,
 * glob, tilde) makes the command non-simple; a non-simple command that reads a file is
 * refused rather than parsed further.
 *
 * Kept semantically identical to `packages/core-py/src/context_shunt/shell.py`; both are
 * pinned by `contracts/v1/conformance/gate-cases.json`.
 */
export type ShellForm =
  | "not_read_like"
  | "full_read"
  | "bounded_lines"
  | "bounded_search"
  | "bounded_metadata"
  | "unclassifiable";

const UNSAFE_CHARS = new Set("|&;<>()$`*?[]{}~!\n\r\\".split(""));
const SEPARATORS = new Set(["|", "&", ";", "<", ">", "\n", "\r"]);

const FULL_READ_CMDS = new Set(["cat", "less", "more", "nl", "tac"]);
const LINE_BOUNDED_CMDS = new Set(["head", "tail"]);
const SEARCH_CMDS = new Set(["grep", "egrep", "fgrep", "rg"]);
// `file` and `stat` are deliberately opaque. Their flag surfaces can select files or
// user-controlled formatting, so only `wc`'s small, enumerated metadata form is allowed.
const METADATA_CMDS = new Set(["wc"]);
const OPAQUE_READ_CMDS = new Set([
  "awk", "gawk", "mawk", "sed", "od", "xxd", "hexdump", "strings", "base64", "cut", "rev",
  "pr", "fold", "expand", "unexpand", "join", "paste", "split", "csplit", "uniq", "sort",
  "column", "jq", "yq", "xmllint", "perl", "python", "python3", "ruby", "node", "dd",
  "tee", "bat", "view", "vim", "vi", "emacs", "file", "stat",
  "source", ".", "read", "mapfile", "readarray",
]);

export const READ_LIKE_CMDS = new Set<string>([
  ...FULL_READ_CMDS, ...LINE_BOUNDED_CMDS, ...SEARCH_CMDS, ...METADATA_CMDS, ...OPAQUE_READ_CMDS,
]);

const CAT_FLAGS = new Set(["-n", "-b", "-s", "-E", "-T", "-v", "-e", "-t", "-A", "-u"]);
const HEAD_TAIL_VALUE_FLAGS = new Set(["-n"]);
const HEAD_TAIL_REJECT_FLAGS = new Set(["-c", "-f", "-F", "--bytes", "--follow"]);
const GREP_VALUE_FLAGS = new Set(["-m", "-A", "-B", "-C", "--max-count", "-e"]);
const GREP_BOOL_FLAGS = new Set(["-i", "-n", "-H", "-h", "-w", "-x", "-F", "-E"]);
const WC_FLAGS = new Set(["-l", "-c", "-w", "-m", "-L"]);
const HEAD_TAIL_DEFAULT_LINES = 10;
const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
const SCRIPT_WRAPPERS = new Set([
  "sh", "bash", "zsh", "dash", "ksh", "fish", "sudo", "doas", "nice", "nohup",
  "timeout", "stdbuf", "xargs", "find", "busybox", "exec",
  "time", "if", "then", "elif", "while", "until", "for", "case", "eval", "builtin",
  "setsid", "watch", "coproc",
]);

export interface Classification {
  readonly form: ShellForm;
  readonly files: readonly string[];
  readonly boundLines?: number;
  readonly boundMatches?: number;
  readonly lineStart?: number;
  readonly fromEnd?: boolean;
  readonly metadataFields?: number;
  readonly reason: string;
}

interface Word {
  text: string;
  quoted: boolean;
  unsafe: boolean;
}

function result(form: ShellForm, extra: Partial<Classification> = {}): Classification {
  return { form, files: extra.files ?? [], reason: extra.reason ?? "", ...extra } as Classification;
}

function splitWords(command: string): { words: Word[]; sawUnsafe: boolean } | null {
  const words: Word[] = [];
  let buf = "";
  let quoted = false;
  let unsafeWord = false;
  let sawUnsafe = false;
  let state: "plain" | "single" | "double" = "plain";

  const flush = () => {
    if (buf.length > 0 || quoted) {
      words.push({ text: buf, quoted, unsafe: unsafeWord });
      buf = "";
      quoted = false;
      unsafeWord = false;
    }
  };

  for (const ch of command) {
    if (state === "plain") {
      if (ch === " " || ch === "\t") {
        flush();
        continue;
      }
      if (ch === "'") {
        state = "single";
        quoted = true;
        continue;
      }
      if (ch === '"') {
        state = "double";
        quoted = true;
        continue;
      }
      if (UNSAFE_CHARS.has(ch)) {
        sawUnsafe = true;
        unsafeWord = true;
        if (SEPARATORS.has(ch)) {
          flush();
          words.push({ text: ch, quoted: false, unsafe: true });
          unsafeWord = false;
          continue;
        }
      }
      buf += ch;
    } else if (state === "single") {
      if (ch === "'") {
        state = "plain";
        continue;
      }
      buf += ch;
    } else {
      if (ch === '"') {
        state = "plain";
        continue;
      }
      // Inside double quotes `$` and a backtick still expand.
      if (ch === "$" || ch === "`") {
        sawUnsafe = true;
        unsafeWord = true;
      }
      buf += ch;
    }
  }
  if (state !== "plain") return null; // unterminated quote
  flush();
  return { words, sawUnsafe };
}

function segments(words: Word[]): Word[][] {
  const out: Word[][] = [[]];
  for (const word of words) {
    if (!word.quoted && SEPARATORS.has(word.text)) {
      out.push([]);
      continue;
    }
    (out[out.length - 1] as Word[]).push(word);
  }
  return out.filter((seg) => seg.length > 0);
}

function basename(cmd: string): string {
  const parts = cmd.split("/");
  return parts[parts.length - 1] as string;
}

function operandsOf(head: string, args: string[]): string[] {
  let valueFlags = new Set<string>();
  if (LINE_BOUNDED_CMDS.has(head)) valueFlags = HEAD_TAIL_VALUE_FLAGS;
  else if (SEARCH_CMDS.has(head)) valueFlags = GREP_VALUE_FLAGS;
  else if (head === "sed") valueFlags = new Set(["-e", "-f"]);

  const operands: string[] = [];
  let skip = false;
  let endOfFlags = false;
  for (const arg of args) {
    if (skip) {
      skip = false;
      continue;
    }
    if (!endOfFlags && arg === "--") {
      endOfFlags = true;
      continue;
    }
    if (!endOfFlags && arg.startsWith("-") && arg !== "-") {
      if (valueFlags.has(arg)) skip = true;
      continue;
    }
    operands.push(arg);
  }
  return operands;
}

/** Flag words before `--`; `-` and `--` are not flags. */
function flagsOf(args: string[]): string[] {
  const out: string[] = [];
  for (const arg of args) {
    if (arg === "--") break;
    if (arg.startsWith("-") && arg !== "-") out.push(arg);
  }
  return out;
}

function hasFileOperand(seg: Word[]): boolean {
  const head = basename((seg[0] as Word).text);
  const args = seg.slice(1).map((w) => w.text);
  const operands = operandsOf(head, args);
  // grep's first operand is the pattern; a file operand comes after it.
  return SEARCH_CMDS.has(head) ? operands.length > 1 : operands.length > 0;
}

function allAbsolute(paths: string[]): boolean {
  return paths.length > 0 && paths.every((p) => p.startsWith("/"));
}

/** Only `sed -n 'A,Bp'` is accepted; every other script is unprovable. */
function parseSedRange(args: string[]): { start: number; count: number } | null {
  if (!args.includes("-n")) return null;
  const scripts = args.filter((a) => !a.startsWith("-") && !a.startsWith("/"));
  if (scripts.length !== 1) return null;
  const script = (scripts[0] as string).trim();
  if (!script.endsWith("p") || !script.includes(",")) return null;
  const [lo, hi] = script.slice(0, -1).split(",");
  if (!lo || !hi || !/^\d+$/.test(lo) || !/^\d+$/.test(hi)) return null;
  const start = Number(lo);
  const end = Number(hi);
  if (start < 1 || end < start) return null;
  return { start, count: end - start + 1 };
}

function unwrapSimple(words: Word[]): { words: Word[]; unsafeWrapper: boolean } {
  let current = [...words];
  let unsafeWrapper = false;

  while (current.length > 0 && ASSIGNMENT.test((current[0] as Word).text)) {
    unsafeWrapper = true;
    current.shift();
  }
  for (;;) {
    const head = basename(current[0]?.text ?? "");
    if (head === "command") {
      current.shift();
      if (current[0]?.text === "--") current.shift();
      if (current[0]?.text.startsWith("-")) return { words: current, unsafeWrapper: true };
      continue;
    }
    if (head === "env") {
      current.shift();
      while (current.length > 0) {
        const arg = (current[0] as Word).text;
        if (arg === "--" || arg === "-i" || arg === "--ignore-environment") {
          current.shift();
          continue;
        }
        if (ASSIGNMENT.test(arg)) {
          unsafeWrapper = true;
          current.shift();
          continue;
        }
        if (arg === "-u" || arg === "--unset") {
          unsafeWrapper = true;
          current.splice(0, Math.min(2, current.length));
          continue;
        }
        if (arg.startsWith("--unset=")) {
          unsafeWrapper = true;
          current.shift();
          continue;
        }
        if (arg.startsWith("-")) return { words: current, unsafeWrapper: true };
        break;
      }
      continue;
    }
    break;
  }
  return { words: current, unsafeWrapper };
}

function containsWrappedRead(words: Word[]): boolean {
  const head = basename(words[0]?.text ?? "");
  if (!SCRIPT_WRAPPERS.has(head)) return false;
  for (const word of words.slice(1)) {
    const nested = splitWords(word.text);
    const candidates = nested?.words ?? [word];
    if (candidates.some((candidate) => READ_LIKE_CMDS.has(basename(candidate.text)))) return true;
  }
  return false;
}

/** A read command hidden inside command substitution is still a read command. */
function containsEmbeddedRead(words: Word[]): boolean {
  for (const word of words) {
    if (!word.unsafe) continue;
    const tokens = word.text.match(/[A-Za-z0-9_./+-]+/g) ?? [];
    if (tokens.some((token) => READ_LIKE_CMDS.has(basename(token)))) return true;
  }
  return false;
}

function copiesToStdout(words: Word[]): boolean {
  if (basename(words[0]?.text ?? "") !== "cp") return false;
  const args = words.slice(1).map((word) => word.text);
  const operands = operandsOf("cp", args);
  const destination = operands.at(-1);
  return destination === "/dev/stdout"
    || destination === "/dev/fd/1"
    || destination === "/proc/self/fd/1";
}

export function classifyCommand(command: string): Classification {
  if (!command || command.trim().length === 0) return result("not_read_like");

  const split = splitWords(command);
  if (split === null) {
    const head = basename((command.trim().split(" ")[0] ?? ""));
    return READ_LIKE_CMDS.has(head)
      ? result("unclassifiable", { reason: "UNPARSEABLE" })
      : result("not_read_like");
  }
  let { words } = split;
  const { sawUnsafe } = split;
  if (words.length === 0) return result("not_read_like");

  if (sawUnsafe) {
    const redirectsInput = words.some(
      (w) => (w.text === "<" && !w.quoted) || (w.unsafe && w.text.includes("<")),
    );
    if (redirectsInput) {
      return result("unclassifiable", { reason: "INPUT_REDIRECTION" });
    }
    if (containsEmbeddedRead(words)) {
      return result("unclassifiable", { reason: "COMMAND_SUBSTITUTION" });
    }
    for (const seg of segments(words)) {
      const normalized = unwrapSimple(seg);
      const head = basename(normalized.words[0]?.text ?? "");
      if (copiesToStdout(normalized.words)) {
        return result("unclassifiable", { reason: "STDOUT_FILE_COPY" });
      }
      if (containsWrappedRead(normalized.words)) {
        return result("unclassifiable", { reason: "COMMAND_WRAPPER" });
      }
      if (!READ_LIKE_CMDS.has(head)) continue;
      if (hasFileOperand(normalized.words) || redirectsInput) {
        return result("unclassifiable", { reason: "NON_SIMPLE_COMMAND" });
      }
    }
    return result("not_read_like");
  }

  const normalized = unwrapSimple(words);
  words = normalized.words;
  const head = basename(words[0]?.text ?? "");
  if (copiesToStdout(words)) {
    return result("unclassifiable", { reason: "STDOUT_FILE_COPY" });
  }
  if (containsWrappedRead(words)) {
    return result("unclassifiable", { reason: "COMMAND_WRAPPER" });
  }
  if (!READ_LIKE_CMDS.has(head)) return result("not_read_like");
  if (normalized.unsafeWrapper) {
    return result("unclassifiable", { reason: "UNSAFE_WRAPPER" });
  }

  const args = words.slice(1).map((w) => w.text);
  const flags = flagsOf(args);
  const operands = operandsOf(head, args);

  // Check metadata flags before the no-operand fast path. `wc --files0-from=PATH`
  // embeds its input path in a flag and otherwise looked like a stdin-only command.
  if (METADATA_CMDS.has(head) && flags.some((flag) => !WC_FLAGS.has(flag))) {
    return result("unclassifiable", { reason: "UNKNOWN_FLAG" });
  }

  if (operands.length === 0 && !SEARCH_CMDS.has(head)) {
    // No file operand: the command reads stdin, so no source is being pulled in.
    return result("not_read_like");
  }

  if (FULL_READ_CMDS.has(head)) {
    if (
      (head === "cat" && flags.some((f) => !CAT_FLAGS.has(f)))
      || (head !== "cat" && flags.length > 0)
    ) {
      return result("unclassifiable", { reason: "UNKNOWN_FLAG" });
    }
    if (!allAbsolute(operands)) return result("unclassifiable", { reason: "UNRESOLVED_PATH" });
    return result("full_read", { files: operands });
  }

  if (LINE_BOUNDED_CMDS.has(head)) {
    if (flags.some((f) => HEAD_TAIL_REJECT_FLAGS.has(f) || f.startsWith("--bytes"))) {
      return result("unclassifiable", { reason: "UNSUPPORTED_FLAG" });
    }
    let bound = HEAD_TAIL_DEFAULT_LINES;
    for (let i = 0; i < args.length; i += 1) {
      const arg = args[i] as string;
      if (arg === "-n") {
        const next = args[i + 1];
        if (next === undefined || !/^\d+$/.test(next)) {
          return result("unclassifiable", { reason: "UNPARSEABLE_COUNT" });
        }
        bound = Number(next);
      } else if (arg.startsWith("-") && /^\d+$/.test(arg.slice(1))) {
        bound = Number(arg.slice(1));
      } else if (arg.startsWith("-") && arg !== "-" && !HEAD_TAIL_VALUE_FLAGS.has(arg)) {
        return result("unclassifiable", { reason: "UNKNOWN_FLAG" });
      }
    }
    if (!allAbsolute(operands)) return result("unclassifiable", { reason: "UNRESOLVED_PATH" });
    return result("bounded_lines", {
      files: operands,
      boundLines: bound,
      lineStart: 1,
      fromEnd: head === "tail",
    });
  }

  if (head === "sed") {
    const range = parseSedRange(args);
    const files = operands.filter((a) => a.startsWith("/"));
    if (range === null || files.length === 0) {
      return result("unclassifiable", { reason: "UNPROVABLE_SED_SCRIPT" });
    }
    return result("bounded_lines", {
      files,
      boundLines: range.count,
      lineStart: range.start,
      fromEnd: false,
    });
  }

  if (SEARCH_CMDS.has(head)) {
    if (flags.some((f) => !GREP_VALUE_FLAGS.has(f) && !GREP_BOOL_FLAGS.has(f))) {
      return result("unclassifiable", { reason: "UNKNOWN_FLAG" });
    }
    if (flags.some((f) => ["-A", "-B", "-C"].includes(f))) {
      return result("unclassifiable", { reason: "OUTPUT_AMPLIFICATION" });
    }
    let limit: number | null = null;
    for (let i = 0; i < args.length; i += 1) {
      const arg = args[i] as string;
      if (arg === "-m" || arg === "--max-count") {
        const next = args[i + 1];
        if (next === undefined || !/^\d+$/.test(next)) {
          return result("unclassifiable", { reason: "UNPARSEABLE_COUNT" });
        }
        limit = Number(next);
      }
    }
    const files = operands.slice(1);
    if (limit === null || limit < 1) return result("unclassifiable", { reason: "UNBOUNDED_SEARCH" });
    if (files.length === 0) return result("not_read_like");
    if (!allAbsolute(files)) return result("unclassifiable", { reason: "UNRESOLVED_PATH" });
    return result("bounded_search", { files, boundMatches: limit });
  }

  if (METADATA_CMDS.has(head)) {
    if (!allAbsolute(operands)) return result("unclassifiable", { reason: "UNRESOLVED_PATH" });
    const metadataFields = flags.length === 0 ? 3 : new Set(flags).size;
    return result("bounded_metadata", { files: operands, metadataFields });
  }

  return result("unclassifiable", { reason: "OPAQUE_READ" });
}
