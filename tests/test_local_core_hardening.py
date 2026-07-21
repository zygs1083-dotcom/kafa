from __future__ import annotations

import ast
import json
import multiprocessing
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
SCRIPTS = PLUGIN_ROOT / "scripts"
for path in (PLUGIN_ROOT, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import harness_db  # noqa: E402
import harness_lib  # noqa: E402
from core import local_core_migration  # noqa: E402
from core import store as store_module  # noqa: E402
from core.local_core_migration import migrate_project_to_schema30  # noqa: E402
from core.projections import (  # noqa: E402
    projection_content_issues,
    render_decisions,
    render_project_state,
)
from core.schema_lifecycle import backup_sqlite_database  # noqa: E402
from core.store import InMemoryStore, SqliteStore, project_db_operation  # noqa: E402
from tests.test_schema30_migration import init_schema29_fixture  # noqa: E402


def _active_projection_validator(root: Path):
    def validate(active_path: Path) -> None:
        local_core_migration._schema30_doctor(active_path)
        harness_db.render_all(root)
        issues = projection_content_issues(root)
        if issues:
            raise RuntimeError(
                "projection validation failed: "
                + "; ".join(str(issue) for issue in issues)
            )

    return validate


def _active_writer(
    root_value: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    root = Path(root_value)
    try:
        with SqliteStore(root).transaction() as conn:
            conn.execute(
                "insert into decisions (id, decision, reason, created_at) "
                "values ('D-concurrent', 'keep', 'committed before migration backup', 'now')"
            )
            ready.set()
            if not release.wait(10):
                raise RuntimeError("writer release event timed out")
        result.put(("ok", ""))
    except Exception as exc:  # pragma: no cover - returned to the parent assertion
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def _paused_migration(
    root_value: str,
    announced: multiprocessing.synchronize.Event,
    staged: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    root = Path(root_value)

    original_operation = local_core_migration.project_db_operation

    def announced_operation(*args: object, **kwargs: object):
        announced.set()
        return original_operation(*args, **kwargs)

    def validate_staging(_staging_path: Path) -> None:
        staged.set()
        if not release.wait(10):
            raise RuntimeError("migration release event timed out")

    try:
        with mock.patch.object(
            local_core_migration,
            "project_db_operation",
            side_effect=announced_operation,
        ):
            migrate_project_to_schema30(
                root,
                staging_validator=validate_staging,
                active_validator=_active_projection_validator(root),
            )
        result.put(("ok", ""))
    except Exception as exc:  # pragma: no cover - returned to the parent assertion
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def _late_writer(
    root_value: str,
    replace_window: multiprocessing.synchronize.Event,
    finished: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    try:
        if not replace_window.wait(10):
            raise RuntimeError("migration did not reach the final replace window")
        with SqliteStore(Path(root_value)).transaction() as conn:
            conn.execute(
                "insert into decisions (id, decision, reason, created_at) "
                "values ('D-race', 'must-not-commit', 'after final fingerprint', 'now')"
            )
        result.put(("committed", ""))
    except Exception as exc:  # pragma: no cover - returned to the parent assertion
        result.put(("rejected", f"{type(exc).__name__}: {exc}"))
    finally:
        finished.set()


def _exit_while_holding_operation_lock(
    root_value: str,
    acquired: multiprocessing.synchronize.Event,
) -> None:
    with project_db_operation(Path(root_value)):
        acquired.set()
        os._exit(0)


def _mutation_with_paused_projection(
    root_value: str,
    committed: multiprocessing.synchronize.Event,
    release_projection: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    root = Path(root_value)
    original_render = harness_db.render_affected

    def paused_render(render_root: Path, *projections: str) -> None:
        committed.set()
        if not release_projection.wait(10):
            raise RuntimeError("writer projection release event timed out")
        original_render(render_root, *projections)

    try:
        with (
            mock.patch.object(harness_db, "emit_audit_event", return_value=None),
            mock.patch.object(harness_db, "render_affected", side_effect=paused_render),
        ):
            harness_db.record_decision(
                root,
                "late projection fact",
                "committed before migration projection backup",
            )
        result.put(("ok", ""))
    except Exception as exc:  # pragma: no cover - returned to the parent assertion
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def _rollback_migration_with_lock_observation(
    root_value: str,
    first_lock_outcome: multiprocessing.queues.Queue,
    projection_backup_complete: multiprocessing.synchronize.Event,
    release_migration: multiprocessing.synchronize.Event,
    result: multiprocessing.queues.Queue,
) -> None:
    root = Path(root_value)
    original_try_os_lock = store_module._try_os_lock
    reported = False

    def observed_try_os_lock(descriptor: int) -> None:
        nonlocal reported
        try:
            original_try_os_lock(descriptor)
        except OSError:
            if not reported:
                reported = True
                first_lock_outcome.put("blocked")
            raise
        if not reported:
            reported = True
            first_lock_outcome.put("acquired")

    def validate_staging(_staging_path: Path) -> None:
        projection_backup_complete.set()
        if not release_migration.wait(10):
            raise RuntimeError("migration rollback release event timed out")

    try:
        with mock.patch.object(
            store_module,
            "_try_os_lock",
            side_effect=observed_try_os_lock,
        ):
            migrate_project_to_schema30(
                root,
                fail_at="after_atomic_replace",
                staging_validator=validate_staging,
                active_validator=_active_projection_validator(root),
            )
        result.put(("unexpected-success", ""))
    except local_core_migration.InjectedLocalCoreMigrationFailure as exc:
        result.put(("rolled-back", str(exc)))
    except Exception as exc:  # pragma: no cover - returned to the parent assertion
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def _projection_rebuild_paused_before_write(
    root_value: str,
    project_state_read: multiprocessing.synchronize.Event,
    release_projection: multiprocessing.synchronize.Event,
    captured_schema: multiprocessing.queues.Queue,
    result: multiprocessing.queues.Queue,
) -> None:
    root = Path(root_value)
    from core import api, projections

    original_write_state = projections.write_state

    def paused_write_state(
        render_root: Path,
        state: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        captured_schema.put(int(state["schema_version"]))
        project_state_read.set()
        if not release_projection.wait(10):
            raise RuntimeError("projection rebuild release event timed out")
        return original_write_state(render_root, state, **kwargs)

    try:
        with mock.patch.object(
            projections,
            "write_state",
            side_effect=paused_write_state,
        ), mock.patch.object(
            projections,
            "PROJECTION_RENDERERS",
            (("project-state", projections.render_project_state),),
        ), mock.patch.object(
            projections,
            "PROJECTION_NAMES",
            ("project-state",),
        ):
            api.projection_rebuild(root)
        result.put(("ok", ""))
    except Exception as exc:  # pragma: no cover - returned to the parent assertion
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def _migration_with_first_lock_observation(
    root_value: str,
    first_lock_outcome: multiprocessing.queues.Queue,
    result: multiprocessing.queues.Queue,
) -> None:
    root = Path(root_value)
    original_try_os_lock = store_module._try_os_lock
    reported = False

    def observed_try_os_lock(descriptor: int) -> None:
        nonlocal reported
        try:
            original_try_os_lock(descriptor)
        except OSError:
            if not reported:
                reported = True
                first_lock_outcome.put("blocked")
            raise
        if not reported:
            reported = True
            first_lock_outcome.put("acquired")

    try:
        with mock.patch.object(
            store_module,
            "_try_os_lock",
            side_effect=observed_try_os_lock,
        ):
            migrate_project_to_schema30(
                root,
                active_validator=_active_projection_validator(root),
            )
        result.put(("ok", ""))
    except Exception as exc:  # pragma: no cover - returned to the parent assertion
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def _unlock_baseexception_probe(
    root_value: str,
    result: multiprocessing.queues.Queue,
) -> None:
    root = Path(root_value)
    try:
        try:
            with mock.patch.object(
                store_module,
                "_unlock_os_lock",
                side_effect=KeyboardInterrupt("cancel during unlock"),
            ):
                with project_db_operation(root):
                    pass
        except KeyboardInterrupt:
            pass
        else:  # pragma: no cover - the injected cancellation must propagate
            result.put(("error", "unlock cancellation did not propagate", -1))
            return

        try:
            with project_db_operation(root, timeout=0.25):
                pass
        except BaseException as exc:  # pragma: no cover - returned to parent
            result.put(
                (
                    "blocked",
                    f"{type(exc).__name__}: {exc}",
                    len(store_module._HELD_FDS),
                )
            )
        else:
            result.put(("released", "", len(store_module._HELD_FDS)))
    except BaseException as exc:  # pragma: no cover - returned to parent
        result.put(("error", f"{type(exc).__name__}: {exc}", -1))


def _open_baseexception_probe(
    lock_path_value: str,
    result: multiprocessing.queues.Queue,
) -> None:
    lock_path = Path(lock_path_value)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    real_open_lock_fd = store_module.ProjectFS.open_lock_fd
    real_close = os.close
    opened: list[int] = []

    def tracked_open_lock_fd(
        project_fs: object,
        *args: object,
        **kwargs: object,
    ) -> int:
        descriptor = real_open_lock_fd(project_fs, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    try:
        with (
            mock.patch.object(
                store_module.ProjectFS,
                "open_lock_fd",
                new=tracked_open_lock_fd,
            ),
            mock.patch.object(
                store_module.os,
                "fsync",
                side_effect=KeyboardInterrupt("cancel during lock open"),
            ),
        ):
            try:
                store_module._open_operation_lock(lock_path)
            except KeyboardInterrupt:
                pass
            else:  # pragma: no cover - the injected cancellation must propagate
                result.put(("error", "open cancellation did not propagate"))
                return

        descriptor = opened[0]
        try:
            os.fstat(descriptor)
        except OSError:
            closed = True
        else:
            closed = False
            real_close(descriptor)
        result.put(("closed" if closed else "leaked", ""))
    except BaseException as exc:  # pragma: no cover - returned to parent
        for descriptor in opened:
            try:
                real_close(descriptor)
            except OSError:
                pass
        result.put(("error", f"{type(exc).__name__}: {exc}"))


def _join_process(process: multiprocessing.Process) -> None:
    process.join(15)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise AssertionError(f"child process did not exit: pid={process.pid}")


class ProjectionDeterminismTests(unittest.TestCase):
    def test_project_state_keys_and_values_exactly_match_database_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            state = harness_lib.read_state(root)
            schema = json.loads(
                (
                    PLUGIN_ROOT / "schemas/project-state.schema.json"
                ).read_text(encoding="utf-8")
            )
            with harness_db.connection(root) as conn:
                row = harness_db.project_row(conn)
                expected = {
                    key: str(row[key])
                    for key in schema["properties"]
                }

        self.assertEqual(set(state), set(schema["properties"]))
        self.assertEqual(state, expected)

    def test_unchanged_database_renders_byte_identical_project_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            projection = root / ".ai-team/control/project-state.yaml"

            with mock.patch.object(
                harness_lib,
                "now_iso",
                return_value="2030-01-01T00:00:00+00:00",
            ):
                render_project_state(root)
            first = projection.read_bytes()

            with mock.patch.object(
                harness_lib,
                "now_iso",
                return_value="2031-01-01T00:00:00+00:00",
            ):
                render_project_state(root)
            second = projection.read_bytes()

            with harness_db.connection(root) as conn:
                authoritative_updated_at = str(
                    conn.execute(
                        "select updated_at from project where id=1"
                    ).fetchone()[0]
                )

        self.assertEqual(second, first)
        self.assertNotIn(b"\r\n", second)
        self.assertIn(
            f"updated_at: {authoritative_updated_at}\n".encode("utf-8"),
            second,
        )

    def test_project_state_rebuild_removes_stale_ad_hoc_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            harness_db.init_runtime(root)
            projection = root / ".ai-team/control/project-state.yaml"
            projection.write_text(
                projection.read_text(encoding="utf-8")
                + "unexpected_stale_key: must-be-removed\n",
                encoding="utf-8",
            )

            render_project_state(root)
            rebuilt = projection.read_text(encoding="utf-8")

        self.assertNotIn("unexpected_stale_key", rebuilt)


class MigrationOperationLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = multiprocessing.get_context("spawn")

    def test_existing_writer_finishes_before_backup_and_fact_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            writer_ready = self.context.Event()
            release_writer = self.context.Event()
            writer_result = self.context.Queue()
            writer = self.context.Process(
                target=_active_writer,
                args=(str(root), writer_ready, release_writer, writer_result),
            )
            writer.start()
            self.assertTrue(writer_ready.wait(10), "writer did not enter its transaction")

            migration_staged = self.context.Event()
            migration_announced = self.context.Event()
            release_migration = self.context.Event()
            migration_result = self.context.Queue()
            migration = self.context.Process(
                target=_paused_migration,
                args=(
                    str(root),
                    migration_announced,
                    migration_staged,
                    release_migration,
                    migration_result,
                ),
            )
            migration.start()
            self.assertTrue(
                migration_announced.wait(10),
                "migration did not announce its sentinel before waiting for the writer",
            )
            self.assertFalse(
                migration_staged.is_set(),
                "migration read/staged the source while an earlier writer still held it",
            )

            release_writer.set()
            _join_process(writer)
            self.assertEqual(writer_result.get(timeout=2), ("ok", ""))
            self.assertTrue(migration_staged.wait(10), "migration did not resume after writer commit")
            release_migration.set()
            _join_process(migration)
            self.assertEqual(migration_result.get(timeout=2), ("ok", ""))

            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                version = conn.execute("select schema_version from project where id=1").fetchone()[0]
                decision = conn.execute(
                    "select decision from decisions where id='D-concurrent'"
                ).fetchone()[0]

        self.assertEqual((version, decision), (30, "keep"))

    def test_unlock_baseexception_releases_descriptor_and_local_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self.context.Queue()
            process = self.context.Process(
                target=_unlock_baseexception_probe,
                args=(temp, result),
            )
            process.start()
            _join_process(process)
            outcome = result.get(timeout=2)

        self.assertEqual(outcome, ("released", "", 0))

    def test_open_baseexception_closes_partial_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            lock_path = (
                Path(temp)
                / ".ai-team/state/harness.db.operation.lock"
            )
            result = self.context.Queue()
            process = self.context.Process(
                target=_open_baseexception_probe,
                args=(str(lock_path), result),
            )
            process.start()
            _join_process(process)
            outcome = result.get(timeout=2)

        self.assertEqual(outcome, ("closed", ""))

    def test_mutation_projection_finishes_before_migration_backup_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            render_decisions(root)
            projection = root / ".ai-team/control/decision-log.md"
            before_projection = projection.read_bytes()

            writer_committed = self.context.Event()
            release_writer_projection = self.context.Event()
            writer_result = self.context.Queue()
            writer = self.context.Process(
                target=_mutation_with_paused_projection,
                args=(
                    str(root),
                    writer_committed,
                    release_writer_projection,
                    writer_result,
                ),
            )
            writer.start()
            self.assertTrue(
                writer_committed.wait(10),
                "writer did not commit and enter its projection lifecycle",
            )

            first_lock_outcome = self.context.Queue()
            projection_backup_complete = self.context.Event()
            release_migration = self.context.Event()
            migration_result = self.context.Queue()
            migration = self.context.Process(
                target=_rollback_migration_with_lock_observation,
                args=(
                    str(root),
                    first_lock_outcome,
                    projection_backup_complete,
                    release_migration,
                    migration_result,
                ),
            )
            migration.start()
            lock_outcome = first_lock_outcome.get(timeout=10)
            if lock_outcome == "acquired":
                self.assertTrue(
                    projection_backup_complete.wait(10),
                    "migration acquired the lock but did not finish its projection backup",
                )

            release_writer_projection.set()
            _join_process(writer)
            self.assertTrue(
                projection_backup_complete.wait(10),
                "migration did not continue after the complete writer lifecycle",
            )
            release_migration.set()
            _join_process(migration)

            writer_outcome = writer_result.get(timeout=2)
            migration_outcome = migration_result.get(timeout=2)
            backup_dir = next(
                (root / ".ai-team/backups").glob("schema-29-before-local-core-*")
            )
            manifest = json.loads(
                (backup_dir / "migration-manifest.json").read_text(encoding="utf-8")
            )
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                version = int(
                    conn.execute("select schema_version from project where id=1").fetchone()[0]
                )
                late_fact_count = int(
                    conn.execute(
                        "select count(*) from decisions where decision='late projection fact'"
                    ).fetchone()[0]
                )
            after_projection = projection.read_bytes()

        self.assertEqual(lock_outcome, "blocked")
        self.assertEqual(writer_outcome, ("ok", ""))
        self.assertEqual(migration_outcome[0], "rolled-back")
        self.assertEqual(version, 29)
        self.assertEqual(late_fact_count, 1)
        self.assertNotEqual(after_projection, before_projection)
        self.assertIn(b"late projection fact", after_projection)
        self.assertEqual(manifest["status"], "rolled-back")
        self.assertEqual(manifest["database_restore_status"], "restored")
        self.assertEqual(manifest["projection_restore_status"], "restored")

    def test_public_projection_rebuild_finishes_before_migration_can_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            project_state_read = self.context.Event()
            release_projection = self.context.Event()
            captured_schema = self.context.Queue()
            rebuild_result = self.context.Queue()
            rebuild = self.context.Process(
                target=_projection_rebuild_paused_before_write,
                args=(
                    str(root),
                    project_state_read,
                    release_projection,
                    captured_schema,
                    rebuild_result,
                ),
            )
            rebuild.start()
            self.assertTrue(
                project_state_read.wait(10),
                "projection rebuild did not read the schema-29 project state",
            )
            self.assertEqual(captured_schema.get(timeout=2), 29)

            first_lock_outcome = self.context.Queue()
            migration_result = self.context.Queue()
            migration = self.context.Process(
                target=_migration_with_first_lock_observation,
                args=(str(root), first_lock_outcome, migration_result),
            )
            migration.start()
            lock_outcome = first_lock_outcome.get(timeout=10)
            if lock_outcome == "acquired":
                _join_process(migration)

            release_projection.set()
            _join_process(rebuild)
            if migration.is_alive():
                _join_process(migration)

            rebuild_outcome = rebuild_result.get(timeout=2)
            migration_outcome = migration_result.get(timeout=2)
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                version = int(
                    conn.execute("select schema_version from project where id=1").fetchone()[0]
                )
            project_state = (
                root / ".ai-team/control/project-state.yaml"
            ).read_text(encoding="utf-8")
            local_core_migration._schema30_doctor(
                root / ".ai-team/state/harness.db"
            )
            doctor_issues = projection_content_issues(root)

        self.assertEqual(lock_outcome, "blocked")
        self.assertEqual(rebuild_outcome, ("ok", ""))
        self.assertEqual(migration_outcome, ("ok", ""))
        self.assertEqual(version, 30)
        self.assertIn("schema_version: 30", project_state)
        self.assertEqual(doctor_issues, [])

    def test_new_connection_fails_closed_when_migration_is_announced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.write_text('{"pid": 999999, "created_at": "now"}\n', encoding="utf-8")

            with self.assertRaisesRegex(Exception, "migration-in-progress") as caught:
                with SqliteStore(root).connection():
                    self.fail("connection opened while migration sentinel existed")

            message = str(caught.exception)
            reported_path = message.split("exists at ", 1)[1].split(" (", 1)[0]
            self.assertEqual(
                os.path.normcase(os.path.realpath(reported_path)),
                os.path.normcase(os.path.realpath(sentinel)),
            )
            self.assertIn("pid=999999", message)
            self.assertIn("inspect the owner", message)

    def test_writer_cannot_enter_fingerprint_to_replace_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            staged = self.context.Event()
            announced = self.context.Event()
            release = self.context.Event()
            migration_result = self.context.Queue()
            migration = self.context.Process(
                target=_paused_migration,
                args=(str(root), announced, staged, release, migration_result),
            )
            migration.start()
            self.assertTrue(announced.wait(10), "migration did not announce its sentinel")
            self.assertTrue(staged.wait(10), "migration did not reach its pre-activation barrier")

            with self.assertRaisesRegex(Exception, "migration-in-progress"):
                with SqliteStore(root).transaction() as conn:
                    conn.execute(
                        "insert into decisions (id, decision, reason, created_at) "
                        "values ('D-too-late', 'reject', 'after migration announcement', 'now')"
                    )

            release.set()
            _join_process(migration)
            try:
                outcome = migration_result.get(timeout=2)
            except queue.Empty as exc:  # pragma: no cover - diagnostic guard
                raise AssertionError("migration returned no result") from exc
            self.assertEqual(outcome, ("ok", ""))

    def test_writer_after_final_fingerprint_is_rejected_without_lost_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            replace_window = self.context.Event()
            writer_finished = self.context.Event()
            writer_result = self.context.Queue()
            original_remove_sidecars = local_core_migration._remove_empty_active_sidecars

            def pause_after_fingerprint(
                active_path: Path,
                **kwargs: object,
            ) -> None:
                replace_window.set()
                if not writer_finished.wait(10):
                    raise RuntimeError("writer did not finish inside replace-window probe")
                original_remove_sidecars(active_path, **kwargs)

            writer = self.context.Process(
                target=_late_writer,
                args=(str(root), replace_window, writer_finished, writer_result),
            )
            writer.start()
            with mock.patch.object(
                local_core_migration,
                "_remove_empty_active_sidecars",
                side_effect=pause_after_fingerprint,
            ):
                migrate_project_to_schema30(
                    root,
                    active_validator=_active_projection_validator(root),
                )
            _join_process(writer)

            writer_outcome = writer_result.get(timeout=2)
            self.assertEqual(writer_outcome[0], "rejected")
            self.assertIn("migration-in-progress", writer_outcome[1])
            with closing(sqlite3.connect(root / ".ai-team/state/harness.db")) as conn:
                self.assertEqual(
                    conn.execute("select count(*) from decisions where id='D-race'").fetchone()[0],
                    0,
                )

    def test_operation_lock_releases_after_success_exception_and_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with project_db_operation(root):
                pass
            with self.assertRaisesRegex(RuntimeError, "injected operation failure"):
                with project_db_operation(root):
                    raise RuntimeError("injected operation failure")
            with project_db_operation(root):
                with project_db_operation(root):
                    pass

            acquired = self.context.Event()
            process = self.context.Process(
                target=_exit_while_holding_operation_lock,
                args=(str(root), acquired),
            )
            process.start()
            self.assertTrue(acquired.wait(10), "child did not acquire the operation lock")
            _join_process(process)
            self.assertEqual(process.exitcode, 0)
            with project_db_operation(root):
                pass

            operation_lock = root / ".ai-team/state/harness.db.operation.lock"
            self.assertTrue(operation_lock.is_file())
            self.assertGreaterEqual(operation_lock.stat().st_size, 1)

    def test_operation_lock_reentry_allows_migration_callbacks_but_not_lock_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text('{"pid": 1, "created_at": "now"}\n', encoding="utf-8")
            with project_db_operation(root, purpose="migration"):
                with project_db_operation(root):
                    pass
            sentinel.unlink()

            with project_db_operation(root):
                with self.assertRaisesRegex(Exception, "migration cannot start"):
                    with project_db_operation(root, purpose="migration"):
                        self.fail("normal operation upgraded itself into a migration")

    def test_operation_lock_does_not_treat_another_thread_as_reentrant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            start = threading.Barrier(2)
            outcome: list[str] = []

            def competing_thread() -> None:
                start.wait()
                try:
                    with project_db_operation(root, timeout=0.2):
                        outcome.append("acquired")
                except Exception as exc:
                    outcome.append(str(exc))

            with project_db_operation(root):
                thread = threading.Thread(target=competing_thread)
                thread.start()
                start.wait()
                thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIn("project-db-operation-timeout", outcome[0])

    def test_sentinel_write_failure_is_cleaned_up_before_operation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            operation_lock = root / ".ai-team/state/harness.db.operation.lock"

            with mock.patch.object(
                local_core_migration.ProjectFS,
                "create_exclusive",
                side_effect=OSError("injected sentinel write failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected sentinel write failure"):
                    migrate_project_to_schema30(
                        root,
                        active_validator=_active_projection_validator(root),
                    )

            self.assertFalse(sentinel.exists())
            self.assertFalse(operation_lock.exists())

    def test_file_backups_fail_closed_on_migration_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            init_schema29_fixture(root)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.write_text('{"pid": 999999, "created_at": "now"}\n', encoding="utf-8")
            explicit_target = root / "manual-backup.db"

            with self.assertRaisesRegex(Exception, "migration-in-progress"):
                SqliteStore(root).backup_to(explicit_target)
            with self.assertRaisesRegex(Exception, "migration-in-progress"):
                backup_sqlite_database(root)

            self.assertFalse(explicit_target.exists())
            self.assertFalse((root / ".ai-team/backups").exists())

    def test_in_memory_store_ignores_file_operation_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sentinel = root / ".ai-team/state/local-core-migration.lock"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text('{"pid": 999999}\n', encoding="utf-8")
            store = InMemoryStore(root)
            self.addCleanup(store.close)
            with store.connection() as conn:
                conn.execute("create table sample (value text not null)")
                conn.commit()
            with store.transaction() as conn:
                conn.execute("insert into sample (value) values ('ok')")
            with store.connection() as conn:
                value = conn.execute("select value from sample").fetchone()[0]

            self.assertEqual(value, "ok")
            self.assertFalse((root / ".ai-team/state/harness.db.operation.lock").exists())

    def test_every_transaction_projection_mutation_holds_the_outer_operation_lock(self) -> None:
        source = (SCRIPTS / "harness_db.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        missing: list[str] = []
        covered: list[str] = []
        for function in (
            node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            call_names = {
                call.func.id
                if isinstance(call.func, ast.Name)
                else call.func.attr
                if isinstance(call.func, ast.Attribute)
                else ""
                for call in ast.walk(function)
                if isinstance(call, ast.Call)
            }
            if "transaction" not in call_names or not {
                "render_affected",
                "render_all",
            } & call_names:
                continue
            decorators = {
                decorator.id
                for decorator in function.decorator_list
                if isinstance(decorator, ast.Name)
            }
            covered.append(function.name)
            if "_project_mutation" not in decorators:
                missing.append(function.name)

        self.assertGreaterEqual(len(covered), 20)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
