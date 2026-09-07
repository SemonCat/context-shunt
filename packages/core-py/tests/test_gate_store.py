"""unit store: authorization, atomicity, refcounts, corruption and recovery.

Everything here is adversarial against the hybrid store. The store is the only thing
standing between an expired or forged handle and a private payload, so each property is
asserted directly rather than through the reader.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from context_shunt.errors import ShuntError
from context_shunt.limits import DEFAULT_LIMITS, STORE_DDL_PATH, store_ddl
from context_shunt.store import (
    Capture,
    OperationRecord,
    ScopeIdentity,
    SnapshotStore,
)

pytestmark = pytest.mark.gate_store

L = DEFAULT_LIMITS
BODY = b"alpha\nbeta\ngamma\n"


def _identity(session: str = "sess", generation: int = 1) -> ScopeIdentity:
    return ScopeIdentity(
        host="test-host", profile="test", principal="local", session=session, generation=generation
    )


def _capture(data: bytes = BODY, **kw) -> Capture:
    return Capture(
        data=data,
        media_type=kw.pop("media_type", "text/plain"),
        line_count=data.count(b"\n"),
        **kw,
    )


def _store(tmp_path: Path, limits=L, clock=None) -> SnapshotStore:
    return SnapshotStore(tmp_path / "cache", limits, wall_clock_ms=clock)


# -- DDL is normative -------------------------------------------------------


def test_the_ddl_comes_from_the_contract_not_from_the_code():
    """Neither core embeds its own CREATE TABLE; both execute the shared file."""
    ddl = store_ddl()
    assert STORE_DDL_PATH.name == "v1.sql"
    for table in ("store_metadata", "scopes", "blobs", "handles", "disclosure_events"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    source = Path(__file__).resolve().parents[1] / "src" / "context_shunt" / "store.py"
    assert "CREATE TABLE" not in source.read_text()


#: Every column the store is allowed to have. A closed allowlist rather than a substring
#: denylist: this way *adding* a content-bearing column fails the gate, which is the thing
#: that actually needs preventing.
ALLOWED_COLUMNS = {
    "at_ms",
    "attempts_started",
    "attempts_usage_complete",
    "baseline_credit_tokens",
    "baseline_credited",
    "baseline_kind",
    "baseline_method",
    "blob_hash",
    "bytes",
    "closed_at_ms",
    "code",
    "created_at_ms",
    "credited_at_ms",
    "delivery_boundary",
    "disclosed_bytes",
    "envelope_token_method",
    "event_id",
    "expires_at_ms",
    "generation",
    "handle_id",
    "hash",
    "host",
    "internal",
    "key",
    "kind",
    "line_count",
    "main_context_tokens_saved",
    "main_model_envelope_bytes",
    "main_model_envelope_tokens",
    "media_type",
    "name",
    "net_tokens_saved",
    "operation_id",
    "pending_delete",
    "principal",
    "profile",
    "raw_input_baseline_tokens",
    "raw_input_bytes",
    "reader_cache_tokens",
    "reader_input_tokens",
    "reader_output_tokens",
    "reader_token_method",
    "refcount",
    "revoked",
    "scope_id",
    "seq",
    "session",
    "status",
    "temp_id",
    "value",
}


def test_the_schema_stores_no_content_bearing_column(tmp_path):
    """The column set is closed, so a path, question, answer or preview cannot be added."""
    store = _store(tmp_path)
    store.open_scope(_identity())
    conn = sqlite3.connect(store.root / "store.sqlite3")
    columns = set()
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        columns |= {row[1].lower() for row in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    assert columns <= ALLOWED_COLUMNS, sorted(columns - ALLOWED_COLUMNS)
    # The scope components are digests, so no session name or profile label is retained.
    # WAL means fresh rows may still be in the -wal file, so both are scanned.
    store.publish(_identity("a-readable-session-name"), [_capture()])
    for name in ("store.sqlite3", "store.sqlite3-wal"):
        candidate = store.root / name
        if candidate.exists():
            assert b"a-readable-session-name" not in candidate.read_bytes()


def test_a_store_from_a_different_ddl_revision_is_refused(tmp_path):
    store = _store(tmp_path)
    store.open_scope(_identity())
    store.close()
    conn = sqlite3.connect(store.root / "store.sqlite3")
    conn.execute("UPDATE store_metadata SET value = '99' WHERE key = 'ddl_version'")
    conn.commit()
    conn.close()
    with pytest.raises(ShuntError) as exc:
        _store(tmp_path).ddl_version()
    assert exc.value.code == "STORE_FAILED" and exc.value.detail == "DDL_VERSION_MISMATCH"


# -- scoping and replay -----------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "other-host"),
        ("profile", "other-profile"),
        ("principal", "someone-else"),
        ("session", "other-session"),
        ("generation", 2),
    ],
)
def test_a_handle_does_not_replay_into_a_different_scope(tmp_path, field, value):
    store = _store(tmp_path)
    mine = _identity()
    handle = store.publish(mine, [_capture()])[0]
    theirs = ScopeIdentity(
        **{
            **{
                "host": mine.host,
                "profile": mine.profile,
                "principal": mine.principal,
                "session": mine.session,
                "generation": mine.generation,
            },
            field: value,
        }
    )
    store.open_scope(theirs)
    with pytest.raises(ShuntError) as exc:
        store.resolve(theirs, handle.handle_id)
    assert exc.value.code == "SOURCE_EXPIRED"
    # A foreign scope learns nothing beyond "unknown".
    assert exc.value.detail == "UNKNOWN_HANDLE"


def test_expiry_is_a_predicate_not_a_file_deletion(tmp_path):
    """An expired handle is unreadable immediately, before any cleanup runs."""
    clock = {"now": 1_700_000_000_000}
    store = _store(tmp_path, clock=lambda: clock["now"])
    identity = _identity()
    handle = store.publish(identity, [_capture()])[0]
    clock["now"] += (L.store_handle_ttl_seconds + 1) * 1000
    with pytest.raises(ShuntError):
        store.resolve(identity, handle.handle_id)
    # The physical file is still there; readability was decided in SQL.
    assert list((store.root / "blobs").rglob("*.bin"))
    assert store.sweep().expired_handles == 1
    assert list((store.root / "blobs").rglob("*.bin")) == []


def test_a_closed_scope_is_unreadable_even_before_the_sweep(tmp_path):
    store = _store(tmp_path)
    identity = _identity()
    handle = store.publish(identity, [_capture()])[0]
    store.close_scope(identity, revoke=False)
    with pytest.raises(ShuntError) as exc:
        store.resolve(identity, handle.handle_id)
    assert exc.value.detail == "SCOPE_CLOSED"


def test_a_forged_handle_id_resolves_to_nothing(tmp_path):
    store = _store(tmp_path)
    identity = _identity()
    store.publish(identity, [_capture()])
    with pytest.raises(ShuntError):
        store.resolve(identity, "src_" + "f" * 16)


# -- atomic publication -----------------------------------------------------


def test_a_multi_source_batch_publishes_all_or_none(tmp_path):
    store = _store(tmp_path)
    identity = _identity()
    good = [_capture(b"one\n"), _capture(b"two\n"), _capture(b"three\n")]
    assert len(store.publish(identity, good)) == 3
    assert store.stats().handles == 3

    oversized = _capture(b"x" * (L.max_source_bytes + 1))
    with pytest.raises(ShuntError):
        store.publish(identity, [_capture(b"four\n"), oversized])
    # Neither handle from the refused batch exists.
    assert store.stats().handles == 3


def test_a_crash_between_rename_and_commit_leaves_no_usable_handle(tmp_path):
    """Simulated by failing the publication transaction after the files are in place."""
    store = _store(tmp_path)
    identity = _identity()
    real_connect = store._connect  # noqa: SLF001

    class Boom(sqlite3.Error):
        pass

    def explode():
        conn = real_connect()

        class Wrapper:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *args):
                if sql.startswith("INSERT INTO handles"):
                    raise Boom("simulated crash")
                return conn.execute(sql, *args)

        return Wrapper()

    store._connect = explode  # noqa: SLF001
    with pytest.raises(ShuntError):
        store.publish(identity, [_capture(b"never published\n")])
    store._connect = real_connect  # noqa: SLF001

    assert store.stats().handles == 0
    # The failed publish now discards what it staged at the point of failure, rather than
    # leaving it for recovery, so there is nothing left to collect by the time it runs.
    assert list((store.root / "blobs").rglob("*.bin")) == []
    assert store.recover().orphan_blob_files == 0


def test_recovery_still_collects_a_blob_no_handler_could_clean_up(tmp_path):
    """A real crash runs no `except` block, so recovery stays the safety net.

    In-process failure paths clean up after themselves, which is why the crash test above
    finds nothing to recover. A process killed between the rename and the commit leaves a
    content file with no row and no chance to run a handler; that is this.
    """
    store = _store(tmp_path)
    store.open_scope(_identity())
    blobs = store.root / "blobs" / "ab"
    blobs.mkdir(parents=True, exist_ok=True)
    stray = blobs / ("ab" + "c" * 62 + ".bin")
    stray.write_bytes(b"orphaned by a kill -9\n")
    assert store.recover().orphan_blob_files >= 1
    assert not stray.exists()


def test_recovery_clears_staged_temp_files(tmp_path):
    store = _store(tmp_path)
    store.open_scope(_identity())
    stray = store.root / "tmp" / ("a" * 64 + ".deadbeef.part")
    stray.write_bytes(b"half-written payload")
    report = store.recover()
    assert report.removed_temps == 1
    assert not stray.exists()


# -- dedupe, refcounts and deletion races -----------------------------------


def test_identical_content_is_stored_once_and_refcounted(tmp_path):
    store = _store(tmp_path)
    identity = _identity()
    first = store.publish(identity, [_capture()])[0]
    second = store.publish(identity, [_capture()])[0]
    assert first.blob_hash == second.blob_hash
    assert first.handle_id != second.handle_id
    assert store.stats().blobs == 1

    # Dropping one handle must not delete content the other still references.
    store.revoke(identity, first.handle_id)
    assert store.load_payload(store.resolve(identity, second.handle_id)) == BODY
    store.revoke(identity, second.handle_id)
    assert list((store.root / "blobs").rglob("*.bin")) == []


def test_a_republish_during_deletion_keeps_the_content_readable(tmp_path):
    """The deletion race: unlink happens outside the lock, so a new publish must win."""
    store = _store(tmp_path)
    identity = _identity()
    handle = store.publish(identity, [_capture()])[0]
    store.revoke(identity, handle.handle_id)  # marks pending_delete and unlinks
    # A later capture of the same bytes rewrites the content-addressed file.
    republished = store.publish(identity, [_capture()])[0]
    assert store.load_payload(store.resolve(identity, republished.handle_id)) == BODY


def test_content_that_does_not_match_its_hash_fails_closed_without_deleting(tmp_path):
    store = _store(tmp_path)
    identity = _identity()
    handle = store.publish(identity, [_capture()])[0]
    blob = next((store.root / "blobs").rglob("*.bin"))
    blob.write_bytes(b"corrupted")
    with pytest.raises(ShuntError) as exc:
        store.load_payload(store.resolve(identity, handle.handle_id))
    assert exc.value.code == "STORE_FAILED"
    # Corruption is reported, never "repaired" by deleting content other handles share.
    assert blob.exists()


def test_a_symlink_where_a_blob_belongs_is_refused(tmp_path):
    store = _store(tmp_path)
    identity = _identity()
    capture = _capture(b"target content\n")
    digest = capture.hash
    blob = store.root / "blobs" / digest[:2] / digest[2:4] / f"{digest}.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"attacker controlled\n")
    blob.symlink_to(outside)
    with pytest.raises(ShuntError) as exc:
        store.publish(identity, [capture])
    assert exc.value.code == "STORE_FAILED" and exc.value.detail == "UNSAFE_BLOB_PATH"
    assert store.stats().handles == 0


def test_a_fifo_where_a_blob_belongs_is_refused(tmp_path):
    store = _store(tmp_path)
    identity = _identity()
    capture = _capture(b"fifo target\n")
    digest = capture.hash
    blob = store.root / "blobs" / digest[:2] / digest[2:4] / f"{digest}.bin"
    blob.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(blob)
    with pytest.raises(ShuntError) as exc:
        store.publish(identity, [capture])
    assert exc.value.code == "STORE_FAILED"
    assert store.stats().handles == 0


def test_directories_and_files_stay_private(tmp_path):
    store = _store(tmp_path)
    store.publish(_identity(), [_capture()])
    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.root / "blobs").st_mode) == 0o700
    for blob in (store.root / "blobs").rglob("*.bin"):
        assert stat.S_IMODE(os.stat(blob).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(blob.parent).st_mode) == 0o700


# -- quotas -----------------------------------------------------------------


def test_the_entry_quota_refuses_rather_than_evicting_a_live_handle(tmp_path):
    limits = L.narrow(store_max_entries=2)
    store = _store(tmp_path, limits)
    identity = _identity()
    store.publish(identity, [_capture(b"one\n"), _capture(b"two\n")])
    with pytest.raises(ShuntError) as exc:
        store.publish(identity, [_capture(b"three\n")])
    assert exc.value.detail == "STORE_ENTRY_QUOTA"
    assert store.stats().handles == 2


def test_disclosure_is_charged_before_bytes_are_returned(tmp_path):
    limits = L.narrow(disclosure_max_per_source_bytes=100)
    store = _store(tmp_path, limits)
    identity = _identity()
    handle = store.publish(identity, [_capture()])[0]
    assert store.disclosure_allowance(identity, handle.handle_id).remaining == 100
    first = store.charge_disclosure(identity, handle.handle_id, "lines", 60)
    assert first.granted and first.disclosed_bytes_source == 60
    # 60 + 60 exceeds the ceiling, so nothing is charged and nothing may be disclosed.
    second = store.charge_disclosure(identity, handle.handle_id, "lines", 60)
    assert not second.granted and second.charged_bytes == 0
    assert store.disclosure_allowance(identity, handle.handle_id).remaining == 40


def test_the_session_ceiling_binds_across_separate_handles(tmp_path):
    limits = L.narrow(disclosure_max_per_session_bytes=100)
    store = _store(tmp_path, limits)
    identity = _identity()
    a, b = store.publish(identity, [_capture(b"one\n"), _capture(b"two\n")])
    assert store.charge_disclosure(identity, a.handle_id, "lines", 80).granted
    # Paging a *different* handle cannot escape the session budget.
    assert not store.charge_disclosure(identity, b.handle_id, "lines", 80).granted


def test_the_baseline_is_credited_exactly_once(tmp_path):
    store = _store(tmp_path)
    identity = _identity()
    handle = store.publish(identity, [_capture()])[0]
    assert store.credit_baseline(identity, handle.handle_id) is True
    for _ in range(5):
        assert store.credit_baseline(identity, handle.handle_id) is False


# -- cross-process ----------------------------------------------------------


def test_two_processes_share_one_store_without_losing_a_handle(tmp_path):
    """Real subprocesses, real SQLite contention, WAL and busy-timeout doing their job."""
    root = tmp_path / "cache"
    src = str(Path(__file__).resolve().parents[1] / "src")
    program = (
        "import sys, json;"
        f"sys.path.insert(0, {src!r});"
        "from context_shunt.store import SnapshotStore, ScopeIdentity, Capture;"
        f"s = SnapshotStore({str(root)!r});"
        "i = ScopeIdentity(host='test-host', profile='test', principal='local', session='shared');"
        "ids = [s.publish(i, [Capture(data=f'{sys.argv[1]}-{n}'.encode(),"
        " media_type='text/plain', line_count=1)])[0].handle_id for n in range(20)];"
        "print(json.dumps(ids))"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", program, tag], stdout=subprocess.PIPE, text=True)
        for tag in ("alpha", "beta")
    ]
    published = []
    for proc in procs:
        out, _ = proc.communicate(timeout=120)
        assert proc.returncode == 0, out
        published.extend(json.loads(out))

    assert len(published) == 40
    assert len(set(published)) == 40
    store = SnapshotStore(root)
    identity = _identity("shared")
    for handle_id in published:
        assert store.resolve(identity, handle_id).handle_id == handle_id


def test_many_processes_can_create_the_same_store_at_once(tmp_path):
    """The contended *first* open, which is a different race from contended writes.

    The normative DDL sets `PRAGMA journal_mode = WAL`, and it runs on every connection.
    The WAL transition needs a brief exclusive lock and SQLite answers a competitor with
    SQLITE_BUSY immediately - `busy_timeout` does not cover it - so processes creating a
    store together raced and the loser failed with `OPEN_FAILED`. This was the cause of an
    intermittent failure in the two-process publish test.

    Six processes rather than two, because the window is small: with the retry removed
    this fails within a couple of rounds, and passes 40 rounds with it.
    """
    root = tmp_path / "cache"
    src = str(Path(__file__).resolve().parents[1] / "src")
    program = (
        "import sys;"
        f"sys.path.insert(0, {src!r});"
        "from context_shunt.store import SnapshotStore, ScopeIdentity;"
        f"s = SnapshotStore({str(root)!r});"
        "s.open_scope(ScopeIdentity(host='h', profile='p', principal='l',"
        " session='shared', generation=1));"
        "print('ok')"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", program],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        assert out.strip() == "ok"


def test_concurrent_disclosure_charges_never_overshoot_the_ceiling(tmp_path):
    limits = L.narrow(disclosure_max_per_source_bytes=1000)
    store = _store(tmp_path, limits)
    identity = _identity()
    handle = store.publish(identity, [_capture()])[0]
    granted: list[int] = []
    lock = threading.Lock()

    def charge() -> None:
        for _ in range(20):
            result = store.charge_disclosure(identity, handle.handle_id, "lines", 25)
            if result.granted:
                with lock:
                    granted.append(result.charged_bytes)

    threads = [threading.Thread(target=charge) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(granted) <= 1000
    assert store.disclosure_allowance(identity, handle.handle_id).per_source_remaining >= 0


# -- accounting persistence -------------------------------------------------


def test_operation_records_round_trip_with_nulls_intact(tmp_path):
    store = _store(tmp_path)
    identity = _identity()
    record = OperationRecord(
        operation_id="acc_" + "1" * 16,
        kind="read",
        status="ok",
        code="ANSWERED",
        raw_input_bytes=2048,
        raw_input_baseline_tokens=512,
        baseline_kind="full_payload_counterfactual",
        baseline_method="bytes_div_4",
        baseline_credit_tokens=512,
        main_model_envelope_bytes=400,
        main_model_envelope_tokens=100,
        envelope_token_method="bytes_div_4",
        reader_input_tokens=None,
        reader_output_tokens=None,
        reader_cache_tokens=None,
        reader_token_method="unknown",
        attempts_started=1,
        attempts_usage_complete=0,
        delivery_boundary="envelope",
        main_context_tokens_saved=412,
        net_tokens_saved=412,
    )
    store.record_operation(identity, record)
    [read_back] = store.operation_page(identity, page=1, page_size=8)
    # "Not reported" survives the round trip as null, never as zero.
    assert read_back.reader_input_tokens is None
    assert read_back.reader_output_tokens is None
    assert read_back.main_context_tokens_saved == 412
    totals = store.operation_totals(identity)
    assert totals["reader_input_tokens"] is None
    assert totals["baseline_credit_tokens"] == 512


def test_stats_never_reach_another_scope(tmp_path):
    store = _store(tmp_path)
    mine, theirs = _identity("mine"), _identity("theirs")
    for identity, op in ((mine, "acc_" + "a" * 16), (theirs, "acc_" + "b" * 16)):
        store.record_operation(
            identity,
            OperationRecord(
                operation_id=op,
                kind="inspect",
                status="ok",
                code="EXTRACTED",
                raw_input_bytes=0,
                raw_input_baseline_tokens=None,
                baseline_kind="none",
                baseline_method="unknown",
                baseline_credit_tokens=0,
                main_model_envelope_bytes=100,
                main_model_envelope_tokens=25,
                envelope_token_method="bytes_div_4",
                reader_input_tokens=None,
                reader_output_tokens=None,
                reader_cache_tokens=None,
                reader_token_method="not_applicable",
                attempts_started=0,
                attempts_usage_complete=0,
                delivery_boundary="extraction",
                main_context_tokens_saved=-25,
                net_tokens_saved=-25,
            ),
        )
    assert store.operation_count(mine) == 1
    assert [r.operation_id for r in store.operation_page(mine, page=1, page_size=8)] == [
        "acc_" + "a" * 16
    ]


# -- the deletion race, at the interleaving that actually loses data ----------


def _force_pending_delete(store: SnapshotStore, digest: str) -> None:
    """Put a blob into "sweepable" state while its file is still on disk."""
    conn = sqlite3.connect(store.root / "store.sqlite3")
    try:
        conn.execute("UPDATE blobs SET refcount = 0, pending_delete = 1 WHERE hash = ?", (digest,))
        conn.commit()
    finally:
        conn.close()


def test_a_publish_that_dedupes_never_lands_on_a_swept_file(tmp_path, monkeypatch):
    """The real deletion race, forced to the interleaving that loses the payload.

    `_stage_blob` skips writing when the content file is already present, and the sweeper
    unlinks outside the lock. So:

      1. publisher sees the file, dedupes, writes nothing;
      2. sweeper unlinks the file and deletes the row (refcount is still 0);
      3. publisher commits, re-inserting the row at refcount 1 and minting a handle.

    The handle and its row exist; the payload does not. The existing republish test does
    not reach this because `revoke` sweeps synchronously, so the publisher finds no file
    and rewrites it. This one pins the interleaving directly.
    """
    store = _store(tmp_path)
    identity = _identity()
    first = store.publish(identity, [_capture()])[0]
    _force_pending_delete(store, first.blob_hash)
    assert store._blob_path(first.blob_hash).exists()

    real_stage = store._stage_blob

    def stage_then_sweep(capture, digest, final):
        staged = real_stage(capture, digest, final)
        # The sweeper runs in the window between the dedupe decision and the commit.
        store._collect_pending_blobs()
        return staged

    monkeypatch.setattr(store, "_stage_blob", stage_then_sweep)
    republished = store.publish(identity, [_capture()])[0]

    # The published handle must address a payload that is actually there.
    assert store.load_payload(store.resolve(identity, republished.handle_id)) == BODY


def test_a_quota_rejection_leaves_no_orphan_file_or_row(tmp_path):
    """A refused publish must not leave its staged content behind.

    Blobs are staged to their final path *before* the capacity check, which runs inside
    the publish transaction. The transaction rolls the rows back, but the renamed files
    and their `orphan_temps` rows are only cleaned up on the `sqlite3.Error` path - a
    capacity `ShuntError` re-raised straight past them.
    """
    store = _store(tmp_path, limits=L.narrow(store_max_bytes=len(BODY) * 2))
    identity = _identity()
    store.publish(identity, [_capture()])

    before_files = sorted(p.name for p in (store.root / "blobs").rglob("*.bin"))
    with pytest.raises(ShuntError) as exc:
        store.publish(identity, [_capture(data=b"x" * 4096)])
    assert exc.value.code in ("LIMIT_EXCEEDED", "STORE_FAILED")

    after_files = sorted(p.name for p in (store.root / "blobs").rglob("*.bin"))
    assert after_files == before_files, "the refused publish left its content on disk"

    conn = sqlite3.connect(store.root / "store.sqlite3")
    try:
        temps = conn.execute("SELECT COUNT(*) FROM orphan_temps").fetchone()[0]
        blobs = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    finally:
        conn.close()
    assert temps == 0, "the refused publish left an orphan_temps row"
    assert blobs == 1


def test_identical_bytes_with_a_different_media_type_are_not_silently_relabelled(tmp_path):
    """Dedupe is keyed by content hash, but the metadata is not part of the content.

    `INSERT ... ON CONFLICT(hash) DO NOTHING` keeps the first publisher's `media_type`,
    so identical bytes published as JSON after being published as text came back
    labelled `text/plain` - and a records selector over them is then refused. Silently
    serving the wrong media type is worse than refusing, so this fails closed.
    """
    store = _store(tmp_path)
    identity = _identity()
    body = b'{"a":1}\n'
    first = store.publish(identity, [_capture(data=body, media_type="text/plain")])[0]
    assert first.media_type == "text/plain"

    with pytest.raises(ShuntError) as exc:
        store.publish(identity, [_capture(data=body, media_type="application/json")])
    assert exc.value.code == "STORE_FAILED"
    assert "METADATA" in (exc.value.detail or "")

    # The original handle is untouched by the refusal.
    assert store.load_payload(store.resolve(identity, first.handle_id)) == body


# -- disclosure and baseline follow the content, not the handle --------------


def test_recapturing_a_source_does_not_reset_its_disclosure_ceiling(tmp_path):
    """The per-source ceiling exists to stop paging reassembling a whole payload.

    It was read from `handles.disclosed_bytes` for one handle, but re-registering the
    same file mints a *new* handle with the counter at zero. A caller could therefore
    disclose the cap, recapture, and disclose the cap again, as many times as it liked.
    The session ceiling was never affected because it sums `disclosure_events` across the
    scope; only the per-source one reset.
    """
    cap = 100
    store = _store(tmp_path, limits=L.narrow(disclosure_max_per_source_bytes=cap))
    identity = _identity()

    first = store.publish(identity, [_capture()])[0]
    charge = store.charge_disclosure(identity, first.handle_id, "bytes", cap)
    assert charge.granted is True and charge.charged_bytes == cap

    # Same bytes, captured again: a new handle for content already disclosed to its cap.
    second = store.publish(identity, [_capture()])[0]
    assert second.handle_id != first.handle_id
    assert second.blob_hash == first.blob_hash

    allowance = store.disclosure_allowance(identity, second.handle_id)
    assert allowance.per_source_remaining == 0

    refused = store.charge_disclosure(identity, second.handle_id, "bytes", 1)
    assert refused.granted is False and refused.limit_reached is True


def test_the_baseline_credit_is_claimed_once_per_source_not_once_per_handle(tmp_path):
    """The withheld-source saving is a property of the content, claimable once.

    Keyed by handle, a recapture of the same content claimed the saving again, so a
    caller could inflate "tokens saved" without withholding anything new.
    """
    store = _store(tmp_path)
    identity = _identity()
    first = store.publish(identity, [_capture()])[0]
    assert store.credit_baseline(identity, first.handle_id) is True
    assert store.credit_baseline(identity, first.handle_id) is False

    second = store.publish(identity, [_capture()])[0]
    assert store.credit_baseline(identity, second.handle_id) is False

    # Genuinely different content still earns its own credit.
    other = store.publish(identity, [_capture(data=b"different bytes\n")])[0]
    assert store.credit_baseline(identity, other.handle_id) is True


def test_a_separate_scope_keeps_its_own_disclosure_and_baseline(tmp_path):
    """Sharing is per scope: another session must not inherit spent allowance."""
    cap = 100
    store = _store(tmp_path, limits=L.narrow(disclosure_max_per_source_bytes=cap))
    one, two = _identity(session="a"), _identity(session="b")

    first = store.publish(one, [_capture()])[0]
    store.charge_disclosure(one, first.handle_id, "bytes", cap)
    assert store.credit_baseline(one, first.handle_id) is True

    second = store.publish(two, [_capture()])[0]
    assert store.disclosure_allowance(two, second.handle_id).per_source_remaining == cap
    assert store.credit_baseline(two, second.handle_id) is True


# -- accounting must survive revocation, and stay per source -----------------


def test_revoking_a_handle_does_not_reset_its_source_disclosure_ceiling(tmp_path):
    """The per-source ceiling must not be resettable by revoke-then-recapture.

    Disclosure history is reconstructed by joining `disclosure_events` to live `handles`
    rows, and both revoke and the expiry sweep *deleted* those rows - so the join lost the
    history and a recaptured source started from zero. A caller could disclose the cap,
    revoke, recapture, and disclose the cap again, up to the session ceiling.
    """
    cap = 100
    store = _store(tmp_path, limits=L.narrow(disclosure_max_per_source_bytes=cap))
    identity = _identity()

    first = store.publish(identity, [_capture()])[0]
    assert store.charge_disclosure(identity, first.handle_id, "bytes", cap).granted is True
    assert store.credit_baseline(identity, first.handle_id) is True

    store.revoke(identity, first.handle_id)
    second = store.publish(identity, [_capture()])[0]

    assert store.disclosure_allowance(identity, second.handle_id).per_source_remaining == 0
    refused = store.charge_disclosure(identity, second.handle_id, "bytes", 1)
    assert refused.granted is False and refused.limit_reached is True
    # The saving was already claimed for this content; recapture does not re-claim it.
    assert store.credit_baseline(identity, second.handle_id) is False


def test_closing_the_scope_is_what_clears_the_accounting(tmp_path):
    """The tombstones are bounded by the session, which is the accounting window."""
    cap = 100
    store = _store(tmp_path, limits=L.narrow(disclosure_max_per_source_bytes=cap))
    identity = _identity()
    handle = store.publish(identity, [_capture()])[0]
    store.charge_disclosure(identity, handle.handle_id, "bytes", cap)
    store.revoke(identity, handle.handle_id)
    store.close_scope(identity, revoke=True)

    fresh = _identity(session="next")
    reborn = store.publish(fresh, [_capture()])[0]
    assert store.disclosure_allowance(fresh, reborn.handle_id).per_source_remaining == cap


def test_a_revoked_handle_still_cannot_be_resolved_or_read(tmp_path):
    """Retaining the row for accounting must not resurrect the capability."""
    store = _store(tmp_path)
    identity = _identity()
    handle = store.publish(identity, [_capture()])[0]
    assert store.revoke(identity, handle.handle_id) is True
    with pytest.raises(ShuntError) as exc:
        store.resolve(identity, handle.handle_id)
    assert exc.value.code in ("SOURCE_EXPIRED", "STORE_FAILED")
    # And the content it held is releasable, because the refcount was dropped.
    assert store.stats().handles == 0


def test_a_batch_cannot_carry_two_media_types_for_the_same_bytes(tmp_path):
    """The metadata check consulted only *committed* rows, so one batch slipped through.

    Publishing identical bytes twice in a single batch returned two handles with different
    declared media types, while the persisted row - and therefore every later resolve -
    carried only the first. The accepted API result disagreed with the stored content.
    """
    store = _store(tmp_path)
    identity = _identity()
    body = b'{"a":1}\n'
    with pytest.raises(ShuntError) as exc:
        store.publish(
            identity,
            [_capture(data=body, media_type="application/json"), _capture(data=body)],
        )
    assert exc.value.detail == "BLOB_METADATA_CONFLICT"
    # Nothing partial survived the refusal.
    assert store.stats().handles == 0


# -- staging is a reservation other processes must honour --------------------


def _second_instance(store: SnapshotStore) -> SnapshotStore:
    """A second store over the same root, standing in for another process."""
    return SnapshotStore(store.root, L)


def test_an_orphan_sweep_spares_content_another_process_is_staging(tmp_path):
    """`orphan_temps` is a durable reservation; the sweep ignored it.

    Staging renames a blob into its final path *before* the publishing transaction
    commits, so between those two points the file has no `blobs` row. Another process
    sweeping orphans saw a file with no row and deleted it, and the publisher then failed
    with `BLOB_MISSING` on content it had written itself.
    """
    store = _store(tmp_path)
    identity = _identity()
    store.open_scope(identity)
    other = _second_instance(store)
    other.open_scope(identity)

    capture = _capture()
    final = store._blob_path(capture.hash)
    assert store._stage_blob(capture, capture.hash, final) is not None
    assert final.exists()

    assert other._collect_orphan_blob_files() == 0
    assert final.exists(), "another process deleted a live staging reservation"

    # And the publish that reservation belongs to still completes.
    handle = store.publish(identity, [capture])[0]
    assert store.load_payload(store.resolve(identity, handle.handle_id)) == capture.data


def test_a_refused_publish_spares_content_another_process_still_holds(tmp_path):
    """Discarding one batch's staging must not remove another publisher's content.

    Both publishers address the same content-addressed file. When the first is refused it
    dropped the file because no committed row referenced the digest yet - and the second
    was mid-publish against exactly those bytes.

    This drives the production path only. It used to call `_record_temp` by hand to give
    the second publisher the reservation that deduping did not create, which manufactured
    the protection it was meant to be testing; `_stage_blob` now takes that lease itself,
    so the test no longer has to.
    """
    store = _store(tmp_path)
    identity = _identity()
    store.open_scope(identity)
    other = _second_instance(store)
    other.open_scope(identity)

    capture = _capture()
    final = store._blob_path(capture.hash)
    mine = store._stage_blob(capture, capture.hash, final)
    theirs = other._stage_blob(capture, capture.hash, final)
    assert mine is not None and theirs is not None

    store._discard_temp_ids([mine])
    assert final.exists(), "a refused batch removed content another process was publishing"


def test_recovery_leaves_a_fresh_reservation_alone(tmp_path):
    """Startup recovery cleared *every* temp, including a live one.

    Recovery exists to clear residue from a process that died. Deleting reservations that
    are seconds old destroys the staging of a process that is still running.
    """
    store = _store(tmp_path)
    identity = _identity()
    store.open_scope(identity)
    other = _second_instance(store)

    capture = _capture()
    final = store._blob_path(capture.hash)
    assert store._stage_blob(capture, capture.hash, final) is not None

    other.recover()
    assert final.exists(), "recovery removed a reservation that was still live"


# -- a publisher must not commit a handle whose payload is gone --------------


def _stage_then(store: SnapshotStore, hook):
    """Wrap `_stage_blob` so another process can act between staging and the commit."""
    real = store._stage_blob

    def wrapped(capture, digest, final):
        staged = real(capture, digest, final)
        hook(capture, digest, final)
        return staged

    store._stage_blob = wrapped


def test_publish_revalidates_its_content_before_committing(tmp_path):
    """The losing schedule: stage, lose the file, commit anyway.

    Staging happens before the publishing transaction opens, and a deduping publisher
    writes nothing and held no reservation, so another process could remove the shared
    file in between. The transaction then committed a handle addressing a payload that no
    longer existed, and resolving it failed with `STORE_FAILED/BLOB_MISSING` - the store
    handing back an unreadable capability for content it had accepted.
    """
    store = _store(tmp_path)
    other = _second_instance(store)
    identity = _identity()
    store.open_scope(identity)
    other.open_scope(identity)

    capture = _capture()
    final = other._blob_path(capture.hash)
    other._stage_blob(capture, capture.hash, final)
    assert final.exists()

    _stage_then(store, lambda _c, _d, f: f.unlink(missing_ok=True))
    handle = store.publish(identity, [capture])[0]

    # The publisher still holds the bytes, so the handle it returns must be readable.
    assert store.load_payload(store.resolve(identity, handle.handle_id)) == capture.data


def test_a_deduping_publisher_holds_a_reservation_of_its_own(tmp_path):
    """Dedupe wrote nothing and reserved nothing, so nothing protected the shared file.

    The previous regression manufactured the missing protection by calling `_record_temp`
    itself. This one drives the production path only: the second publisher dedupes, and
    the first one's refusal must still leave the content it is relying on.
    """
    store = _store(tmp_path)
    other = _second_instance(store)
    identity = _identity()
    store.open_scope(identity)
    other.open_scope(identity)

    capture = _capture()
    final = store._blob_path(capture.hash)
    mine = store._stage_blob(capture, capture.hash, final)
    theirs = other._stage_blob(capture, capture.hash, final)
    assert mine is not None
    assert theirs is not None, "a deduping publisher must still take a reservation"

    store._discard_temp_ids([mine])
    assert final.exists(), "a refusal removed content another publisher had reserved"


def test_an_orphan_sweep_revalidates_before_it_unlinks(tmp_path):
    """The sweep listed files, released its lock, then unlinked against a stale list.

    A publisher that staged after the snapshot was taken had its content removed by a
    decision made before that content existed.
    """
    store = _store(tmp_path)
    other = _second_instance(store)
    identity = _identity()
    store.open_scope(identity)
    other.open_scope(identity)

    capture = _capture(data=b"staged after the snapshot\n")
    final = store._blob_path(capture.hash)
    real_iter = type(other)._iter_blob_files

    def stage_during_scan(self):
        files = list(real_iter(self))
        if not final.exists():
            store._stage_blob(capture, capture.hash, final)
            files = list(real_iter(self))
        return files

    type(other)._iter_blob_files = stage_during_scan
    try:
        other._collect_orphan_blob_files()
    finally:
        type(other)._iter_blob_files = real_iter

    assert final.exists(), "the sweep unlinked content staged after its snapshot"
    handle = store.publish(identity, [capture])[0]
    assert store.load_payload(store.resolve(identity, handle.handle_id)) == capture.data


def test_pending_collection_spares_content_a_publisher_has_deduped_onto(tmp_path):
    """A blob marked for deletion can be adopted by a new publisher before the sweep."""
    store = _store(tmp_path)
    other = _second_instance(store)
    identity = _identity()
    store.open_scope(identity)
    other.open_scope(identity)

    capture = _capture(data=b"adopted while pending\n")
    first = store.publish(identity, [capture])[0]
    store.revoke(identity, first.handle_id)

    final = store._blob_path(capture.hash)
    if not final.exists():
        store._stage_blob(capture, capture.hash, final)
    _force_pending_delete(store, capture.hash)

    assert other._stage_blob(capture, capture.hash, final) is not None
    store._collect_pending_blobs()

    handle = other.publish(identity, [capture])[0]
    assert other.load_payload(other.resolve(identity, handle.handle_id)) == capture.data


# -- real two-process proofs, through the public API only --------------------


_PUBLISHER_PROGRAM = """
import sys, json
sys.path.insert(0, {src!r})
from context_shunt.store import SnapshotStore, ScopeIdentity, Capture

