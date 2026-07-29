from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ProcessLeaseAcceptancePolicyTests(unittest.TestCase):
    def test_network_acceptance_uses_exact_rules_and_private_subset(self) -> None:
        program = textwrap.dedent(
            """
            import argparse
            import asyncio
            import json
            from pathlib import Path

            import executor.browser_runtime.process_lease_acceptance as target


            class StopAfterOpen(Exception):
                pass


            async def main():
                public = "https://example.com:443"
                private = "http://private-origin.test:8000"
                rules = (
                    target._retrieval_rule(public),
                    target._retrieval_rule(private),
                )
                captured = {"open_calls": []}

                async def fake_open(**kwargs):
                    captured["open_calls"].append({
                        "egress_rules": kwargs.get("egress_rules"),
                        "private_origins": kwargs.get("private_origins"),
                        "has_egress_origins": "egress_origins" in kwargs,
                    })
                    raise StopAfterOpen

                original_open = target.client.open_isolated_process_lease
                target.client.open_isolated_process_lease = fake_open
                try:
                    try:
                        await target._run_cli(
                            object(),
                            skill_root=Path("/unused/skill"),
                            workspace=Path("/unused/workspace"),
                            entrypoint="network_identity_probe.py",
                            expected=b"unused",
                            socket_path="/unused/executor.sock",
                            egress_rules=rules,
                            private_origins=(private,),
                        )
                    except StopAfterOpen:
                        pass

                    try:
                        await target._run_exact_visual_skill(
                            object(),
                            skill_root=Path("/unused/visual-skill"),
                            workspace=Path("/unused/visual-workspace"),
                            socket_path="/unused/executor.sock",
                            public_url="https://example.com/proof",
                        )
                    except StopAfterOpen:
                        pass
                finally:
                    target.client.open_isolated_process_lease = original_open

                async def fake_reap(**_kwargs):
                    return {"reaped_leases": 0}

                async def fake_run_cli(*_args, **kwargs):
                    captured["compiled_network_case"] = {
                        "egress_rules": kwargs.get("egress_rules"),
                        "private_origins": kwargs.get("private_origins"),
                        "has_egress_origins": "egress_origins" in kwargs,
                    }
                    return {"status": "captured"}

                original_reap = target.client.reap_isolated_executor_leases
                original_scope = target.client.create_process_owner_scope
                original_run_cli = target._run_cli
                target.client.reap_isolated_executor_leases = fake_reap
                target.client.create_process_owner_scope = lambda **_kwargs: object()
                target._run_cli = fake_run_cli
                try:
                    await target._main(argparse.Namespace(
                        socket="/unused/executor.sock",
                        smoke_skill_root="/unused/smoke",
                        exact_skill_root=None,
                        exact_public_url=None,
                        private_origin=private,
                        abandon_running_lease=False,
                        cli_only="network_identity_probe.py",
                    ))
                finally:
                    target.client.reap_isolated_executor_leases = original_reap
                    target.client.create_process_owner_scope = original_scope
                    target._run_cli = original_run_cli

                print(json.dumps(captured, sort_keys=True))


            asyncio.run(main())
            """
        )
        environment = os.environ.copy()
        environment["CHATDS_ACCEPTANCE_HARNESS_ROOT"] = str(
            PROJECT_ROOT / "harness"
        )
        environment["PYTHONPATH"] = str(PROJECT_ROOT)
        completed = subprocess.run(
            [sys.executable, "-c", program],
            check=True,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        captured = json.loads(completed.stdout)

        run_cli, visual = captured["open_calls"]
        self.assertFalse(run_cli["has_egress_origins"])
        self.assertEqual(
            [
                {
                    "methods": ["GET", "HEAD"],
                    "url_prefix": "https://example.com:443/",
                },
                {
                    "methods": ["GET", "HEAD"],
                    "url_prefix": "http://private-origin.test:8000/",
                },
            ],
            run_cli["egress_rules"],
        )
        self.assertEqual(
            ["http://private-origin.test:8000"],
            run_cli["private_origins"],
        )

        self.assertFalse(visual["has_egress_origins"])
        self.assertEqual(
            [
                {
                    "methods": ["GET", "HEAD"],
                    "url_prefix": "https://example.com:443/",
                },
            ],
            visual["egress_rules"],
        )
        self.assertEqual([], visual["private_origins"])

        compiled = captured["compiled_network_case"]
        self.assertEqual(
            run_cli["egress_rules"],
            compiled["egress_rules"],
        )
        self.assertEqual(
            run_cli["private_origins"],
            compiled["private_origins"],
        )


if __name__ == "__main__":
    unittest.main()
