from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core import delivery as delivery_policy  # noqa: E402
from core import execution as execution_policy  # noqa: E402
from core.execution import ContainerExecutor, ExecutionPolicyError  # noqa: E402
from core.projections import render_executions  # noqa: E402
import harness_db  # noqa: E402


TARGET_DIGEST = "a" * 64
IMAGE_DIGEST_A = "sha256:" + "b" * 64
IMAGE_DIGEST_B = "sha256:" + "c" * 64
REAL_SUBPROCESS_RUN = subprocess.run


def db_path(root: Path) -> Path:
    return root / ".ai-team/state/harness.db"


def write_local_test(root: Path) -> None:
    (root / "test_provenance.py").write_text(
        "import unittest\n\n"
        "class ProvenanceTest(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )


def initialize_local_target(
    root: Path,
    *,
    container: bool = False,
    image: str = "python:3.12-test",
) -> None:
    write_local_test(root)
    harness_db.init_runtime(root)
    harness_db.add_acceptance(root, "AC1", "controller test passes")
    harness_db.add_test_target(
        root,
        "UNIT",
        "unit",
        "python3 -B -m unittest test_provenance.py",
        "provenance target",
        container_image=image if container else "",
        requires_sandbox=container,
        requires_no_network=container,
    )
    harness_db.qualify_test_target(
        root,
        "Q1",
        "UNIT",
        "AC1",
        "UNIT directly exercises AC1",
        "test-controller",
    )


def fact_counts(root: Path) -> tuple[int, int, int]:
    with closing(sqlite3.connect(db_path(root))) as conn:
        return tuple(
            int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            for table in ("executions", "validations", "validation_executions")
        )


def complete_execution_object(
    *,
    runner: str = "container",
    engine: str = "/usr/bin/docker",
    endpoint: str = "unix:///var/run/docker.sock",
) -> dict[str, object]:
    container = runner == "container"
    return {
        "id": "EX-COMPLETE",
        "cycle_id": "CYCLE-current",
        "candidate_sha": "a" * 64,
        "target_id": "UNIT",
        "target_definition_sha256": "b" * 64,
        "command": "true",
        "exit_code": 0,
        "stdout_sha256": "c" * 64,
        "artifact_path": ".ai-team/runtime/executions/EX-COMPLETE/stdout.txt",
        "executed_count": 1,
        "result_format": "pytest-json",
        "semantic_status": "pass",
        "runner": runner,
        "sandbox_status": "available" if container else "",
        "no_network": 1 if container else 0,
        "policy_status": "allowed",
        "platform": "test-platform",
        "runtime_executable": "/usr/bin/python3",
        "runtime_version": "3.14",
        "runtime_executable_sha256": "d" * 64,
        "policy_version": execution_policy.EXECUTION_POLICY_VERSION,
        "container_engine": engine if container else "",
        "container_engine_version": "25.0.0" if container else "",
        "container_engine_endpoint": endpoint if container else "",
        "container_image_requested": "python:test" if container else "",
        "container_image_digest": IMAGE_DIGEST_A if container else "",
        "provenance_status": "complete",
        "created_at": "2026-07-21T00:00:00Z",
    }


def image_inspect_payload(digest: str) -> str:
    return json.dumps(
        [
            {
                "Id": digest,
                "RepoDigests": [f"python@{digest}"],
            }
        ]
    )


def logical_engine_args(command: list[str]) -> list[str]:
    if command[1:2] == ["--host"]:
        return command[3:]
    if command[1:2] == ["--remote=false"]:
        return command[2:]
    return command[1:]


def container_fake(
    observed: list[list[str]],
    *,
    image_digests: list[str] | None = None,
    engine_versions: list[str] | None = None,
    engine_endpoints: list[str] | None = None,
    structured_stdout: str = "",
):
    inspect_values = list(image_digests or [IMAGE_DIGEST_A])
    version_values = list(engine_versions or ["25.0.0"])
    endpoint_values = list(engine_endpoints or ["unix:///var/run/docker.sock"])
    inspect_index = 0
    version_index = 0
    endpoint_index = 0

    def fake(argv, **kwargs):
        nonlocal inspect_index, version_index, endpoint_index
        command = [str(value) for value in argv]
        observed.append(command)
        logical = logical_engine_args(command)
        if logical[:2] == ["context", "show"]:
            return subprocess.CompletedProcess(command, 0, "default\n", "")
        if logical[:2] == ["context", "inspect"]:
            value = endpoint_values[min(endpoint_index, len(endpoint_values) - 1)]
            endpoint_index += 1
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    [
                        {
                            "Name": logical[-1],
                            "Endpoints": {"docker": {"Host": value}},
                        }
                    ]
                ),
                "",
            )
        if logical[:1] == ["version"]:
            value = version_values[min(version_index, len(version_values) - 1)]
            version_index += 1
            return subprocess.CompletedProcess(command, 0, value + "\n", "")
        if logical[:2] == ["image", "inspect"]:
            value = inspect_values[min(inspect_index, len(inspect_values) - 1)]
            inspect_index += 1
            return subprocess.CompletedProcess(
                command,
                0,
                image_inspect_payload(value),
                "",
            )
        if logical[:1] == ["run"]:
            artifact_mount = next(
                value for value in command if value.endswith(":/artifacts:rw")
            )
            artifact_dir = Path(artifact_mount.removesuffix(":/artifacts:rw"))
            (artifact_dir / "stdout.txt").write_text(
                structured_stdout or "Ran 1 test in 0.001s\nOK\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")
        if logical[:2] == ["rm", "-f"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command and Path(command[0]).name == "git":
            return REAL_SUBPROCESS_RUN(argv, **kwargs)
        raise AssertionError(f"unexpected container command: {command}")

    return fake


class LocalExecutionProvenanceRedTests(unittest.TestCase):
    def test_local_verify_persists_complete_controller_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_local_target(root)

            harness_db.verify_run(root, "UNIT", acceptance="AC1")

            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                execution = conn.execute("select * from executions").fetchone()
                target = conn.execute(
                    "select * from test_targets where id='UNIT'"
                ).fetchone()
            self.assertIsNotNone(execution)
            self.assertEqual(
                execution["target_definition_sha256"],
                execution_policy.target_definition_digest(dict(target)),
            )
            self.assertTrue(str(execution["platform"]).strip())
            self.assertEqual(
                Path(execution["runtime_executable"]).resolve(),
                Path(sys.executable).resolve(),
            )
            self.assertTrue(str(execution["runtime_version"]).strip())
            self.assertEqual(
                execution["runtime_executable_sha256"],
                hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
            )
            self.assertEqual(
                execution["policy_version"],
                execution_policy.EXECUTION_POLICY_VERSION,
            )
            self.assertEqual(execution["container_engine"], "")
            self.assertEqual(execution["container_image_digest"], "")
            self.assertEqual(execution["provenance_status"], "complete")

            render_executions(root)
            projection = (root / "docs/harness/executions.md").read_text(
                encoding="utf-8"
            )
            for heading in (
                "Target Definition SHA-256",
                "Platform",
                "Runtime Executable",
                "Runtime Version",
                "Runtime Executable SHA-256",
                "Policy Version",
                "Container Engine",
                "Container Engine Version",
                "Container Engine Endpoint",
                "Container Image Requested",
                "Container Image Digest",
                "Provenance Status",
            ):
                self.assertIn(heading, projection)
            self.assertIn(execution["target_definition_sha256"], projection)
            self.assertIn(execution["runtime_executable_sha256"], projection)
            self.assertIn(execution["policy_version"], projection)

    def test_each_missing_or_legacy_provenance_fact_is_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_local_target(root)
            harness_db.verify_run(root, "UNIT", acceptance="AC1")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("drop trigger executions_no_update")
                original = dict(conn.execute("select * from executions").fetchone())
                candidate = str(original["candidate_sha"])
                cases = (
                    ("target_definition_sha256", "", "target_definition_sha256"),
                    ("platform", "", "platform"),
                    ("runtime_executable", "", "runtime_executable"),
                    ("runtime_version", "", "runtime_version"),
                    (
                        "runtime_executable_sha256",
                        "",
                        "runtime_executable_sha256",
                    ),
                    ("policy_version", "", "policy_version"),
                    ("provenance_status", "legacy-incomplete", "legacy-incomplete"),
                )
                for field, value, expected in cases:
                    with self.subTest(field=field):
                        conn.execute("pragma ignore_check_constraints=on")
                        conn.execute(
                            f"update executions set {field}=? where id=?",
                            (value, original["id"]),
                        )
                        row = conn.execute(
                            "select * from executions where id=?",
                            (original["id"],),
                        ).fetchone()
                        issues = delivery_policy.execution_issues(
                            conn,
                            root,
                            row,
                            candidate,
                        )
                        self.assertIn(expected, " ".join(issues))
                        conn.execute(
                            f"update executions set {field}=? where id=?",
                            (original[field], original["id"]),
                        )
                conn.rollback()

    def test_schema_and_ddl_reject_complete_rows_with_missing_provenance(self) -> None:
        schema = json.loads(
            (PLUGIN_ROOT / "schemas/execution.schema.json").read_text(
                encoding="utf-8"
            )
        )
        properties = schema["properties"]
        for field in (
            "platform",
            "runtime_executable",
            "runtime_version",
            "policy_version",
        ):
            self.assertEqual(properties[field]["type"], "string", field)
        for field in (
            "target_definition_sha256",
            "runtime_executable_sha256",
            "container_image_digest",
        ):
            self.assertIn("pattern", properties[field], field)
        self.assertIn("pattern", properties["container_engine_endpoint"])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into executions
                        (id, cycle_id, candidate_sha, command, exit_code,
                         stdout_sha256, artifact_path, executed_count,
                         result_format, semantic_status, runner,
                         sandbox_status, no_network, policy_status,
                         provenance_status, created_at)
                        values ('EX-BAD', 'CYCLE-current', ?, 'true', 0, ?, '',
                                1, 'regex', 'pass', 'local', '', 0, 'allowed',
                                'complete', 'now')
                        """,
                        ("a" * 64, "b" * 64),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into executions
                        (id, cycle_id, candidate_sha, target_definition_sha256,
                         command, exit_code, stdout_sha256, artifact_path,
                         executed_count, result_format, semantic_status, runner,
                         sandbox_status, no_network, policy_status, platform,
                         runtime_executable, runtime_version,
                         runtime_executable_sha256, policy_version,
                         container_engine, container_engine_version,
                         container_engine_endpoint, container_image_requested,
                         container_image_digest, provenance_status, created_at)
                        values ('EX-REMOTE', 'CYCLE-current', ?, ?, 'true', 0, ?, '',
                                1, 'pytest-json', 'pass', 'container', 'available', 1,
                                'allowed', 'test-platform', '/usr/bin/python3', '3.14',
                                ?, ?, '/usr/bin/docker', '25.0.0',
                                'tcp://203.0.113.10:2375', 'python:test', ?,
                                'complete', 'now')
                        """,
                        (
                            "a" * 64,
                            "b" * 64,
                            "c" * 64,
                            "d" * 64,
                            execution_policy.EXECUTION_POLICY_VERSION,
                            IMAGE_DIGEST_A,
                        ),
                    )

        valid_object = {
            "id": "EX-REMOTE",
            "cycle_id": "CYCLE-current",
            "candidate_sha": "a" * 64,
            "target_id": "UNIT",
            "target_definition_sha256": "b" * 64,
            "command": "true",
            "exit_code": 0,
            "stdout_sha256": "c" * 64,
            "artifact_path": "",
            "executed_count": 1,
            "result_format": "pytest-json",
            "semantic_status": "pass",
            "runner": "container",
            "sandbox_status": "available",
            "no_network": 1,
            "policy_status": "allowed",
            "platform": "test-platform",
            "runtime_executable": "/usr/bin/python3",
            "runtime_version": "3.14",
            "runtime_executable_sha256": "d" * 64,
            "policy_version": execution_policy.EXECUTION_POLICY_VERSION,
            "container_engine": "/usr/bin/docker",
            "container_engine_version": "25.0.0",
            "container_engine_endpoint": "tcp://203.0.113.10:2375",
            "container_image_requested": "python:test",
            "container_image_digest": IMAGE_DIGEST_A,
            "provenance_status": "complete",
            "created_at": "now",
        }
        self.assertIn(
            "container_engine_endpoint",
            " ".join(
                harness_db.validate_object_against_schema(
                    "remote execution",
                    valid_object,
                    schema,
                )
            ),
        )

    def test_execution_json_schema_matches_runtime_status_and_complete_provenance(self) -> None:
        schema = json.loads(
            (PLUGIN_ROOT / "schemas/execution.schema.json").read_text(
                encoding="utf-8"
            )
        )
        valid = complete_execution_object()
        self.assertEqual(
            harness_db.validate_object_against_schema("execution", valid, schema),
            [],
        )

        invalid_values = {
            "result_format": "invented-format",
            "semantic_status": "invented-status",
            "sandbox_status": "invented-sandbox",
            "policy_status": "invented-policy",
        }
        for field, invalid in invalid_values.items():
            with self.subTest(field=field):
                row = dict(valid)
                row[field] = invalid
                issues = harness_db.validate_object_against_schema(
                    "execution",
                    row,
                    schema,
                )
                self.assertTrue(any(field in issue for issue in issues), issues)

        for field in (
            "target_definition_sha256",
            "platform",
            "runtime_executable",
            "runtime_version",
            "runtime_executable_sha256",
            "policy_version",
            "container_engine_version",
            "container_image_requested",
            "container_image_digest",
        ):
            with self.subTest(blank_complete_field=field):
                row = dict(valid)
                row[field] = ""
                issues = harness_db.validate_object_against_schema(
                    "execution",
                    row,
                    schema,
                )
                self.assertTrue(any(field in issue for issue in issues), issues)

        for field in (
            "platform",
            "runtime_executable",
            "runtime_version",
            "container_engine_version",
            "container_image_requested",
        ):
            with self.subTest(whitespace_complete_field=field):
                row = dict(valid)
                row[field] = "   "
                self.assertTrue(
                    any(
                        field in issue
                        for issue in execution_policy.recorded_execution_provenance_issues(
                            row
                        )
                    )
                )
                issues = harness_db.validate_object_against_schema(
                    "execution",
                    row,
                    schema,
                )
                self.assertTrue(any(field in issue for issue in issues), issues)

    def test_engine_and_endpoint_pair_is_bound_in_ddl_runtime_and_json_schema(self) -> None:
        schema = json.loads(
            (PLUGIN_ROOT / "schemas/execution.schema.json").read_text(
                encoding="utf-8"
            )
        )
        valid_pairs = (
            ("/usr/bin/docker", "unix:///var/run/docker.sock"),
            (r"C:\Program Files\Docker\Docker.exe", "npipe:////./pipe/docker_engine"),
            ("/usr/bin/Podman", "local-process"),
        )
        for engine, endpoint in valid_pairs:
            with self.subTest(valid_engine=engine, valid_endpoint=endpoint):
                row = complete_execution_object(engine=engine, endpoint=endpoint)
                self.assertEqual(
                    execution_policy.recorded_execution_provenance_issues(row),
                    [],
                )
                self.assertEqual(
                    harness_db.validate_object_against_schema(
                        "execution",
                        row,
                        schema,
                    ),
                    [],
                )
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    harness_db.init_runtime(root)
                    columns = ", ".join(row)
                    placeholders = ", ".join("?" for _ in row)
                    with closing(sqlite3.connect(db_path(root))) as conn:
                        conn.execute(
                            f"insert into executions ({columns}) values ({placeholders})",
                            tuple(row.values()),
                        )

        invalid_pairs = (
            ("/usr/bin/docker", "local-process"),
            ("/usr/bin/podman", "unix:///run/user/1000/podman.sock"),
            ("/usr/bin/remote-wrapper", "local-process"),
            ("/usr/bin/docker.wrapper", "unix:///var/run/docker.sock"),
            ("docker", "unix:///var/run/docker.sock"),
            ("/usr/bin/docker", "unix:///"),
        )
        for engine, endpoint in invalid_pairs:
            with self.subTest(engine=engine, endpoint=endpoint):
                row = complete_execution_object(engine=engine, endpoint=endpoint)
                runtime_issues = execution_policy.recorded_execution_provenance_issues(
                    row
                )
                self.assertTrue(
                    any("engine" in issue and "endpoint" in issue for issue in runtime_issues),
                    runtime_issues,
                )
                schema_issues = harness_db.validate_object_against_schema(
                    "execution",
                    row,
                    schema,
                )
                self.assertTrue(
                    any(
                        "container_engine" in issue
                        or "container_engine_endpoint" in issue
                        for issue in schema_issues
                    ),
                    schema_issues,
                )
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    harness_db.init_runtime(root)
                    columns = ", ".join(row)
                    placeholders = ", ".join("?" for _ in row)
                    with closing(sqlite3.connect(db_path(root))) as conn:
                        with self.assertRaises(sqlite3.IntegrityError):
                            conn.execute(
                                f"insert into executions ({columns}) values ({placeholders})",
                                tuple(row.values()),
                            )

    def test_doctor_contract_detects_tampered_complete_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_local_target(root)
            harness_db.verify_run(root, "UNIT", acceptance="AC1")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("drop trigger executions_no_update")
                conn.execute("pragma ignore_check_constraints=on")
                conn.execute(
                    "update executions set runtime_version='' where id=(select id from executions limit 1)"
                )
                issues = harness_db.runtime_schema_issues(conn)
            self.assertIn(
                "runtime_version",
                " ".join(issues),
            )

    def test_doctor_rejects_nonlocal_or_local_runner_endpoint_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_local_target(root)
            harness_db.verify_run(root, "UNIT", acceptance="AC1")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("drop trigger executions_no_update")
                conn.execute("pragma ignore_check_constraints=on")
                execution_id = str(
                    conn.execute("select id from executions").fetchone()[0]
                )
                conn.execute(
                    "update executions set container_engine_endpoint=? where id=?",
                    ("unix:///var/run/docker.sock", execution_id),
                )
                local_issues = harness_db.runtime_schema_issues(conn)
                self.assertIn(
                    "local execution provenance container_engine_endpoint must be empty",
                    " ".join(local_issues),
                )
                conn.execute(
                    """
                    update executions
                    set runner='container', sandbox_status='available', no_network=1,
                        container_engine='/usr/bin/docker',
                        container_engine_version='25.0.0',
                        container_engine_endpoint='tcp://203.0.113.10:2375',
                        container_image_requested='python:test',
                        container_image_digest=?
                    where id=?
                    """,
                    (IMAGE_DIGEST_A, execution_id),
                )
                remote_issues = harness_db.runtime_schema_issues(conn)
                self.assertIn(
                    "container execution provenance engine/endpoint pair is unsupported",
                    " ".join(remote_issues),
                )

    def test_runtime_provenance_drift_before_commit_creates_no_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_local_target(root)
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                target_digest = execution_policy.target_definition_digest(
                    dict(
                        conn.execute(
                            "select * from test_targets where id='UNIT'"
                        ).fetchone()
                    )
                )
            current = execution_policy.controller_runtime_provenance(target_digest)
            drifted = replace(
                current,
                runtime_version=current.runtime_version + "-drift",
            )
            with patch.object(
                execution_policy,
                "controller_runtime_provenance",
                side_effect=[current, current, drifted],
            ):
                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "stale runtime provenance",
                ):
                    harness_db.verify_run(root, "UNIT", acceptance="AC1")
            self.assertEqual(fact_counts(root), (0, 0, 0))


