#!/usr/bin/env python3
"""Validate fresh-session skill flow evidence without requiring a Codex service."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "docs" / "runtime" / "skill-eval-transcript-fixture.txt"
CONTRACT = (
    Path(__file__).resolve().parents[1] / "references" / "workflow-contract.json"
)

FORBIDDEN_MARKERS = [
    "harness.py --root . phase ",
    "harness.py --root . scope ",
    "harness.py --root . session ",
    "harness.py --root . dispatch ",
    "harness.py --root . evidence ",
    "harness.py --root . test record",
    "--reviewer-session-id",
    "--reviewer-attestation-id",
]
EXPECTED_ADVANCED_TRIGGER_IDS = (
    "parallel-delegation",
    "deep-kernel-review",
    "harness-audit",
    "project-retrospective",
    "live-host-compatibility",
    "release-rehearsal",
)
FIXTURE_BEGIN = "<!-- BEGIN GENERATED: workflow-contract:skill-eval-transcript -->"
FIXTURE_END = "<!-- END GENERATED: workflow-contract:skill-eval-transcript -->"


def fixture_transcript() -> str:
    text = FIXTURE.read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines.count(FIXTURE_BEGIN) != 1 or lines.count(FIXTURE_END) != 1:
        raise RuntimeError("skill eval fixture must contain one generated transcript block")
    begin = lines.index(FIXTURE_BEGIN)
    end = lines.index(FIXTURE_END)
    if begin >= end:
        raise RuntimeError("skill eval fixture transcript markers are out of order")
    return "\n".join(lines[begin + 1 : end]) + "\n"


def evaluation_markers() -> list[str]:
    try:
        contract: Any = json.loads(CONTRACT.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load workflow contract: {exc}") from exc
    if not isinstance(contract, dict) or contract.get("contract_version") != 1:
        raise RuntimeError("workflow contract must be a version-1 object")
    commands = contract.get("commands")
    dependencies = contract.get("dependencies")
    advanced_triggers = contract.get("advanced_triggers")
    labels = contract.get("output_labels")
    if not isinstance(commands, dict) or not commands or not all(
        isinstance(value, str) and value for value in commands.values()
    ):
        raise RuntimeError("workflow contract commands must be non-empty strings")
    if not isinstance(dependencies, list) or not dependencies:
        raise RuntimeError("workflow contract dependencies must be a non-empty list")
    if not isinstance(advanced_triggers, list):
        raise RuntimeError("workflow contract advanced_triggers must be a list")
    trigger_ids = tuple(
        trigger.get("id") if isinstance(trigger, dict) else None
        for trigger in advanced_triggers
    )
    if trigger_ids != EXPECTED_ADVANCED_TRIGGER_IDS:
        raise RuntimeError(
            "workflow contract advanced trigger IDs mismatch: "
            f"expected={list(EXPECTED_ADVANCED_TRIGGER_IDS)} actual={list(trigger_ids)}"
        )
    scenario_markers = [
        "scenario-verdict: id=small-single-producer; selected=none; result=pass"
    ]
    for trigger in advanced_triggers:
        if not isinstance(trigger, dict) or set(trigger) != {"id", "when", "activates"}:
            raise RuntimeError(
                "workflow advanced trigger requires only id, when, and activates"
            )
        if not all(
            isinstance(trigger.get(field), str) and trigger[field].strip()
            for field in ("id", "when", "activates")
        ):
            raise RuntimeError("workflow advanced trigger fields must be non-empty strings")
        scenario_markers.append(
            "scenario-verdict: "
            f"id={trigger['id']}; selected={trigger['id']}; result=pass; "
            f"when={trigger['when']}; activates={trigger['activates']}"
        )
    dependency_markers: list[str] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {"before", "after"}:
            raise RuntimeError("workflow dependency requires only before and after")
        before = dependency.get("before")
        after = dependency.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            raise RuntimeError("workflow dependency endpoints must be strings")
        dependency_markers.append(f"dependency: {before} -> {after}")
    if not isinstance(labels, dict) or not isinstance(
        labels.get("human_review_required"), str
    ):
        raise RuntimeError("workflow contract is missing human_review_required label")
    command_markers = [f"$ {command}" for command in commands.values()]
    handoff = contract.get("handoff_obligations")
    if not isinstance(handoff, list) or not handoff or not all(
        isinstance(item, str) and item for item in handoff
    ):
        raise RuntimeError("workflow contract handoff_obligations must be non-empty strings")
    return (
        [
            "evaluation: Kafa workflow contract",
            "authority: OpenSpec -> Kafa SQLite -> evaluate_delivery_prerequisites",
            "host: Native Codex/ChatGPT owns task/subagent/worktree/model/cancel/handoff",
            "writer: root controller only",
        ]
        + scenario_markers
        + dependency_markers
        + command_markers
        + [f"handoff: {item}" for item in handoff]
        + [labels["human_review_required"]]
    )


def transcript_evidence() -> tuple[str, str, int, str]:
    command = os.environ.get("CODEX_EVAL_CMD", "").strip()
    if not command:
        return fixture_transcript(), "", 0, "fixture-only"

    try:
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(
                command,
                cwd=temp,
                text=True,
                shell=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return stdout, stderr, 124, "host-evaluated"
    return result.stdout, result.stderr, result.returncode, "host-evaluated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate local-only fresh-session Skill transcript markers"
    )
    parser.parse_args(argv)

    try:
        required_markers = evaluation_markers()
        stdout, stderr, command_returncode, source = transcript_evidence()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    expected = [f"source: {source}", *required_markers]
    actual = stdout.splitlines()
    while actual and not actual[-1]:
        actual.pop()
    missing = [line for line in expected if actual.count(line) < expected.count(line)]
    unexpected = [line for line in actual if actual.count(line) > expected.count(line)]
    retired = [
        marker
        for marker in FORBIDDEN_MARKERS
        if marker in stdout or marker in stderr
    ]
    out_of_order = not missing and not unexpected and actual != expected
    if command_returncode != 0:
        print(
            "ERROR: host skill eval command failed "
            f"(source={source}, returncode={command_returncode})"
        )
    if stderr:
        print("ERROR: host skill eval emitted stderr")
    if missing:
        for marker in missing:
            print(f"ERROR: missing skill eval marker: {marker}")
    if unexpected:
        for line in unexpected:
            print(f"ERROR: unexpected skill eval line: {line}")
    if retired:
        for marker in retired:
            print(f"ERROR: retired skill eval marker present: {marker}")
    if out_of_order:
        print("ERROR: out-of-order skill eval marker")
    if (
        command_returncode != 0
        or bool(stderr)
        or missing
        or unexpected
        or retired
        or out_of_order
    ):
        return 1
    if source == "fixture-only":
        print(
            "OK: fixture-only local contract matched "
            f"({len(required_markers)} required markers); not fresh Host evidence"
        )
    else:
        print(
            "OK: host-evaluated local contract matched "
            f"({len(required_markers)} required markers)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