root, rounds, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
store = SnapshotStore(root)
identity = ScopeIdentity(
    host="test-host", profile="test", principal="local", session="shared", generation=1
)
failures = []
for index in range(rounds):
    payload = ("publisher-%s-%d\\n" % (tag, index)).encode()
    capture = Capture(data=payload, media_type="text/plain", line_count=1)
    try:
        handle = store.publish(identity, [capture])[0]
        got = store.load_payload(store.resolve(identity, handle.handle_id))
        if got != payload:
            failures.append("round %d: payload mismatch" % index)
        # Release it again, so a long run exercises the race rather than the entry quota.
        store.revoke(identity, handle.handle_id)
    except Exception as exc:
        failures.append("round %d: %s" % (index, type(exc).__name__ + "/" + str(exc)))
print(json.dumps(failures))
"""

_SWEEPER_PROGRAM = """
import sys, json
sys.path.insert(0, {src!r})
from context_shunt.store import SnapshotStore

root, rounds = sys.argv[1], int(sys.argv[2])
store = SnapshotStore(root)
failures = []
for _ in range(rounds):
    try:
        store.sweep()
        store.recover()
    except Exception as exc:
        failures.append(type(exc).__name__ + "/" + str(exc))
print(json.dumps(failures))
"""


def _src_root() -> str:
    return str(Path(__file__).resolve().parents[1] / "src")


def test_a_publisher_and_a_sweeper_in_separate_processes_never_lose_a_payload(tmp_path):
    """Two real processes over one store, driven only through the public API.

    Cleanup decides a file is an orphan and then unlinks it, and those two steps are now
    coupled inside one write transaction so the decision cannot go stale across processes.

    Honest about what this proves: it is a *regression*, not a red-green demonstration.
    Removing the coupling does not make it fail, because `_stage_blob` commits its lease
    **before** the payload is written, so a sweeper can never observe a blob file that has
    neither a row nor a lease. The schedule the final review forced needs that observation
    and cannot be reached from the public surface at this ordering. What this does hold is
    the outcome under sustained real contention - two publishers against a sweeper, over
    `publish`, `resolve`, `load_payload`, `sweep` and `recover`, with no private hook and
    no injected schedule - so any future change that reopens the window fails here.

    The deterministic proof of the coupling itself is
    `test_an_orphan_sweep_revalidates_before_it_unlinks`, which does fail without it.
    """
    root = str(tmp_path / "cache")
    rounds = 120
    # Two publishers against one sweeper. The window is narrow, so the proof comes from
    # sustained contention rather than a single pass: with the write-lock coupling removed
    # this loses a payload, and with it in place it does not.
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", program.format(src=_src_root()), root, str(rounds), tag],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for program, tag in (
            (_PUBLISHER_PROGRAM, "a"),
            (_PUBLISHER_PROGRAM, "b"),
            (_SWEEPER_PROGRAM, "sweep"),
        )
    ]
    failures: list[str] = []
    for worker in workers:
        out, err = worker.communicate(timeout=300)
        assert worker.returncode == 0, err
        failures.extend(json.loads(out))
    assert failures == [], f"cleanup and publication raced: {failures[:5]}"


_OPENER_PROGRAM = """
import sys, json
sys.path.insert(0, {src!r})
from context_shunt.store import SnapshotStore, ScopeIdentity

