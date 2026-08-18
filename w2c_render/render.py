"""Long-lived widget render service: Vite dev server + Playwright browser pool.

**This is a long-lived backend service.** Enter ONCE per process via
`async with RenderService(...) as svc:` and share `svc` across every caller —
re-entering the context re-spawns Vite and re-launches Chromium, which costs
seconds each time. For the cross-process variant (one renderer shared by many
programs, with an external supervisor) see `render_daemon.py` /
`render_client.py`.

    async with RenderService(n_workers=8) as svc:
        result = await svc.render(jsx_path)        # single file
        results = await svc.render_dir(jsx_dir)    # every .jsx in a folder

`render()` has exactly two outcomes: a PNG on disk, or a defect of the file
(`runtime` / `empty` / `hang`). Failures of the renderer itself — a dead
browser, a lost Vite, a bare timeout — are repaired and retried internally,
without bound, and never surface to a caller. The rationale, the failure
taxonomy and the invariants this file must hold are documented in
README.md; comments here mark where each one is enforced.

The browser-side programs (settle wait, render audit, chart repaint) live next
to the Vite project in renderer/{settle,audit,resize}.js.
"""
from __future__ import annotations

import asyncio
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Union
from urllib.parse import quote
from urllib.request import urlopen

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# Re-exported so `from w2c_render.render import RenderResult, ...` keeps
# working; the definitions live in render_result.py, which is stdlib-only so
# daemon clients in other repos can import it without playwright.
from .render_result import (  # noqa: F401
    OVERFLOW_WARNING_TEXT,
    WIDGET_DEFECT_ERROR_KINDS,
    RenderResult,
)

PathLike = Union[str, Path]

# renderer/ holds the Vite project that backs this service.
RENDERER_DIR = Path(__file__).resolve().parents[1] / "renderer"

# Two processes must not share one port: when the owner exits it takes the Vite
# server with it and every later render in the survivor fails. Give each
# process its own W2C_VITE_PORT — or better, share one render daemon.
VITE_PORT = int(os.environ.get("W2C_VITE_PORT", 5173))
VITE_HOST = "127.0.0.1"
VITE_BASE = f"http://{VITE_HOST}:{VITE_PORT}"
# Generous on purpose: this machine has started Vite in ~2s idle and >30s at
# load 500. A slow start is not a broken start.
VITE_START_TIMEOUT_S = float(os.environ.get("W2C_VITE_START_TIMEOUT", 300))

# Attempts on one file before the renderer itself is repaired.
RENDER_ATTEMPTS_BEFORE_RECOVERY = 2
RECOVERY_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
# Loud enough to notice in a log, rare enough not to spam one.
RECOVERY_ALARM_AFTER = 5
# The clock starts when the renderer starts failing, not when the caller starts
# waiting. Queueing is not a failure: 1,822 requests against an eight-page pool
# leave the last one waiting almost two minutes by arithmetic alone, and
# counting that as "unrenderable" turned 217 healthy widgets into
# `UnknownRenderFailure`, every one of them "across 0 rebuilds". So this bounds
# the repair regime — how long the renderer may keep rebuilding itself over one
# file before admitting it cannot say why. What comes back then is `unknown` — not the widget's fault,
# not silence, but the renderer saying it cannot explain itself, so the failure
# is discovered instead of waited on. An earlier version never gave up, on the
# reasoning that any answer contaminates the data; a call that never returns
# contaminates it too, silently, and takes the collection with it.
SEVERE_RENDER_TIMEOUT_S = float(os.environ.get("W2C_SEVERE_RENDER_TIMEOUT", 120))
# esbuild is fast; a diagnosis that takes longer than this is not worth the
# render it is holding up.
SYNTAX_DIAGNOSIS_TIMEOUT_S = 20
# Nothing on the render path may wait forever (README.md, invariant
# ③): a pool that lost its pages is only restored by the repair, and the
# repair is only reached by returning a failure.
PAGE_ACQUIRE_TIMEOUT_S = 60.0
# Room for settle, audit and screenshot on top of the readiness budget, after
# which the attempt is abandoned rather than waited on.
ATTEMPT_DEADLINE_SLACK_S = 30.0

# Substrings that identify a dead browser, context, page, or Vite server.
_INFRA_ERROR_MARKERS = (
    "err_connection_refused",
    "err_connection_closed",
    "err_connection_reset",
    "err_empty_response",
    "target page, context or browser has been closed",
    "browser has been closed",
    "page has been closed",
    "browser closed",
    "execution context was destroyed",
)

