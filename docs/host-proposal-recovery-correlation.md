# Proposal: host-side recovery correlation for abandoned pointers

**Status:** proposal, not implemented. Nothing in this document is installed anywhere; it
describes an optional Hermes host-side change for the host maintainer to review and decide
on. Context Shunt's own repository change for this finding is test coverage only - see
`packages/core-py/tests/test_gate_session4_unread_pointer_requery.py` and
`packages/core-ts/test/session4-unread-pointer-requery.test.ts` - because the gap this
document describes is not fixable from inside the capture hook at all.

## The finding

Session 4 of the 2026-09-14 audit (`cron_2cf04e39ace6_20260914_061013`) spilled two GBrain
search results to pointers (message `960329`, ~34.9KB; message `960357`, ~17.6KB). The
reader was never invoked against either pointer. Instead, the caller reissued two smaller,
more targeted GBrain searches of its own (message `960331`, ~10.8KB wire; message `960359`,
~9.1KB) that landed under the tool-result-capture threshold and passed straight through
unmodified. The scope's ledger reported `net_tokens_saved=12516`.

That number is arithmetically correct for what Shunt observed, and every credit behind it
already carries the honest `full_payload_counterfactual` label rather than a claim of
confirmed use (`accounting.BaselineKind` has no member that could assert a realized
saving - see the enum-lock test in the same test file). But read in isolation, `net12516`
invites the wrong conclusion: that this scope's information need was resolved for 12,516
fewer tokens than a full read would have cost. What actually happened, on the evidence
available, is that the caller abandoned both pointers and paid for a second round of tool
calls to get what it needed some other way. That second round is real cost, and it is
invisible to Shunt: a passthrough tool result below `max_tool_result_bytes` produces no
envelope, so `post_tool_result` never calls into the accounting store for it at all
(`SpillEngine.evaluate`'s `action="passthrough"` branches; the `if outcome.envelope is not
None:` guard in `session.py`/`session.ts`). This is not a bug in the arithmetic. It is a
visibility boundary: Shunt's capture hook sees individual tool results, one at a time, with
no memory of *why* a later one arrived or whether it is causally connected to an earlier
spill.

## Why this can't be closed inside the capture hook

Correlating "the caller abandoned pointer A" with "and then issued search B instead" needs
information the hook does not have and should not guess at:

- **Causality, not just proximity.** A second GBrain search in the same scope shortly after
  an unread pointer is *consistent with* a recovery, but proximity in time is not proof. The
  caller may have simply asked an unrelated second question. Treating every nearby tool call
  as a correlated recovery would produce false positives at least as often as it caught real
  ones.
- **The signal lives in the agent's own reasoning/orchestration layer, not in tool-result
  bytes.** Only Hermes (or whatever host schedules and threads tool calls) knows whether a
  later call was issued *because* the earlier pointer was judged unusable, too broad, or the
  reader budget looked too expensive to spend. Shunt sees two independent `post_tool_result`
  invocations with no shared context beyond a session id.
- **A wrong correlation is worse than no correlation.** Silently debiting a scope's net
  figure for a "recovery" that was not actually one would be exactly the kind of fabricated
  precision this project deliberately avoids elsewhere (see `docs/limitations.md`'s
  "counterfactual is labelled as one"). Getting this right needs a host-side signal that
  Hermes is willing to stand behind, not a heuristic Shunt invents from spill timing alone.

## The proposed host-side change

If Hermes' own orchestration layer already knows (or could cheaply record) that a tool call
was issued as a direct consequence of a caller giving up on an unread Shunt pointer, it could
pass that fact through the existing capture surface rather than Shunt reconstructing it:

1. When the host's agent loop reissues a tool call using the same tool id within the same
   session, after a prior call to that tool id produced a Shunt pointer that was never the
   subject of a `context_shunt_read` or `context_shunt_inspect` call, tag the reissued call's
   `post_tool_result` invocation with an optional `recovered_pointer_source_id` (or similar)
   referencing the abandoned handle. This is additive to the existing `post_tool_result`
   signature (`internal_source_id`, `upstream_truncated` are precedent for exactly this kind
   of optional, host-supplied provenance hint) and requires no change to any accounted
   number.
2. Shunt would record that hint as a new, purely informational column on the affected spill's
   accounting row (e.g. `recovered_by_operation_id`), never altering `baseline_credit_tokens`
   or `net_tokens_saved` for either operation - the two formulas stay exactly as specified.
   The only change is that a scope-level report could then say, truthfully, "spill S's
   counterfactual credit was followed by a same-tool recovery call within this scope" rather
   than remaining silent about it.
3. A scope-level summary (a new, separate `stats` field, not a change to the existing
   `totals`) could then surface a count of "spills with no read/inspect and no recorded
   recovery" versus "spills with no read/inspect but a recorded recovery," so a caller
   reading `net_tokens_saved=12516` can also see, honestly, how much of that credit is
   contested.

## What this proposal deliberately does not ask for

- No change to `main_context_tokens_saved` or `net_tokens_saved` as currently specified.
  Those formulas remain exactly `baseline_credit_tokens - main_model_envelope_tokens` and
  the net thereof; this proposal is additive metadata, not a revision to the accounting
  contract.
- No attempt by Shunt to infer recovery from timing or tool name alone. The signal, if it
  exists at all, has to come from the host's own knowledge of why it made the second call.
- No requirement that Hermes implement this. The absence of the signal is a real,
  documented limitation (see `docs/limitations.md`), not a defect that blocks the current
  milestone. This document exists so the choice to add the signal - and the judgment calls
  in step 1 above about what counts as "the same tool id" and "reissued" - is made
  deliberately by whoever owns Hermes' orchestration layer, reviewed like any other host
  change, not folded into an installed patch from this audit.
