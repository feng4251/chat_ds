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


class BrowserRuntimeTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
        cls.services = cls.compose["services"]

    def test_worker_has_no_docker_network_or_direct_route_inputs(self):
        worker = self.services["skill-browser-executor"]
        self.assertEqual(worker["network_mode"], "none")
        for forbidden in ("networks", "dns", "dns_search", "extra_hosts", "ports"):
            self.assertNotIn(forbidden, worker)
        self.assertNotIn("skill_browser_internal", self.compose["networks"])
        self.assertEqual(
            worker["environment"]["SKILL_EGRESS_PROXY_URL"],
            "http://127.0.0.1:18080",
        )

    def test_proxy_is_the_only_networked_skill_browser_component(self):
        proxy = self.services["skill-egress-proxy"]
        self.assertEqual(proxy["networks"], ["browser_egress"])
        self.assertEqual(proxy["group_add"], ["65530"])
        self.assertEqual(
            proxy["environment"]["SKILL_EGRESS_SOCKET_PATH"],
            "/run/chatds-skill-egress/proxy.sock",
        )
        self.assertNotIn(
            "SKILL_EGRESS_PRIVATE_ORIGIN_ALLOWLIST",
            proxy["environment"],
        )
        self.assertNotIn(
            "SKILL_EGRESS_PRIVATE_CIDR_ALLOWLIST",
            proxy["environment"],
        )
        self.assertIn(
            "skill_egress_proxy_socket:/run/chatds-skill-egress",
            proxy["volumes"],
        )
        self.assertTrue(proxy["read_only"])
        self.assertEqual(proxy["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", proxy["security_opt"])
        self.assertNotIn("ports", proxy)
        for name in (
            "skill-browser-executor",
            "skill-browser-executor-socket-init",
            "skill-egress-proxy-socket-init",
        ):
            self.assertEqual(self.services[name]["network_mode"], "none")

    def test_harness_reaches_controller_only_through_readonly_uds(self):
        harness = self.services["harness"]
        self.assertIn(
            "skill_browser_executor_socket:/run/chat-ds-skill-browser-executor:ro",
            harness["volumes"],
        )
        self.assertIn("65533", harness["group_add"])
        self.assertNotIn("skill_egress_proxy_socket", "\n".join(harness["volumes"]))
        self.assertEqual(
            harness["environment"]["SKILL_BROWSER_EXECUTOR_SOCKET"],
            "/run/chat-ds-skill-browser-executor/executor.sock",
        )
        self.assertEqual(
            harness["environment"]["EXECUTOR_V2_AUTH_TOKEN"],
            "${EXECUTOR_V2_AUTH_TOKEN:-}",
        )

    def test_base_executor_has_distinct_root_controller_and_worker(self):
        executor = self.services["executor"]
        environment = executor["environment"]
        self.assertEqual(executor["network_mode"], "none")
        self.assertEqual(executor["user"], "0:0")
        self.assertEqual(environment["EXECUTOR_WORKER_UID"], "65528")
        self.assertEqual(environment["EXECUTOR_WORKER_GID"], "65528")
        self.assertNotEqual(
            environment["EXECUTOR_WORKER_UID"],
            self.services["skill-browser-executor"]["environment"][
                "EXECUTOR_WORKER_UID"
            ],
        )
        self.assertNotEqual(
            environment["EXECUTOR_WORKER_UID"],
            self.services["browser"]["user"].split(":", 1)[0],
        )
        self.assertEqual(
            environment["EXECUTOR_V2_AUTH_TOKEN"],
            "${EXECUTOR_V2_AUTH_TOKEN:-}",
        )
        self.assertEqual(environment["EXECUTOR_RUNTIME_PROFILE"], "base-v1")
        self.assertEqual(environment["EXECUTOR_MAX_PROCESS_LEASES"], "1")
        self.assertEqual(environment["EXECUTOR_MAX_PROCESS_LEASES_PER_SCOPE"], "1")
        self.assertNotIn("EXECUTOR_MAX_ADDRESS_SPACE_BYTES", environment)
        self.assertNotIn("EXECUTOR_PROCESS_MAX_NPROC", environment)
        self.assertEqual(executor["pids_limit"], 64)
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
        self.assertEqual(executor["cap_drop"], ["ALL"])
        self.assertNotIn("SYS_ADMIN", executor["cap_add"])
        self.assertTrue(
            {"SETUID", "SETGID", "KILL", "CHOWN", "FOWNER", "DAC_OVERRIDE"}
            .issubset(set(executor["cap_add"]))
        )
        self.assertIn(
            "seccomp:./executor/runtime/seccomp_profile.json",
            executor["security_opt"],
        )
        self.assertNotIn("seccomp:unconfined", executor["security_opt"])

    def test_exact_skill_snapshots_use_private_executable_tmpfs(self):
        for service_name in ("executor", "skill-browser-executor"):
            service = self.services[service_name]
            self.assertEqual(
                service["environment"]["EXECUTOR_EXECUTION_TEMP_ROOT"],
                "/run/chatds-executor-work",
            )
            self.assertTrue(
                any(
                    item.startswith("/run/chatds-executor-work:")
                    and "mode=0755" in item
                    and item.endswith(",exec")
                    for item in service["tmpfs"]
                ),
                service["tmpfs"],
            )
            self.assertTrue(
                all(
                    ",exec" not in item
                    for item in service["tmpfs"]
                    if item.startswith("/tmp:")
                ),
                service["tmpfs"],
            )

    def test_control_and_proxy_socket_initializers_are_networkless(self):
        control = self.services["skill-browser-executor-socket-init"]
        self.assertEqual(control["network_mode"], "none")
        self.assertTrue(control["read_only"])
        self.assertEqual(control["cap_drop"], ["ALL"])
        control_command = " ".join(control["command"])
        self.assertIn("test ! -L", control_command)
        self.assertIn("chown 0:65533", control_command)
        self.assertIn("chmod 0750", control_command)

        proxy = self.services["skill-egress-proxy-socket-init"]
        self.assertEqual(proxy["network_mode"], "none")
        self.assertEqual(proxy["group_add"], ["65530"])
        self.assertTrue(proxy["read_only"])
        self.assertEqual(proxy["cap_drop"], ["ALL"])
        proxy_command = " ".join(proxy["command"])
        self.assertIn("test ! -L", proxy_command)
        self.assertIn("chown 65531:65530", proxy_command)
        self.assertIn("chmod 2710", proxy_command)
        self.assertEqual(
            proxy["volumes"],
            ["skill_egress_proxy_socket:/run/chatds-skill-egress"],
        )

    def test_worker_is_root_controller_with_fixed_nonroot_skill_identity(self):
        worker = self.services["skill-browser-executor"]
        self.assertEqual(worker["build"]["target"], "browser-executor")
        self.assertEqual(worker["user"], "0:0")
        self.assertEqual(worker["group_add"], ["65530"])
        self.assertTrue(worker["read_only"])
        self.assertEqual(worker["cap_drop"], ["ALL"])
        self.assertNotIn("SYS_ADMIN", worker["cap_add"])
        self.assertIn("SYS_CHROOT", worker["cap_add"])
        self.assertTrue(
            {"SETUID", "SETGID", "KILL", "CHOWN", "FOWNER", "DAC_OVERRIDE"}
            .issubset(set(worker["cap_add"]))
        )
        security = worker["security_opt"]
        self.assertIn("no-new-privileges:true", security)
        self.assertIn(
            "seccomp:./executor/browser_runtime/seccomp_profile.json",
            security,
        )
        self.assertNotIn("seccomp:unconfined", security)
        environment = worker["environment"]
        self.assertEqual(environment["EXECUTOR_SOCKET_MODE"], "0660")
        self.assertEqual(environment["EXECUTOR_SOCKET_GID"], "65533")
        self.assertEqual(environment["EXECUTOR_WORKER_UID"], "65529")
        self.assertEqual(environment["EXECUTOR_WORKER_GID"], "65529")
        self.assertNotEqual(
            environment["EXECUTOR_WORKER_UID"],
            self.services["browser"]["user"].split(":", 1)[0],
        )
        self.assertEqual(environment["EXECUTOR_RUNTIME_PROFILE"], "browser-automation-v1")
        self.assertEqual(
            environment["EXECUTOR_ALLOWED_REQUEST_KINDS"],
            "runtime_capabilities,process_lease",
        )
        self.assertEqual(environment["EXECUTOR_MAX_PROCESS_LEASES"], "1")
        self.assertEqual(environment["EXECUTOR_MAX_PROCESS_LEASES_PER_SCOPE"], "1")
        self.assertEqual(
            environment["EXECUTOR_MAX_ADDRESS_SPACE_BYTES"],
            "unlimited",
        )
        self.assertEqual(environment["EXECUTOR_PROCESS_MAX_NPROC"], "448")
        self.assertEqual(worker["pids_limit"], 512)
        self.assertEqual(environment["SE_OFFLINE"], "true")
        self.assertEqual(environment["SE_AVOID_STATS"], "true")
        self.assertEqual(environment["SE_AVOID_BROWSER_DOWNLOAD"], "true")
        self.assertEqual(
            environment["EXECUTOR_V2_AUTH_TOKEN"],
            "${EXECUTOR_V2_AUTH_TOKEN:-}",
        )
        mounts = "\n".join(worker["volumes"])
        self.assertIn(
            "skill_egress_proxy_socket:/run/chatds-skill-egress:ro",
            mounts,
        )
        self.assertNotIn("browser_cdp_socket", mounts)
        self.assertNotIn("docker.sock", mounts)

        legacy = self.services["browser"]
        self.assertEqual(legacy["cap_drop"], ["ALL"])
        self.assertEqual(legacy["cap_add"], ["SYS_CHROOT"])
        self.assertNotIn("SYS_ADMIN", legacy["cap_add"])
        self.assertIn(
            "seccomp:./executor/browser_runtime/seccomp_profile.json",
            legacy["security_opt"],
        )
        self.assertNotIn("seccomp:unconfined", legacy["security_opt"])

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

    def test_dockerfile_target_starts_controller_dependencies_and_prlimit(self):
        dockerfile = (
            PROJECT_ROOT / "executor/Dockerfile.browser-runtime"
        ).read_text(encoding="utf-8")
        self.assertIn("FROM browser-runtime AS browser-executor", dockerfile)
        self.assertIn("COPY server.py /app/server.py", dockerfile)
        self.assertIn("util-linux", dockerfile)
        self.assertIn(
            "runtime/common-python-requirements.in",
            dockerfile,
        )
        self.assertIn("chatds-browser-executor-entrypoint", dockerfile)
        self.assertIn("chown root:root /workspace", dockerfile)
        self.assertIn("USER 0:0", dockerfile.rsplit("FROM browser-runtime", 1)[1])
        self.assertIn("packaging", (
            PROJECT_ROOT / "executor/browser_runtime/python/requirements.lock"
        ).read_text(encoding="utf-8"))
        large_smoke = (
            PROJECT_ROOT
            / "executor/browser_runtime/smoke/large_visual_artifact.py"
        ).read_text(encoding="utf-8")
        self.assertIn("8 * 1024 * 1024 < size < 24 * 1024 * 1024", large_smoke)
        entrypoint = (
            PROJECT_ROOT
            / "executor/browser_runtime/bin/chatds-browser-executor-entrypoint"
        ).read_text(encoding="utf-8")
        self.assertIn("chatds-skill-egress-bridge", entrypoint)
        self.assertNotIn('Xvfb "${display}"', entrypoint)
        self.assertIn("short-lived Weston compositor", entrypoint)
        self.assertIn("--clear-groups", entrypoint)
        self.assertIn("chatds-browser-readiness.XXXXXX", entrypoint)
        self.assertIn("chatds-browser-runtime-health --browser-smoke", entrypoint)
        self.assertLess(
            entrypoint.index("chatds-browser-runtime-health --browser-smoke"),
            entrypoint.index("/usr/local/bin/python -I /app/server.py"),
        )
        self.assertIn("/usr/local/bin/python -I /app/server.py", entrypoint)

    def test_proxy_image_is_digest_pinned_and_dependency_free(self):
        dockerfile = (
            PROJECT_ROOT / "skill_egress_proxy/Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "python:3.12.11-slim-bookworm@sha256:519591d6",
            dockerfile,
        )
        self.assertNotIn("pip install", dockerfile)
        self.assertNotIn("apt-get install", dockerfile)
        self.assertIn("USER 65531:65531", dockerfile)

    def test_example_environment_documents_authority_and_proxy_policy(self):
        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("EXECUTOR_V2_AUTH_TOKEN=", example)
        self.assertIn("SKILL_EGRESS_PUBLIC_PORTS=80,443", example)
        self.assertNotIn("SKILL_EGRESS_PRIVATE_ORIGIN_ALLOWLIST=", example)
        self.assertNotIn("SKILL_EGRESS_PRIVATE_CIDR_ALLOWLIST=", example)
        self.assertIn("production Skill lane is intentionally public-only", example)


if __name__ == "__main__":
    unittest.main()
