"""Prove this environment renders the golden set byte-exactly, or refuse.

Two identities, and they are different properties. *Across machines*: every
host renders the golden widgets (plain DOM, recharts, SVG, font stacks, an m2
widget whose imports have to resolve, and one written with recharts' defaults)
and compares SHA-256 of the PNGs against the checksums recorded in golden.json.
A match proves the whole raster stack — Chromium, fonts, chart libraries —
produces the reference pixels here; a mismatch aborts before the daemon serves
anything, so a deviating host can never contaminate a collection.

*Across two renders on one machine*: each widget is rendered `--repeats` times
and the hashes have to agree with each other before any of them is compared to
the reference. That half used to have no canary at all, and it is exactly the
half that failed: Chromium reused a tile from an earlier frame and half a
percent of chart renders came back a different picture — invisible to a
reference check, because a single render per widget can only ever be
self-consistent.

    python docker/selfcheck.py                 # verify (exit 1 on any diff)
    python docker/selfcheck.py --cached        # verify once per machine+image
    python docker/selfcheck.py --make-golden   # (re)record golden.json

`--cached` (what the container's CMD uses) records a marker in the runtime
dir — mounted from the host — keyed on the image stamp: the first start of an
image on a machine renders and verifies, every restart after it costs nothing.
Proving the same pixels twice on the same machine buys nothing and delays the
daemon that everything else is waiting for.

Regenerate golden.json only on the reference machine, and only when the
rendering environment is deliberately changed (new image); then rebuild the
image so the new checksums are baked in.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from w2c_render.render import RenderService  # noqa: E402

GOLDEN_DIR = HERE / "golden"
GOLDEN_JSON = HERE / "golden.json"

# A canary is rendered under the contract its name says. Without one for m2 the only proof
# that imports resolve at all would be a collection run discovering it, and the way that
# failed once -- Vite optimising a dependency mid-render and leaving React loaded twice --
# blamed the widget for it and cost only the first render of a fresh container.
MODE_BY_PREFIX = {"m2-": "m2"}

# Three renders is what separates "stable" from "stable once". The failure this
# guards against showed up in about one chart render in two hundred, so three
# will not catch every regression on the first start — but a regression severe
# enough to matter (an animating chart, a reused tile on a whole class of
# widgets) changes the reference hash too, and that is checked on every render.
REPEATS = int(os.environ.get("W2C_SELFCHECK_REPEATS", "3"))


def _mode_for(name: str) -> str:
    for prefix, mode in MODE_BY_PREFIX.items():
        if name.startswith(prefix):
            return mode
    return "m1"


async def render_hashes(repeats: int = REPEATS) -> dict[str, str]:
    """Render the whole golden set `repeats` times — a startup cost, not a test.

    Every repeat of a widget has to produce the same bytes as every other before
    the widget has a hash at all. A canary rendered once proves the machine
    agrees with the reference; only a canary rendered twice proves the machine
    agrees with itself.
    """
    widgets = sorted(GOLDEN_DIR.glob("*.jsx"))
    if not widgets:
        raise SystemExit(f"selfcheck: no golden widgets in {GOLDEN_DIR}")
    jobs = [(jsx, run) for run in range(repeats) for jsx in widgets]
    with tempfile.TemporaryDirectory() as tmp:
        async with RenderService(n_workers=len(widgets)) as svc:
            results = await asyncio.gather(*(
                svc.render(jsx, Path(tmp) / f"{jsx.stem}-{run}.png", mode=_mode_for(jsx.stem))
                for jsx, run in jobs
            ))
            seen: dict[str, set[str]] = {}
            for (jsx, _), result in zip(jobs, results):
                if not result.ok:
                    raise SystemExit(
                        f"selfcheck: {jsx.name} failed to render under "
                        f"{_mode_for(jsx.stem)}: {result.error}"
                    )
                seen.setdefault(jsx.stem, set()).add(
                    hashlib.sha256(result.png_path.read_bytes()).hexdigest())
    unstable = {name: sorted(h) for name, h in seen.items() if len(h) > 1}
    if unstable:
        for name, h in sorted(unstable.items()):
            print(f"selfcheck: UNSTABLE {name}: {len(h)} distinct renders of one "
                  f"widget in {repeats} — {', '.join(x[:12] for x in h)}", file=sys.stderr)
        print("selfcheck: this host does not render the same widget the same way "
              "twice; refusing to serve", file=sys.stderr)
        raise SystemExit(1)
    return {name: h.pop() for name, h in seen.items()}


def _marker() -> Path:
    """Per-machine proof marker, keyed on the exact image build."""
    runtime = Path(os.environ.get("W2C_RENDER_RUNTIME_DIR", "/tmp/w2c-render"))
    return runtime / f"selfcheck-ok-{os.environ.get('W2C_IMAGE_STAMP', 'dev')}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--make-golden", action="store_true",
                        help="record current hashes as the golden reference")
    parser.add_argument("--cached", action="store_true",
                        help="skip when this machine already proved this image")
    parser.add_argument("--out", type=Path, default=GOLDEN_JSON,
                        help="where --make-golden writes (default: baked location)")
    parser.add_argument("--repeats", type=int, default=REPEATS,
                        help="renders per widget; they must all agree (default: %(default)s)")
    args = parser.parse_args()

    if args.cached and _marker().exists():
        print(f"selfcheck: already proven on this machine ({_marker().name})", flush=True)
        return 0

    hashes = asyncio.run(render_hashes(args.repeats))

    if args.make_golden:
        args.out.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")
        print(f"selfcheck: recorded {len(hashes)} golden hashes to {args.out}")
        return 0

    if not GOLDEN_JSON.exists():
        print(f"selfcheck: {GOLDEN_JSON} missing — run --make-golden on the "
              "reference machine and rebuild the image", file=sys.stderr)
        return 1
    golden = json.loads(GOLDEN_JSON.read_text())
    bad = {name: (golden.get(name), got) for name, got in hashes.items()
           if golden.get(name) != got}
    missing = sorted(set(golden) - set(hashes))
    if bad or missing:
        for name, (want, got) in sorted(bad.items()):
            print(f"selfcheck: MISMATCH {name}: golden {want} != rendered {got}",
                  file=sys.stderr)
        for name in missing:
            print(f"selfcheck: MISSING golden widget {name}.jsx", file=sys.stderr)
        print("selfcheck: this host does NOT reproduce the reference pixels; "
              "refusing to serve", file=sys.stderr)
        return 1
    print(f"selfcheck: {len(hashes)}/{len(hashes)} golden renders byte-exact "
          f"and stable over {args.repeats}", flush=True)
    if args.cached:
        # The proof is what matters; the marker only saves the next start from
        # repeating it. A runtime directory this process may not write to (one
        # the host created as another user) is a reason to re-prove every start,
        # never a reason to fail a check that has already passed.
        try:
            _marker().parent.mkdir(parents=True, exist_ok=True)
            _marker().touch()
        except OSError as problem:
            print(f"selfcheck: could not record the proof marker ({problem}); "
                  "every start will re-prove this image", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
