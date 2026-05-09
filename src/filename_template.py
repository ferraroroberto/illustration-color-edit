"""Filename templating for CMYK output PDFs.

The pipeline historically writes ``<stem>_CMYK.pdf`` next to each source
SVG, where ``stem`` is the source filename without extension. Publishers
typically ask for tidier names like ``fig_04_03_CMYK.pdf`` keyed off the
chapter/figure prefix the illustrator already uses in their source files
(``04.03 - venn diagram two.svg`` → chapter 04, figure 03).

This module gives the pipeline:

* :func:`parse_chapter_figure` — pulls a leading ``<chapter>.<figure>``
  pair out of a source filename. Tolerates ``04.03``, ``1.2``, ``4-3``,
  and ``4_3`` so the user is not locked into one numbering convention.
* :func:`apply_template` — interpolates a template like
  ``"fig_{chapter:02d}_{figure:02d}_CMYK"`` against a source stem and
  the parsed chapter/figure. Returns a stem (no extension) the caller
  can suffix with ``.pdf``.

Both functions are pure data — no I/O, no side effects — so they're
trivially unit-testable and safe to call from anywhere in the pipeline.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Match a leading ``<chapter><sep><figure>`` where:
#   - chapter and figure are digit runs (any length)
#   - separator is ``.``, ``-``, or ``_``
#   - the pair is followed by a word boundary (whitespace, dash,
#     underscore, end-of-string) so we don't bite off ``1.23.4`` or
#     ``12-34-bar`` ambiguously — ``\d+[.\-_]\d+`` only matches the
#     first two groups.
#
# Examples that match:
#   "04.03 - venn"   → ("04", "03")
#   "1.2 description" → ("1", "2")
#   "4-3 something"  → ("4", "3")
#   "4_3 something"  → ("4", "3")
#
# Examples that don't match:
#   "books pile - learn"  (no leading digits)
#   "v2 final"            (no separator)
#   "1.2.3 something"     (matches first two groups, leaves ".3" in the rest)
_PREFIX_RE = re.compile(r"^(\d+)[.\-_](\d+)(?=\b|[\s\-_])")


def parse_chapter_figure(stem: str) -> Optional[tuple[str, str]]:
    """Return ``(chapter, figure)`` as digit strings, or ``None`` if not present.

    The values are returned **as written** — no zero-padding — so the
    caller can decide via the template format whether to pad
    (``{chapter:02d}``) or keep the user's natural form (``{chapter}``).
    """
    m = _PREFIX_RE.match(stem)
    if not m:
        return None
    return m.group(1), m.group(2)


def _slugify(text: str) -> str:
    """Lowercase ASCII slug with ``-`` separators. Reasonable for filenames.

    Strips diacritics, drops anything that isn't alphanumeric/space/dash,
    collapses runs of separators, and trims leading/trailing dashes.
    """
    norm = unicodedata.normalize("NFKD", text)
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    norm = norm.lower()
    norm = re.sub(r"[^a-z0-9\s\-_]", "", norm)
    norm = re.sub(r"[\s_]+", "-", norm)
    norm = re.sub(r"-+", "-", norm)
    return norm.strip("-")


def _strip_prefix(stem: str) -> str:
    """Return ``stem`` with any leading chapter.figure prefix removed.

    Mirrors :func:`parse_chapter_figure` — when no prefix is present the
    full stem is returned unchanged. Leading separator characters after
    the prefix (``" - "``, ``"_"``, etc.) are also stripped so the
    description starts on its first real word.
    """
    m = _PREFIX_RE.match(stem)
    if not m:
        return stem
    return stem[m.end() :].lstrip(" -_")


# Template tokens we explicitly support. Anything else in a ``{...}``
# expression will raise ``KeyError`` from ``str.format`` — that's the
# user's signal that they typo'd a placeholder.
_TEMPLATE_TOKENS = ("stem", "chapter", "figure", "description", "slug")


class TemplateError(ValueError):
    """Raised when a template references a variable that wasn't resolvable."""


def apply_template(template: str, stem: str) -> str:
    """Apply ``template`` to ``stem`` and return the resulting filename stem.

    Empty / blank ``template`` returns ``stem`` unchanged so the pipeline
    keeps its historical default. ``stem`` is the source filename without
    extension (``"04.03 - venn diagram two"``).

    Recognized placeholders:

      * ``{stem}``         — full original stem.
      * ``{chapter}`` /
        ``{chapter:02d}``  — leading chapter number, raw or zero-padded.
      * ``{figure}``  /
        ``{figure:02d}``   — leading figure number, raw or zero-padded.
      * ``{description}``  — stem with the chapter.figure prefix removed.
      * ``{slug}``         — slugified ``{description}`` (lowercase, ``-`` sep).

    A template that references ``{chapter}`` or ``{figure}`` against a
    stem that has no parseable prefix raises :class:`TemplateError`. The
    caller should catch this and fall back to ``stem`` with a warning so
    one oddly-named file doesn't blow up an entire batch.

    Numeric format specs (``:02d`` etc.) require the value to be an
    integer; this function feeds parsed chapter/figure as ``int`` when
    a format spec is present and as ``str`` otherwise, so both
    ``{chapter}`` (preserves leading zeros if the user typed them) and
    ``{chapter:02d}`` (forces two-digit padding) work as expected.
    """
    if not template or not template.strip():
        return stem

    description = _strip_prefix(stem)
    slug = _slugify(description) or _slugify(stem)
    parsed = parse_chapter_figure(stem)

    # Build the values dict. For chapter/figure we serve both string and
    # int representations through a helper class so {chapter} -> "04"
    # (preserves user's padding) and {chapter:02d} -> "04" (forces it).
    class _Numeric:
        def __init__(self, raw: str):
            self._raw = raw

        def __str__(self) -> str:
            return self._raw

        def __format__(self, spec: str) -> str:
            if spec == "":
                return self._raw
            # Any non-empty spec routes through int formatting.
            return format(int(self._raw), spec)

    values: dict[str, object] = {
        "stem": stem,
        "description": description,
        "slug": slug,
    }
    if parsed is not None:
        values["chapter"] = _Numeric(parsed[0])
        values["figure"] = _Numeric(parsed[1])

    try:
        return template.format(**values)
    except KeyError as exc:
        # Distinguish "needed a chapter prefix this stem doesn't have"
        # from "you typo'd a placeholder name we don't support".
        missing = exc.args[0] if exc.args else "?"
        if missing in {"chapter", "figure"} and parsed is None:
            raise TemplateError(
                f"template references {{{missing}}} but {stem!r} has no "
                f"chapter.figure prefix"
            ) from None
        raise TemplateError(
            f"template references unknown placeholder {{{missing}}}; "
            f"supported: {', '.join('{' + t + '}' for t in _TEMPLATE_TOKENS)}"
        ) from None


def supported_tokens() -> tuple[str, ...]:
    """Return the supported placeholder names. Used by the settings UI."""
    return _TEMPLATE_TOKENS
