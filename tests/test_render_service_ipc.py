"""A socket in front of the renderer must not weaken what the renderer promises.

`render()` answers with a picture or a defect of the file and never gives up.
Once the call crosses a process boundary there are two new ways to break that:
the reply could lose what a caller reads from it, and a daemon that is
restarting could be reported to the caller as if the widget were at fault. The
first is a serialisation question, the second is the whole point of the client.

These drive real sockets but no browser, so they state the transport rather
than the renderer. `tests/simulate_render_faults.py --only kill-daemon` does the
same against a real one.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from w2c_render import render_ipc as ipc
from w2c_render.render import RenderResult
from w2c_render.render_client import RenderClient


def test_everything_a_caller_reads_survives_the_wire():
    """render_notes is the only account of what the screenshot cannot show, so a
    transport that dropped it would empty the model's feedback silently."""
    original = RenderResult(
        jsx_path=Path("/w/widget.jsx"),
        png_path=Path("/w/widget.png"),
        render_notes=[{"kind": "overflow", "side": "bottom", "amount": 76,
                       "tag": "div", "w": 268, "h": 226}],
        width=300,
        height=150,
        settled=True,
        settle_ms=265,
        console_errors=["console.error: noisy"],
        source_policy={
            "policy_id": "source_policy_test",
            "schema_version": 1,
            "allowed_imports": ["react-icons/*"],
            "allow_dynamic_imports": False,
        },
    )
    wire = ipc.decode(ipc.encode(ipc.result_to_wire(original)))
    assert wire["ok"] is True and wire["error"] is None
    assert wire["image"] == {"width": 300, "height": 150}
    assert wire["layout"] == original.render_notes
    assert "76px" in wire["feedback_text"] and "300x150" in wire["feedback_text"]
    assert wire["log"]["console"] == original.console_errors

    restored = ipc.wire_to_result(wire, jsx_path=original.jsx_path,
                                  png_path=original.png_path)
    assert restored.ok and restored.error_kind is None
    assert restored.render_notes == original.render_notes
    assert restored.settled and restored.settle_ms == 265
    assert restored.has_overflow and (restored.width, restored.height) == (300, 150)
    assert restored.console_errors == original.console_errors
    assert restored.source_policy == original.source_policy
    assert restored.png_path == original.png_path


@pytest.mark.parametrize("kind", ["runtime", "empty", "hang"])
def test_a_defect_crosses_with_its_kind_and_message(kind):
    original = RenderResult(
        jsx_path=Path("/w/widget.jsx"),
        png_path=Path("/w/widget.png"),
        error=f"{kind} detail",
        error_kind=kind,
    )
    wire = ipc.decode(ipc.encode(ipc.result_to_wire(original)))
    assert wire["ok"] is False and wire["image"] is None and wire["layout"] is None
    assert wire["feedback_text"] == f"RENDER FAILED (no image):\n{kind} detail"

    restored = ipc.wire_to_result(wire, jsx_path=original.jsx_path,
                                  png_path=original.png_path)
    assert restored.is_widget_defect and restored.error_kind == kind
    assert restored.error == f"{kind} detail"


async def _tiny_daemon(path: Path, reply: dict, *, ready: asyncio.Event):
    """A socket that answers one canned result, standing in for the daemon."""
    async def handle(reader, writer):
        line = await reader.readline()
        if line:
            writer.write(ipc.encode(reply))
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(path))
    ready.set()
    async with server:
        await asyncio.sleep(3600)


def test_the_client_waits_for_a_daemon_that_is_not_there_yet(tmp_path):
    """A daemon being restarted must look like a slow render, never a failed
    one — otherwise every caller has to learn what a socket error implies about
    a widget, which is nothing."""
    reply = ipc.result_to_wire(
        RenderResult(jsx_path=Path("/w/a.jsx"), png_path=tmp_path / "a.png")
    )

    (tmp_path / "a.jsx").write_text("export default function Widget(){}")

    async def scenario():
        client = RenderClient(runtime_dir=tmp_path)
        render = asyncio.ensure_future(client.render(tmp_path / "a.jsx", tmp_path / "a.png"))

        # Nothing is listening yet: the call must still be outstanding.
        await asyncio.sleep(0.8)
        assert not render.done(), "the client reported a failure instead of waiting"

        ready = asyncio.Event()
        daemon = asyncio.ensure_future(
            _tiny_daemon(ipc.socket_path(tmp_path), reply, ready=ready)
        )
        await ready.wait()
        result = await asyncio.wait_for(render, timeout=30)
        daemon.cancel()
        return result

    result = asyncio.run(scenario())
    assert result.ok


