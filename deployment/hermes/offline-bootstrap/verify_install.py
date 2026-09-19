#!/usr/bin/env python3
"""Verify the installed core against every package file in the pinned offline wheel."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import os
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parent
PACKAGE_VERSION = "1.1.0"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def verify(
    wheel: Path,
    release: dict[str, str],
    *,
    package_root: Path | None = None,
    package_version: str | None = None,
) -> dict[str, object]:
    require(sys.flags.optimize == 0, "optimized Python is unsupported for verification")
    require(
        hashlib.sha256(wheel.read_bytes()).hexdigest() == release["wheel_sha256"],
        "offline wheel hash mismatch",
    )
    with zipfile.ZipFile(wheel) as archive:
        expected = {
            Path(name).relative_to("context_shunt").as_posix(): hashlib.sha256(
                archive.read(name)
            ).hexdigest()
            for name in archive.namelist()
            if name.startswith("context_shunt/") and not name.endswith("/")
        }

    default_root = package_root is None
    if default_root:
        # ensure_core installs with --target ROOT/python.  Never discover the
        # incumbent distribution from the interpreter's global site-packages.
        versioned_root = (ROOT / "python").resolve()
        distribution = next(
            (
                candidate
                for candidate in metadata.distributions(path=[str(versioned_root)])
                if candidate.metadata.get("Name", "").lower() == "context-shunt-core"
            ),
            None,
        )
        require(distribution is not None, "versioned core distribution is absent")
        package_root = (versioned_root / "context_shunt").resolve()
        package_version = distribution.version
        require(
            Path(distribution.locate_file("context_shunt")).resolve() == package_root,
            "installed core is outside the versioned release root",
        )
    require(package_version == PACKAGE_VERSION, "installed core version mismatch")
    installed = {
        path.relative_to(package_root).as_posix(): path
        for path in package_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    require(set(installed) == set(expected), "installed core file set differs from wheel")
    for relative, expected_hash in expected.items():
        require(
            hashlib.sha256(installed[relative].read_bytes()).hexdigest() == expected_hash,
            f"installed core file hash mismatch: {relative}",
        )

    digest = hashlib.sha256()
    for relative, path in sorted(installed.items()):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    tree_hash = digest.hexdigest()
    require(
        tree_hash == release["installed_core_tree_sha256"],
        "installed core tree hash mismatch",
    )
    if default_root:
        # Validate origin in a genuinely fresh interpreter, so an incumbent module
        # already present in this verifier cannot make the check appear successful.
        env = {**os.environ, "PYTHONPATH": str((ROOT / "python").resolve()), "PYTHONNOUSERSITE": "1"}
        probe = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import context_shunt, pathlib; p=pathlib.Path(context_shunt.__file__).resolve(); r=pathlib.Path(__import__('sys').argv[1]).resolve(); raise SystemExit(0 if p.is_relative_to(r) else 1)",
                str((ROOT / "python").resolve()),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        require(probe.returncode == 0, "fresh import did not originate from versioned release root")
    return {
        "core_file_count": len(installed),
        "core_tree_sha256": tree_hash,
        "package_version": package_version,
        "wheel_sha256": release["wheel_sha256"],
    }


def main() -> None:
    release = json.loads((ROOT / "release.json").read_text())
    print(json.dumps(verify(ROOT / release["wheel"], release), sort_keys=True))


if __name__ == "__main__":
    main()
