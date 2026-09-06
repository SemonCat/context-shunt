# OpenClaw adapter

Install, uninstall, cleanup and migration instructions live in
[`docs/install.md`](../../docs/install.md). The mode matrix — including the model
attribution ceiling on this host, the session-lifecycle rule, and why the optional
oversized post-tool mode is disabled — is in
[`docs/capability-matrix.md`](../../docs/capability-matrix.md).

This adapter registers three read-only tools and no writer:

| Tool | Returns | Model calls |
| --- | --- | --- |
| `context_shunt_read` | a cited answer — generated text, not source bytes | one per chunk |
| `context_shunt_inspect` | exact snapshot bytes, capped per page and cumulatively | zero |
| `context_shunt_stats` | this session's own token accounting | zero |

Run `./scripts/verify integration openclaw --mode local` against a real host checkout to
check the wiring; without one it reports `NOT_RUN`, never a pass.
