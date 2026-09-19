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
import shutil

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parent
PACKAGE_VERSION = "1.1.0"
DIST_INFO = "context_shunt_core-1.1.0.dist-info"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def versioned_site() -> Path:
    """The release-local import root selected by the default service run file."""
    return ROOT / "python"


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

    # Do not mutate the image's shared site-packages.  The hook places this
    # release-local root first on the default gateway's PYTHONPATH; rollback
    # restores the old hook and therefore the incumbent package import.
    site = versioned_site()
    package_root = site / "context_shunt"
    sys.path.insert(0, str(site))

    def matches() -> bool:
        try:
            if metadata.version("context-shunt-core") != PACKAGE_VERSION:
                return False
            installed = {
                path.relative_to(package_root).as_posix(): path
                for path in package_root.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and path.name != ".lock"
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

    if not matches():
        shutil.rmtree(package_root, ignore_errors=True)
        for dist_info in site.glob("context_shunt_core-*.dist-info"):
            shutil.rmtree(dist_info, ignore_errors=True)
        site.mkdir(mode=0o755, parents=True, exist_ok=True)
        process = subprocess.run(
            [
                "/usr/local/bin/uv",
                "--no-cache",
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--target",
                str(site),
                str(wheel),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        require(process.returncode == 0, "offline core install failed")
    require(matches(), "installed core does not match pinned wheel")
    import context_shunt

    require(context_shunt.__version__ == PACKAGE_VERSION, "imported core version mismatch")
    require(
        Path(context_shunt.__file__).resolve().is_relative_to(site),
        "imported core is outside the versioned release root",
    )


if __name__ == "__main__":
    main()
