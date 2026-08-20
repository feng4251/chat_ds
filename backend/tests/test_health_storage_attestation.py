import json
import unittest
from unittest.mock import patch

import main


_LOCAL = {
    "version": 1,
    "available": True,
    "identity_sha256": "a" * 64,
}


class HealthStorageAttestationTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_attests_backend_storage_without_legacy_runtime(self):
        with patch.object(
            main,
            "storage_root_attestation",
            return_value=_LOCAL,
        ):
            response = await main.health()

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["storage"], _LOCAL)

    async def test_health_fails_closed_when_backend_storage_is_unavailable(self):
        unavailable = {
            "version": 1,
            "available": False,
            "code": "storage_missing",
        }
        with patch.object(
            main,
            "storage_root_attestation",
            return_value=unavailable,
        ):
            response = await main.health()

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["code"], "storage_root_unavailable")
        self.assertEqual(payload["storage"], unavailable)


if __name__ == "__main__":
    unittest.main()
