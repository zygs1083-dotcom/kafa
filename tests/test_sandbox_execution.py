from __future__ import annotations

import hashlib
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / "plugins/codex-project-harness/scripts/harness.py"
PLUGIN_ROOT = REPO_ROOT / "plugins/codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import harness  # noqa: E402
from core.execution import (  # noqa: E402
    CommandResult,
    ContainerExecutor,
    ContainerImageProvenance,
    controller_runtime_provenance,
)


TEST_CONTAINER_DIGEST = "sha256:" + "d" * 64
TEST_CONTAINER_ENDPOINT = "unix:///var/run/docker.sock"


def run_harness(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result


def run_cli_in_process(root: Path, *args: str) -> tuple[int, str]:
    output = io.StringIO()
    argv = [str(HARNESS), "--root", str(root), *args]
    with mock.patch.object(sys, "argv", argv), redirect_stdout(output):
        return harness.main(), output.getvalue()


def git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    (root / "test_sample.py").write_text(
        "import unittest\n\nclass Sample(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "test_sample.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


def register_target(
    root: Path,
    *,
    container_image: str = "",
    stack_profile: str = "python",
) -> None:
    run_harness(root, "init")
    run_harness(root, "acceptance", "add", "--id", "AC1", "--criterion", "sandboxed tests pass")
    args = [
        "test-target",
        "add",
        "--id",
        "UNIT",
        "--kind",
        "unit",
        "--command-template",
        "python3 -B -m unittest test_sample.py",
        "--stack-profile",
        stack_profile,
        "--requires-sandbox",
        "--requires-no-network",
    ]
    if container_image:
        args.extend(["--container-image", container_image])
    run_harness(root, *args)
    run_harness(
        root,
        "test-target",
        "qualify",
        "--id",
        "UNIT-Q1",
        "--target",
        "UNIT",
        "--acceptance",
        "AC1",
        "--rationale",
        "UNIT is the procedural verification target for AC1",
        "--by",
        "test-fixture",
    )


def fact_counts(root: Path) -> tuple[int, int, int]:
    with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
        return tuple(
            int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            for table in ("executions", "validations", "validation_executions")
        )


def successful_container_result(
    executor: ContainerExecutor,
    command: str,
    *,
    target_id: str,
    result_format: str,
    ordinal: int,
    target_definition_sha256: str,
    container_image: str,
) -> CommandResult:
    payload = b"Ran 1 test in 0.001s\n\nOK\n"
    artifact = (
        executor.root
        / ".ai-team"
        / "runtime"
        / "executions"
        / f"container-test-{ordinal}"
        / "stdout.txt"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(payload)
    runtime = controller_runtime_provenance(target_definition_sha256)
    return CommandResult(
        command=command,
        exit_code=0,
        stdout_sha256=hashlib.sha256(payload).hexdigest(),
        artifact_path=artifact.relative_to(executor.root).as_posix(),
        target_id=target_id,
        executed_count=1,
        executed_count_source="parsed",
        result_format=result_format,
        semantic_status="pass",
        no_network=True,
        sandbox_profile="no-network",
        sandbox_status="available",
        policy_status="allowed",
        policy_reason=f"target {target_id}",
        target_definition_sha256=runtime.target_definition_sha256,
        platform=runtime.platform,
        runtime_executable=runtime.runtime_executable,
        runtime_version=runtime.runtime_version,
        runtime_executable_sha256=runtime.runtime_executable_sha256,
        policy_version=runtime.policy_version,
        container_engine="/usr/bin/docker",
        container_engine_version="25.0.0",
        container_engine_endpoint=TEST_CONTAINER_ENDPOINT,
        container_image_requested=container_image,
        container_image_digest=TEST_CONTAINER_DIGEST,
        provenance_status="complete",
    )


def fixed_container_provenance(
    requested_image: str,
    *,
    expected_engine: str = "",
    expected_endpoint: str = "",
) -> ContainerImageProvenance:
    if expected_engine and expected_engine != "/usr/bin/docker":
        raise AssertionError(expected_engine)
    if expected_endpoint and expected_endpoint != TEST_CONTAINER_ENDPOINT:
        raise AssertionError(expected_endpoint)
    return ContainerImageProvenance(
        engine="/usr/bin/docker",
        engine_version="25.0.0",
        requested_image=requested_image,
        image_digest=TEST_CONTAINER_DIGEST,
        engine_endpoint=TEST_CONTAINER_ENDPOINT,
    )


class SandboxExecutionTest(unittest.TestCase):
    def test_cli_renders_exception_notes_for_manual_review(self) -> None:
        error = harness.HarnessError("unsafe-project-path: target.txt")
        error.add_note("complete metadata rollback requires manual review")

        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(
                harness,
                "projection_rebuild",
                side_effect=error,
            ),
        ):
            returncode, output = run_cli_in_process(
                Path(temp),
                "projection",
                "rebuild",
            )

        self.assertEqual(returncode, 1)
        self.assertIn("ERROR: unsafe-project-path: target.txt", output)
        self.assertIn(
            "NOTE: complete metadata rollback requires manual review",
            output,
        )

    def test_container_verify_run_fails_closed_when_engine_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            register_target(root)

            with mock.patch("core.execution.shutil.which", return_value=None):
                returncode, output = run_cli_in_process(
                    root,
                    "verify",
                    "run",
                    "--target",
                    "UNIT",
                    "--acceptance",
                    "AC1",
                    "--runner",
                    "container",
                )

            self.assertEqual(returncode, 1)
            self.assertIn("sandbox-unavailable", output)
            self.assertEqual(fact_counts(root), (0, 0, 0))

    def test_container_verify_run_records_one_immutable_no_network_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            register_target(root, container_image="python:3.12-test")
            observed_images: list[str] = []

            def fake_run(
                executor: ContainerExecutor,
                command: str,
                *,
                target_id: str,
                target_command_template: str,
                container_image: str,
                result_format: str,
                result_path: str,
                target_definition_sha256: str,
                **_kwargs: object,
            ) -> CommandResult:
                self.assertEqual(command, target_command_template)
                self.assertEqual(result_path, "")
                observed_images.append(container_image)
                return successful_container_result(
                    executor,
                    command,
                    target_id=target_id,
                    result_format=result_format,
                    ordinal=len(observed_images),
                    target_definition_sha256=target_definition_sha256,
                    container_image=container_image,
                )

            with (
                mock.patch.object(
                    ContainerExecutor,
                    "run",
                    autospec=True,
                    side_effect=fake_run,
                ),
                mock.patch(
                    "core.execution.resolve_container_image_provenance",
                    side_effect=fixed_container_provenance,
                ),
            ):
                returncode, output = run_cli_in_process(
                    root,
                    "verify",
                    "run",
                    "--target",
                    "UNIT",
                    "--acceptance",
                    "AC1",
                    "--runner",
                    "container",
                )

            self.assertEqual(returncode, 0, output)
            self.assertIn("OK: verification recorded execution=", output)
            self.assertEqual(observed_images, ["python:3.12-test"])
            self.assertEqual(fact_counts(root), (1, 1, 1))

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                execution = conn.execute(
                    """
                    select id, runner, sandbox_status, no_network, policy_status,
                           semantic_status, executed_count
                    from executions
                    """
                ).fetchone()
                link = conn.execute(
                    "select execution_id from validation_executions"
                ).fetchone()
                self.assertIsNotNone(execution)
                self.assertEqual(execution[1:], ("container", "available", 1, "allowed", "pass", 1))
                self.assertEqual(link, (execution[0],))
                with self.assertRaisesRegex(sqlite3.DatabaseError, "executions are immutable"):
                    conn.execute(
                        "update executions set sandbox_status = 'unavailable' where id = ?",
                        (execution[0],),
                    )
                preserved = conn.execute(
                    "select sandbox_status, no_network from executions where id = ?",
                    (execution[0],),
                ).fetchone()
                self.assertEqual(preserved, ("available", 1))

    def test_container_image_precedence_uses_target_then_stack_profile_for_qualified_runs(self) -> None:
        cases = (
            ("target image", "python:target", "python", "", "python:target"),
            ("stack profile", "", "node", "", "node:22-bookworm-slim"),
        )
        for name, target_image, stack_profile, cli_image, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                git_repo(root)
                register_target(
                    root,
                    container_image=target_image,
                    stack_profile=stack_profile,
                )
                with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                    recorded_profile = conn.execute(
                        "select stack_profile from test_targets where id = 'UNIT'"
                    ).fetchone()[0]
                self.assertEqual(recorded_profile, stack_profile)
                observed: list[str] = []

                def fake_run(
                    executor: ContainerExecutor,
                    command: str,
                    *,
                    target_id: str,
                    container_image: str,
                    result_format: str,
                    target_definition_sha256: str,
                    **_kwargs: object,
                ) -> CommandResult:
                    observed.append(container_image)
                    return successful_container_result(
                        executor,
                        command,
                        target_id=target_id,
                        result_format=result_format,
                        ordinal=1,
                        target_definition_sha256=target_definition_sha256,
                        container_image=container_image,
                    )

                args = [
                    "verify",
                    "run",
                    "--target",
                    "UNIT",
                    "--acceptance",
                    "AC1",
                    "--runner",
                    "container",
                ]
                if cli_image:
                    args.extend(["--container-image", cli_image])
                with (
                    mock.patch.object(
                        ContainerExecutor,
                        "run",
                        autospec=True,
                        side_effect=fake_run,
                    ),
                    mock.patch(
                        "core.execution.resolve_container_image_provenance",
                        side_effect=fixed_container_provenance,
                    ),
                ):
                    returncode, output = run_cli_in_process(root, *args)

                self.assertEqual(returncode, 0, output)
                self.assertEqual(observed, [expected])

    def test_qualified_container_verify_rejects_cli_image_that_differs_from_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            git_repo(root)
            register_target(root, container_image="python:target")

            result = run_harness(
                root,
                "verify",
                "run",
                "--target",
                "UNIT",
                "--acceptance",
                "AC1",
                "--runner",
                "container",
                "--container-image",
                "python:cli",
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot override the qualified container image", result.stdout + result.stderr)
            self.assertEqual(fact_counts(root), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
