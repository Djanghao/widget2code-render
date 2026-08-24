"""A long-lived render service other processes call over a socket.

`RenderService` repairs everything that can go wrong *inside* it — a dead
browser, a lost Vite, a poisoned page — and never gives up. What it cannot
repair is the process holding it: a blocked event loop, exhausted memory, a
deadlock nobody found yet. The thing that would fix those lives inside the
thing that is broken.

So the renderer moves into a process of its own, and something outside it
watches. The daemon publishes a heartbeat — when it last finished a render, and
how many are in flight — and a supervisor that sees work outstanding and
nothing completing kills it. Restarting also reaps every leaked Chromium and
driver at once, which is cleanup the in-process path can only attempt.

Callers do not see any of that. `RenderClient` waits through a restart, so a
render that spans one is slow rather than failed.

    python -m w2c_render.render_daemon --workers 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from w2c_render.render import RenderService  # noqa: E402
from w2c_render import render_ipc as ipc  # noqa: E402
from w2c_render.source_policy import (  # noqa: E402
    SourcePolicy,
    source_policy_from_values,
    write_source_policy,
)


HEARTBEAT_INTERVAL_S = 5.0


class RenderDaemon:
    """One RenderService, many callers, and a pulse a supervisor can read."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        workers: int,
        viewport: tuple[int, int],
        source_policy: SourcePolicy | None = None,
    ):
        self.runtime_dir = runtime_dir
        self.workers = workers
        self.viewport = viewport
        self.source_policy = source_policy or SourcePolicy()
        self.service: RenderService | None = None
        self._in_flight = 0
        self._completed = 0
        self._started_at = time.time()
        # Seeded with startup: a daemon that has never finished anything is
        # judged from when it began, not from the epoch.
        self._last_completed_at = time.time()
        self._stopping = asyncio.Event()

    # ---- heartbeat ------------------------------------------------------

    def _write_heartbeat(self) -> None:
        """What a supervisor needs to tell "busy" from "wedged".

        `in_flight` alone cannot: an idle daemon and a stuck one both complete
        nothing. Work outstanding *and* nothing completing is the signal.
        """
        payload = {
            "pid": os.getpid(),
            "started_at": self._started_at,
            "now": time.time(),
            "last_completed_at": self._last_completed_at,
            "in_flight": self._in_flight,
            "completed": self._completed,
            "generation": self.service._generation if self.service else -1,
            "source_policy": self.source_policy.descriptor(),
        }
        path = ipc.heartbeat_path(self.runtime_dir)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)

    async def _heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self._write_heartbeat()
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=HEARTBEAT_INTERVAL_S
                )
            except asyncio.TimeoutError:
                pass

    # ---- serving --------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    request = ipc.decode(line)
                except Exception:
                    return
                self._in_flight += 1
                try:
                    result, png_bytes = await self._render(request)
                finally:
                    self._in_flight -= 1
                    self._completed += 1
                    self._last_completed_at = time.time()
                # A caller that hung up gets no answer and needs none: its
                # retry simply renders again, which is why rendering may not
                # depend on anything the first attempt left behind.
                try:
                    writer.write(ipc.encode(ipc.result_to_wire(result, png_bytes=png_bytes)))
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let one caller take the daemon down
            print(f"render-daemon: connection failed: {type(exc).__name__}: {exc}", flush=True)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _render(self, request: dict) -> tuple[object, bytes | None]:
        """Serve one request without assuming anything about the caller's disk.

        The source arrives in the request, is rendered under a temporary
        directory of ours, and the screenshot goes back in the reply. That is
        what makes a caller whose directories are not mounted here — or not
        even on this machine — work at all.
        """
        assert self.service is not None
        source = request.get("source")
        if source is None:
            # A client older than this protocol, which named files instead.
            # Nothing sane can be rendered from that, and answering with a
            # widget defect would blame the widget for a deployment mistake.
            raise ValueError(
                f"request carries no source (protocol v{request.get('v')}); "
                f"this daemon speaks v{ipc.PROTOCOL_VERSION}"
            )
        with tempfile.TemporaryDirectory(prefix="w2c-render-") as tmp:
            stem = Path(str(request.get("name") or "widget")).stem or "widget"
            jsx = Path(tmp) / f"{stem}.jsx"
            jsx.write_text(source)
            png = jsx.with_suffix(".png")
            result = await self.service.render(
                jsx, png,
                width=request.get("width"),
                height=request.get("height"),
                wait_extra_ms=request.get("wait_extra_ms", 200),
                force_resize=request.get("force_resize", True),
                freeze_animations=request.get("freeze_animations", True),
            )
            payload = png.read_bytes() if result.ok else None
        return result, payload

    async def run(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        write_source_policy(ipc.source_policy_path(self.runtime_dir), self.source_policy)
        sock = ipc.socket_path(self.runtime_dir)
        if sock.exists():
            sock.unlink()

        async with RenderService(
            n_workers=self.workers,
            default_viewport=self.viewport,
            source_policy=self.source_policy,
        ) as service:
            self.service = service
            # A rollout group arrives as one burst of connects, and asyncio's
            # default backlog of 100 drops the rest of them: 1,822 callers
            # produced a storm of "lost the daemon" reconnects against a daemon
            # that was alive and idle the whole time. Queue them instead — the
            # pool decides how many render at once, and the socket should not
            # also be deciding who gets to ask.
            server = await asyncio.start_unix_server(
                self._handle, path=str(sock), limit=ipc.STREAM_LIMIT, backlog=4096
            )
            heartbeat = asyncio.ensure_future(self._heartbeat_loop())
            print(
                f"render-daemon: listening on {sock} "
                f"(pid {os.getpid()}, {self.workers} workers, "
                f"{self.source_policy.policy_id})",
                flush=True,
            )
            try:
                await self._stopping.wait()
            finally:
                heartbeat.cancel()
                server.close()
                try:
                    await server.wait_closed()
                except Exception:
                    pass
                if sock.exists():
                    sock.unlink()
        print("render-daemon: stopped", flush=True)

    def stop(self) -> None:
        self._stopping.set()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=ipc.DEFAULT_RUNTIME_DIR)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--viewport", type=int, nargs=2, default=(1920, 1080))
    parser.add_argument(
        "--allow-import",
        action="append",
        default=[],
        help="Allowed bare package or package/* pattern; repeat or comma-separate.",
    )
    parser.add_argument("--allow-dynamic-imports", action="store_true")
    args = parser.parse_args()

    policy = source_policy_from_values(
        args.allow_import,
        allow_dynamic_imports=args.allow_dynamic_imports,
    )

    daemon = RenderDaemon(
        runtime_dir=args.runtime_dir,
        workers=args.workers,
        viewport=tuple(args.viewport),
        source_policy=policy,
    )

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, daemon.stop)
        await daemon.run()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
