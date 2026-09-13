#!/usr/bin/env python3
"""Verify the installed core against every package file in the pinned offline wheel."""

from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
import zipfile


ROOT = Path(__file__).resolve().parent


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

    if package_root is None:
        distribution = metadata.distribution("context-shunt-core")
        package_root = Path(distribution.locate_file("context_shunt")).resolve()
        package_version = distribution.version
        site = (
            Path(sys.prefix)
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        ).resolve()
        require(package_root.is_relative_to(site), "installed core is outside target virtualenv")
    require(package_version == "1.1.0", "installed core version mismatch")
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
