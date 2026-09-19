"""In-container entrypoint: starts the relay, then performs one genuine dispatch of the
installed `context_shunt_read` tool through real plugin discovery/registration and
Hermes' own `model_tools.handle_function_call` -- the same host-facing surface a real
agent turn uses. Meant to run as the container's main process inside the reviewed,
`--network none` image, in the same network namespace as the relay it starts, so
`ctx.llm`'s eventual custom-provider dispatch has nowhere to go but that relay -> the
mounted Unix socket.

Design corrections from an earlier draft, made after reading the exact live source
(`agent/plugin_llm.py`, `hermes_cli/plugins.py`) rather than assuming an API shape:

- `ctx.llm` is a `PluginLlm` instance, not a callable -- the real surface is
  `ctx.llm.complete(messages=..., task=..., provider=..., model=..., ...)`.
- `PluginContext.llm` is a lazy property (`self._llm` starts `None`; first access builds
  a real `PluginLlm(plugin_id=...)`). This script never assigns `ctx._llm` itself --
  doing so would just be a longer-winded way of doing exactly what the lazy property
  already does, and assigning it directly is the pattern used for the *fake*-caller
  probe (`evals/hermes-host-contract/probe.py`), which this task explicitly rules out.
- Requesting `provider="custom"` from `context_shunt_read`'s own `ctx.llm.complete(...)`
  call is gated by `plugins.entries.<id>.llm.allow_provider_override` +
  `allowed_providers` (`agent/plugin_llm.py` `_check_overrides`/`_TrustPolicy`), not just
  `allow_model_override` -- both are set in `build_isolated_config` below.
- The reader's provider/model come from `auxiliary.context_shunt_reader.{provider,model}`
  in config (`adapters/hermes/context-shunt/__init__.py:_reader_target`), and
  `auxiliary.context_shunt_reader.fallback_chain` / `auxiliary.transient_retries` are real
  keys read by `agent/auxiliary_client.py` (`_fallback_chain_entry`,
  `_transient_retry_count`) -- confirmed by reading that file on the pinned image, not
  assumed.
- `context_shunt_read` takes `{"paths": [...], "question": "..."}` for an initial capture
  (see `contracts/v1/tool-args.schema.json` `readArgs`) -- no separate capture-tool step
  is needed for a fresh synthetic artifact.

Second round of corrections, after local review found `register_real_plugin` was manual
registration (`spec_from_file_location` + `adapter.register(ctx)`), not real plugin
discovery:

- `register_real_plugin` now stages the real adapter as a bundled-plugin directory
  (`<HERMES_HOME>/bundled-plugins/<plugin_id>/{plugin.yaml,__init__.py}`, read-only
  symlinks to the real files -- never copies/mutates the adapter) and points
  `HERMES_BUNDLED_PLUGINS` at that root (`hermes_cli/plugins.py:get_bundled_plugins_dir`
  reads this env var first, before falling back to the in-repo path). It then constructs
  a real `PluginManager()` and calls its real `discover_and_load()` -- the actual
  manifest-scan/gate/load sweep every installed plugin goes through -- instead of
  building a `PluginContext` and calling `adapter.register(ctx)` directly.
- After discovery, it inspects the manager's own `list_plugins()` audit info for this
  plugin's `enabled`/`error` fields (the host's own post-discovery bookkeeping, not
  something this script invents) and raises if discovery did not cleanly enable it --
  this is the "real host audit outcome assertion" this design was missing before.
- `plugins.enabled: [plugin_id]` is now a real top-level config key
  (`hermes_cli/plugins_discovery.py:_get_enabled_plugins`, read via `plugins.enabled` --
  `None` means opt-in-default/nothing enabled, so an explicit list is required).
- `plugins.entries.<id>.config.reader.{enabled,automatic_extract}` are real
  `context_shunt.config.ReaderConfig` fields (`packages/core-py/src/context_shunt/config.py`,
  both default `True`) -- set explicitly here for auditability rather than relying on the
  implicit default, since `reader.enabled: false` makes `context_shunt_read` return a
  bounded refusal without ever calling a model (`adapters/hermes/README.md`). Note
  `automatic_extract` is a genuine Python-core key, unlike the unrelated OpenClaw/TS
  adapter's own `reader.automatic_extract`, which is a separate config surface entirely.
- Still never assigns `ctx._llm` or any fake `PluginLlm`/provider -- real discovery
  builds the real `PluginContext` internally and calls the adapter's own `register(ctx)`,
  so the lazy `ctx.llm` property still constructs a genuine `PluginLlm(plugin_id=...)`.

Third round of corrections, after a real fake-upstream run on the exact host returned a
false-positive success (`context_shunt_read` fell back to a deterministic
`LEGACY_COMPACTED`/`MODEL_ERROR` compaction -- real, well-formed JSON, `exit 0` -- with
zero proxy dispatches ever reaching the relay):

- The env-only assumption above was wrong: `agent.auxiliary_client.resolve_provider_client`
  (the real per-task provider resolver actually used for `auxiliary.<task>.*` calls, not
  the main-agent-loop `resolve_runtime_provider`) reaches its `_resolve_custom_branch`
  fallback (`OPENAI_BASE_URL`/`OPENAI_API_KEY` env, via `_try_custom_endpoint` ->
  `_resolve_custom_runtime`) only when neither an explicit base_url/api_key nor a task
  config entry supplies one -- and on the exact host, some earlier rung of
  `resolve_runtime_provider(requested="custom")` already returns *a* dict (not raising),
  so `_resolve_custom_runtime`'s own env-var fallback (guarded by `isinstance(runtime,
  dict)`) never triggers. Confirmed by reading `agent/auxiliary_client.py`
  (`_resolve_custom_branch`, `_resolve_custom_runtime`) on the exact host source.
- The real, directly-supported fix (same file, `_resolve_task_provider_model` and
  `_prepare_aux_request`): `auxiliary.<task>.base_url` / `auxiliary.<task>.api_key` are
  first-class per-task config fields, read before any provider-wide runtime resolution
  and passed straight through to `_resolve_call_client` as `resolved_base_url`/
  `resolved_api_key` -- so `auxiliary.context_shunt_reader.base_url` (this canary's
  in-namespace relay URL) and `.api_key` (a placeholder nonce) now set the route
  explicitly, instead of depending on env-var fallback behavior that does not hold on
  this host. This is supported configuration, not a credential-boundary bypass: no real
  key is read or needed (`_scoped_key_env` — confirmed at `agent/auxiliary_client.py`
  lines 1059-1071 — only ever supplies a *paired* env var value, and the placeholder
  string here is never a secret), and `--network none` still means the loopback base_url
  has nowhere to resolve except the mounted relay socket.
- Added `assert_real_dispatch_succeeded()`: `main()` previously always returned 0 after
  printing whatever `context_shunt_read` returned, including the exact false-positive
  above (a fully-formed, valid JSON *refusal*). The predicate is derived from that real
  recorded failure payload (`/tmp/openclaw/shunt-native-wire/stdout.txt`): a genuine
  model-derived answer must have `status: "ok"` and `result_kind: "model_derived"`,
  non-empty `citations`, `coverage.complete: true`, and `provenance.derived: true` with
  both `resolved_provider`/`resolved_model` non-null -- the failing payload fails every
  one of those checks while still being valid JSON with `exit 0`.

Fourth round of corrections, after a second real fake-upstream run reached the real
`PluginLlm`/proxy end-to-end and produced a genuine success (real, recorded at
`/tmp/openclaw/shunt-native-wire/stderr.txt`: `status: "ok"`, `code: "ANSWERED"`,
`result_kind: "model_derived"`, verified citations, `coverage.complete: true`,
`provenance.derived: true`, `resolved_provider`/`resolved_model` = `"custom"`/
`"gpt-5.6-luna"`), which `assert_real_dispatch_succeeded()` incorrectly rejected:

- The predicate's "any non-null `code` marks a partial/error result" check was wrong --
  `code` is a sub-classification (`ANSWERED`, `LEGACY_COMPACTED`, etc.), not itself a
  pass/fail flag; a real success payload has a real, non-null `code`. Fixed to key off
  `status == "ok"` and `result_kind == "model_derived"` instead, matching both real
  recorded payloads (the false-positive has `status: "partial"`, the genuine success has
  `status: "ok"`).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from bridges.native_hermes_relay import serve as relay_serve

PINNED_IMAGE_DIGEST = "sha256:7ae35667fc2bd17cd8f6da6d117ca3f0a5754d8ebeb4b76bcff46c0768b9fb4b"
LIVE_DISPATCH_ENV_GATE = "NATIVE_HERMES_I_UNDERSTAND"
PLUGIN_ID = "context-shunt"


# -- pure config/plan building (no hermes_cli import; safe to unit test anywhere) ------


def build_isolated_config(
    *, model: str, workspace_dir: str, cache_dir: str, relay_base_url: str, plugin_id: str = PLUGIN_ID,
) -> dict[str, Any]:
    """Isolated HERMES_HOME config.yaml content.

    ``auxiliary.context_shunt_reader.base_url``/``.api_key`` set the reader's real
    per-task route explicitly (`agent/auxiliary_client.py:_resolve_task_provider_model`
    reads these directly, ahead of any provider-wide runtime resolution) -- relying on
    `OPENAI_BASE_URL`/`OPENAI_API_KEY` env fallback alone was tried and failed on the
    exact host (see module docstring, third round of corrections). ``api_key`` is a
    placeholder nonce, never a real credential: `--network none` means this base_url
    has nowhere to resolve except the mounted relay socket.
    """
    return {
        "plugins": {
            "enabled": [plugin_id],
            "entries": {
                plugin_id: {
                    "config": {
                        "workspace_roots": [workspace_dir],
                        "cache_dir": cache_dir,
                        "reader": {"enabled": True, "automatic_extract": True},
                    },
                    "llm": {
                        "allow_model_override": True,
                        "allowed_models": [model],
                        "allow_provider_override": True,
                        "allowed_providers": ["custom"],
                    },
                }
            },
        },
        "auxiliary": {
            "transient_retries": 0,
            "context_shunt_reader": {
                "provider": "custom",
                "model": model,
                "base_url": relay_base_url,
                "api_key": "no-key-required",
                "fallback_chain": [],
            },
        },
        "toolsets": {"enabled": ["context_shunt"]},
    }


def build_dispatch_env(*, relay_base_url: str) -> dict[str, str]:
    """Env vars the live path sets so `_resolve_custom_runtime()` finds the in-container
    relay instead of any real provider or the main CLI's saved runtime."""
    return {
        "OPENAI_BASE_URL": relay_base_url,
        "OPENAI_API_KEY": "no-key-required",
    }