root = sys.argv[1]
try:
    store = SnapshotStore(root)
    store.open_scope(ScopeIdentity(
        host="h", profile="p", principal="l", session="s", generation=1
    ))
    print(json.dumps("ok"))
except Exception as exc:
    print(json.dumps(getattr(exc, "code", type(exc).__name__) + "/" + str(getattr(exc, "detail", ""))))
"""


def _revision_one_store(root: Path) -> None:
    """Synthesize a revision-1 store: the current DDL without the revision-2 additions."""
    root.mkdir(parents=True, exist_ok=True)
    ddl = (Path(__file__).resolve().parents[3] / "contracts" / "store" / "v1.sql").read_text()
    ddl = ddl.split("CREATE TABLE IF NOT EXISTS source_credits")[0]
    ddl = ddl.replace("    blob_hash TEXT,\n", "")
    ddl = ddl.replace(
        "    CHECK (bytes >= 0),\n    CHECK (blob_hash IS NULL OR length(blob_hash) = 64)\n",
        "    CHECK (bytes >= 0)\n",
    )
    ddl = ddl.replace(
        "CREATE INDEX IF NOT EXISTS disclosure_by_source"
        " ON disclosure_events (scope_id, blob_hash);\n",
        "",
    )
    conn = sqlite3.connect(root / "store.sqlite3")
    try:
        conn.executescript(ddl)
        for key, value in (
            ("ddl_version", "1"),
            ("store_id", "ab" * 16),
            ("clock_high_water_ms", "0"),
            ("cursor_key", "cd" * 32),
        ):
            conn.execute("INSERT INTO store_metadata (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()


def test_simultaneous_opens_all_migrate_a_revision_one_store(tmp_path):
    """The revision-1 to 2 step is a check followed by an `ALTER TABLE`.

    Every process opening the store sees the same missing column and races to add it.
    `ALTER TABLE ADD COLUMN` has no `IF NOT EXISTS`, so a loser failed with "duplicate
    column name" and the whole open became `STORE_FAILED/MIGRATION_FAILED` - the store
    refusing its first operation after a supported upgrade. Losing that race is a
    successful migration: the column the loser wanted now exists.
    """
    # Rounds, because the window is a real race: a single round caught the defect only
    # about one time in three, which is not a proof of anything. Each round is a fresh
    # revision-1 store raced by twelve processes.
    for attempt in range(6):
        root = tmp_path / f"cache-{attempt}"
        _revision_one_store(root)

        openers = [
            subprocess.Popen(
                [sys.executable, "-c", _OPENER_PROGRAM.format(src=_src_root()), str(root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(12)
        ]
        results = []
        for opener in openers:
            out, err = opener.communicate(timeout=180)
            assert opener.returncode == 0, err
            results.append(json.loads(out))

        assert results == ["ok"] * 12, (
            f"round {attempt}: simultaneous opens did not all migrate: {results}"
        )
        # And the store really is at revision 2 afterwards.
        assert SnapshotStore(root).ddl_version() == 2
