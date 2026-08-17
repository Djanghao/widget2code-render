"""Break the renderer the way a long collection breaks it, and check the contract holds.

`tests/test_render_contract.py` scripts `_render_once` and so proves the policy;
it cannot prove that `_recover_infrastructure` actually repairs a real browser.
This drives a real Vite and a real Chromium and takes them away mid-flight.

Every fault here has happened, or will: a collection runs fifteen to twenty
episodes concurrently for hours on a shared machine. Chromium gets OOM-killed,
Vite exits, the box goes to load 500 and everything times out at once. The only
acceptable outcome for each of them is still one of two — a picture, or a defect
of the widget's own source.

    python tests/simulate_render_faults.py            # every scenario
    python tests/simulate_render_faults.py --only kill-browser
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from w2c_render import render_ipc as ipc  # noqa: E402
from w2c_render.render import RenderService  # noqa: E402
from w2c_render.render_client import RenderClient  # noqa: E402


GOOD = """export default function Widget() {
  return <div style={{width: '320px', height: '200px', background: '#3366cc',
    display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'white'}}>
    <span style={{fontSize: '24px'}}>ok</span></div>;
}
"""

OVERFLOWING = """export default function Widget() {
  return <div style={{width: '200px', height: '100px', background: '#eee',
    boxSizing: 'border-box'}}>
    <div style={{width: '400px', height: '260px', background: '#c33'}}>too big</div>
  </div>;
}
"""

THROWS = """export default function Widget() {
  return <div>{BedIcon()}</div>;
}
"""

EMPTY = "export default function Widget() { return null; }\n"

HANGS = """export default function Widget() {
  const started = Date.now();
  while (Date.now() - started < 600000) { /* the widget itself never yields */ }
  return <div style={{width: '10px', height: '10px'}} />;
}
"""


def write_case(directory: Path, name: str, source: str) -> Path:
    path = directory / f"{name}.jsx"
    path.write_text(source)
    return path


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, scenario: str, passed: bool, detail: str) -> None:
        self.rows.append((scenario, passed, detail))
        print(f"  {'PASS' if passed else 'FAIL'}  {scenario}: {detail}", flush=True)

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.rows)


def contract_holds(result) -> tuple[bool, str]:
    """The whole requirement, in one predicate."""
    if result.ok:
        return True, "rendered"
    if result.is_widget_defect:
        return True, f"defect {result.error_kind}: {(result.error or '')[:60]}"
    return False, f"LEAKED {result.error_kind}: {(result.error or '')[:80]}"


async def _kill_browser_soon(service: RenderService, after: float) -> None:
    """Close the browser out from under the renders, without hanging on it."""
    await asyncio.sleep(after)
    browser = service._browser
    if browser is None:
        return
    task = asyncio.ensure_future(browser.close())
    await asyncio.wait({task}, timeout=10)
    if not task.done():
        task.cancel()


async def _timed(label: str, coro, budget: float):
    """Run with a wall-clock bound so a hang is reported, not waited out."""
    started = time.monotonic()
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=budget)
    if not done:
        task.cancel()
        return None, time.monotonic() - started
    return task.result(), time.monotonic() - started


async def scenario_widget_defects(service: RenderService, work: Path, report: Report) -> None:
    """The two failures that are the model's to fix, and one that only looks like ours."""
    for name, source, expect in (
        ("throws", THROWS, "runtime"),
        ("empty", EMPTY, "empty"),
    ):
        jsx = write_case(work, name, source)
        result = await service.render(jsx, jsx.with_suffix(".png"))
        held, detail = contract_holds(result)
        report.check(
            f"defect/{name}",
            held and result.error_kind == expect,
            f"{detail} (expected {expect})",
        )


async def scenario_render_notes(service: RenderService, work: Path, report: Report) -> None:
    """A success has to carry what the screenshot cannot show."""
    jsx = write_case(work, "overflowing", OVERFLOWING)
    result = await service.render(jsx, jsx.with_suffix(".png"))
    kinds = sorted({note.get("kind") for note in result.render_notes})
    report.check(
        "notes/overflow",
        result.ok and result.has_overflow and "overflow" in kinds,
        f"ok={result.ok} overflow={result.has_overflow} notes={kinds} settled={result.settled}",
    )


