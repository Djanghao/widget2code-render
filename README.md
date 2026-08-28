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
    result.ok, result.png_path, result.render_notes, result.has_overflow
```

`/tmp/w2c-render` (socket + heartbeat) is the only mount. Source and screenshot
travel over the socket, so output paths need not be visible to the container.

## Results

| | |
|---|---|
| `ok=True` | PNG written and signature-verified, plus `render_notes` |
| `error_kind="runtime"` | the component threw |
| `error_kind="empty"` | no DOM element, or zero size |
| `error_kind="hang"` | never became ready while a canary rendered normally |

Dead browser, lost dev server, bare timeout, truncated screenshot: retried and
repaired internally, never returned. A client that cannot reach the daemon waits
instead of failing. Requests beyond the pool size queue; they do not fail.

### render_notes

The screenshot is clipped to the widget's box, so some defects are invisible in
it. After the page settles, one DOM pass measures them:

| kind | criterion |
|---|---|
| `overflow` | in-flow content extends past the widget's border-box |
| `unpainted` | geometry authored, no pixel painted |
| `unloaded` | `<img>` never decoded |
| `zero_size` | drawing surface (svg/canvas/echarts/recharts) with no area |

Skipped: subtrees hidden via `display:none` / `visibility:hidden` /
`opacity:0` (ancestor-aware); for `overflow` also absolute/fixed subtrees,
`<svg>` interiors, and negative-margin subtrees.

## Determinism

Pixels depend on Chromium, the installed fonts, and the chart library versions.
All three are fixed in the image, fonts included — the base image ships ~50, a
workstation ~950, and the difference moves 0.35% of the pixels of a text-heavy
widget.

Each container renders five canaries (DOM, echarts, recharts, SVG, font stacks)
and compares SHA-256 against checksums baked into the image. Mismatch → the
container refuses to serve. The result is cached per machine and image stamp.

- amd64 only.
- Pin a tag or digest for repeatable work; `latest` moves on rebuild.
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
| `force_resize` | True | repaint echarts/recharts before the screenshot |
| `W2C_RENDER_RUNTIME_DIR` | `/tmp/w2c-render` | socket location |
| `W2C_RENDER_WORKERS` | 8 | page pool size (~50–100 MB each) |

`render_result.py`, `render_ipc.py` and `render_client.py` are standard library
only — copy them into another project and no dependencies are needed.

## Protocol

One JSON line each way over the socket:

```python
import base64, json, socket

req = {"v": 3, "name": "w.jsx", "width": None, "height": None,
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
if reply.get("png_b64"):
    open("out.png", "wb").write(base64.b64decode(reply["png_b64"]))
else:
    print(reply["error_kind"], reply["error"])
```

Reply fields: `error`, `error_kind`, `console_errors`, `render_notes`,
`settled`, `settle_ms`, `has_overflow`, `overflow_warning`, `source_policy`, `png_b64`.

`RenderClient` adds: waiting through a daemon restart, the 64 MB stream limit on
both ends (asyncio's default 64 KB fails on large widgets only), and decoding.

## Widget contract

```jsx
export default function Widget() { return (...) }
```

The default source policy allows no imports — `React`, `ReactECharts`, `echarts`, and `Recharts`
are on `window`. Renders at the size the code declares; overflow is clipped at the edge.

Import capability is frozen when the daemon starts, not selected per request. Enable only packages
baked into the image, using exact names or a package-subpath pattern:

```bash
# default: historical no-import profile
docker/run.sh 8

# permits e.g. import { LuSearch } from 'react-icons/lu'
W2C_RENDER_ALLOWED_IMPORTS='react-icons/*' docker/run.sh 8
```

Static imports and re-exports outside the allowlist return `error_kind=policy` before a browser
attempt. Relative/absolute imports are always forbidden. Dynamic `import()` is separately disabled;
it can be enabled with `W2C_RENDER_ALLOW_DYNAMIC_IMPORTS=1`, but still requires an allowlisted literal
package. The effective versioned configuration is returned as `result.source_policy`, written to
`/tmp/w2c-render/source_policy.json`, and included in the heartbeat. Pin both image digest and
`source_policy.policy_id` for a reproducible experiment. Allowlisting a package not installed in the
frozen image does not install it.

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

The supervisor restarts the daemon when requests are outstanding *and* nothing
has completed — "nothing completing" alone also describes an idle service. It
uses SIGKILL, which also reaps leaked Chromium processes.

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
