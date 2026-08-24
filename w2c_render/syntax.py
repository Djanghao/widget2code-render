"""Turn Vite's opaque module-fetch failure into localized JSX syntax feedback."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_SCRIPT = Path(__file__).resolve().parents[1] / "renderer" / "syntax_check.mjs"
_CWD = _SCRIPT.parent
_DATA_URI = re.compile(r"(data:[\w.+-]*/?[\w.+-]*;base64,)([A-Za-z0-9+/=]{32,})")
MAX_EXCERPT_CHARS = 200


@dataclass(frozen=True)
class SyntaxDiagnostic:
    text: str
    line: Optional[int] = None
    column: Optional[int] = None
    length: Optional[int] = None
    line_text: Optional[str] = None


@dataclass(frozen=True)
class SyntaxResult:
    jsx_path: Path
    ok: bool
    errors: tuple[SyntaxDiagnostic, ...] = field(default_factory=tuple)

    @property
    def first(self) -> Optional[SyntaxDiagnostic]:
        return self.errors[0] if self.errors else None


def check_syntax(jsx_path: str | Path, *, timeout: int = 60) -> SyntaxResult:
    """Run one syntax-only esbuild transform; names/import exports resolve later."""

    path = Path(jsx_path)
    if timeout < 1:
        raise ValueError("syntax timeout must be positive")
    process = subprocess.run(
        ["node", str(_SCRIPT), str(path)],
        cwd=_CWD,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    lines = [line for line in process.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(f"syntax_check failed: {process.stderr[:500]}")
    value = json.loads(lines[0])
    if value.get("path") != str(path) or type(value.get("ok")) is not bool:
        raise RuntimeError("syntax_check returned an invalid result")
    errors = tuple(
        SyntaxDiagnostic(
            text=item["text"],
            line=item.get("line"),
            column=item.get("column"),
            length=item.get("length"),
            line_text=item.get("lineText"),
        )
        for item in value.get("errors", [])
    )
    if value["ok"] == bool(errors):
        raise RuntimeError("syntax_check result and diagnostics disagree")
    return SyntaxResult(path, value["ok"], errors)


def format_syntax_error(
    result: SyntaxResult,
    *,
    context: int = 1,
    max_errors: int = 3,
) -> str:
    """Format bounded source excerpts with a caret under each syntax error."""

    if result.ok:
        return ""
    try:
        lines = result.jsx_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    blocks: list[str] = []
    for error in result.errors[:max_errors]:
        if error.line is None:
            blocks.append(f"SyntaxError — {error.text}")
            continue
        head = f"SyntaxError at line {error.line}:{error.column} — {error.text}"
        gutter = len(str(error.line))
        body: list[str] = []
        caret_column: Optional[int] = None
        for number in range(max(1, error.line - context), error.line + 1):
            source_line = (
                lines[number - 1]
                if number - 1 < len(lines)
                else error.line_text or ""
            )
            clipped, moved = _excerpt(
                source_line,
                error.column if number == error.line else None,
            )
            if number == error.line:
                caret_column = moved
            body.append(f"  {number:>{gutter}} | {clipped}")
        if caret_column is not None:
            length = max(1, min(error.length or 1, MAX_EXCERPT_CHARS))
            body.append(f"  {'':>{gutter}} | {' ' * caret_column}{'^' * length}")
        blocks.append(head + "\n" + "\n".join(body))
    extra = len(result.errors) - max_errors
    if extra > 0:
        blocks.append(f"({extra} more diagnostic(s) suppressed.)")
    return "\n\n".join(blocks)


def _excerpt(text: str, column: Optional[int]) -> tuple[str, Optional[int]]:
    """Fold base64 and clip a line while keeping its diagnostic column useful."""

    output: list[str] = []
    cursor = 0
    moved = column
    for match in _DATA_URI.finditer(text):
        replacement = match.group(1) + f"<{len(match.group(2))} chars>"
        output.extend((text[cursor : match.start()], replacement))
        if column is not None:
            folded_end = sum(len(part) for part in output)
            if column >= match.end():
                moved = column - (match.end() - folded_end)
            elif column >= match.start():
                moved = folded_end
        cursor = match.end()
    folded = text if not output else "".join((*output, text[cursor:]))
    if len(folded) > MAX_EXCERPT_CHARS:
        anchor = moved or 0
        start = max(
            0,
            min(anchor - MAX_EXCERPT_CHARS // 2, len(folded) - MAX_EXCERPT_CHARS),
        )
        head = "…" if start else ""
        tail = "…" if start + MAX_EXCERPT_CHARS < len(folded) else ""
        folded = head + folded[start : start + MAX_EXCERPT_CHARS] + tail
        if moved is not None:
            moved -= start - len(head)
    if moved is not None:
        moved = max(0, min(moved, len(folded)))
    return folded, moved