async def scenario_kill_browser(service: RenderService, work: Path, report: Report) -> None:
    """Chromium dies while renders are in flight — the OOM-kill case."""
    jsx = write_case(work, "after_browser_kill", GOOD)
    before = service._generation
    killer = asyncio.create_task(_kill_browser_soon(service, 0.2))
    results, elapsed = await _timed(
        "kill-browser",
        asyncio.gather(
            *[service.render(jsx, work / f"bk_{i}.png") for i in range(4)],
            return_exceptions=True,
        ),
        budget=180,
    )
    await killer
    if results is None:
        report.check("kill-browser", False, f"renders did not finish within {elapsed:.0f}s")
        return
    leaked = [r for r in results if isinstance(r, Exception) or not contract_holds(r)[0]]
    report.check(
        "kill-browser",
        not leaked and service._generation > before,
        f"{len(results)} renders in {elapsed:.0f}s, generation {before} -> "
        f"{service._generation}, leaked {len(leaked)}",
    )


async def scenario_kill_vite(service: RenderService, work: Path, report: Report) -> None:
    """The dev server exits. Nothing can render until it is back."""
    jsx = write_case(work, "after_vite_kill", GOOD)
    proc = service._vite_proc
    if proc is None or proc.poll() is not None:
        report.check("kill-vite", True, "skipped: this service does not own Vite")
        return
    before = service._generation
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as exc:
        report.check("kill-vite", True, f"skipped: {type(exc).__name__}")
        return
    result = await service.render(jsx, jsx.with_suffix(".png"))
    held, detail = contract_holds(result)
    report.check(
        "kill-vite",
        held and result.ok,
        f"{detail}, generation {before} -> {service._generation}",
    )


async def scenario_hanging_widget(service: RenderService, work: Path, report: Report) -> None:
    """A widget that never yields. The renderer is healthy; the source is not.

    This is the case the canary exists for: without it, "the widget loops" and
    "the browser stopped answering" are the same symptom and this would retry
    until the collection was killed.
    """
    jsx = write_case(work, "hangs", HANGS)
    result, elapsed = await _timed(
        "hanging-widget", service.render(jsx, jsx.with_suffix(".png")), budget=300
    )
    if result is None:
        report.check("hanging-widget", False, f"never returned within {elapsed:.0f}s")
        return
    held, detail = contract_holds(result)
    report.check(
        "hanging-widget",
        held and result.error_kind == "hang",
        f"{detail} after {elapsed:.0f}s",
    )


async def scenario_concurrent_mixed(service: RenderService, work: Path, report: Report) -> None:
    """What a collection actually looks like: good, broken and empty at once,
    with the browser taken away in the middle of it."""
    cases = []
    for index in range(9):
        source = (GOOD, THROWS, EMPTY, OVERFLOWING)[index % 4]
        cases.append(write_case(work, f"mixed_{index}", source))

    killer = asyncio.create_task(_kill_browser_soon(service, 0.5))
    results, elapsed = await _timed(
        "concurrent-mixed",
        asyncio.gather(
            *[service.render(jsx, jsx.with_suffix(".png")) for jsx in cases],
            return_exceptions=True,
        ),
        budget=300,
    )
    await killer
    if results is None:
        report.check("concurrent-mixed", False, f"did not finish within {elapsed:.0f}s")
        return
    bad = []
    for jsx, result in zip(cases, results):
        if isinstance(result, Exception):
            bad.append(f"{jsx.stem}: raised {type(result).__name__}")
            continue
        held, detail = contract_holds(result)
        if not held:
            bad.append(f"{jsx.stem}: {detail}")
    report.check(
        "concurrent-mixed",
        not bad,
        f"{len(results)} concurrent renders survived a browser kill in {elapsed:.0f}s"
        if not bad else "; ".join(bad),
    )


