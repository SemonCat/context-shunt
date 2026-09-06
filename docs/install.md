# Install, verify, uninstall

Two adapters, one shared contract. Pick the host you run; both cores are checked for
contract parity against the same fixtures in
[`contracts/v1`](../contracts/v1).

Before installing, read [`capability-matrix.md`](capability-matrix.md): it says which
modes each host actually supports and why the optional Suma post-tool mode is off.

## Prerequisites

| | Version |
| --- | --- |
| Python (Hermes adapter) | 3.11 or newer |
| Node (OpenClaw adapter) | **22.22.3 or newer** — the store uses `node:sqlite`, and this is also OpenClaw's own floor |
| Hermes | compatibility verified against `hermes-agent` 0.18.2, with plugin hooks enabled |
| OpenClaw | compatibility verified against `openclaw` 2026.9.2, with native plugins enabled |
| Reader model | a host model bridge; `gpt-5.6-luna` is the default and is configurable |

Without a model bridge the local gate, `context_shunt_inspect` and `context_shunt_stats`
all still work — none of those calls a model. Only `context_shunt_read` needs one.

Other host versions are not claimed compatible. Rerun the real local integration gate
and review the host hook/model APIs before upgrading either host.

## Build and self-check first

```bash
git clone <your-fork> context-shunt && cd context-shunt

# Python core
python3 -m venv .venv
./.venv/bin/pip install -e 'packages/core-py[dev]'

# TypeScript core and the OpenClaw adapter
npm install

# Every deterministic gate. No host and no provider needed - but both of the
# steps above are: the store gate writes a store with one core and reads it back
# with the other, which is what proves they agree on the normative DDL.
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

The `llm` policy beside the plugin's `config` block is required. OpenClaw independently
authorizes model overrides and completion targets; the example grants this plugin only
`openai/gpt-5.6-luna`. Without that policy the reader fails closed with `MODEL_ERROR`.

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
| `<cache>/store.sqlite3` | authorization metadata only: opaque handle ids, digested scope, TTL, quotas, refcounts, disclosure totals, bounded metrics. **No paths, questions, answers or previews.** | rows removed by TTL, sweep, or a real session boundary |
| `<cache>/blobs/<aa>/<bb>/<sha256>.bin` | the withheld payload bytes, content-addressed, `0600` | refcounted; deleted when no live handle references it |
| `<cache>/tmp/` | in-flight temp files; no live handle ever points here | cleared by startup recovery |
| `reports/` in this repo | non-sensitive verification reports | gitignored, safe to delete |

`<cache>` is `cache_dir` (or the legacy `spill_dir`), defaulting to `~/.cache/context-shunt`
or `$CONTEXT_SHUNT_CACHE`. Every directory is `0700`. The cache root is refused if it
resolves inside a workspace root.

Nothing is written inside a workspace root, and no source file is modified — `unit
no-writes` compares the source tree's hashes, permissions and filenames before and after a
full run and also exercises a read-only source.

Deleting a payload file is deletion, not secure erasure. Treat the cache as sensitive for as
long as it exists, and put it on the same trust boundary as the sources it mirrors.
[`security.md`](security.md) has the full retention picture.

## Cleaning up

The store cleans up on its own: handles expire after an hour by default, every turn takes an
opportunistic sweep, and a real session boundary revokes the scope's handles and drops the
content they held. Startup recovery additionally removes staged temp files and any content
file left without a row by a crash.

To clear everything by hand, stop the host and remove the cache root:

```bash
rm -rf ~/.cache/context-shunt        # or your configured cache_dir
```

That is safe at any time: a missing store is recreated on next use, and nothing in it is
required to read a source again. There is no state to preserve.

To inspect what is there without reading any content:

```bash
sqlite3 ~/.cache/context-shunt/store.sqlite3 \
  "SELECT COUNT(*) AS handles FROM handles WHERE revoked = 0;
   SELECT COUNT(*) AS blobs, SUM(bytes) FROM blobs;"
