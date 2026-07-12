"""Installer helpers for Codex Project Harness."""

from __future__ import annotations

from .version import release_version

__all__ = ["__version__"]

__version__ = release_version()
