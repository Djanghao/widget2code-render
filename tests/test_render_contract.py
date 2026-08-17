"""`render()` returns a picture or a defect of the file, and nothing else.

A caller that receives a timeout can do nothing with it but retry, and a
teacher that receives one is asked to repair a screenshot — which cost 80
episodes of the 1816-episode run. So the retry lives in the service, and with
it the repair that makes retrying worth anything: a browser that has died fails
every attempt identically, and retrying without rebuilding it hangs forever.

These tests script `_render_once` instead of driving a real browser, so they
state the contract rather than the renderer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from w2c_render.render import OVERFLOW_WARNING_TEXT, RenderResult, RenderService


def ok(jsx: Path, png: Path) -> RenderResult:
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    return RenderResult(jsx, png)


def defect(jsx: Path, png: Path, kind: str = "runtime") -> RenderResult:
    return RenderResult(jsx, png, error=f"{kind} boom", error_kind=kind)


def infra(jsx: Path, png: Path, kind: str = "infra") -> RenderResult:
    return RenderResult(jsx, png, error=f"{kind} noise", error_kind=kind)


class ScriptedService(RenderService):
    """A service whose attempts are a list and whose repairs are counted."""

    def __init__(self, script, canary=True):
        super().__init__(n_workers=1)
        self.script = list(script)
        self.attempts = 0
        self.repairs = 0
        self.canary = canary
        self._recovery_lock = asyncio.Lock()

    async def _render_once(self, jsx_path, output_path=None, *args, **kwargs):
        jsx, png = Path(jsx_path), Path(output_path)
        step = self.script[min(self.attempts, len(self.script) - 1)]
        self.attempts += 1
        return step(jsx, png)

    async def _recover_infrastructure(self, generation, *, reason, cycle):
        self.repairs += 1
        self._generation += 1

    async def _canary_renders(self):
        return self.canary


def run(service, tmp_path) -> RenderResult:
    jsx = tmp_path / "w.jsx"
    jsx.write_text("export default function Widget(){return null}")
    return asyncio.run(service.render(jsx, tmp_path / "w.png"))


def test_a_clean_render_is_returned_as_is(tmp_path):
    service = ScriptedService([ok])
    assert run(service, tmp_path).ok
    assert service.repairs == 0


@pytest.mark.parametrize("kind", ["runtime", "empty"])
def test_a_defect_of_the_file_is_returned_without_retrying(tmp_path, kind):
    """These reproduce on every attempt and are the feedback the model acts on."""
    service = ScriptedService([lambda j, p: defect(j, p, kind)])
    result = run(service, tmp_path)
    assert result.is_widget_defect and result.error_kind == kind
    assert service.attempts == 1, "retrying a deterministic defect wastes a render"
    assert service.repairs == 0


def test_infrastructure_noise_never_reaches_the_caller(tmp_path):
    """Two infra failures then a picture: the caller sees only the picture."""
    service = ScriptedService([infra, infra, ok])
    result = run(service, tmp_path)
    assert result.ok
    assert result.error_kind is None
    assert service.repairs == 1, "the second failure triggers one repair"


def test_a_dead_renderer_is_rebuilt_rather_than_retried_forever(tmp_path):
    """Twenty identical failures — retrying alone would never terminate."""
    script = [infra] * 20 + [ok]
    service = ScriptedService(script)
    assert run(service, tmp_path).ok
    assert service.repairs >= 5, "each pair of failures must rebuild the pool"


def test_a_timeout_becomes_a_defect_only_once_a_canary_has_rendered(tmp_path):
    """With the renderer proven healthy, nothing but this file explains why it
    never became ready — so the timeout is the file's own defect."""
    service = ScriptedService([lambda j, p: infra(j, p, "timeout")], canary=True)
    result = run(service, tmp_path)
    assert result.error_kind == "hang"
    assert result.is_widget_defect
    assert "canary widget rendered normally" in result.error


def test_a_timeout_on_a_sick_renderer_keeps_being_repaired(tmp_path):
    """The same symptom with the canary failing is the renderer, not the file."""
    script = [lambda j, p: infra(j, p, "timeout")] * 6 + [ok]
    service = ScriptedService(script, canary=False)
    result = run(service, tmp_path)
    assert result.ok, "a sick renderer must be repaired, not blamed on the widget"
    assert service.repairs >= 3


def test_a_corrupt_screenshot_is_this_process_failing_not_the_widget(tmp_path):
    """`ok` with an unreadable PNG would enter the dataset as a real render."""
    def truncated(jsx: Path, png: Path) -> RenderResult:
        png.write_bytes(b"not a png")
        return RenderResult(jsx, png)

    service = ScriptedService([truncated, truncated, ok])
    result = run(service, tmp_path)
    assert result.ok
    assert result.png_path.read_bytes().startswith(b"\x89PNG")
    assert service.repairs == 1


def test_every_outcome_is_one_of_the_two(tmp_path):
    """The whole contract in one assertion, over every scripted prefix."""
    from w2c_render.render import WIDGET_DEFECT_ERROR_KINDS

    scripts = [
        [ok],
        [lambda j, p: defect(j, p, "runtime")],
        [lambda j, p: defect(j, p, "empty")],
        [infra, ok],
        [infra, infra, infra, ok],
        [lambda j, p: infra(j, p, "timeout")],
    ]
    for script in scripts:
        result = run(ScriptedService(script), tmp_path)
        assert result.ok or result.error_kind in WIDGET_DEFECT_ERROR_KINDS, result.error_kind


