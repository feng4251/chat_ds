import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_runner.config import RunnerConfigurationError, load_settings


class RunnerProviderConfigurationTests(unittest.TestCase):
    def _environment(self, root: Path) -> dict[str, str]:
        profiles = {
            "shaiengine": {
                "backend_base_url": "https://api.example.test/v1",
                "api_key_env": "PUBLIC_PROVIDER_KEY",
                "backend_protocol": "openai",
                "models": ["model-a", "model-b"],
            },
            "local_agentmodel": {
                "backend_base_url": "http://10.10.132.2:1025/v1",
                "api_key_env": "LOCAL_PROVIDER_KEY",
                "backend_protocol": "openai",
                "models": ["AgentModel"],
            },
        }
        return {
            "INTERNAL_API_TOKEN": "i" * 32,
            "SKILL_EGRESS_POLICY_TOKEN": "p" * 32,
            "CLAUDE_WORKSPACE_HOST_ROOT": str(root),
            "CLAUDE_RUNNER_STATE_ROOT": str(root / "state"),
            "WORKSPACE_MUTATION_LOCK_ROOT": str(root / "locks"),
            "CLAUDE_RUNNER_IMAGE": "fixture-runner:1",
            "CLAUDE_EGRESS_PROXY_VOLUME_NAME": "fixture-egress",
            "CLAUDE_WORKSPACE_LOCK_VOLUME_NAME": "fixture-locks",
            "CLAUDE_PROVIDER_PROFILES_JSON": json.dumps(profiles),
            "PUBLIC_PROVIDER_KEY": "public-fixture-key",
            "LOCAL_PROVIDER_KEY": "EMPTY",
            "BROWSER_PRIVATE_ORIGIN_ALLOWLIST": (
                "https://10.10.132.126:18443"
            ),
            "CLAUDE_PROVIDER_PRIVATE_ORIGIN_ALLOWLIST": (
                "http://10.10.132.2:1025"
            ),
        }

    def test_multiple_profiles_and_private_authorities_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, self._environment(root), clear=True):
                settings = load_settings()
        self.assertEqual(
            set(settings.provider_profiles), {"shaiengine", "local_agentmodel"}
        )
        self.assertEqual(
            settings.provider_profiles["shaiengine"].claude_base_url,
            "https://api.example.test",
        )
        self.assertEqual(
            settings.provider_profiles["local_agentmodel"].claude_base_url,
            "http://10.10.132.2:1025",
        )
        self.assertEqual(settings.private_origin_allowlist, (
            "https://10.10.132.126:18443",
            "http://10.10.132.2:1025",
        ))

    def test_missing_profile_credential_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            environment.pop("LOCAL_PROVIDER_KEY")
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(
                    RunnerConfigurationError,
                    "credential env is unavailable",
                ):
                    load_settings()


if __name__ == "__main__":
    unittest.main()
