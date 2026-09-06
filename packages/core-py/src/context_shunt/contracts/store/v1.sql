-- context-shunt snapshot store, DDL revision 1.
--
-- This file is normative. Both language cores execute it verbatim to create or verify
-- their store; neither core may embed an equivalent CREATE TABLE of its own. A
-- cross-language interoperability test opens the same file with both cores.
--
-- What lives here and what does not
-- ---------------------------------
-- SQLite owns authorization only: opaque handle identity, session scope and generation,
-- TTL, quotas, content refcounts, disclosure accounting and cleanup state.
--
-- SQLite never stores, and no column may ever be added to store: a source path, a
-- question, an answer, a quote, a payload preview, a provider error body, a filesystem
-- path of any kind, or a model/provider name used as a key. Immutable raw payloads live
-- in content-addressed private files whose location is derived from `blobs.hash`
-- internally; the derivation is not recorded and no path is exposed.
--
-- Identifier and value formats
-- ----------------------------
--   handles.handle_id   'src_' + 16 lowercase hex characters (opaque, fixed format)
--   scopes.scope_id     'scp_' + 32 lowercase hex characters (opaque, fixed format)
--   blobs.hash          64 lowercase hex characters, the full SHA-256 of the payload
--   *_ms columns        integer milliseconds since the Unix epoch, UTC, subject to the
--                       monotonic high-water rule in store_metadata.clock_high_water_ms
--
-- Readability is a SQL predicate, never file existence: a handle is readable only while
--   revoked = 0 AND expires_at_ms > :now AND scope.closed_at_ms IS NULL
--   AND scope.generation = :generation
-- so an expired, revoked, closed-scope or stale-generation handle is unreadable the
-- instant the predicate stops holding, whether or not physical cleanup has run.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Store-wide singleton values. `key` is a closed enum maintained by the cores:
--   ddl_version           integer, must equal the DDL revision the core supports
--   store_id              opaque store identity, 32 lowercase hex characters
--   clock_high_water_ms   highest timestamp ever observed; wall-clock readings below it
--                         are raised to it so a clock rollback cannot revive a handle
--   cursor_key            per-store random key for continuation-cursor authentication
CREATE TABLE IF NOT EXISTS store_metadata (
    key   TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
) STRICT;

-- One row per trusted (host, profile, principal, session, generation) tuple. All five
-- components come from the host, never from a payload or a tool argument, and they are
-- stored as opaque lowercase-hex digests so no session name, account id or profile label
-- is retained. A new generation makes every earlier handle unreplayable.
CREATE TABLE IF NOT EXISTS scopes (
    scope_id      TEXT PRIMARY KEY NOT NULL,
    host          TEXT    NOT NULL,
    profile       TEXT    NOT NULL,
    principal     TEXT    NOT NULL,
    session       TEXT    NOT NULL,
    generation    INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    closed_at_ms  INTEGER,
    UNIQUE (host, profile, principal, session, generation)
) STRICT;

-- One row per distinct payload content. Content-addressed dedupe: two handles over
-- identical bytes share one row and one file. `refcount` is only ever changed inside the
-- same transaction that publishes or removes a handle. Deletion is mark-then-sweep:
-- `pending_delete` is set in one transaction, the file is unlinked outside every lock,
-- and the row is removed in a second transaction that re-verifies
-- `refcount = 0 AND pending_delete = 1`.
CREATE TABLE IF NOT EXISTS blobs (
    hash           TEXT PRIMARY KEY NOT NULL,
    bytes          INTEGER NOT NULL,
    media_type     TEXT    NOT NULL,
    line_count     INTEGER NOT NULL,
    refcount       INTEGER NOT NULL DEFAULT 0,
    pending_delete INTEGER NOT NULL DEFAULT 0,
    created_at_ms  INTEGER NOT NULL,
    CHECK (bytes >= 0),
    CHECK (refcount >= 0),
    CHECK (pending_delete IN (0, 1)),
    CHECK (length(hash) = 64)
) STRICT;

-- One row per authorized handle. `kind` records how the payload was captured so a
-- legacy artifact can never be promoted into an authorized handle:
--   'shunted_read'  a read the gate blocked, captured because it was blocked
--   'spilled_tool'  an eligible oversized post-tool candidate (capability-gated)
-- `baseline_credited` makes the withheld-source baseline a one-time credit: the first
-- withholding operation credits it, every later operation over the same snapshot records
-- a zero credit and still records its own overhead.
CREATE TABLE IF NOT EXISTS handles (
    handle_id         TEXT PRIMARY KEY NOT NULL,
    scope_id          TEXT    NOT NULL REFERENCES scopes (scope_id),
    blob_hash         TEXT    NOT NULL REFERENCES blobs (hash),
    kind              TEXT    NOT NULL,
    internal          INTEGER NOT NULL DEFAULT 0,
    generation        INTEGER NOT NULL,
    created_at_ms     INTEGER NOT NULL,
    expires_at_ms     INTEGER NOT NULL,
    revoked           INTEGER NOT NULL DEFAULT 0,
    baseline_credited INTEGER NOT NULL DEFAULT 0,
    disclosed_bytes   INTEGER NOT NULL DEFAULT 0,
    CHECK (kind IN ('shunted_read', 'spilled_tool')),
    CHECK (internal IN (0, 1)),
    CHECK (revoked IN (0, 1)),
    CHECK (baseline_credited IN (0, 1)),
    CHECK (disclosed_bytes >= 0)
) STRICT;

