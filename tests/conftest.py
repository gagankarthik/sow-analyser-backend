"""Test bootstrap.

Puts the Lambda source roots on sys.path so tests import the real modules the
way Lambda does:
  - ``lambdas/``          → the ``shared`` package (deployed as a layer)
  - ``lambdas/pipeline/`` → the ``stages`` package + ``handler``
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for rel in ("lambdas", "lambdas/pipeline"):
    p = str(_ROOT / rel)
    if p not in sys.path:
        sys.path.insert(0, p)
