"""unit citations: ``render_excerpt`` gutters a "lines" chunk with authoritative numbers.

Companion to ``test_gate_reader_line_gutters.py``, which proves the effect end to end
against a real corpus item. These are the narrower, direct unit tests of
``context_shunt.chunking.render_excerpt`` itself: exact gutter numbering (including a
blank line), records passthrough, and the LF-only line-break contract shared with
``LineIndex`` (``textindex.py``) - a bare CR, U+2028 (LINE SEPARATOR) and U+0085 (NEXT
LINE) are not physical line breaks here and must not split into their own gutter row.
"""

from __future__ import annotations

import pytest

from context_shunt.chunking import Chunk, render_excerpt

pytestmark = pytest.mark.gate_citations


def _chunk(text: str, start: int, end: int) -> Chunk:
    return Chunk(
        source_id="src",
        snapshot_id="snap",
        locator={"kind": "lines", "start": start, "end": end},
        text=text,
        bytes_len=len(text.encode("utf-8")),
        est_tokens=1,
    )


def test_gutters_every_physical_line_with_its_global_number():
    chunk = _chunk("alpha\nbeta\ngamma", start=10, end=12)
    assert render_excerpt(chunk) == "10: alpha\n11: beta\n12: gamma"


def test_a_blank_physical_line_still_gets_a_gutter():
    """The exact shape of the live bug: a blank line immediately before the cited fact,
    with nothing between its gutter and the newline."""
    chunk = _chunk("note 1199\n\n threshold_0 = 100", start=1200, end=1202)
    rendered = render_excerpt(chunk)
    assert rendered == "1200: note 1199\n1201: \n1202:  threshold_0 = 100"
    # The line actually holding the fact is unambiguous from the gutter alone.
    (line,) = [row for row in rendered.split("\n") if "threshold_0 = 100" in row]
    assert line.startswith("1202: ")


def test_single_line_fragment_from_an_oversize_line_split_keeps_one_gutter():
    """``_split_utf8`` can hand out several ``Chunk``s for one over-budget physical line,
    each with ``start == end``. Every fragment is still (a piece of) that one line, so it
    gets exactly that line's gutter, not a fabricated run of numbers."""
    chunk = _chunk("only-a-fragment-of-one-long-line", start=42, end=42)
    assert render_excerpt(chunk) == "42: only-a-fragment-of-one-long-line"


def test_records_locator_is_untouched():
    chunk = Chunk(
        source_id="src",
        snapshot_id="snap",
        locator={"kind": "records", "pointer": "", "start": 1, "end": 2},
        text='{"a":1}\n{"a":2}',
        bytes_len=15,
        est_tokens=1,
    )
    assert render_excerpt(chunk) == chunk.text


@pytest.mark.parametrize(
    "separator",
    ["\r", " ", "\u0085"],
    ids=["bare-cr", "u2028-line-separator", "u0085-next-line"],
)
def test_non_lf_unicode_separators_do_not_split_a_gutter_line(separator: str):
    """Matches ``LineIndex`` (``textindex.py``): physical lines are LF-only. A bare CR
    (CRLF keeps its CR as part of the line), U+2028 or U+0085 embedded in a line's text
    must stay on that line's single gutter row, never gain a gutter of their own the way
    ``str.splitlines()`` would give them."""
    text = f"before{separator}after"
    chunk = _chunk(text, start=5, end=5)
    assert render_excerpt(chunk) == f"5: {text}"


def test_two_lines_with_a_crlf_first_line_keeps_the_cr_after_its_gutter():
    chunk = _chunk("first\r\nsecond", start=1, end=2)
    assert render_excerpt(chunk) == "1: first\r\n2: second"