def build_context_shunt_read_args(*, source_path: str, question: str) -> dict[str, Any]:
    return {"paths": [source_path], "question": question}


def dry_run_report(
    *, relay_base_url: str, model: str, workspace_dir: str, cache_dir: str,
    source_path: str, question: str, hermes_home: str,
) -> dict[str, Any]:
    """Everything an operator needs to review before a live run -- no hermes_cli import."""
    return {
        "pinned_image_digest": PINNED_IMAGE_DIGEST,
        "hermes_home": hermes_home,
        "config_yaml": build_isolated_config(
            model=model, workspace_dir=workspace_dir, cache_dir=cache_dir, relay_base_url=relay_base_url,
        ),
        "dispatch_env": build_dispatch_env(relay_base_url=relay_base_url),
        "tool_call": {"name": "context_shunt_read", "args": build_context_shunt_read_args(
            source_path=source_path, question=question)},
        "live_dispatch_gate_env": LIVE_DISPATCH_ENV_GATE,
        "note": (
            "Dry run only -- no hermes_cli import attempted, no relay started, no network, "
            "no credentials. See module docstring's ASSUMPTION note before --live-dispatch."
        ),
    }


# -- relay lifecycle (stdlib only) ------------------------------------------------------


def start_relay_background(*, listen_port: int, upstream_socket_path: str, timeout: float = 10.0) -> threading.Thread:
    """Start the relay in a daemon thread and block until it is actually listening.

    Uses `ready_callback`, never a probe TCP connection -- a probe connection would
    itself be the one dispatch `max_connections=1` allows and would be relayed to the
    real upstream socket as bogus traffic.
    """
    ready = threading.Event()
    thread = threading.Thread(
        target=relay_serve,
        kwargs=dict(
            listen_host="127.0.0.1", listen_port=listen_port,
            upstream_socket_path=upstream_socket_path, max_connections=1,
            ready_callback=ready.set,
        ),
        daemon=True,
    )
    thread.start()
    if not ready.wait(timeout):
        raise TimeoutError("relay did not report ready in time")
    return thread


