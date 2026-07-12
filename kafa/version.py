"""Derive the public package version from canonical packaging metadata."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version as distribution_version
from pathlib import Path


def release_version() -> str:
    """Return the source VERSION or the installed distribution version."""

    source_version = Path(__file__).resolve().parents[1] / "VERSION"
    if source_version.is_file():
        return source_version.read_text(encoding="utf-8").strip()
    try:
        return _release_spelling(distribution_version("kafa"))
    except PackageNotFoundError:
        return "unknown"


def _release_spelling(value: str) -> str:
    """Convert PEP 440 beta spelling to the repository release spelling."""

    match = re.fullmatch(r"(\d+\.\d+\.\d+)b(\d+)", value)
    return f"{match.group(1)}-beta.{match.group(2)}" if match else value
