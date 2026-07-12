"""Generic timestamp and revision checks for controller-owned mutations."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_time(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_expired(value: str | None) -> bool:
    parsed = parse_time(value)
    return bool(value) and (parsed is None or parsed <= datetime.now(timezone.utc))