CREATE INDEX IF NOT EXISTS handles_by_scope ON handles (scope_id, revoked, expires_at_ms);
CREATE INDEX IF NOT EXISTS handles_by_blob  ON handles (blob_hash);
CREATE INDEX IF NOT EXISTS handles_by_expiry ON handles (expires_at_ms);

-- Append-only record of bytes disclosed into the main model context. `kind` is a closed
-- enum ('lines', 'bytes', 'search'). No selector text, needle or extracted content is
-- recorded: only how much was disclosed, to which handle, and when. The cumulative
-- ceiling is enforced by check-and-increment inside the read authorization transaction.
CREATE TABLE IF NOT EXISTS disclosure_events (
    event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id  TEXT    NOT NULL REFERENCES scopes (scope_id),
    handle_id TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    bytes     INTEGER NOT NULL,
    at_ms     INTEGER NOT NULL,
    CHECK (kind IN ('lines', 'bytes', 'search')),
    CHECK (bytes >= 0)
) STRICT;

CREATE INDEX IF NOT EXISTS disclosure_by_scope ON disclosure_events (scope_id, at_ms);

-- One row per shunt operation. Every column is bounded non-content metadata: closed
-- enums, byte counts and token counts. `main_context_tokens_saved` and
-- `net_tokens_saved` are signed. A NULL token column means "not reported" and must never
-- be rendered as zero. `*_method` records how a token count was obtained ('exact' from
-- provider-reported usage, or a named deterministic estimate such as 'bytes_div_4').
CREATE TABLE IF NOT EXISTS accounting_events (
    operation_id               TEXT PRIMARY KEY NOT NULL,
    scope_id                   TEXT    NOT NULL REFERENCES scopes (scope_id),
    kind                       TEXT    NOT NULL,
    status                     TEXT    NOT NULL,
    code                       TEXT    NOT NULL,
    raw_input_bytes            INTEGER NOT NULL,
    raw_input_baseline_tokens  INTEGER,
    baseline_kind              TEXT    NOT NULL,
    baseline_method            TEXT    NOT NULL,
    baseline_credit_tokens     INTEGER NOT NULL,
    main_model_envelope_bytes  INTEGER NOT NULL,
    main_model_envelope_tokens INTEGER NOT NULL,
    envelope_token_method      TEXT    NOT NULL,
    reader_input_tokens        INTEGER,
    reader_output_tokens       INTEGER,
    reader_cache_tokens        INTEGER,
    reader_token_method        TEXT    NOT NULL,
    attempts_started           INTEGER NOT NULL,
    attempts_usage_complete    INTEGER NOT NULL,
    delivery_boundary          TEXT    NOT NULL,
    main_context_tokens_saved  INTEGER NOT NULL,
    net_tokens_saved           INTEGER NOT NULL,
    at_ms                      INTEGER NOT NULL,
    CHECK (kind IN ('gate_block', 'capture', 'read', 'refined_read', 'inspect', 'stats', 'spill')),
    CHECK (status IN ('ok', 'partial', 'blocked', 'error')),
    CHECK (baseline_kind IN ('full_payload_counterfactual', 'host_truncated_observed', 'none')),
    CHECK (baseline_method IN ('exact', 'bytes_div_4', 'unknown')),
    CHECK (envelope_token_method IN ('exact', 'bytes_div_4', 'unknown')),
    CHECK (reader_token_method IN ('exact', 'bytes_div_4', 'unknown', 'not_applicable')),
    CHECK (delivery_boundary IN ('envelope', 'extraction', 'pointer', 'block_message', 'none')),
    CHECK (raw_input_bytes >= 0),
    CHECK (attempts_started >= 0),
    CHECK (attempts_usage_complete >= 0)
) STRICT;

CREATE INDEX IF NOT EXISTS accounting_by_scope ON accounting_events (scope_id, at_ms, operation_id);

-- Temp files that were created but whose transaction never committed. Recovery unlinks
-- them on the next startup sweep. Only the derived blob hash and a bounded random
-- suffix are recorded, never a path.
CREATE TABLE IF NOT EXISTS orphan_temps (
    temp_id       TEXT PRIMARY KEY NOT NULL,
    blob_hash     TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    CHECK (length(blob_hash) = 64)
) STRICT;
