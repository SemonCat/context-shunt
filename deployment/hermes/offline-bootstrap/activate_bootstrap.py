#!/usr/bin/env python3
"""Apply or roll back the drift-bound Hermes bootstrap file transaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def require_regular(path: Path) -> os.stat_result:
    metadata = path.lstat()
    require(stat.S_ISREG(metadata.st_mode), f"not a regular file: {path}")
    require(not path.is_symlink(), f"symlink refused: {path}")
    return metadata


def require_metadata(path: Path, *, mode: int, uid: int, gid: int) -> None:
    metadata = require_regular(path)
    require(stat.S_IMODE(metadata.st_mode) == mode, f"mode drift: {path}")
    require(metadata.st_uid == uid, f"uid drift: {path}")
    require(metadata.st_gid == gid, f"gid drift: {path}")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_rename(source: Path, target: Path) -> None:
    """Rename and durably record removal and creation directory entries."""
    os.rename(source, target)
    fsync_directory(source.parent)
    if target.parent != source.parent:
        fsync_directory(target.parent)


def copy_new(source: Path, target: Path, *, mode: int, uid: int, gid: int) -> None:
    require_regular(source)
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fchmod(outgoing.fileno(), mode)
            os.fchown(outgoing.fileno(), uid, gid)
            os.fsync(outgoing.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise


def atomic_restore(
    source: Path,
    target: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
    staging_directory: Path | None = None,
) -> None:
    staging = target.parent if staging_directory is None else staging_directory
    staged = staging / f".{target.name}.context-shunt-restore-{os.getpid()}"
    require(not staged.exists(), f"restore staging path exists: {staged}")
    copy_new(source, staged, mode=mode, uid=uid, gid=gid)
    durable_rename(staged, target)


def load_manifest(candidate: Path) -> dict[str, Any]:
    manifest_path = candidate / "bootstrap.json"
    require_regular(manifest_path)
    return json.loads(manifest_path.read_text())


def verify_checksums(candidate: Path) -> None:
    checksum_file = candidate / "SHA256SUMS"
    require_regular(checksum_file)
    seen: set[str] = set()
    for line in checksum_file.read_text().splitlines():
        expected, separator, relative = line.partition("  ")
        require(separator == "  ", "malformed SHA256SUMS line")
        require(len(expected) == 64, "malformed SHA-256 in SHA256SUMS")
        relative_path = Path(relative)
        require(not relative_path.is_absolute() and ".." not in relative_path.parts, "unsafe path")
        require(relative not in seen, f"duplicate checksum path: {relative}")
        seen.add(relative)
        target = candidate / relative_path
        require_regular(target)
        require(sha256(target) == expected, f"candidate checksum mismatch: {relative}")
    required = {
        "99-hermes-local-patches.before",
        "99-hermes-local-patches.after",
        "activate_bootstrap.py",
        "bootstrap.json",
        "candidate.json",
        "hook.diff",
        "release/context_shunt_core-1.1.0-py3-none-any.whl",
        "release/ensure_core.py",
        "release/release.json",
        "release/verify_install.py",
    }
    require(seen == required, "SHA256SUMS path set differs from transaction contract")


def validate_candidate(candidate: Path, manifest: dict[str, Any]) -> None:
    require(candidate.is_dir() and not candidate.is_symlink(), "candidate directory invalid")
    verify_checksums(candidate)
    hook = manifest["hook"]
    require(
        sha256(candidate / "99-hermes-local-patches.before")
        == hook["expected_before_sha256"],
        "candidate hook preimage mismatch",
    )
    require(
        sha256(candidate / "99-hermes-local-patches.after")
        == hook["expected_after_sha256"],
        "candidate hook result mismatch",
    )
    for name, expected in manifest["release"]["files"].items():
        require(sha256(candidate / "release" / name) == expected, f"release hash mismatch: {name}")
    for name, expected in manifest["candidate_files"].items():
        require(sha256(candidate / name) == expected, f"candidate tool hash mismatch: {name}")


def live_paths(manifest: dict[str, Any]) -> tuple[Path, Path]:
    return Path(manifest["hook"]["host_path"]), Path(manifest["release"]["host_directory"])


def service_run_path(manifest: dict[str, Any]) -> Path:
    return Path(manifest["service_run"]["host_path"])


def expected_service_run_after(manifest: dict[str, Any], before: bytes) -> bytes:
    """Render the exact default-run edit made by the bootstrap shell block."""
    marker = b"# CONTEXT_SHUNT_VERSIONED_IMPORT_PATH\n"
    if marker in before:
        return before
    line = b"export HERMES_S6_SUPERVISED_CHILD=1\n"
    require(line in before, "default service run lacks the supervised-child insertion point")
    python_path = (
        b'export PYTHONPATH="'
        + str(manifest["release"]["container_directory"]).encode()
        + b'/python${PYTHONPATH:+:$PYTHONPATH}"\n'
    )
    insertion = marker + python_path
    return before.replace(line, insertion + line, 1)


def validate_service_run_preimage(manifest: dict[str, Any]) -> None:
    config = manifest["service_run"]
    path = service_run_path(manifest)
    require_metadata(
        path,
        mode=int(config["mode"], 8),
        uid=config["uid"],
        gid=config["gid"],
    )
    require(sha256(path) == config["expected_before_sha256"], "service run content drift")


def validate_live_hook_preimage(manifest: dict[str, Any]) -> None:
    hook_path, _release_path = live_paths(manifest)
    hook = manifest["hook"]
    require_metadata(
        hook_path,
        mode=int(hook["before_mode"], 8),
        uid=hook["before_uid"],
        gid=hook["before_gid"],
    )
    require(sha256(hook_path) == hook["expected_before_sha256"], "live hook content drift")


def validate_live_preimage(manifest: dict[str, Any]) -> None:
    _hook_path, release_path = live_paths(manifest)
    validate_live_hook_preimage(manifest)
    validate_service_run_preimage(manifest)
    require(not release_path.exists(), f"target release already exists: {release_path}")
    require(not release_path.is_symlink(), f"target release symlink refused: {release_path}")


def write_json(path: Path, value: dict[str, Any]) -> None:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    staged = path.parent / f".{path.name}.tmp-{os.getpid()}"
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fchown(handle.fileno(), 0, 0)
            os.fsync(handle.fileno())
        os.replace(staged, path)
        fsync_directory(path.parent)
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def create_backup(backup: Path, hook_path: Path, manifest: dict[str, Any]) -> None:
    require(not backup.exists(), f"backup already exists: {backup}")
    backup.mkdir(mode=0o700, parents=False)
    os.chown(backup, 0, 0)
    original = backup / "99-hermes-local-patches.before"
    copy_new(hook_path, original, mode=0o600, uid=0, gid=0)
    service_run = service_run_path(manifest)
    service_run_backup = backup / "gateway-default.run.before"
    copy_new(service_run, service_run_backup, mode=0o600, uid=0, gid=0)
    write_json(
        backup / "preimage.json",
        {
            "schema": "context-shunt.hermes-offline-bootstrap-preimage.v1",
            "hook": {
                "path": str(hook_path),
                "sha256": manifest["hook"]["expected_before_sha256"],
                "mode": manifest["hook"]["before_mode"],
                "uid": manifest["hook"]["before_uid"],
                "gid": manifest["hook"]["before_gid"],
            },
            "release": {
                "path": manifest["release"]["host_directory"],
                "state": "absent",
            },
            "service_run": {
                "path": str(service_run),
                "sha256": manifest["service_run"]["expected_before_sha256"],
                "mode": manifest["service_run"]["mode"],
                "uid": manifest["service_run"]["uid"],
                "gid": manifest["service_run"]["gid"],
                "expected_after_sha256": hashlib.sha256(
                    expected_service_run_after(
                        manifest, service_run_backup.read_bytes()
                    )
                ).hexdigest(),
            },
            "container_recreate_required_for_package_rollback": True,
        },
    )
    fsync_directory(backup)
    fsync_directory(backup.parent)


def stage_release(
    candidate: Path,
    release_path: Path,
    failure_directory: Path,
    manifest: dict[str, Any],
) -> None:
    release = manifest["release"]
    parent = validate_release_parent(release_path, release)
    staged = parent / f".{release_path.name}.stage-{os.getpid()}"
    require(not staged.exists(), f"release staging path exists: {staged}")
    staged.mkdir(mode=int(release["directory_mode"], 8))
    os.chmod(staged, int(release["directory_mode"], 8))
    os.chown(staged, release["uid"], release["gid"])
    try:
        for name in sorted(release["files"]):
            copy_new(
                candidate / "release" / name,
                staged / name,
                mode=int(release["file_mode"], 8),
                uid=release["uid"],
                gid=release["gid"],
            )
        fsync_directory(staged)
        durable_rename(staged, release_path)
    except Exception:
        if staged.exists():
            failed = failure_directory / "release-stage.failed"
            require(not failed.exists(), f"failed release staging backup exists: {failed}")
            durable_rename(staged, failed)
        raise


def validate_release_parent(release_path: Path, release: dict[str, Any]) -> Path:
    parent = release_path.parent
    require(parent.is_dir() and not parent.is_symlink(), f"release parent invalid: {parent}")
    parent_metadata = parent.stat()
    require(parent_metadata.st_uid == release["uid"], "release parent owner drift")
    require(stat.S_IMODE(parent_metadata.st_mode) & 0o022 == 0, "release parent is writable")
    return parent


def validate_release_at(release_path: Path, release: dict[str, Any]) -> None:
    release_stat = release_path.lstat()
    require(stat.S_ISDIR(release_stat.st_mode), "installed release is not a directory")
    require(not release_path.is_symlink(), "installed release is a symlink")
    require(stat.S_IMODE(release_stat.st_mode) == int(release["directory_mode"], 8), "release mode")
    require((release_stat.st_uid, release_stat.st_gid) == (release["uid"], release["gid"]), "release owner")
    # ensure_core.py populates the release-local import root after the file transaction;
    # it is owned by this release but is not one of the immutable staged inputs.
    python_root = release_path / "python"
    if python_root.exists():
        require(
            python_root.is_dir() and not python_root.is_symlink(),
            "installed python root invalid",
        )
    actual_names = {
        entry.name for entry in release_path.iterdir() if entry.name != "python"
    }
    require(actual_names == set(release["files"]), "installed release file set mismatch")
    for name, expected in release["files"].items():
        target = release_path / name
        require_metadata(
            target,
            mode=int(release["file_mode"], 8),
            uid=release["uid"],
            gid=release["gid"],
        )
        require(sha256(target) == expected, f"installed release hash mismatch: {name}")


def validate_release(manifest: dict[str, Any]) -> None:
    _hook_path, release_path = live_paths(manifest)
    validate_release_at(release_path, manifest["release"])


def hook_state(manifest: dict[str, Any]) -> str:
    hook_path, _release_path = live_paths(manifest)
    hook = manifest["hook"]
    metadata = require_regular(hook_path)
    current = sha256(hook_path)
    states = {
        "before": (
            hook["expected_before_sha256"],
            int(hook["before_mode"], 8),
            hook["before_uid"],
            hook["before_gid"],
        ),
        "after": (
            hook["expected_after_sha256"],
            int(hook["target_mode"], 8),
            hook["target_uid"],
            hook["target_gid"],
        ),
    }
    for name, (expected_hash, mode, uid, gid) in states.items():
        if (
            current == expected_hash
            and stat.S_IMODE(metadata.st_mode) == mode
            and metadata.st_uid == uid
            and metadata.st_gid == gid
        ):
            return name
    raise RuntimeError("live hook is neither the exact before nor after state")


def validate_installed(manifest: dict[str, Any]) -> None:
    require(hook_state(manifest) == "after", "installed hook mismatch")
    validate_release(manifest)


def validate_backup(backup: Path, manifest: dict[str, Any]) -> None:
    require(backup.is_dir() and not backup.is_symlink(), "backup directory invalid")
    metadata = backup.stat()
    require(stat.S_IMODE(metadata.st_mode) == 0o700, "backup mode is not 0700")
    require(
        (metadata.st_uid, metadata.st_gid) == (os.geteuid(), os.getegid()),
        "backup is not owned by deployer",
    )
    original = backup / "99-hermes-local-patches.before"
    require_regular(original)
    require(sha256(original) == manifest["hook"]["expected_before_sha256"], "backup hook mismatch")
    service_run_original = backup / "gateway-default.run.before"
    require_regular(service_run_original)
    require(
        sha256(service_run_original) == manifest["service_run"]["expected_before_sha256"],
        "backup service run mismatch",
    )
    preimage_path = backup / "preimage.json"
    require_regular(preimage_path)
    preimage = json.loads(preimage_path.read_text())
    require(preimage.get("schema") == "context-shunt.hermes-offline-bootstrap-preimage.v1", "bad preimage schema")
    require(preimage.get("hook", {}).get("sha256") == manifest["hook"]["expected_before_sha256"], "bad hook preimage")
    require(preimage.get("release", {}).get("state") == "absent", "bad release preimage")
    require(
        preimage.get("service_run", {}).get("sha256")
        == manifest["service_run"]["expected_before_sha256"],
        "bad service run preimage",
    )


def validate_apply_backup_state(backup: Path) -> None:
    terminal_artifacts = (
        "apply.json",
        "rollback.json",
        "candidate-release.failed",
        "candidate-release.rollback",
        "hook-stage.failed",
        "release-stage.failed",
    )
    for name in terminal_artifacts:
        require(not (backup / name).exists(), f"backup is not reusable: {name}")
    state_path = backup / "state.json"
    if state_path.exists():
        require_regular(state_path)
        state = json.loads(state_path.read_text()).get("state")
        require(
            state in {"RELEASE_READY", "HOOK_READY"},
            f"backup state is not resumable by apply: {state}",
        )


def restore_after_failed_apply(backup: Path, manifest: dict[str, Any]) -> None:
    hook_path, release_path = live_paths(manifest)
    hook = manifest["hook"]
    atomic_restore(
        backup / "gateway-default.run.before",
        service_run_path(manifest),
        mode=int(manifest["service_run"]["mode"], 8),
        uid=manifest["service_run"]["uid"],
        gid=manifest["service_run"]["gid"],
        # /run is commonly tmpfs while the durable transaction backup is on
        # /opt/hermes-data; staging in the destination namespace avoids EXDEV.
        staging_directory=service_run_path(manifest).parent,
    )
    staged_hook = backup / f".{hook_path.name}.stage-{os.getpid()}"
    if staged_hook.exists():
        failed_hook = backup / "hook-stage.failed"
        require(not failed_hook.exists(), f"failed hook staging backup exists: {failed_hook}")
        durable_rename(staged_hook, failed_hook)
    current_hook_state = hook_state(manifest)
    if current_hook_state == "after":
        atomic_restore(
            backup / "99-hermes-local-patches.before",
            hook_path,
            mode=int(hook["before_mode"], 8),
            uid=hook["before_uid"],
            gid=hook["before_gid"],
            staging_directory=backup,
        )
    if release_path.exists() or release_path.is_symlink():
        failed = backup / "candidate-release.failed"
        require(not failed.exists(), f"failed release backup exists: {failed}")
        durable_rename(release_path, failed)
    write_json(backup / "state.json", {"state": "RESTORED_AFTER_FAILED_APPLY"})


def apply(candidate: Path, backup: Path) -> dict[str, Any]:
    manifest = load_manifest(candidate)
    validate_candidate(candidate, manifest)
    hook_path, release_path = live_paths(manifest)
    require(hook_path.parent.is_dir() and not hook_path.parent.is_symlink(), "hook parent invalid")
    validate_release_parent(release_path, manifest["release"])
    require(backup.parent.is_dir() and not backup.parent.is_symlink(), "backup parent invalid")
    backup_parent_stat = backup.parent.stat()
    require(stat.S_IMODE(backup_parent_stat.st_mode) == 0o700, "backup parent mode is not 0700")
    require(backup_parent_stat.st_uid == os.geteuid(), "backup parent owner is not deployer")
    require(backup.parent.stat().st_dev == release_path.parent.stat().st_dev, "backup crosses filesystem")
    require(backup.parent.stat().st_dev == hook_path.parent.stat().st_dev, "hook crosses filesystem")
    if backup.exists():
        validate_backup(backup, manifest)
        validate_apply_backup_state(backup)
    else:
        validate_live_preimage(manifest)
        create_backup(backup, hook_path, manifest)
        validate_backup(backup, manifest)
    try:
        current_hook_state = hook_state(manifest)
        if release_path.exists() or release_path.is_symlink():
            validate_release(manifest)
        else:
            require(current_hook_state == "before", "release absent while new hook is active")
            stage_release(candidate, release_path, backup, manifest)
        write_json(backup / "state.json", {"state": "RELEASE_READY"})
        current_hook_state = hook_state(manifest)
        if current_hook_state == "before":
            staged_hook = backup / f".{hook_path.name}.stage-{os.getpid()}"
            copy_new(
                candidate / "99-hermes-local-patches.after",
                staged_hook,
                mode=int(manifest["hook"]["target_mode"], 8),
                uid=manifest["hook"]["target_uid"],
                gid=manifest["hook"]["target_gid"],
            )
            require_metadata(
                staged_hook,
                mode=int(manifest["hook"]["target_mode"], 8),
                uid=manifest["hook"]["target_uid"],
                gid=manifest["hook"]["target_gid"],
            )
            require(
                sha256(staged_hook) == manifest["hook"]["expected_after_sha256"],
                "staged hook checksum mismatch",
            )
            validate_live_hook_preimage(manifest)
            validate_release(manifest)
            durable_rename(staged_hook, hook_path)
        write_json(backup / "state.json", {"state": "HOOK_READY"})
        validate_installed(manifest)
        receipt = {
            "schema": "context-shunt.hermes-offline-bootstrap-apply.v1",
            "result": "APPLIED",
            "backup": str(backup),
            "hook_sha256": manifest["hook"]["expected_after_sha256"],
            "release": manifest["release"]["files"],
            "container_recreate_required_for_package_rollback": True,
        }
        write_json(backup / "apply.json", receipt)
        return receipt
    except Exception:
        restore_after_failed_apply(backup, manifest)
        raise


def rollback(candidate: Path, backup: Path) -> dict[str, Any]:
    manifest = load_manifest(candidate)
    validate_candidate(candidate, manifest)
    hook_path, release_path = live_paths(manifest)
    require(hook_path.parent.is_dir() and not hook_path.parent.is_symlink(), "hook parent invalid")
    validate_release_parent(release_path, manifest["release"])
    validate_backup(backup, manifest)
    require(backup.stat().st_dev == release_path.parent.stat().st_dev, "backup crosses filesystem")
    require(backup.stat().st_dev == hook_path.parent.stat().st_dev, "hook crosses filesystem")
    original = backup / "99-hermes-local-patches.before"
    service_original = backup / "gateway-default.run.before"
    service_path = service_run_path(manifest)
    require_metadata(
        service_path,
        mode=int(manifest["service_run"]["mode"], 8),
        uid=manifest["service_run"]["uid"],
        gid=manifest["service_run"]["gid"],
    )
    service_current_hash = sha256(service_path)
    service_preimage = json.loads((backup / "preimage.json").read_text())["service_run"]
    require(
        service_current_hash
        in {
            service_preimage["sha256"],
            service_preimage["expected_after_sha256"],
        },
        "service run drift: refusing to overwrite concurrent edits",
    )
    archived = backup / "candidate-release.rollback"
    current_hook_state = hook_state(manifest)
    if archived.exists():
        require(current_hook_state == "before", "archive exists while new hook is active")
        require(
            not release_path.exists() and not release_path.is_symlink(),
            "archive and live release both exist",
        )
        validate_release_at(archived, manifest["release"])
    else:
        require(release_path.exists() and not release_path.is_symlink(), "live release is absent")
        validate_release(manifest)
        require(backup.stat().st_dev == release_path.stat().st_dev, "archive crosses filesystem")
    hook = manifest["hook"]
    atomic_restore(
        service_original,
        service_path,
        mode=int(manifest["service_run"]["mode"], 8),
        uid=manifest["service_run"]["uid"],
        gid=manifest["service_run"]["gid"],
        staging_directory=service_path.parent,
    )
    if current_hook_state == "after":
        atomic_restore(
            original,
            hook_path,
            mode=int(hook["before_mode"], 8),
            uid=hook["before_uid"],
            gid=hook["before_gid"],
            staging_directory=backup,
        )
        write_json(backup / "state.json", {"state": "HOOK_RESTORED"})
    if release_path.exists():
        durable_rename(release_path, archived)
    write_json(backup / "state.json", {"state": "ROLLED_BACK"})
    validate_live_preimage(manifest)
    receipt = {
        "schema": "context-shunt.hermes-offline-bootstrap-rollback.v1",
        "result": "ROLLED_BACK",
        "hook_sha256": manifest["hook"]["expected_before_sha256"],
        "candidate_release_archive": str(archived),
        "container_recreate_required_for_package_rollback": True,
    }
    write_json(backup / "rollback.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preflight", "apply", "rollback"))
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, "activation must run as root")
        manifest = load_manifest(args.candidate)
        if args.action == "preflight":
            validate_candidate(args.candidate, manifest)
            validate_live_preimage(manifest)
            result = {"result": "READY", "source_commit": manifest["source_commit"]}
        else:
            require(args.backup is not None, "--backup is required")
            result = (
                apply(args.candidate, args.backup)
                if args.action == "apply"
                else rollback(args.candidate, args.backup)
            )
        print(json.dumps(result, sort_keys=True))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"activate_bootstrap: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
