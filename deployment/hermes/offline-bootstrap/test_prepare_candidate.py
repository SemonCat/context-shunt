from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import subprocess
import tarfile

import pytest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("prepare_candidate", HERE / "prepare_candidate.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def inspected_hook() -> bytes:
    prefix = b"#!/command/with-contenv sh\nset -eu\nunrelated-before\n"
    old = b"/opt/hermes/.venv/bin/python -B /opt/data/context-shunt/old/ensure_core.py || exit 1\n"
    suffix = b"unrelated-after\n"
    return prefix + MODULE.BEGIN + old + MODULE.END + suffix


def manifest_for(hook: bytes) -> dict[str, object]:
    return {
        "hook": {"expected_before_sha256": MODULE.sha256(hook)},
        "release": {"container_directory": "/opt/data/context-shunt/ffa218c"},
    }


def test_renderer_changes_only_marked_body() -> None:
    before = inspected_hook()
    after = MODULE.render_hook(before, manifest_for(before))
    before_prefix, before_remainder = before.split(MODULE.BEGIN)
    _before_body, before_suffix = before_remainder.split(MODULE.END)
    after_prefix, after_remainder = after.split(MODULE.BEGIN)
    after_body, after_suffix = after_remainder.split(MODULE.END)
    assert (after_prefix, after_suffix) == (before_prefix, before_suffix)
    assert b"CONTEXT_SHUNT_VERSIONED_IMPORT_PATH" in after_body
    assert (
        b"/opt/hermes/.venv/bin/python -B "
        b"/opt/data/context-shunt/ffa218c/ensure_core.py || exit 1\n"
        in after_body
    )


def test_renderer_refuses_drift_and_duplicate_blocks() -> None:
    before = inspected_hook()
    manifest = manifest_for(before)
    with pytest.raises(ValueError, match="drift"):
        MODULE.render_hook(before + b"changed\n", manifest)
    manifest["hook"]["expected_before_sha256"] = MODULE.sha256(before + MODULE.BEGIN + MODULE.END)
    with pytest.raises(ValueError, match="exactly one"):
        MODULE.render_hook(before + MODULE.BEGIN + MODULE.END, manifest)


def test_versioned_release_precedes_and_restores_incumbent_in_fresh_process(
    tmp_path: Path,
) -> None:
    incumbent = tmp_path / "incumbent"
    release = tmp_path / "release" / "python"
    for root, version in ((incumbent, "incumbent"), (release, "candidate")):
        package = root / "context_shunt"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"__version__ = {version!r}\n")

    code = "import context_shunt; print(context_shunt.__version__)"
    with_candidate = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": f"{release}:{incumbent}"},
        check=True,
        capture_output=True,
        text=True,
    )
    after_rollback = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(incumbent)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert with_candidate.stdout.strip() == "candidate"
    assert after_rollback.stdout.strip() == "incumbent"


def test_repository_release_metadata_is_self_consistent() -> None:
    bootstrap = json.loads((HERE / "bootstrap.json").read_text())
    release = json.loads((HERE / "release.json").read_text())
    assert bootstrap["source_commit"] == release["commit"]
    assert release["wheel_sha256"] == bootstrap["release"]["files"][release["wheel"]]
    for name in ("ensure_core.py", "release.json", "verify_install.py"):
        assert MODULE.sha256((HERE / name).read_bytes()) == bootstrap["release"]["files"][name]
    assert MODULE.sha256((HERE / "activate_bootstrap.py").read_bytes()) == bootstrap[
        "candidate_files"
    ]["activate_bootstrap.py"]


