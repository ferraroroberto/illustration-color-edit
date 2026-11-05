"""The vendored conventions ratchet holds the repo to its `.fleet.toml` rules.

This repo's one verification command is pytest, so the ratchet runs here: it fails when a
tracked Python file gains a blocking ``subprocess.run`` with no ``timeout=`` beyond the
committed baseline (empty). Exit 3 means the tracked tree could not be read, which is its own
failure, never a pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "conventions_ratchet.py"

# Reading the tracked tree takes a second or two; the bound only turns a hung child into a
# failure instead of a stuck test run.
RUN_TIMEOUT_S = 300


def test_the_tracked_tree_has_no_new_convention_hits() -> None:
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=RUN_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(f"conventions ratchet did not finish within {RUN_TIMEOUT_S}s (NOT CONFIRMED)") from None
    assert proc.returncode == 0, (proc.stdout + proc.stderr).strip()
