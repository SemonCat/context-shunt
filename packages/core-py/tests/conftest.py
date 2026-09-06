from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "packages" / "core-py" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CONTRACTS = REPO_ROOT / "contracts" / "v1"


def load_json(path: Path):
    with path.open("rb") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def contracts_dir() -> Path:
    return CONTRACTS


@pytest.fixture(scope="session")
def gate_cases():
    return load_json(CONTRACTS / "conformance" / "gate-cases.json")


@pytest.fixture(scope="session")
def line_cases():
    return load_json(CONTRACTS / "conformance" / "line-count-cases.json")


@pytest.fixture(scope="session")
def citation_cases():
    return load_json(CONTRACTS / "conformance" / "citation-cases.json")


@pytest.fixture(scope="session")
def spill_cases():
    return load_json(CONTRACTS / "conformance" / "spill-cases.json")
