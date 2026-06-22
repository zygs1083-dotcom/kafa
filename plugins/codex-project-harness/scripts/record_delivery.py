#!/usr/bin/env python3
"""Write a delivery readiness record to docs/harness/delivery.md."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness_lib import append_event, append_markdown, now_iso


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--acceptance", default="")
    parser.add_argument("--changed-files", default="")
    parser.add_argument("--validation", default="")
    parser.add_argument("--qa", default="")
    parser.add_argument("--collaboration-links", default="")
    parser.add_argument("--failure-mode-coverage", default="")
    parser.add_argument("--quality-gate", default="")
    parser.add_argument("--data-config-notes", default="")
    parser.add_argument("--known-gaps", default="")
    parser.add_argument("--handoff", default="")
    args = parser.parse_args()

    content = f"""
## Delivery Record {now_iso()}

### Scope
{args.scope}

### Acceptance Mapping
{args.acceptance}

### Changed Files
{args.changed_files}

### Validation
{args.validation}

### Independent QA
{args.qa}

### Collaboration Links
{args.collaboration_links}

### Failure Mode Coverage
{args.failure_mode_coverage}

### Quality Gate
{args.quality_gate}

### Data / Config Notes
{args.data_config_notes}

### Known Gaps
{args.known_gaps}

### Handoff Notes
{args.handoff}

### Out Of Scope
Deployment, production release, infrastructure provisioning, production migrations, secret changes, and paid-resource creation.
"""
    root = Path.cwd()
    append_markdown(root, "docs/harness/delivery.md", content)
    append_event(root, "delivery_recorded", {"scope": args.scope})
    print("OK: delivery recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
