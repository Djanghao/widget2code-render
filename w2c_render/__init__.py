"""A widget renderer whose pixels do not move.

    from w2c_render import RenderClient          # talks to the shared daemon
    from w2c_render import RenderService         # an in-process pool instead

`RenderClient` and everything it needs are pure standard library: a process
that only calls the daemon does not install Playwright. `RenderService` is
imported lazily for that reason.
"""
from typing import TYPE_CHECKING

from .render_result import (
    OVERFLOW_WARNING_TEXT,
    WIDGET_DEFECT_ERROR_KINDS,
    RenderResult,
)
from .render_client import RenderClient, RenderTransportError, make_renderer

if TYPE_CHECKING:                                # for type checkers only
    from .render import RenderService

_LAZY = {"RenderService": ".render"}


def __getattr__(name: str):
    """Import the Playwright-backed service only when it is actually used."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    return getattr(import_module(module, __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "OVERFLOW_WARNING_TEXT",
    "RenderClient",
    "RenderResult",
    "RenderService",
    "RenderTransportError",
    "WIDGET_DEFECT_ERROR_KINDS",
    "make_renderer",
]
