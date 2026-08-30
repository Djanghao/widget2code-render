# widget2code-render

JSX widget → PNG. Runs as a service over a Unix socket. Every render returns a
picture or a defect of the submitted code — never a failure of the renderer.

Image: [`houstonzhang/w2c-render`](https://hub.docker.com/r/houstonzhang/w2c-render)

## Using it from an agent

The full instructions - deploy, call, every parameter, and how to read a
failure - are one file:

**https://github.com/Djanghao/widget2code-render/blob/main/SKILL.md**

## Quick start

```bash
docker run -d --name w2c-render --restart unless-stopped --init --shm-size=2g \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v /tmp/w2c-render:/tmp/w2c-render \
  houstonzhang/w2c-render:latest
```

```python
from w2c_render import RenderClient

async with RenderClient() as renderer:
    result = await renderer.render_source(jsx_code, "out.png")
    result.ok, result.png_path, result.render_notes, result.feedback_text
```

`/tmp/w2c-render` (socket + heartbeat) is the only mount. Source and screenshot
travel over the socket, so output paths need not be visible to the container.

## Results

`ok` decides the shape: a render carries an `image` and a `layout`, or an
`error`, never both.

| | |
|---|---|
| `ok=True` | PNG written and signature-verified, its size, and `layout` |
| `error.kind="syntax"` | it would not compile; the message has the line and a caret |
| `error.kind="runtime"` | the component threw |
| `error.kind="unknown_export"` | it imported a name the package does not export |
| `error.kind="empty"` | no DOM element, or zero size |
| `error.kind="hang"` | never yielded its main thread |
| `error.kind="policy"` | it imported something the source policy does not allow |

`feedback_text` is the one wording a model is shown, written from those facts
so that two experiments' feedback are comparable:

```
Rendered 300x150. These problems may not be visible in the image:
- content overflows the bottom edge by 71px (<div> 268x205 "Quarterly Revenue Report")
Shrink the content to fit: reduce font sizes, paddings, gaps, line-heights, icon/image sizes.
If you judge any of these not to be a problem, ignore it.
```

`log` is what is true but not actionable — settle time, the console, and
`unclassified`: console output this service has no rule for yet. It is there to
be persisted and mined, and it is never put in front of a model.

Dead browser, lost dev server, bare timeout, truncated screenshot: retried and
repaired internally, never returned. A client that cannot reach the daemon waits
instead of failing. Requests beyond the pool size queue; they do not fail.

### layout

The screenshot is clipped to the widget's box, so some defects are invisible in
it. After the page settles, one DOM pass measures them:

| kind | criterion |
|---|---|
| `overflow` | in-flow content extends past the widget's border-box |
| `unpainted` | geometry authored, no pixel painted |
| `unloaded` | `<img>` never decoded |
| `zero_size` | drawing surface (svg/canvas/recharts) with no area |

Only the outermost element accountable for an overflowing side is reported: a
descendant crosses the edge because its ancestor does, and saying so once is
the difference between 441 notes and 111 over one collection.

Skipped: subtrees hidden via `display:none` / `visibility:hidden` /
`opacity:0` (ancestor-aware); for `overflow` also absolute/fixed subtrees,
`<svg>` interiors, and negative-margin subtrees.

## Determinism

Pixels depend on Chromium, the installed fonts, and the chart library versions.
All three are fixed in the image, fonts included — the base image ships ~50, a
workstation ~950, and the difference moves 0.35% of the pixels of a text-heavy
widget.

Each container renders four canaries (DOM, recharts, SVG, font stacks)
and compares SHA-256 against checksums baked into the image. Mismatch → the
container refuses to serve. The result is cached per machine and image stamp.

- amd64 only.
- Three tags per build: the version (`1.2.0`), the commit it was built from, and
  `latest`. Pin the version or the digest for repeatable work; `latest` moves.
- Building locally bakes *your* machine's fonts, which is a different baseline.
  Regenerate checksums with `python docker/selfcheck.py --make-golden`.

## Performance

8 workers, shared machine at load ~1000:

| | |
|---|---|
| Restart | ~5 s (self-check cached) |
| First start per machine | ~1 min (self-check + warm-up) |
| Single render | 0.4–0.65 s |
| Throughput | ~18/s, flat from 32 to 256 concurrent |
| 256 concurrent | all succeed, p50 7.4 s |

## API

```python
render_source(source, output_path, *, name="widget.jsx", width=None, height=None,
              wait_extra_ms=200, force_resize=True)
render(jsx_path, output_path=None, ...)      # same, reads the file locally
make_renderer(n_workers=4)                   # RenderClient if a daemon is up,
                                             # else an in-process RenderService
```

| parameter | default | meaning |
|---|---|---|
| `width` / `height` | 1920×1080 | viewport; the widget renders at its own declared size |
| `wait_extra_ms` | 200 | pause after the page settles |
| `force_resize` | True | repaint recharts before the screenshot |
| `W2C_RENDER_RUNTIME_DIR` | `/tmp/w2c-render` | socket location |
| `W2C_RENDER_WORKERS` | 8 | page pool size (~50–100 MB each) |
| `W2C_RENDER_MODE` | `m1` | default contract: `m1` (no imports, globals) or `m2` (imports, none) |
| `W2C_RENDER_STALL_TIMEOUT` | 500 | seconds of outstanding work with nothing completing before the daemon is restarted |

`render_result.py`, `render_ipc.py` and `render_client.py` are standard library
only, and they ship inside the image, so a machine that can pull it needs no
checkout and nothing installed:

```bash
docker run --rm --entrypoint tar houstonzhang/w2c-render:latest \
  -cf - -C /opt/w2c w2c_render | tar -xf -
python -c "from w2c_render import RenderClient"
```

`RenderService`, the half that needs Playwright, is imported lazily, so a process
that only talks to the daemon never pays for it.

## Protocol

One JSON line each way over the socket:

```python
import base64, json, socket

req = {"v": 4, "name": "w.jsx", "width": None, "height": None,
       "wait_extra_ms": 200, "force_resize": True,
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

Reply fields:

```jsonc
{
  "v": 4,
  "ok": true,
  "image":  {"png_b64": "…", "width": 300, "height": 150},   // null when ok is false
  "error":  null,                                            // {kind, text} when ok is false
  "layout": [ /* render notes */ ],                          // null when ok is false
  "feedback_text": "…",                                      // the only text a model is shown
  "log": {"settled": true, "settle_ms": 227, "console": [],
          "unclassified": [], "source_policy": {…}}
}
```

Nothing in a reply names the daemon's temporary directory, its dev server, or
Vite's cache-busting nonce: the nonce differs on every call, so the same defect
would never group with itself in a collection.

`RenderClient` adds: waiting through a daemon restart, the 64 MB stream limit on
both ends (asyncio's default 64 KB fails on large widgets only), and decoding.

## Widget contract

```jsx
export default function Widget() { return (...) }
```

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

Renders at the size the code declares; overflow is clipped at the edge.

Beyond the two named contracts, a daemon can be given an allowlist of its own — exact
names or a package-subpath pattern, and only packages baked into the image, since
allowing one that is not installed does not install it:

```bash
# permits e.g. import { LuSearch } from 'react-icons/lu'
W2C_RENDER_ALLOWED_IMPORTS='react-icons/*' docker/run.sh 8
```

Such a policy is named `custom` and is the daemon's default for requests that name no
mode; `--mode` and an allowlist are mutually exclusive, because a mode is a whole
contract and half of one assembled from parts is not a contract at all.

Static imports and re-exports outside the allowlist return `error_kind=policy` before a browser
attempt. Relative/absolute imports are always forbidden. Dynamic `import()` is separately disabled;
it can be enabled with `W2C_RENDER_ALLOW_DYNAMIC_IMPORTS=1`, but still requires an allowlisted literal
package. The configuration a render actually used is returned as `result.source_policy`; the
daemon's own default is written to `/tmp/w2c-render/source_policy.json` and included in the
heartbeat, and is a default rather than a guarantee now that a request may name its own mode.
Pin the image digest and `source_policy.policy_id` for a reproducible experiment.

## Without Docker

```bash
pip install -r requirements.txt && playwright install chromium
cd renderer && npm ci && cd ..
python w2c_render/supervisor.py --workers 8
```

Determinism is then whatever the host provides.

## Layout

```
w2c_render/render.py          Vite + Playwright pool, self-repairing
w2c_render/render_result.py   result contract          (stdlib)
w2c_render/source_policy.py   startup-frozen import contract (stdlib)
w2c_render/syntax.py          localized JSX syntax diagnosis (stdlib + frozen esbuild)
w2c_render/render_ipc.py      wire format              (stdlib)
w2c_render/render_client.py   client, make_renderer    (stdlib)
w2c_render/render_daemon.py   socket server + heartbeat
w2c_render/supervisor.py      restarts a wedged daemon
renderer/                     Vite + React project
renderer/{audit,settle,resize}.js   programs run inside the page
renderer/syntax_check.mjs     syntax-only esbuild bridge
docker/                       image, canaries, checksums, build/run/publish
tests/                        contract, wire, fault injection, workload shapes
```

The supervisor restarts the daemon when work has been outstanding, with nothing
completing, since before the stall window. Measuring from when the work arrived
is the whole of it: "outstanding and nothing completed for ten minutes" sounds
like the same test, but after an idle spell the last completion is already ten
minutes old, so the first request to arrive satisfies it and a healthy daemon is
killed while serving that request — four times in thirty-three hours before this
was fixed, each surfacing to a caller as a dropped connection. `heartbeat.json`
carries `oldest_in_flight_at` for it. The kill is SIGKILL, which also reaps
leaked Chromium processes.

A stuck render therefore holds callers for at most `W2C_RENDER_STALL_TIMEOUT`
seconds, and clients are expected to wait rather than fail: a render spanning a
restart is slow, not failed.

## Tests

```bash
pytest tests/test_render_contract.py tests/test_render_service_ipc.py   # no browser
python tests/simulate_render_faults.py       # real Vite + Chromium, faults injected
python tests/simulate_render_workloads.py    # batch, multi-turn, rollout burst
```

Faults covered: source-policy/syntax rejection, browser killed mid-render, Vite killed, a widget that never
yields its main thread, 9-way concurrency losing the browser, daemon killed
mid-render.

## License

MIT
