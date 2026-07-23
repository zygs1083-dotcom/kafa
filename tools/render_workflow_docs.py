#!/usr/bin/env python3
"""Render/check bounded documentation views from the workflow contract."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


CONTRACT_RELATIVE = Path(
    "plugins/codex-project-harness/references/workflow-contract.json"
)
EXPECTED_TOP_LEVEL = {
    "contract_version",
    "authorities",
    "safeguards",
    "routes",
    "advanced_triggers",
    "stages",
    "dependencies",
    "commands",
    "output_labels",
    "handoff_obligations",
    "generated_views",
}
REQUIRED_AUTHORITIES = {"root-controller", "native-host"}
REQUIRED_SAFEGUARDS = {
    "local-only",
    "root-controller-single-writer",
    "native-host-lifecycle",
    "immutable-execution",
    "current-candidate-verification",
    "fail-closed-delivery-gate",
}
REQUIRED_ROUTES = {
    "project-harness",
    "minimal-safe-change",
    "bug-fix-loop",
    "test-first-delivery",
    "independent-quality-gate",
    "harness-audit",
    "project-retrospective",
}
REQUIRED_ADVANCED_TRIGGERS = (
    "parallel-delegation",
    "deep-kernel-review",
    "harness-audit",
    "project-retrospective",
    "live-host-compatibility",
    "release-rehearsal",
)
REQUIRED_DEPENDENCIES = {
    ("delivery-plan", "task-start"),
    ("task-start", "task-submit"),
    ("task-submit", "task-accept"),
    ("controller-verification", "task-accept"),
    ("task-accept", "quality-gate"),
    ("baseline-confirmation", "delivery-readiness"),
    ("quality-gate", "delivery-readiness"),
    ("delivery-readiness", "delivery-record"),
}


class ContractError(RuntimeError):
    pass


def _id_map(items: Any, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        raise ContractError(f"{label} must be a list")
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ContractError(f"{label} entry requires string id")
        identifier = item["id"].strip()
        if not identifier or identifier in result:
            raise ContractError(f"{label} contains invalid or duplicate id: {identifier}")
        result[identifier] = item
    return result


def load_contract(root: Path) -> dict[str, Any]:
    path = root / CONTRACT_RELATIVE
    if not path.is_file():
        raise ContractError(f"missing workflow contract: {CONTRACT_RELATIVE}")
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid workflow contract: {exc}") from exc
    if not isinstance(contract, dict):
        raise ContractError("workflow contract must be an object")
    if set(contract) != EXPECTED_TOP_LEVEL:
        raise ContractError(
            "workflow contract top-level keys mismatch: "
            f"missing={sorted(EXPECTED_TOP_LEVEL - set(contract))} "
            f"extra={sorted(set(contract) - EXPECTED_TOP_LEVEL)}"
        )
    if contract["contract_version"] != 1:
        raise ContractError("workflow contract_version must be 1")

    authorities = _id_map(contract["authorities"], "authorities")
    safeguards = _id_map(contract["safeguards"], "safeguards")
    routes = _id_map(contract["routes"], "routes")
    stages = _id_map(contract["stages"], "stages")
    advanced_triggers = _id_map(contract["advanced_triggers"], "advanced_triggers")
    missing_authorities = REQUIRED_AUTHORITIES - set(authorities)
    if missing_authorities:
        raise ContractError(
            f"authority contract missing root-controller/native-host: {sorted(missing_authorities)}"
        )
    missing_safeguards = REQUIRED_SAFEGUARDS - set(safeguards)
    if missing_safeguards:
        raise ContractError(f"safeguard contract missing: {sorted(missing_safeguards)}")
    if set(routes) != REQUIRED_ROUTES:
        raise ContractError(
            "route contract mismatch: "
            f"missing={sorted(REQUIRED_ROUTES - set(routes))} "
            f"extra={sorted(set(routes) - REQUIRED_ROUTES)}"
        )
    actual_advanced_triggers = tuple(advanced_triggers)
    if actual_advanced_triggers != REQUIRED_ADVANCED_TRIGGERS:
        expected = set(REQUIRED_ADVANCED_TRIGGERS)
        actual = set(actual_advanced_triggers)
        raise ContractError(
            "advanced trigger contract mismatch: "
            f"missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)} "
            f"order={list(actual_advanced_triggers)}"
        )
    for identifier, trigger in advanced_triggers.items():
        if set(trigger) != {"id", "when", "activates"}:
            raise ContractError(
                f"advanced trigger requires only id, when, and activates: {identifier}"
            )
        if not all(
            isinstance(trigger[field], str) and trigger[field].strip()
            for field in ("id", "when", "activates")
        ):
            raise ContractError(
                f"advanced trigger fields must be non-empty strings: {identifier}"
            )

    dependencies = contract["dependencies"]
    if not isinstance(dependencies, list):
        raise ContractError("dependencies must be a list")
    pairs: set[tuple[str, str]] = set()
    for item in dependencies:
        if not isinstance(item, dict) or set(item) != {"before", "after"}:
            raise ContractError("dependency requires only before and after")
        before = item.get("before")
        after = item.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            raise ContractError("dependency endpoints must be strings")
        if before not in stages or after not in stages:
            raise ContractError(
                f"dependency references missing stage: {before} -> {after}"
            )
        pairs.add((before, after))
    missing_dependencies = REQUIRED_DEPENDENCIES - pairs
    if missing_dependencies:
        rendered = ", ".join(f"{a}->{b}" for a, b in sorted(missing_dependencies))
        raise ContractError(f"dependency contract missing: {rendered}")
    if "quality-gate" not in stages:
        raise ContractError("quality-gate stage is required by the delivery gate")
    _assert_acyclic(stages, pairs)

    if not isinstance(contract["commands"], dict) or not contract["commands"]:
        raise ContractError("commands must be a non-empty object")
    if not isinstance(contract["output_labels"], dict) or not contract["output_labels"]:
        raise ContractError("output_labels must be a non-empty object")
    if not isinstance(contract["handoff_obligations"], list) or not contract["handoff_obligations"]:
        raise ContractError("handoff_obligations must be a non-empty list")

    views = contract["generated_views"]
    if not isinstance(views, list) or not views:
        raise ContractError("generated_views must be a non-empty list")
    identities: set[str] = set()
    for view in views:
        if not isinstance(view, dict) or set(view) != {"path", "blocks"}:
            raise ContractError("generated view requires only path and blocks")
        relative = _safe_relative(view.get("path"))
        blocks = view.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ContractError(f"generated view has no blocks: {relative}")
        for block in blocks:
            if not isinstance(block, dict) or set(block) != {"id", "renderer"}:
                raise ContractError(
                    f"generated block requires only id and renderer: {relative}"
                )
            block_id = block.get("id")
            renderer = block.get("renderer")
            if not isinstance(block_id, str) or not block_id.strip():
                raise ContractError(f"generated block id is invalid: {relative}")
            if not isinstance(renderer, str) or renderer not in RENDERERS:
                raise ContractError(
                    f"unknown generated block renderer {renderer!r}: {relative}::{block_id}"
                )
            identity = f"{relative.as_posix()}::{block_id}"
            if identity in identities:
                raise ContractError(f"duplicate generated block: {identity}")
            identities.add(identity)
    return contract


def _safe_relative(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("generated view path must be a string")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ContractError(f"generated view path must stay inside repository: {value}")
    return path


def _assert_acyclic(stages: dict[str, Any], pairs: set[tuple[str, str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()
    edges: dict[str, list[str]] = {stage: [] for stage in stages}
    for before, after in pairs:
        edges[before].append(after)

    def visit(stage: str) -> None:
        if stage in visiting:
            raise ContractError(f"workflow dependency cycle includes: {stage}")
        if stage in visited:
            return
        visiting.add(stage)
        for after in edges[stage]:
            visit(after)
        visiting.remove(stage)
        visited.add(stage)

    for stage in stages:
        visit(stage)


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def render_overview(contract: dict[str, Any]) -> str:
    authority_rows = [
        [item["owner"], item["owns"], item["excludes"]]
        for item in contract["authorities"]
    ]
    safeguards = "\n".join(
        f"- `{item['id']}`: {item['rule']}" for item in contract["safeguards"]
    )
    return (
        "## Workflow Authority\n\n"
        + _markdown_table(["Authority", "Owns", "Does not own"], authority_rows)
        + "\n\n## Non-negotiable Safeguards\n\n"
        + safeguards
    )


def render_routes(contract: dict[str, Any]) -> str:
    return (
        "## Skill Routes\n\n"
        + _markdown_table(
            ["Skill", "Use when", "Added obligation"],
            [
                [f"`{item['id']}`", item["when"], item["obligation"]]
                for item in contract["routes"]
            ],
        )
    )


def _stage_lines(contract: dict[str, Any]) -> str:
    return "\n".join(
        f"{index}. **{item['label']}** (`{item['id']}`): {item['action']}"
        for index, item in enumerate(contract["stages"], start=1)
    )


def _dependency_lines(contract: dict[str, Any]) -> str:
    return "\n".join(
        f"- `{item['before']}` → `{item['after']}`"
        for item in contract["dependencies"]
    )


def _command_block(contract: dict[str, Any]) -> str:
    return "\n".join(contract["commands"].values())


def render_happy_path(contract: dict[str, Any]) -> str:
    return (
        "## Verified Patch Happy Path\n\n"
        "The delivery plan creates only the linked planning graph. Baseline confirmation, "
        "controller verification, task acceptance, quality review, readiness, and delivery remain explicit.\n\n"
        + _stage_lines(contract)
        + "\n\nDependency edges:\n\n"
        + _dependency_lines(contract)
        + "\n\n```bash\n"
        + _command_block(contract)
        + "\n```\n\n"
        "`verified-patch` reuses the immutable `verify run` transaction only. It always reports "
        "task, gate, and delivery status and never creates a Host lifecycle or passing gate."
    )


def render_skill_entry(contract: dict[str, Any]) -> str:
    routes = render_routes(contract).replace("## Skill Routes", "### Route")
    safeguards = "\n".join(
        f"- **{item['id']}** — {item['rule']}" for item in contract["safeguards"]
    )
    advanced_index = "\n".join(
        f"- `{item['id']}` — {item['when']}"
        + (
            f"; activates {item['activates']}"
            if item["id"] == "deep-kernel-review"
            else ""
        )
        for item in contract["advanced_triggers"]
    )
    return (
        "## Canonical Workflow Contract\n\n"
        "OpenSpec is the specification authority; Kafa SQLite is the delivery-fact authority; "
        "`core.delivery.evaluate_delivery_prerequisites` is gate authority; Native Codex/ChatGPT "
        "owns collaboration lifecycle. Only the root controller writes Kafa delivery facts. "
        "This generated block is presentation guidance and cannot relax runtime eligibility.\n\n"
        + safeguards
        + "\n\n"
        + routes
        + "\n\n### Advanced Trigger Index\n\n"
        + advanced_index
        + "\n\nThis compact index selects obligations without loading the full delegation "
        "matrix. See [`docs/TRIGGER_MATRIX.md`](../../docs/TRIGGER_MATRIX.md) "
        "for the generated full definitions."
        + "\n\n### Stage Dependencies\n\n"
        + _dependency_lines(contract)
        + "\n\nTask submission and controller verification may occur in either order; both must "
        "finish before task acceptance."
    )


def render_trigger_matrix(contract: dict[str, Any]) -> str:
    route_table = _markdown_table(
        ["Skill", "Trigger", "Obligation"],
        [
            [f"`{item['id']}`", item["when"], item["obligation"]]
            for item in contract["routes"]
        ],
    )
    advanced_table = _markdown_table(
        ["Advanced mode", "Trigger", "Activates"],
        [
            [f"`{item['id']}`", item["when"], item["activates"]]
            for item in contract["advanced_triggers"]
        ],
    )
    return (
        "# Trigger Matrix\n\n"
        + route_table
        + "\n\n## Advanced Modes\n\n"
        + advanced_table
        + "\n\nA user's explicit request for a named advanced mode also activates it. "
        "Explanation, translation, and supplied-text summary do not initialize Kafa. "
        "Deployment, production release, external SaaS actions, and Native Host lifecycle "
        "remain outside the local runtime."
    )


def render_advanced_skill_trigger(
    contract: dict[str, Any], trigger_id: str
) -> str:
    trigger = next(
        item for item in contract["advanced_triggers"] if item["id"] == trigger_id
    )
    return (
        "## Trigger (Non-Default)\n\n"
        f"Trigger when: {trigger['when']}\n\n"
        f"Activates: {trigger['activates']}\n\n"
        "This Skill is not part of the default small single-producer path. Once "
        "triggered, its complete evidence obligations remain active. If a required "
        "check is blocked, skipped, not-run, or unavailable, report that exact state; "
        "a fixture cannot substitute for required live evidence."
    )


def render_harness_audit_trigger(contract: dict[str, Any]) -> str:
    return render_advanced_skill_trigger(contract, "harness-audit")


def render_project_retrospective_trigger(contract: dict[str, Any]) -> str:
    return render_advanced_skill_trigger(contract, "project-retrospective")


def render_full_flow(contract: dict[str, Any]) -> str:
    return (
        "# Full Local Delivery Flow\n\n"
        "This appendix expands the same contract used by the overview, quickstart, and Skill. "
        "It is an example, not a second policy source. The schema 31 runtime is local-only; "
        "Native Codex/ChatGPT owns collaboration lifecycle and the root controller is the sole "
        "Kafa writer. `verified-patch` reuses immutable `verify run` evidence and stops before "
        "deployment or release.\n\n"
        "## Stages\n\n"
        + _stage_lines(contract)
        + "\n\n## Required Ordering\n\n"
        + _dependency_lines(contract)
        + "\n\n## Command Skeleton\n\n```bash\n"
        + _command_block(contract)
        + "\n```\n\n## Handoff\n\n"
        + "\n".join(f"- {item}" for item in contract["handoff_obligations"])
    )


def render_skill_eval_prompts(contract: dict[str, Any]) -> str:
    route_prompts = "\n".join(
        f"- Confirm `{item['id']}` routes work described as: {item['when']}"
        for item in contract["routes"]
    )
    advanced_prompts = [
        "- Small single-producer work: expect no advanced trigger and do not load the full delegation matrix."
    ]
    advanced_prompts.extend(
        f"- Scenario `{item['id']}`: when {item['when']}; expect `{item['id']}` "
        f"and {item['activates']}."
        for item in contract["advanced_triggers"]
    )
    return (
        "# Fresh Skill Evaluation Prompts\n\n"
        "Use a fresh context. Require exact boundary language and refuse to treat skipped, "
        "blocked, not-run, fixture-only, or zero-count evidence as pass.\n\n"
        "## Route Checks\n\n"
        + route_prompts
        + "\n\n## Advanced Trigger Scenarios\n\n"
        + "\n".join(advanced_prompts)
        + "\n\n## Dependency Checks\n\n"
        + _dependency_lines(contract)
        + "\n\n## Command Checks\n\n```bash\n"
        + _command_block(contract)
        + "\n```\n\n## Handoff Checks\n\n"
        + "\n".join(f"- {item}" for item in contract["handoff_obligations"])
        + "\n\n## Result Contract\n\n"
        "A live Host evaluator must return `source: host-evaluated` followed by "
        "the exact ordered contract lines and closed `scenario-verdict` records; "
        "no extra prose, unknown scenario, contradiction, or fixture source is accepted. "
        "The generated local transcript uses `source: fixture-only` and is never fresh "
        "Host evidence."
        + "\n\nHigh/critical work without independent current-candidate provenance must "
        + f"remain `{contract['output_labels']['human_review_required']}`."
    )


def render_skill_eval_transcript(contract: dict[str, Any]) -> str:
    lines = [
        "source: fixture-only",
        "evaluation: Kafa workflow contract",
        "authority: OpenSpec -> Kafa SQLite -> evaluate_delivery_prerequisites",
        "host: Native Codex/ChatGPT owns task/subagent/worktree/model/cancel/handoff",
        "writer: root controller only",
        "scenario-verdict: id=small-single-producer; selected=none; result=pass",
    ]
    lines.extend(
        "scenario-verdict: "
        f"id={item['id']}; selected={item['id']}; result=pass; "
        f"when={item['when']}; activates={item['activates']}"
        for item in contract["advanced_triggers"]
    )
    lines.extend(
        f"dependency: {item['before']} -> {item['after']}"
        for item in contract["dependencies"]
    )
    lines.extend(f"$ {command}" for command in contract["commands"].values())
    lines.extend(f"handoff: {item}" for item in contract["handoff_obligations"])
    lines.append(contract["output_labels"]["human_review_required"])
    return "\n".join(lines)


RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "overview": render_overview,
    "routes": render_routes,
    "happy-path": render_happy_path,
    "skill-entry": render_skill_entry,
    "trigger-matrix": render_trigger_matrix,
    "harness-audit-trigger": render_harness_audit_trigger,
    "project-retrospective-trigger": render_project_retrospective_trigger,
    "full-flow": render_full_flow,
    "skill-eval-prompts": render_skill_eval_prompts,
    "skill-eval-transcript": render_skill_eval_transcript,
}


def markers(block_id: str) -> tuple[str, str]:
    return (
        f"<!-- BEGIN GENERATED: workflow-contract:{block_id} -->",
        f"<!-- END GENERATED: workflow-contract:{block_id} -->",
    )


def expected_file(
    current: str,
    blocks: list[dict[str, Any]],
    contract: dict[str, Any],
) -> tuple[str, list[str]]:
    result = current.replace("\r\n", "\n").replace("\r", "\n")
    drifted: list[str] = []
    for block in blocks:
        block_id = block["id"]
        begin, end = markers(block_id)
        content = RENDERERS[block["renderer"]](contract).strip()
        replacement = f"{begin}\n{content}\n{end}"
        begin_count = result.count(begin)
        end_count = result.count(end)
        if begin_count != end_count or begin_count > 1:
            raise ContractError(
                f"generated block markers invalid: block={block_id} "
                f"begin={begin_count} end={end_count}"
            )
        if begin_count == 0:
            separator = "" if not result else ("\n" if result.endswith("\n") else "\n\n")
            result = result + separator + replacement + "\n"
            drifted.append(block_id)
            continue
        start = result.index(begin)
        finish = result.index(end, start) + len(end)
        if result[start:finish] != replacement:
            drifted.append(block_id)
        result = result[:start] + replacement + result[finish:]
    if result and not result.endswith("\n"):
        result += "\n"
    return result, drifted


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def run(root: Path, *, write: bool) -> int:
    try:
        contract = load_contract(root)
        drift: list[tuple[str, str]] = []
        publications: list[tuple[Path, str]] = []
        for view in contract["generated_views"]:
            relative = _safe_relative(view["path"])
            path = root / relative
            if not path.is_file():
                raise ContractError(f"generated view is missing: {relative.as_posix()}")
            current = path.read_text(encoding="utf-8")
            expected, block_drift = expected_file(current, view["blocks"], contract)
            publications.append((path, expected))
            drift.extend((relative.as_posix(), block_id) for block_id in block_drift)
        if write:
            for path, expected in publications:
                if path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n") != expected:
                    atomic_write(path, expected)
            print(f"OK: rendered {sum(len(view['blocks']) for view in contract['generated_views'])} workflow block(s)")
            return 0
        if drift:
            for path, block_id in drift:
                print(f"ERROR: workflow view drift: file={path} block={block_id}")
            return 1
        print("OK: workflow views match workflow-contract.json")
        return 0
    except (ContractError, OSError, UnicodeError) as exc:
        print(f"ERROR: {exc}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return run(Path.cwd().resolve(), write=bool(args.write))


if __name__ == "__main__":
    raise SystemExit(main())
