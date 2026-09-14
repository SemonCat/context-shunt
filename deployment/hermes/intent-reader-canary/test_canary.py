from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("intent_reader_canary", HERE / "__init__.py")
assert SPEC and SPEC.loader
CANARY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CANARY)


def test_non_gateway_registration_is_inert_without_settings(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["hermes", "dashboard"])
    CANARY.register(object())


def test_live_claim_is_not_stolen(tmp_path: Path) -> None:
    claim = tmp_path / "claimed.json"
    first = CANARY._claim_once(claim)
    assert first is not None
    assert CANARY._claim_once(claim) is None
    first.close()


def test_released_claim_is_recovered(tmp_path: Path) -> None:
    claim = tmp_path / "claimed.json"
    claim.write_text(json.dumps({"pid": 999_999_999}))
    lease = CANARY._claim_once(claim)
    assert lease is not None
    current = json.loads(claim.read_text())
    assert current["pid"] == __import__("os").getpid()
    lease.close()


def test_completion_is_transaction_scoped(tmp_path: Path) -> None:
    done = tmp_path / "done.json"
    done.write_text(json.dumps({"transaction_id": "old", "commit": "abc", "pid": 10}))
    assert CANARY._receipt_matches(
        done, {"transaction_id": "new", "commit": "abc", "pid": 10}
    ) is False
    assert CANARY._receipt_matches(
        done, {"transaction_id": "old", "commit": "abc", "pid": 11}
    ) is False
    assert CANARY._receipt_matches(
        done, {"transaction_id": "old", "commit": "abc", "pid": 10}
    ) is True
    done.write_text("null")
    assert CANARY._receipt_matches(
        done, {"transaction_id": "old", "commit": "abc", "pid": 10}
    ) is False
