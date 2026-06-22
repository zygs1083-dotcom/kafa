"""Revision and lease checks for multi-agent task mutation."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable


LEASE_TTL_SECONDS = 3600


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_expired(value: str | None) -> bool:
    parsed = parse_time(value)
    return bool(parsed and parsed <= datetime.now(timezone.utc))


def lease_deadline() -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=LEASE_TTL_SECONDS)).isoformat()


def require_revision(
    row: sqlite3.Row,
    expected_revision: int | None,
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> None:
    if expected_revision is not None and int(row["revision"]) != expected_revision:
        raise error_factory(f"revision mismatch: expected {expected_revision}, actual {row['revision']}")


def require_lease(
    row: sqlite3.Row,
    agent: str,
    lease_token: str | None,
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> None:
    if row["lease_agent"] != agent:
        raise error_factory(f"task is not leased by agent: {row['id']} agent={agent}")
    if not lease_token or row["lease_token"] != lease_token:
        raise error_factory(f"lease token mismatch: {row['id']}")
    if is_expired(row["lease_expires_at"]):
        raise error_factory(f"lease expired: {row['id']}")
