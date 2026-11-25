import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helpers import FakeClock  # noqa: E402


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
