"""Talk to the render daemon, and wait rather than fail.

`RenderService.render()` promises a picture or a defect of the file and never
gives up. Putting a socket in front of it must not weaken that, so the client
treats an unreachable daemon exactly as the service treats a dead browser:
something to wait out, not something to report. A restart is therefore a slow
render, never a failed one, and no caller has to learn what a socket error
means about a widget.

    async with RenderClient() as renderer:
        result = await renderer.render(jsx, png, width=830, height=752)
        result = await renderer.render_source(code, png)   # no file needed

Nothing but the source crosses the socket, and the screenshot comes back with
the reply — so the daemon needs no access to the caller's files, and the caller
writes the PNG wherever it likes. That is what keeps working when the container
mounts none of the caller's directories, or is not even on this machine.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from .render_result import RenderResult
from . import render_ipc as ipc


RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0)
# Loud enough to notice a daemon that is not coming back, quiet enough that an
# ordinary restart passes without comment.
RECONNECT_ALARM_AFTER = 6


class RenderTransportError(RuntimeError):
    """The socket could not carry the answer, which is neither of the two
    render outcomes and must not be mistaken for one."""


def make_renderer(n_workers: int = 4, runtime_dir: Optional[Path] = None):
    """The production renderer: the shared daemon if one is up, else in-process.

    Every rendering call site goes through this. With the render daemon
    running (docker/run.sh, or w2c_render/supervisor.py) it returns a
    `RenderClient` — one browser pool shared by every task on the machine, and
    `n_workers` is ignored. Without one it falls back to an in-process
    `RenderService`, so development and tests don't require the container.
    Both sides of the fork honor the same contract and the same signatures.
    """
    if ipc.socket_path(runtime_dir).exists():
        print(f"renderer: using shared daemon at {ipc.socket_path(runtime_dir)}", flush=True)
        return RenderClient(runtime_dir)
    from .render import RenderService  # deferred: pulls in playwright
    print("renderer: no daemon socket found; starting an in-process renderer", flush=True)
    return RenderService(n_workers=n_workers)


class RenderClient:
    """A drop-in stand-in for ``RenderService`` that lives in another process."""

    def __init__(self, runtime_dir: Optional[Path] = None):
        self.socket_path = ipc.socket_path(runtime_dir)

    async def __aenter__(self) -> "RenderClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def render(
        self,
        jsx_path,
        output_path=None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        wait_extra_ms: int = 200,
        force_resize: bool = True,
    ) -> RenderResult:
        """Same two outcomes as the in-process service, across a socket.

        The file is read here and its screenshot written here; the daemon only
        ever sees the source.
        """
        jsx = Path(jsx_path)
        return await self.render_source(
            jsx.read_text(),
            Path(output_path) if output_path else jsx.with_suffix(".png"),
            name=jsx.name, jsx_path=jsx,
            width=width, height=height,
            wait_extra_ms=wait_extra_ms, force_resize=force_resize,
        )

    async def render_source(
        self,
        source: str,
        output_path,
        *,
        name: str = "widget.jsx",
        jsx_path=None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        wait_extra_ms: int = 200,
        force_resize: bool = True,
    ) -> RenderResult:
        """Render code the daemon cannot read from disk, and keep the PNG here.

        For callers that hold the widget in memory — a rollout worker scoring a
        sample it just generated — and for every caller whose files the daemon
        has no access to. `jsx_path` only labels the result.
        """
        png = Path(output_path)
        message = await self._exchange(ipc.build_request(
            source, name=name,
            width=width, height=height,
            wait_extra_ms=wait_extra_ms, force_resize=force_resize,
        ))
        result = ipc.wire_to_result(message)
        payload = ipc.wire_png_bytes(message)
        if payload is not None:
            png.parent.mkdir(parents=True, exist_ok=True)
            png.write_bytes(payload)
        # The daemon rendered under its own temporary directory; the caller
        # only ever knows the names it chose itself.
        result.png_path = png
        result.jsx_path = Path(jsx_path) if jsx_path else png.with_suffix(".jsx")
        return result

    async def _exchange(self, request: dict) -> dict:
        """Send one request, waiting out a daemon that is restarting.

        A connection that cannot be made or that drops mid-render is the daemon
        restarting; the request is simply sent again. Rendering is idempotent,
        so a retry after an unknown outcome is safe.
        """
        attempt = 0
        started = time.monotonic()
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(self.socket_path), limit=ipc.STREAM_LIMIT
                )
            except (OSError, asyncio.TimeoutError) as exc:
                attempt += 1
                await self._wait_for_daemon(attempt, f"cannot connect: {exc}", started)
                continue
            try:
                writer.write(ipc.encode(request))
                await writer.drain()
                try:
                    line = await reader.readline()
                except ValueError as exc:
                    # asyncio's way of saying the reply exceeded the stream
                    # limit. Retrying renders the same oversized screenshot
                    # forever, and the message it raises names neither the
                    # renderer nor the fix.
                    raise RenderTransportError(
                        f"the reply exceeded render_ipc.STREAM_LIMIT "
                        f"({ipc.STREAM_LIMIT // (1024 * 1024)} MB): {exc}. Raise it on both "
                        f"ends."
                    ) from exc
                if not line:
                    raise ConnectionResetError("daemon closed the connection")
                return ipc.decode(line)
            except (OSError, ConnectionError, asyncio.IncompleteReadError) as exc:
                attempt += 1
                await self._wait_for_daemon(attempt, f"lost the daemon: {exc}", started)
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

    async def _wait_for_daemon(self, attempt: int, reason: str, started: float) -> None:
        delay = RECONNECT_BACKOFF_S[min(attempt - 1, len(RECONNECT_BACKOFF_S) - 1)]
        if attempt >= RECONNECT_ALARM_AFTER:
            print(
                f"render-client: STILL WAITING for {self.socket_path} — {reason} "
                f"(attempt {attempt}, {time.monotonic() - started:.0f}s so far)",
                flush=True,
            )
        await asyncio.sleep(delay)
