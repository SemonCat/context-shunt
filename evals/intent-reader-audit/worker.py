#!/usr/bin/env python3
"""Execute one benchmark lane against the Context Shunt package on ``PYTHONPATH``."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import context_shunt
from context_shunt.capability import CapabilityReport, supported
from context_shunt.config import load as load_config
from context_shunt.legacy_compact import compact_tool_result
from context_shunt.limits import EMITTED_SCHEMA_VERSION, SUPPORTED_REQUEST_VERSIONS
from context_shunt.provider import (
    READER_SYSTEM_PROMPT,
    HostBridgeProvider,
    ModelResponse,
    ProviderTarget,
    build_user_message,
)
from context_shunt.provenance import ModelIdentity, TokenMethod, Usage
from context_shunt.session import ShuntSession

MODEL = "gpt-5.6-luna"
PROVIDER = "benchmark-mock"
MOCK_DELAY_MS = 2.0
EXCERPT_RE = re.compile(
    r"SOURCE EXCERPT \(locator (\{.*?\})\):\n<<<BEGIN EXCERPT\n(.*?)\nEND EXCERPT>>>",
    re.DOTALL,
)


def encoded_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def pad_text(size: int, facts: list[str]) -> str:
    prefix = "\n".join(facts) + "\n"
    if len(prefix.encode()) > size:
        raise ValueError("facts exceed source size")
    unit = "synthetic-padding-row\n"
    remaining = size - len(prefix.encode())
    text = prefix + unit * (remaining // len(unit)) + "x" * (remaining % len(unit))
    assert len(text.encode()) == size
    return text


def loki_text(size: int, records: list[dict[str, Any]]) -> str:
    values = [
        [str(index), json.dumps(row, separators=(",", ":"))]
        for index, row in enumerate(records)
    ]
    value = {
        "data": {"result": [{"stream": {"job": "synthetic"}, "values": values}]},
        "padding": "",
    }
    base = json.dumps(value, separators=(",", ":"))
    padding = size - len(base.encode())
    if padding < 0:
        raise ValueError("records exceed source size")
    value["padding"] = "x" * padding
    text = json.dumps(value, separators=(",", ":"))
    assert len(text.encode()) == size
    return text


def materialize(spec: dict[str, Any]) -> str:
    if spec["kind"] == "loki":
        return loki_text(spec["bytes"], spec["records"])
    return pad_text(spec["bytes"], spec.get("facts", []))


class InstrumentedProvider:
    """Grounded deterministic provider with directly observed payload and usage records."""

    def __init__(self, unknown_calls: list[int]):
        self.unknown_calls = set(unknown_calls)
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next_call = 0

    @property
    def target(self) -> ProviderTarget:
        return ProviderTarget(model=MODEL, provider=PROVIDER)

    def complete(
        self, *, system: str, user: str, max_output_tokens: int, timeout_ms: int
    ):
        with self._lock:
            self._next_call += 1
            call_number = self._next_call
        reply = self._grounded_reply(user)
        input_bytes = len(system.encode()) + len(user.encode())
        output_bytes = len(reply.encode())
        reports_usage = call_number not in self.unknown_calls
        started = time.perf_counter()
        time.sleep(MOCK_DELAY_MS / 1000)
        delay_elapsed_ms = (time.perf_counter() - started) * 1000
        record = {
            "call": call_number,
            "input_payload_bytes": input_bytes,
            "output_payload_bytes": output_bytes,
            "reported_input_tokens": math.ceil(input_bytes / 4)
            if reports_usage
            else None,
            "reported_output_tokens": math.ceil(output_bytes / 4)
            if reports_usage
            else None,
            "reported_cache_tokens": 0 if reports_usage else None,
            "reported_method": "exact_fixture_report" if reports_usage else "unknown",
            "configured_mock_delay_ms": MOCK_DELAY_MS,
            "observed_mock_call_ms": round(delay_elapsed_ms, 6),
            "timeout_ms": timeout_ms,
            "max_output_tokens": max_output_tokens,
        }
        with self._lock:
            self.calls.append(record)
        identity = ModelIdentity(provider=PROVIDER, model=MODEL)
        usage = (
            Usage(
                input_tokens=record["reported_input_tokens"],
                output_tokens=record["reported_output_tokens"],
                cache_tokens=record["reported_cache_tokens"],
                method=TokenMethod.EXACT,
            )
            if reports_usage
            else Usage()
        )
        return ModelResponse(
            text=reply,
            requested=identity,
            resolved=identity,
            reported=identity,
            provider_confirms_generation=True,
            usage=usage,
        )

    @staticmethod
    def _grounded_reply(user: str) -> str:
        found = EXCERPT_RE.search(user)
        if found is None:
            return json.dumps({"claims": [], "citations": []}, separators=(",", ":"))
        locator = json.loads(found.group(1))
        excerpt = found.group(2)
        markers = (
            "merchant_auto_suspend",
            "retry_limit: 7",
            "decision_status:",
            "level",
        )
        marker = next(
            (candidate for candidate in markers if candidate in excerpt), None
        )
        if marker is None:
            return json.dumps({"claims": [], "citations": []}, separators=(",", ":"))
        lines = excerpt.splitlines() or [excerpt]
        relative = next(index for index, line in enumerate(lines) if marker in line)
        line = lines[relative]
        start = line.index(marker)
        quote = line[start : start + min(160, len(line) - start)]
        citation: dict[str, Any] = {"id": "c1", "quote": quote}
        if locator["kind"] == "records":
            ordinal = int(locator["start"]) + relative
            citation.update({"record_start": ordinal, "record_end": ordinal})
        else:
            ordinal = int(locator.get("start", 1)) + relative
            citation.update({"line_start": ordinal, "line_end": ordinal})
        return json.dumps(
            {
                "claims": [{"text": quote, "citation_ids": ["c1"]}],
                "citations": [citation],
            },
            separators=(",", ":"),
        )


class InstrumentedLiveProvider:
    """Real bridge provider retaining only bounded, non-reversible call evidence."""

    def __init__(self) -> None:
        if __import__("os").environ.get("CONTEXT_SHUNT_LUNA_EVAL") != "1":
            raise RuntimeError("live provider requires CONTEXT_SHUNT_LUNA_EVAL=1")
        from bridges import openclaw_inhost

        self._bridge = openclaw_inhost
        self._provider = HostBridgeProvider(self._call, model=MODEL)
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._next_call = 0

    @property
    def target(self) -> ProviderTarget:
        return self._provider.target

    def complete(self, **kwargs: Any) -> ModelResponse:
        return self._provider.complete(**kwargs)

    @staticmethod
    def _sha(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _payload_attestation(
        self, system: str, user: str, max_output_tokens: int, timeout_ms: int
    ) -> dict[str, Any]:
        found = EXCERPT_RE.fullmatch(user.split("\n\nQUESTION: ", 1)[0])
        question_parts = user.split("\n\nQUESTION: ", 1)
        exact_shape = found is not None and len(question_parts) == 2
        locator: dict[str, Any] | None = None
        excerpt = question = ""
        if exact_shape and found is not None:
            try:
                locator = json.loads(found.group(1))
            except (TypeError, ValueError):
                exact_shape = False
            excerpt = found.group(2)
            question = question_parts[1]
        reconstructed = bool(
            exact_shape
            and locator is not None
            and build_user_message(question, excerpt, locator) == user
        )
        parent_canary = __import__("os").environ.get(
            "CONTEXT_SHUNT_EVAL_PARENT_CONTEXT_CANARY",
            "PRIVATE_PARENT_CONTEXT_CANARY_DO_NOT_SEND",
        )
        role_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
        return {
            "roles": ["system", "user"],
            "system_bytes": len(system.encode("utf-8")),
            "user_bytes": len(user.encode("utf-8")),
            "role_content_bytes": role_bytes,
            "system_sha256": self._sha(system),
            "user_sha256": self._sha(user),
            "system_is_exact_reader_contract": system == READER_SYSTEM_PROMPT,
            "user_is_exact_reader_template": reconstructed,
            "user_sections": ["locator", "source_excerpt", "question"]
            if reconstructed
            else [],
            "locator_kind": locator.get("kind")
            if isinstance(locator, dict) and isinstance(locator.get("kind"), str)
            else None,
            "source_excerpt_bytes": len(excerpt.encode("utf-8"))
            if reconstructed
            else None,
            "source_excerpt_sha256": self._sha(excerpt) if reconstructed else None,
            "question_bytes": len(question.encode("utf-8")) if reconstructed else None,
            "question_sha256": self._sha(question) if reconstructed else None,
            "parent_context_canary_absent": bool(parent_canary)
            and parent_canary not in system
            and parent_canary not in user,
            "max_output_tokens": max_output_tokens,
            "timeout_ms": timeout_ms,
        }

    def _call(
        self,
        *,
        system: str,
        user: str,
        provider: str,
        model: str,
        max_output_tokens: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        with self._lock:
            self._next_call += 1
            call_number = self._next_call
            record = {
                "call": call_number,
                "status": "in_flight_usage_unknown",
                "payload": self._payload_attestation(
                    system, user, max_output_tokens, timeout_ms
                ),
                "reported_input_tokens": None,
                "reported_output_tokens": None,
                "reported_cache_tokens": None,
                "reported_method": "unknown",
                "requested_provider": provider or None,
                "requested_model": model,
                "requested_route": __import__("os").environ.get(
                    "CONTEXT_SHUNT_OPENCLAW_ROUTE"
                ),
            }
            self.calls.append(record)
        started = time.perf_counter()
        try:
            result = self._bridge.complete(
                system=system,
                user=user,
                provider=provider,
                model=model,
                max_output_tokens=max_output_tokens,
                timeout_ms=timeout_ms,
            )
        except TimeoutError:
            record.update(
                status="timed_out_usage_unknown",
                elapsed_ms=round((time.perf_counter() - started) * 1000, 6),
            )
            raise
        except Exception as exc:
            record.update(
                status="failed_usage_unknown",
                failure_kind=type(exc).__name__[:64],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 6),
            )
            raise
        record.update(
            status="completed",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 6),
            output_payload_bytes=len(result["text"].encode("utf-8")),
            output_sha256=self._sha(result["text"]),
            transport_input_bytes=result.get("transport_input_bytes"),
            transport_output_bytes=result.get("transport_output_bytes"),
            host_elapsed_ms=result.get("host_elapsed_ms"),
            resolved_provider=result.get("resolved_provider"),
            resolved_model=result.get("resolved_model"),
            route_attested=result.get("route_attested") is True,
            execution_mode=result.get("execution_mode"),
            execution_owner_kind=result.get("execution_owner_kind"),
            execution_owner_id=result.get("execution_owner_id"),
            reported_input_tokens=result.get("input_tokens"),
            reported_output_tokens=result.get("output_tokens"),
            reported_cache_tokens=result.get("cache_tokens"),
            reported_method="provider_report"
            if result.get("usage_exact") is True
            else "unknown",
        )
        return result

    def shutdown(self) -> None:
        self._bridge.shutdown()


def capability() -> CapabilityReport:
    return CapabilityReport(
        adapter="benchmark",
        adapter_version="1",
        host_name="local",
        host_version="1",
        contract_version=EMITTED_SCHEMA_VERSION,
        reader_model=MODEL,
        modes=[supported("tool_result_capture", evidence=("synthetic benchmark",))],
    )


def make_session(
    root: Path, workflow_id: str, provider: Any
) -> ShuntSession:
    workspace = root / "ws"
    workspace.mkdir(parents=True)
    config = load_config(
        {
            "workspace_roots": [str(workspace)],
            "cache_dir": str(root / "cache"),
            "reader": {"automatic_extract": False, "provider": PROVIDER},
            "tool_result_capture": {
                "enabled": True,
                "host_ordering_verified_locally": True,
            },
        },
        default_spill_dir=root / "cache",
    )
    return ShuntSession(workflow_id, config, capability(), provider=provider)


def read_request(
    entries: list[Any], spec: dict[str, Any], request_id: str
) -> dict[str, Any]:
    indexes = spec["sources"] if "sources" in spec else [spec["source"]]
    sources = []
    for index in indexes:
        entry = entries[index]
        selector = (
            {"kind": "search", "patterns": spec["patterns"], "max_matches": 20}
            if spec.get("patterns")
            else {"kind": "all"}
        )
        sources.append(
            {
                "source_id": entry.source_id,
                "snapshot_id": entry.snapshot.snapshot_id,
                "selector": selector,
            }
        )
    return {
        "schema_version": "1.3",
        "request_id": request_id,
        "operation": "read",
        "question": spec["question"],
        "sources": sources,
        "budgets": {
            "max_chunks": spec["max_chunks"],
            "max_answer_bytes": 8192,
            "deadline_ms": 60000,
        },
    }


def aggregate_request(entry: Any, request_id: str) -> dict[str, Any]:
    return {
        "schema_version": "1.3",
        "request_id": request_id,
        "operation": "inspect",
        "source_id": entry.source_id,
        "snapshot_id": entry.snapshot.snapshot_id,
        "selector": {
            "kind": "aggregate",
            "records_pointer": "/data/result",
            "expand_pointer": "/values",
            "record_pointer": "/1",
            "parse_json": True,
            "filter": {"pointer": "/level", "equals": "error"},
            "distinct": ["/trace_id"],
            "group_by": ["/service"],
        },
        "budgets": {"max_result_bytes": 16384, "max_scan_lines": 20000},
    }


def normalized_aggregate(env: dict[str, Any], source: int) -> dict[str, Any] | None:
    if env.get("code") != "EXTRACTED" or "extraction" not in env:
        return None
    value = json.loads(env["extraction"]["segments"][0]["text"])
    return {
        "source": source,
        "matched_count": value["matched_count"],
        "distinct_count": value["distinct"][0]["count"],
        "groups": {str(row["key"][0]): row["count"] for row in value["groups"]},
    }


def evaluate(
    expected: dict[str, Any],
    evidence: list[str],
    aggregates: list[dict[str, Any]],
    answers: list[str] | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    joined = "\n".join(evidence)
    for needle in expected.get("evidence_contains", []):
        checks.append(
            {
                "kind": "evidence_contains",
                "expected": needle,
                "passed": needle in joined,
            }
        )
    joined_answers = "\n".join(answers or [])
    for pattern in expected.get("answer_matches", []):
        checks.append(
            {
                "kind": "answer_matches",
                "expected_regex": pattern,
                "passed": re.search(pattern, joined_answers, re.IGNORECASE) is not None,
            }
        )
    by_source = {row["source"]: row for row in aggregates}
    for wanted in expected.get("aggregates", []):
        observed = by_source.get(wanted["source"])
        checks.append(
            {
                "kind": "exact_aggregate",
                "source": wanted["source"],
                "expected": wanted,
                "observed": observed,
                "passed": observed == wanted,
            }
        )
    return bool(checks) and all(check["passed"] for check in checks), checks


def summarize_reader(
    provider: Any, stats: dict[str, Any]
) -> dict[str, Any]:
    calls = sorted(provider.calls, key=lambda call: call["call"])
    reported = [
        call
        for call in calls
        if call.get("reported_input_tokens") is not None
        and call.get("reported_output_tokens") is not None
    ]
    cache_reported = [
        call for call in reported if call.get("reported_cache_tokens") is not None
    ]
    totals = stats["stats"]["totals"]
    methods = sorted(
        {
            row["reader_token_method"]
            for row in stats["stats"]["records"]
            if row["attempts_started"] > 0
        }
    )
    return {
        "attempts_observed": len(calls),
        "attempts_usage_reported": len(reported),
        "unknown_usage_attempts": len(calls) - len(reported),
        "input_payload_bytes_observed": sum(
            call.get("input_payload_bytes", call.get("payload", {}).get("role_content_bytes", 0))
            for call in calls
        ),
        "output_payload_bytes_observed": sum(
            call.get("output_payload_bytes", 0) for call in calls
        ),
        "input_tokens_reported_lower_bound": (
            sum(call["reported_input_tokens"] for call in reported)
            if reported
            else None
        ),
        "output_tokens_reported_lower_bound": (
            sum(call["reported_output_tokens"] for call in reported)
            if reported
            else None
        ),
        "cache_tokens_reported_lower_bound": (
            sum(call["reported_cache_tokens"] for call in cache_reported)
            if cache_reported
            else None
        ),
        "core_accounted_input_tokens": totals["reader_input_tokens"],
        "core_accounted_output_tokens": totals["reader_output_tokens"],
        "core_accounted_cache_tokens": totals["reader_cache_tokens"],
        "core_reader_token_methods": methods,
        "attempt_statuses": {
            status: sum(1 for call in calls if call.get("status", "completed") == status)
            for status in sorted({call.get("status", "completed") for call in calls})
        },
        "calls": calls,
    }


def execute_shunt(
    workflow: dict[str, Any], lane: str, variant: str, provider_kind: str
) -> dict[str, Any]:
    provider = (
        InstrumentedLiveProvider()
        if provider_kind == "live"
        else InstrumentedProvider(workflow.get("reader", {}).get("unknown_usage_calls", []))
    )
    started = time.perf_counter()
    with TemporaryDirectory(prefix=f"shunt-bench-{lane}-") as temporary:
        session = make_session(Path(temporary), workflow["id"], provider)
        main_outputs: list[Any] = []
        evidence: list[str] = []
        answers: list[str] = []
        aggregates: list[dict[str, Any]] = []
        trace: list[str] = []
        entries = []
        sources = [materialize(spec) for spec in workflow["sources"]]
        for index, source in enumerate(sources):
            outcome = session.post_tool_result(f"spill-{index}", source)
            if outcome is None or outcome.action != "spill" or outcome.envelope is None:
                raise RuntimeError(f"source {index} did not execute the spill route")
            main_outputs.append(outcome.envelope)
            entries.append(
                session.registry.resolve(session.session_id, outcome.source_id)
            )
            trace.append(f"spill:{index}")

        if "reader" in workflow and (
            lane == "pre" or not workflow.get("aggregate_sources")
        ):
            request = read_request(entries, workflow["reader"], "read-1")
            env = session.read(request)
            main_outputs.append(env)
            evidence.append(env.get("answer", ""))
            answers.append(env.get("answer", ""))
            trace.append("read")
            if workflow["reader"].get("repeat"):
                if variant == "no-cache" and lane == "new":
                    session._reader._answer_cache.clear()  # intentional red-check sabotage
                    session._reader._answer_cache_bytes = 0
                request["request_id"] = "read-2"
                repeated = session.read(request)
                main_outputs.append(repeated)
                evidence.append(repeated.get("answer", ""))
                answers.append(repeated.get("answer", ""))
                trace.append("read-repeat")

        if workflow.get("aggregate_sources"):
            if lane == "new" and variant != "no-aggregation":
                for index in workflow["aggregate_sources"]:
                    env = session.inspect(
                        aggregate_request(entries[index], f"aggregate-{index}")
                    )
                    main_outputs.append(env)
                    evidence.extend(
                        segment["text"]
                        for segment in env.get("extraction", {}).get("segments", [])
                    )
                    normalized = normalized_aggregate(env, index)
                    if normalized is not None:
                        aggregates.append(normalized)
                    trace.append(f"aggregate:{index}")
                if workflow.get("reader", {}).get("repeat"):
                    request = read_request(entries, workflow["reader"], "read-1")
                    first = session.read(request)
                    main_outputs.append(first)
                    evidence.append(first.get("answer", ""))
                    answers.append(first.get("answer", ""))
                    trace.append("read")
                    if variant == "no-cache":
                        session._reader._answer_cache.clear()
                        session._reader._answer_cache_bytes = 0
                    request["request_id"] = "read-2"
                    second = session.read(request)
                    main_outputs.append(second)
                    evidence.append(second.get("answer", ""))
                    answers.append(second.get("answer", ""))
                    trace.append("read-repeat")
            elif lane == "new":
                fallback_spec = dict(workflow["reader"])
                fallback_spec["sources"] = workflow["aggregate_sources"]
                fallback_spec.pop("source", None)
                fallback_spec.pop("patterns", None)
                fallback_spec["question"] = (
                    "Return the exact error count, distinct trace IDs, and counts grouped by service."
                )
                env = session.read(
                    read_request(entries, fallback_spec, "aggregate-bypassed")
                )
                main_outputs.append(env)
                evidence.append(env.get("answer", ""))
                answers.append(env.get("answer", ""))
                trace.append("read-instead-of-aggregate")

        full_read_bytes = 0
        if lane == "pre" and "pre_full_read_source" in workflow:
            index = workflow["pre_full_read_source"]
            entry = entries[index]
            for page, (start, end) in enumerate(
                ((0, 10000), (10000, workflow["sources"][index]["bytes"]))
            ):
                env = session.inspect(
                    {
                        "schema_version": "1.2",
                        "request_id": f"full-read-{page}",
                        "operation": "inspect",
                        "source_id": entry.source_id,
                        "snapshot_id": entry.snapshot.snapshot_id,
                        "selector": {"kind": "bytes", "start": start, "end": end},
                        "budgets": {"max_result_bytes": 10000, "max_scan_lines": 20000},
                    }
                )
                main_outputs.append(env)
                text = "".join(
                    segment["text"] for segment in env["extraction"]["segments"]
                )
                evidence.append(text)
                full_read_bytes += len(text.encode())
                trace.append(f"inspect-full:{page}")
        elif lane == "new" and "new_search" in workflow:
            search = workflow["new_search"]
            entry = entries[search["source"]]
            env = session.inspect(
                {
                    "schema_version": "1.3",
                    "request_id": "selected-search",
                    "operation": "inspect",
                    "source_id": entry.source_id,
                    "snapshot_id": entry.snapshot.snapshot_id,
                    "selector": {
                        "kind": "search",
                        "needle": search["needle"],
                        "max_matches": 10,
                        "context_lines": 0,
                    },
                    "budgets": {"max_result_bytes": 4096, "max_scan_lines": 20000},
                }
            )
            main_outputs.append(env)
            evidence.extend(
                segment["text"] for segment in env["extraction"]["segments"]
            )
            trace.append("inspect-selected")

        requery_bytes = 0
        for index, spec in enumerate(workflow.get("requery_results", [])):
            result = pad_text(spec["bytes"], spec["facts"])
            outcome = session.post_tool_result(f"requery-{index}", result)
            if (
                outcome is None
                or outcome.action != "passthrough"
                or outcome.envelope is not None
            ):
                raise RuntimeError("bounded requery did not execute passthrough")
            main_outputs.append(result)
            evidence.append(result)
            requery_bytes += len(result.encode())
            trace.append(f"requery-passthrough:{index}")

        stats = session.stats(
            {
                "schema_version": "1.2",
                "request_id": "stats",
                "operation": "stats",
                "page_size": 8,
            }
        )
        correct, checks = evaluate(workflow["expected"], evidence, aggregates, answers)
        cache_hits = sum(
            1
            for value in main_outputs
            if isinstance(value, dict)
            and value.get("provenance", {}).get("cache_reused") is True
        )
        citation_values = [
            citation.get("verified")
            for value in main_outputs
            if isinstance(value, dict)
            for citation in value.get("citations", [])
        ]
        coverage = [
            {
                "status": value.get("status"),
                "code": value.get("code"),
                "complete": value.get("coverage", {}).get("complete"),
                "omission_reasons": value.get("coverage", {}).get(
                    "omission_reasons", []
                ),
            }
            for value in main_outputs
            if isinstance(value, dict) and "coverage" in value
        ]
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "workflow": workflow["id"],
            "lane": lane,
            "variant": variant,
            "execution": "actual_shunt_session",
            "operation_trace": trace,
            "module_origin": str(Path(context_shunt.__file__).resolve()),
            "core_emitted_envelope_version": EMITTED_SCHEMA_VERSION,
            "core_supported_request_versions": sorted(SUPPORTED_REQUEST_VERSIONS),
            "source_sha256": [
                hashlib.sha256(source.encode()).hexdigest() for source in sources
            ],
            "main_context_bytes_observed": sum(
                encoded_bytes(value) if isinstance(value, dict) else len(value.encode())
                for value in main_outputs
            ),
            "reader": summarize_reader(provider, stats),
            "answer_cache_hits_observed": cache_hits,
            "requery_bytes_observed": requery_bytes,
            "full_read_bytes_observed": full_read_bytes,
            "correct": correct,
            "correctness_checks": checks,
            "citation_validity": all(citation_values) if citation_values else None,
            "coverage_observed": coverage,
            "harness_elapsed_ms_observed": round(elapsed_ms, 6),
            "mock_delay_ms_configured_total": (
                len(provider.calls) * MOCK_DELAY_MS if provider_kind == "mock" else 0.0
            ),
            "mock_delay_ms_observed_total": round(
                sum(call.get("observed_mock_call_ms", 0.0) for call in provider.calls), 6
            ),
        }


def execute_legacy(workflow: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    sources = [materialize(spec) for spec in workflow["sources"]]
    outputs = [compact_tool_result(source, hard_chars=16000) for source in sources]
    evidence = list(outputs)
    trace = [f"legacy-compact:{index}" for index in range(len(sources))]
    requery_bytes = 0
    for index, spec in enumerate(workflow.get("requery_results", [])):
        result = pad_text(spec["bytes"], spec["facts"])
        outputs.append(result)
        evidence.append(result)
        requery_bytes += len(result.encode())
        trace.append(f"requery-passthrough:{index}")
    full_read_bytes = 0
    if "pre_full_read_source" in workflow:
        raw = sources[workflow["pre_full_read_source"]]
        outputs.append(raw)
        evidence.append(raw)
        full_read_bytes = len(raw.encode())
        trace.append("legacy-full-read")
    correct, checks = evaluate(workflow["expected"], evidence, [])
    return {
        "workflow": workflow["id"],
        "lane": "legacy_compactor",
        "variant": "normal",
        "execution": "actual_incumbent_compactor_port",
        "operation_trace": trace,
        "module_origin": str(Path(context_shunt.__file__).resolve()),
        "core_emitted_envelope_version": None,
        "core_supported_request_versions": None,
        "source_sha256": [
            hashlib.sha256(source.encode()).hexdigest() for source in sources
        ],
        "main_context_bytes_observed": sum(len(value.encode()) for value in outputs),
        "reader": {
            "attempts_observed": 0,
            "attempts_usage_reported": 0,
            "unknown_usage_attempts": 0,
            "input_payload_bytes_observed": None,
            "output_payload_bytes_observed": None,
            "input_tokens_reported_lower_bound": None,
            "output_tokens_reported_lower_bound": None,
            "cache_tokens_reported_lower_bound": None,
            "core_accounted_input_tokens": None,
            "core_accounted_output_tokens": None,
            "core_accounted_cache_tokens": None,
            "core_reader_token_methods": [],
            "calls": [],
        },
        "answer_cache_hits_observed": 0,
        "requery_bytes_observed": requery_bytes,
        "full_read_bytes_observed": full_read_bytes,
        "correct": correct,
        "correctness_checks": checks,
        "citation_validity": None,
        "harness_elapsed_ms_observed": round((time.perf_counter() - started) * 1000, 6),
        "mock_delay_ms_configured_total": 0.0,
        "mock_delay_ms_observed_total": 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lane", choices=("legacy_compactor", "pre", "new"), required=True
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument(
        "--variant", choices=("normal", "no-cache", "no-aggregation"), default="normal"
    )
    parser.add_argument("--provider-kind", choices=("mock", "live"), default="mock")
    args = parser.parse_args()
    corpus = json.loads(args.corpus.read_text())
    started = time.perf_counter()
    try:
        rows = [
            execute_legacy(workflow)
            if args.lane == "legacy_compactor"
            else execute_shunt(
                workflow, args.lane, args.variant, args.provider_kind
            )
            for workflow in corpus["workflows"]
        ]
        print(
            json.dumps(
                {
                    "lane": args.lane,
                    "variant": args.variant,
                    "provider_kind": args.provider_kind,
                    "lane_elapsed_ms_observed": round(
                        (time.perf_counter() - started) * 1000, 6
                    ),
                    "rows": rows,
                },
                sort_keys=True,
            )
        )
    finally:
        if args.provider_kind == "live" and args.lane != "legacy_compactor":
            from bridges import openclaw_inhost

            openclaw_inhost.shutdown()


if __name__ == "__main__":
    main()
