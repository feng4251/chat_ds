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
                "context_windows": {
                    "model-a": 1_000_000,
                    "model-b": 200_000,
                },
                "native_web_tools": True,
            },
            "local_agentmodel": {
                "backend_base_url": "http://10.10.132.2:1025/v1",
                "api_key_env": "LOCAL_PROVIDER_KEY",
                "backend_protocol": "openai",
                "models": ["AgentModel"],
                "context_windows": {"AgentModel": 303_872},
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
            "CLAUDE_PROVIDER_RESPONSE_IDLE_TIMEOUT_SECONDS": "7260",
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
        self.assertTrue(
            settings.provider_profiles["shaiengine"].native_web_tools
        )
        self.assertFalse(
            settings.provider_profiles["local_agentmodel"].native_web_tools
        )
        self.assertEqual(
            settings.provider_profiles["shaiengine"].context_windows,
            {"model-a": 1_000_000, "model-b": 200_000},
        )
        self.assertEqual(
            settings.provider_profiles["local_agentmodel"].claude_base_url,
            "http://10.10.132.2:1025",
        )
        self.assertEqual(
            settings.provider_profiles["local_agentmodel"]
            .response_idle_timeout_seconds,
            7260,
        )
        self.assertEqual(settings.private_origin_allowlist, (
            "https://10.10.132.126:18443",
            "http://10.10.132.2:1025",
        ))
        self.assertEqual(settings.egress_limits, {
            "max_requests": 8192,
            "max_outbound_bytes": 1024 * 1024 * 1024,
            "max_response_wire_bytes": 2 * 1024 * 1024 * 1024,
        })

    def test_turn_budget_cannot_exceed_proxy_policy_ceiling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            environment.update({
                "CLAUDE_EGRESS_MAX_OUTBOUND_BYTES": str(64 * 1024 * 1024),
                "CLAUDE_EGRESS_POLICY_MAX_OUTBOUND_BYTES": str(16 * 1024 * 1024),
            })
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(
                    RunnerConfigurationError,
                    "exceed proxy policy ceilings",
                ):
                    load_settings()

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

    def test_native_web_tool_capability_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            profiles = json.loads(environment["CLAUDE_PROVIDER_PROFILES_JSON"])
            profiles["shaiengine"]["native_web_tools"] = "true"
            environment["CLAUDE_PROVIDER_PROFILES_JSON"] = json.dumps(profiles)
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(
                    RunnerConfigurationError,
                    "native web-tool capability",
                ):
                    load_settings()

    def test_every_profile_model_requires_an_exact_context_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            profiles = json.loads(environment["CLAUDE_PROVIDER_PROFILES_JSON"])
            profiles["shaiengine"]["context_windows"] = {
                "model-a": 1_000_000,
                "renamed-model": 200_000,
            }
            environment["CLAUDE_PROVIDER_PROFILES_JSON"] = json.dumps(profiles)
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(
                    RunnerConfigurationError,
                    "context-window map",
                ):
                    load_settings()

    def test_context_windows_reject_boolean_and_unsafe_bounds(self):
        for value in (True, 199_999, 4_000_001):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = self._environment(root)
                profiles = json.loads(environment["CLAUDE_PROVIDER_PROFILES_JSON"])
                profiles["local_agentmodel"]["context_windows"]["AgentModel"] = value
                environment["CLAUDE_PROVIDER_PROFILES_JSON"] = json.dumps(profiles)
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(
                        RunnerConfigurationError,
                        "context-window map",
                    ):
                        load_settings()

    def test_provider_can_narrow_but_not_escape_idle_timeout_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = self._environment(root)
            profiles = json.loads(environment["CLAUDE_PROVIDER_PROFILES_JSON"])
            profiles["local_agentmodel"][
                "response_idle_timeout_seconds"
            ] = 600
            environment["CLAUDE_PROVIDER_PROFILES_JSON"] = json.dumps(profiles)
            with patch.dict(os.environ, environment, clear=True):
                settings = load_settings()
            self.assertEqual(
                settings.provider_profiles["local_agentmodel"]
                .response_idle_timeout_seconds,
                600,
            )

            profiles["local_agentmodel"][
                "response_idle_timeout_seconds"
            ] = 14_401
            environment["CLAUDE_PROVIDER_PROFILES_JSON"] = json.dumps(profiles)
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(
                    RunnerConfigurationError,
                    "response idle timeout",
                ):
                    load_settings()


if __name__ == "__main__":
    unittest.main()