# Advice React appends to a few messages that cannot apply here: widget files
# have no imports and no exports beyond the default component.
_INAPPLICABLE_ADVICE = (
    " You likely forgot to export your component from the file it's defined in,"
    " or you might have mixed up default and named imports.",
)


# Animation is the one thing a screenshot cannot be honest about: the frame it
# catches is whichever the clock allowed, so the same widget yields different
# pixels on a loaded machine than on an idle one. Measured on a CSS transition:
# four renders, two distinct images.
#
# So time is taken away from the page before it is measured. Durations collapse
# and delays are pulled into the past, which lands finite animations and
# transitions on their final keyframe immediately; `iteration-count: 1` gives an
# infinite animation an end to land on as well. The alternative — pausing —
# freezes an arbitrary frame, which is the nondeterminism, not the cure.
#
# `fill-mode: forwards` is what makes it the *finished* state rather than the
# unstarted one. A CSS animation reverts to the element's base style when it
# ends, so a bar that grows from 0 to 180px snaps back to its authored 20px the
# moment it completes — the animation's whole point, undone, and measured as
# 20px in the screenshot.
_FREEZE_ANIMATIONS_CSS = """
*, *::before, *::after {
  animation-delay: -1ms !important;
  animation-duration: 1ms !important;
  animation-iteration-count: 1 !important;
  animation-fill-mode: forwards !important;
  transition-delay: -1ms !important;
  transition-duration: 1ms !important;
}
"""


# Browser-side programs, kept as .js files beside the Vite project so they get
# highlighting and lint. Loaded once at import; each is a single JS expression
# for page.evaluate().
_SETTLE_JS = (RENDERER_DIR / "settle.js").read_text()
_AUDIT_JS = (RENDERER_DIR / "audit.js").read_text()
_RESIZE_JS = (RENDERER_DIR / "resize.js").read_text()


# ---- small helpers ---------------------------------------------------------

_JSX_MENTION = re.compile(r"[\w.-]+\.jsx")


def _belongs_to(msg, jsx_name: str) -> bool:
    """Is this console error about the widget we are rendering?

    One Vite server serves every page in the pool, so a failure belonging to
    another render can surface here — and the message that does it most often
    ("Failed to load resource: … 500") names no file at all in its text. Its
    *location* does, which is why the URL is consulted first.

    Deciding by which file a message names is what the previous rule got
    wrong: it kept anything that did not contain the word "vite", which is
    every foreign resource error. A message that names no widget at all is
    kept, because on this page it can only be ours.
    """
    location = (getattr(msg, "location", None) or {}).get("url", "")
    named = set(_JSX_MENTION.findall(location)) or set(_JSX_MENTION.findall(msg.text))
    return not named or jsx_name in named


def _first_line(text: str) -> str:
    return text.split("\n")[0].strip()


def _normalize_runtime_error(message: str) -> str:
    for advice in _INAPPLICABLE_ADVICE:
        message = message.replace(advice, "")
    return message.strip()


def _first_page_error(console_errors: list[str]) -> Optional[str]:
    """The widget's own uncaught exception, if the page reported one."""
    for entry in console_errors:
        if entry.startswith("pageerror: "):
            return _first_line(entry[len("pageerror: "):])
    return None



# What a syntax error looks like from inside the browser: the module never
# loads, and the page can only say so. The message names a temporary path and
# no cause, which is the least useful thing the renderer produces — 172 of the
# 252 failures in one collection were exactly this string. esbuild knows the
# real answer, so it is asked, but only here: a widget that loaded fine never
# pays for the check.
_MODULE_FETCH_FAILURE = "failed to fetch dynamically imported module"


async def _syntax_diagnosis(jsx: Path) -> Optional[str]:
    """The real reason this file would not load, or None if it parses.

    esbuild is a subprocess, and calling it inline blocks the event loop this
    daemon serves every other render from — long enough, on a loaded machine,
    for the supervisor to read the silence as a wedged process and restart it.
    So it runs in a thread, and a diagnosis that does not arrive quickly is
    dropped rather than waited on: the render already has an answer, this only
    makes it a better one.
    """
    def probe() -> Optional[str]:
        from .syntax import check_syntax, format_syntax_error
        result = check_syntax(jsx, timeout=SYNTAX_DIAGNOSIS_TIMEOUT_S)
        return None if result.ok else format_syntax_error(result)

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(probe), timeout=SYNTAX_DIAGNOSIS_TIMEOUT_S + 5
        )
    except Exception:
        return None                      # never let diagnosis break a render



# React reports one failure through several channels: the boundary, the window
# handler, and a component-stack message whose frames are all renderer
# internals. Repeating the same sentence three times does not make it clearer,
# and every extra line is one a model has to read past to find the fix.
_REACT_STACK_NOTE = "the above error occurred in the"


