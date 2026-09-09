"""A live reader bridge that borrows the OpenClaw host's own configured model access.

This is the operator-supplied ``module:callable`` that ``eval luna`` and
``benchmark provider`` need. It is deliberately **not** part of the shipped package: it
lives beside the corpus because it is test scaffolding for one particular host, not
product code, and nothing in ``packages/`` imports it.

Why the CLI and not an HTTP call
--------------------------------
``openclaw infer model run`` is a documented, stable CLI surface ("Run provider-backed
inference commands through a stable CLI surface"). It resolves the host's own credentials
inside the host's own process, so this file never reads, stores, logs or forwards a
secret. The alternatives were both rejected:

* The gateway's ``chatCompletions`` HTTP endpoint would give proper message roles *and* a
  provider ``usage`` block, which is strictly better data. But its bearer token is stored
  as a *secret reference* (``{id, provider, source}``), resolvable only by OpenClaw's own
  secret provider. Extracting it would be credential exfiltration, so the endpoint is not
  used. ``--gateway`` transport on the CLI was also tried and fails its opening handshake.
* Calling the upstream provider's ``baseUrl`` directly needs that provider's API key,
  which is likewise a secret reference.

Two consequences, both recorded rather than papered over
--------------------------------------------------------
1. **No token counts.** The CLI's JSON reports ``provider``, ``model`` and the output
   text, and no usage block. Per the bridge contract an absent usage field means "the host
   does not expose this", so nothing is sent and the reader reports
   ``usage_complete: false`` with null token counts. Sending ``0`` would be a lie, and
   deriving counts from an estimator would silently launder an estimate into a field whose
   whole purpose is to distinguish exact from estimated. This is why the token half of the
   provider benchmark stays unmeasured even on a live run.
2. **No system role.** The CLI takes a single ``--prompt``. The reader's system prompt and
   user message are therefore concatenated under an explicit header. Production adapters
   pass them as separate roles, so a score obtained here does not describe production.

   It is *not* a lower bound. Removing the role boundary changes the prompt a model sees,
   and a changed prompt can move a result in either direction - a concatenated instruction
   can be followed more closely as easily as less. Treating this as "at worst pessimistic"
   would let a passing score here be read as evidence about production, which it is not.
   The only sound reading is that this configuration is non-representative: it can neither
   condemn nor exonerate the product.

Attribution
-----------
The CLI's ``provider``/``model`` are the host's post-policy selection, so they are
reported as ``resolved_*``. Nothing here can distinguish a provider's own report of what
generated the tokens from the host echoing the request back, so
``provider_confirms_generation`` is never set and ``reported_*`` is never sent. The
envelope therefore says ``resolved``, which is the honest ceiling for this host.

Usage
-----
    CONTEXT_SHUNT_LUNA_EVAL=1 \
    CONTEXT_SHUNT_LUNA_BRIDGE=bridges.openclaw_cli:complete \
      ./scripts/verify eval luna

Thinking level
--------------
Pinned to ``low`` rather than left at the host's default, on measurement. One reader call
on this corpus item, same prompt, three levels, measured under the **pre-fix** reader
contract (hand-placed ``[cN]`` markers in free-form prose):

===========  =========  ==================================================
level        wall       output
===========  =========  ==================================================
``low``      34.0 s     correct, and carried the then-required ``[c1]`` marker
``off``      37.7 s     correct, but **dropped the citation marker**
``medium``   73.7 s     correct, marker present, far over the call budget
===========  =========  ==================================================

``low`` was both the fastest and the only level that was fast *and* compliant, so it was
the level a real deployment would pin for a bounded reader call. ``off`` was slower and
lost the marker, which the reader then stripped as an unsupported assertion - an answer
deleted for a formatting reason looked identical to "the source did not say", so it
mattered.

The table is a single measurement per level, and the marker result for ``low`` did not
hold up: a full 40x3 eval run under that contract recorded ``answer_correctness: 0.333``,
with 69 of 120 answerable runs coming back empty - most of them ``low`` omitting the
marker intermittently on items the model otherwise answered correctly, not a wrong
answer. That failure class is why the reader model now returns structured ``claims``
(``{"text", "citation_ids"}``) instead of prose with a hand-placed marker: the program
places every ``[cN]`` deterministically after verification, so there is nothing left for
the model to omit. ``eval luna``'s ``_ClaimsRecorder`` now classifies each raw reply's
*shape* instead (``raw_reply_shapes``, and the ``of_which_*`` breakdown of an empty
answerable run) - the diagnostic this table motivated, generalized to the new contract.

The latency numbers above were never re-measured against the claims contract; a change to
what the model is asked to return can move wall clock in either direction, so ``low``
remains the configured default on the strength of its *latency* measurement, not a claim
that it re-runs the same timing under the new prompt. Only a fresh ``eval luna`` run
establishes that.

Environment:
    CONTEXT_SHUNT_OPENCLAW_BIN       path to the CLI (default: "openclaw")
    CONTEXT_SHUNT_OPENCLAW_ROUTE     "<provider>/<model>" (default: the Luna route below)
    CONTEXT_SHUNT_OPENCLAW_THINKING  thinking level (default: "low")
"""