class ContainerExecutionProvenanceRedTests(unittest.TestCase):
    def test_endpoint_classifier_accepts_only_platform_local_transports(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": ""},
            ),
            patch.object(execution_policy.os, "name", "posix"),
        ):
            self.assertEqual(
                execution_policy._local_container_engine_endpoint(
                    "/usr/bin/docker"
                ),
                "unix:///var/run/docker.sock",
            )

        named_pipe = "npipe:////./pipe/docker_engine"
        with (
            patch.dict(
                os.environ,
                {"DOCKER_HOST": named_pipe, "DOCKER_CONTEXT": ""},
            ),
            patch.object(execution_policy.os, "name", "nt"),
        ):
            self.assertEqual(
                execution_policy._local_container_engine_endpoint(
                    "C:/Program Files/Docker/docker.exe"
                ),
                named_pipe,
            )

        with (
            patch.dict(
                os.environ,
                {"DOCKER_HOST": named_pipe, "DOCKER_CONTEXT": ""},
            ),
            patch.object(execution_policy.os, "name", "posix"),
        ):
            with self.assertRaisesRegex(
                ExecutionPolicyError,
                "container-engine-non-local",
            ):
                execution_policy._local_container_engine_endpoint(
                    "/usr/bin/docker"
                )

    def test_ambiguous_docker_routing_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_local_target(root, container=True)
            observed: list[list[str]] = []
            with (
                patch.dict(
                    os.environ,
                    {
                        "DOCKER_HOST": "unix:///var/run/docker.sock",
                        "DOCKER_CONTEXT": "default",
                    },
                ),
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=container_fake(observed),
                ),
            ):
                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "container-engine-routing-ambiguous",
                ):
                    harness_db.verify_run(
                        root,
                        "UNIT",
                        acceptance="AC1",
                        runner="container",
                    )

            self.assertFalse(
                any(
                    command
                    and Path(command[0]).name.lower().startswith(
                        ("docker", "podman")
                    )
                    for command in observed
                )
            )
            self.assertEqual(fact_counts(root), (0, 0, 0))

    def test_remote_docker_routing_fails_before_run_and_writes_no_facts(self) -> None:
        cases = (
            (
                "remote-host",
                {"DOCKER_HOST": "tcp://203.0.113.10:2375", "DOCKER_CONTEXT": ""},
                ["unix:///var/run/docker.sock"],
            ),
            (
                "remote-context",
                {"DOCKER_HOST": "", "DOCKER_CONTEXT": "remote"},
                ["ssh://builder.example.invalid/run/docker.sock"],
            ),
        )
        for label, routing, endpoints in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                initialize_local_target(root, container=True)
                observed: list[list[str]] = []
                with (
                    patch.dict(os.environ, routing),
                    patch(
                        "core.execution.shutil.which",
                        return_value="/usr/bin/docker",
                    ),
                    patch(
                        "core.execution.subprocess.run",
                        side_effect=container_fake(
                            observed,
                            engine_endpoints=endpoints,
                        ),
                    ),
                ):
                    with self.assertRaisesRegex(
                        harness_db.HarnessError,
                        "container-engine-non-local",
                    ):
                        harness_db.verify_run(
                            root,
                            "UNIT",
                            acceptance="AC1",
                            runner="container",
                        )

                self.assertFalse(
                    any(
                        logical_engine_args(command)[:1] == ["run"]
                        for command in observed
                    )
                )
                self.assertEqual(fact_counts(root), (0, 0, 0))

    def test_container_endpoint_drift_creates_no_passing_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_local_target(root, container=True)
            observed: list[list[str]] = []
            with (
                patch.dict(
                    os.environ,
                    {"DOCKER_HOST": "", "DOCKER_CONTEXT": "default"},
                ),
                patch(
                    "core.execution.shutil.which",
                    return_value="/usr/bin/docker",
                ),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=container_fake(
                        observed,
                        engine_endpoints=[
                            "unix:///var/run/docker.sock",
                            "unix:///run/user/1000/docker.sock",
                        ],
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    harness_db.HarnessError,
                    "stale container provenance",
                ):
                    harness_db.verify_run(
                        root,
                        "UNIT",
                        acceptance="AC1",
                        runner="container",
                    )

            self.assertEqual(fact_counts(root), (0, 0, 0))

    def test_container_structured_stdout_matches_local_semantics(self) -> None:
        payload = '{"summary":{"total":1,"passed":1,"failed":0,"errors":0}}'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_local_test(root)
            observed: list[list[str]] = []
            with (
                patch.dict(
                    os.environ,
                    {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": ""},
                ),
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=container_fake(
                        observed,
                        structured_stdout=payload,
                    ),
                ),
            ):
                result = ContainerExecutor(root).run(
                    "python3 -B -m unittest test_provenance.py",
                    target_id="UNIT",
                    target_command_template=(
                        "python3 -B -m unittest test_provenance.py"
                    ),
                    container_image="python:3.12-test",
                    result_format="pytest-json",
                    target_definition_sha256=TARGET_DIGEST,
                )

            self.assertEqual(result.semantic_status, "pass")
            self.assertEqual(result.executed_count, 1)
            self.assertEqual(result.executed_count_source, "structured")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_local_test(root)
            harness_db.init_runtime(root)
            harness_db.add_acceptance(root, "AC1", "structured container output")
            harness_db.add_test_target(
                root,
                "UNIT",
                "unit",
                "python3 -B -m unittest test_provenance.py",
                "structured provenance target",
                container_image="python:3.12-test",
                requires_sandbox=True,
                requires_no_network=True,
                result_format="pytest-json",
            )
            harness_db.qualify_test_target(
                root,
                "Q1",
                "UNIT",
                "AC1",
                "UNIT directly exercises AC1",
                "test-controller",
            )
            observed = []
            with (
                patch.dict(
                    os.environ,
                    {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": ""},
                ),
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=container_fake(
                        observed,
                        structured_stdout=payload,
                    ),
                ),
            ):
                execution_id, validation_id = harness_db.verify_run(
                    root,
                    "UNIT",
                    acceptance="AC1",
                    runner="container",
                )

            with closing(sqlite3.connect(db_path(root))) as conn:
                recorded = conn.execute(
                    "select semantic_status, executed_count from executions where id=?",
                    (execution_id,),
                ).fetchone()
                validation = conn.execute(
                    "select result from validations where id=?",
                    (validation_id,),
                ).fetchone()
            self.assertEqual(recorded, ("pass", 1))
            self.assertEqual(validation, ("pass",))

    def test_container_structured_stdout_cannot_pass_from_a_truncated_prefix(self) -> None:
        prefix = (
            '{"type":"suite","event":"started","test_count":1}\n'
            '{"type":"test","event":"started","name":"test_a"}\n'
            '{"type":"test","event":"ok","name":"test_a"}\n'
            '{"type":"suite","event":"ok","passed":1,"failed":0,"ignored":0,"measured":0,"filtered_out":0}\n'
        )
        later_failure = (
            '{"type":"suite","event":"started","test_count":1}\n'
            '{"type":"test","event":"started","name":"test_b"}\n'
            '{"type":"test","event":"failed","name":"test_b"}\n'
            '{"type":"suite","event":"failed","passed":0,"failed":1,"ignored":0,"measured":0,"filtered_out":0}\n'
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_local_test(root)
            observed: list[list[str]] = []
            with (
                patch.dict(
                    os.environ,
                    {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": ""},
                ),
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=container_fake(
                        observed,
                        structured_stdout=prefix + later_failure,
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    ExecutionPolicyError,
                    "structured-result-truncated",
                ):
                    ContainerExecutor(
                        root,
                        max_stdout_bytes=len(prefix.encode("utf-8")),
                    ).run(
                        "python3 -B -m unittest test_provenance.py",
                        target_id="UNIT",
                        target_command_template=(
                            "python3 -B -m unittest test_provenance.py"
                        ),
                        container_image="python:3.12-test",
                        result_format="cargo-nextest-json",
                        target_definition_sha256=TARGET_DIGEST,
                    )

    def test_container_run_overrides_image_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_local_test(root)
            observed: list[list[str]] = []
            with (
                patch.dict(
                    os.environ,
                    {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": ""},
                ),
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=container_fake(observed),
                ),
            ):
                ContainerExecutor(root).run(
                    "python3 -B -m unittest test_provenance.py",
                    target_id="UNIT",
                    target_command_template=(
                        "python3 -B -m unittest test_provenance.py"
                    ),
                    container_image="python:3.12-test",
                    target_definition_sha256=TARGET_DIGEST,
                )

            run_command = next(
                command
                for command in observed
                if logical_engine_args(command)[:1] == ["run"]
            )
            logical = logical_engine_args(run_command)
            entrypoint_index = logical.index("--entrypoint")
            self.assertEqual(logical[entrypoint_index + 1], "/bin/sh")
            image_index = logical.index(IMAGE_DIGEST_A)
            self.assertEqual(logical[image_index + 1], "-lc")

    def test_container_engine_stdout_cannot_replace_missing_target_artifact(self) -> None:
        payload = '{"summary":{"total":1,"passed":1,"failed":0,"errors":0}}'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_local_test(root)
            observed: list[list[str]] = []
            base_fake = container_fake(observed)

            def fake_without_target_artifact(argv, **kwargs):
                command = [str(value) for value in argv]
                if logical_engine_args(command)[:1] == ["run"]:
                    observed.append(command)
                    return subprocess.CompletedProcess(command, 0, payload, "")
                return base_fake(argv, **kwargs)

            with (
                patch.dict(
                    os.environ,
                    {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": ""},
                ),
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=fake_without_target_artifact,
                ),
            ):
                with self.assertRaisesRegex(
                    ExecutionPolicyError,
                    "container-execution-artifact-missing",
                ):
                    ContainerExecutor(root).run(
                        "python3 -B -m unittest test_provenance.py",
                        target_id="UNIT",
                        target_command_template=(
                            "python3 -B -m unittest test_provenance.py"
                        ),
                        container_image="python:3.12-test",
                        result_format="pytest-json",
                        target_definition_sha256=TARGET_DIGEST,
                    )

    def test_container_unexpected_structured_artifact_cannot_override_stdout(self) -> None:
        failed = '{"summary":{"total":1,"passed":0,"failed":1,"errors":0}}'
        passing = '{"summary":{"total":1,"passed":1,"failed":0,"errors":0}}'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_local_test(root)
            observed: list[list[str]] = []
            base_fake = container_fake(observed, structured_stdout=failed)

            def fake_with_undeclared_artifact(argv, **kwargs):
                command = [str(value) for value in argv]
                if logical_engine_args(command)[:1] == ["run"]:
                    result = base_fake(argv, **kwargs)
                    artifact_mount = next(
                        value for value in command if value.endswith(":/artifacts:rw")
                    )
                    artifact_dir = Path(
                        artifact_mount.removesuffix(":/artifacts:rw")
                    )
                    (artifact_dir / "structured-result").write_text(
                        passing,
                        encoding="utf-8",
                    )
                    return result
                return base_fake(argv, **kwargs)

            with (
                patch.dict(
                    os.environ,
                    {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": ""},
                ),
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=fake_with_undeclared_artifact,
                ),
            ):
                with self.assertRaisesRegex(
                    ExecutionPolicyError,
                    "container-structured-artifact-unexpected",
                ):
                    ContainerExecutor(root).run(
                        "python3 -B -m unittest test_provenance.py",
                        target_id="UNIT",
                        target_command_template=(
                            "python3 -B -m unittest test_provenance.py"
                        ),
                        container_image="python:3.12-test",
                        result_format="pytest-json",
                        target_definition_sha256=TARGET_DIGEST,
                    )

    def test_container_wrapper_fails_closed_setup_and_republishes_declared_result(self) -> None:
        payload = '{"summary":{"total":1,"passed":1,"failed":0,"errors":0}}'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_local_test(root)
            observed: list[list[str]] = []
            base_fake = container_fake(observed, structured_stdout="diagnostic\n")

            def fake_with_declared_result(argv, **kwargs):
                command = [str(value) for value in argv]
                if logical_engine_args(command)[:1] == ["run"]:
                    result = base_fake(argv, **kwargs)
                    artifact_mount = next(
                        value for value in command if value.endswith(":/artifacts:rw")
                    )
                    artifact_dir = Path(
                        artifact_mount.removesuffix(":/artifacts:rw")
                    )
                    (artifact_dir / "structured-result").write_text(
                        payload,
                        encoding="utf-8",
                    )
                    return result
                return base_fake(argv, **kwargs)

            with (
                patch.dict(
                    os.environ,
                    {"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": ""},
                ),
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=fake_with_declared_result,
                ),
            ):
                result = ContainerExecutor(root).run(
                    "python3 emit_result.py",
                    target_id="UNIT",
                    target_command_template="python3 emit_result.py",
                    container_image="python:3.12-test",
                    result_format="pytest-json",
                    result_path=".ai-team/runtime/result.json",
                    target_definition_sha256=TARGET_DIGEST,
                )

            self.assertEqual(result.semantic_status, "pass")
            run_command = next(
                command
                for command in observed
                if logical_engine_args(command)[:1] == ["run"]
            )
            logical = logical_engine_args(run_command)
            script = logical[logical.index("-lc") + 1]
            self.assertTrue(script.startswith("set -eu; "), script)
            self.assertLess(script.index("cp -a /src/. /workspace/"), script.index("(python3 emit_result.py)"))
            self.assertLess(script.index("rc=$?"), script.index("rm -f -- /artifacts/structured-result"))
            self.assertLess(
                script.index("rm -f -- /artifacts/structured-result"),
                script.index("if [ -f .ai-team/runtime/result.json ]"),
            )

    def test_real_local_container_capability_records_complete_provenance(self) -> None:
        image = "python:3.12-slim"
        try:
            available = execution_policy.resolve_container_image_provenance(image)
        except ExecutionPolicyError as exc:
            self.skipTest(f"real local container capability unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_local_target(root, container=True, image=image)

            execution_id, validation_id = harness_db.verify_run(
                root,
                "UNIT",
                acceptance="AC1",
                runner="container",
            )

            with closing(sqlite3.connect(db_path(root))) as conn:
                row = conn.execute(
                    "select container_engine, container_engine_version, "
                    "container_engine_endpoint, container_image_requested, "
                    "container_image_digest, "
                    "provenance_status, no_network, sandbox_status "
                    "from executions where id=?",
                    (execution_id,),
                ).fetchone()
                validation = conn.execute(
                    "select result from validations where id=?",
                    (validation_id,),
                ).fetchone()
            self.assertEqual(row[0], available.engine)
            self.assertEqual(row[1], available.engine_version)
            self.assertEqual(row[2], available.engine_endpoint)
            self.assertEqual(row[3], image)
            self.assertEqual(row[4], available.image_digest)
            self.assertEqual(row[5:], ("complete", 1, "available"))
            self.assertEqual(validation, ("pass",))

    def test_local_image_is_resolved_and_run_without_implicit_pull(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_local_test(root)
            observed: list[list[str]] = []
            with (
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch(
                    "core.execution.subprocess.run",
                    side_effect=container_fake(observed),
                ),
            ):
                result = ContainerExecutor(root).run(
                    "python3 -B -m unittest test_provenance.py",
                    target_id="UNIT",
                    target_command_template=(
                        "python3 -B -m unittest test_provenance.py"
                    ),
                    container_image="python:3.12-test",
                    target_definition_sha256=TARGET_DIGEST,
                )

            run_argv = next(
                command
                for command in observed
                if logical_engine_args(command)[:1] == ["run"]
            )
            self.assertIn("--pull=never", run_argv)
            image_index = run_argv.index(IMAGE_DIGEST_A)
            self.assertEqual(run_argv[image_index + 1], "-lc")
            daemon_commands = [
                command
                for command in observed
                if logical_engine_args(command)
                and logical_engine_args(command)[0]
                in {"version", "image", "run", "rm"}
            ]
            self.assertTrue(daemon_commands)
            for command in daemon_commands:
                self.assertEqual(
                    command[1:3],
                    ["--host", "unix:///var/run/docker.sock"],
                )
            self.assertFalse(
                any(logical_engine_args(command)[:1] == ["pull"] for command in observed)
            )
            self.assertTrue(result.container_engine.endswith("docker"))
            self.assertEqual(result.container_engine_version, "25.0.0")
            self.assertEqual(
                result.container_engine_endpoint,
                "unix:///var/run/docker.sock",
            )
            self.assertEqual(result.container_image_requested, "python:3.12-test")
            self.assertEqual(result.container_image_digest, IMAGE_DIGEST_A)
            self.assertEqual(result.provenance_status, "complete")

    def test_missing_local_image_fails_before_container_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_local_test(root)
            observed: list[list[str]] = []

            def missing_image(argv, **_kwargs):
                command = [str(value) for value in argv]
                observed.append(command)
                logical = logical_engine_args(command)
                if logical[:2] == ["context", "show"]:
                    return subprocess.CompletedProcess(command, 0, "default\n", "")
                if logical[:2] == ["context", "inspect"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        json.dumps(
                            [
                                {
                                    "Name": "default",
                                    "Endpoints": {
                                        "docker": {
                                            "Host": "unix:///var/run/docker.sock"
                                        }
                                    },
                                }
                            ]
                        ),
                        "",
                    )
                if logical[:1] == ["version"]:
                    return subprocess.CompletedProcess(command, 0, "25.0.0\n", "")
                if logical[:2] == ["image", "inspect"]:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        "",
                        "No such image",
                    )
                raise AssertionError("container run must not start for a missing image")

            with (
                patch("core.execution.shutil.which", return_value="/usr/bin/docker"),
                patch("core.execution.subprocess.run", side_effect=missing_image),
            ):
                with self.assertRaisesRegex(
                    ExecutionPolicyError,
                    "container-image-unavailable",
                ):
                    ContainerExecutor(root).run(
                        "python3 -B -m unittest test_provenance.py",
                        target_id="UNIT",
                        target_command_template=(
                            "python3 -B -m unittest test_provenance.py"
                        ),
                        container_image="missing:test",
                        target_definition_sha256=TARGET_DIGEST,
                    )

            self.assertFalse(
                any(logical_engine_args(command)[:1] == ["run"] for command in observed)
            )

    def test_container_image_or_engine_drift_creates_no_passing_facts(self) -> None:
        cases = (
            ("image", [IMAGE_DIGEST_A, IMAGE_DIGEST_B], ["25.0.0"]),
            ("engine", [IMAGE_DIGEST_A], ["25.0.0", "26.0.0"]),
        )
        for label, digests, versions in cases:
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                initialize_local_target(root, container=True)
                observed: list[list[str]] = []
                with (
                    patch(
                        "core.execution.shutil.which",
                        return_value="/usr/bin/docker",
                    ),
                    patch(
                        "core.execution.subprocess.run",
                        side_effect=container_fake(
                            observed,
                            image_digests=digests,
                            engine_versions=versions,
                        ),
                    ),
                ):
                    with self.assertRaisesRegex(
                        harness_db.HarnessError,
                        "stale container provenance",
                    ):
                        harness_db.verify_run(
                            root,
                            "UNIT",
                            acceptance="AC1",
                            runner="container",
                        )
                self.assertEqual(fact_counts(root), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