def test_the_client_retries_a_connection_that_drops_mid_render(tmp_path):
    """The daemon can die between the request and the reply. Rendering writes to
    a path we chose, so sending it again is safe and is what a restart needs."""
    attempts = {"n": 0}
    reply = ipc.result_to_wire(
        RenderResult(jsx_path=Path("/w/a.jsx"), png_path=tmp_path / "a.png")
    )

    async def scenario():
        async def handle(reader, writer):
            await reader.readline()
            attempts["n"] += 1
            if attempts["n"] == 1:
                writer.close()          # hang up without answering
                return
            writer.write(ipc.encode(reply))
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handle, path=str(ipc.socket_path(tmp_path)))
        async with server:
            client = RenderClient(runtime_dir=tmp_path)
            return await asyncio.wait_for(
                client.render(tmp_path / "a.jsx", tmp_path / "a.png"), timeout=30
            )

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a.jsx").write_text("export default function Widget(){}")
    result = asyncio.run(scenario())
    assert result.ok
    assert attempts["n"] == 2, "the dropped attempt must be sent again"


# ---- rendering without a shared filesystem ---------------------------------

def test_the_screenshot_can_travel_instead_of_the_path(tmp_path):
    """The mode that works when the daemon shares no directory with the caller.

    Nothing but the source goes out and nothing but the PNG comes back, so the
    caller names a file only on its own filesystem — which is what makes a
    container that mounts none of its directories, or another machine, work.
    """
    pixels = b"\x89PNG\r\n\x1a\n" + b"rendered elsewhere" * 8
    seen: dict = {}

    async def scenario():
        async def handle(reader, writer):
            seen.update(ipc.decode(await reader.readline()))
            # The daemon rendered under a temporary directory of its own.
            reply = ipc.result_to_wire(
                RenderResult(jsx_path=Path("/daemon/tmp/w.jsx"),
                             png_path=Path("/daemon/tmp/w.png"),
                             render_notes=[{"kind": "overflow", "side": "bottom", "amount": 12}]),
                png_bytes=pixels,
            )
            writer.write(ipc.encode(reply))
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handle, path=str(ipc.socket_path(tmp_path)))
        async with server:
            client = RenderClient(runtime_dir=tmp_path)
            return await asyncio.wait_for(
                client.render_source("export default function Widget(){}",
                                     tmp_path / "out" / "w.png", width=100, height=50),
                timeout=30,
            )

    result = asyncio.run(scenario())
    assert "source" in seen and "jsx" not in seen
    assert result.ok and result.has_overflow
    # The PNG is on the caller's disk, at the caller's path, byte for byte.
    assert (tmp_path / "out" / "w.png").read_bytes() == pixels
    assert result.png_path == tmp_path / "out" / "w.png", "the daemon's temp path must not leak"


def test_a_screenshot_larger_than_a_stream_line_still_arrives(tmp_path):
    """asyncio refuses a line over 64 KB by default, and a full-size widget's
    PNG is bigger than that once base64'd — so the default fails on exactly the
    widgets worth rendering while passing every small one."""
    pixels = b"\x89PNG\r\n\x1a\n" + b"x" * (3 * 1024 * 1024)

    async def scenario():
        async def handle(reader, writer):
            await reader.readline()
            writer.write(ipc.encode(ipc.result_to_wire(
                RenderResult(jsx_path=Path("/d/w.jsx"), png_path=Path("/d/w.png")),
                png_bytes=pixels,
            )))
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(
            handle, path=str(ipc.socket_path(tmp_path)), limit=ipc.STREAM_LIMIT
        )
        async with server:
            client = RenderClient(runtime_dir=tmp_path)
            return await asyncio.wait_for(
                client.render_source("export default function Widget(){}",
                                     tmp_path / "big.png"),
                timeout=60,
            )

    result = asyncio.run(scenario())
    assert result.ok
    assert (tmp_path / "big.png").read_bytes() == pixels


def test_a_request_names_no_file_at_all(tmp_path):
    """Nothing on the wire refers to the caller's disk, which is the whole
    reason a container that mounts none of it still renders."""
    request = ipc.build_request("export default function Widget(){}", name="w.jsx")
    assert "jsx" not in request and "png" not in request
    assert request["source"].startswith("export default")


def test_a_daemon_that_speaks_only_filenames_is_a_deployment_error(tmp_path):
    """An older daemon cannot serve this protocol, and must say so rather than
    render something wrong or blame the widget."""
    from w2c_render.render_daemon import RenderDaemon

    daemon = RenderDaemon(runtime_dir=tmp_path, workers=1, viewport=(800, 600))
    daemon.service = object()
    with pytest.raises(ValueError, match="no source"):
        asyncio.run(daemon._render({"v": 1, "jsx": "/w/a.jsx", "png": "/w/a.png"}))


# ---- what the supervisor decides -------------------------------------------

from w2c_render.supervisor import diagnose  # noqa: E402


