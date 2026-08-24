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
from w2c_render.source_policy import SourcePolicy


def ok(jsx: Path, png: Path) -> RenderResult:
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)
    return RenderResult(jsx, png)


def defect(jsx: Path, png: Path, kind: str = "runtime") -> RenderResult:
    return RenderResult(jsx, png, error=f"{kind} boom", error_kind=kind)


def infra(jsx: Path, png: Path, kind: str = "infra") -> RenderResult:
    return RenderResult(jsx, png, error=f"{kind} noise", error_kind=kind)


class ScriptedService(RenderService):
    """A service whose attempts are a list and whose repairs are counted."""

    def __init__(self, script):
        super().__init__(n_workers=1)
        self.script = list(script)
        self.attempts = 0
        self.repairs = 0
        self._recovery_lock = asyncio.Lock()

    async def _render_once(self, jsx_path, output_path=None, *args, **kwargs):
        jsx, png = Path(jsx_path), Path(output_path)
        step = self.script[min(self.attempts, len(self.script) - 1)]
        self.attempts += 1
        return step(jsx, png)

    async def _recover_infrastructure(self, generation, *, reason, cycle):
        self.repairs += 1
        self._generation += 1


def run(service, tmp_path) -> RenderResult:
    jsx = tmp_path / "w.jsx"
    jsx.write_text("export default function Widget(){return null}")
    return asyncio.run(service.render(jsx, tmp_path / "w.png"))


def test_a_clean_render_is_returned_as_is(tmp_path):
    service = ScriptedService([ok])
    assert run(service, tmp_path).ok
    assert service.repairs == 0


def test_source_policy_rejects_an_import_before_a_browser_attempt(tmp_path):
    service = ScriptedService([ok])
    jsx = tmp_path / "w.jsx"
    jsx.write_text("import React from 'react'; export default function Widget(){}")
    result = asyncio.run(service.render(jsx, tmp_path / "w.png"))
    assert result.error_kind == "policy" and result.is_widget_defect
    assert service.attempts == 0
    assert result.source_policy["allowed_imports"] == []


def test_source_policy_can_enable_a_frozen_package_family(tmp_path):
    service = ScriptedService([ok])
    service.source_policy = SourcePolicy(("react-icons/*",))
    jsx = tmp_path / "w.jsx"
    jsx.write_text(
        "import { LuSearch } from 'react-icons/lu'; "
        "export default function Widget(){return <LuSearch/>}"
    )
    result = asyncio.run(service.render(jsx, tmp_path / "w.png"))
    assert result.ok and service.attempts == 1
    assert result.source_policy["allowed_imports"] == ["react-icons/*"]


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
    """Ten identical failures then a picture — retrying alone never rebuilds."""
    service = ScriptedService([infra] * 10 + [ok])
    assert run(service, tmp_path).ok
    assert service.repairs >= 5, "each pair of failures must rebuild the pool"


def test_a_hung_widget_is_convicted_by_its_own_page(tmp_path):
    """The page that cannot run `1` is the evidence, and the only evidence.

    The renderer used to license this verdict with a canary — a 64x64 square
    that renders perfectly well on a renderer too busy to finish a real widget.
    Fired 1,822 renders at an eight-page pool, that reasoning blamed 219
    widgets for a contention they had nothing to do with; every one of them
    rendered when the pool was not oversubscribed.
    """
    from w2c_render.render import RenderService

    class Blocked(RenderService):
        async def _page_responds(self, page, timeout=10.0):
            return False

    service = Blocked(n_workers=1)
    result = service._failure(Path("w.jsx"), Path("w.png"),
                              TimeoutError("Timeout 30000ms exceeded"), [], page_blocked=True)
    assert result.error_kind == "hang" and result.is_widget_defect
    assert "never yielded its main thread" in result.error


def test_a_timeout_on_a_responsive_page_is_the_renderers_problem(tmp_path):
    """Slow is not hung, and only the renderer can be blamed for slow."""
    from w2c_render.render import RenderService

    result = RenderService._failure(Path("w.jsx"), Path("w.png"),
                                    TimeoutError("Timeout 30000ms exceeded"), [],
                                    page_blocked=False)
    assert result.error_kind == "timeout"
    assert not result.is_widget_defect, "contention must never reach the caller"


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


def test_every_outcome_is_one_of_the_three(tmp_path, monkeypatch):
    """The whole contract in one assertion, over every scripted prefix.

    A picture, a defect of the widget, or — when the renderer has run out of
    explanations — `unknown`. The last is not a fourth kind of answer so much
    as the refusal to give none: an endless timeout used to be promoted to
    `hang` on a canary's word, which named the widget for the renderer's
    problem.
    """
    import w2c_render.render as render_module
    from w2c_render.render import WIDGET_DEFECT_ERROR_KINDS

    monkeypatch.setattr(render_module, "SEVERE_RENDER_TIMEOUT_S", 1.0)
    scripts = [
        [ok],
        [lambda j, p: defect(j, p, "runtime")],
        [lambda j, p: defect(j, p, "empty")],
        [lambda j, p: defect(j, p, "syntax")],
        [infra, ok],
        [infra, infra, infra, ok],
        [lambda j, p: infra(j, p, "timeout")],
    ]
    for script in scripts:
        result = run(ScriptedService(script), tmp_path)
        assert result.ok or result.error_kind in WIDGET_DEFECT_ERROR_KINDS \
            or result.error_kind == "unknown", result.error_kind


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


