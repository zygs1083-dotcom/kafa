#!/usr/bin/env python3
"""Update the current harness phase and append a runtime event."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness_lib import append_event, write_state


VALID_PHASES = {
    "intake",
    "project_bootstrap",
    "requirement_baseline",
    "confirmation",
    "team_architecture",
    "planning",
    "implementation",
    "qa",
    "delivery_readiness",
    "retrospective",
    "archived",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=sorted(VALID_PHASES))
    parser.add_argument("--status", default=None)
    parser.add_argument("--owner", default=None)
    parser.add_argument("--scope-status", default=None)
    parser.add_argument("--blocked-reason", default=None)
    args = parser.parse_args()

    updates = {"phase": args.phase}
    if args.status is not None:
        updates["status"] = args.status
    if args.owner is not None:
        updates["current_owner"] = args.owner
    if args.scope_status is not None:
        updates["scope_status"] = args.scope_status
    if args.blocked_reason is not None:
        updates["blocked_reason"] = args.blocked_reason

    root = Path.cwd()
    state = write_state(root, updates)
    append_event(root, "phase_updated", {"phase": args.phase, "status": state.get("status", "")})
    print(f"OK: phase={args.phase}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
