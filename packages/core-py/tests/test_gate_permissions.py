"""unit permissions: what may become a source, and how private artifacts are stored."""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from context_shunt.binaryguard import assert_supported_blocks, assert_text, looks_binary
from context_shunt.errors import ShuntError
from context_shunt.paths import PathPolicy, assert_no_secret, authorize
from context_shunt.registry import SourceRegistry
from context_shunt.snapshot import snapshot_bytes, snapshot_file
from context_shunt.spill import SpillStore

pytestmark = pytest.mark.gate_permissions


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "ok.txt").write_text("alpha\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified\n")
    return root, outside


def _policy(root, **kw):
    return PathPolicy.from_config([str(root)], **kw)


def test_regular_file_inside_a_root_is_authorized(workspace):
    root, _ = workspace
    authorized = authorize(str(root / "ok.txt"), _policy(root))
    assert authorized.real == (root / "ok.txt").resolve()


def test_relative_path_is_rejected(workspace):
    root, _ = workspace
    with pytest.raises(ShuntError) as exc:
        authorize("ok.txt", _policy(root))
    assert exc.value.detail == "RELATIVE_PATH"


def test_traversal_outside_the_root_is_rejected(workspace):
    root, outside = workspace
    with pytest.raises(ShuntError) as exc:
        authorize(str(root / ".." / "outside" / "secret.txt"), _policy(root))
    assert exc.value.detail == "OUTSIDE_WORKSPACE_ROOT"


def test_symlink_is_rejected_even_when_the_target_is_inside(workspace):
    root, _ = workspace
    link = root / "link.txt"
    link.symlink_to(root / "ok.txt")
    with pytest.raises(ShuntError) as exc:
        authorize(str(link), _policy(root))
    assert exc.value.detail == "SYMLINK"


def test_symlink_escaping_the_root_is_rejected(workspace):
    root, outside = workspace
    link = root / "escape.txt"
    link.symlink_to(outside / "secret.txt")
    with pytest.raises(ShuntError):
        authorize(str(link), _policy(root))


def test_hardlink_is_rejected(workspace):
    root, _ = workspace
    target = root / "hard.txt"
    os.link(root / "ok.txt", target)
    with pytest.raises(ShuntError) as exc:
        authorize(str(target), _policy(root))
    assert exc.value.detail == "HARDLINKED"


def test_directory_and_fifo_are_rejected(workspace, tmp_path):
    root, _ = workspace
    (root / "sub").mkdir()
    with pytest.raises(ShuntError) as exc:
        authorize(str(root / "sub"), _policy(root))
    assert exc.value.detail == "NOT_REGULAR_FILE"
    fifo = root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ShuntError):
        authorize(str(fifo), _policy(root))


@pytest.mark.parametrize(
    "name", [".env", ".env.production", "id_rsa", "server.pem", "auth.json", "credentials"]
)
def test_secret_paths_are_denied(workspace, name):
    root, _ = workspace
    path = root / name
    path.write_text("value\n")
    with pytest.raises(ShuntError) as exc:
        authorize(str(path), _policy(root))
    assert exc.value.detail == "SECRET_PATH"
    assert name not in exc.value.safe_message()


def test_administrator_denylist_is_applied(workspace):
    root, _ = workspace
    (root / "vault").mkdir()
    path = root / "vault" / "notes.txt"
    path.write_text("x\n")
    with pytest.raises(ShuntError):
        authorize(str(path), PathPolicy.from_config([str(root)], ["vault/*"]))


def test_secret_content_is_rejected_without_echoing_the_value():
    payload = b"-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n"
    with pytest.raises(ShuntError) as exc:
        assert_no_secret(payload, "SOURCE")
    assert "MIIabc" not in exc.value.safe_message()
    with pytest.raises(ShuntError):
        snapshot_bytes(payload)


def test_secret_in_a_question_is_rejected():
    with pytest.raises(ShuntError):
        assert_no_secret(b"my key is ghp_abcdefghijklmnopqrstuvwxyz012345", "QUESTION")