def test_an_idle_daemon_is_not_a_wedged_one():
    """Both complete nothing. Only one has work outstanding, and that is the
    whole discriminator — without it a supervisor kills a healthy daemon every
    time collection pauses."""
    now = time.time()
    idle = {"now": now, "in_flight": 0, "last_completed_at": now - 100_000}
    assert diagnose(idle, now=now, stall_s=600, silence_s=60) is None


def test_work_outstanding_and_nothing_completing_is_wedged():
    now = time.time()
    stuck = {"now": now, "in_flight": 3, "last_completed_at": now - 700}
    reason = diagnose(stuck, now=now, stall_s=600, silence_s=60)
    assert reason and "outstanding" in reason


def test_a_daemon_that_stopped_ticking_is_wedged_whatever_it_claims():
    """A blocked event loop stops writing heartbeats too, and the last one it
    wrote may well say it was idle."""
    now = time.time()
    silent = {"now": now - 300, "in_flight": 0, "last_completed_at": now - 300}
    reason = diagnose(silent, now=now, stall_s=600, silence_s=60)
    assert reason and "not even ticking" in reason


def test_a_daemon_that_has_not_started_is_left_alone():
    assert diagnose(None, now=time.time(), stall_s=600, silence_s=60) is None


def test_the_heartbeat_carries_what_the_decision_needs(tmp_path):
    """Serialised by the daemon, read by the supervisor: the two have to agree
    on the field names or the supervisor silently never fires."""
    from w2c_render.render_daemon import RenderDaemon

    daemon = RenderDaemon(runtime_dir=tmp_path, workers=1, viewport=(800, 600))
    daemon._in_flight = 2
    daemon._write_heartbeat()
    beat = json.loads(ipc.heartbeat_path(tmp_path).read_text())
    for field in ("pid", "now", "last_completed_at", "in_flight", "completed"):
        assert field in beat, field
    assert beat["in_flight"] == 2
    assert beat["source_policy"]["allowed_imports"] == []
    assert diagnose(beat, now=time.time(), stall_s=600, silence_s=60) is None


# ---- the shape is the contract ---------------------------------------------

def _wire(result: RenderResult, png: bytes | None = None) -> dict:
    return ipc.decode(ipc.encode(ipc.result_to_wire(result, png_bytes=png)))


def test_a_reply_carries_a_picture_or_a_reason_and_never_both():
    """`ok` decides the whole shape, so no caller has to infer it.

    Every caller of the flat reply invented its own rule — `error is None`
    here, a `has_overflow` boolean there, `render_notes` dropped entirely in a
    third — and the same render was described three different ways. There is
    one rule now and it is checkable.
    """
    rendered = _wire(RenderResult(Path("w.jsx"), Path("w.png"), width=10, height=10),
                     png=b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    failed = _wire(RenderResult(Path("w.jsx"), Path("w.png"),
                                error="boom", error_kind="runtime"))

    assert rendered["ok"] is True
    assert rendered["image"] is not None and rendered["layout"] is not None
    assert rendered["error"] is None
    assert "png_b64" in rendered["image"]

    assert failed["ok"] is False
    assert failed["image"] is None and failed["layout"] is None
    assert failed["error"] == {"kind": "runtime", "text": "boom"}


def test_a_layout_note_the_service_cannot_phrase_stays_out_of_the_feedback():
    """An unknown note is a bug report, not a sentence for a model to read."""
    result = RenderResult(
        Path("w.jsx"), Path("w.png"), width=10, height=10,
        render_notes=[{"kind": "kind_invented_next_year", "detail": "?"},
                      {"kind": "zero_size", "tag": "svg", "w": 0, "h": 0}],
    )
    wire = _wire(result)
    assert wire["feedback_text"] == (
        "Rendered 10x10. These problems may not be visible in the image:\n"
        "- <svg> has no area (0x0)\n"
        "If you judge any of these not to be a problem, ignore it."
    )
    assert wire["layout"] == result.render_notes, "the note itself still travels"


def test_the_renderers_own_noise_is_carried_but_not_counted_as_a_finding():
    """`log.console` keeps everything; `log.unclassified` is what has no home.

    Vite's compile complaint and the 500 that follows it are this process
    describing itself — 200 of the 4,210 renders in one collection. What is
    left over is the queue of things this service has no rule for yet, and it
    is meant to be read by us, never by a model.
    """
    result = RenderResult(
        Path("w.jsx"), Path("w.png"), width=10, height=10,
        console_errors=[
            "console.error: [vite] Internal Server Error\nwidget.jsx: Unexpected token",
            "console.error: Failed to load resource: the server responded with a status of 500",
            'console.error: Error: <path> attribute d: Expected number, "…NaN 248…"',
        ],
    )
    wire = _wire(result)
    assert len(wire["log"]["console"]) == 3
    assert wire["log"]["unclassified"] == [
        'console.error: Error: <path> attribute d: Expected number, "…NaN 248…"'
    ]
    assert "NaN" not in wire["feedback_text"], "log never reaches the model"
