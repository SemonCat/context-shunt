from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "activate_bootstrap", HERE / "activate_bootstrap.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_candidate(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    current_uid = os.getuid()
    current_gid = os.getgid()
    live = tmp_path / "live"
    scripts = live / "scripts"
    store = live / "context-shunt"
    backups = live / "backups"
    for directory in (scripts, store, backups):
        directory.mkdir(parents=True)
    backups.chmod(0o700)
    before = b"before hook\n"
    after = b"after hook\n"
    hook = scripts / "99-hermes-local-patches"
    hook.write_bytes(before)
    hook.chmod(0o755)
    service_run = tmp_path / "run" / "service" / "gateway-default" / "run"
    service_run.parent.mkdir(parents=True)
    service_run.write_bytes(b"#!/bin/sh\nexport HERMES_S6_SUPERVISED_CHILD=1\n")
    service_run.chmod(0o755)

    candidate = tmp_path / "candidate"
    release_dir = candidate / "release"
    release_dir.mkdir(parents=True)
    files = {
        "context_shunt_core-1.1.0-py3-none-any.whl": b"wheel",
        "ensure_core.py": b"ensure",
        "release.json": b"{}\n",
        "verify_install.py": b"verify",
    }
    for name, data in files.items():
        (release_dir / name).write_bytes(data)
    (candidate / "99-hermes-local-patches.before").write_bytes(before)
    (candidate / "99-hermes-local-patches.after").write_bytes(after)
    (candidate / "activate_bootstrap.py").write_bytes((HERE / "activate_bootstrap.py").read_bytes())
    (candidate / "candidate.json").write_text("{}\n")
    (candidate / "hook.diff").write_text("diff\n")
    manifest: dict[str, object] = {
        "source_commit": "ffa218c",
        "hook": {
            "host_path": str(hook),
            "expected_before_sha256": digest(before),
            "expected_after_sha256": digest(after),
            "before_mode": "0755",
            "before_uid": current_uid,
            "before_gid": current_gid,
            "target_mode": "0755",
            "target_uid": current_uid,
            "target_gid": current_gid,
        },
        "service_run": {
            "host_path": str(service_run),
            "expected_before_sha256": digest(service_run.read_bytes()),
            "mode": "0755",
            "uid": current_uid,
            "gid": current_gid,
        },
        "release": {
            "host_directory": str(store / "ffa218c"),
            "container_directory": "/opt/data/context-shunt/ffa218c",
            "directory_mode": "0755",
            "file_mode": "0444",
            "uid": current_uid,
            "gid": current_gid,
            "files": {name: digest(data) for name, data in files.items()},
        },
        "candidate_files": {
            "activate_bootstrap.py": MODULE.sha256(candidate / "activate_bootstrap.py")
        },
    }
    (candidate / "bootstrap.json").write_text(json.dumps(manifest) + "\n")
    names = [
        "99-hermes-local-patches.before",
        "99-hermes-local-patches.after",
        "activate_bootstrap.py",
        "bootstrap.json",
        "candidate.json",
        "hook.diff",
        *(f"release/{name}" for name in sorted(files)),
    ]
    (candidate / "SHA256SUMS").write_text(
        "".join(f"{MODULE.sha256(candidate / name)}  {name}\n" for name in names)
    )
    return candidate, hook, backups / "transaction", manifest