def test_prepare_emits_complete_checksummed_candidate(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    hook = inspected_hook()
    hook_path = tmp_path / "99-hermes-local-patches"
    hook_path.write_bytes(hook)
    wheel = tmp_path / "context_shunt_core-1.1.0-py3-none-any.whl"
    wheel.write_bytes(b"attested-wheel-fixture")
    source_files = {
        "ensure_core.py": b"ensure\n",
        "release.json": b"{}\n",
        "verify_install.py": b"verify\n",
        "activate_bootstrap.py": b"activate\n",
    }
    for name, data in source_files.items():
        (source / name).write_bytes(data)
    manifest = {
        "source_commit": "5492046",
        "hook": {
            "host_path": "/host/hook",
            "expected_before_sha256": MODULE.sha256(hook),
            "target_mode": "0755",
        },
        "release": {
            "host_directory": "/host/release/ffa218c",
            "container_directory": "/opt/data/context-shunt/ffa218c",
            "directory_mode": "0755",
            "file_mode": "0444",
            "files": {
                wheel.name: MODULE.sha256(wheel.read_bytes()),
                **{
                    name: MODULE.sha256(data)
                    for name, data in source_files.items()
                    if name != "activate_bootstrap.py"
                },
            },
        },
        "candidate_files": {
            "activate_bootstrap.py": MODULE.sha256(source_files["activate_bootstrap.py"])
        },
        "service_run": {
            "host_path": "/run/service/gateway-default/run",
            "expected_before_sha256": "a" * 64,
        },
    }
    (source / "bootstrap.json").write_text(json.dumps(manifest) + "\n")
    monkeypatch.setattr(MODULE, "ROOT", source)
    monkeypatch.setattr(MODULE, "load_manifest", lambda: manifest)
    output = tmp_path / "candidate"

    MODULE.prepare(hook_path, wheel, output)

    checksums = (output / "SHA256SUMS").read_text().splitlines()
    assert len(checksums) == 10
    for line in checksums:
        expected, relative = line.split("  ", 1)
        assert MODULE.sha256((output / relative).read_bytes()) == expected


def test_committed_bundle_matches_trust_root_and_deployment_source() -> None:
    expected_digest, bundle_name = (HERE / "bundle.sha256").read_text().split()
    bundle = HERE / bundle_name
    assert MODULE.sha256(bundle.read_bytes()) == expected_digest
    root = bundle_name.removesuffix(".tar.gz")
    file_names = {
        "99-hermes-local-patches.after",
        "99-hermes-local-patches.before",
        "SHA256SUMS",
        "activate_bootstrap.py",
        "bootstrap.json",
        "candidate.json",
        "hook.diff",
        "release/context_shunt_core-1.1.0-py3-none-any.whl",
        "release/ensure_core.py",
        "release/release.json",
        "release/verify_install.py",
    }
    with tarfile.open(bundle, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        assert set(members) == {
            root,
            f"{root}/release",
            *(f"{root}/{name}" for name in file_names),
        }
        assert all(member.isdir() or member.isfile() for member in members.values())

        def archived(relative: str) -> bytes:
            extracted = archive.extractfile(f"{root}/{relative}")
            assert extracted is not None
            return extracted.read()

        checksums = archived("SHA256SUMS").decode().splitlines()
        assert {line.split("  ", 1)[1] for line in checksums} == file_names - {"SHA256SUMS"}
        for line in checksums:
            expected, relative = line.split("  ", 1)
            assert MODULE.sha256(archived(relative)) == expected
        for name in ("activate_bootstrap.py", "bootstrap.json"):
            assert archived(name) == (HERE / name).read_bytes()
        for name in ("ensure_core.py", "release.json", "verify_install.py"):
            assert archived(f"release/{name}") == (HERE / name).read_bytes()
        manifest = json.loads(archived("bootstrap.json"))
        before = archived("99-hermes-local-patches.before")
        after = MODULE.render_hook(before, manifest)
        assert archived("99-hermes-local-patches.after") == after
        assert MODULE.sha256(before) == manifest["hook"]["expected_before_sha256"]
        assert MODULE.sha256(after) == manifest["hook"]["expected_after_sha256"]
        expected_diff = "".join(
            MODULE.difflib.unified_diff(
                before.decode().splitlines(keepends=True),
                after.decode().splitlines(keepends=True),
                fromfile=manifest["hook"]["host_path"],
                tofile=manifest["hook"]["host_path"] + ".5492046",
            )
        ).encode()
        assert archived("hook.diff") == expected_diff
        release = json.loads((HERE / "release.json").read_text())
        assert MODULE.sha256(archived(f"release/{release['wheel']}")) == release[
            "wheel_sha256"
        ]
