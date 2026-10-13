import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "ecam"


@pytest.fixture
def ecam_fixture():
    """Load a vendored live-capture fixture by filename (without .json)."""

    def _load(name: str):
        path = FIXTURES_DIR / f"{name}.json"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    return _load