def _useful_console(entries: list[str], error: Optional[str]) -> list[str]:
    """Console output that says something the result does not already say."""
    kept: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        body = entry.split(": ", 1)[-1].strip()
        if body in seen:
            continue                              # the same throw, again
        seen.add(body)
        if error and body == error.strip():
            continue                              # already the error field
        if _REACT_STACK_NOTE in body.lower():
            continue                              # renderer frames only
        kept.append(entry)
    return kept


def _png_for(jsx_path: PathLike, output_path: Optional[PathLike]) -> Path:
    return Path(output_path) if output_path else Path(jsx_path).with_suffix(".png")


async def _abandon_after(task: asyncio.Task, timeout: float) -> bool:
    """True iff the task finished in time; otherwise cancel it and walk away.

    ``asyncio.wait_for`` awaits the cancellation it requests, and a playwright
    call blocked on a dead driver pipe never completes one. Abandoning the
    task bounds the wait for real.
    """
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except Exception:
        done = set()
    if not task.done():
        task.cancel()
    return bool(done)


async def _bounded(coro, timeout: float) -> None:
    """Await at most ``timeout`` and swallow the outcome (teardown paths)."""
    task = asyncio.ensure_future(coro)
    if await _abandon_after(task, timeout):
        try:
            task.result()
        except BaseException:
            pass


async def _close_quietly(page: Page) -> None:
    await _bounded(page.close(), 5)


async def _dispose(pages, context, browser, driver) -> None:
    """Best-effort teardown of a pool nothing is waiting for any more."""
    for page in pages:
        await _bounded(page.close(), 5)
    if context is not None:
        await _bounded(context.close(), 10)
    if browser is not None:
        await _bounded(browser.close(), 10)
    if driver is not None:
        await _bounded(driver.stop(), 10)


async def _wait_process(proc: subprocess.Popen, timeout: float) -> bool:
    """Poll a child without creating a default-executor thread at shutdown."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if proc.poll() is not None:
            return True
        await asyncio.sleep(0.1)
    return proc.poll() is not None


def _vite_answers(timeout: float = 3.0) -> bool:
    """Does the widget renderer, rather than merely HTTP, answer this port?

    Liveness must be "answers HTTP with our page", not "the port accepts a
    connection" — see README.md for the two failure modes a connect
    probe caused. Synchronous on purpose: it runs only at startup or under the
    recovery lock, and a default-executor thread can outlive asyncio shutdown.
    """
    try:
        with urlopen(VITE_BASE, timeout=timeout) as response:
            body = response.read(16_384).decode("utf-8", errors="replace")
            return response.status == 200 and "<title>Widget Renderer</title>" in body
    except Exception:
        return False


def _port_bound(host: str, port: int, timeout: float = 1.0) -> bool:
    """Is anything holding the port? Used only to wait for a kill to land."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


# ---- service ---------------------------------------------------------------

