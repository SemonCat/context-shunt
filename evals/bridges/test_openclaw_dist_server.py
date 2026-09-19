from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "evals/bridges/openclaw_dist_server.mjs"
OPENCLAW = Path("/Users/edisonpve/openclaw")


@pytest.mark.skipif(
    shutil.which("node") is None or not SERVER.exists() or not OPENCLAW.is_dir(),
    reason="current bundled OpenClaw/node checkout is unavailable",
)
def test_current_bundled_server_identity_protocol_and_clean_shutdown() -> None:
    env = {
        **os.environ,
        "OPENCLAW_STATE_DIR": "/Users/edisonpve/.openclaw",
    }
    process = subprocess.Popen(
        ["node", str(SERVER)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None and process.stdin is not None
    try:
        ready = json.loads(process.stdout.readline())
        assert ready["ready"] is True
        assert ready["identity"]["transport"] == "runtime.llm.complete/isolated-agent-runtime"
        assert ready["identity"]["requested_route"] == "sub2api-openai/gpt-5.6-luna"
        # Invalid bounded protocol input must not dispatch a model request.
        process.stdin.write(json.dumps({"id": 1, "system": "", "user": "", "max_output_tokens": 0, "timeout_ms": 1, "deadline_unix_ms": 0}) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert response["id"] == 1 and response["ok"] is False
    finally:
        process.terminate()
        process.wait(timeout=10)
        assert process.poll() is not None
