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