def test_apply_and_rollback_are_one_recoverable_file_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    candidate, hook, backup, manifest = make_candidate(tmp_path)
    service_run = Path(manifest["service_run"]["host_path"])
    monkeypatch.setattr(MODULE.os, "chown", lambda *_args: None)
    monkeypatch.setattr(MODULE.os, "fchown", lambda *_args: None)

    previous_umask = os.umask(0o077)
    try:
        applied = MODULE.apply(candidate, backup)
    finally:
        os.umask(previous_umask)
    assert applied["result"] == "APPLIED"
    assert applied["container_recreate_required_for_package_rollback"] is True
    assert hook.read_bytes() == b"after hook\n"
    service_run.write_bytes(MODULE.expected_service_run_after(manifest, service_run.read_bytes()))
    release_path = Path(manifest["release"]["host_directory"])
    assert release_path.is_dir()
    assert release_path.stat().st_mode & 0o777 == 0o755
    assert {path.name for path in release_path.iterdir()} == set(
        manifest["release"]["files"]
    )
    # The init hook installs into the release-local import root before rollback.
    (release_path / "python" / "context_shunt").mkdir(parents=True)
    (release_path / "python" / "context_shunt" / "__init__.py").write_text(
        "__version__ = 'candidate'\n"
    )

    rolled_back = MODULE.rollback(candidate, backup)
    assert rolled_back["result"] == "ROLLED_BACK"
    assert rolled_back["container_recreate_required_for_package_rollback"] is True
    assert hook.read_bytes() == b"before hook\n"
    assert service_run.read_bytes() == b"#!/bin/sh\nexport HERMES_S6_SUPERVISED_CHILD=1\n"
    assert not release_path.exists()
    assert (backup / "candidate-release.rollback").is_dir()

    with pytest.raises(RuntimeError, match="backup is not reusable"):
        MODULE.apply(candidate, backup)


def test_rollback_refuses_service_run_drift(tmp_path: Path, monkeypatch) -> None:
    candidate, hook, backup, manifest = make_candidate(tmp_path)
    monkeypatch.setattr(MODULE.os, "chown", lambda *_args: None)
    monkeypatch.setattr(MODULE.os, "fchown", lambda *_args: None)
    MODULE.apply(candidate, backup)
    service_run = Path(manifest["service_run"]["host_path"])
    service_run.write_bytes(b"#!/bin/sh\nconcurrent edit\n")
    with pytest.raises(RuntimeError, match="service run drift"):
        MODULE.rollback(candidate, backup)
    assert service_run.read_bytes() == b"#!/bin/sh\nconcurrent edit\n"


def test_apply_resumes_after_release_rename_interruption(tmp_path: Path, monkeypatch) -> None:
    candidate, hook, backup, manifest = make_candidate(tmp_path)
    monkeypatch.setattr(MODULE.os, "chown", lambda *_args: None)
    monkeypatch.setattr(MODULE.os, "fchown", lambda *_args: None)
    MODULE.create_backup(backup, hook, manifest)
    release_path = Path(manifest["release"]["host_directory"])
    MODULE.stage_release(candidate, release_path, backup, manifest)

    receipt = MODULE.apply(candidate, backup)

    assert receipt["result"] == "APPLIED"
    assert hook.read_bytes() == b"after hook\n"
    assert release_path.is_dir()


def test_rollback_resumes_after_hook_restore_interruption(tmp_path: Path, monkeypatch) -> None:
    candidate, hook, backup, manifest = make_candidate(tmp_path)
    monkeypatch.setattr(MODULE.os, "chown", lambda *_args: None)
    monkeypatch.setattr(MODULE.os, "fchown", lambda *_args: None)
    MODULE.apply(candidate, backup)
    hook_config = manifest["hook"]
    MODULE.atomic_restore(
        backup / "99-hermes-local-patches.before",
        hook,
        mode=int(hook_config["before_mode"], 8),
        uid=hook_config["before_uid"],
        gid=hook_config["before_gid"],
        staging_directory=backup,
    )

    receipt = MODULE.rollback(candidate, backup)

    assert receipt["result"] == "ROLLED_BACK"
    assert hook.read_bytes() == b"before hook\n"
    assert (backup / "candidate-release.rollback").is_dir()


def test_completed_apply_requires_a_fresh_backup(tmp_path: Path, monkeypatch) -> None:
    candidate, _hook, backup, _manifest = make_candidate(tmp_path)
    monkeypatch.setattr(MODULE.os, "chown", lambda *_args: None)
    monkeypatch.setattr(MODULE.os, "fchown", lambda *_args: None)
    MODULE.apply(candidate, backup)

    with pytest.raises(RuntimeError, match="apply.json"):
        MODULE.apply(candidate, backup)


