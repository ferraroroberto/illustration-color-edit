"""Exact DeviceCMYK overrides for PDF content streams.

SVG cannot carry DeviceCMYK paint values, so the CMYK pipeline renders the
SVG to an RGB PDF first, patches selected RGB paint operators to exact
DeviceCMYK, then lets Ghostscript finish the delivery PDF.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .svg_parser import normalize_hex


class DeviceCmykError(RuntimeError):
    """Raised when exact DeviceCMYK PDF patching fails."""


@dataclass(frozen=True)
class DeviceCmyk:
    """CMYK percentages, stored in the printer-facing 0..100 range."""

    c: float
    m: float
    y: float
    k: float

    def __post_init__(self) -> None:
        for name, value in (("c", self.c), ("m", self.m), ("y", self.y), ("k", self.k)):
            if value < 0.0 or value > 100.0:
                raise ValueError(f"{name.upper()} must be between 0 and 100, got {value}")

    def to_dict(self) -> dict[str, float]:
        return {"c": self.c, "m": self.m, "y": self.y, "k": self.k}

    def as_percent_label(self) -> str:
        return f"{_fmt_percent(self.c)}/{_fmt_percent(self.m)}/{_fmt_percent(self.y)}/{_fmt_percent(self.k)}"

    def as_pdf_operands(self) -> list[Decimal]:
        """Return PDF content-stream operands in 0..1 units."""
        return [_pdf_decimal(v / 100.0) for v in (self.c, self.m, self.y, self.k)]


@dataclass
class DeviceCmykPatchReport:
    """Summary of a PDF DeviceCMYK patch pass."""

    requested: int = 0
    operators_rewritten: int = 0
    streams_rewritten: int = 0
    final_operators_rewritten: int = 0
    final_streams_rewritten: int = 0
    by_source: dict[str, int] = field(default_factory=dict)

    @property
    def missing_sources(self) -> list[str]:
        return sorted(src for src, count in self.by_source.items() if count == 0)


def _fmt_percent(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _pdf_decimal(value: float) -> Decimal:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return Decimal(text or "0")


def parse_device_cmyk(value: Any) -> DeviceCmyk:
    """Parse a DeviceCMYK value from JSON/UI-friendly shapes.

    Accepted forms:
    - ``"0/85/85/0"`` or ``"0,85,85,0"``
    - ``[0, 85, 85, 0]``
    - ``{"c": 0, "m": 85, "y": 85, "k": 0}``

    Values are percentages in the 0..100 range.
    """
    if isinstance(value, DeviceCmyk):
        return value
    if isinstance(value, str):
        parts = value.replace(",", "/").replace(" ", "").split("/")
        if len(parts) != 4:
            raise ValueError(f"DeviceCMYK string must have four components: {value!r}")
        return DeviceCmyk(*(float(p) for p in parts))
    if isinstance(value, Mapping):
        return DeviceCmyk(
            float(value.get("c", value.get("C", 0.0))),
            float(value.get("m", value.get("M", 0.0))),
            float(value.get("y", value.get("Y", 0.0))),
            float(value.get("k", value.get("K", 0.0))),
        )
    if isinstance(value, Iterable):
        parts = list(value)
        if len(parts) != 4:
            raise ValueError("DeviceCMYK list must have four components")
        return DeviceCmyk(*(float(p) for p in parts))
    raise ValueError(f"Unsupported DeviceCMYK value: {value!r}")


def normalize_device_cmyk_overrides(raw: Mapping[str, Any] | None) -> dict[str, DeviceCmyk]:
    """Return canonical ``#RRGGBB -> DeviceCmyk`` overrides."""
    out: dict[str, DeviceCmyk] = {}
    for source, value in (raw or {}).items():
        norm = normalize_hex(str(source))
        if norm is None:
            continue
        out[norm] = parse_device_cmyk(value)
    return out


def serialize_device_cmyk_overrides(
    overrides: Mapping[str, DeviceCmyk | Mapping[str, Any] | str | Iterable[float]],
) -> dict[str, dict[str, float]]:
    """Return JSON-safe canonical override payload."""
    return {
        str(src).upper(): parse_device_cmyk(value).to_dict()
        for src, value in overrides.items()
    }


def merge_device_cmyk_overrides(
    global_overrides: Mapping[str, Any],
    per_file_overrides: Mapping[str, Any],
) -> dict[str, DeviceCmyk]:
    """Merge global + per-file exact CMYK overrides; per-file wins."""
    merged = normalize_device_cmyk_overrides(global_overrides)
    merged.update(normalize_device_cmyk_overrides(per_file_overrides))
    return merged


