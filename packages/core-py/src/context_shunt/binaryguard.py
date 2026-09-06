"""Binary and encoding detection.

Extension is not evidence. Content is sniffed for NUL bytes and control-character
density, then strictly decoded as UTF-8. v1 refuses binary, unknown encodings and
non-text MCP blocks rather than guessing, and never base64-expands or OCRs anything.
"""

from __future__ import annotations

from .errors import ShuntError

_TEXT_CONTROL_ALLOWLIST = frozenset({0x09, 0x0A, 0x0D, 0x0C, 0x1B})
_CONTROL_DENSITY_LIMIT = 0.30
_BINARY_MAGICS = (
    b"\x7fELF", b"\x89PNG\r\n\x1a\n", b"GIF8", b"\xff\xd8\xff", b"%PDF-",
    b"PK\x03\x04", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"OggS", b"RIFF",
    b"\xca\xfe\xba\xbe", b"MZ",
)

TEXT_MEDIA_TYPE = "text/plain"
JSON_MEDIA_TYPE = "application/json"


def looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if any(sample.startswith(magic) for magic in _BINARY_MAGICS):
        return True
    if b"\x00" in sample:
        return True
    control = sum(
        1 for b in sample if b < 0x20 and b not in _TEXT_CONTROL_ALLOWLIST
    )
    return control / len(sample) > _CONTROL_DENSITY_LIMIT


def assert_text(data: bytes) -> str:
    """Return decoded text or raise ``BINARY_UNSUPPORTED``. No lossy decoding."""
    if looks_binary(data[:8192]):
        raise ShuntError("BINARY_UNSUPPORTED", "BINARY_CONTENT")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ShuntError("BINARY_UNSUPPORTED", "INVALID_ENCODING") from None


SAFE_BLOCK_TYPES = frozenset({"text"})


def assert_supported_blocks(blocks: list[dict]) -> None:
    """A mixed result containing one unsupported block rejects the whole result."""
    for block in blocks:
        if not isinstance(block, dict):
            raise ShuntError("BINARY_UNSUPPORTED", "UNKNOWN_BLOCK")
        if str(block.get("type", "")) not in SAFE_BLOCK_TYPES:
            raise ShuntError("BINARY_UNSUPPORTED", "UNSUPPORTED_BLOCK")