@pytest.mark.parametrize(
    "payload", [b"\x7fELF\x02\x01", b"%PDF-1.7\n", b"abc\x00def", b"\x89PNG\r\n\x1a\n"]
)
def test_binary_content_is_detected_by_sniffing_not_extension(payload):
    assert looks_binary(payload)
    with pytest.raises(ShuntError) as exc:
        assert_text(payload)
    assert exc.value.code == "BINARY_UNSUPPORTED"


def test_invalid_encoding_is_rejected():
    with pytest.raises(ShuntError) as exc:
        assert_text(b"valid then \xff\xfe invalid")
    assert exc.value.detail in ("INVALID_ENCODING", "BINARY_CONTENT")


def test_mixed_content_blocks_reject_the_whole_result():
    with pytest.raises(ShuntError):
        assert_supported_blocks([{"type": "text", "text": "ok"}, {"type": "image", "data": "x"}])


def test_race_replacement_between_probe_and_read_is_source_changed(workspace):
    root, _ = workspace
    path = root / "race.txt"
    path.write_text("original\n")
    authorized = authorize(str(path), _policy(root))
    time.sleep(0.01)
    path.write_text("swapped content\n")
    with pytest.raises(ShuntError) as exc:
        snapshot_file(authorized)
    assert exc.value.code == "SOURCE_CHANGED"


def test_handles_do_not_resolve_across_sessions():
    registry = SourceRegistry()
    entry = registry.register("sess_a", snapshot_bytes(b"alpha\n"))
    registry.resolve("sess_a", entry.source_id)
    with pytest.raises(ShuntError) as exc:
        registry.resolve("sess_b", entry.source_id)
    assert exc.value.code == "SOURCE_EXPIRED"


def test_expired_handles_are_refused_and_not_refetched():
    registry = SourceRegistry()
    entry = registry.register("sess", snapshot_bytes(b"alpha\n"))
    registry._time = lambda: time.time() + 10**6  # noqa: SLF001 - TTL fast-forward
    with pytest.raises(ShuntError) as exc:
        registry.resolve("sess", entry.source_id)
    assert exc.value.detail == "TTL_ELAPSED"


def test_session_expiry_drops_every_handle():
    registry = SourceRegistry()
    registry.register("sess", snapshot_bytes(b"a\n"))
    registry.register("sess", snapshot_bytes(b"b\n"))
    assert registry.expire_session("sess") == 2
    assert registry.count("sess") == 0


def test_spill_directory_and_files_are_private(tmp_path):
    store = SpillStore(tmp_path / "cache")
    assert stat.S_IMODE(os.stat(store.root).st_mode) == 0o700
    written = store.write("sess", b"payload bytes")
    assert stat.S_IMODE(os.stat(written).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(written.parent).st_mode) == 0o700
    assert written.read_bytes() == b"payload bytes"


def test_spill_lives_outside_every_workspace_root(tmp_path, workspace):
    root, _ = workspace
    store = SpillStore(tmp_path / "cache")
    assert root not in store.root.parents and store.root != root


def test_spill_quota_is_enforced(tmp_path):
    store = SpillStore(tmp_path / "cache")
    store.seed_usage("sess", store._limits.session_spill_quota_bytes)  # noqa: SLF001
    with pytest.raises(ShuntError) as exc:
        store.write("sess", b"one more byte")
    assert exc.value.code == "SPILL_FAILED" and exc.value.detail == "QUOTA_EXCEEDED"


def test_publish_is_atomic_and_leaves_no_partial_files(tmp_path):
    store = SpillStore(tmp_path / "cache")
    store.write("sess", b"complete payload")
    leftovers = [p for p in store.root.rglob("*.part")]
    assert leftovers == []


def test_session_cleanup_removes_private_artifacts(tmp_path):
    store = SpillStore(tmp_path / "cache")
    store.write("sess", b"payload")
    assert store.purge_session("sess") == 1
    assert list(store.root.rglob("*.spill")) == []


def test_error_messages_expose_no_paths_or_values(workspace):
    root, outside = workspace
    try:
        authorize(str(outside / "secret.txt"), _policy(root))
    except ShuntError as exc:
        blob = json.dumps({"m": exc.safe_message(), "d": exc.detail})
        assert "secret.txt" not in blob and str(outside) not in blob
    else:  # pragma: no cover
        pytest.fail("expected rejection")
