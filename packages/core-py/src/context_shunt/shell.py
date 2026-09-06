"""Classification of read-like shell commands.

This is not a shell sandbox and does not claim to understand arbitrary scripts. It
recognises a small, explicitly enumerated set of *provably* bounded forms and refuses
everything else that touches a file, which is the only safe direction: an unrecognised
read-like command is ``UNCLASSIFIABLE_READ``, not "probably fine".

A string regex is not the defence. The command is scanned into words with quote state
tracked, and any shell metacharacter (pipe, list, redirect, substitution, expansion,
glob, tilde) makes the command non-simple; a non-simple command that reads a file is
refused rather than parsed further.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# Metacharacters that end the "one simple command" guarantee.
_UNSAFE_CHARS = set("|&;<>()$`*?[]{}~!\n\r\\")


class ShellForm(str, Enum):
    NOT_READ_LIKE = "not_read_like"
    FULL_READ = "full_read"
    BOUNDED_LINES = "bounded_lines"
    BOUNDED_SEARCH = "bounded_search"
    BOUNDED_METADATA = "bounded_metadata"
    UNCLASSIFIABLE = "unclassifiable"


# Commands that pull whole file contents into the transcript.
_FULL_READ_CMDS = frozenset({"cat", "less", "more", "nl", "tac"})
# Commands whose output is bounded by an explicit line count.
_LINE_BOUNDED_CMDS = frozenset({"head", "tail"})
_SEARCH_CMDS = frozenset({"grep", "egrep", "fgrep", "rg"})
# ``file`` and ``stat`` have file-selecting / user-formatting flag surfaces. Only the
# small, enumerated ``wc`` metadata form is accepted; the others remain read-like but
# opaque and therefore fail closed.
_METADATA_CMDS = frozenset({"wc"})
# Read-like but not provably bounded in lines: refused whenever a file operand is present.
_OPAQUE_READ_CMDS = frozenset(
    {
        "awk",
        "gawk",
        "mawk",
        "sed",
        "od",
        "xxd",
        "hexdump",
        "strings",
        "base64",
        "cut",
        "rev",
        "pr",
        "fold",
        "expand",
        "unexpand",
        "join",
        "paste",
        "split",
        "csplit",
        "uniq",
        "sort",
        "column",
        "jq",
        "yq",
        "xmllint",
        "perl",
        "python",
        "python3",
        "ruby",
        "node",
        "dd",
        "tee",
        "bat",
        "view",
        "vim",
        "vi",
        "emacs",
        "file",
        "stat",
        "source",
        ".",
        "read",
        "mapfile",
        "readarray",
    }
)
READ_LIKE_CMDS = (
    _FULL_READ_CMDS | _LINE_BOUNDED_CMDS | _SEARCH_CMDS | _METADATA_CMDS | _OPAQUE_READ_CMDS
)

# Flags accepted on the provably-safe forms. An unknown flag is a refusal, not a guess.
_CAT_FLAGS = frozenset({"-n", "-b", "-s", "-E", "-T", "-v", "-e", "-t", "-A", "-u"})
_HEAD_TAIL_VALUE_FLAGS = frozenset({"-n"})
_HEAD_TAIL_REJECT_FLAGS = frozenset({"-c", "-f", "-F", "--bytes", "--follow"})
_GREP_VALUE_FLAGS = frozenset({"-m", "-A", "-B", "-C", "--max-count", "-e"})
_GREP_BOOL_FLAGS = frozenset({"-i", "-n", "-H", "-h", "-w", "-x", "-F", "-E"})
_WC_FLAGS = frozenset({"-l", "-c", "-w", "-m", "-L"})

_HEAD_TAIL_DEFAULT_LINES = 10
_SCRIPT_WRAPPERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "sudo",
        "doas",
        "nice",
        "nohup",
        "timeout",
        "stdbuf",
        "xargs",
        "find",
        "busybox",
        "exec",
        "time",
        "if",
        "then",
        "elif",
        "while",
        "until",
        "for",
        "case",
        "eval",
        "builtin",
        "setsid",
        "watch",
        "coproc",
    }
)


class TokenizeError(Exception):
    pass


@dataclass(frozen=True)
class Classification:
    form: ShellForm
    files: tuple[str, ...] = ()
    bound_lines: int | None = None
    bound_matches: int | None = None
    line_start: int | None = None
    from_end: bool = False
    metadata_fields: int | None = None
    reason: str = ""


@dataclass
class _Word:
    text: str
    quoted: bool = False
    unsafe: bool = field(default=False)


def _split_words(command: str) -> tuple[list[_Word], bool]:
    """Split into words tracking quotes. Returns (words, saw_unsafe_construct).

    Characters inside single quotes are literal. Inside double quotes, ``$`` and a
    backtick still expand, so they still count as unsafe.
    """
    words: list[_Word] = []
    buf: list[str] = []
    quoted = False
    unsafe_word = False
    saw_unsafe = False
    state = "plain"  # plain | single | double
    for ch in command:
        if state == "plain":
            if ch in (" ", "\t"):
                if buf or quoted:
                    words.append(_Word("".join(buf), quoted, unsafe_word))
                    buf, quoted, unsafe_word = [], False, False
                continue
            if ch == "'":
                state, quoted = "single", True
                continue
            if ch == '"':
                state, quoted = "double", True
                continue
            if ch in _UNSAFE_CHARS:
                saw_unsafe = True
                unsafe_word = True
                # Separators end the current word so segment heads stay visible.
                if ch in "|&;<>\n\r":
                    if buf or quoted:
                        words.append(_Word("".join(buf), quoted, unsafe_word))
                        buf, quoted, unsafe_word = [], False, False
                    words.append(_Word(ch, False, True))
                    unsafe_word = False
                    continue
            buf.append(ch)
        elif state == "single":
            if ch == "'":
                state = "plain"
                continue
            buf.append(ch)
        else:  # double
            if ch == '"':
                state = "plain"
                continue
            if ch in ("$", "`"):
                saw_unsafe = True
                unsafe_word = True
            buf.append(ch)
    if state != "plain":
        raise TokenizeError("unterminated quote")
    if buf or quoted:
        words.append(_Word("".join(buf), quoted, unsafe_word))
    return words, saw_unsafe


def _segments(words: list[_Word]) -> list[list[_Word]]:
    """Split a word list on shell separators so each segment has its own head word."""
    out: list[list[_Word]] = [[]]
    for w in words:
        if not w.quoted and w.text in ("|", "&", ";", "<", ">", "\n", "\r"):
            out.append([])
            continue
        out[-1].append(w)
    return [seg for seg in out if seg]


def _basename(cmd: str) -> str:
    return cmd.rsplit("/", 1)[-1]


def _has_file_operand(seg: list[_Word]) -> bool:
    """Whether a read-like segment names a file to read (as opposed to reading stdin)."""
    head = _basename(seg[0].text)
    args = [w.text for w in seg[1:]]
    operands = _operands(head, args)
    if head in _SEARCH_CMDS:
        # The first operand of grep is the pattern; a file operand comes after it.
        return len(operands) > 1
    return len(operands) > 0


def _operands(head: str, args: list[str]) -> list[str]:
    """Positional operands, skipping flags and the values that flags consume."""
    value_flags: frozenset[str] = frozenset()
    if head in _LINE_BOUNDED_CMDS:
        value_flags = _HEAD_TAIL_VALUE_FLAGS
    elif head in _SEARCH_CMDS:
        value_flags = _GREP_VALUE_FLAGS
    elif head == "sed":
        value_flags = frozenset({"-e", "-f"})
    operands: list[str] = []
    skip = False
    end_of_flags = False
    for arg in args:
        if skip:
            skip = False
            continue
        if not end_of_flags and arg == "--":
            end_of_flags = True
            continue
        if not end_of_flags and arg.startswith("-") and arg != "-":
            if arg in value_flags:
                skip = True
            continue
        operands.append(arg)
    return operands


def _flags(args: list[str]) -> list[str]:
    """Flag words before the ``--`` end-of-flags marker; ``-`` and ``--`` are not flags."""
    out: list[str] = []
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("-") and arg != "-":
            out.append(arg)
    return out


def _all_absolute(paths: list[str]) -> bool:
    return bool(paths) and all(p.startswith("/") for p in paths)


def _parse_sed_range(args: list[str]) -> tuple[int, int] | None:
    """Only ``sed -n 'A,Bp'`` is accepted; every other script is unprovable."""
    if "-n" not in args:
        return None
    scripts = [a for a in args if not a.startswith("-") and not a.startswith("/")]
    if len(scripts) != 1:
        return None
    script = scripts[0].strip()
    if not script.endswith("p") or "," not in script:
        return None
    lo, _, hi = script[:-1].partition(",")
    if not lo.isdigit() or not hi.isdigit():
        return None
    start, end = int(lo), int(hi)
    if start < 1 or end < start:
        return None
    return start, end - start + 1


def _is_assignment(value: str) -> bool:
    name, separator, _value = value.partition("=")
    return bool(
        separator
        and name
        and (name[0].isalpha() or name[0] == "_")
        and all(ch.isalnum() or ch == "_" for ch in name)
    )


def _unwrap_simple(words: list[_Word]) -> tuple[list[_Word], bool]:
    current = list(words)
    unsafe_wrapper = False
    while current and _is_assignment(current[0].text):
        unsafe_wrapper = True
        current.pop(0)

    while current:
        head = _basename(current[0].text)
        if head == "command":
            current.pop(0)
            if current and current[0].text == "--":
                current.pop(0)
            if current and current[0].text.startswith("-"):
                return current, True
            continue
        if head == "env":
            current.pop(0)
            while current:
                arg = current[0].text
                if arg in ("--", "-i", "--ignore-environment"):
                    current.pop(0)
                    continue
                if _is_assignment(arg):
                    unsafe_wrapper = True
                    current.pop(0)
                    continue
                if arg in ("-u", "--unset"):
                    unsafe_wrapper = True
                    del current[: min(2, len(current))]
                    continue
                if arg.startswith("--unset="):
                    unsafe_wrapper = True
                    current.pop(0)
                    continue
                if arg.startswith("-"):
                    return current, True
                break
            continue
        break
    return current, unsafe_wrapper


def _contains_wrapped_read(words: list[_Word]) -> bool:
    if not words or _basename(words[0].text) not in _SCRIPT_WRAPPERS:
        return False
    for word in words[1:]:
        try:
            nested, _unsafe = _split_words(word.text)
        except TokenizeError:
            nested = [word]
        if any(_basename(candidate.text) in READ_LIKE_CMDS for candidate in nested):
            return True
    return False


def _contains_embedded_read(words: list[_Word]) -> bool:
    """Detect read commands hidden inside command-substitution words."""
    for word in words:
        if not word.unsafe:
            continue
        tokens = re.findall(r"[A-Za-z0-9_./+-]+", word.text)
        if any(_basename(token) in READ_LIKE_CMDS for token in tokens):
            return True
    return False


def _copies_to_stdout(words: list[_Word]) -> bool:
    if not words or _basename(words[0].text) != "cp":
        return False
    operands = _operands("cp", [word.text for word in words[1:]])
    return bool(operands) and operands[-1] in {
        "/dev/stdout",
        "/dev/fd/1",
        "/proc/self/fd/1",
    }


def classify_command(command: str) -> Classification:
    """Classify one shell command string. Never executes anything."""
    if not command or not command.strip():
        return Classification(ShellForm.NOT_READ_LIKE)

    try:
        words, saw_unsafe = _split_words(command)
    except TokenizeError:
        head = _basename(command.strip().split(" ", 1)[0])
        if head in READ_LIKE_CMDS:
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNPARSEABLE")
        return Classification(ShellForm.NOT_READ_LIKE)

    if not words:
        return Classification(ShellForm.NOT_READ_LIKE)

    if saw_unsafe:
        segs = _segments(words)
        redirects_input = any(
            (w.text == "<" and not w.quoted) or (w.unsafe and "<" in w.text) for w in words
        )
        if redirects_input:
            return Classification(ShellForm.UNCLASSIFIABLE, reason="INPUT_REDIRECTION")
        if _contains_embedded_read(words):
            return Classification(ShellForm.UNCLASSIFIABLE, reason="COMMAND_SUBSTITUTION")
        for seg in segs:
            if not seg:
                continue
            normalized, _unsafe_wrapper = _unwrap_simple(seg)
            head = _basename(normalized[0].text) if normalized else ""
            if _copies_to_stdout(normalized):
                return Classification(ShellForm.UNCLASSIFIABLE, reason="STDOUT_FILE_COPY")
            if _contains_wrapped_read(normalized):
                return Classification(ShellForm.UNCLASSIFIABLE, reason="COMMAND_WRAPPER")
            if head not in READ_LIKE_CMDS:
                continue
            if _has_file_operand(normalized) or redirects_input:
                return Classification(ShellForm.UNCLASSIFIABLE, reason="NON_SIMPLE_COMMAND")
        return Classification(ShellForm.NOT_READ_LIKE)

    words, unsafe_wrapper = _unwrap_simple(words)
    head = _basename(words[0].text) if words else ""
    if _copies_to_stdout(words):
        return Classification(ShellForm.UNCLASSIFIABLE, reason="STDOUT_FILE_COPY")
    if _contains_wrapped_read(words):
        return Classification(ShellForm.UNCLASSIFIABLE, reason="COMMAND_WRAPPER")
    if head not in READ_LIKE_CMDS:
        return Classification(ShellForm.NOT_READ_LIKE)
    if unsafe_wrapper:
        return Classification(ShellForm.UNCLASSIFIABLE, reason="UNSAFE_WRAPPER")

    args = [w.text for w in words[1:]]
    flags = _flags(args)
    operands = _operands(head, args)

    # Check flags before the no-operand fast path: ``wc --files0-from=PATH``
    # embeds its input path in the flag and otherwise appeared stdin-only.
    if head in _METADATA_CMDS and any(flag not in _WC_FLAGS for flag in flags):
        return Classification(ShellForm.UNCLASSIFIABLE, reason="UNKNOWN_FLAG")

    if not operands and head not in _SEARCH_CMDS:
        # No file operand: the command reads stdin, so no source is being pulled in.
        return Classification(ShellForm.NOT_READ_LIKE)

    if head in _FULL_READ_CMDS:
        if (head == "cat" and any(f not in _CAT_FLAGS for f in flags)) or (head != "cat" and flags):
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNKNOWN_FLAG")
        if not _all_absolute(operands):
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNRESOLVED_PATH")
        return Classification(ShellForm.FULL_READ, files=tuple(operands))

    if head in _LINE_BOUNDED_CMDS:
        if any(f in _HEAD_TAIL_REJECT_FLAGS or f.startswith("--bytes") for f in flags):
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNSUPPORTED_FLAG")
        bound = _HEAD_TAIL_DEFAULT_LINES
        for i, arg in enumerate(args):
            if arg == "-n":
                if i + 1 >= len(args) or not args[i + 1].isdigit():
                    return Classification(ShellForm.UNCLASSIFIABLE, reason="UNPARSEABLE_COUNT")
                bound = int(args[i + 1])
            elif arg.startswith("-") and arg[1:].isdigit():
                bound = int(arg[1:])
            elif arg.startswith("-") and arg not in ("-",) and not arg[1:].isdigit():
                if arg not in _HEAD_TAIL_VALUE_FLAGS:
                    return Classification(ShellForm.UNCLASSIFIABLE, reason="UNKNOWN_FLAG")
        if not _all_absolute(operands):
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNRESOLVED_PATH")
        return Classification(
            ShellForm.BOUNDED_LINES,
            files=tuple(operands),
            bound_lines=bound,
            line_start=1,
            from_end=head == "tail",
        )

    if head == "sed":
        line_range = _parse_sed_range(args)
        files = [a for a in operands if a.startswith("/")]
        if line_range is None or not files:
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNPROVABLE_SED_SCRIPT")
        start, bound = line_range
        return Classification(
            ShellForm.BOUNDED_LINES,
            files=tuple(files),
            bound_lines=bound,
            line_start=start,
        )

    if head in _SEARCH_CMDS:
        if any(f not in (_GREP_VALUE_FLAGS | _GREP_BOOL_FLAGS) for f in flags):
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNKNOWN_FLAG")
        if any(f in ("-A", "-B", "-C") for f in flags):
            return Classification(ShellForm.UNCLASSIFIABLE, reason="OUTPUT_AMPLIFICATION")
        limit: int | None = None
        for i, arg in enumerate(args):
            if arg in ("-m", "--max-count"):
                if i + 1 >= len(args) or not args[i + 1].isdigit():
                    return Classification(ShellForm.UNCLASSIFIABLE, reason="UNPARSEABLE_COUNT")
                limit = int(args[i + 1])
        files = operands[1:]
        if limit is None or limit < 1:
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNBOUNDED_SEARCH")
        if not files:
            return Classification(ShellForm.NOT_READ_LIKE)
        if not _all_absolute(files):
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNRESOLVED_PATH")
        return Classification(ShellForm.BOUNDED_SEARCH, files=tuple(files), bound_matches=limit)

    if head in _METADATA_CMDS:
        if not _all_absolute(operands):
            return Classification(ShellForm.UNCLASSIFIABLE, reason="UNRESOLVED_PATH")
        fields = 3 if not flags else len(set(flags))
        return Classification(
            ShellForm.BOUNDED_METADATA,
            files=tuple(operands),
            metadata_fields=fields,
        )

    return Classification(ShellForm.UNCLASSIFIABLE, reason="OPAQUE_READ")
