import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = ROOT / "browser_runtime"

import sys

sys.path.insert(0, str(RUNTIME_ROOT))

from chatds_browser_runtime.chromium_proxy import controlled_arguments
from chatds_browser_runtime.healthcheck import _common_requirement_names, _run
from chatds_browser_runtime.policy import (
    PolicyError,
    ProxyPolicy,
    load_proxy_environment,
    proxy_environment,
)
from chatds_browser_runtime.runtime_exec import LaunchError, command_for_script
from chatds_browser_runtime import runtime_exec


class BrowserRuntimeProfileTests(unittest.TestCase):
    def test_manifest_has_pinned_generic_runtime_contract(self):
        profile = json.loads((RUNTIME_ROOT / "profile.json").read_text())
        self.assertEqual(profile["profile_id"], "browser-automation-v1")
        self.assertEqual(profile["interpreters"]["node"], "22.18.0")
        self.assertEqual(profile["interpreters"]["python"], "3.12.11")
        self.assertEqual(profile["libraries"]["node_playwright"], "1.61.0")
        self.assertEqual(profile["libraries"]["python_playwright"], "1.61.0")
        self.assertEqual(profile["libraries"]["selenium"], "4.46.0")
        self.assertEqual(profile["libraries"]["packaging"], "25.0")
        self.assertEqual(
            profile["script_extensions"],
            [".cjs", ".js", ".mjs", ".py", ".sh", ".bash"],
        )
        self.assertEqual(profile["identities"]["controller_uid"], 0)
        self.assertNotEqual(
            profile["identities"]["controller_uid"],
            profile["identities"]["worker_uid"],
        )
        self.assertEqual(profile["identities"]["worker_supplementary_groups"], [])
        self.assertTrue(
            profile["identities"]["controller_drops_child_identity"]
        )
        self.assertFalse(any(profile["runtime_installers"].values()))
        self.assertFalse(profile["network_contract"]["direct_egress_allowed"])
        self.assertEqual(
            profile["network_contract"]["proxy_environment_variable"],
            "SKILL_EGRESS_PROXY_URL",
        )
        self.assertTrue(
            profile["network_contract"]["proxy_wrapper_is_defense_in_depth"]
        )
        self.assertEqual(
            profile["network_contract"]["worker_network_mode"],
            "none",
        )
        self.assertEqual(
            set(profile["seccomp_contract"]["chromium_namespace_syscalls"]),
            {"clone", "setns", "unshare"},
        )
        self.assertEqual(
            profile["seccomp_contract"]["required_capabilities"],
            ["SYS_CHROOT"],
        )
        self.assertTrue(
            profile["network_contract"]["production_skill_lane_public_only"]
        )
        self.assertEqual(
            profile["display_contract"],
            {
                "backend": "weston-headless-wayland",
                "headed_browser": True,
                "session_scope": (
                    "one private compositor and Wayland socket per lease process"
                ),
                "worker_owned": True,
                "x11": False,
            },
        )

    def test_node_lock_is_exact_and_integrity_checked(self):
        package = json.loads((RUNTIME_ROOT / "node/package.json").read_text())
        lock = json.loads((RUNTIME_ROOT / "node/package-lock.json").read_text())
        self.assertEqual(package["dependencies"], {"playwright": "1.61.0"})
        self.assertEqual(lock["lockfileVersion"], 3)
        for name in ("playwright", "playwright-core"):
            record = lock["packages"][f"node_modules/{name}"]
            self.assertEqual(record["version"], "1.61.0")
            self.assertTrue(record["integrity"].startswith("sha512-"))

    def test_python_lock_is_exact_and_hash_checked(self):
        lines = (RUNTIME_ROOT / "python/requirements.lock").read_text().splitlines()
        requirements = [
            line
            for line in lines
            if line and not line.startswith(("#", " ", "\t"))
        ]
        self.assertGreaterEqual(len(requirements), 10)
        self.assertTrue(all("==" in line for line in requirements))
        text = "\n".join(lines)
        self.assertEqual(text.count("playwright==1.61.0"), 1)
        self.assertEqual(text.count("selenium==4.46.0"), 1)
        self.assertEqual(text.count("packaging==25.0"), 1)
        self.assertGreaterEqual(text.count("--hash=sha256:"), len(requirements))

    def test_browser_lock_covers_shared_base_python_manifest(self):
        common_path = ROOT / "runtime/common-python-requirements.in"
        expected = {
            name.lower().replace("_", "-")
            for name in _common_requirement_names(common_path)
        }
        lock_text = (
            RUNTIME_ROOT / "python/requirements.lock"
        ).read_text(encoding="utf-8")
        locked = {
            line.split("==", 1)[0].strip().lower().replace("_", "-")
            for line in lock_text.splitlines()
            if line and not line.startswith(("#", " ", "\t")) and "==" in line
        }
        self.assertTrue(expected.issubset(locked))

    def test_dockerfile_has_immutable_build_and_nonroot_runtime(self):
        text = (ROOT / "Dockerfile.browser-runtime").read_text()
        self.assertEqual(text.count("sha256:752ea8a2"), 1)
        self.assertEqual(text.count("sha256:519591d6"), 2)
        self.assertIn("snapshot.debian.org/archive/debian/", text)
        self.assertIn("chromium-driver", text)
        self.assertIn("weston", text)
        self.assertNotIn("\n      xvfb", text)
        self.assertNotIn("\n      xauth", text)
        self.assertIn("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1", text)
        self.assertIn("USER 65529:65529", text)
        self.assertIn("mv /usr/bin/chromium", text)
        self.assertIn("chromium-headless-shell", text)
        self.assertIn(
            "ln -s /opt/chatds-browser-runtime/node_modules /node_modules",
            text,
        )
        self.assertIn("! command -v npm", text)
        self.assertIn("! command -v pip", text)
        self.assertIn("! command -v apt-get", text)
        final_stage = text.rsplit("\nFROM ", 1)[1]
        self.assertNotIn("\nRUN npm ", final_stage)
        self.assertNotIn("\nRUN python -m pip install", final_stage)

    def test_policy_uses_fixed_runtime_environment_key(self):
        policy = load_proxy_environment(
            {"SKILL_EGRESS_PROXY_URL": "http://egress-proxy:3128"}
        )
        self.assertEqual(policy.policy_id, "runtime-egress-proxy")
        self.assertEqual(policy.proxy_url, "http://egress-proxy:3128")
        environment = proxy_environment(policy)
        self.assertEqual(
            environment["SKILL_EGRESS_PROXY_URL"],
            "http://egress-proxy:3128",
        )
        self.assertEqual(environment["NO_PROXY"], "localhost,127.0.0.1,[::1]")

    def test_policy_rejects_credentials_path_and_missing_value(self):
        for value in (
            "",
            "http://user:password@proxy:3128",
            "http://proxy:3128/path",
            "https://proxy:3128",
            "http://proxy",
            "http://proxy:not-a-port",
            "http://proxy:3128?",
            "http://proxy:3128\n--no-proxy-server",
        ):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                load_proxy_environment({"SKILL_EGRESS_PROXY_URL": value})

    def test_unrelated_proxy_environment_cannot_replace_fixed_key(self):
        with self.assertRaises(PolicyError):
            load_proxy_environment(
                {
                    "HTTP_PROXY": "http://proxy:3128",
                    "HTTPS_PROXY": "http://proxy:3128",
                }
            )

    def test_chromium_wrapper_rejects_proxy_override(self):
        policy = ProxyPolicy("lease", "http://proxy:3128")
        forbidden = (
            "--proxy-server=http://attacker:8080",
            "--proxy-bypass-list=*",
            "--host-resolver-rules=MAP * 127.0.0.1",
            "--remote-debugging-address=0.0.0.0",
            "--remote-debugging-port=9222",
            "--disable-blink-features=AutomationControlled",
            "--disable-seccomp-filter-sandbox",
            "--disable-namespace-sandbox",
            "--disable-gpu-sandbox",
            "--disable-zygote-sandbox",
            "--no-proxy-server",
        )
        for argument in forbidden:
            with self.subTest(argument=argument), self.assertRaises(ValueError):
                controlled_arguments([argument], policy)

    def test_only_trusted_chromedriver_may_request_ephemeral_loopback_control(self):
        policy = ProxyPolicy("lease", "http://proxy:3128")
        with self.assertRaises(ValueError):
            controlled_arguments(["--remote-debugging-port=0"], policy)
        arguments = controlled_arguments(
            ["--remote-debugging-port=0"],
            policy,
            trusted_chromedriver_parent=True,
        )
        self.assertIn("--remote-debugging-port=0", arguments)
        with self.assertRaises(ValueError):
            controlled_arguments(
                ["--remote-debugging-port=9222"],
                policy,
                trusted_chromedriver_parent=True,
            )

    def test_chromium_wrapper_appends_runtime_owned_controls(self):
        policy = ProxyPolicy("lease", "http://proxy:3128")
        arguments = controlled_arguments(
            ["--headless=new", "--no-sandbox"],
            policy,
        )
        self.assertEqual(arguments[0], "--headless=new")
        self.assertNotIn("--no-sandbox", arguments)
        self.assertIn("--proxy-server=http://proxy:3128", arguments)
        self.assertIn("--proxy-bypass-list=<-loopback>", arguments)
        self.assertIn("--disable-quic", arguments)
        self.assertIn("--disable-breakpad", arguments)
        self.assertIn("--disable-crash-reporter", arguments)
        self.assertIn("--disable-dev-shm-usage", arguments)
        self.assertIn("--ozone-platform=wayland", arguments)
        self.assertIn("--disable-setuid-sandbox", arguments)
        self.assertIn(
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            arguments,
        )

    def test_launcher_selects_exact_cjs_and_python_interpreters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node_script = root / "session.cjs"
            python_script = root / "operator.py"
            shell_script = root / "operator.sh"
            node_script.write_text("process.exit(0)")
            python_script.write_text("raise SystemExit(0)")
            shell_script.write_text("exit 0")
            self.assertEqual(
                command_for_script(node_script, ["--x"]),
                ["/usr/local/bin/node", str(node_script.resolve()), "--x"],
            )
            self.assertEqual(
                command_for_script(python_script, []),
                ["/usr/local/bin/python", "-I", str(python_script.resolve())],
            )
            self.assertEqual(
                command_for_script(shell_script, []),
                [
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    str(shell_script.resolve()),
                ],
            )

    def test_launcher_rejects_unknown_extension_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shell_script = root / "install.fish"
            shell_script.write_text("npm install")
            with self.assertRaises(LaunchError):
                command_for_script(shell_script, [])
            link = root / "linked.py"
            link.symlink_to(shell_script)
            with self.assertRaises(LaunchError):
                command_for_script(link, [])

    def test_worker_environment_reuses_private_lease_directories(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            home = root / "home"
            temporary = root / "tmp"
            home.mkdir(mode=0o700)
            temporary.mkdir(mode=0o700)
            environment = {
                "HOME": str(home),
                "TMPDIR": str(temporary),
                "OPENBLAS_NUM_THREADS": "1",
                "PYTHONUTF8": "1",
                "SKILL_DIR": "/immutable/exact-skill",
            }
            with (
                mock.patch.object(runtime_exec, "WORKER_UID", os.geteuid()),
                mock.patch.dict(os.environ, environment, clear=True),
            ):
                result = runtime_exec.worker_environment(
                    ProxyPolicy("lease", "http://proxy:3128"),
                    process_id=123,
                )
            self.assertEqual(result["HOME"], str(home))
            self.assertEqual(result["TMPDIR"], str(temporary))
            self.assertEqual(result["XDG_RUNTIME_DIR"], str(temporary))
            self.assertEqual(result["OPENBLAS_NUM_THREADS"], "1")
            self.assertEqual(result["PYTHONUTF8"], "1")
            self.assertEqual(
                result["SKILL_DIR"],
                "/immutable/exact-skill",
            )
            self.assertFalse((Path("/tmp") / "chatds-browser-worker-123").exists())

    def test_worker_environment_rejects_symlinked_lease_directory(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            home = root / "home"
            temporary = root / "tmp"
            home.mkdir(mode=0o700)
            temporary.mkdir(mode=0o700)
            linked = root / "linked"
            linked.symlink_to(temporary, target_is_directory=True)
            with (
                mock.patch.object(runtime_exec, "WORKER_UID", os.geteuid()),
                mock.patch.dict(
                    os.environ,
                    {"HOME": str(home), "TMPDIR": str(linked)},
                    clear=True,
                ),
                self.assertRaises(LaunchError),
            ):
                runtime_exec.worker_environment(
                    ProxyPolicy("lease", "http://proxy:3128"),
                    process_id=123,
                )

    def test_helper_sources_compile(self):
        for source in (RUNTIME_ROOT / "chatds_browser_runtime").glob("*.py"):
            compile(source.read_text(), str(source), "exec")

    def test_weston_uses_private_lease_wayland_socket_only(self):
        source = (
            RUNTIME_ROOT / "chatds_browser_runtime/runtime_exec.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"--backend=headless-backend.so"', source)
        self.assertIn('f"--socket={socket_name}"', source)
        self.assertIn('"WAYLAND_DISPLAY"', source)
        self.assertIn('"XDG_SESSION_TYPE"', source)
        self.assertNotIn("/tmp/.X11-unix", source)
        self.assertNotIn("XAUTHORITY", source.split("def _start_weston", 1)[0])
        self.assertNotIn("xvfb-run", source)
        self.assertNotIn("_start_xvfb", source)

    def test_health_subprocess_preserves_only_private_runtime_roots(self):
        observed = {}

        def fake_run(command, **kwargs):
            observed.update(kwargs["env"])
            return type("Completed", (), {"stdout": "ok"})()

        with (
            mock.patch.dict(
                os.environ,
                {
                    "HOME": "/lease/home",
                    "TMPDIR": "/lease/tmp",
                    "DISPLAY": ":99",
                    "XAUTHORITY": "/secret",
                    "UNRELATED_SECRET": "secret",
                },
                clear=True,
            ),
            mock.patch("subprocess.run", side_effect=fake_run),
        ):
            self.assertEqual(_run(["fixed"]), "ok")
        self.assertEqual(observed["HOME"], "/lease/home")
        self.assertEqual(observed["TMPDIR"], "/lease/tmp")
        self.assertNotIn("DISPLAY", observed)
        self.assertNotIn("XAUTHORITY", observed)
        self.assertNotIn("UNRELATED_SECRET", observed)

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process groups")
    def test_cleanup_kills_group_descendant_after_leader_exits(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os,time\n"
                    "child=os.fork()\n"
                    "if child:\n"
                    " print(child, flush=True)\n"
                    " os._exit(0)\n"
                    "time.sleep(60)\n"
                ),
            ],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        assert process.stdout is not None
        descendant = int(process.stdout.readline())
        process.stdout.close()
        process.wait(timeout=5)
        try:
            runtime_exec._stop_process_group(process)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{descendant}/stat").read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    break
                if state == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail("process-group descendant survived wrapper cleanup")
        finally:
            try:
                os.kill(descendant, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_real_node_smokes_cover_commonjs_and_esm_resolution(self):
        commonjs = (
            RUNTIME_ROOT / "smoke/node_playwright.cjs"
        ).read_text(encoding="utf-8")
        esm = (
            RUNTIME_ROOT / "smoke/node_playwright.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn("require(\"playwright\")", commonjs)
        self.assertIn('from "playwright"', esm)
        self.assertIn("headless: false", commonjs)
        self.assertIn("headless: false", esm)

    def test_python_smoke_is_headed_for_playwright_and_selenium(self):
        source = (
            RUNTIME_ROOT / "smoke/python_browsers.py"
        ).read_text(encoding="utf-8")
        self.assertIn("headless=False", source)
        self.assertNotIn("--headless", source)

    def test_persistent_smoke_exposes_public_class_and_factory(self):
        source_path = RUNTIME_ROOT / "smoke/persistent_browser.py"
        source = source_path.read_text(encoding="utf-8")
        compiled = compile(source, str(source_path), "exec")
        self.assertIsNotNone(compiled)
        self.assertIn("class BrowserProbe:", source)
        self.assertIn("def open_browser_probe(", source)
        self.assertIn("headless=False", source)
        self.assertNotIn("--headless", source)


if __name__ == "__main__":
    unittest.main()