```

## Migrating from a pre-1.1 cache

Revision 1.0 wrote loose spill files at `<cache>/<32-hex>/<sha256>.spill`. Those files are
**never imported as authorized handles** — an unauthenticated file on disk is not a
capability, and promoting one would create a handle nobody ever authorized.

They are simply inert. Nothing reads them, and they do not count toward any quota. To remove
them:

```bash
# Report first.
./.venv/bin/python -c "
from context_shunt.store import SnapshotStore
s = SnapshotStore('$HOME/.cache/context-shunt')
print(s.legacy_artifact_count(), 'legacy artifacts')"

# Then remove.
./.venv/bin/python -c "
from context_shunt.store import SnapshotStore
s = SnapshotStore('$HOME/.cache/context-shunt')
print(s.purge_legacy_artifacts(), 'removed')"
```

Or just delete the cache root, which is equivalent and simpler.

A store created by a **different DDL revision** is refused at open time with
`STORE_FAILED / DDL_VERSION_MISMATCH` rather than migrated by guesswork. If you see that,
the supported path is to remove the cache root and let the current revision recreate it; no
data that matters is lost, because the store only ever holds a cache of content that can be
re-read from its source.

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

The bridge callable receives `system`, `user`, `provider`, `model`, `max_output_tokens` and
`timeout_ms` as keyword arguments and returns a mapping. Only `text` is required; every
provenance and usage field is optional, and an **absent field means "the host does not
expose this"** rather than a default that would overstate what is known:

| Key | Meaning |
| --- | --- |
| `text` | the completion. Required. |
| `resolved_provider` / `resolved_model` | what the host says it selected. Omit if the host does not expose its selection. |
| `reported_provider` / `reported_model` | what the **provider** says generated the tokens. Omit if it does not report one. |
| `provider_confirms_generation` | `true` only if you can show the reported value came from the provider, not from the host echoing your request. When `false`, attribution is reported `unverified` — never `actual`. |
| `input_tokens` / `output_tokens` / `cache_tokens` | omit when not reported. **Do not send `0` to mean unknown.** |
| `usage_exact` | `true` only when those counts came from the provider. |
| `fallback_used` | `true` if an availability fallback produced this result. |

A reported or resolved value that contradicts the requested model fails the call with
`MODEL_ERROR` rather than being accepted.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Plugin refuses to load, log says `WRITER_UNSUPPORTED_CONFIGURATION` | `writer.enabled: true` is set. v1 has no writer and refuses the flag instead of ignoring it. |
| Plugin refuses to load, log says `BAD_ATTRIBUTION_POLICY` | `reader.attribution_policy` is not `allow_unverified` or `require_match`. |
| Plugin refuses to load, log says `LIMIT_MAY_ONLY_NARROW` | A `limits` value is wider than the contract default. |
| Every source is rejected with `UNSAFE_SOURCE` | `workspace_roots` does not contain the path, or the path is a symlink, a hardlink, or matches the secret policy. |
| Reader returns `MODEL_ERROR` with an empty answer | The host cannot serve the configured model, or reported one that contradicts it. The gate, `inspect` and `stats` keep working. |
| Reader returns `PROVENANCE_UNAVAILABLE` | `reader.attribution_policy: require_match` is set and the host cannot prove which model answered. See [`capability-matrix.md`](capability-matrix.md) for the per-host ceiling. |
| Envelope says `attribution_status: unverified` | Expected on Hermes: the plugin LLM facade cannot be distinguished from an echo of the request. This is reported, not hidden. |
| `inspect` returns `DISCLOSURE_EXHAUSTED` | The cumulative per-source or per-session disclosure ceiling is used up. That is the ceiling working; raise it with `limits.disclosure_max_per_*_bytes` only if you mean to. |
| Store refuses with `DDL_VERSION_MISMATCH` | The cache was created by a different revision. Remove the cache root; see "Migrating from a pre-1.1 cache" above. |
| A handle is `SOURCE_EXPIRED` sooner than expected | The default TTL is one hour, and a real session boundary revokes early. A per-turn boundary does not. |
| A large read is not blocked | The tool id is outside the covered list in [`capability-matrix.md`](capability-matrix.md). Disable uncontrolled read tools at the host. |
