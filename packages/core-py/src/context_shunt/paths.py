"""Source authorization: roots, canonicalization, file identity and secret policy.

Nothing becomes a source unless it canonicalizes inside a configured workspace root,
is a regular file, and survives the secret policy. Rejections carry a code and a
bounded token - never the path, and never the matched value.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .errors import ShuntError

# Filenames and suffixes that are refused outright. Detection is best effort: this is a
# denylist, not a proof of secrecy, which is why the provider must also be an approved
# data destination and why chunks stay minimal.
_SECRET_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".netrc",
        "_netrc",
        "credentials",
        "auth.json",
        ".htpasswd",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".pgpass",
        "shadow",
        "master.key",
        "secrets.yaml",
        "secrets.yml",
        ".npmrc",
        ".pypirc",
        ".dockercfg",
    }
)
_SECRET_SUFFIXES = ("_rsa", "_dsa", "_ed25519")
_SECRET_EXTENSIONS = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk"})
_SECRET_DIR_PARTS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".docker"})

# Content markers checked on snapshot bytes, questions, answers and quotes.
_SECRET_CONTENT_MARKERS = (
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN PGP PRIVATE KEY BLOCK-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"aws_secret_access_key",
    b"AKIA",
    b"ghp_",
    b"github_pat_",
    b"xoxb-",
    b"xoxp-",
    b"sk-ant-",
)


@dataclass(frozen=True)
class PathPolicy:
    """Workspace roots plus an administrator denylist of relative globs."""

    roots: tuple[Path, ...]
    denylist: tuple[str, ...] = ()
    follow_symlinks: bool = False
    extra_secret_names: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_config(cls, roots: list[str], denylist: list[str] | None = None) -> PathPolicy:
        if not roots:
            raise ShuntError("UNSAFE_SOURCE", "NO_WORKSPACE_ROOT")
        resolved = tuple(Path(r).resolve(strict=False) for r in roots)
        return cls(roots=resolved, denylist=tuple(denylist or ()))


@dataclass(frozen=True)
class AuthorizedPath:
    """A path proven safe, together with the identity used to detect a swap mid-read."""

    real: Path
    dev: int
    ino: int
    size: int
    mtime_ns: int

    def identity(self) -> tuple[int, int, int, int]:
        return (self.dev, self.ino, self.size, self.mtime_ns)


def _is_secret_path(real: Path, policy: PathPolicy) -> bool:
    name = real.name
    lowered = name.lower()
    if lowered in _SECRET_NAMES or lowered in policy.extra_secret_names:
        return True
    if real.suffix.lower() in _SECRET_EXTENSIONS:
        return True
    if any(lowered.endswith(sfx) for sfx in _SECRET_SUFFIXES):
        return True
    if lowered.startswith(".env"):
        return True
    return any(part in _SECRET_DIR_PARTS for part in real.parts)


def _matches_denylist(real: Path, policy: PathPolicy) -> bool:
    for root in policy.roots:
        try:
            rel = real.relative_to(root)
        except ValueError:
            continue
        posix = PurePosixPath(rel.as_posix())
        for pattern in policy.denylist:
            if posix.match(pattern):
                return True
    return False


def authorize(path: str, policy: PathPolicy) -> AuthorizedPath:
    """Canonicalize and authorize one source path, or raise a bounded ShuntError.

    ``os.lstat`` is used before resolution so a symlink is seen as a symlink, and the
    descriptor identity captured here is re-checked after reading to detect a swap.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ShuntError("UNSAFE_SOURCE", "RELATIVE_PATH")
    try:
        lst = os.lstat(candidate)
    except OSError:
        raise ShuntError("UNSAFE_SOURCE", "NOT_FOUND") from None
    if stat.S_ISLNK(lst.st_mode) and not policy.follow_symlinks:
        raise ShuntError("UNSAFE_SOURCE", "SYMLINK")

    real = candidate.resolve(strict=False)
    if not any(real == root or root in real.parents for root in policy.roots):
        raise ShuntError("UNSAFE_SOURCE", "OUTSIDE_WORKSPACE_ROOT")
    if _is_secret_path(real, policy) or _matches_denylist(real, policy):
        raise ShuntError("UNSAFE_SOURCE", "SECRET_PATH")

    try:
        st = os.stat(real)
    except OSError:
        raise ShuntError("UNSAFE_SOURCE", "NOT_FOUND") from None
    if not stat.S_ISREG(st.st_mode):
        raise ShuntError("UNSAFE_SOURCE", "NOT_REGULAR_FILE")
    if st.st_nlink > 1:
        # A hardlinked file can be re-pointed outside the root between checks.
        raise ShuntError("UNSAFE_SOURCE", "HARDLINKED")
    return AuthorizedPath(
        real=real, dev=st.st_dev, ino=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns
    )


def contains_secret_marker(data: bytes) -> bool:
    return any(marker in data for marker in _SECRET_CONTENT_MARKERS)


def assert_no_secret(data: bytes, stage: str) -> None:
    """Reject the affected operation without echoing the matched value."""
    if contains_secret_marker(data):
        raise ShuntError("UNSAFE_SOURCE", f"SECRET_IN_{stage}")