# -- real dispatch (imports are local; only importable inside the real image) ----------


def stage_bundled_plugin(*, adapter_init_path: str, hermes_home: str, plugin_id: str = PLUGIN_ID) -> Path:
    """Stage the real adapter directory as a bundled-plugin source: read-only symlinks
    to the real `plugin.yaml` and `__init__.py`, never a copy or mutation of either.
    Returns the bundled-plugins *root* (the parent of `<plugin_id>/`), which callers
    point `HERMES_BUNDLED_PLUGINS` at -- `get_bundled_plugins_dir()` reads that env var
    before falling back to the in-repo `plugins/` directory."""
    adapter_dir = Path(adapter_init_path).resolve().parent
    bundled_root = Path(hermes_home) / "bundled-plugins"
    staged_dir = bundled_root / plugin_id
    staged_dir.mkdir(parents=True, exist_ok=True)
    for name in ("plugin.yaml", "__init__.py"):
        src = adapter_dir / name
        dst = staged_dir / name
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src)
    return bundled_root


def register_real_plugin(*, adapter_init_path: str, hermes_home: str, plugin_id: str = PLUGIN_ID):
    """Run real Hermes plugin discovery (`PluginManager.discover_and_load`) against the
    staged bundled-plugin directory -- not manual `spec_from_file_location` +
    `adapter.register(ctx)`, which only exercises the adapter's own `register()` and
    skips the host's actual gating (enabled/disabled lists, manifest validation,
    dependency ordering, per-plugin error capture).

    Never touches `ctx._llm` or constructs a `PluginContext` itself: `discover_and_load`
    builds the real `PluginContext` internally and calls the adapter's own
    `register(ctx)`, so the lazy `ctx.llm` property still triggers a genuine
    `PluginLlm(plugin_id=...)` exactly as it would for any real installed plugin.

    Raises if the host's own post-discovery bookkeeping (`PluginManager.list_plugins()`)
    does not show this plugin cleanly enabled with no error -- a real audit assertion,
    not an assumption that discovery succeeded.
    """
    from hermes_cli import plugins as host_plugins
    from hermes_cli.plugins import PluginManager
    import model_tools

    bundled_root = stage_bundled_plugin(
        adapter_init_path=adapter_init_path, hermes_home=hermes_home, plugin_id=plugin_id,
    )
    os.environ["HERMES_BUNDLED_PLUGINS"] = str(bundled_root)

    manager = PluginManager()
    host_plugins._plugin_manager = manager
    manager.discover_and_load()

    loaded = {info["key"]: info for info in manager.list_plugins()}
    info = loaded.get(plugin_id)
    if info is None:
        raise RuntimeError(
            f"real plugin discovery never saw {plugin_id!r}; loaded plugins: {sorted(loaded)}"
        )
    if not info["enabled"] or info["error"]:
        raise RuntimeError(f"real plugin discovery did not cleanly enable {plugin_id!r}: {info}")
    return manager, model_tools, info


