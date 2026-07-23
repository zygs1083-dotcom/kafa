from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import ast
from contextlib import closing
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL = REPO_ROOT / "plugins/codex-project-harness/scripts/run_agent_e2e_eval.py"
sys.path.insert(0, str(EVAL.parent))
import run_agent_e2e_eval  # noqa: E402


def configure_repo_lf(root: Path) -> None:
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=root,
        check=True,
    )


def unlink_git_object(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, path.stat().st_mode | stat.S_IWUSR)
        path.unlink()


def run_eval(*args: str, env: dict[str, str] | None = None) -> dict[str, object]:
    result = run_eval_process(*args, env=env)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def run_eval_process(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env.setdefault("N8N_MCP_ACCESS_TOKEN", "ambient-test-secret")
    if env is not None:
        command_env.update(env)
        if env.get("HARNESS_E2E_CODEX_BIN"):
            command_env.setdefault("HARNESS_E2E_ALLOW_CODEX_BIN_OVERRIDE", "1")
    return subprocess.run([sys.executable, str(EVAL), *args], text=True, capture_output=True, check=False, env=command_env)


def make_fake_codex(
    root: Path,
    *,
    tamper_state: bool = False,
    tamper_attribution: bool = False,
    inject_retired_table: bool = False,
) -> Path:
    script = root / "fake_codex.py"
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        f"TAMPER_STATE = {tamper_state!r}\n"
        f"TAMPER_ATTRIBUTION = {tamper_attribution!r}\n"
        f"INJECT_RETIRED_TABLE = {inject_retired_table!r}\n"
        "args = sys.argv[1:]\n"
        "if os.environ.get('N8N_MCP_ACCESS_TOKEN'):\n"
        "    print('ambient secret leaked to configured Codex binary', file=sys.stderr)\n"
        "    raise SystemExit(91)\n"
        "if args == ['login', 'status']:\n"
        "    print('Logged in using test fixture')\n"
        "    raise SystemExit(0)\n"
        "if args == ['--version']:\n"
        "    print('codex-cli 0.143.0')\n"
        "    raise SystemExit(0)\n"
        "if args and args[0] == 'exec':\n"
        "    work = pathlib.Path(args[args.index('--cd') + 1])\n"
        "    prompt = args[-1]\n"
        "    if 'ALPHA-PRODUCER' in prompt:\n"
        "        time.sleep(0.15)\n"
        "        (work / 'alpha.py').write_text('VALUE = \\\"after\\\"\\n', encoding='utf-8')\n"
        "        if TAMPER_ATTRIBUTION:\n"
        "            (work / 'beta.py').write_text('VALUE = \\\"tampered\\\"\\n', encoding='utf-8')\n"
        "        token_count = 600\n"
        "    elif 'BETA-PRODUCER' in prompt:\n"
        "        time.sleep(0.15)\n"
        "        (work / 'beta.py').write_text('VALUE = \\\"after\\\"\\n', encoding='utf-8')\n"
        "        token_count = 700\n"
        "    else:\n"
        "        candidate_source = 'VALUE = \\\"after\\\"\\n'\n"
        "        if INJECT_RETIRED_TABLE:\n"
        "            candidate_source = (\n"
        "                'import sqlite3\\n'\n"
        "                \"with sqlite3.connect('.ai-team/state/harness.db') as conn:\\n\"\n"
        "                \"    conn.execute('create table if not exists adapter_actions (id text primary key)')\\n\"\n"
        "                'VALUE = \\\"after\\\"\\n'\n"
        "            )\n"
        "        (work / 'candidate.py').write_text(candidate_source, encoding='utf-8')\n"
        "        token_count = 1234\n"
        "    if TAMPER_STATE:\n"
        "        state = work / '.ai-team/state/harness.db'\n"
        "        state.parent.mkdir(parents=True, exist_ok=True)\n"
        "        state.write_text('tampered', encoding='utf-8')\n"
        "    if '--output-last-message' in args:\n"
        "        out = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
        "        out.parent.mkdir(parents=True, exist_ok=True)\n"
        "        out.write_text('edited candidate.py and ran the requested test\\n', encoding='utf-8')\n"
        "    print(json.dumps({'type': 'turn.completed', 'usage': {\n"
        "        'input_tokens': token_count - 10,\n"
        "        'cached_input_tokens': 100,\n"
        "        'output_tokens': 10,\n"
        "        'reasoning_output_tokens': 1,\n"
        "    }}))\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = root / "codex.bat"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = root / "codex"
        launcher.write_text(f'#!{sys.executable}\nexec(open({str(script)!r}).read())\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


class AgentE2EEvalTest(unittest.TestCase):
    def test_evaluation_source_identity_excludes_generated_python_caches(self) -> None:
        self.assertTrue(
            run_agent_e2e_eval._is_evaluation_cache_path(
                "plugins/codex-project-harness/scripts/__pycache__/eval.pyc"
            )
        )
        self.assertTrue(
            run_agent_e2e_eval._is_evaluation_cache_path("tests/.pytest_cache/state")
        )
        self.assertFalse(
            run_agent_e2e_eval._is_evaluation_cache_path("tests/test_agent_e2e_eval.py")
        )

    def test_pinned_controller_source_ignores_transient_original_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            harness = root / "plugins/codex-project-harness/scripts/harness.py"
            harness.parent.mkdir(parents=True)
            original = b"print('trusted-controller')\n"
            harness.write_bytes(original)
            subprocess.run(
                ["git", "add", "plugins/codex-project-harness/scripts/harness.py"],
                cwd=root,
                check=True,
            )
            start_identity = run_agent_e2e_eval.evaluation_source_identity(root)
            worker_ready = threading.Event()
            read_pinned = threading.Event()
            observed: dict[str, bytes] = {}

            with run_agent_e2e_eval.pinned_evaluation_source(
                root,
                start_identity,
            ) as pinned_harness:
                def read_controller() -> None:
                    worker_ready.set()
                    observed["bytes"] = pinned_harness.read_bytes()
                    read_pinned.set()

                harness.write_bytes(b"print('transient-untrusted-controller')\n")
                worker = threading.Thread(target=read_controller)
                worker.start()
                self.assertTrue(worker_ready.wait(5))
                self.assertTrue(read_pinned.wait(5))
                harness.write_bytes(original)
                worker.join(5)
                self.assertFalse(worker.is_alive())

            end_identity = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertEqual(observed["bytes"], original)
        self.assertEqual(
            start_identity["workspace_sha256"],
            end_identity["workspace_sha256"],
        )

    def test_pinned_controller_snapshot_ignores_ambient_git_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            ambient_git_dir = Path(temp) / "ambient.git"
            root.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            configure_repo_lf(root)
            harness = root / "plugins/codex-project-harness/scripts/harness.py"
            harness.parent.mkdir(parents=True)
            harness.write_bytes(b"print('trusted-controller')\n")
            subprocess.run(
                ["git", "add", "plugins/codex-project-harness/scripts/harness.py"],
                cwd=root,
                check=True,
            )
            identity = run_agent_e2e_eval.evaluation_source_identity(root)

            with mock.patch.dict(
                os.environ,
                {"GIT_DIR": str(ambient_git_dir)},
                clear=False,
            ):
                with run_agent_e2e_eval.pinned_evaluation_source(
                    root,
                    identity,
                ) as pinned_harness:
                    pinned_bytes = pinned_harness.read_bytes()

        self.assertEqual(pinned_bytes, b"print('trusted-controller')\n")
        self.assertFalse(ambient_git_dir.exists())

    def test_evaluation_source_identity_hashes_runtime_bytes_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            subprocess.run(
                ["git", "config", "core.autocrlf", "true"],
                cwd=root,
                check=True,
            )
            attributes = root / ".gitattributes"
            attributes.write_text("* text=auto eol=lf\n", encoding="utf-8")
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"first\nsecond\n")
            subprocess.run(
                ["git", "add", "--", str(attributes), str(source)],
                cwd=root,
                check=True,
            )
            lf_digest = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]
            source.write_bytes(b"first\r\nsecond\r\n")
            runtime_crlf_digest = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]
            source.write_bytes(b"first\r\nchanged\r\n")
            changed_digest = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]

            binary = root / "tests/payload.bin"
            binary.write_bytes(b"\0first\r\nsecond\r\n")
            binary_digest = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]
            binary.write_bytes(b"\0first\nsecond\n")
            changed_binary_digest = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]

            binary.unlink()
            before_add = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]
            added = root / "tests/added.py"
            added.write_text("value = 1\n", encoding="utf-8")
            after_add = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]
            added.unlink()
            after_delete = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]
            mode_before = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]
            if os.name == "nt":
                subprocess.run(
                    ["git", "update-index", "--chmod=+x", "tests/source.py"],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            else:
                source.chmod(source.stat().st_mode | 0o111)
            mode_after = run_agent_e2e_eval.evaluation_source_identity(root)[
                "workspace_sha256"
            ]

        self.assertNotEqual(lf_digest, runtime_crlf_digest)
        self.assertNotEqual(runtime_crlf_digest, changed_digest)
        self.assertNotEqual(binary_digest, changed_binary_digest)
        self.assertNotEqual(before_add, after_add)
        self.assertEqual(before_add, after_delete)
        self.assertNotEqual(mode_before, mode_after)

    def test_evaluation_source_identity_frames_fixed_file_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            source = root / "tests/payload.bin"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"runtime-bytes\0with-binary")
            source.chmod(0o644)

            identity = run_agent_e2e_eval.evaluation_source_identity(root)
            file_sha256 = hashlib.sha256(source.read_bytes()).hexdigest().encode("ascii")
            expected = hashlib.sha256(
                b"tests/payload.bin\0" + b"100644\0" + file_sha256 + b"\0"
            ).hexdigest()

        self.assertEqual(identity["workspace_sha256"], expected)

    def test_evaluation_source_identity_survives_commit_of_same_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            configure_repo_lf(root)
            subprocess.run(
                ["git", "config", "user.email", "eval@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Eval Test"],
                cwd=root,
                check=True,
            )
            attributes = root / ".gitattributes"
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            attributes.write_bytes(b"* text=auto eol=lf\n")
            source.write_bytes(b"VALUE = 1\n")

            before_commit = run_agent_e2e_eval.evaluation_source_identity(root)
            subprocess.run(
                ["git", "add", "--", str(attributes), str(source)],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "publish runtime"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            after_commit = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertEqual(
            before_commit["workspace_sha256"],
            after_commit["workspace_sha256"],
        )
        self.assertTrue(before_commit["git_dirty"])
        self.assertFalse(after_commit["git_dirty"])
        self.assertNotEqual(before_commit["status_sha256"], after_commit["status_sha256"])

    def test_evaluation_source_identity_rejects_source_symlink_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", str(source)], cwd=root, check=True)
            regular = run_agent_e2e_eval.evaluation_source_identity(root)
            original_is_symlink = Path.is_symlink

            def report_source_as_symlink(path: Path) -> bool:
                if path.resolve() == source.resolve():
                    return True
                return original_is_symlink(path)

            with mock.patch.object(
                Path,
                "is_symlink",
                new=report_source_as_symlink,
            ):
                symlink = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertNotEqual(regular["workspace_sha256"], "")
        self.assertEqual(symlink["workspace_sha256"], "")

    def test_evaluation_source_identity_rejects_gitlink_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            subprocess.run(
                ["git", "config", "user.email", "eval@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Eval Test"],
                cwd=root,
                check=True,
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", str(source)], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            baseline = run_agent_e2e_eval.evaluation_source_identity(root)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            gitlink = root / "tests/submodule"
            gitlink.mkdir()
            (gitlink / "runtime.py").write_text(
                "raise RuntimeError('submodule runtime')\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{head},tests/submodule",
                ],
                cwd=root,
                check=True,
            )
            with_gitlink = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertNotEqual(baseline["workspace_sha256"], "")
        self.assertEqual(with_gitlink["workspace_sha256"], "")

    def test_evaluation_source_identity_rejects_head_only_gitlink_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            subprocess.run(
                ["git", "config", "user.email", "eval@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Eval Test"], cwd=root, check=True
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "tests/source.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{commit},tests/submodule",
                ],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "record gitlink"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "rm", "--cached", "tests/submodule"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

            identity = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertEqual(identity["workspace_sha256"], "")

    def test_evaluation_source_identity_ignores_commit_replace_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            subprocess.run(
                ["git", "config", "user.email", "eval@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Eval Test"],
                cwd=root,
                check=True,
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "tests/source.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "clean baseline"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            clean_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"160000,{clean_commit},tests/submodule",
                ],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "head contains gitlink"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            replaced_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "rm", "--cached", "tests/submodule"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "replace", replaced_head, clean_commit],
                cwd=root,
                check=True,
            )

            identity = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertEqual(identity["workspace_sha256"], "")
        self.assertEqual(identity["source_scope"], [])

    def test_evaluation_source_identity_rejects_unmerged_source_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            subprocess.run(
                ["git", "config", "user.email", "eval@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Eval Test"],
                cwd=root,
                check=True,
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 'base'\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", str(source)], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            primary = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "-b", "other"], cwd=root, check=True, capture_output=True)
            source.write_text("VALUE = 'other'\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "other change"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", primary], cwd=root, check=True, capture_output=True)
            source.write_text("VALUE = 'primary'\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "primary change"], cwd=root, check=True, capture_output=True)
            merge = subprocess.run(
                ["git", "merge", "other"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            identity = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertNotEqual(merge.returncode, 0)
        self.assertEqual(identity["workspace_sha256"], "")
        self.assertEqual(identity["source_scope"], [])

    def test_evaluation_source_identity_ignores_ambient_git_work_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            ambient_work_tree = Path(temp) / "ambient-work-tree"
            root.mkdir()
            ambient_work_tree.mkdir()
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            visible = root / "tests/visible.py"
            hidden = root / "tests/hidden.py"
            visible.parent.mkdir(parents=True)
            visible.write_text("VISIBLE = True\n", encoding="utf-8")
            hidden.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", str(visible)], cwd=root, check=True)

            with mock.patch.dict(
                os.environ,
                {"GIT_WORK_TREE": str(ambient_work_tree)},
                clear=False,
            ):
                original = run_agent_e2e_eval.evaluation_source_identity(root)
                hidden.write_text(
                    "raise RuntimeError('ambient worktree drift')\n",
                    encoding="utf-8",
                )
                changed = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertNotEqual(
            original["workspace_sha256"],
            changed["workspace_sha256"],
        )

    def test_evaluation_source_identity_pins_root_against_local_core_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            redirected = Path(temp) / "redirected-worktree"
            root.mkdir()
            redirected.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            visible = root / "tests/visible.py"
            visible.parent.mkdir(parents=True)
            visible.write_text("VISIBLE = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "tests/visible.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "core.worktree", str(redirected)],
                cwd=root,
                check=True,
            )

            original = run_agent_e2e_eval.evaluation_source_identity(root)
            hidden = root / "plugins/evil.py"
            hidden.parent.mkdir(parents=True)
            hidden.write_text("raise RuntimeError('must be bound')\n", encoding="utf-8")
            changed = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertNotEqual(original["workspace_sha256"], "")
        self.assertNotEqual(changed["workspace_sha256"], "")
        self.assertNotEqual(
            original["workspace_sha256"],
            changed["workspace_sha256"],
        )
        self.assertIs(changed["git_dirty"], True)

    def test_evaluation_source_identity_disables_lazy_git_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            subprocess.run(
                ["git", "config", "user.email", "eval@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Eval Test"],
                cwd=root,
                check=True,
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", str(source)], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            original_run = subprocess.run
            git_environments: list[dict[str, str] | None] = []
            git_commands: list[list[str]] = []

            def record_git_environment(
                command: list[str],
                *args: object,
                **kwargs: object,
            ) -> subprocess.CompletedProcess[object]:
                if command and command[0] == "git":
                    environment = kwargs.get("env")
                    git_commands.append(command)
                    git_environments.append(
                        environment if isinstance(environment, dict) else None
                    )
                return original_run(command, *args, **kwargs)

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "GIT_DIR": str(root / "ambient-git-dir"),
                        "GIT_WORK_TREE": str(root / "ambient-work-tree"),
                    },
                    clear=False,
                ),
                mock.patch.object(
                    run_agent_e2e_eval.subprocess,
                    "run",
                    side_effect=record_git_environment,
                ),
            ):
                identity = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertNotEqual(identity["workspace_sha256"], "")
        self.assertTrue(any("ls-tree" in command for command in git_commands))
        self.assertTrue(git_environments)
        for environment in git_environments:
            with self.subTest(environment=environment):
                self.assertIsNotNone(environment)
                assert environment is not None
                self.assertEqual(environment.get("GIT_NO_LAZY_FETCH"), "1")
                self.assertEqual(environment.get("GIT_NO_REPLACE_OBJECTS"), "1")
                self.assertEqual(environment.get("GIT_OPTIONAL_LOCKS"), "0")
                self.assertEqual(environment.get("GIT_TERMINAL_PROMPT"), "0")
                self.assertEqual(environment.get("GIT_WORK_TREE"), str(root.resolve()))
                self.assertEqual(
                    {
                        key.upper()
                        for key in environment
                        if key.upper().startswith("GIT_")
                    },
                    {
                        "GIT_NO_LAZY_FETCH",
                        "GIT_NO_REPLACE_OBJECTS",
                        "GIT_OPTIONAL_LOCKS",
                        "GIT_TERMINAL_PROMPT",
                        "GIT_WORK_TREE",
                    },
                )

    def test_evaluation_source_identity_fails_closed_on_missing_git_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            subprocess.run(
                ["git", "config", "user.email", "eval@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Eval Test"],
                cwd=root,
                check=True,
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", str(source)], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            original_run = subprocess.run

            def fail_local_object_read(
                command: list[str],
                *args: object,
                **kwargs: object,
            ) -> subprocess.CompletedProcess[object]:
                if "ls-tree" in command:
                    environment = kwargs.get("env")
                    self.assertIsInstance(environment, dict)
                    assert isinstance(environment, dict)
                    self.assertEqual(environment.get("GIT_NO_LAZY_FETCH"), "1")
                    self.assertEqual(environment.get("GIT_NO_REPLACE_OBJECTS"), "1")
                    raise subprocess.CalledProcessError(128, command)
                return original_run(command, *args, **kwargs)

            with mock.patch.object(
                run_agent_e2e_eval.subprocess,
                "run",
                side_effect=fail_local_object_read,
            ):
                identity = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertEqual(identity["workspace_sha256"], "")
        self.assertEqual(identity["git_head"], "")
        self.assertIsNone(identity["git_dirty"])

    def test_evaluation_source_identity_fails_closed_on_missing_git_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            subprocess.run(
                ["git", "config", "user.email", "eval@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Eval Test"],
                cwd=root,
                check=True,
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", str(source)], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            object_id = subprocess.run(
                ["git", "rev-parse", "HEAD:tests/source.py"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            loose_object = root / ".git/objects" / object_id[:2] / object_id[2:]
            self.assertTrue(loose_object.is_file())
            unlink_git_object(loose_object)

            identity = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertEqual(identity["workspace_sha256"], "")
        self.assertEqual(identity["git_head"], "")
        self.assertIsNone(identity["git_dirty"])

    def test_evaluation_source_checkout_contract_forces_lf(self) -> None:
        self.assertEqual(
            (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8"),
            "* text=auto eol=lf\n",
        )
        self.assertIn(".gitattributes", run_agent_e2e_eval.EVALUATION_SOURCE_FILES)

    def test_evaluation_source_identity_does_not_execute_git_clean_filters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            source = root / "tests/source.py"
            source.parent.mkdir(parents=True)
            source.write_text("ORIGINAL = True\n", encoding="utf-8")
            subprocess.run(
                ["git", "config", "user.email", "eval@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Eval Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "core.trustctime", "false"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "core.checkStat", "minimal"],
                cwd=root,
                check=True,
            )
            source.write_text("A" * 100, encoding="utf-8")
            subprocess.run(["git", "add", "--", str(source)], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )

            marker = root / "clean-filter-executed.txt"
            filter_script = root / "constant-clean-filter.py"
            filter_script.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "Path(sys.argv[1]).write_text('executed', encoding='utf-8')\n"
                "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
                encoding="utf-8",
            )
            filter_command = " ".join(
                f'"{Path(argument).as_posix()}"'
                for argument in (sys.executable, filter_script, marker)
            )
            subprocess.run(
                ["git", "config", "filter.constant.clean", filter_command],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "filter.constant.required", "true"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "core.fsmonitor", filter_command],
                cwd=root,
                check=True,
            )
            attributes = root / ".git/info/attributes"
            attributes.parent.mkdir(parents=True, exist_ok=True)
            attributes.write_text("tests/source.py filter=constant\n", encoding="utf-8")

            source_stat = source.stat()
            marker.unlink(missing_ok=True)
            source.write_text("B" * 100, encoding="utf-8")
            os.utime(
                source,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            )
            first = run_agent_e2e_eval.evaluation_source_identity(root)
            filter_executed = marker.exists()
            marker.unlink(missing_ok=True)
            source.write_text("C" * 100, encoding="utf-8")
            os.utime(
                source,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            )
            changed = run_agent_e2e_eval.evaluation_source_identity(root)
            filter_executed = filter_executed or marker.exists()

        self.assertFalse(filter_executed, "source identity must not execute Git clean filters")
        self.assertNotEqual(first["workspace_sha256"], changed["workspace_sha256"])
        self.assertEqual(first["status_sha256"], changed["status_sha256"])

    def test_evaluation_source_identity_includes_ignored_runtime_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            tests_dir = root / "tests"
            tests_dir.mkdir(parents=True)
            loader = tests_dir / "loader.py"
            extension = tests_dir / "runtime_extension.py"
            loader.write_text("from . import runtime_extension\n", encoding="utf-8")
            extension.write_text("VALUE = 1\n", encoding="utf-8")
            (root / ".gitignore").write_text(
                "tests/runtime_extension.py\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "--", str(loader)], cwd=root, check=True)

            original = run_agent_e2e_eval.evaluation_source_identity(root)
            extension.write_text(
                "raise RuntimeError('ignored semantic drift')\n",
                encoding="utf-8",
            )
            changed = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertNotEqual(
            original["workspace_sha256"],
            changed["workspace_sha256"],
        )

    def test_evaluation_source_identity_has_unambiguous_file_framing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True, text=True
            )
            first_path = root / "tests/a"
            second_path = root / "tests/b"
            first_path.parent.mkdir(parents=True)

            first_path.write_bytes(b"X\0tests/b\0" + b"100644" + b"\0Y")
            one_file = run_agent_e2e_eval.evaluation_source_identity(root)

            first_path.write_bytes(b"X")
            second_path.write_bytes(b"Y")
            two_files = run_agent_e2e_eval.evaluation_source_identity(root)

        self.assertNotEqual(
            one_file["workspace_sha256"],
            two_files["workspace_sha256"],
        )

    def test_native_host_absolute_path_shape_is_cross_platform(self) -> None:
        self.assertTrue(
            run_agent_e2e_eval._is_cross_platform_absolute_path(
                "/Applications/Codex.app/Contents/Resources/codex"
            )
        )
        self.assertTrue(
            run_agent_e2e_eval._is_cross_platform_absolute_path(
                r"C:\Program Files\Codex\codex.exe"
            )
        )
        self.assertTrue(
            run_agent_e2e_eval._is_cross_platform_absolute_path(
                r"\\server\share\Codex\codex.exe"
            )
        )
        self.assertFalse(
            run_agent_e2e_eval._is_cross_platform_absolute_path("relative/codex")
        )
        for invalid in ("", "/", "C:relative\\codex.exe", "C:\\", "/abs/with\0nul"):
            with self.subTest(invalid=invalid):
                self.assertFalse(
                    run_agent_e2e_eval._is_cross_platform_absolute_path(invalid)
                )

    def test_persisted_report_keeps_historical_git_state_but_requires_current_source(self) -> None:
        report = json.loads(
            (REPO_ROOT / "docs/runtime/native-codex-live-eval.json").read_text(
                encoding="utf-8"
            )
        )
        historical_checkout = {
            **report["evaluation_source"],
            "git_head": "f" * 40,
            "git_dirty": False,
            "status_sha256": "e" * 64,
            "status_entry_count": 0,
        }
        with mock.patch.object(
            run_agent_e2e_eval,
            "evaluation_source_identity",
            return_value=historical_checkout,
        ):
            strict_errors = run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
                require_current_matrix=False,
            )
            persisted_errors = run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
                require_current_git_state=False,
                require_current_matrix=False,
            )

        self.assertTrue(any("current checkout" in error for error in strict_errors))
        self.assertEqual(persisted_errors, [])

        existing_but_historical = json.loads(json.dumps(report))
        existing_but_historical["native_host"]["resolved_path"] = str(EVAL.resolve())
        existing_but_historical["native_host"]["sha256"] = "f" * 64
        with mock.patch.object(
            run_agent_e2e_eval,
            "evaluation_source_identity",
            return_value=existing_but_historical["evaluation_source"],
        ):
            historical_binary_errors = run_agent_e2e_eval.report_consistency_errors(
                existing_but_historical,
                require_current_binary=False,
                require_current_git_state=False,
                require_current_matrix=False,
            )
        self.assertEqual(historical_binary_errors, [])

        foreign_binary = json.loads(json.dumps(report))
        foreign_binary["native_host"]["resolved_path"] = (
            "/Applications/Codex.app/Contents/Resources/codex"
            if os.name == "nt"
            else r"C:\Program Files\Codex\codex.exe"
        )
        with mock.patch.object(
            run_agent_e2e_eval,
            "evaluation_source_identity",
            return_value=foreign_binary["evaluation_source"],
        ):
            foreign_binary_errors = run_agent_e2e_eval.report_consistency_errors(
                foreign_binary,
                require_current_binary=True,
                require_current_git_state=False,
                require_current_matrix=False,
            )
        self.assertTrue(
            any("different operating system" in error for error in foreign_binary_errors)
        )

        changed_source = {
            **historical_checkout,
            "workspace_sha256": "0" * 64,
        }
        with mock.patch.object(
            run_agent_e2e_eval,
            "evaluation_source_identity",
            return_value=changed_source,
        ):
            changed_source_errors = run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
                require_current_git_state=False,
                require_current_matrix=False,
            )
        self.assertTrue(
            any("workspace_sha256" in error for error in changed_source_errors)
        )

        zero_head = json.loads(json.dumps(report))
        zero_head["evaluation_source"]["git_head"] = "0" * 40
        zero_head_errors = run_agent_e2e_eval.report_consistency_errors(
            zero_head,
            require_current_binary=False,
            require_current_git_state=False,
            require_current_matrix=False,
        )
        self.assertTrue(any("git_head" in error for error in zero_head_errors))

        for generated_at in (None, "not-a-timestamp", "2026-07-12T06:00:00"):
            with self.subTest(generated_at=generated_at):
                invalid_time = json.loads(json.dumps(report))
                invalid_time["evaluation_source"]["generated_at"] = generated_at
                invalid_time_errors = run_agent_e2e_eval.report_consistency_errors(
                    invalid_time,
                    require_current_binary=False,
                    require_current_git_state=False,
                    require_current_matrix=False,
                )
                self.assertTrue(
                    any("generated_at" in error for error in invalid_time_errors)
                )
                self.assertTrue(run_agent_e2e_eval.should_fail(invalid_time))

    def test_closed_persistent_eligibility_separates_history_from_current_cleanliness(self) -> None:
        report = json.loads(
            (REPO_ROOT / "docs/runtime/native-codex-live-eval.json").read_text(
                encoding="utf-8"
            )
        )
        with mock.patch.object(
            run_agent_e2e_eval,
            "evaluation_source_identity",
            side_effect=AssertionError("historical validation consulted current checkout"),
        ):
            historical_errors = run_agent_e2e_eval.persistent_evidence_errors(
                report,
                eligibility="historical-integrity",
            )
        with mock.patch.object(
            run_agent_e2e_eval,
            "evaluation_source_identity",
            return_value=report["evaluation_source"],
        ):
            current_errors = run_agent_e2e_eval.persistent_report_consistency_errors(
                report,
                require_current_binary=False,
                require_current_git_state=True,
                require_current_matrix=False,
                require_current_source=True,
                require_clean_source=True,
            )

        self.assertEqual(historical_errors, [])
        self.assertTrue(any("not clean" in error for error in current_errors), current_errors)

    def test_validate_evidence_cli_only_validates_stdin_and_rejects_incomplete_native(self) -> None:
        detail = (REPO_ROOT / "docs/runtime/native-codex-live-eval.json").read_text(
            encoding="utf-8"
        )
        valid = subprocess.run(
            [
                sys.executable,
                str(EVAL),
                "--validate-evidence",
                "historical-integrity",
            ],
            input=detail,
            text=True,
            capture_output=True,
            check=False,
        )
        invalid = subprocess.run(
            [
                sys.executable,
                str(EVAL),
                "--validate-evidence",
                "historical-integrity",
            ],
            input='{"report_version":1,"mode":"live-codex"}\n',
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        self.assertTrue(json.loads(valid.stdout)["ok"])
        self.assertNotEqual(invalid.returncode, 0)
        self.assertFalse(json.loads(invalid.stdout)["ok"])

    def test_fixture_eval_runs_six_real_local_kernel_scenarios(self) -> None:
        report = run_eval("--mode", "fixture")

        summary = report["summary"]
        scenarios = {scenario["name"]: scenario for scenario in report["scenarios"]}
        self.assertEqual(report["mode"], "fixture")
        self.assertEqual(len(report["evaluation_source"]["workspace_sha256"]), 64)
        self.assertEqual(len(report["evaluation_source"]["status_sha256"]), 64)
        self.assertTrue(report["evaluation_source"]["generated_at"])
        self.assertIn("matrix", report)
        self.assertEqual(report["matrix"]["profile"], "fixture")
        self.assertEqual(report["evidence_scope"], "deterministic-local-runtime")
        self.assertEqual(summary["scenario_count"], 6)
        self.assertEqual(summary["passed_count"], 6)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["skipped_count"], 0)
        self.assertEqual(summary["false_pass_count"], 0)
        self.assertEqual(summary["forged_evidence_block_count"], 1)
        self.assertEqual(summary["expected_human_review_required_count"], 1)
        self.assertEqual(summary["human_intervention_count"], 0)
        self.assertEqual(summary["sqlite_lock_error_count"], 0)
        self.assertNotIn("task_once_completion_rate", summary)
        self.assertNotIn("retry_count", summary)
        self.assertIsNone(report["token_count"])
        self.assertIsNone(report["agent_runtime_seconds"])
        self.assertEqual(
            set(scenarios),
            {
                "fresh_local_install_and_init",
                "quickstart_stops_before_independent_review",
                "current_candidate_supersedes_stale_validation",
                "manual_evidence_cannot_satisfy_delivery",
                "open_high_finding_blocks_delivery",
                "high_risk_requires_human_review",
            },
        )
        self.assertTrue(all(scenario["pass"] for scenario in scenarios.values()))

    def test_success_scenarios_do_not_replace_delivery_validator(self) -> None:
        tree = ast.parse(EVAL.read_text(encoding="utf-8"))
        assignments = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "validate_runtime":
                    assignments.append(node.lineno)

        self.assertEqual(assignments, [], f"release-critical validate_runtime replaced at lines {assignments}")

    def test_stability_eval_runs_matrix_scenarios(self) -> None:
        report = run_eval("--mode", "stability")

        summary = report["summary"]
        scenarios = {scenario["name"]: scenario for scenario in report["scenarios"]}
        self.assertEqual(report["mode"], "stability")
        self.assertEqual(report["matrix"]["profile"], "stability")
        self.assertTrue(report["matrix"]["sqlite_stress"])
        self.assertEqual(summary["scenario_count"], 11)
        self.assertEqual(summary["passed_count"], 11)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["skipped_count"], 0)
        self.assertEqual(summary["false_pass_count"], 0)
        self.assertEqual(summary["forged_evidence_block_count"], 1)
        self.assertEqual(summary["expected_human_review_required_count"], 1)
        self.assertEqual(summary["sqlite_lock_error_count"], 0)
        self.assertEqual(summary["human_intervention_count"], 0)
        self.assertEqual(
            set(scenarios),
            {
                "fresh_local_install_and_init",
                "quickstart_stops_before_independent_review",
                "current_candidate_supersedes_stale_validation",
                "manual_evidence_cannot_satisfy_delivery",
                "open_high_finding_blocks_delivery",
                "high_risk_requires_human_review",
                "structured_and_no_network_policy_fail_closed",
                "cycle_isolation",
                "sqlite_contention_stress",
                "schema27_to_active_migration_and_rollback",
                "installed_plugin_surface",
            },
        )
        self.assertTrue(scenarios["sqlite_contention_stress"]["pass"], scenarios["sqlite_contention_stress"]["details"])
        self.assertEqual(scenarios["sqlite_contention_stress"]["details"]["sqlite_lock_error_count"], 0)
        self.assertTrue(
            scenarios["schema27_to_active_migration_and_rollback"]["details"][
                "rollback_observed"
            ]
        )
        installed_details = scenarios["installed_plugin_surface"]["details"]
        self.assertEqual(installed_details["skill_count"], 7)
        self.assertEqual(installed_details["hook_count"], 3)
        self.assertEqual(installed_details["template_count"], 3)
        self.assertEqual(installed_details["project_template_count"], 3)
        self.assertEqual(installed_details["schema_count"], 18)
        self.assertEqual(installed_details["core_count"], 20)
        self.assertEqual(installed_details["runtime_script_count"], 7)
        self.assertEqual(installed_details["hook_file_count"], 2)
        self.assertEqual(installed_details["reference_count"], 3)
        self.assertEqual(installed_details["public_runtime_domain_count"], 22)

    def test_installed_surface_rejects_undeclared_nested_runtime_file(self) -> None:
        nested_relative = Path("core/nested/undeclared.py")

        def install_fixture(
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            environment = kwargs.get("env")
            self.assertIsInstance(environment, dict)
            home = Path(str(environment["HOME"]))  # type: ignore[index]
            installed = home / ".agents/plugins/codex-project-harness"
            shutil.copytree(
                run_agent_e2e_eval.PLUGIN_ROOT,
                installed,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            extra = installed / nested_relative
            extra.parent.mkdir(parents=True)
            extra.write_text("undeclared nested runtime file\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "installed\n", "")

        with mock.patch.object(
            run_agent_e2e_eval.subprocess,
            "run",
            side_effect=install_fixture,
        ):
            scenario = run_agent_e2e_eval.scenario_installed_plugin_surface()

        self.assertFalse(scenario["pass"], scenario["details"])

    def test_installed_surface_follows_manifest_hook_file_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            plugin = repo / "plugins/codex-project-harness"
            plugin.parent.mkdir(parents=True)
            shutil.copytree(
                REPO_ROOT / "kafa",
                repo / "kafa",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            shutil.copytree(
                run_agent_e2e_eval.PLUGIN_ROOT,
                plugin,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            for name in ("VERSION", "release.json", "pyproject.toml"):
                shutil.copyfile(REPO_ROOT / name, repo / name)

            hooks_root = plugin / "hooks"
            old_definition = hooks_root / "hooks.json"
            old_runner = hooks_root / "harness_hook.py"
            new_definition = hooks_root / "event-bindings.json"
            new_runner = hooks_root / "event_runner.py"
            old_definition.rename(new_definition)
            old_runner.rename(new_runner)

            manifest_path = plugin / "references/distribution-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["hooks"]["files"] = [
                new_definition.name,
                new_runner.name,
            ]
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            hooks = json.loads(new_definition.read_text(encoding="utf-8"))
            for groups in hooks["hooks"].values():
                for group in groups:
                    for hook in group["hooks"]:
                        hook["command"] = hook["command"].replace(
                            "harness_hook.py",
                            new_runner.name,
                        )
                        hook["commandWindows"] = hook["commandWindows"].replace(
                            "harness_hook.py",
                            new_runner.name,
                        )
            new_definition.write_text(
                json.dumps(hooks, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            structure = subprocess.run(
                [
                    sys.executable,
                    str(plugin / "scripts/validate_structure.py"),
                    str(plugin),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                structure.returncode,
                0,
                structure.stdout + structure.stderr,
            )

            with mock.patch.object(run_agent_e2e_eval, "ROOT", repo):
                scenario = run_agent_e2e_eval.scenario_installed_plugin_surface()

        self.assertTrue(scenario["pass"], scenario["details"])
        self.assertEqual(scenario["details"]["hook_file_count"], 2)

    def test_eval_source_contains_no_retired_provider_or_connector_scenarios(self) -> None:
        source = EVAL.read_text(encoding="utf-8")

        for marker in (
            "HostCodexProvider",
            "scenario_host_codex_fake_sdk_e2e",
            "connector_mock",
            "scenario_connector",
            "spark_policy",
            "provider_crash_recovery",
            "native_receipt",
        ):
            self.assertNotIn(marker, source)

    def test_live_codex_without_enable_is_not_run_and_fails_explicit_profile(self) -> None:
        result = run_eval_process("--mode", "live-codex", env={"HARNESS_E2E_ENABLE_LIVE_CODEX": ""})
        report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["mode"], "live-codex")
        self.assertTrue(report["live_skipped"])
        self.assertEqual(report["live_status"], "not-run")
        self.assertEqual(report["summary"]["passed_count"], 0)
        self.assertEqual(report["summary"]["failed_count"], 0)
        self.assertEqual(report["summary"]["skipped_count"], 1)
        self.assertTrue(all(not scenario["pass"] for scenario in report["scenarios"]))
        self.assertEqual({scenario["name"] for scenario in report["scenarios"]}, {"native_codex_edit_and_controller_verify"})
        self.assertIn("HARNESS_E2E_ENABLE_LIVE_CODEX", "; ".join(report["matrix"]["live_skipped_reasons"]))

    def test_live_codex_enabled_without_authenticated_codex_is_blocked(self) -> None:
        result = run_eval_process(
            "--mode",
            "live-codex",
            env={
                "HARNESS_E2E_ENABLE_LIVE_CODEX": "1",
                "HARNESS_E2E_CODEX_BIN": sys.executable,
            },
        )
        report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report["live_skipped"])
        self.assertEqual(report["live_status"], "blocked")
        self.assertEqual(report["summary"]["passed_count"], 0)
        self.assertGreaterEqual(report["summary"]["failed_count"], 1)
        self.assertTrue(all(not scenario["skip_reason"] for scenario in report["scenarios"]))
        self.assertTrue(all(scenario["details"]["capability_status"] == "blocked" for scenario in report["scenarios"]))

    def test_live_codex_environment_copies_only_auth_into_isolated_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            target = Path(temp) / "isolated"
            source.mkdir()
            (source / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            (source / "config.toml").write_text("model = 'fixture'\n", encoding="utf-8")
            (source / "plugins").mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(source),
                    "HARNESS_CODEX_MODEL_POLICY": "retired-policy-must-not-leak",
                    "N8N_MCP_ACCESS_TOKEN": "ambient-secret-must-not-leak",
                },
            ):
                env = run_agent_e2e_eval.isolated_live_codex_environment(target)

            self.assertEqual(Path(env["CODEX_HOME"]), target)
            self.assertEqual(Path(env["HOME"]), target)
            self.assertEqual({path.name for path in target.iterdir()}, {"auth.json"})
            self.assertEqual((target / "auth.json").read_text(encoding="utf-8"), '{"fixture": true}\n')
            self.assertIsNone(env.get("HARNESS_CODEX_MODEL_POLICY"))
            self.assertIsNone(env.get("N8N_MCP_ACCESS_TOKEN"))

    def test_report_validator_rejects_unknown_mode_and_forged_fixture_inventory(self) -> None:
        started = time.perf_counter()
        unknown = run_agent_e2e_eval.summarize(
            "connector",
            [
                run_agent_e2e_eval.scenario_result(
                    "forged_connector_report",
                    started,
                    True,
                    {
                        "controller_verify_returncode": 99,
                        "retired_host_tables": ["agent_sessions"],
                    },
                    category="connector",
                    mode="connector",
                )
            ],
            started,
        )
        self.assertTrue(run_agent_e2e_eval.report_consistency_errors(unknown))
        self.assertTrue(run_agent_e2e_eval.should_fail(unknown))

        forged_scenarios = []
        for index in range(len(run_agent_e2e_eval.FIXTURE_SCENARIOS)):
            details = {
                "false_pass_count": 0,
                "human_intervention_count": 0,
            }
            if index == 0:
                details["forged_evidence_block_count"] = 1
            if index == 1:
                details["expected_human_review_required_count"] = 1
            forged_scenarios.append(
                run_agent_e2e_eval.scenario_result(
                    f"forged_fixture_{index}",
                    started,
                    True,
                    details,
                    category="local",
                    mode="local",
                )
            )
        forged_fixture = run_agent_e2e_eval.summarize(
            "fixture",
            forged_scenarios,
            started,
        )
        self.assertTrue(run_agent_e2e_eval.report_consistency_errors(forged_fixture))
        self.assertTrue(run_agent_e2e_eval.should_fail(forged_fixture))

    def test_report_validator_binds_generation_matrix_facts(self) -> None:
        report = run_eval("--mode", "fixture")
        self.assertEqual(run_agent_e2e_eval.report_consistency_errors(report), [])

        mutations = {
            "platform": "ConnectorOS",
            "python_version": "",
            "git_version": "forged",
            "container_available": "yes",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                tampered = json.loads(json.dumps(report))
                tampered["matrix"][field] = value
                errors = run_agent_e2e_eval.report_consistency_errors(tampered)
                self.assertTrue(
                    any(f"matrix {field}" in error for error in errors),
                    errors,
                )
                self.assertTrue(run_agent_e2e_eval.should_fail(tampered))

        historical = json.loads(json.dumps(report))
        historical["matrix"].update(
            {
                "platform": "HistoricalOS-1",
                "python_version": "3.11.9",
                "git_version": "git version 2.45.1",
                "container_available": not report["matrix"]["container_available"],
            }
        )
        self.assertEqual(
            run_agent_e2e_eval.persistent_report_consistency_errors(historical),
            [],
        )

    def test_report_validator_rejects_boolean_version_and_summary_numbers(self) -> None:
        report = run_eval("--mode", "fixture")
        self.assertEqual(run_agent_e2e_eval.report_consistency_errors(report), [])

        mutations = (
            ("report_version", None, True),
            ("summary", "failed_count", False),
            ("summary", "skipped_count", False),
            ("summary", "false_pass_count", False),
            ("summary", "forged_evidence_block_count", True),
            ("summary", "expected_human_review_required_count", True),
            ("summary", "sqlite_lock_error_count", False),
            ("summary", "human_intervention_count", False),
            ("summary", "scenario_pass_rate", True),
        )
        for surface, field, value in mutations:
            with self.subTest(surface=surface, field=field):
                tampered = json.loads(json.dumps(report))
                if surface == "report_version":
                    tampered["report_version"] = value
                    label = "report_version"
                else:
                    tampered["summary"][field] = value
                    label = f"summary {field}"
                errors = run_agent_e2e_eval.report_consistency_errors(tampered)
                self.assertTrue(
                    any(label in error for error in errors),
                    errors,
                )
                self.assertTrue(run_agent_e2e_eval.should_fail(tampered))

    def test_report_validator_rejects_negative_detail_counter_cancellation(self) -> None:
        report = run_eval("--mode", "fixture")
        self.assertEqual(run_agent_e2e_eval.report_consistency_errors(report), [])

        for field in (
            "false_pass_count",
            "human_intervention_count",
            "sqlite_lock_error_count",
        ):
            with self.subTest(field=field):
                tampered = json.loads(json.dumps(report))
                tampered["scenarios"][0]["details"][field] = 1
                tampered["scenarios"][1]["details"][field] = -1
                errors = run_agent_e2e_eval.report_consistency_errors(tampered)
                self.assertTrue(
                    any(field in error and "non-negative" in error for error in errors),
                    errors,
                )
                self.assertTrue(run_agent_e2e_eval.should_fail(tampered))

    def test_live_codex_profile_wiring_edits_then_controller_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            result = run_eval_process(
                "--mode",
                "live-codex",
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "HARNESS_E2E_LIVE_TIMEOUT": "30",
                    "CODEX_HOME": str(codex_home),
                },
            )
            report = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(report["live_skipped"])
        self.assertEqual(report["live_status"], "passed")
        self.assertEqual(report["summary"]["scenario_count"], 1)
        self.assertEqual(report["summary"]["passed_count"], 1)
        scenario = report["scenarios"][0]
        self.assertEqual(scenario["name"], "native_codex_edit_and_controller_verify")
        self.assertEqual(scenario["details"]["changed_files"], ["candidate.py"])
        self.assertEqual(scenario["details"]["exclusive_files"], ["candidate.py"])
        self.assertEqual(scenario["details"]["workload_units"], 1)
        self.assertEqual(scenario["details"]["native_token_scope"], "native-producers-only")
        self.assertTrue(scenario["details"]["test_file_unchanged"])
        self.assertEqual(scenario["details"]["controller_verify_returncode"], 0)
        self.assertEqual(scenario["details"]["execution_count"], 1)
        self.assertEqual(scenario["details"]["validation_count"], 1)
        self.assertEqual(scenario["details"]["task_status"], "submitted")
        self.assertTrue(scenario["details"]["provider_surface_absent"])
        self.assertEqual(scenario["details"]["retired_host_tables"], [])
        self.assertTrue(scenario["details"]["producer_scope_valid"])
        self.assertTrue(scenario["details"]["controller_state_unchanged_during_native"])
        self.assertEqual(scenario["details"]["integrated_files"], ["candidate.py"])
        self.assertEqual(report["token_count"], 1234)
        self.assertEqual(report["token_usage"]["input_tokens"], 1224)
        self.assertEqual(report["token_usage"]["output_tokens"], 10)
        self.assertGreater(report["agent_runtime_seconds"], 0)
        self.assertIsNone(report["estimated_cost"])
        self.assertEqual(
            report["native_host"]["trust"],
            "local-capability-only-not-delivery-provenance",
        )
        self.assertEqual(report["native_host"]["source"], "explicit-test-override")
        self.assertEqual(len(report["native_host"]["sha256"]), 64)
        self.assertEqual(
            run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
            ),
            [],
        )
        self.assertTrue(
            run_agent_e2e_eval.persistent_report_consistency_errors(report)
        )

        passing_contract_mutations = {
            "controller_verify_returncode": 99,
            "controller_verify_status": "failed",
            "execution_count": 0,
            "validation_count": 0,
            "task_status": "active",
            "provider_surface_absent": False,
            "retired_host_tables": ["agent_sessions"],
            "controller_state_unchanged_during_native": False,
            "controller_test_unchanged": False,
            "last_message_recorded": False,
        }
        for field, invalid_value in passing_contract_mutations.items():
            with self.subTest(passing_contract_field=field):
                tampered = json.loads(json.dumps(report))
                tampered["scenarios"][0]["details"][field] = invalid_value
                tampered["native_host"]["resolved_path"] = sys.executable
                tampered["native_host"]["sha256"] = hashlib.sha256(
                    Path(sys.executable).read_bytes()
                ).hexdigest()
                errors = run_agent_e2e_eval.report_consistency_errors(tampered)
                self.assertTrue(errors, f"passing single report trusted {field}")
                self.assertTrue(run_agent_e2e_eval.should_fail(tampered))

        exact_integer_mutations = {
            "native_returncode": (False, 0.0),
            "controller_verify_returncode": (False, 0.0),
            "task_submit_returncode": (False, 0.0),
            "execution_count": (True, 1.0),
            "validation_count": (True, 1.0),
            "workload_units": (True, 1.0),
        }
        for field, invalid_values in exact_integer_mutations.items():
            for invalid_value in invalid_values:
                with self.subTest(exact_integer_field=field, invalid_value=invalid_value):
                    tampered = json.loads(json.dumps(report))
                    tampered["scenarios"][0]["details"][field] = invalid_value
                    errors = run_agent_e2e_eval.report_consistency_errors(
                        tampered,
                        require_current_binary=False,
                    )
                    self.assertTrue(
                        any(field in error for error in errors),
                        errors,
                    )
                    self.assertTrue(run_agent_e2e_eval.should_fail(tampered))

        for field in ("token_count", "token_usage.input_tokens"):
            with self.subTest(exact_top_level_integer_field=field):
                tampered = json.loads(json.dumps(report))
                if field == "token_count":
                    tampered[field] = float(report[field])
                    error_label = "top-level token_count"
                else:
                    tampered["token_usage"]["input_tokens"] = float(
                        report["token_usage"]["input_tokens"]
                    )
                    error_label = "top-level input_tokens"
                errors = run_agent_e2e_eval.report_consistency_errors(
                    tampered,
                    require_current_binary=False,
                )
                self.assertTrue(
                    any(error_label in error for error in errors),
                    errors,
                )

        missing_usage = json.loads(json.dumps(report))
        missing_usage_details = missing_usage["scenarios"][0]["details"]
        for field in (
            "native_usage",
            "native_token_count",
            "native_runtime_seconds",
        ):
            missing_usage_details[field] = None
        missing_usage["token_usage"] = None
        missing_usage["token_count"] = None
        missing_usage["agent_runtime_seconds"] = None
        missing_usage_errors = run_agent_e2e_eval.report_consistency_errors(
            missing_usage,
            require_current_binary=False,
        )
        self.assertTrue(missing_usage_errors)

        forged_scope = json.loads(json.dumps(report))
        forged_scope["evidence_scope"] = "external-connector"
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                forged_scope,
                require_current_binary=False,
            )
        )

        connector_detail = json.loads(json.dumps(report))
        connector_detail["scenarios"][0]["details"]["connector_receipt"] = {
            "status": "passed"
        }
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                connector_detail,
                require_current_binary=False,
            )
        )

        unavailable_matrix = json.loads(json.dumps(report))
        unavailable_matrix["matrix"]["codex_available"] = False
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                unavailable_matrix,
                require_current_binary=False,
            )
        )

        skipped_matrix = json.loads(json.dumps(report))
        skipped_matrix["matrix"]["live_skipped_reasons"] = ["codex unavailable"]
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                skipped_matrix,
                require_current_binary=False,
            )
        )

        contradictory_git_state = json.loads(json.dumps(report))
        contradictory_git_state["evaluation_source"]["git_dirty"] = False
        contradictory_git_state["evaluation_source"]["status_entry_count"] = 4
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                contradictory_git_state,
                require_current_binary=False,
                require_current_git_state=False,
            )
        )

        forged_cost = json.loads(json.dumps(report))
        forged_cost["estimated_cost"] = 999
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                forged_cost,
                require_current_binary=False,
            )
        )

        zero_telemetry = json.loads(json.dumps(report))
        zero_usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "token_count": 0,
        }
        zero_details = zero_telemetry["scenarios"][0]["details"]
        zero_details["native_usage"] = zero_usage
        zero_details["native_token_count"] = 0
        zero_telemetry["token_usage"] = zero_usage
        zero_telemetry["token_count"] = 0
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                zero_telemetry,
                require_current_binary=False,
            )
        )

        nonfinite_runtime = json.loads(json.dumps(report))
        nonfinite_runtime["scenarios"][0]["details"][
            "native_runtime_seconds"
        ] = float("inf")
        nonfinite_runtime["agent_runtime_seconds"] = float("inf")
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                nonfinite_runtime,
                require_current_binary=False,
            )
        )

        zero_durations = json.loads(json.dumps(report))
        zero_durations["summary"]["duration_seconds"] = 0
        zero_durations["scenarios"][0]["duration_seconds"] = 0
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                zero_durations,
                require_current_binary=False,
            )
        )

        substituted_binary = json.loads(json.dumps(report))
        substituted_binary["native_host"]["resolved_path"] = sys.executable
        substituted_binary["native_host"]["sha256"] = hashlib.sha256(
            Path(sys.executable).read_bytes()
        ).hexdigest()
        substituted_binary["native_host"]["source"] = "path-discovery"
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(substituted_binary)
        )
        self.assertTrue(run_agent_e2e_eval.should_fail(substituted_binary))

        inconsistent = json.loads(json.dumps(report))
        inconsistent["summary"]["scenario_count"] = 99
        inconsistent["summary"]["duration_seconds"] = -1
        inconsistent["live_status"] = "blocked"
        inconsistent["token_count"] += 1
        inconsistent["evaluation_source"]["workspace_sha256"] = "0" * 64
        inconsistent["evaluation_source"]["status_sha256"] = "1" * 64
        inconsistent["native_host"]["resolved_path"] = str(EVAL)
        inconsistent["native_host"]["sha256"] = "1" * 64
        inconsistent_details = inconsistent["scenarios"][0]["details"]
        inconsistent_details["integrated_files"] = []
        inconsistent_details["native_token_source"] = "assistant-text"
        errors = run_agent_e2e_eval.report_consistency_errors(inconsistent)
        self.assertTrue(any("scenario_count" in error for error in errors))
        self.assertTrue(any("summary duration_seconds" in error for error in errors))
        self.assertTrue(any("live_status" in error for error in errors))
        self.assertTrue(any("top-level token_count" in error for error in errors))
        self.assertTrue(any("nonzero SHA-256" in error for error in errors))
        self.assertTrue(any("current checkout" in error for error in errors))
        self.assertTrue(any("resolved binary" in error for error in errors))
        self.assertTrue(any("integrated_files" in error for error in errors))
        self.assertTrue(any("native_token_source" in error for error in errors))
        self.assertTrue(run_agent_e2e_eval.should_fail(inconsistent))

        empty_scope = json.loads(json.dumps(report))
        empty_scope["scenarios"][0]["details"]["exclusive_files"] = []
        empty_scope_errors = run_agent_e2e_eval.report_consistency_errors(empty_scope)
        self.assertTrue(any("exclusive_files is empty" in error for error in empty_scope_errors))
        self.assertTrue(run_agent_e2e_eval.should_fail(empty_scope))

        missing_binary = json.loads(json.dumps(report))
        missing_binary["native_host"]["resolved_path"] = (
            r"C:\definitely\missing\kafa-codex.exe"
            if os.name == "nt"
            else "/definitely/missing/kafa-codex"
        )
        missing_binary_errors = run_agent_e2e_eval.report_consistency_errors(missing_binary)
        self.assertTrue(any("resolved binary is unavailable" in error for error in missing_binary_errors))
        self.assertTrue(run_agent_e2e_eval.should_fail(missing_binary))
        serialized = json.dumps(report, ensure_ascii=False)
        for key in run_agent_e2e_eval.VERBOSE_NATIVE_OUTPUT_KEYS:
            self.assertNotIn(key, serialized)

    def test_live_codex_rejects_unexpected_runtime_table_created_by_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root, inject_retired_table=True)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text(
                '{"fixture": true}\n',
                encoding="utf-8",
            )
            result = run_eval_process(
                "--mode",
                "live-codex",
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "HARNESS_E2E_LIVE_TIMEOUT": "30",
                    "CODEX_HOME": str(codex_home),
                },
            )
            report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["live_status"], "failed")
        details = report["scenarios"][0]["details"]
        self.assertFalse(details["provider_surface_absent"])
        self.assertEqual(details["retired_host_tables"], ["adapter_actions"])
        self.assertTrue(run_agent_e2e_eval.should_fail(report))

    def test_active_table_contract_rejects_unexpected_sqlite_prefixed_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_agent_e2e_eval.harness_db.init_runtime(root)
            database = root / ".ai-team/state/harness.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("create table adapter_actions(id text)")
                conn.execute("pragma writable_schema=on")
                conn.execute(
                    """
                    update sqlite_master
                    set name='sqlite_adapter_actions',
                        tbl_name='sqlite_adapter_actions',
                        sql='CREATE TABLE sqlite_adapter_actions(id text)'
                    where name='adapter_actions'
                    """
                )
                conn.commit()
            with closing(sqlite3.connect(database)) as conn:
                hidden_table_count = int(
                    conn.execute(
                        "select count(*) from sqlite_adapter_actions"
                    ).fetchone()[0]
                )
            unexpected, exact = run_agent_e2e_eval.active_table_contract(root)
            doctor_issues = run_agent_e2e_eval.harness_db.doctor(
                root,
                require_project_files=False,
            )

        self.assertEqual(hidden_table_count, 0)
        self.assertIn("sqlite_adapter_actions", unexpected)
        self.assertFalse(exact)
        self.assertTrue(
            any("sqlite_adapter_actions" in issue for issue in doctor_issues),
            doctor_issues,
        )

    def test_native_usage_parser_accepts_only_structured_turn_completion(self) -> None:
        output = "\n".join(
            [
                json.dumps({"type": "item.completed", "item": {"text": "tokens used\\n999"}}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 13400,
                            "cached_input_tokens": 2000,
                            "output_tokens": 66,
                            "reasoning_output_tokens": 12,
                        },
                    }
                ),
            ]
        )

        usage = run_agent_e2e_eval.parse_native_usage_jsonl(output)

        self.assertEqual(usage["token_count"], 13466)
        self.assertEqual(usage["cached_input_tokens"], 2000)
        self.assertIsNone(run_agent_e2e_eval.parse_native_usage_jsonl("tokens used\n999\n"))
        self.assertIsNone(
            run_agent_e2e_eval.parse_native_usage_jsonl(output + "\n" + output.splitlines()[-1])
        )
        invalid_reasoning = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 2,
                },
            }
        )
        self.assertIsNone(run_agent_e2e_eval.parse_native_usage_jsonl(invalid_reasoning))

    def test_explicit_binary_override_cannot_write_persistent_live_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text(
                '{"fixture": true}\n', encoding="utf-8"
            )
            evidence = root / "persistent-live.json"
            result = run_eval_process(
                "--mode",
                "live-codex",
                "--evidence-out",
                str(evidence),
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "HARNESS_E2E_LIVE_TIMEOUT": "30",
                    "CODEX_HOME": str(codex_home),
                },
            )
            evidence_exists = evidence.exists()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing persistent evidence", result.stderr)
        self.assertFalse(evidence_exists)

    def test_compact_evidence_report_removes_only_verbose_native_output(self) -> None:
        report = {
            "token_count": 10,
            "details": {
                "native_stdout_tail": "verbose",
                "native_stderr_tail": "verbose",
                "controller_verify_output": "verbose",
                "result": "pass",
                "producers": [
                    {"stdout_tail": "verbose", "stderr_tail": "verbose", "token_count": 10}
                ],
            },
        }

        compact = run_agent_e2e_eval.compact_evidence_report(report)

        self.assertEqual(compact["token_count"], 10)
        self.assertEqual(compact["details"]["result"], "pass")
        self.assertEqual(compact["details"]["producers"], [{"token_count": 10}])
        self.assertFalse(run_agent_e2e_eval.VERBOSE_NATIVE_OUTPUT_KEYS & set(compact["details"]))

    def test_live_codex_has_no_permanent_repository_profile_skip(self) -> None:
        source = EVAL.read_text(encoding="utf-8")

        self.assertNotIn("no repository-local live profile is configured", source)

    def test_live_parallel_scope_guard_rejects_shared_write_paths(self) -> None:
        conflicts = run_agent_e2e_eval.live_eval_scope_conflicts(
            [
                {"task": "A", "exclusive_files": ["shared.py", "alpha.py"]},
                {"task": "B", "exclusive_files": ["shared.py", "beta.py"]},
            ]
        )

        self.assertEqual(conflicts, {"shared.py": ["A", "B"]})
        aliases = run_agent_e2e_eval.live_eval_scope_conflicts(
            [
                {"task": "A", "exclusive_files": ["alpha.py"]},
                {"task": "B", "exclusive_files": ["./alpha.py"]},
            ]
        )
        invalid = run_agent_e2e_eval.live_eval_scope_conflicts(
            [{"task": "A", "exclusive_files": ["../escape.py"]}]
        )
        self.assertEqual(aliases, {"alpha.py": ["A", "B"]})
        self.assertEqual(invalid, {"<invalid:../escape.py>": ["A"]})

    def test_live_parallel_without_enable_is_not_run(self) -> None:
        result = run_eval_process(
            "--mode",
            "live-codex-parallel",
            env={"HARNESS_E2E_ENABLE_LIVE_CODEX_PARALLEL": ""},
        )
        report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report["mode"], "live-codex-parallel")
        self.assertTrue(report["live_skipped"])
        self.assertEqual(report["live_status"], "not-run")
        self.assertEqual(report["summary"]["passed_count"], 0)
        self.assertEqual(report["summary"]["skipped_count"], 1)
        self.assertEqual(
            {scenario["name"] for scenario in report["scenarios"]},
            {"native_codex_two_producer_integration"},
        )

    def test_live_parallel_profile_runs_two_disjoint_producers_and_combined_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            result = run_eval_process(
                "--mode",
                "live-codex-parallel",
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX_PARALLEL": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "HARNESS_E2E_LIVE_TIMEOUT": "30",
                    "CODEX_HOME": str(codex_home),
                },
            )
            report = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(report["live_status"], "passed")
        self.assertEqual(report["token_count"], 1300)
        self.assertGreater(report["agent_runtime_seconds"], 0)
        scenario = report["scenarios"][0]
        details = scenario["details"]
        self.assertEqual(details["producer_count"], 2)
        self.assertGreater(details["producer_overlap_seconds"], 0)
        self.assertEqual(details["workload_units"], 2)
        self.assertEqual(details["native_token_scope"], "native-producers-only")
        self.assertEqual(
            details["producer_overlap_seconds"],
            round(
                min(producer["finished_offset_seconds"] for producer in details["producers"])
                - max(producer["started_offset_seconds"] for producer in details["producers"]),
                6,
            ),
        )
        self.assertEqual(details["changed_files"], ["alpha.py", "beta.py"])
        self.assertEqual(details["integrated_files"], ["alpha.py", "beta.py"])
        self.assertTrue(details["producer_attribution_valid"])
        self.assertTrue(details["controller_state_unchanged_during_native"])
        self.assertTrue(details["test_files_unchanged"])
        self.assertEqual(details["targeted_verify_returncodes"], {"LIVE-ALPHA": 0, "LIVE-BETA": 0})
        self.assertEqual(details["combined_verify_returncode"], 0)
        self.assertEqual(
            details["task_statuses"],
            {"LIVE-INTEGRATE": "submitted", "LIVE-P1": "accepted", "LIVE-P2": "accepted"},
        )
        self.assertEqual(details["scope_conflicts"], {})
        self.assertEqual(details["overlap_policy"], "block-parallel-on-declared-overlap")
        self.assertEqual(
            details["scope_enforcement"],
            "isolated-producer-workspaces-plus-exact-diff-integration",
        )
        self.assertEqual(details["retired_host_tables"], [])
        self.assertEqual(
            run_agent_e2e_eval.report_consistency_errors(
                report,
                require_current_binary=False,
            ),
            [],
        )

        parallel_contract_mutations = {
            "controller_state_unchanged_during_native": False,
            "test_files_unchanged": False,
            "targeted_verify_returncodes": {"LIVE-ALPHA": 99, "LIVE-BETA": 0},
            "combined_verify_returncode": 99,
            "combined_verify_status": "failed",
            "integration_dependency_blocked_before_producers": False,
            "task_statuses": {
                "LIVE-INTEGRATE": "active",
                "LIVE-P1": "submitted",
                "LIVE-P2": "submitted",
            },
            "execution_count": 0,
            "validation_count": 0,
            "retired_host_tables": ["agent_sessions"],
            "scope_enforcement": "unverified",
            "overlap_policy": "allow-overlap",
        }
        for field, invalid_value in parallel_contract_mutations.items():
            with self.subTest(parallel_contract_field=field):
                tampered = json.loads(json.dumps(report))
                tampered["scenarios"][0]["details"][field] = invalid_value
                tampered["native_host"]["resolved_path"] = sys.executable
                tampered["native_host"]["sha256"] = hashlib.sha256(
                    Path(sys.executable).read_bytes()
                ).hexdigest()
                errors = run_agent_e2e_eval.report_consistency_errors(tampered)
                self.assertTrue(errors, f"passing parallel report trusted {field}")
                self.assertTrue(run_agent_e2e_eval.should_fail(tampered))

        parallel_exact_integer_mutations = {
            "producer_count": (2.0,),
            "workload_units": (2.0,),
            "integration_start_returncode": (False, 0.0),
            "combined_verify_returncode": (False, 0.0),
            "integration_submit_returncode": (False, 0.0),
            "execution_count": (3.0,),
            "validation_count": (3.0,),
        }
        for field, invalid_values in parallel_exact_integer_mutations.items():
            for invalid_value in invalid_values:
                with self.subTest(exact_parallel_field=field, invalid_value=invalid_value):
                    tampered = json.loads(json.dumps(report))
                    tampered["scenarios"][0]["details"][field] = invalid_value
                    errors = run_agent_e2e_eval.report_consistency_errors(
                        tampered,
                        require_current_binary=False,
                    )
                    self.assertTrue(
                        any(field in error for error in errors),
                        errors,
                    )
                    self.assertTrue(run_agent_e2e_eval.should_fail(tampered))

        for field, invalid_value in (
            ("targeted_verify_returncodes", 0.0),
            ("producer_state_returncodes", 0.0),
            ("producer_returncode", False),
            ("producer_returncode", 0.0),
            ("producer_token_count", 600.0),
        ):
            with self.subTest(exact_nested_integer_field=field, invalid_value=invalid_value):
                tampered = json.loads(json.dumps(report))
                tampered_details = tampered["scenarios"][0]["details"]
                if field == "targeted_verify_returncodes":
                    tampered_details[field]["LIVE-ALPHA"] = invalid_value
                elif field == "producer_state_returncodes":
                    tampered_details[field][0] = invalid_value
                elif field == "producer_returncode":
                    tampered_details["producers"][0]["returncode"] = invalid_value
                else:
                    tampered_details["producers"][0]["token_count"] = invalid_value
                errors = run_agent_e2e_eval.report_consistency_errors(
                    tampered,
                    require_current_binary=False,
                )
                self.assertTrue(
                    any(
                        field in error
                        or (field.startswith("producer_") and "producer" in error)
                        for error in errors
                    ),
                    errors,
                )
                self.assertTrue(run_agent_e2e_eval.should_fail(tampered))

        missing_message = json.loads(json.dumps(report))
        missing_message["scenarios"][0]["details"]["producers"][0][
            "last_message_recorded"
        ] = False
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                missing_message,
                require_current_binary=False,
            )
        )

        swapped_tasks = json.loads(json.dumps(report))
        swapped_producers = swapped_tasks["scenarios"][0]["details"]["producers"]
        swapped_producers[0]["task"], swapped_producers[1]["task"] = (
            swapped_producers[1]["task"],
            swapped_producers[0]["task"],
        )
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                swapped_tasks,
                require_current_binary=False,
            )
        )

        unbalanced_scopes = json.loads(json.dumps(report))
        unbalanced_producers = unbalanced_scopes["scenarios"][0]["details"][
            "producers"
        ]
        unbalanced_producers[0]["exclusive_files"] = ["alpha.py", "beta.py"]
        unbalanced_producers[0]["changed_files"] = ["alpha.py", "beta.py"]
        unbalanced_producers[1]["exclusive_files"] = []
        unbalanced_producers[1]["changed_files"] = []
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                unbalanced_scopes,
                require_current_binary=False,
            )
        )

        contradictory_error = json.loads(json.dumps(report))
        contradictory_error["scenarios"][0]["details"]["producers"][0][
            "error"
        ] = "Native subprocess timed out"
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                contradictory_error,
                require_current_binary=False,
            )
        )

        host_worker_detail = json.loads(json.dumps(report))
        host_worker_detail["scenarios"][0]["details"]["producers"][0][
            "host_sdk_worker"
        ] = "present"
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(
                host_worker_detail,
                require_current_binary=False,
            )
        )

        inconsistent = json.loads(json.dumps(report))
        inconsistent_details = inconsistent["scenarios"][0]["details"]
        producers = inconsistent_details["producers"]
        producers[1]["exclusive_files"] = ["alpha.py"]
        producers[0]["returncode"] = 1
        producers[0]["test_file_unchanged"] = False
        producers[0]["runtime_seconds"] = 999
        producers[0]["token_source"] = "assistant-text"
        inconsistent_details["producer_count"] = 3
        inconsistent_details["producer_overlap_seconds"] += 0.25
        inconsistent_details["integrated_files"] = ["alpha.py"]
        errors = run_agent_e2e_eval.report_consistency_errors(inconsistent)
        self.assertTrue(any("scope_conflicts" in error for error in errors))
        self.assertTrue(any("producer_attribution_valid" in error for error in errors))
        self.assertTrue(any("producer_count" in error for error in errors))
        self.assertTrue(any("producer_overlap_seconds" in error for error in errors))
        self.assertTrue(any("integrated_files" in error for error in errors))
        self.assertTrue(any("runtime_seconds" in error for error in errors))
        self.assertTrue(any("token_source" in error for error in errors))
        self.assertTrue(run_agent_e2e_eval.should_fail(inconsistent))

    def test_live_single_rejects_producer_state_tampering_before_controller_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root, tamper_state=True)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            result = run_eval_process(
                "--mode",
                "live-codex",
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "CODEX_HOME": str(codex_home),
                },
            )
            report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        details = report["scenarios"][0]["details"]
        self.assertFalse(details["producer_scope_valid"])
        self.assertTrue(details["controller_state_unchanged_during_native"])
        self.assertEqual(details["integrated_files"], [])
        self.assertEqual(details["controller_verify_status"], "not-run")
        self.assertIn(".ai-team/state/harness.db", details["producer_changed_files"])

    def test_live_parallel_rejects_cross_producer_file_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake_codex = make_fake_codex(root, tamper_attribution=True)
            codex_home = root / "codex-home"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text('{"fixture": true}\n', encoding="utf-8")
            result = run_eval_process(
                "--mode",
                "live-codex-parallel",
                env={
                    "HARNESS_E2E_ENABLE_LIVE_CODEX_PARALLEL": "1",
                    "HARNESS_E2E_CODEX_BIN": str(fake_codex),
                    "CODEX_HOME": str(codex_home),
                },
            )
            report = json.loads(result.stdout)

        self.assertNotEqual(result.returncode, 0)
        details = report["scenarios"][0]["details"]
        self.assertFalse(details["producer_attribution_valid"])
        self.assertTrue(details["controller_state_unchanged_during_native"])
        self.assertEqual(details["integrated_files"], [])
        alpha = next(item for item in details["producers"] if item["task"] == "LIVE-P1")
        self.assertEqual(alpha["changed_files"], ["alpha.py", "beta.py"])

    def test_should_fail_thresholds(self) -> None:
        scenario_count = len(run_agent_e2e_eval.FIXTURE_SCENARIOS) + len(
            run_agent_e2e_eval.STABILITY_SCENARIOS
        )
        scenario_functions = [
            *run_agent_e2e_eval.FIXTURE_SCENARIOS,
            *run_agent_e2e_eval.STABILITY_SCENARIOS,
        ]
        scenarios = [
            {
                "name": scenario.__name__.removeprefix("scenario_"),
                "category": run_agent_e2e_eval.LOCAL_SCENARIO_CONTRACT[
                    scenario.__name__.removeprefix("scenario_")
                ][0],
                "mode": run_agent_e2e_eval.LOCAL_SCENARIO_CONTRACT[
                    scenario.__name__.removeprefix("scenario_")
                ][1],
                "pass": True,
                "skip_reason": "",
                "duration_seconds": 0.0,
                "details": (
                    {
                        "forged_evidence_block_count": 1,
                        "expected_human_review_required_count": 1,
                    }
                    if index == 0
                    else {}
                ),
            }
            for index, scenario in enumerate(scenario_functions)
        ]
        base = run_agent_e2e_eval.summarize("stability", scenarios, time.perf_counter())
        self.assertEqual(run_agent_e2e_eval.report_consistency_errors(base), [])
        self.assertFalse(run_agent_e2e_eval.should_fail(base))
        connector_scenarios = json.loads(json.dumps(base))
        for scenario in connector_scenarios["scenarios"]:
            scenario["category"] = "connector"
            scenario["mode"] = "connector"
        self.assertTrue(
            run_agent_e2e_eval.report_consistency_errors(connector_scenarios)
        )
        self.assertTrue(run_agent_e2e_eval.should_fail(connector_scenarios))
        locked = json.loads(json.dumps(base))
        locked["summary"]["sqlite_lock_error_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(locked))
        false_pass = json.loads(json.dumps(base))
        false_pass["summary"]["false_pass_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(false_pass))
        human_intervention = json.loads(json.dumps(base))
        human_intervention["summary"]["human_intervention_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(human_intervention))
        skipped = json.loads(json.dumps(base))
        skipped["summary"]["skipped_count"] = 1
        self.assertTrue(run_agent_e2e_eval.should_fail(skipped))
        missing_forged_block = json.loads(json.dumps(base))
        missing_forged_block["summary"]["forged_evidence_block_count"] = 0
        self.assertTrue(run_agent_e2e_eval.should_fail(missing_forged_block))
        live_skipped = {"mode": "live-codex", "live_skipped": True, "summary": {"failed_count": 0}}
        self.assertTrue(run_agent_e2e_eval.should_fail(live_skipped))

    def test_failed_profile_writes_diagnostic_out_but_preserves_persistent_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "report.json"
            evidence_out = Path(temp) / "evidence.json"
            prior_evidence = b'{"status":"historical"}\n'
            evidence_out.write_bytes(prior_evidence)
            result = run_eval_process(
                "--mode",
                "live-codex",
                "--out",
                str(out),
                "--evidence-out",
                str(evidence_out),
                env={"HARNESS_E2E_ENABLE_LIVE_CODEX": ""},
            )
            report = json.loads(result.stdout)
            from_file = json.loads(out.read_text(encoding="utf-8"))
            evidence_bytes = evidence_out.read_bytes()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(from_file, report)
        self.assertEqual(evidence_bytes, prior_evidence)
        self.assertIn("refusing failed persistent evidence", result.stderr)
        self.assertIn("matrix", from_file)
        self.assertIn("summary", from_file)
        self.assertIn("scenarios", from_file)


if __name__ == "__main__":
    unittest.main()
