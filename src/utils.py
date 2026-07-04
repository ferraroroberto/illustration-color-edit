"""Small stateless helpers shared across the CLI and Streamlit tabs.

Pure data logic only — never import ``streamlit`` here (mirrors the
``src/`` vs ``app/`` split documented in ``CLAUDE.md``).
"""

from __future__ import annotations


def format_bytes(n: int) -> str:
    """Human-readable file size: ``"1.23 MB"`` / ``"4.5 KB"`` / ``"512 B"``."""
    if n >= 1_048_576:
        return f"{n/1_048_576:.2f} MB"
    if n >= 1024:
        return f"{n/1024:.1f} KB"
    return f"{n} B"
