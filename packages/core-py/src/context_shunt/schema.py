"""Contract validation.

The JSON Schemas are the cross-language boundary; this module is the Python side of it.
Schema length checks count UTF-16 code units, so a byte guard is applied on top for the
fields where the contract is stated in bytes.

Version handling is deliberately strict in both directions:

* a request declaring a revision this core does not support is ``UNSUPPORTED_VERSION``;
* a request declaring 1.0 while carrying a 1.1 field, or using a 1.1 operation, is
  ``INVALID_REQUEST`` - an unknown mandatory field is refused, never ignored;
* ``propose_patch`` stays reserved and refused.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator

from .errors import ShuntError
from .limits import (
    CONTRACTS_DIR,
    DEFAULT_LIMITS,
    V11_ONLY_OPERATIONS,
    V11_ONLY_REQUEST_FIELDS,
    supported_request_version,
)

READ_OPERATIONS = frozenset({"read"})
ALL_OPERATIONS = frozenset({"read", "inspect", "stats"})


@cache
def _validator(name: str) -> Draft202012Validator:
    with (CONTRACTS_DIR / name).open("rb") as fh:
        return Draft202012Validator(json.load(fh))


def request_validator() -> Draft202012Validator:
    return _validator("request.schema.json")


def envelope_validator() -> Draft202012Validator:
    return _validator("envelope.schema.json")


def tool_args_validator() -> Draft202012Validator:
    return _validator("tool-args.schema.json")


def validate_request(
    request: Any, *, operations: frozenset[str] = READ_OPERATIONS
) -> dict[str, Any]:
    """Validate one core request.

    ``operations`` is the set this call site accepts, so the reader cannot be handed a
    stats request and the stats path cannot be handed a question.
    """
    if not isinstance(request, dict):
        raise ShuntError("INVALID_REQUEST", "NOT_OBJECT", retryable=False)
    version = request.get("schema_version")
    if not supported_request_version(version):
        raise ShuntError("UNSUPPORTED_VERSION", "BAD_SCHEMA_VERSION", retryable=False)
    operation = request.get("operation")
    if operation == "propose_patch":
        # Reserved for a future writer contract. v1 refuses it as an unsupported
        # operation rather than treating it as an unknown enum value.
        raise ShuntError("INVALID_REQUEST", "WRITER_OPERATION_UNSUPPORTED", retryable=False)
    if operation not in ALL_OPERATIONS:
        raise ShuntError("INVALID_REQUEST", "UNKNOWN_OPERATION", retryable=False)
    if operation not in operations:
        raise ShuntError("INVALID_REQUEST", "OPERATION_NOT_ACCEPTED_HERE", retryable=False)
    if version == "1.0":
        if operation in V11_ONLY_OPERATIONS:
            raise ShuntError("INVALID_REQUEST", "OPERATION_REQUIRES_1_1", retryable=False)
        if set(request) & V11_ONLY_REQUEST_FIELDS:
            raise ShuntError("INVALID_REQUEST", "FIELD_REQUIRES_1_1", retryable=False)
    if request_validator().is_valid(request):
        _byte_guards(request)
        return request
    raise ShuntError("INVALID_REQUEST", "SCHEMA_VIOLATION", retryable=False)


def validate_tool_args(args: Any) -> dict[str, Any]:
    """Validate the arguments an agent passed to one of the three escape-hatch tools."""
    if not isinstance(args, dict):
        raise ShuntError("INVALID_REQUEST", "NOT_OBJECT", retryable=False)
    if not tool_args_validator().is_valid(args):
        raise ShuntError("INVALID_REQUEST", "TOOL_ARGS_VIOLATION", retryable=False)
    question = args.get("question")
    if isinstance(question, str):
        _assert_question(question)
    return args


def _byte_guards(request: dict[str, Any]) -> None:
    if request.get("operation") != "read":
        return
    _assert_question(request.get("question", ""))


def _assert_question(question: str) -> None:
    if len(question.encode("utf-8")) > DEFAULT_LIMITS.max_question_bytes:
        raise ShuntError("INVALID_REQUEST", "QUESTION_OVER_BYTE_CAP", retryable=False)
    if not question.strip():
        raise ShuntError("INVALID_REQUEST", "EMPTY_QUESTION", retryable=False)


def validate_envelope(envelope: Any) -> bool:
    return envelope_validator().is_valid(envelope)


def envelope_errors(envelope: Any) -> list[str]:
    return [e.message for e in envelope_validator().iter_errors(envelope)]
