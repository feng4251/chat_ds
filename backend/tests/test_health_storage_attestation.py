import json
import unittest
from unittest.mock import patch

import main


_LOCAL = {
    "version": 1,
    "available": True,
    "identity_sha256": "a" * 64,
}


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.httpx.HTTPStatusError(
                "failed",
                request=main.httpx.Request("GET", "http://harness/health"),
                response=main.httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url):
        return self._response


class HealthStorageAttestationTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_requires_matching_harness_storage(self):
        remote = _Response({"status": "ok", "storage": dict(_LOCAL)})
        with patch.object(main, "storage_root_attestation", return_value=_LOCAL), patch.object(
            main.httpx,
            "AsyncClient",
            return_value=_Client(remote),
        ):
            response = await main.health()

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["storage"], _LOCAL)

    async def test_health_fails_closed_on_split_storage(self):
        remote = _Response(
            {
                "status": "ok",
                "storage": {**_LOCAL, "identity_sha256": "b" * 64},
            }
        )
        with patch.object(main, "storage_root_attestation", return_value=_LOCAL), patch.object(
            main.httpx,
            "AsyncClient",
            return_value=_Client(remote),
        ):
            response = await main.health()

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["code"], "shared_storage_identity_mismatch")
        self.assertNotIn("harness", json.dumps(payload["storage"]))

    async def test_health_fails_closed_on_malformed_harness_health(self):
        remote = _Response([])
        with patch.object(main, "storage_root_attestation", return_value=_LOCAL), patch.object(
            main.httpx,
            "AsyncClient",
            return_value=_Client(remote),
        ):
            response = await main.health()

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["code"], "shared_storage_identity_mismatch")


if __name__ == "__main__":
    unittest.main()
