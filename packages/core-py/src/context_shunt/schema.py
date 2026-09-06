"""Contract validation.

The JSON Schemas are the cross-language boundary; this module is the Python side of it.
Schema length checks count UTF-16 code units, so a byte guard is applied on top for the
fields where the contract is stated in bytes.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from .errors import ShuntError
from .limits import CONTRACTS_DIR, DEFAULT_LIMITS, SCHEMA_VERSION


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    with (CONTRACTS_DIR / name).open("rb") as fh:
        return Draft202012Validator(json.load(fh))


def request_validator() -> Draft202012Validator:
    return _validator("request.schema.json")


def envelope_validator() -> Draft202012Validator:
    return _validator("envelope.schema.json")


def validate_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ShuntError("INVALID_REQUEST", "NOT_OBJECT", retryable=False)
    version = request.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ShuntError("UNSUPPORTED_VERSION", "BAD_SCHEMA_VERSION", retryable=False)
    if request.get("operation") == "propose_patch":
        # Reserved for a future writer contract. v1 refuses it as an unsupported
        # operation rather than treating it as an unknown enum value.
        raise ShuntError("INVALID_REQUEST", "WRITER_OPERATION_UNSUPPORTED", retryable=False)
    if request_validator().is_valid(request):
        _byte_guards(request)
        return request
    raise ShuntError("INVALID_REQUEST", "SCHEMA_VIOLATION", retryable=False)


def _byte_guards(request: dict[str, Any]) -> None:
    question = request.get("question", "")
    if len(question.encode("utf-8")) > DEFAULT_LIMITS.max_question_bytes:
        raise ShuntError("INVALID_REQUEST", "QUESTION_OVER_BYTE_CAP", retryable=False)
    if not question.strip():
        raise ShuntError("INVALID_REQUEST", "EMPTY_QUESTION", retryable=False)


def validate_envelope(envelope: Any) -> bool:
    return envelope_validator().is_valid(envelope)


def envelope_errors(envelope: Any) -> list[str]:
    return [e.message for e in envelope_validator().iter_errors(envelope)]
