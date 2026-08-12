from pathlib import Path
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
