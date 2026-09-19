from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

import bridges.native_hermes_invoke as invoke
from bridges.native_hermes_invoke import (
    LIVE_DISPATCH_ENV_GATE,
    DispatchNotSuccessful,
    assert_real_dispatch_succeeded,
    build_context_shunt_read_args,
    build_dispatch_env,
    build_isolated_config,
    dry_run_report,
    main,
    stage_bundled_plugin,
    start_relay_background,
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _short_unix_socket_path() -> str:
    return str(Path(tempfile.gettempdir()) / f"nhi-{uuid.uuid4().hex[:8]}.sock")


def test_build_isolated_config_grants_provider_and_model_override_and_zero_retries() -> None:
    config = build_isolated_config(
        model="gpt-5.6-luna", workspace_dir="/w", cache_dir="/c",
        relay_base_url="http://127.0.0.1:18080/v1",
    )
    llm_cfg = config["plugins"]["entries"]["context-shunt"]["llm"]
    assert llm_cfg["allow_provider_override"] is True
    assert llm_cfg["allowed_providers"] == ["custom"]
    assert llm_cfg["allow_model_override"] is True
    assert llm_cfg["allowed_models"] == ["gpt-5.6-luna"]
    aux = config["auxiliary"]
    assert aux["transient_retries"] == 0
    assert aux["context_shunt_reader"] == {
        "provider": "custom", "model": "gpt-5.6-luna",
        "base_url": "http://127.0.0.1:18080/v1", "api_key": "no-key-required",
        "fallback_chain": [],
    }
    assert config["plugins"]["entries"]["context-shunt"]["config"]["workspace_roots"] == ["/w"]


def test_build_isolated_config_enables_plugin_and_reader_explicitly() -> None:
    config = build_isolated_config(
        model="gpt-5.6-luna", workspace_dir="/w", cache_dir="/c",
        relay_base_url="http://127.0.0.1:18080/v1",
    )
    assert config["plugins"]["enabled"] == ["context-shunt"]
    reader_cfg = config["plugins"]["entries"]["context-shunt"]["config"]["reader"]
    assert reader_cfg == {"enabled": True, "automatic_extract": True}


def test_build_isolated_config_sets_reader_base_url_and_placeholder_key_not_env_only() -> None:
    # Env-only OPENAI_BASE_URL/OPENAI_API_KEY was tried and failed on the exact host
    # (resolve_provider_client's own env fallback never triggers once an earlier ladder
    # rung already returns a runtime dict) -- the per-task auxiliary.*.base_url/api_key
    # fields are the real, directly-supported route.
    config = build_isolated_config(
        model="gpt-5.6-luna", workspace_dir="/w", cache_dir="/c",
        relay_base_url="http://127.0.0.1:19999/v1",
    )
    reader_cfg = config["auxiliary"]["context_shunt_reader"]
    assert reader_cfg["base_url"] == "http://127.0.0.1:19999/v1"
    assert reader_cfg["api_key"] == "no-key-required"


def test_stage_bundled_plugin_symlinks_real_adapter_files_read_only(tmp_path: Path) -> None:
    adapter_dir = tmp_path / "real-adapter"
    adapter_dir.mkdir()
    (adapter_dir / "plugin.yaml").write_text("name: context-shunt\n", encoding="utf-8")
    (adapter_dir / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
    hermes_home = tmp_path / "home"

    bundled_root = stage_bundled_plugin(
        adapter_init_path=str(adapter_dir / "__init__.py"), hermes_home=str(hermes_home),
    )

    staged = bundled_root / "context-shunt"
    assert (staged / "plugin.yaml").is_symlink()
    assert (staged / "__init__.py").is_symlink()
    assert (staged / "plugin.yaml").resolve() == (adapter_dir / "plugin.yaml").resolve()
    assert (staged / "__init__.py").read_text(encoding="utf-8") == "def register(ctx):\n    pass\n"


def test_build_dispatch_env_points_at_relay_with_placeholder_key() -> None:
    env = build_dispatch_env(relay_base_url="http://127.0.0.1:18080/v1")
    assert env == {"OPENAI_BASE_URL": "http://127.0.0.1:18080/v1", "OPENAI_API_KEY": "no-key-required"}


def test_build_context_shunt_read_args_uses_paths_and_question_per_contract() -> None:
    args = build_context_shunt_read_args(source_path="/w/artifact.txt", question="what is in it?")
    assert args == {"paths": ["/w/artifact.txt"], "question": "what is in it?"}


def test_dry_run_report_contains_no_real_credential_and_is_json_safe() -> None:
    report = dry_run_report(
        relay_base_url="http://127.0.0.1:18080/v1", model="gpt-5.6-luna",
        workspace_dir="/w", cache_dir="/c", source_path="/w/artifact.txt",
        question="synthetic-only", hermes_home="/tmp/hermes-home",
    )
    encoded = json.dumps(report)  # must not raise
    assert "no-key-required" in encoded
    assert "synthetic-only" in encoded
    assert report["tool_call"]["name"] == "context_shunt_read"


def test_main_dry_run_never_imports_hermes_cli(tmp_path: Path, capsys) -> None:
    assert "hermes_cli" not in sys.modules
    source = tmp_path / "artifact.txt"
    source.write_text("synthetic-only", encoding="utf-8")
    rc = main([
        "--relay-listen-port", "18080",
        "--upstream-socket", str(tmp_path / "up.sock"),
        "--question", "synthetic-only",
        "--source-file", str(source),
    ])
    assert rc == 0
    assert "hermes_cli" not in sys.modules
    out = json.loads(capsys.readouterr().out)
    assert out["note"].startswith("Dry run only")


# Copied verbatim (schema/field shape, not any real secret) from real recorded canary
# runs on the exact host. The first is a false-positive LEGACY_COMPACTED/MODEL_ERROR
# deterministic fallback (/tmp/openclaw/shunt-native-wire/stdout.txt, an earlier run) --
# valid, well-formed JSON, `exit 0`, zero proxy dispatches. The second is a genuine
# success from a later run that reached the real PluginLlm/proxy end-to-end
# (/tmp/openclaw/shunt-native-wire/stderr.txt): `status: "ok"`, `code: "ANSWERED"`,
# `result_kind: "model_derived"`, one verified citation, `provenance.derived: true`,
# resolved to `custom`/`gpt-5.6-luna`.
_RECORDED_LEGACY_COMPACTION_FALLBACK = (
    '{"schema_version":"1.3","request_id":"req_gate","status":"partial","code":"LEGACY_COMPACTED",'
    '"answer":"","citations":[],"coverage":{"complete":false,"processed_chunks":0,"planned_chunks":1,'
    '"omitted":[{"source_id":"src_faa552f742356ad2","selector":{"kind":"lines","start":1,"end":1},'
    '"reason":"MODEL_ERROR"},{"source_id":"src_faa552f742356ad2","selector":{"kind":"all"},'
    '"reason":"UNKNOWN_REMAINDER"}],"upstream_truncated":false},'
    '"sources":[{"source_id":"src_faa552f742356ad2",'
    '"snapshot_id":"sha256:674eb0bca6f0884dafef451bc98ffc2ca20e21af9c1441b0c7c934aadb2dfce3",'
    '"media_type":"text/plain","bytes":35,"expires_at":"2026-09-19T11:11:03Z"}],"retryable":false,'
    '"guidance":"legacy-shaped compaction, not model-derived","failure_detail":"AVAILABILITY_EXHAUSTED",'
    '"result_kind":"legacy_compaction","provenance":{"derived":false,"label":"legacy_compaction",'
    '"attribution_status":"unknown","attribution_confidence":"none","attribution_policy":"allow_unverified",'
    '"attempts_started":2,"usage_complete":false,"attempts_usage_complete":0,'
    '"citations_mechanically_verified":false,"requested_provider":"custom","requested_model":"gpt-5.6-luna",'
    '"resolved_provider":null,"resolved_model":null,"reported_provider":null,"reported_model":null,'
    '"fallback_used":false},"accounting_id":"acc_eebac98ff9ed34bb"}'
)

_RECORDED_GENUINE_SUCCESS = (
    '{"schema_version":"1.3","request_id":"req_gate","status":"ok","code":"ANSWERED",'
    '"answer":"The deployment code is Orchid-742 [c1].","citations":[{"id":"c1",'
    '"source_id":"src_ce71283c0309da5f",'
    '"snapshot_id":"sha256:674eb0bca6f0884dafef451bc98ffc2ca20e21af9c1441b0c7c934aadb2dfce3",'
    '"locator":{"kind":"lines","start":1,"end":1},"quote":"The deployment code is Orchid-742.",'
    '"verified":true}],"coverage":{"complete":true,"processed_chunks":1,"planned_chunks":1,'
    '"omitted":[],"upstream_truncated":false},"sources":[{"source_id":"src_ce71283c0309da5f",'
    '"snapshot_id":"sha256:674eb0bca6f0884dafef451bc98ffc2ca20e21af9c1441b0c7c934aadb2dfce3",'
    '"media_type":"text/plain","bytes":35,"expires_at":"2026-09-19T11:20:11Z"}],'
    '"retryable":false,"result_kind":"model_derived","provenance":{"derived":true,'
    '"label":"model_generated_answer","attribution_status":"resolved",'
    '"attribution_confidence":"medium","attribution_policy":"allow_unverified",'
    '"attempts_started":1,"usage_complete":true,"attempts_usage_complete":1,'
    '"citations_mechanically_verified":true,"requested_provider":"custom",'
    '"requested_model":"gpt-5.6-luna","resolved_provider":"custom",'
    '"resolved_model":"gpt-5.6-luna","reported_provider":null,"reported_model":null,'
    '"fallback_used":false},"accounting_id":"acc_58f0ceeeb1ad9190"}'
)


def test_assert_real_dispatch_succeeded_rejects_the_recorded_legacy_compaction_false_positive() -> None:
    with pytest.raises(DispatchNotSuccessful) as excinfo:
        assert_real_dispatch_succeeded(_RECORDED_LEGACY_COMPACTION_FALLBACK)
    message = str(excinfo.value)
    assert "status='partial'" in message
    assert "result_kind='legacy_compaction'" in message
    assert "citations is empty" in message
    assert "coverage.complete is false" in message
    assert "provenance.derived is false" in message
    assert "resolved_provider" in message


def test_assert_real_dispatch_succeeded_accepts_the_recorded_genuine_success() -> None:
    parsed = assert_real_dispatch_succeeded(_RECORDED_GENUINE_SUCCESS)
    assert parsed["code"] == "ANSWERED"
    assert parsed["answer"] == "The deployment code is Orchid-742 [c1]."


def test_main_live_dispatch_returns_nonzero_on_the_recorded_false_positive(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(LIVE_DISPATCH_ENV_GATE, "1")
    source = tmp_path / "artifact.txt"
    source.write_text("synthetic-only", encoding="utf-8")

    def fake_live_run(**_kwargs):
        return _RECORDED_LEGACY_COMPACTION_FALLBACK

    monkeypatch.setattr(invoke, "live_run", fake_live_run)
    rc = main([
        "--relay-listen-port", "18080",
        "--upstream-socket", str(tmp_path / "up.sock"),
        "--question", "synthetic-only",
        "--source-file", str(source),
        "--live-dispatch",
    ])
    assert rc != 0
    err = json.loads(capsys.readouterr().err)
    assert "audit_failure" in err
    assert "legacy_compaction" in err["audit_failure"]


def test_main_live_dispatch_returns_zero_on_the_recorded_genuine_success(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv(LIVE_DISPATCH_ENV_GATE, "1")
    source = tmp_path / "artifact.txt"
    source.write_text("synthetic-only", encoding="utf-8")

    def fake_live_run(**_kwargs):
        return _RECORDED_GENUINE_SUCCESS

    monkeypatch.setattr(invoke, "live_run", fake_live_run)
    rc = main([
        "--relay-listen-port", "18080",
        "--upstream-socket", str(tmp_path / "up.sock"),
        "--question", "synthetic-only",
        "--source-file", str(source),
        "--live-dispatch",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "audit_failure" not in out


def test_main_live_dispatch_refuses_without_env_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(LIVE_DISPATCH_ENV_GATE, raising=False)
    source = tmp_path / "artifact.txt"
    source.write_text("synthetic-only", encoding="utf-8")
    with pytest.raises(SystemExit):
        main([
            "--relay-listen-port", "18080",
            "--upstream-socket", str(tmp_path / "up.sock"),
            "--question", "synthetic-only",
            "--source-file", str(source),
            "--live-dispatch",
        ])


def _run_unix_echo_server(sock_path: str, ready: threading.Event) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    server.settimeout(5)
    ready.set()
    try:
        conn, _ = server.accept()
    except socket.timeout:
        return
    with conn:
        conn.settimeout(5)
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            conn.sendall(chunk)
    server.close()


def test_start_relay_background_ready_fires_without_a_probe_connection_then_relays_once() -> None:
    sock_path = _short_unix_socket_path()
    echo_ready = threading.Event()
    echo_thread = threading.Thread(target=_run_unix_echo_server, args=(sock_path, echo_ready), daemon=True)
    echo_thread.start()
    assert echo_ready.wait(5)

    port = _free_port()
    thread = start_relay_background(listen_port=port, upstream_socket_path=sock_path)
    assert thread.is_alive()

    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    with client:
        client.sendall(b"ping")
        client.shutdown(socket.SHUT_WR)
        client.settimeout(5)
        received = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            received += chunk
        assert received == b"ping"
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_live_run_writes_isolated_config_sets_env_and_dispatches_through_fakes(tmp_path: Path) -> None:
    hermes_home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    cache = tmp_path / "cache"
    source = tmp_path / "artifact.txt"
    source.write_text("synthetic content", encoding="utf-8")
    port = _free_port()
    sock_path = str(tmp_path / "up.sock")

    calls: dict = {}

    def fake_register(*, adapter_init_path, hermes_home, plugin_id="context-shunt"):
        calls["adapter_init_path"] = adapter_init_path
        calls["hermes_home"] = hermes_home
        return object(), object(), {"key": plugin_id, "enabled": True, "error": None}

    def fake_dispatch(*, model_tools_module, args, **_kwargs):
        calls["args"] = args
        return json.dumps({"answer": "ok"})

    original_register = invoke.register_real_plugin
    original_dispatch = invoke.dispatch_context_shunt_read
    invoke.register_real_plugin = fake_register
    invoke.dispatch_context_shunt_read = fake_dispatch
    try:
        result = invoke.live_run(
            hermes_home=str(hermes_home), adapter_init_path="/fake/adapter/__init__.py",
            relay_listen_port=port, upstream_socket_path=sock_path,
            model="gpt-5.6-luna", workspace_dir=str(workspace), cache_dir=str(cache),
            source_path=str(source), question="what does the file say?",
        )
    finally:
        invoke.register_real_plugin = original_register
        invoke.dispatch_context_shunt_read = original_dispatch
        os.environ.pop("OPENAI_BASE_URL", None)
        os.environ.pop("OPENAI_API_KEY", None)

    assert result == json.dumps({"answer": "ok"})
    assert calls["args"] == {"paths": [str(source)], "question": "what does the file say?"}
    assert calls["adapter_init_path"] == "/fake/adapter/__init__.py"
    written = json.loads((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert written["auxiliary"]["transient_retries"] == 0
