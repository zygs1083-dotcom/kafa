from __future__ import annotations

import json
import math
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "codex-project-harness"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.operator_output import (  # noqa: E402
    OperatorBlocker,
    OperatorEnvelope,
    OperatorOutputError,
    build_operator_envelope,
    render_concise,
    render_json,
    render_verbose,
)


class OperatorOutputUnitTest(unittest.TestCase):
    def test_envelope_is_deeply_immutable_and_detached(self) -> None:
        blockers = [
            {"code": "second", "message": "second message"},
            {"code": "first", "message": "first message"},
        ]
        actions = ["kafa second", "kafa first"]
        details = {
            "phase": "intake",
            "nested": {"issues": ["one", "two"]},
        }

        envelope = build_operator_envelope(
            state="blocked",
            blockers=blockers,
            actions=actions,
            details=details,
        )
        blockers.reverse()
        actions.clear()
        details["phase"] = "changed"
        details["nested"]["issues"].append("changed")

        self.assertEqual(
            [blocker.code for blocker in envelope.blockers],
            ["second", "first"],
        )
        self.assertEqual(envelope.actions, ("kafa second", "kafa first"))
        self.assertEqual(envelope.details["phase"], "intake")
        self.assertEqual(envelope.details["nested"]["issues"], ("one", "two"))
        with self.assertRaises(FrozenInstanceError):
            envelope.state = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            envelope.details["phase"] = "changed"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            envelope.details["nested"]["issues"].append("changed")

    def test_public_shape_and_input_order_are_exact(self) -> None:
        envelope = OperatorEnvelope(
            state="needs-work",
            blockers=(
                OperatorBlocker("z-last-by-name", "canonical first"),
                OperatorBlocker("a-first-by-name", "canonical second"),
            ),
            actions=("command-two", "command-one"),
            details={"z": 1, "a": [True, None]},
        )

        payload = envelope.as_dict()

        self.assertEqual(
            list(payload),
            ["state", "blockers", "actions", "details"],
        )
        self.assertEqual(
            payload["blockers"],
            [
                {"code": "z-last-by-name", "message": "canonical first"},
                {"code": "a-first-by-name", "message": "canonical second"},
            ],
        )
        self.assertEqual(payload["actions"], ["command-two", "command-one"])
        self.assertEqual(list(payload["details"]), ["z", "a"])

        payload["blockers"].clear()
        payload["details"]["a"].append("detached")
        self.assertEqual(len(envelope.blockers), 2)
        self.assertEqual(envelope.details["a"], (True, None))

    def test_concise_output_is_exactly_three_lines_and_uses_only_first_values(self) -> None:
        envelope = build_operator_envelope(
            state="blocked",
            blockers=(
                {"code": "canonical-first", "message": "first reason"},
                {"code": "later", "message": "must stay hidden"},
            ),
            actions=("kafa first", "kafa later"),
            details={"phase": "intake"},
        )

        rendered = render_concise(envelope)

        self.assertEqual(
            rendered.splitlines(),
            [
                "state: blocked",
                "blocker: [canonical-first] first reason",
                "next: kafa first",
            ],
        )
        self.assertTrue(rendered.endswith("\n"))
        self.assertNotIn("must stay hidden", rendered)
        self.assertNotIn("kafa later", rendered)
        self.assertNotIn("intake", rendered)

    def test_concise_healthy_output_uses_explicit_none_values(self) -> None:
        envelope = build_operator_envelope(state="healthy", details={})

        self.assertEqual(
            render_concise(envelope),
            "state: healthy\nblocker: none\nnext: none\n",
        )

    def test_json_is_one_complete_object_with_no_human_diagnostics(self) -> None:
        envelope = build_operator_envelope(
            state="recovery-required",
            blockers=(
                {
                    "code": "rollback-incomplete",
                    "message": "Do not remove the migration sentinel",
                },
            ),
            actions=(),
            details={
                "manifest_path": ".ai-team/backups/recovery/manifest.json",
                "guidance": "Do not remove recovery evidence",
            },
        )

        rendered = render_json(envelope)
        payload = json.loads(rendered)

        self.assertEqual(rendered.count("\n"), 1)
        self.assertEqual(set(payload), {"state", "blockers", "actions", "details"})
        self.assertEqual(
            set(payload["blockers"][0]),
            {"code", "message"},
        )
        self.assertEqual(payload["actions"], [])
        self.assertIn("Do not remove", payload["details"]["guidance"])
        self.assertNotIn("ERROR:", rendered)

    def test_verbose_passthrough_never_swallows_recovery_guidance(self) -> None:
        lines = [
            "ERROR: rollback-incomplete",
            "manifest: .ai-team/backups/recovery/manifest.json",
            "Do not remove the sentinel; restore from the manifest.",
            "No initialization command is safe during recovery.",
        ]

        self.assertEqual(render_verbose(lines), "\n".join(lines) + "\n")
        complete = "\n".join(lines)
        self.assertEqual(render_verbose(complete), complete + "\n")
        self.assertEqual(render_verbose(complete + "\n"), complete + "\n")

    def test_malformed_state_blockers_and_actions_are_rejected(self) -> None:
        invalid_envelopes = (
            {"state": "", "blockers": (), "actions": (), "details": {}},
            {"state": "bad\nstate", "blockers": (), "actions": (), "details": {}},
            {"state": "ok", "blockers": "bad", "actions": (), "details": {}},
            {
                "state": "ok",
                "blockers": ({"code": "missing-message"},),
                "actions": (),
                "details": {},
            },
            {
                "state": "ok",
                "blockers": ({"code": "bad code", "message": "reason"},),
                "actions": (),
                "details": {},
            },
            {
                "state": "ok",
                "blockers": (
                    {"code": "valid", "message": "reason\nsecond line"},
                ),
                "actions": (),
                "details": {},
            },
            {"state": "ok", "blockers": (), "actions": "bad", "details": {}},
            {"state": "ok", "blockers": (), "actions": ("",), "details": {}},
            {
                "state": "ok",
                "blockers": (),
                "actions": ("command\nsecond",),
                "details": {},
            },
        )
        for fields in invalid_envelopes:
            with self.subTest(fields=fields):
                with self.assertRaises(OperatorOutputError):
                    OperatorEnvelope(**fields)

    def test_malformed_details_are_rejected(self) -> None:
        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic
        invalid_details = (
            [],
            {1: "non-string key"},
            {"": "empty key"},
            {"bad\nkey": "value"},
            {"path": Path("not-json")},
            {"nan": math.nan},
            {"infinity": math.inf},
            cyclic,
        )
        for details in invalid_details:
            with self.subTest(details=repr(details)):
                with self.assertRaises(OperatorOutputError):
                    OperatorEnvelope(
                        state="ok",
                        blockers=(),
                        actions=(),
                        details=details,
                    )

    def test_malformed_verbose_lines_are_rejected(self) -> None:
        for value in (["ok", 1], ["embedded\nline"], ["nul\x00line"], 42):
            with self.subTest(value=value):
                with self.assertRaises(OperatorOutputError):
                    render_verbose(value)


if __name__ == "__main__":
    unittest.main()
