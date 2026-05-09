"""Delivery snapshots — freeze project state at handoff time.

After a CMYK batch run lands clean PDFs in ``output_cmyk/``, the
illustrator delivers them to a publisher. Six weeks later the publisher
asks "can you re-export figure 04.03 with a tiny tweak?" — the global
maps may have moved on, the ICC profile may have been swapped, or the
semantic-palette theme may have been reshuffled. Without a snapshot,
reproducing the exact delivered file is guesswork.

A snapshot bundles:

  * Copies of ``config.json``, ``color-config.json``, and (when
    present) ``semantic-palette.json``.
  * A manifest of every PDF in the delivery, with SHA-256 checksums.
  * Hardlinks (or copies) of the actual PDFs.
  * A README summarizing the delivery for the publisher's records.

Snapshots live under ``deliveries/<YYYY-MM-DD>-<slug>/``. They are
self-contained and never read by the running pipeline; they exist
purely as a reproducibility / audit artifact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Manifest schema
# --------------------------------------------------------------------------- #
@dataclass
class DeliveryFile:
    source_filename: str
    output_filename: str
    sha256: str
    bytes: int


@dataclass
class DeliveryManifest:
    delivery_id: str
    label: str
    timestamp: str
    icc_profile: str = ""
    pdfx: bool = False
    width_inches: float = 0.0
    height_inches: float = 0.0
    bleed_inches: float = 0.0
    files: list[DeliveryFile] = field(default_factory=list)


def _slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text)
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    norm = re.sub(r"[^a-zA-Z0-9\s\-_]", "", norm).lower()
    norm = re.sub(r"[\s_]+", "-", norm).strip("-")
    return norm or "untitled"


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink ``src`` to ``dst``; fall back to copy across filesystems / on FS errors."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except (OSError, NotImplementedError):
        shutil.copy2(src, dst)


# --------------------------------------------------------------------------- #
# Snapshot creation
# --------------------------------------------------------------------------- #
def create_snapshot(
    *,
    label: str,
    project_root: Path,
    output_dir: Path,
    deliveries_dir: Optional[Path] = None,
    pdf_pattern: str = "*_CMYK.pdf",
    icc_profile: str = "",
    pdfx: bool = False,
    width_inches: float = 0.0,
    height_inches: float = 0.0,
    bleed_inches: float = 0.0,
) -> Path:
    """Create a delivery snapshot.

    :param label: human-readable label, e.g. ``"acme-2026-05"``. Slugified
        for the directory name. Required.
    :param project_root: where ``config.json`` etc. live.
    :param output_dir: where the CMYK PDFs that should be included live.
    :param deliveries_dir: where to write the snapshot. Defaults to
        ``project_root / "deliveries"``.
    :param pdf_pattern: glob the output dir with this pattern. The default
        matches the historical naming; pass ``"*.pdf"`` for custom
        templates.
    :returns: the path to the created delivery directory.

    Idempotent in the sense that the directory name includes the
    timestamp — running twice in the same minute creates two separate
    snapshots side-by-side rather than overwriting.
    """
    if not label.strip():
        raise ValueError("label is required for delivery snapshots")
    project_root = Path(project_root)
    output_dir = Path(output_dir)
    deliveries_dir = deliveries_dir or project_root / "deliveries"

    now = datetime.now(timezone.utc)
    delivery_id = f"{now.strftime('%Y-%m-%d-%H%M%S')}-{_slugify(label)}"
    target = deliveries_dir / delivery_id
    target.mkdir(parents=True, exist_ok=False)

    # Copy config files we care about. Missing files are skipped silently
    # — a project that hasn't created `semantic-palette.json` yet still
    # produces a valid delivery.
    for src_name in ("config.json", "color-config.json", "semantic-palette.json"):
        src = project_root / src_name
        if src.is_file():
            shutil.copy2(src, target / f"{src_name}.snapshot")

    # Hardlink every matching PDF + soft-proof PNG into pdfs/.
    pdfs_dir = target / "pdfs"
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    files: list[DeliveryFile] = []
    for pdf in sorted(output_dir.glob(pdf_pattern)):
        dst = pdfs_dir / pdf.name
        _link_or_copy(pdf, dst)
        files.append(DeliveryFile(
            source_filename=pdf.name,
            output_filename=pdf.name,
            sha256=_sha256_of(pdf),
            bytes=pdf.stat().st_size,
        ))
        # Carry the soft-proof PNG along when one exists. Useful for
        # the publisher to eyeball without opening Acrobat.
        preview = pdf.with_name(pdf.stem + "_preview.png")
        if preview.is_file():
            _link_or_copy(preview, pdfs_dir / preview.name)

    manifest = DeliveryManifest(
        delivery_id=delivery_id,
        label=label,
        timestamp=now.isoformat(timespec="seconds"),
        icc_profile=icc_profile,
        pdfx=pdfx,
        width_inches=width_inches,
        height_inches=height_inches,
        bleed_inches=bleed_inches,
        files=files,
    )
    (target / "manifest.json").write_text(
        json.dumps(_dict_for_manifest(manifest), indent=2) + "\n",
        encoding="utf-8",
    )
    (target / "README.md").write_text(_render_readme(manifest), encoding="utf-8")
    log.info("Delivery snapshot written: %s (%d files)", target, len(files))
    return target


def _dict_for_manifest(m: DeliveryManifest) -> dict:
    raw = asdict(m)
    return raw


def _render_readme(m: DeliveryManifest) -> str:
    rows = "\n".join(
        f"| {f.source_filename} | {f.bytes:,} | {f.sha256[:12]}… |"
        for f in m.files
    ) or "| _(no files)_ | | |"
    pdfx = "PDF/X-1a:2003" if m.pdfx else "plain DeviceCMYK"
    return f"""# Delivery: {m.label}

- **Delivery ID:** `{m.delivery_id}`
- **Timestamp (UTC):** {m.timestamp}
- **ICC profile:** `{m.icc_profile or '(unspecified)'}`
- **Output mode:** {pdfx}
- **Trim:** {m.width_inches:.3f} × {m.height_inches:.3f} in (bleed {m.bleed_inches:.3f})
- **File count:** {len(m.files)}

## Reproducibility

Configuration snapshots in this folder:

- `config.json.snapshot` — paths, ICC, trim/bleed, etc.
- `color-config.json.snapshot` — global maps, matching, print safety.
- `semantic-palette.json.snapshot` — slot bindings + active theme (when present).

To reproduce this delivery, drop the three snapshot files back into the
project root (renaming `.snapshot` off) and re-run the CMYK batch.

## Files

| File | Bytes | SHA-256 |
|------|------:|---------|
{rows}
"""


__all__ = [
    "DeliveryFile",
    "DeliveryManifest",
    "create_snapshot",
]
