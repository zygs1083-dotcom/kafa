#!/usr/bin/env python3
"""Measure local-core slimming without turning wall-clock timings into gates."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db  # noqa: E402
from core.projections import PROJECTION_NAMES, render_affected, render_all  # noqa: E402


BASELINE_SOURCE = "docs/audits/2026-07-11-local-core-slimming-baseline.md:58"
BASELINE = {
    "schema_version": 29,
    "init_seconds": 0.31,
    "empty_db_bytes": 552_960,
    "fact_count": 5_000,
    "single_mutation_seconds": 0.146113,
    "targeted_projection_seconds": None,
    "targeted_projection_status": "not-recorded",
    "full_test_seconds": 406.72,
    "full_test_count": 370,
    "full_test_status": "passed",
}
TARGETED_REQUIREMENT_PROJECTIONS = (
    "project-state",
    "requirements",
    "traceability",
)


def timing_summary(samples: list[float]) -> dict[str, object]:
    return {
        "samples_seconds": [round(sample, 6) for sample in samples],
        "median_seconds": round(statistics.median(samples), 6),
        "sample_count": len(samples),
    }


def measure(callback: Callable[[], None], repetitions: int) -> dict[str, object]:
    samples: list[float] = []
    for _ in range(repetitions):
        start = time.perf_counter()
        callback()
        samples.append(time.perf_counter() - start)
    return timing_summary(samples)


def git_metadata() -> dict[str, object]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "head": head.stdout.strip() if head.returncode == 0 else "unavailable",
        "working_tree_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def benchmark_init(repetitions: int) -> tuple[dict[str, object], dict[str, object]]:
    timing_samples: list[float] = []
    sizes: list[int] = []
    for _ in range(repetitions):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            start = time.perf_counter()
            result = subprocess.run(
                [sys.executable, str(HARNESS), "--root", str(root), "init"],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            timing_samples.append(time.perf_counter() - start)
            if result.returncode != 0:
                raise RuntimeError(
                    "schema 30 init benchmark failed: " + (result.stdout + result.stderr).strip()
                )
            sizes.append((root / ".ai-team/state/harness.db").stat().st_size)
    return timing_summary(timing_samples), {
        "samples_bytes": sizes,
        "median_bytes": int(statistics.median(sizes)),
        "sample_count": len(sizes),
    }


def seed_local_facts(root: Path, fact_count: int) -> None:
    created_at = "2026-07-11T00:00:00Z"
    with harness_db.connection(root) as conn:
        conn.executemany(
            "insert into decisions (id, decision, reason, created_at) values (?, ?, ?, ?)",
            [
                (f"BENCH-{index}", f"Historical decision {index}", "benchmark seed", created_at)
                for index in range(fact_count)
            ],
        )
        conn.commit()


def benchmark_mutation_and_projections(
    fact_count: int,
    repetitions: int,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        harness_db.init_runtime(root)
        seed_local_facts(root, fact_count)
        mutation_counter = 0

        def mutate_requirement() -> None:
            nonlocal mutation_counter
            mutation_counter += 1
            harness_db.add_requirement(
                root,
                "R-BENCH",
                "functional",
                f"Targeted mutation {mutation_counter}",
            )

        mutation = measure(mutate_requirement, repetitions)
        targeted = measure(
            lambda: render_affected(root, TARGETED_REQUIREMENT_PROJECTIONS),
            repetitions,
        )
        full = measure(lambda: render_all(root), repetitions)

    mutation.update({"fact_count": fact_count, "projection_count": 3})
    targeted.update(
        {
            "projection_count": len(TARGETED_REQUIREMENT_PROJECTIONS),
            "projections": list(TARGETED_REQUIREMENT_PROJECTIONS),
        }
    )
    full.update(
        {
            "projection_count": len(PROJECTION_NAMES),
            "projections": list(PROJECTION_NAMES),
        }
    )
    return mutation, targeted, full


def comparison(baseline: float | int | None, current: float | int | None) -> dict[str, object]:
    if baseline is None or current is None:
        return {
            "status": "not-comparable",
            "baseline": baseline,
            "current": current,
            "delta": None,
            "current_over_baseline": None,
        }
    return {
        "status": "measured",
        "baseline": baseline,
        "current": current,
        "delta": round(float(current) - float(baseline), 6),
        "current_over_baseline": round(float(current) / float(baseline), 6)
        if baseline
        else None,
    }


def build_report(
    *,
    fact_count: int = 5_000,
    repetitions: int = 5,
    test_duration_seconds: float | None = None,
    test_count: int | None = None,
    test_status: str = "not-run",
) -> dict[str, object]:
    if fact_count < 1:
        raise ValueError("fact_count must be positive")
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    if test_duration_seconds is None:
        test_status = "not-run"
        test_count = None
    elif test_status not in {"passed", "failed"}:
        raise ValueError("measured test duration requires test_status passed or failed")

    init_timing, db_size = benchmark_init(repetitions)
    mutation, targeted, full = benchmark_mutation_and_projections(
        fact_count,
        repetitions,
    )
    test_time = {
        "status": test_status,
        "seconds": round(test_duration_seconds, 6)
        if test_duration_seconds is not None
        else None,
        "test_count": test_count,
        "command": "python3 -W error::ResourceWarning -m unittest discover -s tests -p test_*.py",
    }
    current = {
        "schema_version": harness_db.SCHEMA_VERSION,
        "runtime_version": harness_db.RUNTIME_VERSION,
        "init": init_timing,
        "empty_db": db_size,
        "single_mutation_after_local_facts": mutation,
        "targeted_projection": targeted,
        "full_projection": full,
        "full_test": test_time,
    }
    return {
        "report_version": 1,
        "benchmark_kind": "comparative-report-only",
        "timing_assertions": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            **git_metadata(),
        },
        "baseline_source": BASELINE_SOURCE,
        "baseline": BASELINE,
        "schema30": current,
        "comparison": {
            "init_seconds": comparison(
                BASELINE["init_seconds"], init_timing["median_seconds"]
            ),
            "empty_db_bytes": comparison(
                BASELINE["empty_db_bytes"], db_size["median_bytes"]
            ),
            "single_mutation_seconds": comparison(
                BASELINE["single_mutation_seconds"], mutation["median_seconds"]
            ),
            "targeted_projection_seconds": comparison(
                BASELINE["targeted_projection_seconds"],
                targeted["median_seconds"],
            ),
            "full_test_seconds": comparison(
                BASELINE["full_test_seconds"], test_time["seconds"]
            ),
            "current_full_over_targeted_projection": comparison(
                targeted["median_seconds"], full["median_seconds"]
            ),
        },
        "notes": [
            "Wall-clock values are evidence, not CI pass/fail thresholds.",
            "Baseline targeted projection timing was not recorded and is reported as not-comparable.",
            "A not-run full test is not a pass; inject task 11.3 wall time after the suite completes.",
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=int, default=5_000)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--test-duration-seconds", type=float)
    parser.add_argument("--test-count", type=int)
    parser.add_argument(
        "--test-status",
        choices=["not-run", "passed", "failed"],
        default="not-run",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        fact_count=args.facts,
        repetitions=args.samples,
        test_duration_seconds=args.test_duration_seconds,
        test_count=args.test_count,
        test_status=args.test_status,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"OK: local-core benchmark report written to {args.out}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
