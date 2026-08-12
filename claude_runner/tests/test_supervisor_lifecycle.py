import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from claude_runner.config import ProviderProfile, RunnerSettings
from claude_runner import server as supervisor_server
from claude_runner.server import (
    NotFound,
    RunIdentityRequest,
    RunManager,
    StartRunRequest,
    _read_json,
    _terminal_status,
    _update_status,
    _validate_runner_image_security,
    _validate_runner_image_self_test_output,
)
from claude_runner.runtime_capabilities import (
    compile_runtime_capability_contract,
    render_runtime_capability_prompt,
)


class _FakeContainer:
    def __init__(self, collection, run_id: str) -> None:
        self.collection = collection
        self.id = "container-" + run_id
        self.name = "chatds-claude-" + run_id
        self.labels = {"chatds.run_id": run_id}
        self.status = "running"
        self.removed = False
        self.kill_signals = []

    def reload(self):
        if self.removed:
            raise NotFound("removed")

    def wait(self, timeout=None):
        del timeout
        return {"StatusCode": 124 if self.kill_signals else 143}

    def stop(self, timeout=None):
        del timeout
        self.status = "exited"

    def kill(self, signal=None):
        self.kill_signals.append(signal)
        self.status = "exited"

    def remove(self, force=False):
        del force
        self.removed = True
        self.collection.remove(self)


class _FakeContainers:
    def __init__(self) -> None:
        self.values = {}
        self.last_run = None

    def run(self, image, **kwargs):
        self.last_run = {"image": image, **kwargs}
        run_id = str(kwargs["labels"]["chatds.run_id"])
        container = _FakeContainer(self, run_id)
        container.labels.update(dict(kwargs["labels"]))
        self.attach(container)
        return container

    def attach(self, container):
        self.values[container.id] = container
        self.values[container.name] = container

    def remove(self, container):
        for key, value in list(self.values.items()):
            if value is container:
                self.values.pop(key, None)

    def get(self, identity):
        container = self.values.get(identity)
        if container is None:
            raise NotFound(identity)
        return container

    def list(self, all=False, filters=None):
        del all
        unique = {id(value): value for value in self.values.values()}.values()
        rows = list(unique)
        labels = (filters or {}).get("label", [])
        if isinstance(labels, str):
            labels = [labels]
        for expression in labels:
            key, _, expected = expression.partition("=")
            rows = [row for row in rows if row.labels.get(key) == expected]
        return rows


class _FakeClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()