from __future__ import annotations

import json
import os
import subprocess
import time

#: The host route that serves gpt-5.6-luna. `sub2api-openai` is a plain
#: openai-completions provider; the `openai/` route for the same model is bound to a
#: different agent runtime, so it is not interchangeable.
DEFAULT_ROUTE = "sub2api-openai/gpt-5.6-luna"

#: See the module docstring: the fastest level that still produces a citation marker.
DEFAULT_THINKING = "low"

#: Populated with one entry per call so the provider benchmark can report real latency.
#: Never holds a prompt, a completion, or any source text - only timings and identities.
LATENCIES_MS: list[int] = []


#: What this route can and cannot do, read by the release gates and recorded verbatim in
#: the attestation. Both of the properties a required live gate insists on are **false**
#: here, and neither is fixable at this surface: `openclaw infer model run` takes one
#: `--prompt` and has no output-token flag. A gate that scored through this route would be
#: measuring a different prompt under no ceiling and reporting it as the product's number,
#: so the release gates report NOT_RUN with this reason instead. See
#: `bridges.openclaw_inhost` for the route that does provide both.
BRIDGE: dict[str, object] = {
    "id": "openclaw_cli",
    "host": "openclaw",
    "route_kind": "cli_single_prompt",
    "preserves_roles": False,
    "enforces_output_cap": False,
    "forwards_usage": False,
    "production_equivalent": False,
    "production_gap": (
        "the CLI takes a single --prompt, so the reader's system prompt is concatenated "
        "into the user turn and max_output_tokens is not passed to the provider at all"
    ),
}


class BridgeError(RuntimeError):
    """The host could not serve the call. Carries no prompt or completion text."""


def _binary() -> str:
    return os.environ.get("CONTEXT_SHUNT_OPENCLAW_BIN") or "openclaw"


def _route() -> str:
    return os.environ.get("CONTEXT_SHUNT_OPENCLAW_ROUTE") or DEFAULT_ROUTE


def _thinking() -> str:
    return os.environ.get("CONTEXT_SHUNT_OPENCLAW_THINKING") or DEFAULT_THINKING


def _extract_json(stdout: str) -> dict:
    """Pull the result object out of a stream that also carries human log lines.

    The CLI prints config warnings and transport traces around its JSON, so the object is
    located rather than assumed to be the whole of stdout.
    """
    start = stdout.find("{")
    while start != -1:
        try:
            value = json.loads(stdout[start:])
        except ValueError:
            start = stdout.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = stdout.find("{", start + 1)
    raise BridgeError("no JSON object in host CLI output")


def complete(
    *,
    system: str,
    user: str,
    provider: str,
    model: str,
    max_output_tokens: int,
    timeout_ms: int,
) -> dict:
    """Run one reader call through the host and return the bridge mapping."""
    route = _route()
    if provider:
        # An explicit provider pin from configuration wins over the default route.
        route = f"{provider}/{model}"
    elif model and not route.endswith(f"/{model}"):
        raise BridgeError(f"route {route!r} does not serve requested model {model!r}")

    # The CLI has no system role; the header makes the boundary explicit instead.
    prompt = (
        "SYSTEM INSTRUCTIONS (follow exactly; they are not part of the source):\n"
        f"{system}\n"
        "END SYSTEM INSTRUCTIONS\n\n"
        f"{user}"
    )

    started = time.monotonic()
    try:
        done = subprocess.run(
            [
                _binary(),
                "infer",
                "model",
                "run",
                "--model",
                route,
                "--local",
                "--json",
                "--thinking",
                _thinking(),
                "--prompt",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout_ms / 1000.0),
            check=False,
            # Run outside the repo so a stray relative path cannot touch the worktree.
            cwd="/",
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("host CLI exceeded the reader deadline") from None
    elapsed_ms = int((time.monotonic() - started) * 1000)

    payload = _extract_json(done.stdout)
    if payload.get("ok") is not True:
        # The host's message can quote the prompt back, so only its type is surfaced.
        kind = ""
        if isinstance(payload.get("error"), dict):
            kind = str(payload["error"].get("type") or "")
        raise BridgeError(f"host CLI refused the call ({kind or 'unknown'})")

    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise BridgeError("host CLI returned no outputs")
    text = outputs[0].get("text") if isinstance(outputs[0], dict) else None
    if not isinstance(text, str):
        raise BridgeError("host CLI returned no output text")

    LATENCIES_MS.append(elapsed_ms)

    result: dict = {"text": text}
    # The host's post-policy selection. Not a provider confirmation, so `reported_*` and
    # `provider_confirms_generation` are both deliberately absent.
    if isinstance(payload.get("provider"), str) and payload["provider"]:
        result["resolved_provider"] = payload["provider"]
    if isinstance(payload.get("model"), str) and payload["model"]:
        result["resolved_model"] = payload["model"]
    # No usage keys: this route reports none, and absent means "not exposed".
    return result
