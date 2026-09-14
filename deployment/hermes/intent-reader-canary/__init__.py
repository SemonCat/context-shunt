"""One-shot, PID-bound production canary for intent-reader aggregation and reuse.

The canary uses only synthetic data.  Its receipt contains hashes and measurements, never
the question, source excerpts, prompts, exception messages, or provider credentials.  It
registers no transform listener and disposes its temporary fixture tool in ``finally``.
"""

from __future__ import annotations

import contextvars
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
import time


CANARY_TOOL = "context_shunt_intent_reader_canary"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _core_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _claim_once(path: Path):
    """Hold an atomic one-shot lease that the kernel releases if the worker dies."""
    claim = path.open("a+")
    try:
        fcntl.flock(claim.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        claim.close()
        return None
    claim.seek(0)
    claim.truncate()
    json.dump({"pid": os.getpid()}, claim)
    claim.flush()
    os.fsync(claim.fileno())
    return claim


def _receipt_matches(path: Path, settings: dict) -> bool:
    try:
        receipt = json.loads(path.read_text())
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(receipt, dict):
        return False
    return (
        receipt.get("transaction_id") == settings.get("transaction_id")
        and receipt.get("commit") == settings.get("commit")
        and receipt.get("pid") == settings.get("pid")
    )


def register(ctx):
    base = Path(__file__).resolve().parent
    if "gateway" not in __import__("sys").argv:
        return
    if not __debug__:
        raise RuntimeError("optimized Python is unsupported for the intent-reader canary")

    def worker():
        deadline = time.monotonic() + 600
        trigger = base / "trigger.json"
        candidate = None
        while time.monotonic() < deadline:
            try:
                candidate = json.loads(trigger.read_text())
                if isinstance(candidate, dict) and candidate.get("pid") == os.getpid():
                    break
            except (OSError, TypeError, ValueError):
                pass
            time.sleep(1)
        else:
            return

        claim = _claim_once(base / "claimed.json")
        if claim is None:
            return
        assert candidate is not None
        trigger_transaction = str(candidate.get("transaction_id") or "")[:128]
        trigger_commit = str(candidate.get("commit") or "")[:64]
        trigger_identity = {
            "transaction_id": trigger_transaction,
            "commit": trigger_commit,
            "pid": os.getpid(),
        }
        if _receipt_matches(base / "done.json", trigger_identity):
            claim.close()
            return

        report = {
            "schema": "context-shunt.hermes-intent-reader-canary.v1",
            "result": "FAIL",
            "pid": os.getpid(),
            "commit": trigger_commit,
            "transaction_id": trigger_transaction,
        }
        registration = None
        original_provider = None
        session = None
        adapter = None
        session_id = None
        release_session_id = None
        stage = "bootstrap"
        try:
            stage = "settings"
            settings = json.loads((base / "settings.json").read_text())
            assert trigger_transaction and trigger_commit
            assert trigger_transaction == settings.get("transaction_id")
            assert trigger_commit == settings.get("commit")
            stage = "imports"
            import context_shunt
            import model_tools
            from context_shunt.guard import enforce

            stage = "runtime_binding"
            assert _core_tree_sha256(Path(context_shunt.__file__).resolve().parent) == settings[
                "core_tree_sha256"
            ]
            listeners = ctx._manager._hooks.get("transform_tool_result", [])
            assert len(listeners) == 1
            adapter = listeners[0].__globals__
            adapter_path = Path(adapter["__file__"]).resolve()
            assert adapter_path == Path(settings["adapter_path"])
            assert _sha256(adapter_path) == settings["adapter_sha256"]
            assert CANARY_TOOL in adapter["_capture_tool_allowlist"]
            assert context_shunt.EMITTED_SCHEMA_VERSION == "1.3"

            transaction = settings["transaction_id"]
            namespace = hashlib.sha256(transaction.encode("utf-8")).hexdigest()[:32]
            parent_context_marker = "parent-context-" + namespace
            session_id = "intent-reader-canary-" + parent_context_marker
            release_session_id = "release-canary-" + namespace
            session = adapter["_session"](session_id, session_id)
            assert session._store.operation_count(session.identity) == 0

            private_marker = hashlib.sha256((transaction + ":private").encode()).hexdigest()
            unselected_marker = hashlib.sha256((transaction + ":unselected").encode()).hexdigest()
            body_rows = ["intent_probe = 7319\n"]
            body_rows.extend("synthetic row %06d\n" % index for index in range(5000))
            body_rows[2500] = "unselected_marker = " + unselected_marker + "\n"
            body_rows.append("private_marker = " + private_marker + "\n")
            body = "".join(body_rows)
            schema = {
                "name": CANARY_TOOL,
                "description": "One-time synthetic intent-reader canary fixture.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            }
            registration = ctx.register_tool(
                CANARY_TOOL, CANARY_TOOL, schema, lambda *args, **kwargs: body
            )
            assert registration is not None

            call_number = 0

            def call(name, args):
                nonlocal call_number, stage
                call_number += 1
                stage = f"dispatch_{name}_{call_number}"
                raw = model_tools.handle_function_call(
                    name,
                    args,
                    task_id=session_id,
                    session_id=session_id,
                    tool_call_id=namespace + "-" + str(call_number),
                    # This is Hermes' production ambient-user-task channel.  It reaches
                    # the adapter as trusted host metadata and must never be copied into
                    # the isolated reader model payload.
                    user_task=parent_context_marker,
                )
                assert isinstance(raw, str) and len(raw.encode("utf-8")) <= 32768
                assert body not in raw and private_marker not in raw
                value = json.loads(raw)
                if isinstance(value.get("error"), str):
                    value = json.loads(value["error"])
                return value

            stage = "capture"
            pointer = call(CANARY_TOOL, {})
            enforce(pointer)
            assert pointer["code"] == "SPILLED" and pointer["result_kind"] == "pointer"
            pointer_wire = json.dumps(pointer, ensure_ascii=False)
            assert "intent_probe = 7319" not in pointer_wire
            assert unselected_marker not in pointer_wire
            assert private_marker not in pointer_wire
            handle = {
                key: pointer["sources"][0][key] for key in ("source_id", "snapshot_id")
            }
            entry = session.registry.resolve(session.session_id, handle["source_id"])
            assert entry.snapshot.data.decode("utf-8") == body

            payloads = []

            class ObservedProvider:
                def __init__(self, inner):
                    self.inner = inner

                def __getattr__(self, name):
                    return getattr(self.inner, name)

                def complete(self, **kwargs):
                    system = kwargs["system"]
                    user = kwargs["user"]
                    system_bytes = system.encode("utf-8")
                    user_bytes = user.encode("utf-8")
                    assert parent_context_marker not in system and parent_context_marker not in user
                    assert private_marker not in system and private_marker not in user
                    assert unselected_marker not in system and unselected_marker not in user
                    assert body not in system and body not in user
                    assert "intent_probe = 7319" in user
                    assert len(system_bytes) + len(user_bytes) <= settings[
                        "max_reader_payload_bytes"
                    ]
                    payloads.append(
                        {
                            "roles": ["system", "user"],
                            "system_bytes": len(system_bytes),
                            "user_bytes": len(user_bytes),
                            "system_sha256": _sha256_bytes(system_bytes),
                            "user_sha256": _sha256_bytes(user_bytes),
                        }
                    )
                    return self.inner.complete(**kwargs)

            original_provider = session._reader._provider
            session._reader._provider = ObservedProvider(original_provider)
            question = "What is the exact intent_probe value? Cite the line containing it."
            read_args = {
                "handles": [handle],
                "question": question,
                "selector": {"kind": "lines", "start": 1, "end": 10},
            }
            stage = "real_luna_first"
            first = call("context_shunt_read", read_args)
            enforce(first)
            assert first["code"] == "ANSWERED" and first["result_kind"] == "model_derived"
            assert "7319" in first["answer"]
            assert first["citations"] and all(item["verified"] for item in first["citations"])
            assert any("intent_probe = 7319" in item["quote"] for item in first["citations"])
            provenance = first["provenance"]
            assert provenance["requested_model"] == "gpt-5.6-luna"
            assert provenance["resolved_model"] == "gpt-5.6-luna"
            assert provenance["resolved_provider"] == "codex-stable"
            assert provenance["attribution_status"] == "resolved"
            assert 1 <= provenance["attempts_started"] <= 2
            assert len(payloads) == provenance["attempts_started"]

            stage = "exact_query_cache"
            payload_count_before_cache = len(payloads)
            second = call("context_shunt_read", read_args)
            enforce(second)
            assert second["answer"] == first["answer"]
            assert second["citations"] == first["citations"]
            assert second["provenance"]["cache_reused"] is True
            assert second["provenance"]["attempts_started"] == 0
            assert second["provenance"]["attempts_usage_complete"] == 0
            assert second["provenance"]["usage_complete"] is True
            assert len(payloads) == payload_count_before_cache

            stage = "aggregate_capture"
            logs = [
                {"level": "error", "trace_id": "tr-a", "service": "billing"},
                {"level": "info", "trace_id": "tr-b", "service": "billing"},
                {"level": "error", "trace_id": "tr-a", "service": "checkout"},
                {"level": "error", "trace_id": "tr-c", "service": "billing"},
                {"level": "error", "trace_id": None},
                {"level": "error", "trace_id": "tr-d", "service": None},
            ]
            logs.extend(
                {"level": "info", "trace_id": "padding-%d" % index, "service": "padding"}
                for index in range(700)
            )
            loki = {
                "data": {
                    "result": [{
                        "stream": {"job": "synthetic"},
                        "values": [
                            [str(index), json.dumps(row, separators=(",", ":"))]
                            for index, row in enumerate(logs)
                        ],
                    }]
                }
            }
            loki_body = json.dumps(loki, separators=(",", ":"))
            assert len(loki_body.encode("utf-8")) > adapter["_config"].limits.max_tool_result_bytes
            captured = session.post_tool_result("req_" + namespace + "_aggregate", loki_body)
            assert captured is not None and captured.envelope is not None
            aggregate_handle = {
                key: captured.envelope["sources"][0][key]
                for key in ("source_id", "snapshot_id")
            }
            aggregate = call(
                "context_shunt_inspect",
                {
                    **aggregate_handle,
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
                },
            )
            enforce(aggregate)
            assert aggregate["code"] == "EXTRACTED"
            assert aggregate["provenance"]["attempts_started"] == 0
            exact = json.loads(aggregate["extraction"]["segments"][0]["text"])
            assert exact["matched_count"] == 5 and exact["records_scanned"] == len(logs)
            assert exact["distinct"][0]["count"] == 4
            assert exact["distinct"][0]["values_complete"] is True
            assert exact["groups"] == [
                {"key": ["billing"], "count": 2},
                {"key": ["checkout"], "count": 1},
                {"key": [None], "count": 1},
                {"key": [{"missing": True}], "count": 1},
            ]

            stage = "usage_accounting"
            stats = call("context_shunt_stats", {})
            rows = stats["stats"]["records"]
            first_row = next(row for row in rows if row["operation_id"] == first["accounting_id"])
            cache_row = next(row for row in rows if row["operation_id"] == second["accounting_id"])
            aggregate_row = next(
                row for row in rows if row["operation_id"] == aggregate["accounting_id"]
            )
            assert first_row["attempts_started"] == provenance["attempts_started"]
            assert cache_row["attempts_started"] == 0
            assert cache_row["reader_token_method"] == "not_applicable"
            assert aggregate_row["attempts_started"] == 0

            # The controller runs the established release canary first, then this
            # supplemental canary. Revoke only those two exact synthetic scopes so no
            # canary payload or raw-artifact recovery file survives acceptance.
            stage = "synthetic_scope_cleanup"
            session._reader._provider = original_provider
            original_provider = None
            closed_scopes = []
            for canary_session_id in (release_session_id, session_id):
                canary_session = adapter["_sessions"].pop(canary_session_id, None)
                assert canary_session is not None
                canary_session.close()
                closed_scopes.append(canary_session_id)
            session = None

            stage = "complete"
            report.update(
                result="PASS",
                adapter_sha256=settings["adapter_sha256"],
                core_tree_sha256=settings["core_tree_sha256"],
                emitted_schema="1.3",
                payload_roles_preserved=True,
                payload_parent_user_task_marker_absent=True,
                payload_unselected_source_absent=True,
                payload_bounded=True,
                payloads=payloads,
                real_luna={
                    "requested_provider": provenance["requested_provider"],
                    "requested_model": provenance["requested_model"],
                    "resolved_provider": provenance["resolved_provider"],
                    "resolved_model": provenance["resolved_model"],
                    "attribution_status": provenance["attribution_status"],
                    "attempts_started": provenance["attempts_started"],
                    "attempts_usage_complete": provenance["attempts_usage_complete"],
                    "usage_complete": provenance["usage_complete"],
                    "reader_token_method": first_row["reader_token_method"],
                    "reader_input_tokens": first_row.get("reader_input_tokens"),
                    "reader_output_tokens": first_row.get("reader_output_tokens"),
                    "reader_cache_tokens": first_row.get("reader_cache_tokens"),
                },
                citations_verified=True,
                exact_query_cache={
                    "hit": True,
                    "provider_attempts": cache_row["attempts_started"],
                    "reader_token_method": cache_row["reader_token_method"],
                },
                aggregate={
                    "matched_count": exact["matched_count"],
                    "records_scanned": exact["records_scanned"],
                    "distinct_count": exact["distinct"][0]["count"],
                    "groups": exact["groups"],
                    "provider_attempts": aggregate_row["attempts_started"],
                },
                synthetic_scopes_closed=len(closed_scopes),
                no_raw_leak=True,
            )
        except BaseException as exc:
            report["failure_type"] = type(exc).__name__
            report["failure_stage"] = stage
            import traceback

            extracted = traceback.extract_tb(exc.__traceback__)
            report["failure_line"] = extracted[-1].lineno if extracted else None
        finally:
            try:
                try:
                    if session is not None and original_provider is not None:
                        session._reader._provider = original_provider
                except BaseException as exc:
                    report.update(
                        result="FAIL",
                        failure_type=type(exc).__name__,
                        failure_stage="cleanup_provider_restore",
                    )
                sessions_to_close = []
                if adapter is not None:
                    for canary_session_id in (release_session_id, session_id):
                        if canary_session_id is None:
                            continue
                        canary_session = adapter["_sessions"].pop(canary_session_id, None)
                        if canary_session is not None:
                            sessions_to_close.append(canary_session)
                if session is not None and all(item is not session for item in sessions_to_close):
                    sessions_to_close.append(session)
                for canary_session in sessions_to_close:
                    try:
                        canary_session.close()
                    except BaseException as exc:
                        report.update(
                            result="FAIL",
                            failure_type=type(exc).__name__,
                            failure_stage="cleanup_session_close",
                        )
                try:
                    if registration is not None:
                        registration.dispose()
                except BaseException as exc:
                    report.update(
                        result="FAIL",
                        failure_type=type(exc).__name__,
                        failure_stage="cleanup_tool_dispose",
                    )
                pending = base / ".done.json"
                pending.write_text(json.dumps(report, separators=(",", ":")))
                os.chmod(pending, 0o600)
                os.replace(pending, base / "done.json")
            finally:
                claim.close()

    context = contextvars.copy_context()
    threading.Thread(
        target=lambda: context.run(worker),
        daemon=True,
        name="context-shunt-intent-reader-canary",
    ).start()
