"""Deterministic, startup-frozen policy for widget source capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

SOURCE_POLICY_SCHEMA_VERSION = 1

_PACKAGE_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_IDENTIFIER_START = re.compile(r"[A-Za-z_$]")
_IDENTIFIER_PART = re.compile(r"[A-Za-z0-9_$]")


@dataclass(frozen=True)
class SourcePolicy:
    """Allowed module imports; defaults to the historical no-import contract."""

    allowed_imports: tuple[str, ...] = ()
    allow_dynamic_imports: bool = False

    def __post_init__(self) -> None:
        normalized = tuple(sorted(set(self.allowed_imports)))
        if normalized != self.allowed_imports:
            object.__setattr__(self, "allowed_imports", normalized)
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
            "allowed_imports": list(self.allowed_imports),
            "allow_dynamic_imports": self.allow_dynamic_imports,
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


def source_policy_from_values(
    allowed_imports: Sequence[str] = (), *, allow_dynamic_imports: bool = False
) -> SourcePolicy:
    values: list[str] = []
    for item in allowed_imports:
        values.extend(part.strip() for part in item.split(",") if part.strip())
    return SourcePolicy(tuple(sorted(set(values))), allow_dynamic_imports)


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
