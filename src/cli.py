"""
Batch CLI for the SVG color-to-grayscale conversion pipeline.

Run with::

    python -m src.cli --help

Subcommands:

  ``status``     Print per-file status (pending / in_progress / reviewed / exported).
  ``inspect``    Show the colors found in a single SVG and proposed mapping.
  ``convert``    Convert all (or only ``--only-reviewed``) SVGs in ``input/``
                 to ``output/`` using the current global map + per-file
                 overrides. Marks each converted file as ``exported``.

Exit code is non-zero if:

  * print-safety warnings occur and ``print_safety.warn_only`` is ``False``
  * any file failed to convert
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .color_mapper import ColorMapper, MatchKind
from .config import AppConfig, configure_logging, load_config
from .library_manager import LibraryEntry, LibraryManager
from .mapping_store import MappingStore, merge_mappings
from .print_safety import SafetyWarning, check_mapping
from .svg_parser import parse_svg
from .svg_writer import write_converted_svg

log = logging.getLogger("color_edit.cli")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@dataclass
class RunSummary:
    converted: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file, reason)
    failed: list[tuple[str, str]] = field(default_factory=list)
    unmapped_per_file: dict[str, list[str]] = field(default_factory=dict)
    safety_warnings: list[SafetyWarning] = field(default_factory=list)

    def to_text(self) -> str:
        lines = []
        lines.append(f"Converted: {len(self.converted)}")
        lines.append(f"Skipped:   {len(self.skipped)}")
        lines.append(f"Failed:    {len(self.failed)}")

        if self.skipped:
            lines.append("")
            lines.append("Skipped:")
            for f, why in self.skipped:
                lines.append(f"  - {f}  ({why})")

        if self.failed:
            lines.append("")
            lines.append("Failed:")
            for f, why in self.failed:
                lines.append(f"  - {f}: {why}")

        files_with_unmapped = {f: u for f, u in self.unmapped_per_file.items() if u}
        if files_with_unmapped:
            lines.append("")
            lines.append("Unmapped colors per file:")
            for f, colors in files_with_unmapped.items():
                lines.append(f"  - {f}: {', '.join(sorted(colors))}")

        if self.safety_warnings:
            lines.append("")
            lines.append("Print-safety warnings:")
            for w in self.safety_warnings:
                lines.append(f"  - {w}")

        return "\n".join(lines)


def _build_pieces(cfg: AppConfig) -> tuple[MappingStore, LibraryManager]:
    cfg_path = cfg.source_path or Path("config.json")
    store = MappingStore(cfg_path, cfg.paths.metadata_dir)
    library = LibraryManager(cfg.paths.input_dir, store)
    return store, library


# --------------------------------------------------------------------------- #
# Subcommand: status
# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace, cfg: AppConfig) -> int:
    _, library = _build_pieces(cfg)
    entries = library.scan()
    if not entries:
        print(f"No SVG files in {cfg.paths.input_dir}.")
        return 0
    counts = library.status_counts()
    print(f"Library: {cfg.paths.input_dir}")
    print(f"  pending={counts['pending']}  in_progress={counts['in_progress']}  "
          f"reviewed={counts['reviewed']}  exported={counts['exported']}")
    print()
    print(f"{'STATUS':<13} {'OVERRIDES':>9} {'SIZE(KB)':>9}  FILE")
    for e in entries:
        print(f"{e.status:<13} {e.override_count:>9} {e.size_kb:>9.1f}  {e.filename}")
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: inspect
# --------------------------------------------------------------------------- #
def cmd_inspect(args: argparse.Namespace, cfg: AppConfig) -> int:
    store, _ = _build_pieces(cfg)
    path = Path(args.path)
    if not path.is_file():
        log.error("File not found: %s", path)
        return 2
    parsed = parse_svg(path)
    if not parsed.colors:
        print(f"{path.name}: no concrete colors found.")
        return 0

    illu = store.load_illustration(path.name)
    mapper = ColorMapper(global_map=store.load_global_map(), matching=cfg.matching)
    mapper = mapper.with_overrides(illu.overrides)

    print(f"{path.name}: {parsed.unique_color_count} unique colors")
    print(f"{'SOURCE':<10} {'COUNT':>5}  {'KIND':<6}  {'TARGET':<10}  DETAILS")
    for src in sorted(parsed.colors, key=lambda h: -parsed.colors[h].count):
        usage = parsed.colors[src]
        s = mapper.suggest(src)
        target = s.target or "—"
        detail = ""
        if s.kind is MatchKind.NEAR:
            detail = f"near {s.via} (Δ{cfg.matching.metric.upper()}={s.distance:.2f})"
        elif s.kind is MatchKind.EXACT and s.label:
            detail = s.label
        elif s.kind is MatchKind.NONE:
            detail = "no match — needs manual mapping"
        print(f"{src:<10} {usage.count:>5}  {s.kind.value:<6}  {target:<10}  {detail}")
    return 0


# --------------------------------------------------------------------------- #
# Subcommand: convert
# --------------------------------------------------------------------------- #
def cmd_convert(args: argparse.Namespace, cfg: AppConfig) -> int:
    store, library = _build_pieces(cfg)
    cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)

    entries = library.scan()
    if args.only_reviewed:
        entries = [e for e in entries if e.status == "reviewed"]
    if args.file:
        wanted = {Path(f).name for f in args.file}
        entries = [e for e in entries if e.filename in wanted]

    if not entries:
        log.warning("No matching files to convert.")
        return 0

    global_map = store.load_global_map()
    summary = RunSummary()

    for entry in entries:
        try:
            _convert_one(
                entry, store, global_map, cfg, summary,
                dry_run=args.dry_run, force=args.force,
            )
        except Exception as exc:  # pragma: no cover - defensive top-level
            log.exception("Failed to convert %s", entry.filename)
            summary.failed.append((entry.filename, str(exc)))

    print(summary.to_text())

    # Exit code policy
    if summary.failed:
        return 3
    if summary.safety_warnings and not cfg.print_safety.warn_only:
        return 4
    return 0


def _convert_one(
    entry: LibraryEntry,
    store: MappingStore,
    global_map: dict[str, dict[str, str]],
    cfg: AppConfig,
    summary: RunSummary,
    *,
    dry_run: bool,
    force: bool,
) -> None:
    illu = store.load_illustration(entry.filename)
    merged = merge_mappings(global_map, illu.overrides)

    parsed = parse_svg(entry.path)
    unmapped_here = [h for h in parsed.colors if h not in merged]

    # Skip files that have unmapped colors AND no saved overrides AND aren't
    # already marked reviewed — unless --force.
    needs_review = (
        unmapped_here
        and not illu.overrides
        and entry.status != "reviewed"
        and not force
    )
    if needs_review:
        summary.skipped.append((entry.filename, f"{len(unmapped_here)} unmapped colors"))
        summary.unmapped_per_file[entry.filename] = unmapped_here
        return

    summary.unmapped_per_file[entry.filename] = unmapped_here

    file_warnings = check_mapping(
        {h: merged[h] for h in parsed.colors if h in merged},
        cfg.print_safety,
    )
    summary.safety_warnings.extend(file_warnings)

    if dry_run:
        log.info("[dry-run] %s would write to %s", entry.filename,
                 cfg.paths.output_dir / entry.filename)
        summary.converted.append(entry.filename)
        return

    dst = cfg.paths.output_dir / entry.filename
    write_converted_svg(entry.path, merged, dst)
    illu.with_status("exported")
    store.save_illustration(illu)
    summary.converted.append(entry.filename)


# --------------------------------------------------------------------------- #
# Argparse wiring
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="color_edit",
        description="SVG color-to-grayscale batch processor.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging.")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="List library status counts and per-file rows.")

    p_inspect = sub.add_parser("inspect", help="Inspect one SVG.")
    p_inspect.add_argument("path", type=str, help="Path to an SVG file.")

    p_convert = sub.add_parser("convert", help="Batch convert SVGs.")
    p_convert.add_argument("--only-reviewed", action="store_true",
                           help="Only process files with status 'reviewed'.")
    p_convert.add_argument("--file", action="append", default=[],
                           help="Convert specific file(s). Can be passed multiple times.")
    p_convert.add_argument("--force", action="store_true",
                           help="Convert even if unmapped colors are present.")
    p_convert.add_argument("--dry-run", action="store_true",
                           help="Don't write files; just produce the report.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = load_config()
    cfg.ensure_dirs()
    configure_logging("DEBUG" if args.verbose else cfg.log_level)

    if args.command == "status":
        return cmd_status(args, cfg)
    if args.command == "inspect":
        return cmd_inspect(args, cfg)
    if args.command == "convert":
        return cmd_convert(args, cfg)
    parser.error(f"Unknown command: {args.command}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
