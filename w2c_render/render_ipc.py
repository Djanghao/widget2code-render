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


PROTOCOL_VERSION = 4

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


def source_policy_path(runtime_dir: Path | None = None) -> Path:
    return (runtime_dir or DEFAULT_RUNTIME_DIR) / "source_policy.json"


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
    freeze_animations: bool = True,
    mode: str | None = None,
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
        "freeze_animations": freeze_animations,
        # Absent means the daemon's own mode. Naming one per request is what lets a
        # single pool serve both contracts, and every reply carries the one it used.
        "mode": mode,
    }


def result_to_wire(result: RenderResult, *, png_bytes: bytes | None = None) -> dict[str, Any]:
    """One render, in the groups a caller actually reads.

    `ok` is the whole shape: a reply carries an `image` and a `layout`, or an
    `error`, never both. Eleven flat fields let each caller invent its own
    rule for that — one derived success from `error is None`, another kept
    only the `has_overflow` boolean and dropped the notes it came from — and
    the same render was then described three different ways downstream.

    `feedback_text` is the only thing a model is shown, and it is derived:
    every fact behind it is in `error` and `layout`. The service writes the
    sentence once so that two experiments' feedback can be compared.

    `log` is what is true but not actionable — how long the page took to
    settle, what the console said, what this service could not account for.
    It travels so it can be persisted and mined; it is never shown to a model.

    Nothing here names the daemon's temporary directory: the result is
    scrubbed before it is encoded, and `jsx`/`png` are gone — the client
    overwrote them with its caller's own paths anyway.
    """
    message: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "ok": result.ok,
        "image": None if not result.ok else {
            "width": result.width,
            "height": result.height,
        },
        "error": None if result.ok else {
            "kind": result.error_kind,
            "text": result.error,
        },
        "layout": None if not result.ok else list(result.render_notes),
        "feedback_text": result.feedback_text,
        "log": {
            "settled": result.settled,
            "settle_ms": result.settle_ms,
            "console": list(result.console_errors),
            "unclassified": result.unclassified,
            "source_policy": result.source_policy,
        },
    }
    if png_bytes is not None and message["image"] is not None:
        message["image"]["png_b64"] = base64.b64encode(png_bytes).decode("ascii")
    return message


def wire_png_bytes(message: Mapping[str, Any]) -> bytes | None:
    """The screenshot itself, when the reply carried one."""
    payload = (message.get("image") or {}).get("png_b64")
    return base64.b64decode(payload) if payload else None


def wire_to_result(
    message: Mapping[str, Any], *, jsx_path: Path, png_path: Path
) -> RenderResult:
    """Rebuild the result. The paths are the caller's — the wire carries none."""
    image = message.get("image") or {}
    error = message.get("error") or {}
    log = message.get("log") or {}
    return RenderResult(
        jsx_path=jsx_path,
        png_path=png_path,
        error=error.get("text"),
        error_kind=error.get("kind"),
        console_errors=list(log.get("console") or []),
        render_notes=list(message.get("layout") or []),
        settled=bool(log.get("settled")),
        settle_ms=int(log.get("settle_ms") or 0),
        width=image.get("width"),
        height=image.get("height"),
        source_policy=log.get("source_policy"),
    )
