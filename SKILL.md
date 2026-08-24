---
name: w2c-render
description: >-
  Render self-contained React/JSX widgets to PNG through a frozen, pixel-stable
  renderer. Use when turning widget code into an image, checking why a widget
  fails to render, collecting rendered trajectories, or scoring generated code
  against a screenshot. Covers pulling the image, running the daemon, calling it
  from Python, every parameter, and how to read a failure.
---

# w2c-render

Turns a React/JSX widget into a PNG. Everything that decides a pixel - Chromium,
its font set, node, and the chart libraries - is baked into one image, so the
same widget renders byte-identically on every amd64 machine. The container
proves that on startup against a golden set instead of assuming it.

## Deploy

The daemon is the supported way to run it: one browser pool per machine, nothing
to install, and no per-call startup cost.

```bash
docker run -d --name w2c-render --restart unless-stopped --init --shm-size=2g \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /tmp/w2c-render:/tmp/w2c-render \
  houstonzhang/w2c-render:latest
```

Or from a checkout, which also waits until the socket answers:

```bash
docker/run.sh [workers]        # default 8
```

| what | why it is there |
|---|---|
| `--user $(id -u):$(id -g)` | the socket has to be connectable by the caller, so the container runs as you, not root |
| `-e HOME=/tmp` | Chromium and fontconfig need a writable home for their caches |
| `--shm-size=2g` | Chromium crashes on the default 64 MB shared memory |
| `--init` | reaps the browser processes a render leaves behind |
| `-v /tmp/w2c-render` | the only mount: source and screenshot both travel over the socket, so the daemon never sees your files |

Readiness: `/tmp/w2c-render/render.sock` exists and `heartbeat.json` is being
updated. The container refuses to serve if its golden self-check fails, so a
machine whose pixels deviate stops rather than quietly producing different ones.

## Environment

| variable | default | effect |
|---|---|---|
| `W2C_RENDER_WORKERS` | `8` | browser contexts, i.e. concurrent renders |
| `W2C_RENDER_IMAGE` | `w2c-render:latest` | image `docker/run.sh` starts |
| `W2C_RENDER_RUNTIME_DIR` | `/tmp/w2c-render` | where the socket and heartbeat live |
| `W2C_RENDER_ALLOW_REACT_ICONS` | `0` | set to `1` to allow static `react-icons/*` imports |

Workers cost memory, not GPU - the renderer never uses CUDA. Size it against
your rollout concurrency.

## Call it

```python
from w2c_render import make_renderer          # or: from core.services import make_renderer

async with make_renderer(8) as r:             # RenderClient if the socket exists,
    result = await r.render(                  # in-process RenderService otherwise
        "widget.jsx",
        output_path="out.png",
        width=976, height=668,
    )
```

`render()` parameters:

| parameter | default | meaning |
|---|---|---|
| `jsx_path` | — | the widget source; read by the client, never by the daemon |
| `output_path` | `<jsx>.png` | where the screenshot is written |
| `width`, `height` | natural size | viewport to render at |
| `wait_extra_ms` | `200` | settle time after the page reports ready |
| `force_resize` | `True` | force the widget to the requested box rather than letting content grow it |
| `freeze_animations` | `True` | pause CSS/JS animation so the same frame is captured every time |

`render_source(source, output_path, ...)` takes the code as a string instead of
a path. `render_dir(dir, ...)` renders every `.jsx` in a directory.

## Read the result

```python
result.ok             # True iff a PNG was written
result.png_path
result.error          # set only when no PNG was produced
result.error_kind     # runtime | empty | hang | infra | timeout
result.console_errors # diagnostics on both paths
result.render_notes   # measured facts the picture cannot show, e.g. overflow
result.settled, result.settle_ms
```

`error_kind` is the field that matters when a render feeds a model: `runtime`,
`empty` and `hang` are defects of the widget and belong in feedback to whoever
wrote it; `infra` and `timeout` are properties of the rendering process and must
not be reported as the code's fault - they mean retry, not penalise.

A widget that renders but overflows its box still has `ok=True`; look in
`render_notes` for it.

## Troubleshooting

| symptom | cause |
|---|---|
| socket missing, container exited | the golden self-check failed; `docker logs w2c-render` |
| every render times out | `--shm-size` too small, or workers oversubscribed |
| `error_kind=infra` | a browser context died; the daemon restarts it, retry the call |
| pixels differ from another machine | the images differ - compare `W2C_IMAGE_STAMP`, and pin the digest |

Pin the digest, not the tag, when a run has to be provably the same renderer.
