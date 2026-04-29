"""
Library manager — scan ``input_dir`` for SVGs and combine each file with its
:class:`IllustrationMapping` from the metadata store to produce a unified
view (filename, size, modified time, status, override count).

This module owns no state beyond the filesystem; it's a thin read layer over
the input directory plus :class:`MappingStore`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .mapping_store import IllustrationMapping, MappingStore, Status

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LibraryEntry:
    """One row in the Library tab."""

    filename: str               # basename, e.g. "figure-01.svg"
    path: Path                  # absolute path on disk
    size_bytes: int
    modified_iso: str
    status: Status
    override_count: int
    has_metadata: bool
    notes: str = ""

    @property
    def size_kb(self) -> float:
        return self.size_bytes / 1024.0


@dataclass
class LibraryManager:
    """
    Scan ``input_dir`` for ``.svg`` files and join them with metadata.

    Use :meth:`scan` to get fresh entries. The manager itself is stateless.
    """

    input_dir: Path
    store: MappingStore

    def __post_init__(self) -> None:
        self.input_dir = Path(self.input_dir)

    # ----- discovery ------------------------------------------------------- #
    def list_svg_paths(self) -> list[Path]:
        """Return sorted paths to all ``.svg`` files in ``input_dir``."""
        if not self.input_dir.is_dir():
            log.warning("Input dir %s does not exist.", self.input_dir)
            return []
        # Match both .svg and .SVG, no recursion (illustrations live flat in input/).
        out: list[Path] = []
        for p in self.input_dir.iterdir():
            if p.is_file() and p.suffix.lower() == ".svg":
                out.append(p)
        out.sort(key=lambda p: p.name.lower())
        return out

    # ----- scan ------------------------------------------------------------ #
    def scan(self) -> list[LibraryEntry]:
        """Build :class:`LibraryEntry` rows for every SVG in ``input_dir``."""
        entries: list[LibraryEntry] = []
        for path in self.list_svg_paths():
            entries.append(self._entry_for(path))
        return entries

    def _entry_for(self, path: Path) -> LibraryEntry:
        try:
            stat = path.stat()
            size = stat.st_size
            mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        except OSError as exc:
            log.warning("Cannot stat %s: %s", path, exc)
            size = 0
            mtime_iso = ""

        meta_path = self.store.metadata_path_for(path.name)
        has_meta = meta_path.is_file()
        if has_meta:
            mapping = self.store.load_illustration(path.name)
        else:
            mapping = IllustrationMapping(filename=path.name)
        return LibraryEntry(
            filename=path.name,
            path=path,
            size_bytes=size,
            modified_iso=mtime_iso,
            status=mapping.status,
            override_count=len(mapping.overrides),
            has_metadata=has_meta,
            notes=mapping.notes,
        )

    # ----- queries --------------------------------------------------------- #
    def by_status(self, status: Status) -> list[LibraryEntry]:
        return [e for e in self.scan() if e.status == status]

    def next_pending(self) -> Optional[LibraryEntry]:
        """Return the first entry whose status is ``pending``, or ``None``."""
        for e in self.scan():
            if e.status == "pending":
                return e
        return None

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"pending": 0, "in_progress": 0, "reviewed": 0, "exported": 0}
        for e in self.scan():
            counts[e.status] = counts.get(e.status, 0) + 1
        return counts

    # ----- mutations ------------------------------------------------------- #
    def mark(self, filename: str, status: Status) -> IllustrationMapping:
        """Set the status for one illustration. Persists immediately."""
        mapping = self.store.load_illustration(filename)
        mapping.with_status(status)
        self.store.save_illustration(mapping)
        return mapping