def _rgb_operands_for_hex(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return (
        int(h[0:2], 16) / 255.0,
        int(h[2:4], 16) / 255.0,
        int(h[4:6], 16) / 255.0,
    )


def _operands_match_rgb(operands: Iterable[Any], rgb: tuple[float, float, float]) -> bool:
    vals = [float(v) for v in operands]
    if len(vals) != 3:
        return False
    # Inkscape/pdfwrite may emit rounded decimals (e.g. 0.90588 as 0.9059).
    return all(abs(a - b) <= 0.002 for a, b in zip(vals, rgb))


def _operands_match_cmyk(operands: Iterable[Any], cmyk: DeviceCmyk) -> bool:
    vals = [float(v) for v in operands]
    if len(vals) != 4:
        return False
    target = [float(v) for v in cmyk.as_pdf_operands()]
    # Ghostscript may quantize exact 0..1 values slightly (e.g. 0.85 becomes
    # 0.849609). This tolerance is tight enough to avoid unrelated colors.
    return all(abs(a - b) <= 0.002 for a, b in zip(vals, target))


def _content_streams(page: Any) -> list[Any]:
    try:
        contents = page.Contents
    except AttributeError:
        return []
    if contents is None:
        return []
    if isinstance(contents, list):
        return list(contents)
    # pikepdf Array is not a Python list but is iterable and lacks read_bytes.
    if not hasattr(contents, "read_bytes") and hasattr(contents, "__iter__"):
        return list(contents)
    return [contents]


def patch_pdf_rgb_colors_to_device_cmyk(
    pdf_path: Path,
    overrides: Mapping[str, DeviceCmyk | Mapping[str, Any] | str | Iterable[float]],
) -> DeviceCmykPatchReport:
    """Rewrite matching RGB paint operators in ``pdf_path`` to exact DeviceCMYK.

    The PDF is modified in place. Only DeviceRGB fill/stroke operators
    (``rg``/``RG``) whose operands match the source hex colors are rewritten.
    Other color spaces, images, gradients and patterns are intentionally left
    alone because their structure is not a plain per-color paint operator.
    """
    normalized = normalize_device_cmyk_overrides(overrides)
    report = DeviceCmykPatchReport(
        requested=len(normalized),
        by_source={src: 0 for src in normalized},
    )
    if not normalized:
        return report

    try:
        import pikepdf
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise DeviceCmykError(
            "pikepdf is required for exact DeviceCMYK overrides; install requirements.txt"
        ) from exc

    pdf_path = Path(pdf_path)
    rgb_by_source = {src: _rgb_operands_for_hex(src) for src in normalized}
    tmp_path: Optional[Path] = None
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            for stream in _content_streams(page):
                try:
                    instructions = pikepdf.parse_content_stream(stream)
                except Exception as exc:
                    raise DeviceCmykError(f"Could not parse PDF content stream: {exc}") from exc

                changed = False
                rewritten = []
                for inst in instructions:
                    op = str(inst.operator)
                    if op in ("rg", "RG") and len(inst.operands) == 3:
                        match_src: Optional[str] = None
                        for src, rgb in rgb_by_source.items():
                            if _operands_match_rgb(inst.operands, rgb):
                                match_src = src
                                break
                        if match_src is not None:
                            new_op = "k" if op == "rg" else "K"
                            rewritten.append(
                                pikepdf.ContentStreamInstruction(
                                    normalized[match_src].as_pdf_operands(),
                                    pikepdf.Operator(new_op),
                                )
                            )
                            report.operators_rewritten += 1
                            report.by_source[match_src] += 1
                            changed = True
                            continue
                    rewritten.append(inst)

                if changed:
                    stream.write(pikepdf.unparse_content_stream(rewritten))
                    report.streams_rewritten += 1

        if report.operators_rewritten:
            fd, tmp_name = tempfile.mkstemp(
                dir=pdf_path.parent,
                prefix=f".{pdf_path.stem}.device-cmyk.",
                suffix=".pdf",
            )
            os.close(fd)
            tmp_path = Path(tmp_name)
            pdf.save(tmp_path)
    if tmp_path is not None:
        try:
            os.replace(tmp_path, pdf_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    return report


def patch_pdf_device_cmyk_values_to_exact(
    pdf_path: Path,
    overrides: Mapping[str, DeviceCmyk | Mapping[str, Any] | str | Iterable[float]],
) -> DeviceCmykPatchReport:
    """Snap near-target DeviceCMYK operators in ``pdf_path`` to exact values.

    Ghostscript keeps DeviceCMYK operators as DeviceCMYK, but can quantize
    decimals while rewriting the PDF. This final pass restores the exact quad
    the user requested.
    """
    normalized = normalize_device_cmyk_overrides(overrides)
    report = DeviceCmykPatchReport(
        requested=len(normalized),
        by_source={src: 0 for src in normalized},
    )
    if not normalized:
        return report

    try:
        import pikepdf
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise DeviceCmykError(
            "pikepdf is required for exact DeviceCMYK overrides; install requirements.txt"
        ) from exc

    pdf_path = Path(pdf_path)
    tmp_path: Optional[Path] = None
    with pikepdf.open(pdf_path) as pdf:
        for page in pdf.pages:
            for stream in _content_streams(page):
                try:
                    instructions = pikepdf.parse_content_stream(stream)
                except Exception as exc:
                    raise DeviceCmykError(f"Could not parse PDF content stream: {exc}") from exc

                changed = False
                rewritten = []
                for inst in instructions:
                    op = str(inst.operator)
                    if op in ("k", "K") and len(inst.operands) == 4:
                        match_src: Optional[str] = None
                        for src, cmyk in normalized.items():
                            if _operands_match_cmyk(inst.operands, cmyk):
                                match_src = src
                                break
                        if match_src is not None:
                            rewritten.append(
                                pikepdf.ContentStreamInstruction(
                                    normalized[match_src].as_pdf_operands(),
                                    pikepdf.Operator(op),
                                )
                            )
                            report.final_operators_rewritten += 1
                            report.by_source[match_src] += 1
                            changed = True
                            continue
                    rewritten.append(inst)

                if changed:
                    stream.write(pikepdf.unparse_content_stream(rewritten))
                    report.final_streams_rewritten += 1

        if report.final_operators_rewritten:
            fd, tmp_name = tempfile.mkstemp(
                dir=pdf_path.parent,
                prefix=f".{pdf_path.stem}.device-cmyk-final.",
                suffix=".pdf",
            )
            os.close(fd)
            tmp_path = Path(tmp_name)
            pdf.save(tmp_path)
    if tmp_path is not None:
        try:
            os.replace(tmp_path, pdf_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    return report
