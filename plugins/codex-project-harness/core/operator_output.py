"""Pure presentation helpers for concise and complete operator output.

This module deliberately has no database or delivery-policy dependencies.  Its
callers are responsible for evaluating state, ordering blockers, and selecting
legal actions.  The helpers only validate, freeze, and render those facts.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


_BLOCKER_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class OperatorOutputError(ValueError):
    """The supplied presentation facts cannot form a stable operator report."""


def _single_line_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise OperatorOutputError(f"{label} must be a string")
    if not value.strip():
        raise OperatorOutputError(f"{label} must be non-empty")
    if value != value.strip():
        raise OperatorOutputError(f"{label} must not have surrounding whitespace")
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise OperatorOutputError(f"{label} must be one line")
    return value


def _freeze_json_value(
    value: object,
    *,
    label: str,
    active_containers: set[int],
) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OperatorOutputError(f"{label} must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise OperatorOutputError(f"{label} must not contain a reference cycle")
        active_containers.add(identity)
        try:
            frozen: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not key:
                    raise OperatorOutputError(
                        f"{label} object keys must be non-empty strings"
                    )
                if any(character in key for character in ("\r", "\n", "\x00")):
                    raise OperatorOutputError(
                        f"{label} object keys must not contain control lines"
                    )
                frozen[key] = _freeze_json_value(
                    item,
                    label=f"{label}.{key}",
                    active_containers=active_containers,
                )
            return MappingProxyType(frozen)
        finally:
            active_containers.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise OperatorOutputError(f"{label} must not contain a reference cycle")
        active_containers.add(identity)
        try:
            return tuple(
                _freeze_json_value(
                    item,
                    label=f"{label}[{index}]",
                    active_containers=active_containers,
                )
                for index, item in enumerate(value)
            )
        finally:
            active_containers.remove(identity)
    raise OperatorOutputError(
        f"{label} must contain only JSON-compatible values, got "
        f"{type(value).__name__}"
    )


def _thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class OperatorBlocker:
    """One ordered operator blocker with an exact public JSON shape."""

    code: str
    message: str

    def __post_init__(self) -> None:
        code = _single_line_text(self.code, "operator blocker code")
        if not _BLOCKER_CODE.fullmatch(code):
            raise OperatorOutputError(
                "operator blocker code must use letters, digits, dot, underscore, or dash"
            )
        _single_line_text(self.message, "operator blocker message")

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _coerce_blocker(value: object, index: int) -> OperatorBlocker:
    if isinstance(value, OperatorBlocker):
        return value
    if not isinstance(value, Mapping):
        raise OperatorOutputError(
            f"operator blockers[{index}] must be an OperatorBlocker or object"
        )
    if not all(isinstance(key, str) for key in value):
        raise OperatorOutputError(
            f"operator blockers[{index}] keys must be strings"
        )
    actual = set(value)
    expected = {"code", "message"}
    if actual != expected:
        raise OperatorOutputError(
            f"operator blockers[{index}] keys mismatch: "
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )
    return OperatorBlocker(code=value["code"], message=value["message"])


@dataclass(frozen=True, slots=True)
class OperatorEnvelope:
    """Deeply immutable projection of a complete canonical operator report."""

    state: str
    blockers: tuple[OperatorBlocker, ...] = ()
    actions: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "state",
            _single_line_text(self.state, "operator state"),
        )

        if isinstance(self.blockers, (str, bytes)):
            raise OperatorOutputError("operator blockers must be an ordered collection")
        try:
            blockers = tuple(
                _coerce_blocker(blocker, index)
                for index, blocker in enumerate(self.blockers)
            )
        except TypeError as exc:
            raise OperatorOutputError(
                "operator blockers must be an ordered collection"
            ) from exc
        object.__setattr__(self, "blockers", blockers)

        if isinstance(self.actions, (str, bytes)):
            raise OperatorOutputError("operator actions must be an ordered collection")
        try:
            actions = tuple(
                _single_line_text(action, f"operator actions[{index}]")
                for index, action in enumerate(self.actions)
            )
        except TypeError as exc:
            raise OperatorOutputError(
                "operator actions must be an ordered collection"
            ) from exc
        object.__setattr__(self, "actions", actions)

        if not isinstance(self.details, Mapping):
            raise OperatorOutputError("operator details must be an object")
        frozen_details = _freeze_json_value(
            self.details,
            label="operator details",
            active_containers=set(),
        )
        object.__setattr__(self, "details", frozen_details)

    def as_dict(self) -> dict[str, object]:
        """Return a detached JSON-compatible value with the exact public shape."""

        return {
            "state": self.state,
            "blockers": [blocker.as_dict() for blocker in self.blockers],
            "actions": list(self.actions),
            "details": _thaw_json_value(self.details),
        }

    def concise_lines(self) -> tuple[str, str, str]:
        """Render the first already-ordered blocker and action as three lines."""

        blocker = (
            f"[{self.blockers[0].code}] {self.blockers[0].message}"
            if self.blockers
            else "none"
        )
        action = self.actions[0] if self.actions else "none"
        return (
            f"state: {self.state}",
            f"blocker: {blocker}",
            f"next: {action}",
        )


def build_operator_envelope(
    *,
    state: str,
    blockers: Iterable[OperatorBlocker | Mapping[str, object]] = (),
    actions: Iterable[str] = (),
    details: Mapping[str, object] | None = None,
) -> OperatorEnvelope:
    """Validate and freeze facts without evaluating or reordering them."""

    return OperatorEnvelope(
        state=state,
        blockers=tuple(blockers),
        actions=tuple(actions),
        details={} if details is None else details,
    )


def render_concise(envelope: OperatorEnvelope) -> str:
    """Return the exact three-line default operator card."""

    if not isinstance(envelope, OperatorEnvelope):
        raise OperatorOutputError("concise output requires an OperatorEnvelope")
    return "\n".join(envelope.concise_lines()) + "\n"


def render_json(envelope: OperatorEnvelope) -> str:
    """Return exactly one complete JSON object followed by one newline."""

    if not isinstance(envelope, OperatorEnvelope):
        raise OperatorOutputError("JSON output requires an OperatorEnvelope")
    return json.dumps(envelope.as_dict(), ensure_ascii=False) + "\n"


def render_verbose(existing_lines: str | Iterable[str]) -> str:
    """Preserve an existing complete human report without re-evaluating facts."""

    if isinstance(existing_lines, str):
        if "\x00" in existing_lines:
            raise OperatorOutputError("verbose output must not contain NUL")
        return existing_lines if existing_lines.endswith("\n") else existing_lines + "\n"

    rendered: list[str] = []
    try:
        for index, line in enumerate(existing_lines):
            if not isinstance(line, str):
                raise OperatorOutputError(
                    f"verbose output line {index} must be a string"
                )
            if any(character in line for character in ("\r", "\n", "\x00")):
                raise OperatorOutputError(
                    f"verbose output line {index} must be one complete line"
                )
            rendered.append(line)
    except TypeError as exc:
        raise OperatorOutputError("verbose output must be text or complete lines") from exc
    return "\n".join(rendered) + ("\n" if rendered else "")


__all__ = [
    "OperatorBlocker",
    "OperatorEnvelope",
    "OperatorOutputError",
    "build_operator_envelope",
    "render_concise",
    "render_json",
    "render_verbose",
]
