"""A deterministic, in-package port of the incumbent heuristic tool-result compactor.

Source of truth
----------------
Ported, function-for-function on the text-shaping path, from the live plugin at
``oversize-tool-result-compactor`` v0.3.0 (author: Edison; ``transform_tool_result`` on the
operator's own Hermes 0.21.1 host), read read-only over SSH on 2026-09-09 for this port.
The constants below (``_SIGNAL_RE``, sample/repeat/path caps, the secret-value pattern) are
copied verbatim from that source, not reconstructed from memory or from
``evals/shadow/corpus.json``'s reference emulation - that emulation is explicitly documented
as "not product code" and implements a narrower head/tail/elision shape than the real
incumbent, so it was not a safe basis for this port.

What was ported and what was not
---------------------------------
Ported: the signal-line detector (error/exception/failure/timeout/traceback/5xx), bounded
head/tail sampling, consecutive-run and cross-file repeated-line collapsing, JSON structure
listing, "interesting key" extraction (query/time/count/status-shaped fields), representative
JSON string sampling, and inline secret-value redaction (bearer tokens, ``sk-*``, Slack
``xox*`` tokens).

Not ported: artifact-file writing, the per-session manifest (JSON/Markdown), retention
sweeps, and the structured-content/media (image/base64) redaction walk. Those exist in the
incumbent because it is its own capture/persistence layer; this module is pure and has no
filesystem or store access by design. Persistence for context-shunt's callers goes through
the existing :mod:`context_shunt.store` snapshot instead, and its captured sources are
already refused at binary/media content (see ``binaryguard.py``), so the media-redaction walk
has no reachable input here.

Contract
--------
:func:`compact_tool_result` is a pure function: same bytes in, same summary out, no I/O, no
model call, no exception escaping normal input. It is deterministic in the same sense the
incumbent is deterministic - a heuristic shape, not a claim of completeness. Result is always
capped at ``hard_chars`` characters (a legacy_compaction envelope is not a promise the source
fits inside it; ``coverage.omitted`` on the caller's envelope says so).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

#: Ported verbatim from the incumbent (v0.3.0). Case-insensitive; matches a bare
#: error/exception/failure/timeout/traceback word or an HTTP 5xx status marker.
_SIGNAL_RE = re.compile(
    r"\b(error|exception|fail(?:ed|ure|ing)?|timeout|traceback)\b"
    r"|status(?:_code|Code|\s+code)?\s*(?::|=|\s)\s*5\d\d\b"
    r"|\bhttp[/ ]\S*\s+5\d\d\b",
    re.IGNORECASE,
)
#: Ported verbatim. Field names worth surfacing regardless of the signal check.
_INTERESTING_KEY_RE = re.compile(
    r"(query|logql|expr|expression|start|end|from|to|time|timestamp|"
    r"count|total|limit|status|statuscode|status_code|resulttype|datasource)",
    re.IGNORECASE,
)
#: Ported verbatim. Bearer tokens, OpenAI-shaped secret keys, Slack tokens.
_SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{20,}|"
    r"\b(sk-[A-Za-z0-9][A-Za-z0-9_-]{16,})\b|"
    r"\b(xox[baprs]-[A-Za-z0-9-]{20,})\b"
)

_MAX_SIGNAL_LINES = 80
_MAX_SAMPLE_LINES = 24
_MAX_REPEATED_LINES = 24
_MAX_JSON_PATHS = 80
_MAX_JSON_STRINGS = 40
_MAX_LINE_CHARS = 4_000
_DEFAULT_HARD_CHARS = 60_000


def _redact_secret_values(text: str) -> str:
    return _SECRET_VALUE_RE.sub(lambda m: (m.group(1) or "") + "[redacted secret]", text)


def _line_preview(line: str, max_chars: int = _MAX_LINE_CHARS) -> str:
    if len(line) <= max_chars:
        return line
    head = max_chars // 2
    tail = max_chars - head
    return f"{line[:head]}\n[... line truncated from {len(line)} chars ...]\n{line[-tail:]}"


def _numbered_lines(lines: list[tuple[int, str]], max_lines: int) -> list[str]:
    output: list[str] = []
    for line_no, line in lines[:max_lines]:
        output.append(f"L{line_no}: {_line_preview(line)}")
    if len(lines) > max_lines:
        output.append(f"... {len(lines) - max_lines} more matching lines omitted ...")
    return output


def _collapse_consecutive(lines: list[tuple[int, str]]) -> list[str]:
    if not lines:
        return []
    output: list[str] = []
    start_no, current = lines[0]
    repeat = 1
    for line_no, line in lines[1:]:
        if line == current:
            repeat += 1
            continue
        output.append(_format_collapsed_line(start_no, current, repeat))
        start_no, current = line_no, line
        repeat = 1
    output.append(_format_collapsed_line(start_no, current, repeat))
    return output


def _format_collapsed_line(line_no: int, line: str, repeat: int) -> str:
    suffix = f" [repeated {repeat}x]" if repeat > 1 else ""
    return f"L{line_no}: {_line_preview(line)}{suffix}"


def _sample_lines(lines: list[str]) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    indexed = list(enumerate(lines, start=1))
    if len(indexed) <= _MAX_SAMPLE_LINES * 2:
        return indexed, []
    return indexed[:_MAX_SAMPLE_LINES], indexed[-_MAX_SAMPLE_LINES:]


def _top_repeated_lines(lines: list[str]) -> list[str]:
    counts = Counter(line for line in lines if line.strip())
    repeated = [(line, count) for line, count in counts.most_common() if count > 1]
    output = []
    for line, count in repeated[:_MAX_REPEATED_LINES]:
        output.append(f"{count}x: {_line_preview(line)}")
    if len(repeated) > _MAX_REPEATED_LINES:
        output.append(f"... {len(repeated) - _MAX_REPEATED_LINES} more repeated lines omitted ...")
    return output


def _compact_log_text(text: str) -> str:
    lines = text.splitlines()
    signals = [
        (line_no, line) for line_no, line in enumerate(lines, start=1) if _SIGNAL_RE.search(line)
    ]
    first, last = _sample_lines(lines)
    repeated = _top_repeated_lines(lines)

    sections: list[str] = [
        f"Line count: {len(lines)}",
        f"High-signal line count: {len(signals)}",
    ]
    if signals:
        sections.append("")
        sections.append("High-signal exact lines:")
        sections.extend(_numbered_lines(signals, _MAX_SIGNAL_LINES))
    sections.append("")
    sections.append("First sample lines:")
    sections.extend(_collapse_consecutive(first))
    if last:
        sections.append("")
        sections.append("Last sample lines:")
        sections.extend(_collapse_consecutive(last))
    if repeated:
        sections.append("")
        sections.append("Repeated exact lines:")
        sections.extend(repeated)
    return "\n".join(sections)


def _json_type(value: Any) -> str:
    if isinstance(value, dict):
        return f"object({len(value)} keys)"
    if isinstance(value, list):
        return f"array({len(value)} items)"
    if isinstance(value, str):
        return f"string({len(value)} chars)"
    if value is None:
        return "null"
    return type(value).__name__


def _json_scalar(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= 500:
        return encoded
    return encoded[:500] + f"... ({len(encoded)} chars total)"


def _walk_json(value: Any, path: str = "$"):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            safe_key = str(key).replace("\\", "\\\\").replace(".", "\\.")
            yield from _walk_json(child, f"{path}.{safe_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{path}[{index}]")


def _json_structure_lines(parsed: Any) -> list[str]:
    lines: list[str] = [f"Root: {_json_type(parsed)}"]
    count = 0
    for path, value in _walk_json(parsed):
        if path == "$":
            continue
        if not isinstance(value, (dict, list)):
            continue
        lines.append(f"{path}: {_json_type(value)}")
        count += 1
        if count >= _MAX_JSON_PATHS:
            lines.append("... additional JSON containers omitted ...")
            break
    return lines


def _json_interesting_lines(parsed: Any) -> list[str]:
    lines: list[str] = []
    for path, value in _walk_json(parsed):
        tail = path.rsplit(".", 1)[-1]
        if not _INTERESTING_KEY_RE.search(tail):
            continue
        if isinstance(value, (dict, list)):
            lines.append(f"{path}: {_json_type(value)}")
        else:
            lines.append(f"{path}: {_json_scalar(value)}")
        if len(lines) >= _MAX_JSON_PATHS:
            lines.append("... additional query/time/count/status fields omitted ...")
            break
    return lines


def _json_string_samples(parsed: Any) -> tuple[list[str], list[str]]:
    strings: list[str] = []
    signal_strings: list[str] = []
    for path, value in _walk_json(parsed):
        if not isinstance(value, str) or not value:
            continue
        line = f"{path}: {value}"
        if _SIGNAL_RE.search(value):
            signal_strings.append(line)
        elif len(strings) < _MAX_JSON_STRINGS:
            strings.append(line)
        if len(signal_strings) >= _MAX_SIGNAL_LINES and len(strings) >= _MAX_JSON_STRINGS:
            break
    return signal_strings, strings


def _json_representative_snippets(parsed: Any) -> list[str]:
    snippets: list[str] = []
    if isinstance(parsed, list) and parsed:
        snippets.append(
            "First array item: " + json.dumps(parsed[0], ensure_ascii=False, sort_keys=True)[:2_000]
        )
        if len(parsed) > 1:
            snippets.append(
                "Last array item: "
                + json.dumps(parsed[-1], ensure_ascii=False, sort_keys=True)[:2_000]
            )
    elif isinstance(parsed, dict):
        keys = list(parsed.keys())
        snippets.append("Top-level keys: " + ", ".join(str(key) for key in keys[:40]))
        for key in keys[:5]:
            value = parsed[key]
            if isinstance(value, (dict, list)):
                snippets.append(
                    f"$.{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)[:2_000]}"
                )
            else:
                snippets.append(f"$.{key}: {_json_scalar(value)}")
    return snippets


def _compact_json_text(text: str, parsed: Any) -> str:
    sections: list[str] = ["JSON structure:"]
    sections.extend(_json_structure_lines(parsed))

    interesting = _json_interesting_lines(parsed)
    if interesting:
        sections.append("")
        sections.append("Query/time/count/status fields:")
        sections.extend(interesting)

    signal_strings, strings = _json_string_samples(parsed)
    if signal_strings:
        sections.append("")
        sections.append("High-signal exact JSON string values:")
        sections.extend(
            _numbered_lines(list(enumerate(signal_strings, start=1)), _MAX_SIGNAL_LINES)
        )
    if strings:
        sections.append("")
        sections.append("Representative exact JSON string values:")
        sections.extend(_numbered_lines(list(enumerate(strings, start=1)), _MAX_JSON_STRINGS))

    snippets = _json_representative_snippets(parsed)
    if snippets:
        sections.append("")
        sections.append("Representative JSON snippets:")
        sections.extend(_line_preview(snippet, 2_500) for snippet in snippets)

    log_like_strings = "\n".join(
        value for _, value in _walk_json(parsed) if isinstance(value, str) and "\n" in value
    )
    if log_like_strings:
        sections.append("")
        sections.append("Embedded multiline text summary:")
        sections.append(_compact_log_text(log_like_strings))
    elif not signal_strings and not strings:
        sections.append("")
        sections.append("Raw JSON prefix:")
        sections.append(text[:4_000])
    return "\n".join(sections)


def _try_parse_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _cap_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    notice = f"\n\n[Compacted summary truncated to {max_chars} chars. Original source is larger.]"
    keep = max(0, max_chars - len(notice))
    return text[:keep].rstrip() + notice


def compact_tool_result(text: str, *, hard_chars: int = _DEFAULT_HARD_CHARS) -> str:
    """Deterministic compact summary for oversized text or JSON.

    Ported behavior: JSON-shaped input (starts with ``[`` or ``{`` and parses) gets the
    structure/interesting-key/string-sample treatment; everything else gets the signal-line
    plus head/tail/repeated-line treatment. Secret-shaped values are redacted before capping.
    Pure and total: any input, including malformed JSON or a JSON prefix that fails to
    parse, degrades to the log-text path rather than raising.
    """
    redacted = _redact_secret_values(text)
    parsed = _try_parse_json(redacted)
    compacted = (
        _compact_json_text(redacted, parsed) if parsed is not None else _compact_log_text(redacted)
    )
    return _cap_text(compacted, max(1, hard_chars))


__all__ = ["compact_tool_result"]
