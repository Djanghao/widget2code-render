"""Deterministic, startup-frozen policy for widget source capabilities.

The contract a widget is written against has two halves: what it may import, and what
it may assume already exists. Both belong here. Carrying only the first left the second
hard-coded in the renderer's entry module, so `policy_id` -- the thing an experiment
pins to prove which contract produced its pixels -- described half a contract.

Two contracts are named and supported:

    m1  no imports at all; React and Recharts are on the page as globals, so a widget
        writes `Recharts.LineChart` as a free identifier. Icons have to be drawn by
        hand, because react-icons has no global. This is what the reference set of
        1,816 widgets is written in.

    m2  imports required, no globals at all: `import { AreaChart } from 'recharts'`,
        `import { PiEyeBold } from 'react-icons/pi'`. React itself may not be imported
        -- the automatic JSX runtime makes it unnecessary, and its absence is what
        makes hooks and state unreachable rather than merely discouraged.

They are deliberately exclusive rather than one being the other plus permissions. If
globals survived alongside imports, the same widget could be written either way and a
collection would be a mixture of both; and since react-icons has no global, a file
would end up importing its icons while reaching its charts off the window. Written the
wrong way round, each fails loudly in the other: `Recharts is not defined` under m2,
`import 'recharts' is not allowed` under m1.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

SOURCE_POLICY_SCHEMA_VERSION = 2

DEFAULT_GLOBALS = ("React", "Recharts")

_PACKAGE_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_GLOBAL_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_IDENTIFIER_START = re.compile(r"[A-Za-z_$]")
_IDENTIFIER_PART = re.compile(r"[A-Za-z0-9_$]")


@dataclass(frozen=True)
class SourcePolicy:
    """What a widget may import, and what it may assume the page already provides."""

    allowed_imports: tuple[str, ...] = ()
    allow_dynamic_imports: bool = False
    globals: tuple[str, ...] = DEFAULT_GLOBALS
    # Last, and keyword-only in practice: callers construct this positionally, and a new
    # field in front of the ones they pass silently reinterprets every one of them.
    name: str = "custom"

    def __post_init__(self) -> None:
        normalized = tuple(sorted(set(self.allowed_imports)))
        if normalized != self.allowed_imports:
            object.__setattr__(self, "allowed_imports", normalized)
        names = tuple(sorted(set(self.globals)))
        if names != self.globals:
            object.__setattr__(self, "globals", names)
        for name in self.globals:
            if name not in DEFAULT_GLOBALS or _GLOBAL_NAME.fullmatch(name) is None:
                raise ValueError(f"unknown global: {name!r}")
        if type(self.allow_dynamic_imports) is not bool:
            raise ValueError("allow_dynamic_imports must be boolean")
        for pattern in self.allowed_imports:
            literal = pattern[:-2] if pattern.endswith("/*") else pattern
            if "*" in literal or not _is_bare_package(literal):
                raise ValueError(f"invalid import allowlist pattern: {pattern!r}")

    @property
    def policy_id(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "source_policy_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_POLICY_SCHEMA_VERSION,
            "mode": self.name,
            "allowed_imports": list(self.allowed_imports),
            "allow_dynamic_imports": self.allow_dynamic_imports,
            "globals": list(self.globals),
        }

    def descriptor(self) -> dict[str, Any]:
        return {"policy_id": self.policy_id, **self.to_dict()}

    def violation(self, source: str) -> str | None:
        """Return one stable author-facing violation, or None when source is allowed."""

        references, invalid_dynamic = _import_references(source)
        if invalid_dynamic:
            return "dynamic import() must use one literal bare-package specifier"
        for specifier, dynamic in references:
            if dynamic and not self.allow_dynamic_imports:
                return "dynamic import() is disabled by the renderer source policy"
            problem = self._specifier_problem(specifier)
            if problem is not None:
                return problem
        return None

    def _specifier_problem(self, specifier: str) -> str | None:
        if specifier.startswith((".", "/")):
            return f"local or absolute import is forbidden: {specifier!r}"
        if not _is_bare_package(specifier):
            return f"non-package import is forbidden: {specifier!r}"
        if any(_matches(specifier, pattern) for pattern in self.allowed_imports):
            return None
        allowed = ", ".join(self.allowed_imports) or "none"
        return f"import {specifier!r} is not allowed; allowed imports: {allowed}"


MODES: dict[str, "SourcePolicy"] = {}


def policy_for_mode(name: str) -> "SourcePolicy":
    """The named contract, or a ValueError naming the ones that exist."""
    try:
        return MODES[name]
    except KeyError:
        raise ValueError(
            f"unknown render mode {name!r}; modes: {', '.join(sorted(MODES))}"
        ) from None


def source_policy_from_values(
    allowed_imports: Sequence[str] = (),
    *,
    allow_dynamic_imports: bool = False,
    globals: Sequence[str] | None = None,
    name: str = "custom",
) -> SourcePolicy:
    values: list[str] = []
    for item in allowed_imports:
        values.extend(part.strip() for part in item.split(",") if part.strip())
    names: list[str] = []
    if globals is not None:
        for item in globals:
            names.extend(part.strip() for part in item.split(",") if part.strip())
    return SourcePolicy(
        allowed_imports=tuple(sorted(set(values))),
        allow_dynamic_imports=allow_dynamic_imports,
        globals=DEFAULT_GLOBALS if globals is None else tuple(sorted(set(names))),
        name=name,
    )


def write_source_policy(path: Path, policy: SourcePolicy) -> None:
    """Atomically publish the daemon's effective policy beside its socket."""

    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(policy.descriptor(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _matches(specifier: str, pattern: str) -> bool:
    if pattern.endswith("/*"):
        return specifier.startswith(pattern[:-1]) and len(specifier) > len(pattern) - 1
    return specifier == pattern


def _is_bare_package(specifier: str) -> bool:
    """Whether a module name is an npm-style package or package subpath."""

    parts = specifier.split("/")
    if specifier.startswith("@"):
        if len(parts) < 2 or not parts[0][1:]:
            return False
        parts[0] = parts[0][1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return False
    return all(_PACKAGE_SEGMENT.fullmatch(part) is not None for part in parts)


def _import_references(source: str) -> tuple[list[tuple[str, bool]], bool]:
    """Read import tokens while ignoring comments and displayed string/template text."""

    tokens = _tokens(source)
    references: list[tuple[str, bool]] = []
    invalid_dynamic = bool(re.search(r"\$\{[^}]*\bimport\s*\(", source, re.S))
    for index, (kind, value) in enumerate(tokens):
        if kind != "identifier" or value not in {"import", "export"}:
            continue
        following = tokens[index + 1 :]
        if value == "import":
            if not following:
                continue
            if following[0] == ("punct", "("):
                if len(following) < 3 or following[1][0] != "string" or following[2] != (
                    "punct",
                    ")",
                ):
                    invalid_dynamic = True
                else:
                    references.append((following[1][1], True))
                continue
            if following[0][0] == "string":
                references.append((following[0][1], False))
                continue
            for candidate, token in enumerate(following):
                if (
                    token == ("identifier", "from")
                    and candidate + 1 < len(following)
                    and following[candidate + 1][0] == "string"
                ):
                    references.append((following[candidate + 1][1], False))
                    break
                if token == ("punct", ";"):
                    break
        elif following and following[0] in {("punct", "*"), ("punct", "{")}:
            for candidate, token in enumerate(following):
                if (
                    token == ("identifier", "from")
                    and candidate + 1 < len(following)
                    and following[candidate + 1][0] == "string"
                ):
                    references.append((following[candidate + 1][1], False))
                    break
                if token == ("punct", ";"):
                    break
    return references, invalid_dynamic


def _tokens(source: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            close = source.find("*/", index + 2)
            index = len(source) if close < 0 else close + 2
            continue
        if character in {"'", '"', "`"}:
            quote = character
            start = index + 1
            index = start
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    break
                index += 1
            if quote != "`":
                tokens.append(("string", source[start:index]))
            index = min(len(source), index + 1)
            continue
        if _IDENTIFIER_START.fullmatch(character):
            start = index
            index += 1
            while index < len(source) and _IDENTIFIER_PART.fullmatch(source[index]):
                index += 1
            tokens.append(("identifier", source[start:index]))
            continue
        tokens.append(("punct", character))
        index += 1
    return tokens


MODES.update(
    {
        "m1": SourcePolicy(name="m1"),
        "m2": SourcePolicy(
            name="m2",
            allowed_imports=("react-icons/pi", "react-icons/si", "recharts"),
            globals=(),
        ),
    }
)