def _make_readonly_view(view: Path) -> str:
    descriptor = view / "plugin" / ".claude-plugin" / "plugin.json"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text(
        json.dumps({"name": "fixture", "version": "1.0.0"}),
        encoding="utf-8",
    )
    instruction = view / "plugin" / "skills" / "fixture" / "SKILL.md"
    instruction.parent.mkdir(parents=True)
    instruction.write_text(
        "---\nname: fixture\ndescription: generic lifecycle fixture\n---\n",
        encoding="utf-8",
    )
    payload = descriptor.read_bytes()
    instruction_payload = instruction.read_bytes()
    identity = {
        "schema": "chatds.claude-skill-view.v1",
        "skills": [{
            "name": "fixture",
            "scope": "session",
            "files": [{
                "path": "SKILL.md",
                "sha256": hashlib.sha256(instruction_payload).hexdigest(),
                "size": len(instruction_payload),
            }],
        }],
        "files": [
            {
                "path": "plugin/.claude-plugin/plugin.json",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            },
            {
                "path": "plugin/skills/fixture/SKILL.md",
                "sha256": hashlib.sha256(instruction_payload).hexdigest(),
                "size": len(instruction_payload),
            },
        ],
    }
    digest = hashlib.sha256(json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    (view / "manifest.json").write_text(
        json.dumps({**identity, "sha256": digest}),
        encoding="utf-8",
    )
    for walk_root, directories, files in os.walk(view, topdown=False):
        for name in files:
            os.chmod(Path(walk_root) / name, 0o444)
        for name in directories:
            os.chmod(Path(walk_root) / name, 0o555)
    os.chmod(view, 0o555)
    return digest


class SupervisorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.mkdtemp()
        self.root = Path(self.temporary)
        self.user_id = "1" * 32
        self.conversation_id = "2" * 32
        self.session = self.root / self.user_id / self.conversation_id
        self.workspace = self.session / "workspace"
        self.workspace.mkdir(parents=True)
        skill_views = self.session / "runtime" / "claude" / "skill-views"
        provisional = skill_views / "provisional"
        self.view_digest = _make_readonly_view(provisional)
        self.view = skill_views / self.view_digest
        provisional.rename(self.view)
        self.client = _FakeClient()
        self.profile = ProviderProfile(
            id="shaiengine",
            backend_base_url="https://api.shaiengine.com/v1",
            backend_protocol="openai",
            claude_base_url="https://api.shaiengine.com",
            api_key="fixture-key",
            models=frozenset({"glm-5.2"}),
            context_windows={"glm-5.2": 1_000_000},
        )
        self.settings = RunnerSettings(
            internal_token="fixture-internal-token",
            workspace_host_root=self.root,
            state_root=self.root / "supervisor-state",
            runner_image="fixture-image",
            egress_proxy_volume="egress-volume",
            workspace_lock_volume="lock-volume",
            workspace_lock_root=self.root / "locks",
            max_concurrent_runs=1,
            preflight_timeout_seconds=120,
            max_run_seconds=120,
            worker_uid=os.getuid(),
            worker_gid=os.getgid(),
            security_mode="seccomp_stripped_setid",
            egress_limits={
                "max_requests": 8192,
                "max_outbound_bytes": 64 * 1024 * 1024,
                "max_response_wire_bytes": 2 * 1024 * 1024 * 1024,
            },
            provider_profiles={"shaiengine": self.profile},
            private_origin_allowlist=(),
        )
        self.manager = RunManager(self.settings, self.client)

    def test_runner_image_attests_security_and_egress_policy_schema(self):
        image = SimpleNamespace(labels={
            "org.opencontainers.image.chatds.setid-stripped": "true",
            "org.opencontainers.image.chatds.egress-policy": (
                "signed-public-read-v1"
            ),
            "org.opencontainers.image.chatds.runner-runtime": (
                "installed-isolated-package-v1"
            ),
        })
        _validate_runner_image_security(image, "seccomp_stripped_setid")

        image.labels.pop("org.opencontainers.image.chatds.egress-policy")
        with self.assertRaisesRegex(RuntimeError, "egress_policy_attestation"):
            _validate_runner_image_security(image, "seccomp_stripped_setid")

        image.labels["org.opencontainers.image.chatds.egress-policy"] = (
            "signed-public-read-v1"
        )
        image.labels.pop("org.opencontainers.image.chatds.runner-runtime")
        with self.assertRaisesRegex(RuntimeError, "runtime_attestation"):
            _validate_runner_image_security(image, "seccomp_stripped_setid")

    def test_runner_image_self_test_receipt_is_exact_and_bounded(self):
        _validate_runner_image_self_test_output(json.dumps({
            "schema": "chatds.claude-runner-image-self-test.v1",
            "status": "ok",
            "mcp_entrypoints": 3,
            "compatibility_entrypoints": 3,
        }).encode())
        for payload in (
            b"",
            b'{"schema":"wrong","status":"ok","mcp_entrypoints":3,'
            b'"compatibility_entrypoints":3}\n',
            b'{"schema":"chatds.claude-runner-image-self-test.v1",'
            b'"status":"ok","mcp_entrypoints":2,'
            b'"compatibility_entrypoints":3}\n',
            b"not-json\n",
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RuntimeError, "self_test"):
                    _validate_runner_image_self_test_output(payload)

    async def test_missing_dynamic_runner_image_is_structured_503(self):
        class MissingImages:
            @staticmethod
            def get(_name):
                raise supervisor_server.ImageNotFound("missing")

        fake_manager = SimpleNamespace(
            client=SimpleNamespace(ping=lambda: True, images=MissingImages()),
            settings=self.settings,
        )
        with patch.object(supervisor_server, "manager", fake_manager):
            response = await supervisor_server.health()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            json.loads(response.body),
            {"status": "error", "code": "docker_or_image_unavailable"},
        )

    async def asyncTearDown(self):
        self.manager._draining = True
        for task in tuple(self.manager._tasks.values()):
            task.cancel()
        if self.manager._tasks:
            await asyncio.gather(
                *tuple(self.manager._tasks.values()), return_exceptions=True
            )
        for walk_root, directories, files in os.walk(
            self.root, topdown=False, followlinks=False
        ):
            for name in files:
                try:
                    os.chmod(Path(walk_root) / name, 0o600)
                except OSError:
                    pass
            for name in directories:
                try:
                    os.chmod(Path(walk_root) / name, 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _request(self, run_id: str | None = None) -> StartRunRequest:
        identity = run_id or uuid.uuid4().hex
        return StartRunRequest(
            run_id=identity,
            root_run_id=identity,
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            model_id="glm-5.2",
            api_model="glm-5.2",
            provider_profile="shaiengine",
            provider_base_url=self.profile.backend_base_url,
            provider_protocol="openai",
            messages=[{"role": "user", "content": "fixture"}],
            max_output_tokens=1024,
            context_window_tokens=1_000_000,
            workspace_path=str(self.workspace),
            skill_view_path=str(self.view),
            skill_view_sha256=self.view_digest,
            native_session_id=str(uuid.uuid4()),
            source="chat",
            user_turn_text="fixture",
        )

    async def test_provider_protocol_is_part_of_the_deployment_profile(self):
        request = self._request().model_copy(update={
            "provider_protocol": "anthropic",
        })
        with self.assertRaises(HTTPException):
            self.manager._provider(request)

    async def test_context_window_is_part_of_the_deployment_profile(self):
        request = self._request().model_copy(update={
            "context_window_tokens": 200_000,
        })
        with self.assertRaisesRegex(
            HTTPException,
            "context window",
        ):
            self.manager._provider(request)

    async def test_start_admission_is_idempotent_and_identity_bound(self):
        await self.manager._semaphore.acquire()
        request = self._request()
        try:
            first = await self.manager.start(request)
            task = self.manager._tasks[request.run_id]
            second = await self.manager.start(request)
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertIs(self.manager._tasks[request.run_id], task)
            conflicting = request.model_copy(update={
                "messages": [{"role": "user", "content": "renamed holdout"}],
            })
            with self.assertRaises(HTTPException):
                await self.manager.start(conflicting)
            await self.manager.cancel(
                request.run_id,
                RunIdentityRequest(
                    user_id=self.user_id,
                    conversation_id=self.conversation_id,
                ),
            )
        finally:
            self.manager._semaphore.release()

    def test_selected_skill_entrypoint_precedes_fresh_and_resumed_prompts(self):
        entrypoint = "chatds-session-skills:chatds-harness-session-entry"
        fresh = supervisor_server._build_prompt(
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "task"},
            ],
            resume=False,
            skill_entrypoint=entrypoint,
        )
        resumed = supervisor_server._build_prompt(
            [
                {"role": "user", "content": "old"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "next"},
            ],
            resume=True,
            skill_entrypoint=entrypoint,
        )
        self.assertTrue(fresh.startswith(f"/{entrypoint}\n\n<SYSTEM>"))
        self.assertEqual(resumed, f"/{entrypoint}\n\nnext")

    def test_native_skill_discovery_does_not_force_installed_skill_on_turn(self):
        manifest = {
            "plugin_name": "chatds-session-skills",
            "entrypoint_skill_name": None,
            "selected_primary_skill_names": ["museum-provenance"],
            "skills": [{
                "name": "museum-provenance",
                "scope": "session",
            }],
        }
        entrypoint = supervisor_server._manifest_skill_entrypoint(manifest)
        self.assertIsNone(entrypoint)
        prompt = supervisor_server._build_prompt(
            [{"role": "user", "content": "unrelated current weather"}],
            resume=True,
            skill_entrypoint=entrypoint,
        )
        self.assertEqual(prompt, "unrelated current weather")
        with self.assertRaisesRegex(RuntimeError, "skill_entrypoint"):
            supervisor_server._manifest_skill_entrypoint({
                **manifest,
                "selected_primary_skill_names": ["missing"],
            })

    def test_compiled_persistent_process_requirement_adds_generic_guidance(self):
        prompt = supervisor_server._attach_runtime_contract(
            "unrelated task",
            [{
                "skill_name": "renamed-holdout",
                "persistent_stdin_process": True,
            }],
        )
        self.assertTrue(prompt.startswith("unrelated task\n\n"))
        self.assertIn("chatds-process", prompt)
        self.assertIn("process_write", prompt)
        self.assertEqual(
            supervisor_server._attach_runtime_contract("plain", []),
            "plain",
        )

    def test_current_capability_contract_supersedes_stale_session_claims(self):
        contract = compile_runtime_capability_contract(
            manifest={
                "harness_egress_rules": [
                    {
                        "capability": "renamed_catalog_lookup",
                        "url_prefix": "http://catalog.internal:8090/v1/item",
                        "methods": ["GET"],
                    },
                    {
                        "capability": "web_search",
                        "url_prefix": "http://search.internal:8080/search",
                        "methods": ["GET"],
                    },
                ],
            },
            egress_policy={
                "public_read": {
                    "methods": ["GET", "HEAD"],
                    "ports": [80, 443],
                },
            },
        )
        prompt = render_runtime_capability_prompt(contract)
        self.assertEqual(
            contract["structured_capabilities"],
            ["renamed_catalog_lookup", "web_search"],
        )
        self.assertIn("supersedes", prompt)
        self.assertIn("renamed_catalog_lookup", prompt)
        self.assertIn("GET/HEAD", prompt)
        self.assertIn("80, 443", prompt)
        self.assertIn("most specific structured capability", prompt)
        self.assertIn("current tool result", prompt)

    def test_capability_contract_does_not_invent_generic_network_authority(self):
        contract = compile_runtime_capability_contract(
            manifest={
                "harness_egress_rules": [{
                    "capability": "renamed_evidence_lookup",
                    "url_prefix": "https://evidence.example.test/v2/query",
                    "methods": ["POST"],
                }],
            },
            egress_policy={"public_read": None},
        )
        prompt = render_runtime_capability_prompt(contract)
        self.assertFalse(contract["public_http_read"]["enabled"])
        self.assertIn("No generic public HTTP read grant", prompt)
        self.assertIn("renamed_evidence_lookup", prompt)

    def test_skill_entrypoint_manifest_fails_closed_when_inconsistent(self):
        valid = {
            "plugin_name": "chatds-session-skills",
            "entrypoint_skill_name": "chatds-harness-session-entry",
            "selected_primary_skill_names": ["fixture"],
            "skills": [
                {"name": "fixture", "scope": "session"},
                {
                    "name": "chatds-harness-session-entry",
                    "scope": "harness",
                    "bundle_role": "entrypoint",
                },
            ],
        }
        self.assertEqual(
            supervisor_server._manifest_skill_entrypoint(valid),
            "chatds-session-skills:chatds-harness-session-entry",
        )
        invalid = {**valid, "selected_primary_skill_names": ["missing"]}
        with self.assertRaisesRegex(RuntimeError, "skill_entrypoint"):
            supervisor_server._manifest_skill_entrypoint(invalid)

    def test_verified_manifest_carries_policy_inputs_without_second_read(self):
        receipt = supervisor_server.verify_skill_view(
            self.view, self.view_digest
        )
        with patch.object(
            Path,
            "read_text",
            side_effect=AssertionError("policy reread immutable Skill input"),
        ):
            policy = supervisor_server.compile_turn_egress_policy(
                skill_view_root=self.view,
                skill_view_sha256=self.view_digest,
                verified_skill_view=receipt,
                user_turn_text="generic fixture",
                provider_base_url=self.profile.claude_base_url,
                configured_private_origins=(),
                budget_scope_sha256="a" * 64,
                call_id_sha256="b" * 64,
                limits=dict(self.settings.egress_limits),
            )
        self.assertEqual(policy["policy_version"], 3)

    async def test_turn_container_has_one_session_mount_and_no_network(self):
        request = self._request()
        workspace, skill_view, state = self.manager._validate_paths(request)
        run_dir = state / "control" / "runs" / request.run_id
        run_dir.mkdir(parents=True)
        request_path = run_dir / "request.json"
        request_path.write_text("{}", encoding="utf-8")
        status_path = run_dir / "status.json"
        status_path.write_text(json.dumps({
            "run_id": request.run_id,
            "user_id": request.user_id,
            "conversation_id": request.conversation_id,
            "status": "starting",
            "phase": "starting",
        }), encoding="utf-8")
        with (
            patch.dict(os.environ, {
                "SKILL_EGRESS_POLICY_TOKEN": "x" * 32,
            }),
            patch.object(
                supervisor_server,
                "_seccomp_security_option",
                return_value="seccomp={}",
            ),
        ):
            container = self.manager._create_container_sync(
                request,
                workspace,
                skill_view,
                state,
                run_dir,
                request_path,
                status_path,
                self.profile,
            )
        self.assertIsNotNone(container)
        launch = self.client.containers.last_run
        self.assertEqual(launch["network_mode"], "none")
        self.assertTrue(launch["read_only"])
        self.assertEqual(launch["cap_drop"], ["ALL"])
        self.assertTrue(any(
            value.startswith("seccomp=")
            for value in launch["security_opt"]
        ))
        self.assertNotIn("no-new-privileges:true", launch["security_opt"])
        volume_sources = set(launch["volumes"])
        self.assertEqual(volume_sources, {
            str(workspace),
            str(state),
            str(skill_view),
            str(request_path),
            self.settings.egress_proxy_volume,
            self.settings.workspace_lock_volume,
        })
        self.assertNotIn(str(self.session), volume_sources)
        self.assertNotIn("/var/run/docker.sock", volume_sources)
        # docker-py 7.1 rejects Docker's low-level StopTimeout field as an
        # unexpected ``containers.run`` kwarg.  Shutdown deadlines are passed
        # explicitly to ``container.stop`` by the Supervisor instead.
        self.assertNotIn("stop_timeout", launch)
        self.assertEqual(
            launch["volumes"][str(workspace)],
            {"bind": "/workspace", "mode": "rw"},
        )

    async def test_queued_cancel_never_dispatches_container(self):
        await self.manager._semaphore.acquire()
        request = self._request()
        await self.manager.start(request)
        try:
            admission_status = self.manager._admission_dir(
                request.run_id
            ) / "status.json"
            for _ in range(100):
                if _read_json(admission_status).get("phase") == "queued":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(_read_json(admission_status)["phase"], "queued")
            self.assertTrue(await self.manager.cancel(
                request.run_id,
                RunIdentityRequest(
                    user_id=self.user_id,
                    conversation_id=self.conversation_id,
                ),
            ))
        finally:
            self.manager._semaphore.release()
        run_dir = self.manager._run_dir(
            self.user_id, self.conversation_id, request.run_id
        )
        self.assertEqual(_terminal_status(run_dir / "events.jsonl"), "cancelled")
        self.assertEqual(_read_json(run_dir / "status.json")["phase"], "terminal")
        self.assertEqual(self.client.containers.values, {})

    async def test_blocked_preflight_does_not_block_start_or_cancel(self):
        request = self._request()
        entered = threading.Event()
        release = threading.Event()
        real_verify = supervisor_server.verify_skill_view

        def blocked_verify(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return real_verify(*args, **kwargs)

        with patch.object(
            supervisor_server, "verify_skill_view", side_effect=blocked_verify
        ):
            accepted = await asyncio.wait_for(
                self.manager.start(request), timeout=0.5
            )
            self.assertEqual(accepted["phase"], "preflight")
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertTrue(await asyncio.wait_for(
                self.manager.cancel(
                    request.run_id,
                    RunIdentityRequest(
                        user_id=self.user_id,
                        conversation_id=self.conversation_id,
                    ),
                ),
                timeout=0.5,
            ))
            local_events, _admission_status, _native_events = (
                self.manager.event_paths(request.run_id)
            )
            self.assertEqual(_terminal_status(local_events), "cancelled")
            stream = supervisor_server._event_stream(
                self.manager.event_paths(request.run_id), 0
            )
            terminal_frame = await asyncio.wait_for(anext(stream), timeout=1)
            done_frame = await asyncio.wait_for(anext(stream), timeout=1)
            self.assertIn("chatds.supervisor.terminal", terminal_frame)
            self.assertEqual(done_frame, "data: [DONE]\n\n")
            task = self.manager._tasks[request.run_id]
            release.set()
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        self.assertEqual(self.client.containers.values, {})

    async def test_preflight_timeout_revokes_late_container_authority(self):
        settings = replace(self.settings, preflight_timeout_seconds=0.05)
        manager = RunManager(settings, self.client)
        request = self._request()
        entered = threading.Event()
        release = threading.Event()
        real_verify = supervisor_server.verify_skill_view

        def blocked_verify(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return real_verify(*args, **kwargs)

        try:
            with patch.object(
                supervisor_server,
                "verify_skill_view",
                side_effect=blocked_verify,
            ):
                await manager.start(request)
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                admission = manager._admission_dir(request.run_id)
                for _ in range(100):
                    if _terminal_status(admission / "events.jsonl") == "failed":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(
                    _terminal_status(admission / "events.jsonl"), "failed"
                )
                self.assertEqual(
                    _read_json(admission / "status.json")["error"],
                    "preflight_timeout",
                )
                release.set()
                await asyncio.sleep(0.1)
            self.assertEqual(self.client.containers.values, {})
        finally:
            release.set()
            manager._draining = True
            for task in tuple(manager._tasks.values()):
                task.cancel()
            if manager._tasks:
                await asyncio.gather(
                    *tuple(manager._tasks.values()), return_exceptions=True
                )
            manager._preflight_executor.shutdown(
                wait=False, cancel_futures=True
            )

    async def test_one_preflight_attestation_receipt_is_reused_by_policy(self):
        await self.manager._semaphore.acquire()
        request = self._request()
        real_verify = supervisor_server.verify_skill_view
        try:
            with patch.object(
                supervisor_server,
                "verify_skill_view",
                wraps=real_verify,
            ) as verify:
                await self.manager.start(request)
                admission_status = self.manager._admission_dir(
                    request.run_id
                ) / "status.json"
                for _ in range(100):
                    if _read_json(admission_status).get("phase") == "queued":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(_read_json(admission_status)["phase"], "queued")
                self.assertEqual(verify.call_count, 1)
                await self.manager.cancel(
                    request.run_id,
                    RunIdentityRequest(
                        user_id=self.user_id,
                        conversation_id=self.conversation_id,
                    ),
                )
        finally:
            self.manager._semaphore.release()

    async def test_session_cleanup_fences_and_revokes_queued_run(self):
        await self.manager._semaphore.acquire()
        request = self._request()
        await self.manager.start(request)
        try:
            result = await self.manager.cleanup_session(
                self.user_id, self.conversation_id
            )
        finally:
            self.manager._semaphore.release()
        self.assertTrue(result["success"])
        self.assertFalse((
            self.session / "runtime" / "claude" / "state"
        ).exists())
        with self.assertRaisesRegex(Exception, "revoked"):
            await self.manager.start(self._request())

    async def test_starting_cancel_waits_for_deterministic_container(self):
        request = self._request()
        creating = threading.Event()
        release = threading.Event()
        container = _FakeContainer(self.client.containers, request.run_id)

        def delayed_create(*args, **kwargs):
            del args, kwargs
            creating.set()
            release.wait(timeout=5)
            self.client.containers.attach(container)
            status_path = self.manager._run_dir(
                self.user_id, self.conversation_id, request.run_id
            ) / "status.json"
            _update_status(
                status_path,
                status="running",
                phase="running",
                container_id=container.id,
            )
            return container

        with patch.object(
            self.manager, "_create_container_sync", side_effect=delayed_create
        ):
            await self.manager.start(request)
            self.assertTrue(await asyncio.to_thread(creating.wait, 5))
            cancel_task = asyncio.create_task(self.manager.cancel(
                request.run_id,
                RunIdentityRequest(
                    user_id=self.user_id,
                    conversation_id=self.conversation_id,
                ),
            ))
            await asyncio.sleep(0.05)
            self.assertFalse(cancel_task.done())
            release.set()
            self.assertTrue(await asyncio.wait_for(cancel_task, timeout=5))
        run_dir = self.manager._run_dir(
            self.user_id, self.conversation_id, request.run_id
        )
        self.assertEqual(_terminal_status(run_dir / "events.jsonl"), "cancelled")
        self.assertTrue(container.removed or container.status == "exited")

    async def test_supervisor_shutdown_detaches_without_false_terminal(self):
        run_id = uuid.uuid4().hex
        run_dir = self.manager._run_dir(
            self.user_id, self.conversation_id, run_id
        )
        run_dir.mkdir(parents=True)
        status_path = run_dir / "status.json"
        status_path.write_text(json.dumps({
            "run_id": run_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "status": "running",
            "phase": "running",
            "container_id": "container-" + run_id,
        }))
        container = _FakeContainer(self.client.containers, run_id)
        self.client.containers.attach(container)
        task = asyncio.create_task(self.manager._adopt_existing_container(
            container,
            run_id=run_id,
            run_dir=run_dir,
            status_path=status_path,
            remaining_seconds=120,
        ))
        self.manager._tasks[run_id] = task
        await asyncio.sleep(0.05)
        await self.manager.detach_for_shutdown()
        self.assertFalse((run_dir / "events.jsonl").exists())
        self.assertEqual(_read_json(status_path)["status"], "running")
        self.assertFalse(container.removed)

    async def test_hard_timeout_is_failed_not_cancelled(self):
        run_id = uuid.uuid4().hex
        run_dir = self.manager._run_dir(
            self.user_id, self.conversation_id, run_id
        )
        run_dir.mkdir(parents=True)
        status_path = run_dir / "status.json"
        status_path.write_text(json.dumps({
            "run_id": run_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "status": "running",
            "phase": "running",
            "container_id": "container-" + run_id,
        }))
        container = _FakeContainer(self.client.containers, run_id)
        self.client.containers.attach(container)
        await self.manager._monitor_container(
            container,
            run_id=run_id,
            run_dir=run_dir,
            status_path=status_path,
            remaining_seconds=0,
        )
        self.assertEqual(container.kill_signals, ["SIGUSR1"])
        self.assertEqual(_terminal_status(run_dir / "events.jsonl"), "failed")
        self.assertEqual(_read_json(status_path)["status"], "failed")

    async def test_early_process_exit_preserves_stage_and_exit_code(self):
        run_id = uuid.uuid4().hex
        run_dir = self.manager._run_dir(
            self.user_id, self.conversation_id, run_id
        )
        run_dir.mkdir(parents=True)
        status_path = run_dir / "status.json"
        status_path.write_text(json.dumps({
            "run_id": run_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "status": "running",
            "phase": "running",
            "container_id": "container-" + run_id,
        }))
        container = _FakeContainer(self.client.containers, run_id)
        container.status = "exited"
        self.client.containers.attach(container)
        await self.manager._monitor_container(
            container,
            run_id=run_id,
            run_dir=run_dir,
            status_path=status_path,
            remaining_seconds=120,
        )
        envelope = json.loads(
            (run_dir / "events.jsonl").read_text(encoding="utf-8")
        )
        event = envelope["event"]
        self.assertEqual(event["error"], "runner_process_exited_before_terminal")
        self.assertEqual(event["error_stage"], "bootstrap_or_controller")
        self.assertEqual(event["exit_code"], 143)
        status = _read_json(status_path)
        self.assertEqual(status["error"], event["error"])
        self.assertEqual(status["error_stage"], event["error_stage"])

    async def test_bootstrap_terminal_is_authoritative_in_status_projection(self):
        run_id = uuid.uuid4().hex
        run_dir = self.manager._run_dir(
            self.user_id, self.conversation_id, run_id
        )
        run_dir.mkdir(parents=True)
        status_path = run_dir / "status.json"
        status_path.write_text(json.dumps({
            "run_id": run_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "status": "running",
            "phase": "running",
            "container_id": "container-" + run_id,
        }))
        (run_dir / "events.jsonl").write_text(json.dumps({
            "seq": 1,
            "channel": "bootstrap",
            "event": {
                "type": "chatds.supervisor.terminal",
                "status": "failed",
                "error": "runner_runtime_import_failed",
                "error_code": "runner_runtime_import_failed",
                "error_stage": "bootstrap_import",
                "exit_code": 70,
            },
        }) + "\n")
        container = _FakeContainer(self.client.containers, run_id)
        container.status = "exited"
        self.client.containers.attach(container)
        await self.manager._monitor_container(
            container,
            run_id=run_id,
            run_dir=run_dir,
            status_path=status_path,
            remaining_seconds=120,
        )
        self.assertEqual(
            len((run_dir / "events.jsonl").read_text().splitlines()),
            1,
        )
        status = _read_json(status_path)
        self.assertEqual(status["error"], "runner_runtime_import_failed")
        self.assertEqual(status["error_stage"], "bootstrap_import")

    async def test_durable_queued_run_is_requeued_after_restart(self):
        await self.manager._semaphore.acquire()
        request = self._request()
        await self.manager.start(request)
        admission_status = self.manager._admission_dir(
            request.run_id
        ) / "status.json"
        for _ in range(100):
            if _read_json(admission_status).get("phase") == "queued":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(_read_json(admission_status)["phase"], "queued")
        await self.manager.detach_for_shutdown()
        self.manager._semaphore.release()
        run_dir = self.manager._run_dir(
            self.user_id, self.conversation_id, request.run_id
        )
        self.assertIsNone(_terminal_status(run_dir / "events.jsonl"))

        replacement = RunManager(self.settings, self.client)
        await replacement._semaphore.acquire()
        try:
            result = await replacement.reconcile_existing_containers()
            self.assertEqual(result["requeued"], 1)
            self.assertEqual(_read_json(run_dir / "status.json")["phase"], "queued")
            self.assertIsNone(_terminal_status(run_dir / "events.jsonl"))
        finally:
            replacement._draining = True
            for task in tuple(replacement._tasks.values()):
                task.cancel()
            if replacement._tasks:
                await asyncio.gather(
                    *tuple(replacement._tasks.values()), return_exceptions=True
                )
            replacement._semaphore.release()

    async def test_durable_preflight_is_requeued_after_restart(self):
        request = self._request()
        self.manager._ensure_admission(request)
        replacement = RunManager(self.settings, self.client)
        await replacement._semaphore.acquire()
        try:
            result = await replacement.reconcile_existing_containers()
            self.assertEqual(result["requeued_preflight"], 1)
            admission_status = replacement._admission_dir(
                request.run_id
            ) / "status.json"
            for _ in range(100):
                if _read_json(admission_status).get("phase") == "queued":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(_read_json(admission_status)["phase"], "queued")
        finally:
            replacement._draining = True
            for task in tuple(replacement._tasks.values()):
                task.cancel()
            if replacement._tasks:
                await asyncio.gather(
                    *tuple(replacement._tasks.values()), return_exceptions=True
                )
            replacement._semaphore.release()


if __name__ == "__main__":
    unittest.main()