async def scenario_kill_daemon(service: RenderService, work: Path, report: Report) -> None:
    """The whole render process dies while callers are waiting on it.

    This is the one failure `RenderService` cannot answer for itself: a wedged
    or killed process cannot run the code that would repair it. The supervisor
    starts a new one, and the client's job is to make that look like a slow
    render rather than a failed one — a caller must never have to decide what a
    socket error implies about a widget, which is nothing.

    Runs its own daemon and supervisor; the `service` passed in is unused.
    """
    runtime = work / "daemon"
    runtime.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # Not start_new_session: a supervisor is built to outlive whoever started
    # it, which is right in production and wrong here — killing this harness
    # mid-scenario once left two of them respawning daemons for hours. Keeping
    # it in our process group means one signal reaches all of it.
    supervisor = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "exp" / "run_render_service.py"),
         "--runtime-dir", str(runtime), "--workers", "2",
         "--silence-timeout", "600", "--poll", "2"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        client = RenderClient(runtime_dir=runtime)
        jsx = write_case(work, "via_daemon", GOOD)

        first, elapsed = await _timed(
            "kill-daemon/warmup", client.render(jsx, work / "d0.png"), budget=420
        )
        if first is None or not first.ok:
            report.check("kill-daemon", False, f"daemon never served a render ({elapsed:.0f}s)")
            return

        # Kill it mid-flight and let the supervisor bring it back.
        renders = asyncio.gather(
            *[client.render(jsx, work / f"d{i}.png") for i in range(1, 4)],
            return_exceptions=True,
        )
        await asyncio.sleep(0.3)
        beat = json.loads(ipc.heartbeat_path(runtime).read_text())
        os.killpg(os.getpgid(beat["pid"]), signal.SIGKILL)

        results, elapsed = await _timed("kill-daemon", renders, budget=420)
        if results is None:
            report.check("kill-daemon", False, f"clients never recovered ({elapsed:.0f}s)")
            return
        bad = [
            r for r in results
            if isinstance(r, Exception) or not contract_holds(r)[0]
        ]
        after = json.loads(ipc.heartbeat_path(runtime).read_text())
        report.check(
            "kill-daemon",
            not bad and after["pid"] != beat["pid"],
            f"{len(results)} renders survived a daemon kill in {elapsed:.0f}s "
            f"(pid {beat['pid']} -> {after['pid']})"
            if not bad else "; ".join(str(b)[:80] for b in bad),
        )
    finally:
        # The supervisor restarts daemons; stop it before the daemon, or it
        # simply starts another one.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if supervisor.poll() is not None:
                break
            try:
                supervisor.send_signal(sig)
                supervisor.wait(timeout=20)
            except Exception:
                pass
        beat = ipc.heartbeat_path(runtime)
        try:
            pid = json.loads(beat.read_text())["pid"]
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass


SCENARIOS = {
    "defects": scenario_widget_defects,
    "notes": scenario_render_notes,
    "kill-browser": scenario_kill_browser,
    "kill-vite": scenario_kill_vite,
    "hanging-widget": scenario_hanging_widget,
    "concurrent-mixed": scenario_concurrent_mixed,
    "kill-daemon": scenario_kill_daemon,
}


async def main(names: list[str], workers: int) -> int:
    report = Report()
    work = Path(tempfile.mkdtemp(prefix="render-faults-"))
    print(f"work dir: {work}\nscenarios: {', '.join(names)}\n", flush=True)
    try:
        async with RenderService(n_workers=workers) as service:
            for name in names:
                print(f"[{name}]", flush=True)
                try:
                    await SCENARIOS[name](service, work, report)
                except Exception as exc:
                    report.check(name, False, f"raised {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    passed = sum(1 for _, ok_, _ in report.rows if ok_)
    print(f"\n{passed}/{len(report.rows)} checks passed", flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", choices=sorted(SCENARIOS))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.only or list(SCENARIOS), args.workers)))