def test_the_renderer_answers_even_when_it_cannot_explain_itself(tmp_path, monkeypatch):
    """Three outcomes and no fourth — silence is not one of them.

    Repairing forever was the earlier answer, on the reasoning that any reply
    to an unexplained failure contaminates the data. But a call that never
    returns contaminates it too, and takes the collection with it: nothing
    downstream can tell a stalled renderer from a slow one. So the wall clock
    bounds it, and what comes back names what happened under a kind no caller
    can mistake for a defect of the widget.
    """
    import w2c_render.render as render_module
    from w2c_render.render import WIDGET_DEFECT_ERROR_KINDS

    monkeypatch.setattr(render_module, "SEVERE_RENDER_TIMEOUT_S", 0.0)
    service = ScriptedService([infra] * 500)
    result = run(service, tmp_path)
    assert not result.ok
    assert result.error_kind == "unknown"
    assert result.error_kind not in WIDGET_DEFECT_ERROR_KINDS, (
        "the renderer's own failure must never read as the widget's"
    )
    assert "cannot say why" in result.error


def test_a_renderer_that_recovers_late_still_returns_the_picture(tmp_path):
    """Giving up is a last resort, not an early one."""
    service = ScriptedService([infra] * 8 + [ok])
    assert run(service, tmp_path).ok


class FakeConsoleMessage:
    """Playwright's console message, reduced to what ownership is decided on."""

    def __init__(self, text, url=""):
        self.type = "error"
        self.text = text
        self.location = {"url": url}


def test_a_console_error_naming_another_widget_is_not_ours():
    """One Vite server serves the whole pool, so a failure belonging to another
    render can surface on this page. The message that does it most often names
    no file in its text — only in its location."""
    from w2c_render.render import _belongs_to

    foreign_500 = FakeConsoleMessage(
        "Failed to load resource: the server responded with a status of 500 (Internal Server Error)",
        url="http://127.0.0.1:5173/@fs/tmp/w2c-render-abc/broken.jsx?v=1",
    )
    assert not _belongs_to(foreign_500, "mine.jsx"), (
        "a resource error for another widget was recorded against this one"
    )
    assert _belongs_to(FakeConsoleMessage("boom", url="http://127.0.0.1:5173/@fs/x/mine.jsx"), "mine.jsx")


def test_an_error_that_names_no_widget_is_kept():
    """On this page it can only be ours, and dropping it would empty the
    diagnostics for every runtime failure that mentions no filename."""
    from w2c_render.render import _belongs_to

    assert _belongs_to(FakeConsoleMessage("ReferenceError: X is not defined"), "mine.jsx")


def test_the_old_rule_kept_foreign_errors_that_never_said_vite():
    """The previous rule kept anything without the word "vite" in it, which is
    every foreign resource error. Pinned so it does not come back."""
    import inspect

    import w2c_render.render as render_module

    source = inspect.getsource(render_module)
    assert '"vite" not in text.lower()' not in source


def test_every_kind_the_render_path_produces_is_classified():
    """A kind in neither set is treated as infrastructure and repaired forever.

    `syntax` was missing from WIDGET_DEFECT_ERROR_KINDS for one build, and the
    daemon rebuilt its browser pool 18 times over a widget with an unterminated
    string literal — the widget was never rendered and the caller never
    answered.
    """
    import re

    import w2c_render.render as render_module
    from w2c_render.render import WIDGET_DEFECT_ERROR_KINDS

    source = inspect_source(render_module)
    produced = set(re.findall(r'error_kind=["\'](\w+)["\']', source))
    produced |= set(re.findall(r'kind = ["\'](\w+)["\']', source))
    produced |= set(re.findall(r'["\'](\w+)["\'], "(?:infra|timeout)"', source))
    from w2c_render.render_result import RENDERER_FAILURE_ERROR_KINDS
    infrastructure = {"infra", "timeout"} | set(RENDERER_FAILURE_ERROR_KINDS)
    unclassified = produced - set(WIDGET_DEFECT_ERROR_KINDS) - infrastructure
    assert not unclassified, f"unclassified error kinds: {sorted(unclassified)}"


def test_a_syntax_error_is_the_widgets_own_defect():
    """It reproduces on every attempt, so retrying it is a loop."""
    from w2c_render.render import RenderResult

    result = RenderResult(Path("w.jsx"), Path("w.png"),
                          error='SyntaxError at line 2:0 — Expected ">"', error_kind="syntax")
    assert result.is_widget_defect


def inspect_source(module):
    import inspect
    return inspect.getsource(module)
