import json
from pathlib import Path
import tomllib
import unittest

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _network_names(value: object) -> set[str]:
    if isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(value, dict):
        return {str(item) for item in value}
    return set()


class DeploymentTopologyTests(unittest.TestCase):
    def test_search_limiter_trusts_only_application_networks_and_exact_hosts(self):
        limiter = tomllib.loads(
            (REPOSITORY_ROOT / "searxng" / "limiter.toml").read_text(
                encoding="utf-8"
            )
        )
        pass_ips = set(
            limiter["botdetection"]["ip_lists"]["pass_ip"]
        )
        self.assertIn("172.29.250.0/24", pass_ips)
        self.assertFalse({
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        } & pass_ips)
        self.assertFalse(
            limiter["botdetection"]["ip_lists"]["pass_searxng_org"]
        )

    def test_native_engines_share_exact_provider_capacity_authority(self):
        """A wire-model rename must not split the two native profile ceilings."""

        compose = yaml.safe_load(
            (REPOSITORY_ROOT / "docker-compose.yml").read_text(
                encoding="utf-8"
            )
        )
        services = compose["services"]
        claude_profiles = json.loads(
            services["claude-runner-supervisor"]["environment"]
            ["CLAUDE_PROVIDER_PROFILES_JSON"]
        )
        deepseek_profiles = json.loads(
            services["deepseek-runner-supervisor"]["environment"]
            ["DEEPSEEK_HARNESS_PROVIDER_PROFILES_JSON"]
        )

        self.assertEqual(set(claude_profiles), set(deepseek_profiles))
        for profile_name in sorted(claude_profiles):
            claude = claude_profiles[profile_name]
            deepseek = deepseek_profiles[profile_name]
            self.assertEqual(claude["models"], deepseek["models"])
            self.assertEqual(
                claude["context_windows"],
                deepseek["context_windows"],
                profile_name,
            )

    def test_active_compose_graph_contains_only_native_agent_runtimes(self):
        compose = yaml.safe_load(
            (REPOSITORY_ROOT / "docker-compose.yml").read_text(
                encoding="utf-8"
            )
        )
        services = compose["services"]
        retired_services = {
            "harness",
            "browser",
            "browser-socket-init",
            "executor",
            "executor-web",
            "executor-browser",
            "executor-code",
        }
        self.assertFalse(retired_services & set(services))
        self.assertEqual(
            "native-session-runtime",
            services["native-session-runtime-image"]["build"]["target"],
        )
        for runner in ("claude-runner-image", "deepseek-harness-runner-image"):
            dependencies = services[runner].get("depends_on", {})
            self.assertIn("native-session-runtime-image", dependencies)
            self.assertFalse(retired_services & set(dependencies))

        dockerfile = (
            REPOSITORY_ROOT / "executor" / "Dockerfile.browser-runtime"
        ).read_text(encoding="utf-8")
        neutral_stage = dockerfile.split(
            "FROM browser-runtime AS native-session-runtime",
            1,
        )[1].split("\nFROM ", 1)[0]
        self.assertNotIn("EXECUTOR_ALLOWED_REQUEST_KINDS", neutral_stage)
        self.assertNotIn("chatds-browser-executor-entrypoint", neutral_stage)

        for native_image in (
            "claude_runner/Dockerfile.runner",
            "claude_runner/Dockerfile.supervisor",
            "deepseek_runner/Dockerfile.runner",
            "deepseek_runner/Dockerfile.supervisor",
        ):
            contents = (REPOSITORY_ROOT / native_image).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("COPY harness/", contents)
            self.assertNotIn("/app/harness", contents)

        for supervisor in (
            "claude_runner/Dockerfile.supervisor",
            "deepseek_runner/Dockerfile.supervisor",
        ):
            contents = (REPOSITORY_ROOT / supervisor).read_text(
                encoding="utf-8"
            )
            self.assertIn("COPY native_security /app/native_security", contents)

        dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(
            encoding="utf-8"
        )
        self.assertIn("!native_security/", dockerignore)
        self.assertIn("!native_security/**", dockerignore)

    def test_typed_private_gateway_has_one_proxy_visible_address_plane(self):
        compose = yaml.safe_load(
            (REPOSITORY_ROOT / "docker-compose.yml").read_text(
                encoding="utf-8"
            )
        )
        services = compose["services"]
        broker_networks = _network_names(
            services["market-data-gateway"].get("networks")
        )
        proxy_networks = _network_names(
            services["skill-egress-proxy"].get("networks")
        )

        # A signed private hostname is DNS-pinned to its application network.
        # Giving the broker another interface lets Docker return an address
        # outside that pin and makes a valid typed capability fail closed.
        self.assertEqual({"search_net"}, broker_networks)
        self.assertLessEqual(broker_networks, proxy_networks)


if __name__ == "__main__":
    unittest.main()
