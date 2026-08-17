"""The wire between a render client and the render daemon.

The contract that `RenderService.render()` holds in one process has to survive
the socket unchanged: a caller gets a picture or a defect of the file it sent,
and never anything about the renderer's own health. Infrastructure failures do
not cross this wire — they are the daemon's problem, and a client that cannot
reach the daemon waits rather than being told about it.

The source crosses, and the screenshot comes back. Nothing about either side's
filesystem is assumed — no shared directory, no agreed-upon absolute path, and
nothing stopping the socket from being replaced by a network one later. The
reply carries the same metadata a local ``RenderResult`` would.

An earlier version passed filenames instead, which was cheaper and worked
exactly as long as both ends saw the same disk. Mount the container elsewhere
and the daemon wrote a real PNG into its own /tmp, reported success, and the
caller found nothing — a silent failure discovered much later as a missing
file. Pixels on the wire cost under a millisecond against a 400 ms render, so
the cheaper mode bought nothing worth that.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .render_result import RenderResult


PROTOCOL_VERSION = 3

# One JSON line now carries a whole screenshot, and asyncio's stream reader
# refuses a line longer than 64 KB by default — which a 1920x1080 widget
# exceeds while a small one does not, i.e. it fails on the interesting half of
# the corpus only. Both ends must raise it or the reply is unreadable.
#
# Sized against the corpus rather than guessed: over the 67,353 PNGs in this
# repo the p99 render is 435 KB and the single largest file is 7.27 MB, which
# base64 to ~9.7 MB. 64 MB leaves ~6x headroom over anything ever produced
# here, and the reader only ever buffers replies for renders actually in
# flight (bounded by the daemon's pool). A reply that somehow exceeds it is
# reported as a transport error naming this constant, not as a widget defect.
STREAM_LIMIT = 64 * 1024 * 1024

DEFAULT_RUNTIME_DIR = Path(
    os.environ.get("W2C_RENDER_RUNTIME_DIR", "/tmp/w2c-render")
)


def socket_path(runtime_dir: Path | None = None) -> Path:
    return (runtime_dir or DEFAULT_RUNTIME_DIR) / "render.sock"


def heartbeat_path(runtime_dir: Path | None = None) -> Path:
    return (runtime_dir or DEFAULT_RUNTIME_DIR) / "heartbeat.json"


def encode(message: Mapping[str, Any]) -> bytes:
    """One JSON object per line: framing a supervisor can also read by eye."""
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    return json.loads(line.decode("utf-8"))


def build_request(
    source: str,
    *,
    name: str = "widget",
    width: int | None = None,
    height: int | None = None,
    wait_extra_ms: int = 200,
    force_resize: bool = True,
) -> dict[str, Any]:
    """A render of code the daemon has never seen and a file it cannot read.

    `name` only labels the temporary file the daemon writes, and through it any
    error message the widget produces; it does not have to exist anywhere.
    """
    return {
        "v": PROTOCOL_VERSION,
        "source": source,
        "name": name,
        "width": width,
        "height": height,
        "wait_extra_ms": wait_extra_ms,
        "force_resize": force_resize,
    }


def result_to_wire(result: RenderResult, *, png_bytes: bytes | None = None) -> dict[str, Any]:
    """Exactly the fields a caller reads, and nothing about the renderer.

    `has_overflow` / `overflow_warning` are derived from `render_notes` on
    both sides; they stay on the wire for humans reading it by eye.

    `png_bytes` travels base64-encoded: the caller has no access to the
    temporary file the daemon rendered into, so the screenshot itself is the
    only thing that can carry a successful render back.
    """
    message: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        # The daemon's own temporary names; the client replaces them with the
        # ones its caller chose. Kept on the wire for a human reading it.
        "jsx": str(result.jsx_path),
        "png": str(result.png_path),
        "error": result.error,
        "error_kind": result.error_kind,
        "console_errors": list(result.console_errors),
        "render_notes": list(result.render_notes),
        "settled": result.settled,
        "settle_ms": result.settle_ms,
        "has_overflow": result.has_overflow,
        "overflow_warning": result.overflow_warning,
    }
    if png_bytes is not None:
        message["png_b64"] = base64.b64encode(png_bytes).decode("ascii")
    return message


def wire_png_bytes(message: Mapping[str, Any]) -> bytes | None:
    """The screenshot itself, when the reply carried one."""
    payload = message.get("png_b64")
    return base64.b64decode(payload) if payload else None


def wire_to_result(message: Mapping[str, Any]) -> RenderResult:
    return RenderResult(
        jsx_path=Path(message["jsx"]),
        png_path=Path(message["png"]),
        error=message.get("error"),
        error_kind=message.get("error_kind"),
        console_errors=list(message.get("console_errors") or []),
        render_notes=list(message.get("render_notes") or []),
        settled=bool(message.get("settled")),
        settle_ms=int(message.get("settle_ms") or 0),
    )
