from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
HARNESS = SCRIPTS / "harness.py"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core import (  # noqa: E402
    invariant_checker,
    local_core_migration,
    projections,
    schema_guard,
    schema_lifecycle,
)
from core.invariant_checker import check_runtime_invariants  # noqa: E402
import harness as harness_cli  # noqa: E402
import harness_db  # noqa: E402


def db_path(root: Path) -> Path:
    return root / ".ai-team/state/harness.db"


def run_harness(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HARNESS), "--root", str(root), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def shipped_schemas() -> dict[str, dict[str, object]]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((PLUGIN_ROOT / "schemas").glob("*.schema.json"))
    }


class CanonicalStateContractRedTests(unittest.TestCase):
    def test_requirement_invalid_status_is_rejected_before_api_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            projection = root / ".ai-team/requirements/requirements.md"
            before_projection = projection.read_bytes()
            with closing(sqlite3.connect(db_path(root))) as conn:
                before = conn.execute(
                    "select (select count(*) from requirements), "
                    "(select count(*) from events), "
                    "(select revision from project where id=1)"
                ).fetchone()

            with self.assertRaisesRegex(
                harness_db.HarnessError,
                "requirement status must be one of",
            ):
                harness_db.add_requirement(
                    root,
                    "REQ-BAD",
                    "functional",
                    "must never persist",
                    status="nonsense",
                )

            with closing(sqlite3.connect(db_path(root))) as conn:
                after = conn.execute(
                    "select (select count(*) from requirements), "
                    "(select count(*) from events), "
                    "(select revision from project where id=1)"
                ).fetchone()
            self.assertEqual(after, before)
            self.assertEqual(projection.read_bytes(), before_projection)

    def test_requirement_invalid_status_is_rejected_by_cli_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(run_harness(root, "init").returncode, 0)
            with closing(sqlite3.connect(db_path(root))) as conn:
                before = conn.execute(
                    "select (select count(*) from requirements), "
                    "(select count(*) from events), "
                    "(select revision from project where id=1)"
                ).fetchone()

            result = run_harness(
                root,
                "requirement",
                "add",
                "--id",
                "REQ-BAD",
                "--kind",
                "functional",
                "--body",
                "must never persist",
                "--status",
                "nonsense",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid choice", result.stderr)
            with closing(sqlite3.connect(db_path(root))) as conn:
                after = conn.execute(
                    "select (select count(*) from requirements), "
                    "(select count(*) from events), "
                    "(select revision from project where id=1)"
                ).fetchone()
            self.assertEqual(after, before)

    def test_one_canonical_state_authority_matches_all_public_schemas(self) -> None:
        self.assertEqual(
            schema_guard.REQUIREMENT_STATUSES,
            frozenset({"active", "cancelled"}),
        )
        self.assertEqual(
            schema_guard.ACCEPTANCE_STATUSES,
            frozenset({"active", "cancelled"}),
        )
        self.assertEqual(
            schema_guard.FAILURE_MODE_STATUSES,
            frozenset({"identified", "accepted", "exempt"}),
        )
        schemas = shipped_schemas()
        self.assertEqual(
            set(schemas["requirement.schema.json"]["properties"]["status"]["enum"]),
            set(schema_guard.REQUIREMENT_STATUSES),
        )
        self.assertEqual(
            set(schemas["acceptance.schema.json"]["properties"]["status"]["enum"]),
            set(schema_guard.ACCEPTANCE_STATUSES),
        )
        self.assertEqual(
            set(schemas["failure-mode.schema.json"]["properties"]["status"]["enum"]),
            set(schema_guard.FAILURE_MODE_STATUSES),
        )

    def test_all_runtime_state_consumers_share_schema_guard_authority(self) -> None:
        for module in (
            harness_db,
            invariant_checker,
            local_core_migration,
            projections,
        ):
            self.assertIs(
                module.REQUIREMENT_STATUSES,
                schema_guard.REQUIREMENT_STATUSES,
                module.__name__,
            )
            self.assertIs(
                module.ACCEPTANCE_STATUSES,
                schema_guard.ACCEPTANCE_STATUSES,
                module.__name__,
            )
            self.assertIs(
                module.FAILURE_MODE_STATUSES,
                schema_guard.FAILURE_MODE_STATUSES,
                module.__name__,
            )
        self.assertIs(
            schema_lifecycle.REQUIREMENT_STATUS_VALUES,
            schema_guard.REQUIREMENT_STATUS_VALUES,
        )
        self.assertIs(
            schema_lifecycle.ACCEPTANCE_STATUS_VALUES,
            schema_guard.ACCEPTANCE_STATUS_VALUES,
        )
        self.assertIs(
            schema_lifecycle.FAILURE_MODE_STATUS_VALUES,
            schema_guard.FAILURE_MODE_STATUS_VALUES,
        )
        self.assertIs(
            harness_cli.REQUIREMENT_STATUS_VALUES,
            schema_guard.REQUIREMENT_STATUS_VALUES,
        )
        self.assertIs(
            harness_cli.FAILURE_MODE_STATUS_VALUES,
            schema_guard.FAILURE_MODE_STATUS_VALUES,
        )

    def test_invariant_checker_reports_each_invalid_state_with_exact_entity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "REQ1", "functional", "required")
            harness_db.add_acceptance(root, "AC1", "accepted")
            harness_db.add_failure_mode(
                root,
                "FM1",
                "delivery",
                "invalid state",
                "tamper",
                "detect",
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("pragma ignore_check_constraints=on")
                conn.execute("update requirements set status='nonsense' where id='REQ1'")
                conn.execute("update acceptance set status='nonsense' where id='AC1'")
                conn.execute("update failure_modes set status='nonsense' where id='FM1'")
                issues = check_runtime_invariants(conn, root)

        self.assertEqual(
            {
                (item.code, item.entity_type, item.entity_id)
                for item in issues
                if item.code.startswith("invalid-")
            },
            {
                ("invalid-requirement-status", "requirement", "REQ1"),
                ("invalid-acceptance-status", "acceptance", "AC1"),
                ("invalid-failure-mode-status", "failure_mode", "FM1"),
            },
        )

    def test_projection_rebuild_fails_closed_before_writing_invalid_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "REQ1", "functional", "required")
            before = {
                path: path.read_bytes()
                for path in sorted((root / ".ai-team").rglob("*.md"))
            }
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("pragma ignore_check_constraints=on")
                conn.execute(
                    "update requirements set status='nonsense' where id='REQ1'"
                )
                conn.commit()

            result = run_harness(root, "projection", "rebuild")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("REQ1", result.stdout + result.stderr)
            self.assertIn("invalid requirement status", result.stdout + result.stderr)
            after = {
                path: path.read_bytes()
                for path in sorted((root / ".ai-team").rglob("*.md"))
            }
            self.assertEqual(after, before)

    def test_doctor_reports_every_invalid_canonical_state_with_entity_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "REQ1", "functional", "required")
            harness_db.add_acceptance(root, "AC1", "accepted")
            harness_db.add_failure_mode(
                root,
                "FM1",
                "delivery",
                "invalid state",
                "tamper",
                "detect",
            )
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute("pragma ignore_check_constraints=on")
                conn.execute("update requirements set status='nonsense' where id='REQ1'")
                conn.execute("update acceptance set status='nonsense' where id='AC1'")
                conn.execute("update failure_modes set status='nonsense' where id='FM1'")
                conn.commit()

            result = run_harness(root, "doctor")

        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        for label, entity_id in (
            ("invalid requirement status", "REQ1"),
            ("invalid acceptance status", "AC1"),
            ("invalid failure mode status", "FM1"),
        ):
            self.assertIn(label, output)
            self.assertIn(entity_id, output)

    def test_canonical_state_projection_roundtrip_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            harness_db.add_requirement(root, "REQ-A", "functional", "active")
            harness_db.add_requirement(
                root,
                "REQ-C",
                "functional",
                "cancelled audit history",
                status="cancelled",
            )
            harness_db.add_acceptance(root, "AC-A", "active acceptance")
            with closing(sqlite3.connect(db_path(root))) as conn:
                conn.execute(
                    "insert into acceptance "
                    "(id, cycle_id, criterion, status) values "
                    "('AC-C', 'CYCLE-current', 'cancelled audit history', 'cancelled')"
                )
                conn.commit()
            harness_db.add_failure_mode(
                root,
                "FM-I",
                "delivery",
                "identified",
                "runtime",
                "detect",
                status="identified",
            )
            for fm_id, status in (("FM-A", "accepted"), ("FM-E", "exempt")):
                harness_db.add_failure_mode(
                    root,
                    fm_id,
                    "delivery",
                    status,
                    "runtime",
                    "record metadata",
                    status=status,
                    accepted_by="risk-owner",
                    acceptance_reason=f"{status} for projection test",
                    acceptance_scope="current local project",
                    expires_at="2099-01-01T00:00:00Z",
                )

            projections.render_all(root)

            requirements = (
                root / ".ai-team/requirements/requirements.md"
            ).read_text(encoding="utf-8")
            acceptance = (
                root / ".ai-team/requirements/acceptance.md"
            ).read_text(encoding="utf-8")
            failure_modes = (
                root / ".ai-team/requirements/failure-modes.md"
            ).read_text(encoding="utf-8")
            self.assertIn("| REQ-A | functional | active |  | active |", requirements)
            self.assertIn("| REQ-C | functional | cancelled audit history |  | cancelled |", requirements)
            self.assertIn("| AC-A | active acceptance |  | active |", acceptance)
            self.assertIn("| AC-C | cancelled audit history |  | cancelled |", acceptance)
            self.assertIn("| FM-I |", failure_modes)
            self.assertIn("| identified |", failure_modes)
            self.assertIn("| accepted |", failure_modes)
            self.assertIn("| exempt |", failure_modes)
            self.assertEqual(projections.projection_content_issues(root), [])


