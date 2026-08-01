import tempfile
import unittest
from pathlib import Path

from storage_attestation import storage_root_attestation


class StorageAttestationTests(unittest.TestCase):
    def test_identity_is_stable_path_free_and_directory_specific(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            other = root / "other"
            other.mkdir()

            first = storage_root_attestation(root)
            second = storage_root_attestation(root)
            distinct = storage_root_attestation(other)

            self.assertEqual(first, second)
            self.assertTrue(first["available"])
            self.assertEqual(len(first["identity_sha256"]), 64)
            self.assertNotIn(str(root), str(first))
            self.assertNotEqual(
                first["identity_sha256"],
                distinct["identity_sha256"],
            )

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


if __name__ == "__main__":
    unittest.main()