def test_a_success_carries_everything_the_caller_reads_from_it(tmp_path):
    """The retry wrapper must be transparent. `render_notes` is the only source
    of facts the screenshot cannot show — overflow, unpainted, unloaded,
    zero_size — and `settled` says whether they describe a finished page.
    Dropping any of them would silently empty the model's feedback.
    """
    notes = [
        {"kind": "overflow", "side": "bottom", "amount": 76, "tag": "div"},
        {"kind": "unloaded", "src": "logo.png"},
    ]

    def rich(jsx: Path, png: Path) -> RenderResult:
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
        return RenderResult(
            jsx, png,
            render_notes=notes,
            settled=True,
            settle_ms=265,
            console_errors=["console.error: noisy but harmless"],
        )

    # Also survives a repair: the successful attempt is returned untouched.
    result = run(ScriptedService([infra, infra, rich]), tmp_path)
    assert result.ok
    assert result.render_notes == notes
    assert result.has_overflow is True, "derived from the overflow note"
    assert result.overflow_warning == OVERFLOW_WARNING_TEXT
    assert result.settled is True and result.settle_ms == 265
    assert result.console_errors == ["console.error: noisy but harmless"]


def test_a_defect_carries_its_message_and_the_console_that_explains_it(tmp_path):
    service = ScriptedService([
        lambda j, p: RenderResult(
            j, p, error="ReferenceError: BedIcon is not defined",
            error_kind="runtime", console_errors=["pageerror: ReferenceError: BedIcon"],
        )
    ])
    result = run(service, tmp_path)
    assert result.error == "ReferenceError: BedIcon is not defined"
    assert result.console_errors == ["pageerror: ReferenceError: BedIcon"]


def test_the_canary_does_not_shorten_renders_running_beside_it(tmp_path):
    """The canary needs a short readiness budget. Taking it from the shared
    attribute would have handed that budget to every render in flight."""
    seen: list[int | None] = []

    class Recorder(ScriptedService):
        async def _render_once(self, jsx_path, output_path=None, *args, timeout_ms=None, **kw):
            seen.append(timeout_ms)
            return await super()._render_once(jsx_path, output_path)

        async def _canary_renders(self):
            await self._render_once(tmp_path / "c.jsx", tmp_path / "c.png", timeout_ms=15_000)
            return True

    service = Recorder([lambda j, p: infra(j, p, "timeout")])
    service.default_timeout_ms = 30_000
    run(service, tmp_path)
    assert seen[0] is None, "the real render keeps the service default"
    assert 15_000 in seen, "the canary passes its own budget instead of mutating state"
    assert service.default_timeout_ms == 30_000, "shared state is untouched"


def test_liveness_is_answering_http_not_holding_a_socket(monkeypatch):
    """Three ways the old TCP-connect probe stalled a collection for good.

    A 0.2s connect reports a healthy Vite as absent whenever the machine is
    loaded — and `--strictPort` then refuses to start the replacement, so every
    render and every repair after it fails while zombie Vites pile up. A socket
    that is bound but serves nothing is the same mistake inverted: it reads as a
    server worth reusing, and nothing restarts the real one. And a Vite this
    service did not start used to be exempt from repair entirely.
    """
    import w2c_render.render as render_module

    assert not hasattr(render_module, "_port_in_use"), "connect-probing is the bug"
    assert not hasattr(render_module.RenderService, "_vite_alive"), (
        "liveness cannot depend on owning the process"
    )

    calls: list[str] = []
    monkeypatch.setattr(render_module, "_vite_answers", lambda *a, **k: calls.append("http") or True)
    service = RenderService(n_workers=1)
    service._owns_vite = False
    asyncio.run(service._start_vite_if_needed())
    assert calls == ["http"], "a served port is reused without touching the process"


def test_a_vite_that_answers_nothing_is_replaced_even_if_we_did_not_start_it(monkeypatch):
    import w2c_render.render as render_module

    started: list[bool] = []
    monkeypatch.setattr(render_module, "_vite_answers", lambda *a, **k: bool(started))
    monkeypatch.setattr(render_module, "_port_bound", lambda *a, **k: False)

    class FakeProc:
        returncode = None

        def poll(self):
            started.append(True)  # answers from the next probe on
            return None

    monkeypatch.setattr(render_module.subprocess, "Popen", lambda *a, **k: FakeProc())
    service = RenderService(n_workers=1)
    service._owns_vite = False  # someone else's server, and it is dead
    asyncio.run(service._start_vite_if_needed())
    assert service._owns_vite is True, "a dead server is replaced and adopted"


def test_the_renderer_never_gives_up(tmp_path):
    """Unbounded repair is a decision, not an oversight.

    A bounded renderer has to answer something once its bound is reached, and
    every answer contaminates the data: infrastructure noise returned as the
    widget's fault, or an episode quietly dropped. Either way the renderer is
    deciding what enters the dataset. Stalling loudly is the one failure that
    costs time instead of data.

    A bound belongs to whatever owns the collection window — and it must stop
    the window rather than answer a render.
    """
    import inspect

    import w2c_render.render as render_module

    source = inspect.getsource(render_module)
    assert "MAX_RECOVERY_CYCLES" not in source
    assert "recovery exhausted" not in source
    # Fifty consecutive failures then a picture: the caller still gets the
    # picture, and no exception on the way.
    service = ScriptedService([infra] * 50 + [ok])
    assert run(service, tmp_path).ok
    assert service.repairs >= 20
