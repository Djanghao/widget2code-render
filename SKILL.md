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

Started with nothing set, that daemon serves `m1` and answers requests that name `m2` as
well — see [Two contracts](#two-contracts). `W2C_RENDER_ALLOWED_IMPORTS` is for an
allowlist neither mode covers: exact comma-separated packages or package-subpath patterns
such as `react-icons/*`, and only dependencies baked into the image can resolve.
`W2C_RENDER_ALLOW_DYNAMIC_IMPORTS=1` separately permits literal dynamic imports.

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

A wedged daemon is restarted from outside it, so a client should wait through a
restart rather than treat a dropped connection as a failed render. Nothing you send
can cause one: a widget that will not compile and a page that hangs both come back as
results carrying an `error_kind`.

Readiness: `/tmp/w2c-render/render.sock` exists and `heartbeat.json` is being
updated. The container refuses to serve if its golden self-check fails, so a
machine whose pixels deviate stops rather than quietly producing different ones.

## Environment

| variable | default | effect |
|---|---|---|
| `W2C_RENDER_WORKERS` | `8` | browser contexts, i.e. concurrent renders |
| `W2C_RENDER_IMAGE` | `w2c-render:latest` | image `docker/run.sh` starts |
| `W2C_RENDER_RUNTIME_DIR` | `/tmp/w2c-render` | where the socket and heartbeat live |
| `W2C_RENDER_MODE` | `m1` | default contract for requests that name none |
| `W2C_RENDER_STALL_TIMEOUT` | `500` | seconds of outstanding work with nothing completing before the daemon is restarted |
| `W2C_RENDER_ALLOWED_IMPORTS` | empty | comma-separated bare packages or `package/*` patterns |
| `W2C_RENDER_ALLOW_DYNAMIC_IMPORTS` | empty | `1/true/yes` permits literal dynamic imports, still allowlisted |

Workers cost memory, not GPU - the renderer never uses CUDA. Size it against
your rollout concurrency.

## Two contracts

A widget is written against one of two contracts, and the renderer serves both. They are
exclusive rather than nested: source written for one fails loudly under the other, which
is what keeps a collection from quietly becoming a mixture.

| | `m1` | `m2` |
|---|---|---|
| imports | none allowed | `recharts`, `react-icons/pi`, `react-icons/si` |
| on the page | `React`, `Recharts` as globals | nothing |
| a chart | `<Recharts.AreaChart>` | `import { AreaChart } from 'recharts'` |
| an icon | drawn by hand in SVG | `import { PiEyeBold } from 'react-icons/pi'` |
| `import React` | unnecessary — it is global | refused; the automatic JSX runtime makes it pointless, and its absence puts hooks and state out of reach |

`m1` is the older contract and what the 1,816-widget reference set is written in. `m2` buys
real icon sets and ordinary React import syntax, at the price of its own reference data.

Pick per request, or give a daemon a default:

```python
await client.render_source(source, "out.png", mode="m2")   # this render
```

```bash
W2C_RENDER_MODE=m2 docker/run.sh 16                         # every render this daemon serves
```

Unset is `m1`. Every result carries the contract it was rendered under — `mode`,
`allowed_imports`, `globals` and a `policy_id` over all three — so a collection can be
checked for the contract that produced it rather than trusted to have used one.

Written for the wrong contract, source does not half-work:

```
m1 source under m2   ReferenceError: Recharts is not defined
m2 source under m1   SourcePolicyError: import 'recharts' is not allowed
```

## Get the client

The client is inside the image, so a machine that can pull the image needs nothing
else — no checkout, no `pip install`, no package on any index.

```bash
docker run --rm --entrypoint tar houstonzhang/w2c-render:latest \
  -cf - -C /opt/w2c w2c_render | tar -xf -
```

120 KB, standard library only: `RenderService`, the half that needs Playwright, is
imported lazily and never touched by a process that only talks to the daemon. Or skip
it entirely and speak the socket yourself — see [Talking to it](#talking-to-it), which
is one JSON line each way and needs no files at all.

## Call it

```python
from w2c_render import make_renderer

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
| `freeze_animations` | `True` | land CSS animations and transitions on their final frame |

`render_source(source, output_path, ...)` takes the code as a string instead of
a path. `render_dir(dir, ...)` renders every `.jsx` in a directory.

## What comes back

`ok` decides the shape. A render carries an `image` and a `layout`, or an
`error`, never both.

```python
result.ok                  # True iff a PNG was written
result.png_path
result.width, result.height          # the screenshot's own size

result.error               # set only when no PNG was produced
result.error_kind          # syntax | runtime | empty | hang | policy | unknown

result.render_notes        # measured facts the picture cannot show (below)
result.feedback_text       # THE ONE STRING TO SHOW A MODEL
result.console_errors, result.unclassified   # diagnostics; never model-facing
result.settled, result.settle_ms
result.source_policy       # policy_id, allowed_imports, dynamic-import flag
```

### feedback_text — the only thing a model should read

The service holds every fact behind it, so it writes the sentence once. Three
projects each wrote their own version of it and no two runs' feedback were
comparable.

```
Rendered 300x150.
```
```
Rendered 300x150. These problems may not be visible in the image:
- content overflows the bottom edge by 71px (<div> 268x205 "Quarterly Revenue Report")
Shrink the content to fit: reduce font sizes, paddings, gaps, line-heights, icon/image sizes.
If you judge any of these not to be a problem, ignore it.
```
```
RENDER FAILED (no image):
SyntaxError at line 45:23 — Expected identifier but found ","
  44 |               top: 10,
  45 |               left: 50%,
     |                        ^
```

Everything in it is derived from `error` and `render_notes`, so a caller that
wants its own wording still has the numbers. Nothing in it names the daemon's
temporary directory, its dev server, or Vite's cache-busting nonce.

### error_kind

| kind | means | show the model? |
|---|---|---|
| `syntax` | it would not compile; the message carries the line and a caret | yes |
| `runtime` | the component threw | yes |
| `unknown_export` | a name imported from a package that does not export it — an invented icon name, usually, and one of ten costs the whole render | yes |
| `empty` | no DOM element, or zero size | yes |
| `hang` | never yielded its main thread | yes |
| `policy` | imported something the source policy does not allow | yes |
| `unknown` | the renderer repaired itself and still cannot explain this file | **no** — a bug report about the service |

`infra` and `timeout` never leave the service; they are retried and repaired
internally. A kind you do not recognise is not the widget's fault.

### render_notes — what the picture cannot say

The screenshot is clipped to the widget's box, so some defects are invisible or
indistinguishable from an intentionally empty area. One DOM pass after the page
settles measures them:

| kind | criterion | fields |
|---|---|---|
| `overflow` | in-flow content extends past the widget's border-box | `side` `amount` `tag` `w` `h` `text` |
| `unpainted` | geometry authored, no pixel painted | `tag` `attr` `value` |
| `unloaded` | `<img>` never decoded | `src` |
| `zero_size` | drawing surface with no area | `tag` `w` `h` |

Only the outermost element accountable for an overflowing side is reported: a
descendant crosses the edge because its ancestor does, and saying it once is the
difference between 441 notes and 111 over one collection.

A widget that renders but overflows still has `ok=True` — look in
`render_notes`, or just read `feedback_text`.

## Talking to it

One JSON line each way over the socket, so any language reaches it and the Python
client above is a convenience rather than a requirement. The request is unchanged from
v3; the reply is the four groups above.

```python
import base64, json, socket

req = {"v": 4, "name": "w.jsx", "width": None, "height": None,
       "wait_extra_ms": 200, "force_resize": True, "freeze_animations": True,
       "source": "export default function Widget(){ return <div style={{width:200,"
                 "height:80,background:'#28a'}}/>; }"}

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/tmp/w2c-render/render.sock")
s.sendall((json.dumps(req) + "\n").encode())
buf = b""
while not buf.endswith(b"\n"):
    chunk = s.recv(1 << 20)
    if not chunk:
        break
    buf += chunk

reply = json.loads(buf)
if reply["ok"]:
    open("out.png", "wb").write(base64.b64decode(reply["image"]["png_b64"]))
print(reply["feedback_text"])
```

```jsonc
{
  "v": 4,
  "ok": true,
  "image":  {"png_b64": "…", "width": 300, "height": 150},   // null when ok is false
  "error":  null,                                            // {kind, text} when ok is false
  "layout": [ /* render notes */ ],                          // null when ok is false
  "feedback_text": "…",
  "log": {"settled": true, "settle_ms": 227, "console": [],
          "unclassified": [], "source_policy": {…}}
}
```

`log` is what is true but not actionable. `unclassified` is console output the
service has no rule for yet — read it yourself, never give it to a model.

## What it costs

Measured on an idle 96-core machine, 16 workers:

| | |
|---|---|
| One render | ~400 ms, of which 200 ms is the `wait_extra_ms` pause |
| Throughput | ~20/s, reached at concurrency 16 and flat beyond it |
| Not the limit | CPU (4–6 cores of 96), Vite (400 transforms/s), worker count (16 and 48 measure the same) |

Lowering `wait_extra_ms` is the one parameter that moves single-render latency:
0 ms gives ~180 ms. It is 200 by default because a widget that is still moving
would otherwise be screenshotted mid-animation.

Chart animation is not among the things it waits for, and does not need to be:
recharts draws its charts finished, because the browser reports
`prefers-reduced-motion` and recharts 3 resolves its default
`isAnimationActive: 'auto'` against it. A widget that asks for animation
explicitly — `isAnimationActive={true}` — still animates, and is the one
remaining way to get a different picture from the same source.

## Troubleshooting

| symptom | cause |
|---|---|
| socket missing, container exited | the golden self-check failed; `docker logs w2c-render` |
| every render times out | `--shm-size` too small, or workers oversubscribed |
| `error_kind=infra` | a browser context died; the daemon restarts it, retry the call |
| pixels differ from another machine | the images differ - compare `W2C_IMAGE_STAMP`, and pin the digest |

Every build is published under three tags: the version (`1.2.1`), the commit it
was built from, and `latest`. Pin the version for a reproducible setup and the
digest when a run has to be provably the same renderer.
Also record `result.source_policy["policy_id"]` (or the identical
`/tmp/w2c-render/source_policy.json`), because one image can serve different startup profiles.
