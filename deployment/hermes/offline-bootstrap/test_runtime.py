from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest


HERE = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VERIFY = load_module("verify_install", HERE / "verify_install.py")


def make_wheel(path: Path, package: dict[str, bytes]) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        for relative, data in package.items():
            archive.writestr(f"context_shunt/{relative}", data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hash(package: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative, data in sorted(package.items()):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def test_verifier_checks_every_path_and_body_before_import(tmp_path: Path) -> None:
    package = {"__init__.py": b'__version__ = "1.1.0"\n', "data/item.json": b"{}\n"}
    package_root = tmp_path / "installed"
    for relative, data in package.items():
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    wheel = tmp_path / "context_shunt_core-1.1.0-py3-none-any.whl"
    release = {
        "wheel_sha256": make_wheel(wheel, package),
        "installed_core_tree_sha256": tree_hash(package),
    }
    result = VERIFY.verify(
        wheel,
        release,
        package_root=package_root,
        package_version="1.1.0",
    )
    assert result["core_file_count"] == 2

    (package_root / "data/item.json").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="file hash mismatch"):
        VERIFY.verify(
            wheel,
            release,
            package_root=package_root,
            package_version="1.1.0",
        )


def test_default_verifier_uses_versioned_root_not_global_incumbent(tmp_path: Path, monkeypatch) -> None:
    package = {"__init__.py": b'__version__ = "1.1.0"\n', "data/item.json": b"candidate\n"}
    incumbent = tmp_path / "incumbent"
    (incumbent / "context_shunt").mkdir(parents=True)
    (incumbent / "context_shunt" / "__init__.py").write_bytes(package["__init__.py"])
    (incumbent / "context_shunt" / "data").mkdir()
    (incumbent / "context_shunt" / "data/item.json").write_bytes(b"stale incumbent\n")

    release_root = tmp_path / "release"
    package_root = release_root / "python" / "context_shunt"
    for relative, data in package.items():
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    dist_info = release_root / "python" / "context_shunt_core-1.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: context-shunt-core\nVersion: 1.1.0\n")
    wheel = tmp_path / "context_shunt_core-1.1.0-py3-none-any.whl"
    release = {
        "wheel_sha256": make_wheel(wheel, package),
        "installed_core_tree_sha256": tree_hash(package),
    }
    original_root = VERIFY.ROOT
    VERIFY.ROOT = release_root
    original_distributions = VERIFY.metadata.distributions
    calls: list[list[str] | None] = []

    def guarded_distributions(**kwargs):
        path = kwargs.get("path")
        calls.append(path)
        assert path == [str((release_root / "python").resolve())]
        return original_distributions(**kwargs)

    monkeypatch.setattr(VERIFY.metadata, "distributions", guarded_distributions)
    try:
        result = VERIFY.verify(wheel, release)
    finally:
        VERIFY.ROOT = original_root
    assert result["core_file_count"] == 2
    assert calls == [[str((release_root / "python").resolve())]]


def test_runtime_tools_reject_optimized_python() -> None:
    environment = {**os.environ, "PYTHONOPTIMIZE": "1"}
    for script, message in (
        ("ensure_core.py", "optimized Python is unsupported for bootstrap"),
        ("verify_install.py", "optimized Python is unsupported for verification"),
    ):
        process = subprocess.run(
            [sys.executable, "-B", str(HERE / script)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        assert process.returncode != 0
        assert message in process.stderr


def test_attested_wheel_when_supplied() -> None:
    raw = os.environ.get("CONTEXT_SHUNT_ATTESTED_WHEEL")
    if not raw:
        pytest.skip("attested release wheel not supplied")
    wheel = Path(raw)
    release = json.loads((HERE / "release.json").read_text())
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == release["wheel_sha256"]


def test_bundled_wheel_tree_matches_release() -> None:
    release = json.loads((HERE / "release.json").read_text())
    _expected_digest, bundle_name = (HERE / "bundle.sha256").read_text().split()
    root = bundle_name.removesuffix(".tar.gz")
    with tarfile.open(HERE / bundle_name, "r:gz") as bundle:
        extracted = bundle.extractfile(f"{root}/release/{release['wheel']}")
        assert extracted is not None
        wheel = extracted.read()
    assert hashlib.sha256(wheel).hexdigest() == release["wheel_sha256"]
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        package = {
            Path(name).relative_to("context_shunt").as_posix(): archive.read(name)
            for name in archive.namelist()
            if name.startswith("context_shunt/") and not name.endswith("/")
        }
    assert tree_hash(package) == release["installed_core_tree_sha256"]
