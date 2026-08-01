import tempfile
import unittest
from pathlib import Path

from storage_attestation import (
    storage_attestations_match,
    storage_root_attestation,
)


class StorageAttestationTests(unittest.TestCase):
    def test_attestation_is_stable_path_free_and_directory_specific(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            other = root / "other"
            other.mkdir()

            first = storage_root_attestation(root)
            second = storage_root_attestation(root)
            distinct = storage_root_attestation(other)

            self.assertEqual(first, second)
            self.assertEqual(first["version"], 1)
            self.assertTrue(first["available"])
            self.assertEqual(len(first["identity_sha256"]), 64)
            self.assertNotIn(str(root), str(first))
            self.assertFalse(storage_attestations_match(first, distinct))

    def test_missing_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)

            self.assertFalse(
                storage_root_attestation(root / "missing")["available"]
            )
            self.assertFalse(storage_root_attestation(link)["available"])

    def test_match_is_strict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            attestation = storage_root_attestation(temp_dir)

            self.assertTrue(
                storage_attestations_match(attestation, dict(attestation))
            )
            self.assertFalse(storage_attestations_match(attestation, None))
            self.assertFalse(storage_attestations_match(attestation, []))
            self.assertFalse(
                storage_attestations_match(
                    attestation,
                    {**attestation, "version": 2},
                )
            )
            self.assertFalse(
                storage_attestations_match(
                    attestation,
                    {**attestation, "available": False},
                )
            )


if __name__ == "__main__":
    unittest.main()
