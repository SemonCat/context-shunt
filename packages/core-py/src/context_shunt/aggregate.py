"""Bounded deterministic count/distinct/grouping for validated JSON snapshots."""

from __future__ import annotations

import json
from typing import Any

from .errors import ShuntError
from .inspect import Extraction, Segment, escaped_json_cost
from .limits import Limits
from .snapshot import Snapshot, canonical_json, json_depth_and_nodes, resolve_pointer

_MAX_RETURNED_VALUES = 200
_MAX_RETURNED_GROUPS = 200
_MAX_SCALAR_BYTES = 512
_MISSING = object()
_MISSING_OUTPUT = {"missing": True}


def _optional_pointer(value: Any, pointer: str) -> Any:
    try:
        return resolve_pointer(value, pointer)
    except ShuntError as exc:
        if exc.detail == "POINTER_NOT_FOUND":
            return _MISSING
        raise


def _scalar(value: Any) -> Any:
    if value is _MISSING or value is None or isinstance(value, (str, bool)):
        return value
    raise ShuntError("INVALID_REQUEST", "BAD_SELECTOR", retryable=False)


def _output_scalar(value: Any) -> bool:
    return len(canonical_json(value).encode("utf-8")) <= _MAX_SCALAR_BYTES


def aggregate_snapshot(
    snapshot: Snapshot,
    selector: dict[str, Any],
    *,
    max_result_bytes: int,
    max_wire_bytes: int,
    max_records: int,
    limits: Limits,
) -> Extraction:
    """Aggregate in one bounded pass, including Loki-style nested JSON log records."""
    root = snapshot.json_value
    if root is None:
        if snapshot.media_type != "text/plain":
            raise ShuntError("INVALID_REQUEST", "BAD_SELECTOR", retryable=False)
        try:
            root = json.loads(snapshot.data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ShuntError("INVALID_REQUEST", "BAD_JSON", retryable=False) from None
        json_depth_and_nodes(root, limits)
    outer = resolve_pointer(root, selector["records_pointer"])
    if not isinstance(outer, list):
        raise ShuntError("INVALID_REQUEST", "BAD_SELECTOR", retryable=False)

    records: list[Any] = []
    work_units = 0
    for item in outer:
        work_units += 1
        if work_units > max_records:
            raise ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", retryable=False)
        expanded = (
            [item]
            if "expand_pointer" not in selector
            else resolve_pointer(item, selector["expand_pointer"])
        )
        if not isinstance(expanded, list):
            raise ShuntError("INVALID_REQUEST", "BAD_SELECTOR", retryable=False)
        if "expand_pointer" in selector:
            work_units += len(expanded)
        if work_units > max_records:
            raise ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", retryable=False)
        records.extend(expanded)

    distinct_paths = selector.get("distinct", [])
    group_paths = selector.get("group_by", [])
    distinct: dict[str, dict[str, Any]] = {path: {} for path in distinct_paths}
    groups: dict[str, dict[str, Any]] = {}
    matched = 0
    parsed_bytes = 0
    parsed_nodes = 0
    filter_spec = selector.get("filter")
    filter_expected_key: str | None = None
    if filter_spec is not None and "equals" in filter_spec:
        expected = _scalar(filter_spec["equals"])
        if not _output_scalar(expected):
            raise ShuntError("INVALID_REQUEST", "BAD_SELECTOR", retryable=False)
        filter_expected_key = canonical_json(expected)

    for raw in records:
        record = (
            raw
            if "record_pointer" not in selector
            else resolve_pointer(raw, selector["record_pointer"])
        )
        if selector.get("parse_json"):
            if not isinstance(record, str):
                raise ShuntError("INVALID_REQUEST", "BAD_SELECTOR", retryable=False)
            parsed_bytes += len(record.encode("utf-8"))
            if parsed_bytes > limits.max_source_bytes:
                raise ShuntError("LIMIT_EXCEEDED", "RESULT_OVER_SOURCE_CAP", retryable=False)
            try:
                record = json.loads(record)
            except (ValueError, RecursionError):
                raise ShuntError("INVALID_REQUEST", "BAD_JSON", retryable=False) from None
            _, nodes = json_depth_and_nodes(record, limits)
            parsed_nodes += nodes
            if parsed_nodes > limits.json_max_nodes:
                raise ShuntError("LIMIT_EXCEEDED", "JSON_TOO_MANY_NODES", retryable=False)

        if filter_spec is not None:
            candidate = _optional_pointer(record, filter_spec["pointer"])
            if candidate is _MISSING:
                continue
            if "equals" in filter_spec:
                if not (candidate is None or isinstance(candidate, (str, bool))):
                    continue
                candidate = _scalar(candidate)
                if (
                    not _output_scalar(candidate)
                    or canonical_json(candidate) != filter_expected_key
                ):
                    continue
            elif not isinstance(candidate, str) or filter_spec["contains"] not in candidate:
                continue
        matched += 1

        for path in distinct_paths:
            value = _scalar(_optional_pointer(record, path))
            if value is not _MISSING:
                distinct[path][canonical_json(value)] = value
        if group_paths:
            key = []
            for path in group_paths:
                value = _scalar(_optional_pointer(record, path))
                key.append(_MISSING_OUTPUT if value is _MISSING else value)
            encoded = canonical_json(key)
            if encoded in groups:
                groups[encoded]["count"] += 1
            else:
                groups[encoded] = {"key": key, "count": 1}

    distinct_rows: list[dict[str, Any]] = []
    for path in distinct_paths:
        all_values = sorted(distinct[path].items(), key=lambda item: item[0].encode("utf-8"))
        values = [value for _, value in all_values if _output_scalar(value)][:_MAX_RETURNED_VALUES]
        distinct_rows.append(
            {
                "path": path,
                "count": len(all_values),
                "values": values,
                "values_complete": len(values) == len(all_values),
            }
        )
    all_groups = sorted(groups.items(), key=lambda item: item[0].encode("utf-8"))
    group_rows = [
        row for _, row in all_groups if all(_output_scalar(value) for value in row["key"])
    ][:_MAX_RETURNED_GROUPS]
    groups_complete = len(group_rows) == len(all_groups)

    def build() -> str:
        return canonical_json(
            {
                "distinct": distinct_rows,
                "group_by": group_paths,
                "group_count": len(all_groups),
                "groups": group_rows,
                "groups_complete": groups_complete,
                "matched_count": matched,
                "records_scanned": len(records),
                "schema": "context_shunt.aggregate.v1",
            }
        )

    text = build()

    def fits() -> bool:
        return len(text.encode("utf-8")) <= max_result_bytes and (
            Segment.wire_overhead("aggregate", 0, len(records)) + escaped_json_cost(text)
            <= max_wire_bytes
        )

    while not fits() and group_rows:
        group_rows.pop()
        groups_complete = False
        text = build()
    while not fits() and any(row["values"] for row in distinct_rows):
        row = next(row for row in reversed(distinct_rows) if row["values"])
        row["values"].pop()
        row["values_complete"] = False
        text = build()
    if not fits():
        raise ShuntError("LIMIT_EXCEEDED", "UNIT_OVER_PAGE_BUDGET", retryable=False)

    return Extraction(
        mode="aggregate",
        segments=[Segment(kind="aggregate", start=0, end=len(records), text=text)],
        result_bytes=len(text.encode("utf-8")),
        complete=True,
        # Aggregate traverses parsed records, not the text line index.
        lines_scanned=0,
        records_scanned=len(records),
        records_matched=matched,
    )


__all__ = ["aggregate_snapshot"]