class PublicJsonSchemaContractRedTests(unittest.TestCase):
    def test_every_schema_has_unique_schema31_id_and_explicit_closed_properties(self) -> None:
        schemas = shipped_schemas()
        self.assertEqual(len(schemas), 18)
        ids = []
        for name, schema in schemas.items():
            entity = name.removesuffix(".schema.json")
            expected = f"urn:kafa:schema:31:{entity}"
            self.assertEqual(schema.get("$id"), expected, name)
            self.assertIs(schema.get("additionalProperties"), False, name)
            ids.append(schema.get("$id"))
        self.assertEqual(len(ids), len(set(ids)))

    def test_runtime_validator_enforces_closed_supported_keyword_subset(self) -> None:
        schema = {
            "type": "object",
            "required": ["schema_version", "name", "count", "items"],
            "additionalProperties": False,
            "properties": {
                "schema_version": {"type": "integer", "const": 31},
                "name": {"type": "string", "minLength": 3, "pattern": "^[A-Z]+$"},
                "count": {"type": "integer", "minimum": 1},
                "items": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 2, "pattern": "^x"},
                },
            },
        }
        valid = {
            "schema_version": 31,
            "name": "ABC",
            "count": 1,
            "items": ["xy"],
        }
        self.assertEqual(
            harness_db.validate_object_against_schema("fixture", valid, schema),
            [],
        )
        cases = (
            ({**valid, "schema_version": 30}, "const"),
            ({**valid, "name": "AB"}, "minLength"),
            ({**valid, "name": "Abc"}, "pattern"),
            ({**valid, "count": 0}, "minimum"),
            ({**valid, "items": ["y"]}, "items[0]"),
            ({**valid, "unknown": True}, "not declared"),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                issues = harness_db.validate_object_against_schema(
                    "fixture",
                    payload,
                    schema,
                )
                self.assertTrue(issues)
                self.assertIn(expected, " ".join(issues))

    def test_supported_schema_keyword_definitions_fail_closed(self) -> None:
        from core.json_schema_contract import schema_definition_issues

        cases = (
            ({"$schema": 31}, "$.$schema must be a non-empty string"),
            ({"$id": ""}, "$.$id must be a non-empty string"),
            ({"title": 31}, "$.title must be a string"),
            ({"description": False}, "$.description must be a string"),
            ({"readOnly": "yes"}, "$.readOnly must be boolean"),
            ({"type": ["string", "string"]}, "$.type entries must be unique"),
            ({"required": ["id", "id"]}, "$.required entries must be unique"),
            ({"enum": "active"}, "$.enum must be a non-empty array"),
            ({"enum": []}, "$.enum must be a non-empty array"),
            ({"type": "string", "format": "uuid"}, "$.format is unsupported"),
        )
        for schema, expected in cases:
            with self.subTest(schema=schema):
                issues = schema_definition_issues(schema)
                self.assertTrue(
                    any(expected in issue for issue in issues),
                    issues,
                )

        malformed_runtime_schema = {
            "type": "object",
            "properties": {"status": {"enum": 31}},
            "additionalProperties": False,
        }
        runtime_issues = harness_db.validate_object_against_schema(
            "fixture",
            {"status": "active"},
            malformed_runtime_schema,
        )
        self.assertTrue(runtime_issues)
        self.assertIn("invalid schema definition", " ".join(runtime_issues))

    def test_every_shipped_constraint_and_unknown_field_policy_is_executed(self) -> None:
        executed = {
            key: 0
            for key in ("enum", "minimum", "const", "minLength", "pattern", "format")
        }
        for name, schema in shipped_schemas().items():
            unknown = harness_db.validate_object_against_schema(
                name,
                {"__unexpected__": True},
                schema,
            )
            self.assertTrue(
                any("not declared" in issue for issue in unknown),
                name,
            )
            for field, definition in schema.get("properties", {}).items():
                if "enum" in definition:
                    executed["enum"] += 1
                    enum_values = definition["enum"]
                    invalid_value = (
                        "__invalid_enum__"
                        if not enum_values or isinstance(enum_values[0], str)
                        else max(enum_values) + 1
                    )
                    issues = harness_db.validate_object_against_schema(
                        name,
                        {field: invalid_value},
                        schema,
                    )
                    self.assertTrue(
                        any(field in issue and "not in" in issue for issue in issues),
                        f"{name}:{field}",
                    )
                if "minimum" in definition:
                    executed["minimum"] += 1
                    issues = harness_db.validate_object_against_schema(
                        name,
                        {field: definition["minimum"] - 1},
                        schema,
                    )
                    self.assertTrue(
                        any(field in issue and "minimum" in issue for issue in issues),
                        f"{name}:{field}",
                    )
                if "const" in definition:
                    executed["const"] += 1
                    expected = definition["const"]
                    invalid_value = (
                        expected + 1
                        if isinstance(expected, int) and not isinstance(expected, bool)
                        else f"{expected}__invalid__"
                    )
                    issues = harness_db.validate_object_against_schema(
                        name,
                        {field: invalid_value},
                        schema,
                    )
                    self.assertTrue(
                        any(field in issue and "const" in issue for issue in issues),
                        f"{name}:{field}",
                    )
                if "minLength" in definition:
                    executed["minLength"] += 1
                    invalid_value = "x" * max(0, int(definition["minLength"]) - 1)
                    issues = harness_db.validate_object_against_schema(
                        name,
                        {field: invalid_value},
                        schema,
                    )
                    self.assertTrue(
                        any(field in issue and "minLength" in issue for issue in issues),
                        f"{name}:{field}",
                    )
                if "pattern" in definition:
                    executed["pattern"] += 1
                    pattern = str(definition["pattern"])
                    invalid_value = ""
                    if re.search(pattern, invalid_value):
                        invalid_value = "__invalid_pattern__"
                    self.assertIsNone(
                        re.search(pattern, invalid_value),
                        f"test fixture unexpectedly matches {name}:{field}",
                    )
                    issues = harness_db.validate_object_against_schema(
                        name,
                        {field: invalid_value},
                        schema,
                    )
                    self.assertTrue(
                        any(field in issue and "pattern" in issue for issue in issues),
                        f"{name}:{field}",
                    )
                if "format" in definition:
                    executed["format"] += 1
                    self.assertEqual(definition["format"], "date-time")
                    issues = harness_db.validate_object_against_schema(
                        name,
                        {field: "2026-02-30T00:00:00Z"},
                        schema,
                    )
                    self.assertTrue(
                        any(field in issue and "format" in issue for issue in issues),
                        f"{name}:{field}",
                    )
        self.assertEqual(
            executed,
            {
                "enum": 30,
                "minimum": 11,
                "const": 2,
                "minLength": 10,
                "pattern": 11,
                "format": 2,
            },
        )

    def test_structure_validation_rejects_unsupported_nested_schema_keyword(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_copy = root / "plugins" / "codex-project-harness"
            plugin_copy.parent.mkdir(parents=True)
            shutil.copytree(
                PLUGIN_ROOT,
                plugin_copy,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            for name in ("VERSION", "release.json", "pyproject.toml"):
                shutil.copyfile(REPO_ROOT / name, root / name)
            schema_path = plugin_copy / "schemas" / "requirement.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema["properties"]["body"]["maxLength"] = 50
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(plugin_copy / "scripts" / "validate_structure.py"), str(plugin_copy)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported schema keyword", result.stdout)
        self.assertIn("maxLength", result.stdout)
        self.assertIn("requirement.schema.json", result.stdout)


if __name__ == "__main__":
    unittest.main()
