#!/usr/bin/env python3
"""Render a drift-bound Hermes offline-bootstrap candidate without touching live files."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys


ROOT = Path(__file__).resolve().parent
BEGIN = b"# BEGIN context-shunt pinned offline bootstrap\n"
END = b"# END context-shunt pinned offline bootstrap\n"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest() -> dict[str, object]:
    return json.loads((ROOT / "bootstrap.json").read_text())


def render_hook(before: bytes, manifest: dict[str, object]) -> bytes:
    if sha256(before) != manifest["hook"]["expected_before_sha256"]:  # type: ignore[index]
        raise ValueError("live hook drift: refusing to render")
    if before.count(BEGIN) != 1 or before.count(END) != 1:
        raise ValueError("expected exactly one context-shunt bootstrap block")
    prefix, remainder = before.split(BEGIN, 1)
    _old_body, suffix = remainder.split(END, 1)
    release = manifest["release"]  # type: ignore[assignment]
    versioned_site = str(release["container_directory"]) + "/python"  # type: ignore[index]
    command = (
        b"# CONTEXT_SHUNT_VERSIONED_IMPORT_PATH\n"
        + b'default_run="/run/service/gateway-default/run"\n'
        + b'expected_python_path='
        + shlex.quote(
            'export PYTHONPATH="'
            + versioned_site
            + '${PYTHONPATH:+:$PYTHONPATH}"'
        ).encode()
        + b'\n'
        + b'if [ -f "$default_run" ]; then\n'
        + b'  if grep -q CONTEXT_SHUNT_VERSIONED_IMPORT_PATH "$default_run"; then\n'
        + b'    grep -Fqx "$expected_python_path" "$default_run" || { echo "context-shunt service run marker drift" >&2; exit 1; }\n'
        + b'  else\n'
        + b'  tmp_run="${default_run}.context-shunt.$$"\n'
        + b"  awk -v shunt_python="
        + json.dumps(versioned_site).encode()
        + b" ' /^export HERMES_S6_SUPERVISED_CHILD=1$/ && !done { print \"# CONTEXT_SHUNT_VERSIONED_IMPORT_PATH\"; print \"export PYTHONPATH=\\\"\" shunt_python \"${PYTHONPATH:+:$PYTHONPATH}\\\"\"; done=1 } { print } END { if (!done) exit 42 }' \"$default_run\" > \"$tmp_run\"\n"
        + b'  chown --reference="$default_run" "$tmp_run" 2>/dev/null || chown 10000:10000 "$tmp_run"\n'
        + b'  chmod --reference="$default_run" "$tmp_run" 2>/dev/null || chmod 755 "$tmp_run"\n'
        + b'  mv -f "$tmp_run" "$default_run"\n'
        + b'  fi\n'
        + b"fi\n"
        + b"/opt/hermes/.venv/bin/python -B "
        + str(release["container_directory"]).encode()  # type: ignore[index]
        + b"/ensure_core.py || exit 1\n"
    )
    after = prefix + BEGIN + command + END + suffix
    after_prefix, after_remainder = after.split(BEGIN, 1)
    _after_body, after_suffix = after_remainder.split(END, 1)
    if (prefix, suffix) != (after_prefix, after_suffix):
        raise AssertionError("renderer changed bytes outside the shunt block")
    return after


def verify_release_inputs(wheel: Path, manifest: dict[str, object]) -> dict[str, bytes]:
    release = manifest["release"]  # type: ignore[assignment]
    expected = release["files"]  # type: ignore[index]
    inputs = {
        wheel.name: wheel.read_bytes(),
        "ensure_core.py": (ROOT / "ensure_core.py").read_bytes(),
        "release.json": (ROOT / "release.json").read_bytes(),
        "verify_install.py": (ROOT / "verify_install.py").read_bytes(),
    }
    if set(inputs) != set(expected):
        raise ValueError("release filenames do not match bootstrap.json")
    for name, data in inputs.items():
        if sha256(data) != expected[name]:
            raise ValueError(f"release input hash mismatch: {name}")
    return inputs


def write_new(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def prepare(hook: Path, wheel: Path, output: Path) -> None:
    manifest = load_manifest()
    before = hook.read_bytes()
    after = render_hook(before, manifest)
    release_inputs = verify_release_inputs(wheel, manifest)
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.mkdir(mode=0o700, parents=True)
    release = manifest["release"]  # type: ignore[assignment]
    release_dir = output / "release"
    release_dir.mkdir(mode=int(release["directory_mode"], 8))  # type: ignore[index]
    try:
        write_new(output / "99-hermes-local-patches.before", before, 0o600)
        write_new(
            output / "99-hermes-local-patches.after",
            after,
            int(manifest["hook"]["target_mode"], 8),  # type: ignore[index]
        )
        write_new(
            output / "activate_bootstrap.py",
            (ROOT / "activate_bootstrap.py").read_bytes(),
            0o500,
        )
        write_new(output / "bootstrap.json", (ROOT / "bootstrap.json").read_bytes(), 0o400)
        file_mode = int(release["file_mode"], 8)  # type: ignore[index]
        for name, data in release_inputs.items():
            write_new(release_dir / name, data, file_mode)
        diff = "".join(
            difflib.unified_diff(
                before.decode().splitlines(keepends=True),
                after.decode().splitlines(keepends=True),
                fromfile=str(manifest["hook"]["host_path"]),  # type: ignore[index]
                tofile=(
                    str(manifest["hook"]["host_path"])
                    + "."
                    + str(manifest["source_commit"])[:7]
                ),
            )
        ).encode()
        write_new(output / "hook.diff", diff, 0o600)
        receipt = {
            "schema": "context-shunt.hermes-offline-bootstrap-candidate.v1",
            "source_commit": manifest["source_commit"],
            "hook_before_sha256": sha256(before),
            "hook_after_sha256": sha256(after),
            "release_files": {name: sha256(data) for name, data in release_inputs.items()},
            "transaction": {
                "snapshot": [
                    manifest["hook"]["host_path"],  # type: ignore[index]
                    manifest["service_run"]["host_path"],  # type: ignore[index]
                    manifest["release"]["host_directory"],  # type: ignore[index]
                ],
                "drift_guard": manifest["hook"]["expected_before_sha256"],  # type: ignore[index]
                "atomic_targets": [
                    manifest["release"]["host_directory"],  # type: ignore[index]
                    manifest["hook"]["host_path"],  # type: ignore[index]
                    manifest["service_run"]["host_path"],  # type: ignore[index]
                ],
                "rollback_unit": "hook plus complete pinned release directory",
                "package_rollback": "discard and recreate the candidate container from the prior image/package preimage; preserve Aida and the incumbent container until Ruby's scoped cutover procedure",
                "service_run_snapshot": {
                    "path": manifest["service_run"]["host_path"],  # type: ignore[index]
                    "expected_before_sha256": manifest["service_run"]["expected_before_sha256"],  # type: ignore[index]
                    "rollback": "restore atomically from the transaction backup before restoring the hook",
                },
            },
        }
        write_new(
            output / "candidate.json",
            (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode(),
            0o600,
        )
        checksum_paths = [
            output / "99-hermes-local-patches.before",
            output / "99-hermes-local-patches.after",
            output / "activate_bootstrap.py",
            output / "bootstrap.json",
            output / "candidate.json",
            output / "hook.diff",
            *(release_dir / name for name in sorted(release_inputs)),
        ]
        checksums = "".join(
            f"{sha256(path.read_bytes())}  {path.relative_to(output).as_posix()}\n"
            for path in checksum_paths
        ).encode()
        write_new(output / "SHA256SUMS", checksums, 0o600)
    except Exception:
        shutil.rmtree(output)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hook", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.hook, args.wheel, args.output)
    except (OSError, ValueError) as error:
        print(f"prepare_candidate: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
