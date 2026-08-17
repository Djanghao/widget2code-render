"""Drive the render service the way its three real callers do, and check it holds.

`simulate_render_faults.py` breaks the renderer on purpose. This one does the
opposite: it reproduces the *shapes* of traffic the renderer actually sees, so
a change that is fine under a smoke test but wrong under real load — a pool
that saturates, a client that serializes, a path the container cannot see —
shows up before a collection or a training run pays for it.

Three shapes, taken from the call sites:

  inference   batch generation — one render per widget, a large batch of them
              at once, bounded by a semaphore. Throughput is the metric.
  teacher     correction episodes running concurrently, each a strictly
              sequential draft → edit → edit chain. Every episode is a slow
              serial thread; the concurrency is across episodes. Per-episode
              latency is the metric.
  grpo        a rollout worker — G samples of the same prompt, all rendering at
              once, many prompts in flight. The bursty one: group size
              multiplies instantly.

Each shape reports what its caller would feel, plus what the contract
guarantees: nothing but a picture or a defect of the widget ever comes back.

    python tests/simulate_render_workloads.py                  # all three
    python tests/simulate_render_workloads.py --only grpo
    python tests/simulate_render_workloads.py --widgets 64     # heavier

Renders go through `make_renderer()`, so this measures the shared daemon when
one is up (`docker/run.sh`) and an in-process pool otherwise — the same
decision every caller makes.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from w2c_render.render_client import make_renderer  # noqa: E402


# A draft the student would produce, and the two edits a correction episode
# applies to it. Deliberately ordinary: a card with text, a chart, an icon.
DRAFT = """export default function Widget() {
  const bars = [%(bars)s];
  return (
    <div style={{width: 360, height: 220, background: '#%(bg)s', color: '#fff',
                 padding: 16, fontFamily: 'system-ui, sans-serif', overflow: 'hidden',
                 borderRadius: 10, display: 'flex', flexDirection: 'column', gap: 8}}>
      <div style={{fontSize: 15, fontWeight: 600}}>Widget %(id)s</div>
      <div style={{fontSize: %(size)d, fontWeight: 700}}>$%(id)s,420.%(id)s</div>
      <svg width="40" height="14" viewBox="0 0 40 14">
        <path d="M2 12 L12 4 L22 9 L38 2" fill="none" stroke="#9fe" strokeWidth="2"
              strokeLinecap="round" />
      </svg>
      <div style={{display: 'flex', gap: 4, marginTop: 'auto'}}>
        {bars.map((h, i) => (
          <div key={i} style={{flex: 1, height: 40, display: 'flex', alignItems: 'flex-end'}}>
            <div style={{width: '100%%', height: `${h}%%`, background: 'rgba(255,255,255,.7)',
                         borderRadius: 2}} />
          </div>
        ))}
      </div>
    </div>
  );
}
"""


def draft(index: int, size: int = 26) -> str:
    bars = ", ".join(str(30 + (index * 7 + i * 13) % 65) for i in range(7))
    return DRAFT % {"bars": bars, "bg": f"{(index * 37) % 0xFFFFFF:06x}",
                    "id": index % 10, "size": size}


class Outcomes:
    """Every result, judged only by what the contract promises."""

    def __init__(self) -> None:
        self.pictures = 0
        self.defects: list[str] = []
        self.leaks: list[str] = []
        self.latencies: list[float] = []

    def record(self, result, seconds: float) -> None:
        self.latencies.append(seconds)
        if result.ok:
            self.pictures += 1
        elif result.is_widget_defect:
            self.defects.append(f"{result.error_kind}: {(result.error or '')[:60]}")
        else:
            # The renderer's own failures are repaired inside render(); one
            # reaching a caller is the contract breaking.
            self.leaks.append(f"{result.error_kind}: {(result.error or '')[:80]}")

    def line(self, label: str, wall: float) -> str:
        n = len(self.latencies)
        p50 = statistics.median(self.latencies) if self.latencies else 0.0
        p95 = (sorted(self.latencies)[int(n * 0.95) - 1] if n >= 20 else max(self.latencies, default=0.0))
        return (f"{label:<26}{n:>5} renders  {wall:6.2f}s wall  "
                f"{n / wall if wall else 0:5.1f}/s  "
                f"p50 {p50 * 1000:5.0f}ms  p95 {p95 * 1000:5.0f}ms  "
                f"pictures {self.pictures}  defects {len(self.defects)}  "
                f"LEAKED {len(self.leaks)}")


async def timed(renderer, jsx: Path, png: Path, outcomes: Outcomes, **kw):
    started = time.monotonic()
    result = await renderer.render(jsx, png, **kw)
    outcomes.record(result, time.monotonic() - started)
    return result


# ---- the three shapes -------------------------------------------------------

async def shape_inference(renderer, work: Path, widgets: int, workers: int) -> tuple[Outcomes, float]:
    """gen_initial.py: render a whole generated batch, semaphore-bounded."""
    outcomes = Outcomes()
    semaphore = asyncio.Semaphore(workers)

    async def one(i: int) -> None:
        jsx = work / f"inf_{i:03d}.jsx"
        jsx.write_text(draft(i))
        async with semaphore:
            await timed(renderer, jsx, jsx.with_suffix(".png"), outcomes,
                        width=360, height=220)

    started = time.monotonic()
    await asyncio.gather(*(one(i) for i in range(widgets)))
    return outcomes, time.monotonic() - started


async def shape_teacher(renderer, work: Path, episodes: int, turns: int) -> tuple[Outcomes, float, list[float]]:
    """The harness: concurrent episodes, each a serial draft → edit → edit chain."""
    outcomes = Outcomes()
    episode_seconds: list[float] = []

    async def episode(e: int) -> None:
        started = time.monotonic()
        jsx = work / f"ep{e:03d}.jsx"
        jsx.write_text(draft(e))
        # The initial render of the draft, then one render per accepted action.
        for turn in range(turns):
            await timed(renderer, jsx, work / f"ep{e:03d}_r{turn}.png", outcomes,
                        width=360, height=220)
            # An edit lands between renders, exactly as the environment writes it.
            jsx.write_text(draft(e, size=26 + 2 * (turn + 1)))
        episode_seconds.append(time.monotonic() - started)

    started = time.monotonic()
    await asyncio.gather(*(episode(e) for e in range(episodes)))
    return outcomes, time.monotonic() - started, episode_seconds


async def shape_grpo(renderer, work: Path, prompts: int, group: int) -> tuple[Outcomes, float]:
    """Rollout: G samples of each prompt render at once, several prompts in flight."""
    outcomes = Outcomes()

    async def rollout(p: int, g: int) -> None:
        jsx = work / f"p{p:02d}_g{g:02d}.jsx"
        jsx.write_text(draft(p * 100 + g))
        await timed(renderer, jsx, jsx.with_suffix(".png"), outcomes,
                    width=360, height=220)

    started = time.monotonic()
    await asyncio.gather(*(rollout(p, g) for p in range(prompts) for g in range(group)))
    return outcomes, time.monotonic() - started


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("inference", "teacher", "grpo"))
    parser.add_argument("--widgets", type=int, default=48, help="inference batch size")
    parser.add_argument("--workers", type=int, default=8, help="inference semaphore")
    parser.add_argument("--episodes", type=int, default=12)
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--prompts", type=int, default=6)
    parser.add_argument("--group", type=int, default=8)
    parser.add_argument("--work", type=Path,
                        default=ROOT / "output" / "_render_workloads",
                        help="scratch directory for the generated widgets")
    args = parser.parse_args()

    work = args.work
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    shapes = [args.only] if args.only else ["inference", "teacher", "grpo"]
    print(f"work dir: {work}")
    leaked = 0

    async with make_renderer(args.workers) as renderer:
        print(f"renderer: {type(renderer).__name__}\n")
        for shape in shapes:
            if shape == "inference":
                outcomes, wall = await shape_inference(renderer, work, args.widgets, args.workers)
                print(outcomes.line(f"inference x{args.widgets}", wall))
            elif shape == "teacher":
                outcomes, wall, per_episode = await shape_teacher(
                    renderer, work, args.episodes, args.turns)
                print(outcomes.line(f"teacher {args.episodes}x{args.turns}", wall))
                print(f"{'':26}episode wall: median "
                      f"{statistics.median(per_episode):.2f}s, max {max(per_episode):.2f}s")
            else:
                outcomes, wall = await shape_grpo(renderer, work, args.prompts, args.group)
                print(outcomes.line(f"grpo {args.prompts}x{args.group}", wall))
            for defect in outcomes.defects[:3]:
                print(f"{'':26}defect: {defect}")
            for leak in outcomes.leaks[:3]:
                print(f"{'':26}LEAK:   {leak}")
            leaked += len(outcomes.leaks)

    print()
    if leaked:
        print(f"FAIL: {leaked} infrastructure failure(s) reached a caller")
        return 1
    print("OK: every render returned a picture or a defect of the widget")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