class RenderService:
    """Vite + Playwright pool. Renders any absolute-path .jsx to PNG."""

    def __init__(
        self,
        n_workers: int = 4,
        default_viewport: tuple[int, int] = (1920, 1080),
        default_timeout_ms: int = 30000,
        settle_budget_ms: int = 3000,
    ):
        self.n_workers = n_workers
        self.default_viewport = default_viewport
        self.default_timeout_ms = default_timeout_ms
        # Upper bound on the post-render quiescence wait; measured p99 is
        # ~265ms, so this only bounds pathological pages.
        self.settle_budget_ms = settle_budget_ms

        self._vite_proc: Optional[subprocess.Popen] = None
        self._owns_vite = False
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        # One queue for the service's lifetime. Recovery replaces its contents,
        # never the object: a caller already blocked in `get()` would otherwise
        # wait on an orphaned queue that nothing ever fills again.
        self._free: asyncio.Queue[Page] = asyncio.Queue()
        # Bumped on every pool rebuild. A worker that failed under generation G
        # and finds the service at G+1 knows someone else repaired it, so
        # concurrent failures cause one recovery rather than one per worker.
        self._generation = 0
        self._recovery_lock: Optional[asyncio.Lock] = None
        self._disposals: set[asyncio.Task] = set()
        self._closed = False

    # ---- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "RenderService":
        self._recovery_lock = asyncio.Lock()
        # Vite and Chromium start concurrently — the browser first needs Vite
        # at its first goto, so cold start costs max(vite, chromium), not sum.
        failure = await self._start_renderer()
        if failure is not None:
            await self.aclose()
            raise failure
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Idempotent shutdown — release every owned resource.

        Each step has its own timeout; failure in one resource does not block
        cleanup of the next.
        """
        if self._closed:
            return
        self._closed = True

        await self._close_browser_pool()
        if self._disposals:
            _, pending = await asyncio.wait(self._disposals, timeout=30)
            for task in pending:
                task.cancel()

        if self._owns_vite and self._vite_proc is not None:
            await self._kill_vite()
            self._vite_proc = None
            self._owns_vite = False

    async def _start_renderer(self) -> Optional[Exception]:
        """Start Vite and the browser pool together; None means both are up.

        Startup only: no caller can hold a page before `__aenter__` returns,
        so filling the pool while Vite is still starting is safe here and
        unsafe in recovery (see `_recover_infrastructure`).
        """
        results = await asyncio.gather(
            self._start_vite_if_needed(),
            self._launch_browser_pool(),
            return_exceptions=True,
        )
        for outcome in results:
            if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                raise outcome  # cancellation and friends propagate
        return next((r for r in results if isinstance(r, Exception)), None)

    # ---- public api --------------------------------------------------------

    async def render(
        self,
        jsx_path: PathLike,
        output_path: Optional[PathLike] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        wait_extra_ms: int = 200,
        force_resize: bool = True,
        freeze_animations: bool = True,
    ) -> RenderResult:
        """Render a .jsx to PNG. Returns a picture or a defect of that file.

        Two outcomes and no third: ``ok`` with a verified PNG on disk, or a
        failure the JSX itself causes (``runtime`` / ``empty`` / ``hang``).
        Infrastructure failures are retried here, and after
        `RENDER_ATTEMPTS_BEFORE_RECOVERY` failures the pool is rebuilt and
        Vite restarted if it stopped answering — retrying without repair would
        hang forever on a browser that has died.

        A timeout is only returned (as ``hang``) once a canary widget has
        rendered on the repaired pool: with the renderer proven healthy,
        nothing but this file explains why it never became ready.

        Three outcomes and no fourth, and each of them arrives: the repair
        loop is bounded by the wall clock (`SEVERE_RENDER_TIMEOUT_S`), and what
        it returns at that bound is `unknown` — not a defect of the file, not
        silence, but a statement that this file did not render in two minutes
        of repairs and the renderer cannot explain why.
        Nothing downstream may show that to a model as the widget's fault —
        it is the renderer admitting it does not know, and it exists so the
        failure is discovered rather than waited on.
        """
        attempts = 0
        recoveries = 0
        repairing_since: Optional[float] = None
        while True:
            generation = self._generation
            result = await self._attempt_within_deadline(
                jsx_path, output_path, width, height, wait_extra_ms, force_resize,
                freeze_animations,
            )
            if result.ok:
                bad_png = self._png_defect(result.png_path)
                if bad_png is None:
                    return result
                # A screenshot that is not a PNG is this process failing, not
                # the widget: take the same repair path.
                result = RenderResult(
                    result.jsx_path, result.png_path,
                    error=f"screenshot is not a usable PNG: {bad_png}",
                    error_kind="infra",
                    console_errors=list(result.console_errors),
                )
            if result.is_widget_defect:
                return result

            attempts += 1
            if attempts < RENDER_ATTEMPTS_BEFORE_RECOVERY:
                await asyncio.sleep(0.5)
                continue

            now = asyncio.get_event_loop().time()
            if repairing_since is None:
                repairing_since = now
            elapsed = now - repairing_since
            if elapsed > SEVERE_RENDER_TIMEOUT_S:
                return RenderResult(
                    Path(jsx_path), _png_for(jsx_path, output_path),
                    error=(
                        f"UnknownRenderFailure: the renderer rebuilt itself "
                        f"{recoveries} times over {elapsed:.0f}s and this file still "
                        f"does not render; it cannot say why. The last thing it knew was "
                        f"{_first_line(result.error or 'nothing')}"
                    ),
                    error_kind="unknown",
                    console_errors=list(result.console_errors),
                )
            recoveries += 1
            await self._recover_infrastructure(
                generation, reason=result.error or "", cycle=recoveries
            )
            attempts = 0

            # A timeout is never promoted to `hang` here. The canary that used
            # to license that promotion is a 64x64 square, and it renders
            # perfectly well on a renderer too busy to finish a real widget:
            # 1,822 renders fired at an eight-page pool produced 219 widgets
            # blamed for a contention their own pages could have denied. The
            # question is answered where it can be answered — on the page
            # itself, in `_render_once` — and a timeout that reaches here is
            # therefore the renderer's, and is retried.

    async def render_dir(
        self,
        jsx_dir: PathLike,
        output_dir: Optional[PathLike] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        wait_extra_ms: int = 200,
        force_resize: bool = True,
    ) -> list[RenderResult]:
        """Render every top-level .jsx in a directory to PNG, concurrently.

        Outputs land in `output_dir` (default: `jsx_dir`) as `<stem>.png`,
        sorted by filename. For per-file parameters, call `render()` in your
        own asyncio.gather().
        """
        src = Path(jsx_dir).resolve()
        out = Path(output_dir).resolve() if output_dir else src
        out.mkdir(parents=True, exist_ok=True)
        files = sorted(src.glob("*.jsx"))
        return await asyncio.gather(*[
            self.render(
                f, output_path=out / f"{f.stem}.png",
                width=width, height=height,
                wait_extra_ms=wait_extra_ms,
                force_resize=force_resize,
            )
            for f in files
        ])

    # ---- one attempt -------------------------------------------------------

    async def _attempt_within_deadline(
        self, jsx_path, output_path, width, height, wait_extra_ms, force_resize,
        freeze_animations=True,
    ) -> RenderResult:
        """One attempt, bounded by a real clock rather than by Playwright.

        Playwright's timeouts run as script inside the page, so a widget that
        never yields its main thread outruns them (README.md,
        invariant ①). The abandoned attempt still holds its page; the bounded
        acquisition and the repair are what restore the pool.
        """
        budget = self.default_timeout_ms / 1000 + ATTEMPT_DEADLINE_SLACK_S
        task = asyncio.ensure_future(
            self._render_once(jsx_path, output_path, width, height, wait_extra_ms,
                              force_resize, freeze_animations=freeze_animations)
        )
        if await _abandon_after(task, budget):
            return task.result()
        return RenderResult(
            Path(jsx_path), _png_for(jsx_path, output_path),
            error=(
                f"TimeoutError: the render did not finish within {budget:.0f}s "
                f"(readiness budget {self.default_timeout_ms} ms)"
            ),
            error_kind="timeout",
        )

    async def _render_once(
        self,
        jsx_path: PathLike,
        output_path: Optional[PathLike] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        wait_extra_ms: int = 200,
        force_resize: bool = True,
        freeze_animations: bool = True,
        timeout_ms: Optional[int] = None,
    ) -> RenderResult:
        """One attempt. May return an infrastructure failure; `render` will not.

        `timeout_ms` overrides the readiness budget for this call only; the
        canary needs a short one and must not shorten renders running beside
        it. The .jsx must default-export a `Widget` component, no imports —
        the renderer provides React / ReactECharts / Recharts as globals.
        """
        assert self._context is not None, "service not started"
        jsx = Path(jsx_path).resolve()
        png = Path(output_path).resolve() if output_path else jsx.with_suffix(".png")
        png.parent.mkdir(parents=True, exist_ok=True)

        page = await self._acquire_page()
        if page is None:
            return RenderResult(
                Path(jsx_path), _png_for(jsx_path, output_path),
                error=f"no render page became available within {PAGE_ACQUIRE_TIMEOUT_S:.0f}s",
                error_kind="infra",
            )

        # Listeners are removed in `finally` so they don't accumulate across
        # calls. Vite's HMR socket broadcasts compile errors of any file to
        # every open page, so console events are filtered to THIS file.
        console_errors: list[str] = []
        # A page that failed may still be running the widget's loop; returning
        # it to the pool would hand that wait to the next caller.
        reusable = True

        def _on_pageerror(exc):
            console_errors.append(f"pageerror: {exc}")

        def _on_console(msg):
            if msg.type != "error":
                return
            if _belongs_to(msg, jsx.name):
                console_errors.append(f"console.error: {msg.text}")

        page.on("pageerror", _on_pageerror)
        page.on("console", _on_console)
        try:
            return await self._capture(
                page, jsx, png, width, height, wait_extra_ms, force_resize,
                freeze_animations, timeout_ms, console_errors,
            )
        except Exception as e:
            reusable = False
            # A readiness timeout means one of two things, and only the page
            # itself can say which.
            blocked = ("timeout" in f"{type(e).__name__}: {e}".lower()
                       and not await self._page_responds(page))
            return self._failure(jsx, png, e, console_errors, page_blocked=blocked)
        finally:
            try:
                page.remove_listener("pageerror", _on_pageerror)
                page.remove_listener("console", _on_console)
            except Exception:
                pass
            self._release_page(page, reusable)


    async def _page_responds(self, page: Page, timeout: float = 10.0) -> bool:
        """Can this page still run a line of JavaScript?

        The direct evidence for the difference the canary could only guess at.
        A widget in a loop never yields its main thread, so nothing evaluates
        on its page — that is a hang. A page merely starved of CPU answers as
        soon as it is scheduled, however loaded the machine is, so a timeout
        there is the renderer being oversubscribed and belongs to the retry,
        not to the widget.

        Measured: 1,822 renders fired at an eight-page pool produced 157
        `hang` verdicts; the same widgets, eight at a time, all rendered.
        """
        task = asyncio.ensure_future(page.evaluate("1"))
        if not await _abandon_after(task, timeout):
            return False
        try:
            task.result()
            return True
        except Exception:
            return False           # a dead page is not a hung widget either

    async def _capture(
        self, page: Page, jsx: Path, png: Path, width, height,
        wait_extra_ms: int, force_resize: bool, freeze_animations: bool,
        timeout_ms: Optional[int], console_errors: list[str],
    ) -> RenderResult:
        """Navigate, wait for readiness, settle, audit, screenshot."""
        await page.set_viewport_size({
            "width": width or self.default_viewport[0],
            "height": height or self.default_viewport[1],
        })

        # The mtime is a cache-buster: successive edits stay fresh even though
        # the Vite filesystem watcher is off.
        url = f"{VITE_BASE}/?path={quote(str(jsx))}&v={jsx.stat().st_mtime_ns}"
        await page.goto(url, wait_until="domcontentloaded")

        # Playwright enforces its own timeout from inside the page, which a
        # widget that never yields starves along with everything else — a 30s
        # budget was measured still waiting at 300s. Bounding the wait on this
        # side is what makes the timeout arrive at all, and arriving is what
        # lets the page be asked whether it is blocked or merely slow.
        budget_s = (timeout_ms or self.default_timeout_ms) / 1000
        ready = asyncio.ensure_future(page.wait_for_function(
            "() => window.__widget_ready === true || window.__widget_error",
            timeout=timeout_ms or self.default_timeout_ms,
        ))
        if not await _abandon_after(ready, budget_s + 2):
            raise TimeoutError(
                f"the widget did not become ready within {budget_s:.0f}s"
            )
        ready.result()          # re-raise Playwright's own error, if any

        err = await page.evaluate("window.__widget_error || null")
        if err:
            message = _normalize_runtime_error(_first_line(str(err)))
            kind = "empty" if message.startswith("EmptyRender") else "runtime"
            if _MODULE_FETCH_FAILURE in message.lower():
                diagnosis = await _syntax_diagnosis(jsx)
                if diagnosis:
                    # The console for this failure is the transport complaining
                    # (a 500 and a resource error); with the cause in hand it
                    # is noise, and noise is what a model has to read past.
                    message, kind, console_errors = diagnosis, "syntax", []
            return RenderResult(
                jsx, png,
                error=message,
                error_kind=kind,
                console_errors=_useful_console(console_errors, message),
            )

        if freeze_animations:
            # Before force_resize and settle, so the audit and the screenshot
            # describe the same finished state rather than two moments of a
            # moving one.
            try:
                await page.add_style_tag(content=_FREEZE_ANIMATIONS_CSS)
            except Exception:
                pass                  # a widget that renders is worth more than this

        if force_resize:
            await page.evaluate(_RESIZE_JS)

        # Wait for the widget to stop changing, then audit and screenshot that
        # one finished state — a fixed sleep would let the audit describe a
        # different moment than the PNG.
        settle_started = time.monotonic()
        try:
            settle = await page.evaluate(_SETTLE_JS, self.settle_budget_ms)
        except Exception:
            settle = {"quiet": False, "reason": "error"}
        if wait_extra_ms:
            await asyncio.sleep(wait_extra_ms / 1000)
        settle_ms = int((time.monotonic() - settle_started) * 1000)

        # Best effort: an audit failure must not block a good render.
        try:
            notes = await page.evaluate(_AUDIT_JS) or []
        except Exception:
            notes = []

        await page.locator("#widget-root").screenshot(
            path=str(png), omit_background=False,
        )
        return RenderResult(
            jsx, png,
            render_notes=notes,
            settled=bool(settle.get("quiet")),
            settle_ms=settle_ms,
            console_errors=_useful_console(console_errors, None),
        )

    @staticmethod
    def _failure(
        jsx: Path,
        png: Path,
        exc: Exception,
        console_errors: list[str],
        page_blocked: bool = False,
    ) -> RenderResult:
        """Classify a render exception, preferring the page's own diagnosis.

        A widget whose component throws leaves an uncaught `pageerror` behind
        and then stalls whatever Playwright call comes next; the page error is
        the actionable evidence, so it wins over the timeout that followed it.
        """
        raw = f"{type(exc).__name__}: {exc}"
        lowered = raw.lower()
        page_error = _first_page_error(console_errors)
        if any(marker in lowered for marker in _INFRA_ERROR_MARKERS):
            error, kind = _first_line(raw), "infra"
        elif page_error:
            error, kind = _normalize_runtime_error(page_error), "runtime"
        elif "timeout" in lowered:
            if page_blocked:
                error = (
                    f"HangError: the widget never yielded its main thread, so the "
                    f"page could not run a single expression after "
                    f"{_first_line(raw)}"
                )
                kind = "hang"
            else:
                error, kind = _first_line(raw), "timeout"
        else:
            error, kind = _first_line(raw), "infra"
        return RenderResult(
            jsx, png, error=error, error_kind=kind,
            console_errors=_useful_console(console_errors, error),
        )

    @staticmethod
    def _png_defect(png: Path) -> Optional[str]:
        """Why this file is not a usable PNG, or None. An unreadable PNG must
        not enter the dataset as a successful render."""
        try:
            with open(png, "rb") as handle:
                head = handle.read(8)
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"
        if head != b"\x89PNG\r\n\x1a\n":
            return "missing PNG signature"
        if png.stat().st_size < 100:
            return f"only {png.stat().st_size} bytes"
        return None

    # ---- page pool ---------------------------------------------------------

    async def _acquire_page(self) -> Optional[Page]:
        """A pooled page, or None once the bounded wait runs out.

        The wait must be bounded (invariant ③): a pool that lost its pages is
        only refilled by the repair, and the repair is only reached by a
        returned failure — an unbounded `get()` here is a deadlock.
        """
        try:
            return await asyncio.wait_for(
                self._free.get(), timeout=PAGE_ACQUIRE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return None

    def _release_page(self, page: Page, reusable: bool) -> None:
        if getattr(page, "_w2c_generation", None) != self._generation:
            # The pool was rebuilt while this render was in flight; the
            # replacement already holds n_workers live pages, so this one is
            # dropped rather than counted twice.
            asyncio.ensure_future(_close_quietly(page))
        elif reusable:
            self._free.put_nowait(page)
        else:
            asyncio.ensure_future(self._retire_page(page))

    async def _retire_page(self, page: Page, generation: int | None = None) -> None:
        """Replace a page that may still be busy, without shrinking the pool."""
        generation = self._generation if generation is None else generation
        context = self._context
        if context is None or self._generation != generation:
            # The pool was rebuilt and already holds its full set.
            asyncio.ensure_future(_close_quietly(page))
            return
        try:
            fresh = await context.new_page()
            fresh.set_default_timeout(self.default_timeout_ms)
            fresh._w2c_generation = self._generation
        except Exception:
            # No replacement available — put the casualty back rather than
            # shrink the pool (invariant ④): a page that may still be busy is
            # a timeout, and a timeout reaches the repair; an empty queue is a
            # deadlock.
            if getattr(page, "_w2c_generation", None) == self._generation:
                self._free.put_nowait(page)
            return
        asyncio.ensure_future(_close_quietly(page))
        self._free.put_nowait(fresh)

    async def _launch_browser_pool(self) -> None:
        """Build playwright, browser, context and the page pool."""
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            viewport={"width": self.default_viewport[0],
                      "height": self.default_viewport[1]},
            device_scale_factor=1,
        )
        pages = await asyncio.gather(
            *(self._context.new_page() for _ in range(self.n_workers))
        )
        for page in pages:
            page.set_default_timeout(self.default_timeout_ms)
            # Stamped so a worker still holding a page from a previous pool
            # discards it instead of returning a dead page to the live one.
            page._w2c_generation = self._generation
            self._free.put_nowait(page)

    async def _close_browser_pool(self) -> None:
        """Detach the current pool; dispose of it behind us, never in front.

        Closing a browser that has already died can block forever, cancellation
        included — which once hung a repair while it held the recovery lock
        (README.md). So the fields are cleared first and the old
        objects are disposed of in a detached task: a driver we fail to shut
        down leaks one process; a cleanup we wait for costs the collection.
        """
        pages: list[Page] = []
        while True:
            try:
                pages.append(self._free.get_nowait())
            except asyncio.QueueEmpty:
                break
        victims = (pages, self._context, self._browser, self._pw)
        self._context = None
        self._browser = None
        self._pw = None
        disposal = asyncio.ensure_future(_dispose(*victims))
        self._disposals.add(disposal)
        disposal.add_done_callback(self._disposals.discard)

    # ---- repair ------------------------------------------------------------

    async def _recover_infrastructure(
        self, generation: int, *, reason: str, cycle: int
    ) -> None:
        """Rebuild the browser pool, and Vite if it stopped answering.

        Guarded by generation so that N workers failing on the same dead
        browser perform one repair between them rather than N.
        """
        assert self._recovery_lock is not None, "service not started"
        async with self._recovery_lock:
            if self._generation != generation:
                return  # someone else already repaired this generation
            delay = RECOVERY_BACKOFF_S[min(cycle - 1, len(RECOVERY_BACKOFF_S) - 1)]
            severity = "STILL BROKEN" if cycle >= RECOVERY_ALARM_AFTER else "repairing"
            print(
                f"renderer: {severity} after {_first_line(reason)}; cycle {cycle}, "
                f"generation {self._generation}, waiting {delay}s",
                flush=True,
            )
            await asyncio.sleep(delay)
            await self._close_browser_pool()
            # Bump before the rebuild, not after (invariant ②): the new pages
            # are stamped with the current generation, and a pool stamped with
            # the old one is discarded page by page until everyone deadlocks.
            self._generation += 1
            # Sequential on purpose, unlike startup: callers are already
            # blocked in `_acquire_page`, and a pool filled before Vite
            # answers hands them pages whose every goto fails.
            while True:
                try:
                    await self._start_vite_if_needed()
                    await self._launch_browser_pool()
                    return
                except Exception as exc:
                    # Deliberately unbounded — see render().
                    print(
                        f"renderer: relaunch failed ({type(exc).__name__}: "
                        f"{_first_line(str(exc))}); retrying in {delay}s",
                        flush=True,
                    )
                    await self._close_browser_pool()
                    await asyncio.sleep(delay)

    # ---- vite --------------------------------------------------------------

    async def _start_vite_if_needed(self) -> None:
        """Ensure something serves the renderer, starting one if nothing does."""
        if _vite_answers():
            return  # ours or someone else's; either way the renderer is served
        # Nothing is answering. Anything still holding the port would make
        # --strictPort refuse, so clear ours and wait for the socket to go.
        await self._kill_vite()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 30
        while loop.time() < deadline:
            if not _port_bound(VITE_HOST, VITE_PORT):
                break
            await asyncio.sleep(0.5)
        if _port_bound(VITE_HOST, VITE_PORT):
            raise RuntimeError(
                f"port {VITE_PORT} is held by an unhealthy non-renderer service; "
                "set a unique W2C_VITE_PORT"
            )
        # Spawn Vite directly when its entry point is present — `npm run` adds
        # an extra node+shell startup, which is seconds under load. strictPort:
        # fail loudly instead of silently landing on 5174.
        vite_js = RENDERER_DIR / "node_modules" / "vite" / "bin" / "vite.js"
        if not vite_js.exists():
            # Say it here rather than let node exit 127 and report "the dev
            # server exited during startup with code 127", which is the first
            # thing a fresh checkout hits and explains nothing.
            raise RuntimeError(
                f"the renderer's dependencies are not installed: {vite_js} is "
                f"missing. Run `npm ci` in {RENDERER_DIR}, or use the container "
                f"image, which has them baked in."
            )
        cmd = ["node", str(vite_js),
               "--host", VITE_HOST, "--port", str(VITE_PORT), "--strictPort"]
        self._vite_proc = subprocess.Popen(
            cmd,
            cwd=str(RENDERER_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._owns_vite = True

        deadline = loop.time() + VITE_START_TIMEOUT_S
        announced = False
        while loop.time() < deadline:
            if self._vite_proc.poll() is not None:
                raise RuntimeError(
                    f"Vite dev server exited during startup with code "
                    f"{self._vite_proc.returncode}"
                )
            if _vite_answers():
                return
            waited = VITE_START_TIMEOUT_S - (deadline - loop.time())
            if waited > 30 and not announced:
                announced = True
                print(
                    f"renderer: Vite still starting after {waited:.0f}s "
                    f"(budget {VITE_START_TIMEOUT_S}s); the machine is probably loaded",
                    flush=True,
                )
            await asyncio.sleep(0.5)
        await self._kill_vite()
        raise RuntimeError(
            f"Vite dev server did not start within {VITE_START_TIMEOUT_S}s"
        )

    async def _kill_vite(self) -> None:
        proc = self._vite_proc
        if proc is None or proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        if await _wait_process(proc, 5):
            return
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        await _wait_process(proc, 5)
