from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLING_PATH = REPO_ROOT / "release-tooling.json"


def run(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)
    return completed.stdout


def create_source_repo(root: Path) -> Path:
    repo = root / "source"
    repo.mkdir()
    (repo / "release-tooling.json").write_bytes(TOOLING_PATH.read_bytes())
    (repo / "release.json").write_bytes((REPO_ROOT / "release.json").read_bytes())
    (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    run(["git", "init"], cwd=repo)
    run(["git", "config", "user.email", "supply-chain-test@example.invalid"], cwd=repo)
    run(["git", "config", "user.name", "Supply Chain Test"], cwd=repo)
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-m", "fixture"], cwd=repo)
    return repo


def create_artifacts(root: Path) -> Path:
    dist = root / "dist"
    dist.mkdir()
    (dist / "kafa-2.0.0b1-py3-none-any.whl").write_bytes(b"wheel-content\n")
    (dist / "kafa-2.0.0b1.tar.gz").write_bytes(b"sdist-content\n")
    return dist


def create_fake_syft(
    root: Path,
    *,
    version: str = "1.48.0",
    commit: str = "3e2bc6ed095f7ec1a415fb38cfe1c319e95dfed6",
    mutate_artifact: bool = False,
    mutate_source: Path | None = None,
) -> list[str]:
    script = root / "fake_syft.py"
    script.write_text(
        "import hashlib, json, sys\n"
        "from pathlib import Path\n"
        f"VERSION = {version!r}\n"
        f"COMMIT = {commit!r}\n"
        f"MUTATE_ARTIFACT = {mutate_artifact!r}\n"
        f"MUTATE_SOURCE = {str(mutate_source) if mutate_source else ''!r}\n"
        "args = sys.argv[1:]\n"
        "if args == ['version', '-o', 'json']:\n"
        "    print(json.dumps({'application':'syft','version':VERSION,'gitCommit':COMMIT,'platform':'test/test'}))\n"
        "    raise SystemExit(0)\n"
        "if len(args) == 4 and args[0] == 'scan' and args[2] == '-o':\n"
        "    artifact = Path(args[1])\n"
        "    output_spec = args[3]\n"
        "    format_name, output_name = output_spec.split('=', 1)\n"
        "    if format_name != 'cyclonedx-json@1.6':\n"
        "        raise SystemExit(4)\n"
        "    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()\n"
        "    payload = {\n"
        "      '$schema':'http://cyclonedx.org/schema/bom-1.6.schema.json',\n"
        "      'bomFormat':'CycloneDX','specVersion':'1.6','version':1,\n"
        "      'metadata':{\n"
        "        'tools':{'components':[{'type':'application','author':'anchore','name':'syft','version':VERSION}]},\n"
        "        'component':{'type':'file','name':artifact.name,'version':'sha256:' + digest}\n"
        "      },\n"
        "      'components':[]\n"
        "    }\n"
        "    Path(output_name).write_text(json.dumps(payload), encoding='utf-8')\n"
        "    if MUTATE_ARTIFACT:\n"
        "        artifact.write_bytes(artifact.read_bytes() + b'race')\n"
        "    if MUTATE_SOURCE:\n"
        "        source = Path(MUTATE_SOURCE)\n"
        "        source.write_text(source.read_text(encoding='utf-8') + 'race\\n', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(3)\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def generate_fixture(root: Path) -> tuple[Path, Path]:
    from kafa.supply_chain import generate_release_evidence

    repo = create_source_repo(root)
    dist = create_artifacts(root)
    generate_release_evidence(
        repo,
        dist,
        syft_command=create_fake_syft(root),
        builder_command=[
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--sdist",
            "--outdir",
            str(dist),
        ],
        build_frontend_version="1.5.0",
        build_backend_version="83.0.0",
        started_at="2026-07-21T00:00:00Z",
        finished_at="2026-07-21T00:00:01Z",
    )
    return repo, dist


def rewrite_json(path: Path, mutate: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)  # type: ignore[operator]
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class SupplyChainToolingContractTest(unittest.TestCase):
    def test_official_build_only_tooling_is_fully_pinned(self) -> None:
        tooling = json.loads(TOOLING_PATH.read_text(encoding="utf-8"))

        self.assertEqual(tooling["schema_version"], 1)
        self.assertEqual(tooling["checked_on"], "2026-07-21")
        self.assertEqual(
            tooling["local_statement_assurance"],
            "unsigned-local-integrity-statement",
        )

        sbom = tooling["sbom"]
        self.assertEqual(sbom["tool"], "anchore/syft")
        self.assertEqual(sbom["version"], "1.48.0")
        self.assertRegex(sbom["source_commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(sbom["format"], "cyclonedx-json@1.6")
        self.assertEqual(sbom["predicate_type"], "https://cyclonedx.org/bom")
        self.assertEqual(
            set(sbom["downloads"]),
            {
                "darwin-amd64",
                "darwin-arm64",
                "linux-amd64",
                "linux-arm64",
                "windows-amd64",
                "windows-arm64",
            },
        )
        for platform_name, download in sbom["downloads"].items():
            self.assertIn(platform_name.split("-")[0], download["filename"])
            self.assertRegex(download["sha256"], r"^[0-9a-f]{64}$")

        action = tooling["github_attestation"]
        self.assertEqual(action["action"], "actions/attest")
        self.assertEqual(action["version"], "v4.2.0")
        self.assertRegex(action["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(
            action["uses"],
            f"actions/attest@{action['commit']}",
        )

        statements = tooling["statements"]
        self.assertEqual(statements["in_toto_type"], "https://in-toto.io/Statement/v1")
        self.assertEqual(statements["slsa_predicate_type"], "https://slsa.dev/provenance/v1")
        self.assertEqual(statements["slsa_spec"], "1.2")
        self.assertEqual(tooling["python_build"]["version"], "1.5.0")
        self.assertEqual(tooling["python_build"]["backend"], "setuptools")
        self.assertEqual(tooling["python_build"]["backend_version"], "83.0.0")

    def test_pin_manifest_contains_only_primary_https_sources(self) -> None:
        tooling = json.loads(TOOLING_PATH.read_text(encoding="utf-8"))
        sources = tooling["official_sources"]

        self.assertGreaterEqual(len(sources), 7)
        self.assertEqual(len(sources), len(set(sources)))
        self.assertTrue(all(re.fullmatch(r"https://[^\s]+", source) for source in sources))
        self.assertTrue(any("docs.github.com" in source for source in sources))
        self.assertTrue(any("oss.anchore.com" in source for source in sources))
        self.assertTrue(any("slsa.dev" in source for source in sources))


class SupplyChainGenerationContractTest(unittest.TestCase):
    def test_cli_generate_queries_the_installed_build_frontend_version(self) -> None:
        from kafa import supply_chain

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = create_source_repo(root)
            dist = create_artifacts(root)
            versions = {"build": "0.0.0", "setuptools": "83.0.0"}
            with (
                patch.object(
                    supply_chain,
                    "_distribution_version",
                    side_effect=lambda name: versions[name],
                ) as distribution_version,
                patch.object(
                    supply_chain,
                    "generate_release_evidence",
                    return_value={"ok": True},
                ) as generate,
                patch("builtins.print"),
            ):
                result = supply_chain.main(
                    [
                        "generate",
                        "--repo",
                        str(repo),
                        "--dist",
                        str(dist),
                        "--syft",
                        "/tmp/pinned-syft",
                        "--json",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            [item.args[0] for item in distribution_version.call_args_list],
            ["build", "setuptools"],
        )
        self.assertEqual(
            generate.call_args.kwargs["build_frontend_version"],
            "0.0.0",
        )

    def test_generate_and_verify_exact_wheel_sdist_evidence(self) -> None:
        from kafa.supply_chain import generate_release_evidence, verify_release_evidence

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = create_source_repo(root)
            dist = create_artifacts(root)
            syft = create_fake_syft(root)

            generated = generate_release_evidence(
                repo,
                dist,
                syft_command=syft,
                builder_command=[
                    sys.executable,
                    "-m",
                    "build",
                    "--no-isolation",
                    "--wheel",
                    "--sdist",
                    "--outdir",
                    str(dist),
                ],
                build_frontend_version="1.5.0",
                build_backend_version="83.0.0",
                started_at="2026-07-21T00:00:00Z",
                finished_at="2026-07-21T00:00:01Z",
            )
            verified = verify_release_evidence(repo, dist)

            self.assertTrue(generated["ok"], generated)
            self.assertTrue(verified["ok"], verified)
            self.assertEqual(generated["artifact_count"], 2)
            self.assertEqual(generated["sbom_count"], 2)
            self.assertEqual(
                {entry["name"] for entry in generated["artifacts"]},
                {
                    "kafa-2.0.0b1-py3-none-any.whl",
                    "kafa-2.0.0b1.tar.gz",
                },
            )

            checksums = (dist / "SHA256SUMS").read_bytes()
            self.assertNotIn(b"\r", checksums)
            self.assertEqual(checksums.count(b"\n"), 2)

            provenance = json.loads(
                (dist / "kafa-build-provenance.intoto.json").read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["_type"], "https://in-toto.io/Statement/v1")
            self.assertEqual(provenance["predicateType"], "https://slsa.dev/provenance/v1")
            self.assertEqual(len(provenance["subject"]), 2)
            self.assertEqual(
                provenance["predicate"]["buildDefinition"]["internalParameters"]["assurance"],
                "unsigned-local-integrity-statement",
            )

            for artifact in generated["artifacts"]:
                sbom = json.loads(
                    (dist / artifact["sbom"]).read_text(encoding="utf-8")
                )
                component = sbom["metadata"]["component"]
                self.assertEqual(component["name"], artifact["name"])
                self.assertEqual(
                    component["hashes"],
                    [{"alg": "SHA-256", "content": artifact["sha256"]}],
                )

    def test_verifier_accepts_an_unchanged_candidate_moved_to_a_new_directory(self) -> None:
        from kafa.supply_chain import verify_release_evidence

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, original = generate_fixture(root)
            moved = root / "downloaded-candidate"
            original.rename(moved)

            verified = verify_release_evidence(repo, moved)

        self.assertTrue(verified["ok"], verified)

    def test_generation_still_rejects_a_builder_targeting_another_directory(self) -> None:
        from kafa.supply_chain import SupplyChainError, generate_release_evidence

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = create_source_repo(root)
            dist = create_artifacts(root)
            other = root / "other-dist"
            other.mkdir()

            with self.assertRaisesRegex(
                SupplyChainError,
                "builder command does not match",
            ):
                generate_release_evidence(
                    repo,
                    dist,
                    syft_command=create_fake_syft(root),
                    builder_command=[
                        sys.executable,
                        "-m",
                        "build",
                        "--no-isolation",
                        "--wheel",
                        "--sdist",
                        "--outdir",
                        str(other),
                    ],
                    build_frontend_version="1.5.0",
                    build_backend_version="83.0.0",
                )

    def test_generation_rejects_wrong_syft_version_or_commit(self) -> None:
        from kafa.supply_chain import SupplyChainError, generate_release_evidence

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = create_source_repo(root)
            dist = create_artifacts(root)

            for syft in (
                create_fake_syft(root, version="1.47.1"),
                create_fake_syft(root, commit="0" * 40),
            ):
                with self.subTest(syft=syft), self.assertRaisesRegex(
                    SupplyChainError,
                    "Syft .* does not match pinned",
                ):
                    generate_release_evidence(
                        repo,
                        dist,
                        syft_command=syft,
                        builder_command=[
                            "python",
                            "-m",
                            "build",
                            "--no-isolation",
                            "--wheel",
                            "--sdist",
                            "--outdir",
                            str(dist),
                        ],
                        build_frontend_version="1.5.0",
                        build_backend_version="83.0.0",
                    )

    def test_generation_requires_exact_regular_artifact_inventory(self) -> None:
        from kafa.supply_chain import SupplyChainError, generate_release_evidence

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = create_source_repo(root)
            syft = create_fake_syft(root)
            dist = create_artifacts(root)
            (dist / "kafa-2.0.0b1-extra.whl").write_bytes(b"extra")

            with self.assertRaisesRegex(SupplyChainError, "exactly one wheel"):
                generate_release_evidence(
                    repo,
                    dist,
                    syft_command=syft,
                    builder_command=[
                        "python",
                        "-m",
                        "build",
                        "--no-isolation",
                        "--wheel",
                        "--sdist",
                        "--outdir",
                        str(dist),
                    ],
                    build_frontend_version="1.5.0",
                    build_backend_version="83.0.0",
                )


class SupplyChainTamperContractTest(unittest.TestCase):
    def test_verifier_rejects_each_bound_input_tamper(self) -> None:
        from kafa.supply_chain import SupplyChainError, verify_release_evidence

        def artifact_bytes(repo: Path, dist: Path) -> None:
            wheel = dist / "kafa-2.0.0b1-py3-none-any.whl"
            wheel.write_bytes(wheel.read_bytes() + b"tamper")

        def checksum_digest(repo: Path, dist: Path) -> None:
            path = dist / "SHA256SUMS"
            payload = path.read_bytes()
            path.write_bytes((b"0" if payload[:1] != b"0" else b"1") + payload[1:])

        def checksum_line_endings(repo: Path, dist: Path) -> None:
            path = dist / "SHA256SUMS"
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

        def sbom_subject(repo: Path, dist: Path) -> None:
            path = dist / "kafa-2.0.0b1.tar.gz.cdx.json"
            rewrite_json(
                path,
                lambda payload: payload["metadata"]["component"].__setitem__(
                    "name", "unrelated.tar.gz"
                ),
            )

        def provenance_subject(repo: Path, dist: Path) -> None:
            path = dist / "kafa-build-provenance.intoto.json"
            rewrite_json(
                path,
                lambda payload: payload["subject"][0]["digest"].__setitem__(
                    "sha256", "0" * 64
                ),
            )

        def provenance_builder(repo: Path, dist: Path) -> None:
            path = dist / "kafa-build-provenance.intoto.json"
            rewrite_json(
                path,
                lambda payload: payload["predicate"]["buildDefinition"][
                    "externalParameters"
                ].__setitem__("builder_command", ["python", "setup.py", "bdist_wheel"]),
            )

        def source_bytes(repo: Path, dist: Path) -> None:
            path = repo / "candidate.txt"
            path.write_text("different candidate\n", encoding="utf-8")

        def tooling_bytes(repo: Path, dist: Path) -> None:
            path = repo / "release-tooling.json"
            payload = path.read_text(encoding="utf-8")
            path.write_text(payload.replace('"checked_on": "2026-07-21"', '"checked_on": "2026-07-22"'), encoding="utf-8")

        for name, tamper in {
            "artifact-bytes": artifact_bytes,
            "checksum-digest": checksum_digest,
            "checksum-crlf": checksum_line_endings,
            "sbom-subject": sbom_subject,
            "provenance-subject": provenance_subject,
            "provenance-builder": provenance_builder,
            "source-bytes": source_bytes,
            "tooling-bytes": tooling_bytes,
        }.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                repo, dist = generate_fixture(Path(temp))
                tamper(repo, dist)
                with self.assertRaises(SupplyChainError):
                    verify_release_evidence(repo, dist)

    def test_verifier_rejects_duplicate_keys_and_duplicate_or_extra_subjects(self) -> None:
        from kafa.supply_chain import SupplyChainError, verify_release_evidence

        def duplicate_key(path: Path) -> None:
            payload = path.read_text(encoding="utf-8")
            path.write_text(
                payload.replace('  "subject": [', '  "subject": [],\n  "subject": [', 1),
                encoding="utf-8",
            )

        def duplicate_subject(path: Path) -> None:
            rewrite_json(
                path,
                lambda payload: payload["subject"].append(dict(payload["subject"][0])),
            )

        def extra_subject(path: Path) -> None:
            rewrite_json(
                path,
                lambda payload: payload["subject"].append(
                    {"name": "extra.bin", "digest": {"sha256": "1" * 64}}
                ),
            )

        for name, tamper in {
            "duplicate-key": duplicate_key,
            "duplicate-subject": duplicate_subject,
            "extra-subject": extra_subject,
        }.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                repo, dist = generate_fixture(Path(temp))
                provenance = dist / "kafa-build-provenance.intoto.json"
                tamper(provenance)
                with self.assertRaises(SupplyChainError):
                    verify_release_evidence(repo, dist)

    def test_generation_detects_artifact_and_source_races(self) -> None:
        from kafa.supply_chain import SupplyChainError, generate_release_evidence

        for race in ("artifact", "source"):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = create_source_repo(root)
                dist = create_artifacts(root)
                syft = create_fake_syft(
                    root,
                    mutate_artifact=race == "artifact",
                    mutate_source=(repo / "candidate.txt") if race == "source" else None,
                )
                with self.assertRaisesRegex(SupplyChainError, "changed"):
                    generate_release_evidence(
                        repo,
                        dist,
                        syft_command=syft,
                        builder_command=[
                            sys.executable,
                            "-m",
                            "build",
                            "--no-isolation",
                            "--wheel",
                            "--sdist",
                            "--outdir",
                            str(dist),
                        ],
                        build_frontend_version="1.5.0",
                        build_backend_version="83.0.0",
                    )

    def test_artifact_symlink_is_rejected_without_platform_symlink_creation(self) -> None:
        from kafa.supply_chain import SupplyChainError, discover_artifacts

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = create_source_repo(root)
            dist = create_artifacts(root)
            original = Path.is_symlink

            def reports_wheel_symlink(path: Path) -> bool:
                if path.name.endswith(".whl"):
                    return True
                return original(path)

            with patch.object(Path, "is_symlink", autospec=True, side_effect=reports_wheel_symlink):
                with self.assertRaisesRegex(SupplyChainError, "not a regular file"):
                    discover_artifacts(repo, dist)

    def test_generated_build_metadata_does_not_change_source_identity(self) -> None:
        from kafa.supply_chain import source_identity

        with tempfile.TemporaryDirectory() as temp:
            repo = create_source_repo(Path(temp))
            before = source_identity(repo)
            (repo / "build/lib").mkdir(parents=True)
            (repo / "build/lib/output.py").write_text("generated\n", encoding="utf-8")
            (repo / "kafa.egg-info").mkdir()
            (repo / "kafa.egg-info/SOURCES.txt").write_text("generated\n", encoding="utf-8")
            after = source_identity(repo)

        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
