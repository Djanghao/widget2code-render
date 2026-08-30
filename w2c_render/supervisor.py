"""Keep a render daemon alive, and kill it when it stops making progress.

The daemon repairs everything inside itself and never gives up, which leaves
exactly one failure it cannot answer: being wedged. A blocked event loop or an
exhausted process cannot run the code that would fix it, so something outside
has to notice and start over.

Noticing needs the right signal, and the obvious one is wrong. "Requests
outstanding and nothing completed for ten minutes" reads as a stall, but after
an idle spell the last completion is already ten minutes old, so the first
request to arrive satisfies it and a healthy daemon is killed while serving
that request. It happened four times in thirty-three hours before this was
written, each "stall" the length of a gap between runs, and each one surfacing
as a dropped connection to whoever sent that request.

The measurement is from when the outstanding work began. An idle daemon has
nothing outstanding to be late; a stuck one is late from the moment its request
went in. `oldest_in_flight_at` is what the heartbeat carries for it.

SIGKILL rather than SIGTERM on that path, deliberately: a wedged process may not
run its handlers either, and the restart is also how leaked Chromium processes
and Playwright drivers get reaped — the kernel does the cleanup the daemon can
only attempt.

Clients wait through all of it. A render spanning a restart is slow, not failed.

    python w2c_render/supervisor.py --workers 8
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from w2c_render import render_ipc as ipc  # noqa: E402
from w2c_render.source_policy import source_policy_from_values  # noqa: E402


def read_heartbeat(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def diagnose(beat: dict | None, *, now: float, stall_s: float, silence_s: float) -> str | None:
    """Why this daemon should be killed, or None to leave it alone."""
    if beat is None:
        return None  # not up yet; the spawn path handles that
    if now - beat.get("now", 0) > silence_s:
        return (
            f"heartbeat is {now - beat.get('now', 0):.0f}s old "
            f"(> {silence_s:.0f}s): the daemon is not even ticking"
        )
    if beat.get("in_flight", 0) > 0:
        # Whichever is later: work cannot be overdue before it arrived, and completions since it
        # arrived are proof the daemon is serving. A heartbeat from before this field existed has
        # no oldest_in_flight_at, and falls back to the reading that only errs towards killing.
        oldest = beat.get("oldest_in_flight_at") or beat.get("last_completed_at", 0)
        since = now - max(oldest, beat.get("last_completed_at", 0))
        if since > stall_s:
            return (
                f"{beat['in_flight']} render(s) outstanding and nothing completed for "
                f"{since:.0f}s (> {stall_s:.0f}s)"
            )
    return None


def kill_process_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=ipc.DEFAULT_RUNTIME_DIR)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--stall-timeout", type=float, default=600.0,
        help="Seconds with work outstanding and nothing completing before a restart.",
    )
    parser.add_argument(
        "--silence-timeout", type=float, default=60.0,
        help="Seconds without a heartbeat tick before a restart.",
    )
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument(
        "--allow-import",
        action="append",
        default=[],
        help="Allowed bare package or package/* pattern; repeat or comma-separate. "
        "Also reads W2C_RENDER_ALLOWED_IMPORTS.",
    )
    parser.add_argument(
        "--allow-dynamic-imports",
        action="store_true",
        default=os.environ.get("W2C_RENDER_ALLOW_DYNAMIC_IMPORTS", "").lower()
        in {"1", "true", "yes"},
    )
    args = parser.parse_args()
    policy_values = list(args.allow_import)
    if os.environ.get("W2C_RENDER_ALLOWED_IMPORTS"):
        policy_values.append(os.environ["W2C_RENDER_ALLOWED_IMPORTS"])
    source_policy = source_policy_from_values(
        policy_values,
        allow_dynamic_imports=args.allow_dynamic_imports,
    )

    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = ipc.heartbeat_path(args.runtime_dir)
    restarts = 0
    proc: subprocess.Popen | None = None
    stopping = False

    def _stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        while not stopping:
            if proc is None or proc.poll() is not None:
                if proc is not None:
                    restarts += 1
                    print(
                        f"supervisor: daemon exited with {proc.returncode}; "
                        f"restart #{restarts}",
                        flush=True,
                    )
                heartbeat.unlink(missing_ok=True)
                daemon_command = [
                    sys.executable, "-u", "-m", "w2c_render.render_daemon",
                    "--runtime-dir", str(args.runtime_dir),
                    "--workers", str(args.workers),
                ]
                for pattern in source_policy.allowed_imports:
                    daemon_command.extend(("--allow-import", pattern))
                if source_policy.allow_dynamic_imports:
                    daemon_command.append("--allow-dynamic-imports")
                proc = subprocess.Popen(
                    daemon_command,
                    cwd=str(ROOT),
                    start_new_session=True,
                )
                print(
                    f"supervisor: started daemon pid {proc.pid} "
                    f"with {source_policy.policy_id}",
                    flush=True,
                )
                # Give it room to boot before judging its pulse.
                deadline = time.time() + args.silence_timeout
                while time.time() < deadline and proc.poll() is None:
                    if read_heartbeat(heartbeat):
                        break
                    time.sleep(args.poll)

            time.sleep(args.poll)
            reason = diagnose(
                read_heartbeat(heartbeat),
                now=time.time(),
                stall_s=args.stall_timeout,
                silence_s=args.silence_timeout,
            )
            if reason and proc is not None and proc.poll() is None:
                print(f"supervisor: KILLING wedged daemon — {reason}", flush=True)
                kill_process_group(proc.pid)
                try:
                    proc.wait(timeout=30)
                except Exception:
                    pass
    finally:
        if proc is not None and proc.poll() is None:
            print("supervisor: stopping daemon", flush=True)
            try:
                proc.terminate()
                proc.wait(timeout=30)
            except Exception:
                kill_process_group(proc.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
