"""The render contract's result type — stdlib only, importable anywhere.

Kept free of playwright (and any other dependency) on purpose: together with
`render_ipc.py` and `render_client.py` this is the complete client side of the
render daemon. Another repo can copy these three files (or import them
directly) and talk to a running daemon without installing anything.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Superseded by `RenderResult.feedback_text`, which says the same thing with
# the measured numbers in it. Kept until the collection scripts that import it
# are switched over; it has no reader inside this repo.
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
#
# Every kind the render path can produce must appear either here or in the
# infrastructure set. A kind in neither is treated as infrastructure and
# repaired forever: `syntax` was missing from this tuple for one build, and
# the daemon rebuilt its browser pool 18 times over a widget with an
# unterminated string.
WIDGET_DEFECT_ERROR_KINDS = ("runtime", "empty", "hang", "syntax", "policy")

# What the renderer answers when it has run out of explanations: not the
# widget's fault, not silence. Callers must surface it rather than feed it to a
# model, and its appearance in a collection is a bug report about this service.
RENDERER_FAILURE_ERROR_KINDS = ("unknown",)


@dataclass
class RenderResult:
    """Outcome of one render call: a picture, or a defect of the file.

    `ok` is True iff a screenshot PNG was written to `png_path`. `error` is
    populated only for hard failures where no PNG was produced; console-only
    errors stay in `console_errors` as diagnostics on both paths.

    `error_kind` separates what the widget's author can fix from what only the
    operator can: `runtime` / `empty` / `hang` / `syntax` / `policy` are deterministic widget defects
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
    width: Optional[int] = None
    height: Optional[int] = None
    source_policy: Optional[dict[str, Any]] = None

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

    @property
    def unclassified(self) -> list[str]:
        """Console output this service could not account for.

        Everything it recognises is already in `error` or `render_notes`; what
        is left is either a defect there is no rule for yet or a message that
        should be dropped. It is never shown to a model — its job is to be
        read by us, the same way `unknown` is: a bug report about this service.
        """
        return [
            entry for entry in self.console_errors
            if not any(noise in entry.lower() for noise in _RENDERER_NOISE)
        ]

    @property
    def feedback_text(self) -> str:
        """The one wording a model is shown. Derived; nothing is only here.

        Three repos each wrote their own sentence for the same result, so an
        experiment's feedback was not comparable with the next one's. The
        service holds every fact, so it writes the sentence.
        """
        if not self.ok:
            return f"RENDER FAILED (no image):\n{self.error}"

        head = (f"Rendered {self.width}x{self.height}."
                if self.width and self.height else "Rendered.")
        lines = [line for line in map(_note_line, self.render_notes) if line]
        if not lines:
            return head
        # The audit measures; it does not judge. Intentional overhang and
        # deliberate empty space both exist, and making a model "fix" a layout
        # that was already right is worse than missing one that was not.
        tail = "\nIf you judge any of these not to be a problem, ignore it."
        if self.has_overflow:
            tail = ("\nShrink the content to fit: reduce font sizes, paddings, gaps, "
                    "line-heights, icon/image sizes.") + tail
        return (head + " These problems may not be visible in the image:\n"
                + "\n".join(lines) + tail)


# Vite complaining about a file it could not compile, and the browser reporting
# the 500 that followed: the renderer's plumbing describing itself.
_RENDERER_NOISE = ("[vite] internal server error", "failed to load resource:")


def _overflow_line(note: dict) -> str:
    excerpt = " " + json.dumps(note["text"], ensure_ascii=False) if note.get("text") else ""
    return (f"- content overflows the {note['side']} edge by {note['amount']}px "
            f"(<{note['tag']}> {note['w']}x{note['h']}{excerpt})")


def _unloaded_line(note: dict) -> str:
    return f"- <img src={json.dumps(note['src'])}> failed to load"


def _zero_size_line(note: dict) -> str:
    return f"- <{note['tag']}> has no area ({note['w']}x{note['h']})"


def _unpainted_line(note: dict) -> str:
    return f"- <{note['tag']} {note['attr']}={json.dumps(note['value'])}> painted no pixels"


# What each kind must carry before it can be put into a sentence.
_NOTE_TEMPLATES = {
    "overflow": (("side", "amount", "tag", "w", "h"), _overflow_line),
    "unloaded": (("src",), _unloaded_line),
    "zero_size": (("tag", "w", "h"), _zero_size_line),
    "unpainted": (("tag", "attr", "value"), _unpainted_line),
}


def _note_line(note: dict) -> Optional[str]:
    """One render note as the sentence a model reads, or None.

    A kind with no template, or a note missing what its template needs, is
    dropped rather than printed raw or allowed to raise. The audit is a
    program running in a browser and this is a result object: a note it cannot
    phrase must not be able to take the whole render down with it, and a
    half-formed sentence in the feedback is worse than a note only `log` sees.
    """
    required, render = _NOTE_TEMPLATES.get(note.get("kind"), (None, None))
    if render is None or any(note.get(key) is None for key in required):
        return None
    return render(note)
