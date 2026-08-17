"""The render contract's result type — stdlib only, importable anywhere.

Kept free of playwright (and any other dependency) on purpose: together with
`render_ipc.py` and `render_client.py` this is the complete client side of the
render daemon. Another repo can copy these three files (or import them
directly) and talk to a running daemon without installing anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Canonical human/LLM-readable warning derived from an `overflow` render note.
# Surfaced verbatim to the reviser LLM via the tool wrapper in the parent project and persisted
# by the collection scripts, so both share a single wording.
OVERFLOW_WARNING_TEXT = (
    "⚠ Content is too large for the widget's declared size and is "
    "overflowing — at least one element extends past widget bounds and is "
    "being clipped (invisible in the rendered PNG because the screenshot "
    "stops at the widget edge). Shrink the content to fit: make it smaller "
    "and more compact (reduce font sizes, paddings, gaps, line-heights, "
    "icon/image dimensions). The original target image is the authoritative "
    "reference for layout and element sizes."
)

# A widget defect renders the same way on every attempt, so retrying is
# pointless and the model must see it. Everything else is the renderer
# misbehaving and is retried instead of taught. `hang` is a timeout that
# survived a verified-healthy renderer — see RenderService.render().
WIDGET_DEFECT_ERROR_KINDS = ("runtime", "empty", "hang")


@dataclass
class RenderResult:
    """Outcome of one render call: a picture, or a defect of the file.

    `ok` is True iff a screenshot PNG was written to `png_path`. `error` is
    populated only for hard failures where no PNG was produced; console-only
    errors stay in `console_errors` as diagnostics on both paths.

    `error_kind` separates what the widget's author can fix from what only the
    operator can: `runtime` / `empty` / `hang` are deterministic widget defects
    and belong in model feedback; `infra` / `timeout` are properties of the
    rendering process and never leave `RenderService.render()`.

    `render_notes` are the measured facts the screenshot cannot show —
    `overflow`, `unpainted`, `unloaded`, `zero_size` (see renderer/audit.js).
    `settled` records whether the widget went quiet before the audit; False
    means the notes describe a still-moving page and belongs in diagnostics,
    not in model feedback. `has_overflow` / `overflow_warning` are derived
    from the notes.
    """
    jsx_path: Path
    png_path: Path
    error: Optional[str] = None
    error_kind: Optional[str] = None
    console_errors: list[str] = field(default_factory=list)
    render_notes: list[dict] = field(default_factory=list)
    settled: bool = False
    settle_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def is_widget_defect(self) -> bool:
        """True when the failure is reproducible from the JSX alone."""
        return self.error_kind in WIDGET_DEFECT_ERROR_KINDS

    @property
    def has_overflow(self) -> bool:
        """In-flow content extends past #widget-root — invisible in the PNG."""
        return any(note.get("kind") == "overflow" for note in self.render_notes)

    @property
    def overflow_warning(self) -> Optional[str]:
        return OVERFLOW_WARNING_TEXT if self.has_overflow else None
