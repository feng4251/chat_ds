import json
from pathlib import Path
import unittest

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.yml"
SECCOMP_PATH = (
    PROJECT_ROOT / "executor/browser_runtime/seccomp_profile.json"
)
BASE_SECCOMP_PATH = PROJECT_ROOT / "executor/runtime/seccomp_profile.json"
PROXY_DOCKERFILE_PATH = PROJECT_ROOT / "skill_egress_proxy/Dockerfile"


class BrowserRuntimeTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        cls.services = cls.compose["services"]
        cls.native_runner_anchors = (
            "claude-runner-image",
            "deepseek-harness-runner-image",
        )

    def test_session_sandbox_has_no_docker_network_or_direct_route_inputs(self):
        for name in self.native_runner_anchors:
            sandbox = self.services[name]
            self.assertEqual(sandbox["network_mode"], "none")
            for forbidden in (
                "networks",
                "dns",
                "dns_search",
                "extra_hosts",
                "ports",
                "pid",
            ):
                self.assertNotIn(forbidden, sandbox)
        self.assertNotIn("skill_browser_internal", self.compose["networks"])
        self.assertNotIn("browser_egress", self.compose["networks"])
        self.assertFalse({
            "harness", "browser", "executor", "executor-2",
            "executor-3", "executor-4",
        } & set(self.services))

    def test_proxy_is_the_only_networked_session_sandbox_component(self):
        proxy = self.services["skill-egress-proxy"]
        self.assertEqual(
            proxy["networks"],
            ["public_egress", "search_net"],
        )
        self.assertEqual(proxy["group_add"], ["65530"])
        self.assertEqual(
            proxy["environment"]["SKILL_EGRESS_SOCKET_PATH"],
            "/run/chatds-skill-egress/proxy.sock",
        )
        private_origins = proxy["environment"][
            "SKILL_EGRESS_PRIVATE_ORIGIN_ALLOWLIST"
        ]
        self.assertIn("${BROWSER_PRIVATE_ORIGIN_ALLOWLIST:-}", private_origins)
        self.assertIn(
            "${CLAUDE_PROVIDER_PRIVATE_ORIGIN_ALLOWLIST:-",
            private_origins,
        )
        self.assertIn("${MARKET_DATA_PRIVATE_ORIGIN:-", private_origins)
        self.assertEqual(
            proxy["environment"][
                "SKILL_EGRESS_PRIVATE_CIDR_ALLOWLIST"
            ],
            "${SKILL_EGRESS_PRIVATE_CIDR_ALLOWLIST:-172.29.250.0/24}",
        )
        expected_budget_environment = {
            "SKILL_EGRESS_MAX_REQUESTS": (
                "${SKILL_EGRESS_MAX_REQUESTS:-8192}"
            ),
            "SKILL_EGRESS_MAX_OUTBOUND_BYTES": (
                "${SKILL_EGRESS_MAX_OUTBOUND_BYTES:-67108864}"
            ),
            "SKILL_EGRESS_MAX_RESPONSE_WIRE_BYTES": (
                "${SKILL_EGRESS_MAX_RESPONSE_WIRE_BYTES:-2147483648}"
            ),
            "SKILL_EGRESS_MAX_POLICY_SCOPE_ENTRIES": (
                "${SKILL_EGRESS_MAX_POLICY_SCOPE_ENTRIES:-65536}"
            ),
            "SKILL_EGRESS_POLICY_SCOPE_TTL_SECONDS": (
                "${SKILL_EGRESS_POLICY_SCOPE_TTL_SECONDS:-86400}"
            ),
        }
        for name, expected in expected_budget_environment.items():
            self.assertEqual(proxy["environment"][name], expected)
        self.assertEqual(
            "1",
            proxy["environment"]["SKILL_EGRESS_REQUIRE_POLICY_V3"],
        )
        self.assertIn(
            "skill_egress_proxy_socket:/run/chatds-skill-egress",
            proxy["volumes"],
        )
        self.assertIn(
            (
                "skill_egress_proxy_private:"
                "/var/lib/chatds-skill-egress-private"
            ),
            proxy["volumes"],
        )
        self.assertTrue(proxy["read_only"])
        self.assertEqual(proxy["user"], "65531:65531")
        self.assertEqual(proxy["cap_drop"], ["ALL"])
        # This host cannot stack NNP with any seccomp filter (runc errno 524).
        # The proxy therefore keeps Docker's default seccomp while its image
        # is attested as setid/capability stripped and runs non-root/capless.
        self.assertNotIn("security_opt", proxy)
        proxy_dockerfile = PROXY_DOCKERFILE_PATH.read_text(encoding="utf-8")
        self.assertIn(
            'org.opencontainers.image.chatds.setid-stripped="true"',
            proxy_dockerfile,
        )
        self.assertIn("find / -xdev -type f -perm /6000", proxy_dockerfile)
        self.assertIn("getcap -r /", proxy_dockerfile)
        self.assertNotIn("ports", proxy)
        for name in self.native_runner_anchors:
            self.assertEqual(self.services[name]["network_mode"], "none")
        self.assertEqual(
            self.services["skill-egress-proxy-socket-init"]["network_mode"],
            "none",
        )
        proxy_socket_consumers = {
            name
            for name, service in self.services.items()
            if any(
                str(volume).startswith("skill_egress_proxy_socket:")
                for volume in service.get("volumes", [])
            )
        }
        self.assertEqual(
            proxy_socket_consumers,
            {
                "skill-egress-proxy",
                "skill-egress-proxy-socket-init",
            },
        )
        private_trust_consumers = {
            name
            for name, service in self.services.items()
            if any(
                str(volume).startswith(
                    "skill_egress_proxy_private:"
                )
                for volume in service.get("volumes", [])
            )
        }
        self.assertEqual(
            private_trust_consumers,
            {
                "skill-egress-proxy",
                "skill-egress-proxy-socket-init",
            },
        )
        # Native Turn containers are created dynamically by their trusted
        # supervisors, so the static Compose graph must not mount the proxy
        # socket into Backend, Frontend, or either image anchor.
        self.assertTrue(
            set(self.native_runner_anchors).isdisjoint(proxy_socket_consumers)
        )

    @unittest.skip(
        "Archived executor-pool topology; native control planes are covered "
        "by claude_runner/tests/test_deployment_topology.py"
    )
    def test_harness_reaches_only_the_unified_controller_uds(self):
        harness = self.services["harness"]
        short_mounts = {
            volume
            for volume in harness["volumes"]
            if isinstance(volume, str)
        }
        expected_mounts = {
            "executor_socket:/run/chat-ds-executor:ro",
            "executor_socket_2:/run/chat-ds-executor-2:ro",
            "executor_socket_3:/run/chat-ds-executor-3:ro",
            "executor_socket_4:/run/chat-ds-executor-4:ro",
        }
        self.assertTrue(
            expected_mounts.issubset(short_mounts),
        )
        self.assertIn("65533", harness["group_add"])
        self.assertNotIn(
            "skill_egress_proxy_socket",
            "\n".join(sorted(short_mounts)),
        )
        self.assertNotIn(
            "SKILL_EGRESS_POLICY_TOKEN",
            harness["environment"],
        )
        self.assertEqual(
            harness["environment"]["SKILL_BROWSER_EXECUTOR_SOCKET"],
            self.services["executor"]["environment"]["EXECUTOR_SOCKET"],
        )
        self.assertEqual(
            harness["environment"]["SKILL_BROWSER_EXECUTOR_SOCKET"],
            "/run/chat-ds-executor/executor.sock",
        )
        self.assertEqual(
            harness["environment"]["EXECUTOR_SOCKET"],
            "/run/chat-ds-executor/executor.sock",
        )
        self.assertEqual(
            harness["environment"]["EXECUTOR_POOL_SOCKETS"].split(","),
            [
                "/run/chat-ds-executor/executor.sock",
                "/run/chat-ds-executor-2/executor.sock",
                "/run/chat-ds-executor-3/executor.sock",
                "/run/chat-ds-executor-4/executor.sock",
            ],
        )
        self.assertEqual(
            harness["environment"]["EXECUTOR_V2_AUTH_TOKEN"],
            "${EXECUTOR_V2_AUTH_TOKEN:-}",
        )
        self.assertTrue(
            set(self.executor_names).issubset(
                set(harness["depends_on"])
            )
        )

    def test_workspace_lock_plane_is_local_and_native_control_planes_only(self):
        self.assertEqual(
            self.compose["volumes"]["workspace_mutation_locks"],
            {
                "driver": "local",
                "name": (
                    "${WORKSPACE_MUTATION_LOCK_VOLUME_NAME:-"
                    "chat_ds_workspace_mutation_locks}"
                ),
            },
        )
        expected_mount = {
            "type": "volume",
            "source": "workspace_mutation_locks",
            "target": "/run/chatds-workspace-lock-plane",
            "volume": {"nocopy": True},
        }
        consumers = set()
        for name, service in self.services.items():
            for mount in service.get("volumes", []):
                if (
                    isinstance(mount, dict)
                    and mount.get("source") == "workspace_mutation_locks"
                ):
                    consumers.add(name)
                    self.assertEqual(expected_mount, mount)
                    self.assertFalse(mount.get("read_only", False))
                elif (
                    isinstance(mount, str)
                    and mount.split(":", 1)[0]
                    == "workspace_mutation_locks"
                ):
                    self.fail(
                        "workspace lock plane must use nocopy long syntax"
                    )
        self.assertEqual(
            consumers,
            {
                "backend",
                "claude-runner-supervisor",
                "deepseek-runner-supervisor",
            },
        )
        for name in consumers:
            environment = self.services[name]["environment"]
            self.assertEqual(
                environment["WORKSPACE_MUTATION_LOCK_ROOT"],
                "/run/chatds-workspace-lock-plane/locks",
            )
            self.assertEqual(
                environment[
                    "WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT"
                ],
                "1",
            )
        for name in (
            *self.native_runner_anchors,
            "browser",
            "skill-egress-proxy",
            "frontend",
            "searxng",
            "searxng-valkey",
        ):
            self.assertNotIn(name, consumers)

    @unittest.skip(
        "Archived executor-pool topology; native per-Turn launch security is "
        "covered by both supervisor lifecycle suites"
    )
    def test_unified_sandbox_has_root_controller_and_fixed_nonroot_worker(self):
        sandbox = self.services["executor"]
        environment = sandbox["environment"]
        self.assertEqual(sandbox["build"]["target"], "session-sandbox")
        self.assertEqual(sandbox["network_mode"], "none")
        self.assertEqual(sandbox["user"], "0:0")
        self.assertEqual(sandbox["group_add"], ["65530"])
        self.assertEqual(environment["EXECUTOR_SOCKET_MODE"], "0660")
        self.assertEqual(environment["EXECUTOR_SOCKET_GID"], "65533")
        self.assertEqual(environment["EXECUTOR_WORKER_UID"], "65529")
        self.assertEqual(environment["EXECUTOR_WORKER_GID"], "65529")
        self.assertNotEqual(
            environment["EXECUTOR_WORKER_UID"],
            self.services["browser"]["user"].split(":", 1)[0],
        )
        self.assertEqual(
            environment["EXECUTOR_V2_AUTH_TOKEN"],
            "${EXECUTOR_V2_AUTH_TOKEN:-}",
        )
        self.assertEqual(
            environment["SKILL_EGRESS_POLICY_TOKEN"],
            "${EXECUTOR_V2_AUTH_TOKEN:-}",
        )
        self.assertEqual(
            self.services["skill-egress-proxy"]["environment"][
                "SKILL_EGRESS_POLICY_TOKEN"
            ],
            environment["SKILL_EGRESS_POLICY_TOKEN"],
        )
        self.assertEqual(
            environment["EXECUTOR_RUNTIME_PROFILE"],
            "session-sandbox-v1",
        )
        self.assertEqual(environment["EXECUTOR_MAX_PROCESS_LEASES"], "1")
        self.assertEqual(environment["EXECUTOR_MAX_PROCESS_LEASES_PER_SCOPE"], "1")
        self.assertEqual(
            environment["EXECUTOR_MAX_ADDRESS_SPACE_BYTES"],
            "unlimited",
        )
        self.assertEqual(environment["EXECUTOR_PROCESS_MAX_NPROC"], "4096")
        self.assertEqual(sandbox["pids_limit"], 512)
        self.assertEqual(
            set(environment["EXECUTOR_ALLOWED_REQUEST_KINDS"].split(",")),
            {
                "runtime_capabilities",
                "process_lease",
                "skill_script",
                "declared_command",
                "session_code",
                "legacy_code",
            },
        )
        self.assertEqual(environment["SE_OFFLINE"], "true")
        self.assertEqual(environment["SE_AVOID_STATS"], "true")
        self.assertEqual(environment["SE_AVOID_BROWSER_DOWNLOAD"], "true")
        self.assertEqual(sandbox["cap_drop"], ["ALL"])
        self.assertNotIn("SYS_ADMIN", sandbox["cap_add"])
        self.assertIn("SYS_CHROOT", sandbox["cap_add"])
        self.assertTrue(
            {"SETUID", "SETGID", "KILL", "CHOWN", "FOWNER", "DAC_OVERRIDE"}
            .issubset(set(sandbox["cap_add"]))
        )
        self.assertIn(
            "no-new-privileges:true",
            sandbox["security_opt"],
        )
        self.assertIn(
            "seccomp:./executor/browser_runtime/seccomp_profile.json",
            sandbox["security_opt"],
        )
        self.assertNotIn("seccomp:unconfined", sandbox["security_opt"])
        mounts = "\n".join(sandbox["volumes"])
        self.assertIn(
            "skill_egress_proxy_socket:/run/chatds-skill-egress:ro",
            mounts,
        )
        self.assertNotIn("browser_cdp_socket", mounts)
        self.assertNotIn("docker.sock", mounts)

    @unittest.skip(
        "Archived executor-pool topology; native Skill-view mounts are covered "
        "by both supervisor lifecycle suites"
    )
    def test_exact_skill_snapshots_use_private_executable_tmpfs(self):
        sandbox = self.services["executor"]
        self.assertEqual(
            sandbox["environment"]["EXECUTOR_EXECUTION_TEMP_ROOT"],
            "/run/chatds-executor-work",
        )
        self.assertTrue(
            any(
                item.startswith("/run/chatds-executor-work:")
                and "mode=0755" in item
                and item.endswith(",exec")
                for item in sandbox["tmpfs"]
            ),
            sandbox["tmpfs"],
        )
        self.assertTrue(
            all(
                ",exec" not in item
                for item in sandbox["tmpfs"]
                if item.startswith("/tmp:")
            ),
            sandbox["tmpfs"],
        )

    def test_proxy_socket_initializer_is_networkless(self):
        proxy = self.services["skill-egress-proxy-socket-init"]
        self.assertEqual(proxy["network_mode"], "none")
        self.assertEqual(proxy["group_add"], ["65530"])
        self.assertTrue(proxy["read_only"])
        self.assertEqual(proxy["cap_drop"], ["ALL"])
        self.assertEqual(
            proxy["cap_add"],
            ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
        )
        proxy_command = " ".join(proxy["command"])
        self.assertIn("test ! -L", proxy_command)
        self.assertIn("chown 65531:65530", proxy_command)
        self.assertIn("chmod 2710", proxy_command)
        self.assertIn("chown 65531:65531", proxy_command)
        self.assertIn("chmod 0700", proxy_command)
        self.assertIn("private_count", proxy_command)
        self.assertIn(
            "SKILL_EGRESS_ALLOW_LEGACY_TRUST_MIGRATION",
            proxy_command,
        )
        self.assertIn(
            "test ! -e /run/chatds-skill-egress/generation.json",
            proxy_command,
        )
        self.assertNotIn(
            "rm -f /var/lib/chatds-skill-egress-private",
            proxy_command,
        )
        self.assertEqual(
            proxy["environment"][
                "SKILL_EGRESS_ALLOW_LEGACY_TRUST_MIGRATION"
            ],
            "${SKILL_EGRESS_ALLOW_LEGACY_TRUST_MIGRATION:-0}",
        )
        self.assertEqual(
            proxy["volumes"],
            [
                "skill_egress_proxy_socket:/run/chatds-skill-egress",
                (
                    "skill_egress_proxy_private:"
                    "/var/lib/chatds-skill-egress-private"
                ),
            ],
        )

    @unittest.skip(
        "The retired browser service is absent from the native deployment"
    )
    def test_legacy_browser_keeps_browser_sandbox_security(self):
        legacy = self.services["browser"]
        self.assertEqual(legacy["cap_drop"], ["ALL"])
        self.assertEqual(legacy["cap_add"], ["SYS_CHROOT"])
        self.assertNotIn("SYS_ADMIN", legacy["cap_add"])
        self.assertIn(
            "seccomp:./executor/browser_runtime/seccomp_profile.json",
            legacy["security_opt"],
        )
        self.assertNotIn("seccomp:unconfined", legacy["security_opt"])

    @unittest.skip(
        "The retired four-slot executor pool is absent from the native deployment"
    )
    def test_four_homogeneous_executor_slots_have_private_socket_volumes(self):
        self.assertNotIn("skill-browser-executor", self.services)
        self.assertNotIn("skill-browser-executor-socket-init", self.services)
        self.assertNotIn("skill_browser_executor_socket", self.compose["volumes"])
        self.assertEqual(
            {
                name
                for name, service in self.services.items()
                if service.get("environment", {}).get(
                    "EXECUTOR_RUNTIME_PROFILE"
                )
                == "session-sandbox-v1"
            },
            set(self.executor_names),
        )
        first = self.services["executor"]
        for name in self.executor_names:
            slot = self.services[name]
            for field in (
                "image",
                "build",
                "network_mode",
                "user",
                "group_add",
                "read_only",
                "pids_limit",
                "mem_limit",
                "cpus",
                "environment",
                "cap_drop",
                "cap_add",
                "security_opt",
                "tmpfs",
            ):
                self.assertEqual(first[field], slot[field])
        socket_mounts = {
            name: next(
                volume
                for volume in self.services[name]["volumes"]
                if volume.endswith(":/run/chat-ds-executor")
            )
            for name in self.executor_names
        }
        self.assertEqual(4, len(set(socket_mounts.values())))
        self.assertEqual(
            {
                "executor_socket",
                "executor_socket_2",
                "executor_socket_3",
                "executor_socket_4",
            },
            {
                mount.split(":", 1)[0]
                for mount in socket_mounts.values()
            },
        )

    def test_pinned_seccomp_is_default_deny_plus_chromium_namespace_calls(self):
        profile = json.loads(SECCOMP_PATH.read_text(encoding="utf-8"))
        self.assertEqual(profile["defaultAction"], "SCMP_ACT_ERRNO")
        namespace_rule = profile["syscalls"][0]
        self.assertEqual(namespace_rule["action"], "SCMP_ACT_ALLOW")
        self.assertEqual(
            set(namespace_rule["names"]),
            {"clone", "setns", "unshare"},
        )
        self.assertNotIn("SCMP_ACT_ALLOW", {profile["defaultAction"]})
        allowed = {
            name
            for rule in profile["syscalls"]
            if rule["action"] == "SCMP_ACT_ALLOW"
            for name in rule["names"]
        }
        forbidden_ipc = {
            "ipc",
            "msgctl",
            "msgget",
            "msgrcv",
            "msgsnd",
            "semctl",
            "semget",
            "semop",
            "semtimedop",
            "semtimedop_time64",
            "shmat",
            "shmctl",
            "shmdt",
            "shmget",
            "mq_getsetattr",
            "mq_notify",
            "mq_open",
            "mq_timedreceive",
            "mq_timedreceive_time64",
            "mq_timedsend",
            "mq_timedsend_time64",
            "mq_unlink",
        }
        self.assertTrue(forbidden_ipc.isdisjoint(allowed))

        base_profile = json.loads(
            BASE_SECCOMP_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(base_profile["defaultAction"], "SCMP_ACT_ERRNO")
        base_allowed = {
            name
            for rule in base_profile["syscalls"]
            if rule["action"] == "SCMP_ACT_ALLOW"
            for name in rule["names"]
        }
        self.assertTrue(forbidden_ipc.isdisjoint(base_allowed))
        self.assertFalse(
            any(
                rule["action"] == "SCMP_ACT_ALLOW"
                and set(rule["names"]) == {"clone", "setns", "unshare"}
                and not rule.get("args")
                for rule in base_profile["syscalls"]
            )
        )

    def test_dockerfile_target_builds_unified_browser_dependency_superset(self):
        dockerfile = (
            PROJECT_ROOT / "executor/Dockerfile.browser-runtime"
        ).read_text(encoding="utf-8")
        self.assertIn("FROM browser-runtime AS session-sandbox", dockerfile)
        self.assertNotIn("FROM browser-runtime AS browser-executor", dockerfile)
        self.assertIn("COPY server.py /app/server.py", dockerfile)
        self.assertIn("util-linux", dockerfile)
        self.assertIn(
            "runtime/common-python-requirements.in",
            dockerfile,
        )
        self.assertIn("chromium-driver", dockerfile)
        self.assertIn("chatds-browser-executor-entrypoint", dockerfile)
        self.assertIn("chown root:root /workspace", dockerfile)
        self.assertIn("USER 0:0", dockerfile.rsplit("FROM browser-runtime", 1)[1])
        self.assertIn(
            "EXECUTOR_RUNTIME_PROFILE=session-sandbox-v1",
            dockerfile,
        )
        self.assertIn(
            "runtime_capabilities,process_lease,skill_script,"
            "declared_command,session_code,legacy_code",
            dockerfile,
        )
        python_requirements = (
            PROJECT_ROOT / "executor/browser_runtime/python/requirements.lock"
        ).read_text(encoding="utf-8")
        self.assertIn("packaging", python_requirements)
        self.assertIn("playwright==", python_requirements)
        self.assertIn("selenium==", python_requirements)
        node_package = (
            PROJECT_ROOT / "executor/browser_runtime/node/package.json"
        ).read_text(encoding="utf-8")
        self.assertIn('"playwright"', node_package)
        large_smoke = (
            PROJECT_ROOT
            / "executor/browser_runtime/smoke/large_visual_artifact.py"
        ).read_text(encoding="utf-8")
        self.assertIn("8 * 1024 * 1024 < size < 24 * 1024 * 1024", large_smoke)
        entrypoint = (
            PROJECT_ROOT
            / "executor/browser_runtime/bin/chatds-browser-executor-entrypoint"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            entrypoint.count(
                "/usr/local/bin/chatds-skill-egress-bridge &"
            ),
            1,
        )
        self.assertIn(
            'SKILL_EGRESS_PROXY_URL="http://127.0.0.1:18080"',
            entrypoint,
        )
        self.assertNotIn('Xvfb "${display}"', entrypoint)
        self.assertIn("short-lived Weston compositor", entrypoint)
        self.assertIn("/usr/bin/env -i", entrypoint)
        self.assertIn("--clear-groups", entrypoint)
        self.assertIn("chatds-browser-readiness.XXXXXX", entrypoint)
        self.assertIn("chatds-browser-runtime-health --browser-smoke", entrypoint)
        bridge_stop = entrypoint.index(
            'kill -TERM "${readiness_bridge_pid}"'
        )
        smoke = entrypoint.index(
            "chatds-browser-runtime-health --browser-smoke"
        )
        server = entrypoint.index(
            "/usr/local/bin/python -I /app/server.py"
        )
        self.assertLess(
            smoke,
            bridge_stop,
        )
        self.assertLess(bridge_stop, server)

    def test_proxy_image_uses_pinned_base_and_snapshot_crypto_runtime(self):
        dockerfile = (
            PROJECT_ROOT / "skill_egress_proxy/Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "python:3.12.11-slim-bookworm@sha256:519591d6",
            dockerfile,
        )
        self.assertNotIn("pip install", dockerfile)
        self.assertIn(
            "snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/",
            dockerfile,
        )
        self.assertIn("apt-get install -y --no-install-recommends", dockerfile)
        self.assertIn("openssl", dockerfile)
        self.assertIn("test -x /usr/bin/openssl", dockerfile)
        self.assertIn("USER 65531:65531", dockerfile)

    def test_example_environment_documents_authority_and_proxy_policy(self):
        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("EXECUTOR_V2_AUTH_TOKEN=", example)
        self.assertIn("SKILL_EGRESS_PRIVATE_CIDR_ALLOWLIST=", example)
        self.assertIn("SKILL_EGRESS_PUBLIC_PORTS=80,443", example)
        self.assertNotIn("SKILL_EGRESS_PRIVATE_ORIGIN_ALLOWLIST=", example)
        self.assertIn(
            "deployment/user-turn origin",
            example,
        )
        self.assertIn("Exact literal-IP", example)


if __name__ == "__main__":
    unittest.main()
