# Archived Hermes 0.21.3 patch proposal — superseded

> Superseded on 2026-09-17. The exact unmodified host exposes sufficient scoped evidence
> through its official post-middleware `pre_api_request` hook. Do not apply this patch or
> build a modified Hermes image for the context-shunt candidate. See
> [`../capability-matrix.md`](../capability-matrix.md) and the no-core exact-image probe.

Status: **proposal only**. It was not applied to installed source, built into an image, or
enabled in production. Ruby owns approval, image construction, canary, and rollback.

## Immutable inputs

| Input | Immutable identity |
| --- | --- |
| Current image | `context-shunt/hermes@sha256:9b221081de712c5dfbda93ae10e357a8fc336fcb5f96efaaf7015e646b4bc0e8` |
| Image-created time | `2026-09-15T11:45:50.395950096Z` |
| Hermes Agent | `0.21.3` |
| `/opt/hermes/model_tools.py` | `c99620c824ab59f341ac7d0e22cde016b0c469d0643e7a5a5a82e0d63176e4b5` |
| `/opt/hermes/agent/agent_init.py` | `d1a1df8dc03a1381a9fd7591d4293e912fb1cb0fa2c1370f8c72a7500acb824a` |
| `/opt/hermes/agent/tool_executor.py` | `9634ebdc01b12591753fa9a1653f4aa8937edde7130175bbd69be8a0e5895ab7` |
| `/opt/hermes/tools/tool_search.py` | `1875c04284090cf9b6d9d96b0faa3072c7bbb65417815208e6e3a43b3ca2fdcb` |
| `/opt/hermes/hermes_cli/lifecycle.py` | `b421f36d8e68c79da1367da571493892beee763f27d44ea83944233bfd91af75` |
| Proposal patch | `bb2c69954aad3c045ed4bf62fad003148006bb63d90c7fa5bbc03ac8fda9f8e3` |
| Exact patched `model_tools.py` | `738e5ede949a93ab7632da544f17422780fac7ecd7ea5a4749158a9de3519897` |
| Exact-host probe | `b45e3f7b4fc80e5eb18a902a17aee87323655eb83f42e14a123436fa2a7d7b6f` |

The proposal changes one third-party file only. Its unified diff is 93 lines and adds one
immutable invocation descriptor at the already-scoped dispatch boundary. It neither adds a
tool nor widens a caller's direct or deferred tool universe.

## Why supported hooks/config are insufficient

Read-only source inspection and the exact unmodified-image red control exhausted the
available surfaces:

- `agent/agent_init.py:1061-1066` constructs the final model-visible names;
- `agent/tool_executor.py:1536-1551` copies those names and the invocation toolsets;
- `model_tools.py:858-909` retains them through direct and deferred `tool_call` dispatch;
- `tools/tool_search.py:524-530` exposes the supported scoped deferred-name derivation;
- `model_tools.py:834-848,941-945` invokes `transform_tool_result` after dropping those
  facts;
- session-start, pre-LLM, pre-tool, post-tool, plugin configuration, and global tool
  registration expose no equivalent final invocation-scoped consumer descriptor.

Global availability cannot substitute for the missing value: it would publish an
unusable pointer to restricted cron/terminal-only callers. The owned adapter therefore
stays disabled unless both operator attestations are true and still falls back when the
per-invocation descriptor is absent or incomplete.

## Candidate build plan (execute only after Ruby approves the source patch)

Proposed tag:
`context-shunt/hermes:5492046470eb-v2026.9.14-shunt-consumer-v1`.

1. Create an empty, private build directory and copy only
   `/opt/hermes/model_tools.py` out of a stopped container created from the immutable base
   digest. Verify its SHA-256 before editing.
2. Apply `hermes-0.21.3-consumer-capabilities.patch` in that private directory. Refuse any
   offset/fuzz and verify the resulting SHA-256 is exactly
   `738e5ede949a93ab7632da544f17422780fac7ecd7ea5a4749158a9de3519897`.
3. Build with this minimal Dockerfile shape (the operator should create it in the isolated
   build directory, not in the installed Hermes tree):

   ```dockerfile
   FROM context-shunt/hermes@sha256:9b221081de712c5dfbda93ae10e357a8fc336fcb5f96efaaf7015e646b4bc0e8
   COPY --chmod=0644 model_tools.py /opt/hermes/model_tools.py
   RUN python -m py_compile /opt/hermes/model_tools.py && \
       python -c 'from pathlib import Path; import hashlib; p=Path("/opt/hermes/model_tools.py"); assert hashlib.sha256(p.read_bytes()).hexdigest()=="738e5ede949a93ab7632da544f17422780fac7ecd7ea5a4749158a9de3519897"'
   LABEL context-shunt.hermes-base="sha256:9b221081de712c5dfbda93ae10e357a8fc336fcb5f96efaaf7015e646b4bc0e8" \
         context-shunt.consumer-capability-patch="bb2c69954aad3c045ed4bf62fad003148006bb63d90c7fa5bbc03ac8fda9f8e3"
   ```

4. Record the resulting image ID/digest. Do not reuse the incumbent tag and do not restart
   or replace the live container.
5. Copy `/opt/hermes` and its interpreter from a new, isolated candidate container into a
   temporary test root, then run:

   ```sh
   CONTEXT_SHUNT_HERMES_ROOT=/isolated/candidate/opt/hermes \
   CONTEXT_SHUNT_HERMES_PYTHON=/isolated/candidate/python \
     ./scripts/verify integration hermes --mode post-tool
   ```

   The probe is synthetic, makes zero provider calls, requires Hermes 0.21.3 and the exact
   patched source hash, and cleans up its temporary store. Also rerun the unmodified-image
   `--expect missing-seam` control so red and green remain paired.

## Isolated canary and rollback candidate

Keep Shunt capture false and the incumbent compactor active while installing the candidate
only into a separate canary instance with no production traffic. First run direct,
deferred, terminal-only, partial-direct, unscoped, lifecycle, accounting, citation
recovery, and missing-payload cases using synthetic/public inputs. Inspect the actual
model-visible result. Only Ruby may then authorize an atomic canary switch: incumbent off,
Shunt capture on, both host attestations true. Never run both transform listeners as a
redundancy mechanism.

Rollback is one atomic operator action: set Shunt capture false, restore the incumbent
listener, and select the unchanged base image digest above. Stores are independent, so no
migration is needed. Preserve only redacted canary evidence; apply normal retention to
payloads. Re-derive this patch for every Hermes upgrade because hook keyword filtering,
line ownership, deferred catalog construction, and final model-visible scope may change.
