from __future__ import annotations

import tarfile
import tomllib
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COPYRIGHT_LINE = "Copyright (c) 2026 zygs1083-dotcom"
MIT_GRANT = (
    "Permission is hereby granted, free of charge, to any person obtaining a copy"
)
MIT_DISCLAIMER = "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND"


def assert_complete_mit_text(test: unittest.TestCase, text: str, label: str) -> None:
    test.assertIn("MIT License", text, label)
    test.assertIn(COPYRIGHT_LINE, text, label)
    test.assertIn(MIT_GRANT, text, label)
    test.assertIn(MIT_DISCLAIMER, text, label)
    test.assertTrue(text.endswith("\n"), label)


def assert_mit_package_metadata(
    test: unittest.TestCase,
    text: str,
    label: str,
) -> None:
    header = text.split("\n\n", 1)[0]
    test.assertIn("Name: kafa", header, label)
    test.assertIn("Author: zygs1083-dotcom", header, label)
    test.assertIn("License-Expression: MIT", header, label)
    test.assertIn("License-File: LICENSE", header, label)


class LicenseContractTests(unittest.TestCase):
    def test_repository_and_metadata_declare_one_complete_mit_license(self) -> None:
        license_path = REPO_ROOT / "LICENSE"
        self.assertTrue(license_path.is_file(), "root LICENSE is missing")
        assert_complete_mit_text(
            self,
            license_path.read_text(encoding="utf-8"),
            "root LICENSE",
        )

        metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["license"], "MIT")
        self.assertEqual(
            metadata["project"]["authors"],
            [{"name": "zygs1083-dotcom"}],
        )
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("[MIT License](LICENSE)", readme)

    def test_built_wheel_contains_the_root_license(self) -> None:
        wheel_text = self._artifact_path("KAFA_TEST_WHEEL")
        if wheel_text is None:
            self.skipTest("KAFA_TEST_WHEEL not supplied; artifact coverage runs in rehearsal")
        with zipfile.ZipFile(wheel_text) as archive:
            candidates = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/licenses/LICENSE")
                or name.endswith(".dist-info/LICENSE")
            ]
            self.assertEqual(len(candidates), 1, archive.namelist())
            text = archive.read(candidates[0]).decode("utf-8")
            metadata_name = next(
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            )
            metadata = archive.read(metadata_name).decode("utf-8")
        assert_complete_mit_text(self, text, "wheel LICENSE")
        assert_mit_package_metadata(self, metadata, "wheel METADATA")

    def test_built_sdist_contains_the_root_license(self) -> None:
        sdist = self._artifact_path("KAFA_TEST_SDIST")
        if sdist is None:
            self.skipTest("KAFA_TEST_SDIST not supplied; artifact coverage runs in rehearsal")
        with tarfile.open(sdist, "r:gz") as archive:
            candidates = [
                member
                for member in archive.getmembers()
                if member.isfile() and Path(member.name).name == "LICENSE"
            ]
            self.assertEqual(len(candidates), 1, [member.name for member in candidates])
            handle = archive.extractfile(candidates[0])
            self.assertIsNotNone(handle)
            assert handle is not None
            text = handle.read().decode("utf-8")
            metadata_member = next(
                member
                for member in archive.getmembers()
                if member.isfile()
                and member.name.endswith("/PKG-INFO")
                and member.name.count("/") == 1
            )
            metadata_handle = archive.extractfile(metadata_member)
            self.assertIsNotNone(metadata_handle)
            assert metadata_handle is not None
            metadata = metadata_handle.read().decode("utf-8")
        assert_complete_mit_text(self, text, "sdist LICENSE")
        assert_mit_package_metadata(self, metadata, "sdist PKG-INFO")

    def _artifact_path(self, variable: str) -> Path | None:
        import os

        value = os.environ.get(variable, "").strip()
        if not value:
            return None
        path = Path(value).resolve()
        self.assertTrue(path.is_file(), f"{variable} artifact missing: {path}")
        return path


if __name__ == "__main__":
    unittest.main()
