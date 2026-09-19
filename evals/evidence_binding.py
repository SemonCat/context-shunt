"""Non-circular source binding for generated evaluation evidence.

Generated reports are release artifacts, so committing them necessarily changes the Git
commit and tree after the provider run.  Binding a report to ``HEAD`` therefore creates an
impossible fixed point.  This module hashes every tracked byte except the explicit
generated-evidence outputs.  A later artifact-only commit keeps the source manifest
stable, while any code, contract, corpus, prompt, scorer, config, or tooling change moves
it.  The artifact itself remains independently SHA-256 bound by release attestation.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


GENERATED_EVIDENCE_PATHS = frozenset(
    {
        "docs/five-workflow-real-luna.md",
        "evals/intent-reader-audit/real-luna-lanes-latest.json",
        "evals/intent-reader-audit/real-luna-latest.json",
    }
)


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


def source_manifest_sha256(root: Path) -> str:
    """Hash the path, mode, and current bytes of every tracked non-evidence file."""
    digest = hashlib.sha256()
    paths = sorted(
        path.decode("utf-8", "surrogateescape")
        for path in _git(root, "ls-files", "-z").split(b"\0")
        if path
    )
    for relative in paths:
        if relative in GENERATED_EVIDENCE_PATHS:
            continue
        path = root / relative
        stat = path.lstat()
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(format(stat.st_mode & 0o7777, "o").encode("ascii"))
        digest.update(b"\0")
        if path.is_symlink():
            payload = os.readlink(path).encode("utf-8", "surrogateescape")
        else:
            payload = path.read_bytes()
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def regular_file_tree_sha256(root: Path) -> str:
    """Bind generated files and dependency-link topology without traversing dependencies.

    OpenClaw's generated ``dist/extensions/*/node_modules`` contains pnpm links. Those
    links are part of the runtime topology, but their installed dependency contents are
    outside this checkout's source identity and are deliberately not traversed.
    """
    lexical_root = root
    if lexical_root.is_symlink():
        raise ValueError(f"runtime artifact root symlink refused: {lexical_root.name}")
    root = lexical_root.resolve()
    if not root.is_dir():
        raise ValueError(f"runtime artifact root is not a directory: {root.name}")
    digest = hashlib.sha256()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        dirnames[:] = sorted(dirnames)
        filenames = sorted(filenames)
        for name in list(dirnames):
            path = directory_path / name
            relative_path = path.relative_to(root)
            if path.is_symlink():
                if "node_modules" not in relative_path.parts:
                    raise ValueError(f"runtime artifact symlink refused: {relative_path}")
                _add_link_digest(digest, relative_path, path)
                dirnames.remove(name)
        for name in filenames:
            path = directory_path / name
            relative_path = path.relative_to(root)
            if "node_modules" in relative_path.parts:
                if path.is_symlink():
                    _add_link_digest(digest, relative_path, path)
                continue
            if path.is_symlink():
                raise ValueError(f"runtime artifact symlink refused: {relative_path}")
            relative = relative_path.as_posix()
            payload = path.read_bytes()
            digest.update(b"file\0")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(format(path.stat().st_mode & 0o7777, "o").encode("ascii"))
            digest.update(b"\0")
            digest.update(str(len(payload)).encode("ascii"))
            digest.update(b"\0")
            digest.update(payload)
            digest.update(b"\0")
    return digest.hexdigest()


def _add_link_digest(digest: "hashlib._Hash", relative_path: Path, path: Path) -> None:
    """Record a dependency link itself, never the target it may escape to."""
    digest.update(b"link\0")
    digest.update(relative_path.as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
    digest.update(b"\0")


def non_evidence_dirty_paths(root: Path) -> list[str]:
    """Dirty paths that are not the known generated evidence outputs."""
    raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = [entry for entry in raw.split(b"\0") if entry]
    out: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        status = entry[:2]
        relative = entry[3:].decode("utf-8", "surrogateescape")
        if relative not in GENERATED_EVIDENCE_PATHS:
            out.append(relative)
        # Porcelain v1 emits a second NUL path for renames/copies.
        if b"R" in status or b"C" in status:
            index += 1
            if index < len(entries):
                other = entries[index].decode("utf-8", "surrogateescape")
                if other not in GENERATED_EVIDENCE_PATHS:
                    out.append(other)
        index += 1
    return sorted(set(out))
