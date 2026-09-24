from __future__ import annotations

import sys
from pathlib import Path

# These standalone container entry points are mounted together at /benchmark.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
