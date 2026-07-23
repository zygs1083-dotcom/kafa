from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from kafa.artifact_subject import (
    ArtifactSubject,
    ArtifactSubjectError,
    assert_exact_subjects,
    in_toto_subjects,
    manifest_records,
    parse_manifest_records,
    sha256sum_bytes,
    subjects_by_kind,
)


class ArtifactSubjectTest(unittest.TestCase):
    def test_regular_file_has_one_canonical_subject_and_legacy_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "kafa.whl"
            artifact.write_bytes(b"candidate\n")
            subject = ArtifactSubject.from_file(artifact, kind="wheel")

        expected = hashlib.sha256(b"candidate\n").hexdigest()
        self.assertEqual(subject.name, "kafa.whl")
        self.assertEqual(subject.kind, "wheel")
        self.assertEqual(subject.sha256, expected)
        self.assertEqual(
            subject.manifest_record(),
            {"name": "kafa.whl", "kind": "wheel", "sha256": expected},
        )
        self.assertEqual(
            subject.in_toto_record(),
            {"name": "kafa.whl", "digest": {"sha256": expected}},
        )
        self.assertEqual(subject.checksum_line(), f"{expected}  kafa.whl\n".encode())

    def test_subject_rejects_unsafe_or_noncanonical_identity(self) -> None:
        valid = {"name": "kafa.whl", "kind": "wheel", "sha256": "1" * 64}
        cases = [
            {**valid, "name": "../kafa.whl"},
            {**valid, "name": "nested/kafa.whl"},
            {**valid, "name": "kafa\n.whl"},
            {**valid, "kind": "Wheel"},
            {**valid, "kind": "wheel/zip"},
            {**valid, "sha256": "A" * 64},
            {**valid, "sha256": "0" * 64},
            {**valid, "sha256": "1" * 63},
        ]

        for record in cases:
            with self.subTest(record=record), self.assertRaises(ArtifactSubjectError):
                ArtifactSubject(**record)

    def test_subject_collection_is_sorted_unique_and_exact(self) -> None:
        wheel = ArtifactSubject("kafa.whl", "wheel", "1" * 64)
        sdist = ArtifactSubject("kafa.tar.gz", "sdist", "2" * 64)
        subjects = parse_manifest_records(
            [sdist.manifest_record(), wheel.manifest_record()]
        )

        self.assertEqual(subjects, (sdist, wheel))
        self.assertEqual(manifest_records(subjects), [sdist.manifest_record(), wheel.manifest_record()])
        self.assertEqual(sha256sum_bytes(subjects), sdist.checksum_line() + wheel.checksum_line())
        self.assertEqual(in_toto_subjects(subjects), [sdist.in_toto_record(), wheel.in_toto_record()])
        self.assertEqual(subjects_by_kind(subjects), {"wheel": wheel, "sdist": sdist})
        assert_exact_subjects(subjects, [sdist, wheel])

        with self.assertRaisesRegex(ArtifactSubjectError, "duplicate"):
            parse_manifest_records([wheel.manifest_record(), wheel.manifest_record()])
        with self.assertRaisesRegex(ArtifactSubjectError, "do not match"):
            assert_exact_subjects(subjects, [wheel])

    def test_file_subject_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.whl"
            link = root / "link.whl"
            target.write_bytes(b"candidate")
            link.symlink_to(target)

            with self.assertRaisesRegex(ArtifactSubjectError, "regular file"):
                ArtifactSubject.from_file(link, kind="wheel")


if __name__ == "__main__":
    unittest.main()