def dispatch_context_shunt_read(
    *, model_tools_module, args: dict[str, Any], session_id: str = "native-canary-session",
    call_id: str = "native-canary-call-1",
) -> str:
    """Real host tool dispatch -- the same surface a genuine agent turn uses."""
    return model_tools_module.handle_function_call(
        "context_shunt_read", args,
        task_id=session_id, session_id=session_id, tool_call_id=call_id,
        turn_id=f"turn-{call_id}", api_request_id=f"api-{call_id}",
        enabled_tools=None, enabled_toolsets=["context_shunt"], disabled_toolsets=[],
        skip_pre_tool_call_hook=False,
    )


class DispatchNotSuccessful(RuntimeError):
    """Raised when the real `context_shunt_read` result is a bounded refusal/fallback,
    not a genuine model-derived answer -- e.g. a `LEGACY_COMPACTED`/`MODEL_ERROR`
    deterministic compaction, which is well-formed, valid JSON and would otherwise look
    like success to any caller that only checks "did this raise / is exit code 0"."""


def assert_real_dispatch_succeeded(tool_result_json: str) -> dict[str, Any]:
    """Real success predicate for one `context_shunt_read` result.

    Derived from two real recorded payloads on the exact host
    (`/tmp/openclaw/shunt-native-wire/std{out,err}.txt`): a false-positive fallback
    (`status: "partial"`, `code: "LEGACY_COMPACTED"`, `result_kind: "legacy_compaction"`,
    empty `citations`, `coverage.complete: false`, `provenance.derived: false`, both
    `resolved_provider`/`resolved_model` null) and a genuine success (`status: "ok"`,
    `code: "ANSWERED"`, `result_kind: "model_derived"`, non-empty verified `citations`,
    `coverage.complete: true`, `provenance.derived: true`, `resolved_provider`/
    `resolved_model` both `"custom"`/`"gpt-5.6-luna"`). `code` is a sub-classification,
    not itself a pass/fail signal -- `ANSWERED` is a real code on a success, so the
    predicate keys off `status`/`result_kind`, not "is `code` non-null". Returns the
    parsed result on success; raises `DispatchNotSuccessful` with every failing check
    otherwise."""
    parsed = json.loads(tool_result_json)
    problems: list[str] = []
    if parsed.get("status") != "ok":
        problems.append(f"status={parsed.get('status')!r} (expected 'ok')")
    if parsed.get("result_kind") != "model_derived":
        problems.append(
            f"result_kind={parsed.get('result_kind')!r} (expected 'model_derived'; "
            "'legacy_compaction' is a deterministic fallback, not model output)"
        )
    if not parsed.get("citations"):
        problems.append("citations is empty -- no verified evidence backs the answer")
    if not (parsed.get("coverage") or {}).get("complete"):
        problems.append("coverage.complete is false -- not all requested sources were processed")
    provenance = parsed.get("provenance") or {}
    if not provenance.get("derived", False):
        problems.append("provenance.derived is false -- the answer was not produced by a model")
    if not provenance.get("resolved_provider") or not provenance.get("resolved_model"):
        problems.append(
            f"provenance.resolved_provider={provenance.get('resolved_provider')!r} / "
            f"resolved_model={provenance.get('resolved_model')!r} -- the audited task never "
            "actually resolved to a real provider/model route"
        )
    if problems:
        raise DispatchNotSuccessful("real dispatch did not succeed: " + "; ".join(problems))
    return parsed


