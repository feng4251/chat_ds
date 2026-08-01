import json
import unittest
from unittest.mock import patch

import main


class HealthStorageAttestationTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_returns_path_free_storage_identity(self):
        storage = {
            "version": 1,
            "available": True,
            "identity_sha256": "a" * 64,
        }
        with patch.object(main, "storage_root_attestation", return_value=storage):
            response = await main.health()

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["storage"], storage)

    async def test_health_fails_when_storage_is_unavailable(self):
        storage = {
            "version": 1,
            "available": False,
            "identity_sha256": "",
        }
        with patch.object(main, "storage_root_attestation", return_value=storage):
            response = await main.health()

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["code"], "storage_root_unavailable")


if __name__ == "__main__":
    unittest.main()
