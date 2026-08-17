"""Prove this environment renders the golden set byte-exactly, or refuse.

Cross-machine identity is not assumed, it is checked: every host renders the
golden widgets (plain DOM, echarts, recharts, SVG, font stacks) and compares
SHA-256 of the PNGs against the checksums recorded in golden.json. A match
proves the whole raster stack — Chromium, fonts, chart libraries — produces
the reference pixels on this machine; a mismatch aborts before the daemon
serves anything, so a deviating host can never contaminate a collection.

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
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from w2c_render.render import RenderService  # noqa: E402

GOLDEN_DIR = HERE / "golden"
GOLDEN_JSON = HERE / "golden.json"


async def render_hashes() -> dict[str, str]:
    """Render the whole golden set at once — it is a startup cost, not a test."""
    widgets = sorted(GOLDEN_DIR.glob("*.jsx"))
    if not widgets:
        raise SystemExit(f"selfcheck: no golden widgets in {GOLDEN_DIR}")
    with tempfile.TemporaryDirectory() as tmp:
        async with RenderService(n_workers=len(widgets)) as svc:
            results = await asyncio.gather(*(
                svc.render(jsx, Path(tmp) / f"{jsx.stem}.png") for jsx in widgets
            ))
            hashes: dict[str, str] = {}
            for jsx, result in zip(widgets, results):
                if not result.ok:
                    raise SystemExit(f"selfcheck: {jsx.name} failed to render: {result.error}")
                hashes[jsx.stem] = hashlib.sha256(result.png_path.read_bytes()).hexdigest()
    return hashes


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
    args = parser.parse_args()

    if args.cached and _marker().exists():
        print(f"selfcheck: already proven on this machine ({_marker().name})", flush=True)
        return 0

    hashes = asyncio.run(render_hashes())

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
    print(f"selfcheck: {len(hashes)}/{len(hashes)} golden renders byte-exact", flush=True)
    if args.cached:
        _marker().parent.mkdir(parents=True, exist_ok=True)
        _marker().touch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