def live_run(
    *, hermes_home: str, adapter_init_path: str, relay_listen_port: int, upstream_socket_path: str,
    model: str, workspace_dir: str, cache_dir: str, source_path: str, question: str,
) -> str:
    """Full in-namespace sequence: write isolated config, start relay, wait for it,
    register the real plugin, dispatch one genuine `context_shunt_read` call."""
    relay_base_url = f"http://127.0.0.1:{relay_listen_port}/v1"
    Path(hermes_home).mkdir(parents=True, exist_ok=True)
    Path(workspace_dir).mkdir(parents=True, exist_ok=True)
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    config = build_isolated_config(
        model=model, workspace_dir=workspace_dir, cache_dir=cache_dir, relay_base_url=relay_base_url,
    )
    (Path(hermes_home) / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    os.environ["HERMES_HOME"] = hermes_home
    for key, value in build_dispatch_env(relay_base_url=relay_base_url).items():
        os.environ[key] = value

    start_relay_background(listen_port=relay_listen_port, upstream_socket_path=upstream_socket_path)

    _manager, model_tools_module, _plugin_info = register_real_plugin(
        adapter_init_path=adapter_init_path, hermes_home=hermes_home,
    )
    args = build_context_shunt_read_args(source_path=source_path, question=question)
    return dispatch_context_shunt_read(model_tools_module=model_tools_module, args=args)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", default="/run/context-shunt/hermes-home")
    parser.add_argument("--workspace-dir", default="/run/context-shunt/workspace")
    parser.add_argument("--cache-dir", default="/run/context-shunt/cache")
    parser.add_argument("--adapter-init-path", default="/opt/context-shunt/adapters/hermes/context-shunt/__init__.py")
    parser.add_argument("--relay-listen-port", type=int, required=True)
    parser.add_argument("--upstream-socket", required=True)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--question", required=True, help="bounded synthetic question; never a real/sensitive value")
    parser.add_argument("--source-file", required=True, help="path to a bounded synthetic artifact to write and read")
    parser.add_argument(
        "--live-dispatch", action="store_true",
        help=f"perform a real dispatch; also requires {LIVE_DISPATCH_ENV_GATE}=1 in the environment",
    )
    args = parser.parse_args(argv)

    if not args.live_dispatch:
        print(json.dumps(dry_run_report(
            relay_base_url=f"http://127.0.0.1:{args.relay_listen_port}/v1", model=args.model,
            workspace_dir=args.workspace_dir, cache_dir=args.cache_dir,
            source_path=args.source_file, question=args.question, hermes_home=args.hermes_home,
        ), indent=2))
        return 0

    if os.environ.get(LIVE_DISPATCH_ENV_GATE) != "1":
        raise SystemExit(
            f"--live-dispatch also requires {LIVE_DISPATCH_ENV_GATE}=1 in the environment "
            "(explicit double opt-in; refusing to dispatch for real otherwise)"
        )

    result = live_run(
        hermes_home=args.hermes_home, adapter_init_path=args.adapter_init_path,
        relay_listen_port=args.relay_listen_port, upstream_socket_path=args.upstream_socket,
        model=args.model, workspace_dir=args.workspace_dir, cache_dir=args.cache_dir,
        source_path=args.source_file, question=args.question,
    )
    try:
        assert_real_dispatch_succeeded(result)
    except DispatchNotSuccessful as exc:
        print(json.dumps({"live_dispatch": True, "tool_result": result, "audit_failure": str(exc)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({"live_dispatch": True, "tool_result": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
