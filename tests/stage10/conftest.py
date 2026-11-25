"""Make shared Stage 3/4/5 deterministic fakes importable for Stage 10."""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent.parent
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
