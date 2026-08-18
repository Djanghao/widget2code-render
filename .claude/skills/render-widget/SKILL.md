---
name: render-widget
description: Render JSX widgets to PNG through the shared render service — start it, call it, and read what comes back. Use when rendering widget code, checking why a widget fails to render, collecting teacher trajectories, scoring GRPO rollouts, or debugging the renderer itself (hang, timeout, overflow, unloaded images).
---

# Rendering a widget

The renderer is a service, not a library call. Start it once per machine; every
caller shares one browser pool.

```bash
W2C_RENDER_IMAGE=houstonzhang/w2c-render:latest docker/render/run.sh 8
```

First start on a machine takes ~1 min (it renders five canaries and compares
SHA-256 against the checksums in the image, so a host that does not reproduce
the reference pixels refuses to serve). Restarts take ~5s. `docker/render/run.sh`
pulls the image if it is missing.

## Calling it

```python
from w2c_render import make_renderer          # client if a daemon is up, else in-process

async with make_renderer(n_workers=8) as renderer:
    result = await renderer.render(jsx_path, png_path)
    result = await renderer.render_source(code, png_path)   # code in memory, no file
```

Only the socket is shared, never the filesystem: source goes over the wire and
the PNG comes back, so `png_path` may be anywhere the caller can write.

**Concurrency: match the pool.** Measured on 8 workers — throughput peaks at 8
concurrent (12.7/s) and does not improve above it; 128 concurrent renders the
same 12.7/s with p50 climbing from 540ms to 8.1s, all of it queueing. Bound the
caller with a semaphore of `n_workers`; requests beyond it queue and never fail.

## Reading the answer

Three outcomes, and no fourth:

```python
if result.ok:
    result.png_path          # written and signature-checked
    result.render_notes      # what the screenshot cannot show (below)
    result.settled           # False means the page never went quiet
elif result.is_widget_defect:
    result.error_kind        # syntax | runtime | empty | hang
    result.error             # the actual reason, with line and column for syntax
else:
    result.error_kind == "unknown"   # the renderer's own failure, never the code's
```

`unknown` means the renderer repaired itself repeatedly and still cannot render
the file. **Never show it to a model as a defect of the widget** — drop or
retry the sample, and treat its appearance as a bug report about the service.

| error_kind | means |
|---|---|
| `syntax` | esbuild's diagnostic with line, column and a caret excerpt |
| `runtime` | the component threw — `ReferenceError: BedIcon is not defined` |
| `empty` | no DOM element, or an element with zero size |
| `hang` | the widget's own page could not evaluate `1`, so it blocks its main thread |

Infrastructure failures — a dead browser, a lost dev server, a bare timeout —
are repaired and retried inside and never returned.

## render_notes — what the picture cannot say

The screenshot is clipped to the widget's box, and "this shape failed to paint"
looks exactly like "nothing was meant to be here". After the page settles, one
DOM pass measures what the image cannot:

| kind | example line | teacher fix rate |
|---|---|---|
| `overflow` | `overflow: right +120px — div 1096x296 "Pesquisar"` | 91% |
| `unloaded` | `unloaded: <img src="https://…"> never loaded` | 88% |
| `zero_size` | `zero_size: <svg> drawing surface is 1352x0 — nothing inside it can appear` | 56% |
| `unpainted` | `unpainted: <polygon points="64,40 76,32 84,36 72,44 Z"> paints nothing` | 40% |

Render them for a model with `the caller's own formatter (the notes are plain dicts)` —
teacher collection, SFT export and GRPO rollout share that one implementation,
so the wording cannot drift between training and RL. The fix rates come from
1,816 episodes; `unpainted` states a consequence without a cause, which is why
it is the hardest of the four to act on.

## Determinism

Same code, same pixels — on every machine, months apart. The rasterization stack
(Chromium, fonts, chart libraries) is frozen in the image, and animations are
landed on their finished state before the audit and the screenshot, so a CSS
transition cannot be caught mid-flight. Pass `freeze_animations=False` only to
inspect an animation in motion.

Pin a tag or digest for work that must be repeatable; `latest` moves on rebuild.

## When something looks wrong

| symptom | what it is |
|---|---|
| client prints `STILL WAITING for …/render.sock` | the daemon is starting or restarting; the call is waiting, not failing |
| container exits with `selfcheck: MISMATCH` | this host does not reproduce the reference pixels — do not bypass it |
| many `unknown` | the renderer is genuinely broken; check `docker logs w2c-render` |
| `RenderTransportError: exceeded STREAM_LIMIT` | a screenshot over 64 MB; raise `w2c_render.render_ipc.STREAM_LIMIT` on both ends |

Full contract: `README.md`. Fault injection: `tests/simulate_render_faults.py`.
Traffic shapes: `tests/simulate_render_workloads.py`.
