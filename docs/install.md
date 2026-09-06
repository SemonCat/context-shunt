# Install, verify, uninstall

Two adapters, one shared contract. Pick the host you run; the semantics are identical
either way because both cores are validated against the same fixtures in
[`contracts/v1`](../contracts/v1).

Before installing, read [`capability-matrix.md`](capability-matrix.md): it says which
modes each host actually supports and why the optional Suma post-tool mode is off.

## Prerequisites

| | Version |
| --- | --- |
| Python (Hermes adapter) | 3.11 or newer |
| Node (OpenClaw adapter) | 20 or newer |
| Hermes | `hermes-agent` 0.18.0 or newer, with plugin hooks enabled |
| OpenClaw | `openclaw` 2026.9.x or newer, with native plugins enabled |
| Reader model | a host bridge that serves `gpt-5.6-luna` |

Without a `gpt-5.6-luna` bridge the local gate still works; the reader reports
`MODEL_ERROR` rather than answering with another model.

## Build and self-check first

```bash
git clone <your-fork> context-shunt && cd context-shunt

# Python core
python3 -m venv .venv
./.venv/bin/pip install -e 'packages/core-py[dev]'

# TypeScript core and the OpenClaw adapter
npm install

# Every deterministic gate. No host and no provider needed.
./scripts/verify unit all
./scripts/verify packaging all
```

`scripts/verify` exits `0` on pass, `1` on failure, and `2` for `NOT_RUN` — a gate whose
prerequisite is absent. `NOT_RUN` is never a pass.

## Hermes

The plugin is a directory containing `plugin.yaml` and `__init__.py`, which is what Hermes
loads. It runs in the agent process, so review the code before enabling it.

```bash
# 1. Install the core into the interpreter Hermes uses.
/path/to/hermes/python -m pip install ./packages/core-py

# 2. Link or copy the plugin into the Hermes plugin directory.
cp -R adapters/hermes/context-shunt ~/.hermes/plugins/context-shunt
# or, for development:
ln -s "$PWD/adapters/hermes/context-shunt" ~/.hermes/plugins/context-shunt

# 3. Merge examples/config/hermes.config.yaml into ~/.hermes/config.yaml and set
#    workspace_roots to the paths you want readable.

# 4. Restart Hermes. The capability report is logged at startup.
```

The `llm` block in the example config is required: Hermes gates plugin model overrides per
plugin, and the reader only ever asks for `gpt-5.6-luna`.

Verify against your own install:

```bash
CONTEXT_SHUNT_HERMES_ROOT=/path/to/hermes-agent \
CONTEXT_SHUNT_HERMES_PYTHON=/path/to/hermes/python \
  ./scripts/verify integration hermes --mode local

./scripts/verify integration hermes --mode unsupported
```

Uninstall:

```bash
rm -rf ~/.hermes/plugins/context-shunt
/path/to/hermes/python -m pip uninstall context-shunt-core
rm -rf ~/.cache/context-shunt          # private snapshots and spill
# then remove the plugins.entries.context-shunt block from ~/.hermes/config.yaml
```

## OpenClaw

Native plugins run in the Gateway process, so review the code before loading it.

```bash
# 1. Build the core the adapter imports.
npm run build --workspace @context-shunt/core

# 2. Link and enable the plugin (--force acknowledges a local source).
openclaw plugins install --link ./adapters/openclaw --force
openclaw plugins enable context-shunt

# 3. Merge examples/config/openclaw.json into your openclaw.json and set
#    workspace_roots to the paths you want readable.

# 4. Inspect what the host actually loaded.
openclaw plugins inspect context-shunt --runtime --json
```

`before_tool_call` needs no conversation-access opt-in. The reader tool is registered only
when the capability probe finds a working model bridge.

Verify against your own install:

```bash
CONTEXT_SHUNT_OPENCLAW_ROOT=/path/to/openclaw \
  ./scripts/verify integration openclaw --mode local

./scripts/verify integration openclaw --mode unsupported
```

Uninstall:

```bash
openclaw plugins disable context-shunt
openclaw plugins uninstall context-shunt
rm -rf ~/.cache/context-shunt          # private snapshots and spill
# then remove the plugins.entries.context-shunt block from openclaw.json
```

## What gets written where

| Path | Contents | Lifetime |
| --- | --- | --- |
| `spill_dir` (default `~/.cache/context-shunt`) | snapshots and spilled results, directory `0700`, files `0600` | cleared on session end; 1 hour TTL |
| `reports/` in this repo | non-sensitive verification reports | gitignored, safe to delete |

Nothing is written inside a workspace root, and no source file is modified — `unit
no-writes` compares the source tree's hashes, permissions and filenames before and after a
full run and also exercises a read-only source.

Deleting a spill file is deletion, not secure erasure. Treat the cache as sensitive for as
long as it exists, and put it on the same trust boundary as the sources it mirrors.

## Optional gates

```bash
# Live reader eval. Needs a bridge that serves gpt-5.6-luna.
CONTEXT_SHUNT_LUNA_EVAL=1 \
CONTEXT_SHUNT_LUNA_BRIDGE=your_module:your_callable \
  ./scripts/verify eval luna

# Deterministic benchmark half (no provider needed).
./scripts/verify benchmark core

# Everything, in order. Expect exit 2 until live Luna access exists.
./scripts/verify release all
```

The bridge callable receives `system`, `user`, `model`, `max_output_tokens` and
`timeout_ms` as keyword arguments and returns
`{"text", "model", "input_tokens", "output_tokens"}`. If it reports a model other than
`gpt-5.6-luna`, the call fails with `MODEL_ERROR` rather than being accepted.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Plugin refuses to load, log says `WRITER_UNSUPPORTED_CONFIGURATION` | `writer.enabled: true` is set. v1 has no writer and refuses the flag instead of ignoring it. |
| Plugin refuses to load, log says `MODEL_NOT_ALLOWED` | `reader.model` is not `gpt-5.6-luna`. |
| Plugin refuses to load, log says `LIMIT_MAY_ONLY_NARROW` | A `limits` value is wider than the contract default. |
| Every source is rejected with `UNSAFE_SOURCE` | `workspace_roots` does not contain the path, or the path is a symlink, a hardlink, or matches the secret policy. |
| Reader returns `MODEL_ERROR` with an empty answer | The host cannot serve `gpt-5.6-luna`. The gate keeps working. |
| A large read is not blocked | The tool id is outside the covered list in [`capability-matrix.md`](capability-matrix.md). Disable uncontrolled read tools at the host. |
