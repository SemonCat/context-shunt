"""Strict offline core-only install. Never upgrades or downgrades dependencies."""

from __future__ import annotations

from email.parser import BytesParser
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parent
PACKAGE_VERSION = "1.1.0"
DIST_INFO = "context_shunt_core-1.1.0.dist-info"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def installed_site() -> Path:
    return (
        Path(sys.prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )


def main() -> None:
    require(sys.flags.optimize == 0, "optimized Python is unsupported for bootstrap")
    manifest = json.loads((ROOT / "release.json").read_text())
    wheel = ROOT / manifest["wheel"]
    require(
        hashlib.sha256(wheel.read_bytes()).hexdigest() == manifest["wheel_sha256"],
        "offline wheel hash mismatch",
    )

    with zipfile.ZipFile(wheel) as archive:
        package = {
            Path(name).relative_to("context_shunt").as_posix(): hashlib.sha256(
                archive.read(name)
            ).hexdigest()
            for name in archive.namelist()
            if name.startswith("context_shunt/") and not name.endswith("/")
        }
        wheel_metadata = BytesParser().parsebytes(archive.read(f"{DIST_INFO}/METADATA"))
    for raw_requirement in wheel_metadata.get_all("Requires-Dist", []):
        requirement = Requirement(raw_requirement)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed_version = metadata.version(requirement.name)
        except metadata.PackageNotFoundError as error:
            raise RuntimeError(f"required distribution is absent: {requirement.name}") from error
        require(
            installed_version in requirement.specifier,
            f"required distribution is incompatible: {requirement.name}",
        )

    site = installed_site()
    package_root = site / "context_shunt"

    def matches() -> bool:
        try:
            if metadata.version("context-shunt-core") != PACKAGE_VERSION:
                return False
            installed = {
                path.relative_to(package_root).as_posix(): path
                for path in package_root.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            }
            if set(installed) != set(package):
                return False
            if any(
                hashlib.sha256(installed[relative].read_bytes()).hexdigest() != expected_hash
                for relative, expected_hash in package.items()
            ):
                return False
            digest = hashlib.sha256()
            for relative, path in sorted(installed.items()):
                digest.update(relative.encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
            return digest.hexdigest() == manifest["installed_core_tree_sha256"]
        except (FileNotFoundError, metadata.PackageNotFoundError):
            return False

    excluded = "context-shunt-core"
    before = sorted(
        (distribution.metadata["Name"], distribution.version)
        for distribution in metadata.distributions()
        if distribution.metadata["Name"].lower().replace("_", "-") != excluded
    )
    if not matches():
        process = subprocess.run(
            [
                "/usr/local/bin/uv",
                "--no-cache",
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--reinstall-package",
                "context-shunt-core",
                "--python",
                sys.executable,
                str(wheel),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        require(process.returncode == 0, "offline core install failed")
    require(matches(), "installed core does not match pinned wheel")
    after = sorted(
        (distribution.metadata["Name"], distribution.version)
        for distribution in metadata.distributions()
        if distribution.metadata["Name"].lower().replace("_", "-") != excluded
    )
    require(after == before, "offline install changed another distribution")

    import context_shunt

    require(context_shunt.__version__ == PACKAGE_VERSION, "imported core version mismatch")
    require(
        Path(context_shunt.__file__).resolve().is_relative_to(site),
        "imported core is outside the target virtualenv",
    )


if __name__ == "__main__":
    main()
