"""
Persistence for the global color map and per-illustration overrides.

Two stores:

* **Global color map** lives in ``config.json`` under ``global_color_map``.
  This is the canonical "this red always becomes this gray" registry shared
  across the whole book.

* **Per-illustration mapping** lives in ``metadata/<filename>.mapping.json``.
  It records: status (pending / in_progress / reviewed / exported), per-file
  overrides, last-updated timestamp, and free-form notes.

The store also computes a cross-library *history*: for each source color,
how many distinct illustrations have mapped it to each target. The Editor
tab uses this to surface suggestions like "the red #E74C3C was previously
mapped to #333333 in 4 other illustrations — reuse?".
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional

log = logging.getLogger(__name__)


Status = Literal["pending", "in_progress", "reviewed", "exported"]
VALID_STATUSES: tuple[Status, ...] = ("pending", "in_progress", "reviewed", "exported")


# --------------------------------------------------------------------------- #
# Per-illustration mapping
# --------------------------------------------------------------------------- #
@dataclass
class IllustrationMapping:
    """In-memory representation of ``metadata/<name>.mapping.json``.

    Carries state for **both** pipelines on the same illustration:

    * ``status`` + ``overrides`` — the grayscale workflow.
    * ``cmyk_status`` + ``cmyk_overrides`` — the CMYK print-export workflow.

    The two are independent — an illustration can be ``reviewed`` for grayscale
    while still ``pending`` for CMYK, and vice versa.
    """

    filename: str
    status: Status = "pending"
    overrides: dict[str, str] = field(default_factory=dict)
    cmyk_status: Status = "pending"
    cmyk_overrides: dict[str, str] = field(default_factory=dict)
    updated_at: str = ""
    notes: str = ""

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def with_status(self, new_status: Status) -> "IllustrationMapping":
        if new_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {new_status!r}. Allowed: {VALID_STATUSES}")
        self.status = new_status
        self.touch()
        return self

    def with_cmyk_status(self, new_status: Status) -> "IllustrationMapping":
        if new_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {new_status!r}. Allowed: {VALID_STATUSES}")
        self.cmyk_status = new_status
        self.touch()
        return self

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        # Normalize hex case in stored data.
        d["overrides"] = {k.upper(): v.upper() for k, v in self.overrides.items()}
        d["cmyk_overrides"] = {k.upper(): v.upper() for k, v in self.cmyk_overrides.items()}
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "IllustrationMapping":
        overrides_raw = raw.get("overrides") or {}
        if not isinstance(overrides_raw, dict):
            raise ValueError(f"'overrides' must be a dict, got {type(overrides_raw).__name__}")
        cmyk_raw = raw.get("cmyk_overrides") or {}
        if not isinstance(cmyk_raw, dict):
            raise ValueError(f"'cmyk_overrides' must be a dict, got {type(cmyk_raw).__name__}")
        return cls(
            filename=str(raw.get("filename", "")),
            status=_coerce_status(raw.get("status", "pending")),
            overrides={str(k).upper(): str(v).upper() for k, v in overrides_raw.items()},
            cmyk_status=_coerce_status(raw.get("cmyk_status", "pending")),
            cmyk_overrides={str(k).upper(): str(v).upper() for k, v in cmyk_raw.items()},
            updated_at=str(raw.get("updated_at", "")),
            notes=str(raw.get("notes", "")),
        )


def _coerce_status(value: object) -> Status:
    s = str(value).lower()
    if s not in VALID_STATUSES:
        log.warning("Unknown status %r; defaulting to 'pending'.", value)
        return "pending"
    return s  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Atomic JSON IO
# --------------------------------------------------------------------------- #
def _atomic_write_json(path: Path, payload: object) -> None:
    """Write ``payload`` to ``path`` atomically (tempfile + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup if rename failed.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# MappingStore
# --------------------------------------------------------------------------- #
@dataclass
class MappingStore:
    """
    Manage the global color map (in ``config_path``) and per-illustration
    overrides (in ``metadata_dir``).

    All hex codes are stored canonical-uppercase (``#RRGGBB``).
    """

    config_path: Path
    metadata_dir: Path

    def __post_init__(self) -> None:
        self.config_path = Path(self.config_path)
        self.metadata_dir = Path(self.metadata_dir)

    # ----- global map ------------------------------------------------------ #
    def load_config_raw(self) -> dict[str, object]:
        """Load the full ``config.json`` (or ``{}`` if it doesn't exist yet)."""
        if not self.config_path.is_file():
            return {}
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def load_global_map(self) -> dict[str, dict[str, str]]:
        """Return the canonical-keyed global color map."""
        raw = self.load_config_raw()
        gm = raw.get("global_color_map", {}) or {}
        return {
            str(k).upper(): {
                "target": str(v.get("target", "")).upper(),
                "label": str(v.get("label", "")),
                "notes": str(v.get("notes", "")),
            }
            for k, v in gm.items()
        }

    def save_global_map(self, mapping: dict[str, dict[str, str]]) -> None:
        """Persist the global color map back into ``config.json`` in place."""
        cleaned = {
            str(k).upper(): {
                "target": str(v.get("target", "")).upper(),
                "label": str(v.get("label", "")),
                "notes": str(v.get("notes", "")),
            }
            for k, v in mapping.items()
        }
        raw = self.load_config_raw()
        raw["global_color_map"] = cleaned
        _atomic_write_json(self.config_path, raw)

    def upsert_global_entry(
        self,
        source_hex: str,
        target_hex: str,
        label: str = "",
        notes: str = "",
    ) -> None:
        """Insert or update one global entry."""
        gm = self.load_global_map()
        gm[source_hex.upper()] = {
            "target": target_hex.upper(),
            "label": label,
            "notes": notes,
        }
        self.save_global_map(gm)

    def remove_global_entry(self, source_hex: str) -> bool:
        gm = self.load_global_map()
        if source_hex.upper() in gm:
            del gm[source_hex.upper()]
            self.save_global_map(gm)
            return True
        return False

    # ----- CMYK correction map (parallel to global map) -------------------- #
    def load_cmyk_correction_map(self) -> dict[str, dict[str, str]]:
        """Return the canonical-keyed CMYK pre-correction map.

        Mirrors :meth:`load_global_map` but reads ``cmyk_correction_map`` from
        ``config.json``. Values are RGB→RGB pre-corrections that nudge colors
        into a print-safe RGB starting point before the ICC profile does the
        final CMYK conversion. See ``docs/2026-05-07-cmyk-pipeline.md``.
        """
        raw = self.load_config_raw()
        gm = raw.get("cmyk_correction_map", {}) or {}
        return {
            str(k).upper(): {
                "target": str(v.get("target", "")).upper(),
                "label": str(v.get("label", "")),
                "notes": str(v.get("notes", "")),
            }
            for k, v in gm.items()
        }

    def save_cmyk_correction_map(self, mapping: dict[str, dict[str, str]]) -> None:
        """Persist the CMYK pre-correction map back into ``config.json``."""
        cleaned = {
            str(k).upper(): {
                "target": str(v.get("target", "")).upper(),
                "label": str(v.get("label", "")),
                "notes": str(v.get("notes", "")),
            }
            for k, v in mapping.items()
        }
        raw = self.load_config_raw()
        raw["cmyk_correction_map"] = cleaned
        _atomic_write_json(self.config_path, raw)

    def upsert_cmyk_correction_entry(
        self,
        source_hex: str,
        target_hex: str,
        label: str = "",
        notes: str = "",
    ) -> None:
        gm = self.load_cmyk_correction_map()
        gm[source_hex.upper()] = {
            "target": target_hex.upper(),
            "label": label,
            "notes": notes,
        }
        self.save_cmyk_correction_map(gm)

    def remove_cmyk_correction_entry(self, source_hex: str) -> bool:
        gm = self.load_cmyk_correction_map()
        if source_hex.upper() in gm:
            del gm[source_hex.upper()]
            self.save_cmyk_correction_map(gm)
            return True
        return False

    # ----- per-illustration ------------------------------------------------ #
    def metadata_path_for(self, svg_filename: str) -> Path:
        """Return the metadata path for an illustration filename."""
        # Strip directories — only the basename is meaningful.
        base = Path(svg_filename).name
        return self.metadata_dir / f"{base}.mapping.json"

    def load_illustration(self, svg_filename: str) -> IllustrationMapping:
        """
        Load per-illustration mapping. If no metadata file exists yet,
        return a fresh ``IllustrationMapping(status='pending')`` — does NOT
        write anything to disk.
        """
        path = self.metadata_path_for(svg_filename)
        base = Path(svg_filename).name
        if not path.is_file():
            return IllustrationMapping(filename=base)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.error("Corrupt metadata at %s: %s. Returning empty mapping.", path, exc)
            return IllustrationMapping(filename=base)
        m = IllustrationMapping.from_dict(raw)
        # Defensive: filename inside file should match what we asked for.
        if m.filename != base:
            log.warning("Metadata %s claims filename=%r, expected %r — using disk value.",
                        path, m.filename, base)
        return m

    def save_illustration(self, mapping: IllustrationMapping) -> None:
        """Persist a per-illustration mapping. Stamps ``updated_at``."""
        if not mapping.filename:
            raise ValueError("IllustrationMapping.filename must be set before saving.")
        mapping.touch()
        _atomic_write_json(self.metadata_path_for(mapping.filename), mapping.to_dict())

    def delete_illustration(self, svg_filename: str) -> bool:
        path = self.metadata_path_for(svg_filename)
        if path.is_file():
            path.unlink()
            return True
        return False

    def all_illustrations(self) -> list[IllustrationMapping]:
        """Load every per-illustration metadata file in ``metadata_dir``."""
        if not self.metadata_dir.is_dir():
            return []
        out: list[IllustrationMapping] = []
        for p in sorted(self.metadata_dir.glob("*.mapping.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                out.append(IllustrationMapping.from_dict(raw))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Skipping unreadable metadata %s: %s", p, exc)
        return out

    # ----- cross-library history ------------------------------------------- #
    def history(self) -> dict[str, dict[str, int]]:
        """
        Build cross-library mapping history.

        Returns ``{source_hex: {target_hex: count}}`` — across all
        per-illustration overrides AND the global map (which always
        contributes count 1).
        """
        hist: dict[str, dict[str, int]] = {}

        # Global map contributes 1 each.
        for src, entry in self.load_global_map().items():
            tgt = entry.get("target", "").upper()
            if tgt:
                hist.setdefault(src.upper(), {})[tgt] = hist.get(src.upper(), {}).get(tgt, 0) + 1

        # Per-illustration overrides each contribute 1.
        for m in self.all_illustrations():
            for src, tgt in m.overrides.items():
                hist.setdefault(src.upper(), {})[tgt.upper()] = (
                    hist.get(src.upper(), {}).get(tgt.upper(), 0) + 1
                )
        return hist

    def usage_counts(self) -> dict[str, int]:
        """How many illustrations use each global-map source color."""
        counts: dict[str, int] = {}
        for m in self.all_illustrations():
            for src in m.overrides:
                counts[src.upper()] = counts.get(src.upper(), 0) + 1
        return counts

    # ----- CMYK history & usage (parallel to grayscale) -------------------- #
    def cmyk_history(self) -> dict[str, dict[str, int]]:
        """Cross-library CMYK mapping history.

        Returns ``{source_hex: {target_hex: count}}`` aggregated across the
        global ``cmyk_correction_map`` and every per-illustration
        ``cmyk_overrides`` block.
        """
        hist: dict[str, dict[str, int]] = {}
        for src, entry in self.load_cmyk_correction_map().items():
            tgt = entry.get("target", "").upper()
            if tgt:
                hist.setdefault(src.upper(), {})[tgt] = hist.get(src.upper(), {}).get(tgt, 0) + 1
        for m in self.all_illustrations():
            for src, tgt in m.cmyk_overrides.items():
                hist.setdefault(src.upper(), {})[tgt.upper()] = (
                    hist.get(src.upper(), {}).get(tgt.upper(), 0) + 1
                )
        return hist

    def cmyk_usage_counts(self) -> dict[str, int]:
        """How many illustrations have a per-file override for each source color."""
        counts: dict[str, int] = {}
        for m in self.all_illustrations():
            for src in m.cmyk_overrides:
                counts[src.upper()] = counts.get(src.upper(), 0) + 1
        return counts

    # ----- maintenance ----------------------------------------------------- #
    def cleanup_identity_entries(self) -> dict[str, int]:
        """Strip identity (``target == source``) entries from CMYK storage.

        Walks every per-file ``cmyk_overrides`` block and the project-wide
        ``cmyk_correction_map``, removing any entry that maps a color to
        itself. Identity entries are no-op corrections — they take up
        space, surface as misleading suggestions in the history dropdown,
        and pollute "Replace globally" pre-flight counts. Older save flows
        wrote them by accident; this is the one-shot migration to clean up.

        Returns ``{"global": N, "files": M, "metadata_files_touched": K}``
        with the counts of entries removed and metadata files rewritten.
        """
        report = {"global": 0, "files": 0, "metadata_files_touched": 0}

        # Global cmyk_correction_map.
        gm = self.load_cmyk_correction_map()
        cleaned_gm = {
            k: v for k, v in gm.items()
            if v.get("target", "").upper() != k.upper()
        }
        if len(cleaned_gm) != len(gm):
            report["global"] = len(gm) - len(cleaned_gm)
            self.save_cmyk_correction_map(cleaned_gm)

        # Per-file cmyk_overrides.
        for illu in self.all_illustrations():
            cleaned = {
                k: v for k, v in illu.cmyk_overrides.items()
                if v.upper() != k.upper()
            }
            removed = len(illu.cmyk_overrides) - len(cleaned)
            if removed:
                illu.cmyk_overrides = cleaned
                self.save_illustration(illu)
                report["files"] += removed
                report["metadata_files_touched"] += 1
        return report


# --------------------------------------------------------------------------- #
# Convenience helpers
# --------------------------------------------------------------------------- #
def merge_mappings(
    global_map: dict[str, dict[str, str]],
    overrides: dict[str, str],
) -> dict[str, str]:
    """
    Flatten a global map plus per-illustration overrides into a single
    ``source_hex -> target_hex`` dict suitable for the SVG writer.

    Per-illustration overrides win on conflict. Used for both pipelines:
    pass ``global_color_map`` + ``overrides`` for grayscale,
    ``cmyk_correction_map`` + ``cmyk_overrides`` for CMYK pre-correction.
    """
    out: dict[str, str] = {
        k.upper(): v["target"].upper() for k, v in global_map.items() if v.get("target")
    }
    out.update({k.upper(): v.upper() for k, v in overrides.items()})
    return out