def test_create_backup_fsyncs_backup_parent(tmp_path: Path, monkeypatch) -> None:
    candidate, hook, backup, manifest = make_candidate(tmp_path)
    del candidate
    monkeypatch.setattr(MODULE.os, "chown", lambda *_args: None)
    monkeypatch.setattr(MODULE.os, "fchown", lambda *_args: None)
    synced: list[Path] = []
    monkeypatch.setattr(MODULE, "fsync_directory", synced.append)

    MODULE.create_backup(backup, hook, manifest)

    assert synced[-2:] == [backup, backup.parent]


def test_apply_refuses_candidate_hook_changed_after_validation(
    tmp_path: Path, monkeypatch
) -> None:
    candidate, hook, backup, _manifest = make_candidate(tmp_path)
    monkeypatch.setattr(MODULE.os, "chown", lambda *_args: None)
    monkeypatch.setattr(MODULE.os, "fchown", lambda *_args: None)
    copy_new = MODULE.copy_new

    def tampering_copy(source: Path, target: Path, **metadata) -> None:
        if source == candidate / "99-hermes-local-patches.after":
            source.write_bytes(b"changed after candidate validation\n")
        copy_new(source, target, **metadata)

    monkeypatch.setattr(MODULE, "copy_new", tampering_copy)

    with pytest.raises(RuntimeError, match="staged hook checksum mismatch"):
        MODULE.apply(candidate, backup)

    assert hook.read_bytes() == b"before hook\n"
    assert (backup / "candidate-release.failed").is_dir()


def test_resumed_apply_does_not_overwrite_unknown_live_hook(
    tmp_path: Path, monkeypatch
) -> None:
    candidate, hook, backup, manifest = make_candidate(tmp_path)
    monkeypatch.setattr(MODULE.os, "chown", lambda *_args: None)
    monkeypatch.setattr(MODULE.os, "fchown", lambda *_args: None)
    MODULE.create_backup(backup, hook, manifest)
    release_path = Path(manifest["release"]["host_directory"])
    MODULE.stage_release(candidate, release_path, backup, manifest)
    unknown = b"external drift\n"
    hook.write_bytes(unknown)

    with pytest.raises(RuntimeError, match="neither the exact before nor after state"):
        MODULE.apply(candidate, backup)

    assert hook.read_bytes() == unknown
    assert release_path.is_dir()


def test_apply_verifies_snapshot_before_live_mutation(tmp_path: Path, monkeypatch) -> None:
    candidate, hook, backup, manifest = make_candidate(tmp_path)
    monkeypatch.setattr(MODULE.os, "chown", lambda *_args: None)
    monkeypatch.setattr(MODULE.os, "fchown", lambda *_args: None)
    copy_new = MODULE.copy_new
    before = hook.read_bytes()

    def racing_copy(source: Path, target: Path, **metadata) -> None:
        if source == hook and target == backup / "99-hermes-local-patches.before":
            hook.write_bytes(b"raced snapshot\n")
            copy_new(source, target, **metadata)
            hook.write_bytes(before)
            return
        copy_new(source, target, **metadata)

    monkeypatch.setattr(MODULE, "copy_new", racing_copy)

    with pytest.raises(RuntimeError, match="backup hook mismatch"):
        MODULE.apply(candidate, backup)

    assert hook.read_bytes() == before
    assert not Path(manifest["release"]["host_directory"]).exists()


def test_durable_rename_fsyncs_both_directories(tmp_path: Path, monkeypatch) -> None:
    source_directory = tmp_path / "source"
    target_directory = tmp_path / "target"
    source_directory.mkdir()
    target_directory.mkdir()
    source = source_directory / "item"
    target = target_directory / "item"
    source.write_text("data")
    synced: list[Path] = []
    monkeypatch.setattr(MODULE, "fsync_directory", synced.append)

    MODULE.durable_rename(source, target)

    assert target.read_text() == "data"
    assert synced == [source_directory, target_directory]
