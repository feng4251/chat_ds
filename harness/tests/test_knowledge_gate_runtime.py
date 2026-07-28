import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import knowledge_gate_runtime as runtime
from agent_loop import _resolve_declared_tool_selector
from knowledge_gate import (
    compile_symbolic_knowledge_gate,
    is_canonical_knowledge_gate_identifier,
)
from knowledge_gate_runtime import (
    KnowledgeGateCompileError,
    activated_knowledge_gate_candidate_authority,
    canonical_json_sha256,
    compile_runtime_knowledge_gate_plan,
    decision_tool_schema,
    validate_knowledge_gate_candidate_authority,
    validate_knowledge_gate_decisions,
)
from tools.context import ToolContext
from tools.delegation import (
    _exact_knowledge_gate_candidate_grants,
    _strict_knowledge_gate_plan,
)
from tools.isolated_skill_executor import snapshot_skill_package
from tools.knowledge_gate import (
    SUBMIT_KNOWLEDGE_GATE_DECISIONS_SCHEMA,
    submit_knowledge_gate_decisions,
)
from skills.command_grants import (
    command_grant_from_selector,
    grant_tuple,
    scope_command_grant,
)


OWNER_SKILL = "generic-workflow"
ADAPTER_SKILL = "catalog-adapter"


def _group(*selectors: str) -> dict:
    return {
        "mode": "one_of",
        "selectors": list(selectors),
    }


def _branch(outcome: str, *groups: dict, action: str = "") -> dict:
    return {
        "outcome": outcome,
        "action": action,
        "selector_groups": list(groups),
    }


def _check(check_id: str, *branches: dict) -> dict:
    return {
        "id": check_id,
        "question": f"Is {check_id} evidence sufficient?",
        "branches": list(branches),
    }


def _symbolic(*checks: dict) -> dict:
    digest_projection = {
        "schema_version": 1,
        "source_file": "orchestration/workers/evidence.yaml",
        "checks": list(checks),
        "skill_refs": [],
        "local_resources": [],
        "resource_expansions": [],
    }
    return {
        **digest_projection,
        "valid": True,
        "diagnostic_summary": {
            "error_count": 0,
            "warning_count": 0,
        },
        "ir_sha256": canonical_json_sha256(digest_projection),
    }


class KnowledgeGateRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        base = Path(self.temp_dir.name)
        self.owner_root = base / "owner"
        self.adapter_root = base / "adapter"
        self.owner_root.mkdir()
        self.adapter_root.mkdir()
        owner_main = self.owner_root / "SKILL.md"
        adapter_main = self.adapter_root / "SKILL.md"
        owner_main.write_text(
            "---\n"
            f"name: {OWNER_SKILL}\n"
            "description: Synthetic workflow fixture.\n"
            "---\n",
            encoding="utf-8",
        )
        adapter_main.write_text(
            "---\n"
            f"name: {ADAPTER_SKILL}\n"
            "description: Synthetic adapter fixture.\n"
            "---\n",
            encoding="utf-8",
        )
        self.loaded_packages = {
            OWNER_SKILL: {
                "skill_dir": str(self.owner_root),
                "skill_md_sha256": hashlib.sha256(
                    owner_main.read_bytes()
                ).hexdigest(),
                "workflow_contract": {},
            },
            ADAPTER_SKILL: {
                "skill_dir": str(self.adapter_root),
                "skill_md_sha256": hashlib.sha256(
                    adapter_main.read_bytes()
                ).hexdigest(),
                "workflow_contract": {},
            },
        }

    def _compile(
        self,
        symbolic_ir: dict,
        *,
        available_tools=(),
        loaded_packages=None,
        allowed_resources=(),
        allowed_scripts=(),
        process_only_scripts=(),
        allowed_package_digests=None,
        allowed_commands=(),
        allowed_http_get=(),
        allowed_http_post=(),
        frozen_mcp_catalog=None,
        resolve_tool_selector=_resolve_declared_tool_selector,
        worker_id="worker-evidence",
    ):
        packages = (
            self.loaded_packages
            if loaded_packages is None
            else loaded_packages
        )
        if allowed_package_digests is None:
            allowed_package_digests = {
                (
                    skill_name,
                    snapshot_skill_package(Path(loaded["skill_dir"])).sha256,
                )
                for skill_name, loaded in packages.items()
                if (
                    isinstance(loaded, dict)
                    and isinstance(loaded.get("skill_dir"), str)
                )
            }
        return compile_runtime_knowledge_gate_plan(
            symbolic_ir,
            worker_id=worker_id,
            owner_skill=OWNER_SKILL,
            available_tools=available_tools,
            loaded_packages=packages,
            allowed_resources=allowed_resources,
            allowed_scripts=allowed_scripts,
            process_only_scripts=process_only_scripts,
            allowed_package_digests=allowed_package_digests,
            allowed_commands=allowed_commands,
            allowed_http_get=allowed_http_get,
            allowed_http_post=allowed_http_post,
            frozen_mcp_catalog=frozen_mcp_catalog,
            resolve_tool_selector=resolve_tool_selector,
        )

    def test_unicode_symbolic_ids_reach_strict_delegated_boundary(self):
        worker_id = "_研究员"
        symbolic = compile_symbolic_knowledge_gate(
            {
                "checks": [{
                    "id": "_证据检查",
                    "question": "是否需要外部证据？",
                    "if_yes": {
                        "action": "检索一个精确来源。",
                        "tool_groups": [{
                            "id": "_来源组",
                            "any_of": ["web_search"],
                        }],
                    },
                }],
            },
            skill_dir=self.owner_root,
            source_file="orchestration/workers/研究员.yaml",
            worker_id=worker_id,
        )
        self.assertTrue(symbolic.ir["valid"])

        plan, digest = self._compile(
            symbolic.ir,
            worker_id=worker_id,
            available_tools={"web_search"},
        )
        assert plan is not None and digest is not None
        self.assertEqual("_证据检查", plan["checks"][0]["id"])
        self.assertTrue(is_canonical_knowledge_gate_identifier(worker_id))
        self.assertTrue(all(
            is_canonical_knowledge_gate_identifier(group["id"])
            for group in plan["groups"]
        ))
        self.assertTrue(all(
            is_canonical_knowledge_gate_identifier(
                candidate["candidate_id"]
            )
            for candidate in plan["candidates"]
        ))

        strict_plan, strict_digest, strict_error = (
            _strict_knowledge_gate_plan({
                "skill_name": OWNER_SKILL,
                "worker_id": worker_id,
                "knowledge_gate_plan": plan,
                "knowledge_gate_plan_sha256": digest,
            })
        )
        self.assertIsNone(strict_error)
        self.assertEqual(plan, strict_plan)
        self.assertEqual(digest, strict_digest)

    def test_exact_native_and_cross_client_aliases_stay_exact(self):
        plan, _digest = self._compile(
            _symbolic(_check(
                "native-tools",
                _branch(
                    "yes",
                    _group("web_search"),
                    _group("Read"),
                    _group("run_skill_python"),
                ),
            )),
            available_tools={
                "web_search",
                "web_search_news",
                "read_file",
                "run_skill_python",
                "skill_view",
            },
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        candidates = {
            candidate["tool_name"]: candidate
            for candidate in plan["candidates"]
        }
        self.assertEqual({"web_search", "read_file"}, set(candidates))
        self.assertEqual("native_tool", candidates["web_search"]["kind"])
        self.assertEqual(
            ["web_search"],
            candidates["web_search"]["tool_names"],
        )
        self.assertNotIn("web_search_news", candidates)
        # The Read alias must not turn the non-action Skill reader into an
        # ambient native candidate.
        self.assertNotIn("skill_view", candidates)
        # Exact-grant bridge tools are candidates only through a skill:<name>
        # selector backed by a compiled script/HTTP/command grant.
        bridge_group = plan["groups"][2]
        self.assertEqual([], bridge_group["candidate_ids"])
        self.assertEqual(
            ["run_skill_python"],
            bridge_group["unresolved_selectors"],
        )

    def test_skill_selector_compiles_exact_script_command_and_http_routes(self):
        script = self.adapter_root / "scripts" / "query.py"
        script.parent.mkdir()
        script.write_text(
            "def query(term: str):\n"
            "    return {\"term\": term}\n",
            encoding="utf-8",
        )
        script_sha = hashlib.sha256(script.read_bytes()).hexdigest()
        package_sha = snapshot_skill_package(self.adapter_root).sha256
        skill_main_sha = self.loaded_packages[ADAPTER_SKILL][
            "skill_md_sha256"
        ]
        get_prefix = "https://catalog.invalid/api/search"
        post_prefix = "https://catalog.invalid/graphql"
        plan, _digest = self._compile(
            _symbolic(_check(
                "adapter",
                _branch("yes", _group(f"skill:{ADAPTER_SKILL}")),
            )),
            available_tools={
                "run_skill_script",
                "run_skill_python",
                "run_declared_command",
                "skill_http_get",
                "skill_http_post_json",
            },
            allowed_scripts={
                (ADAPTER_SKILL, "scripts/query.py", script_sha),
            },
            allowed_commands={
                (
                    ADAPTER_SKILL,
                    "query-cli",
                    "python",
                    ("-m", "catalog_client"),
                ),
            },
            allowed_http_get={(ADAPTER_SKILL, get_prefix)},
            allowed_http_post={(ADAPTER_SKILL, post_prefix)},
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        candidates = plan["candidates"]
        self.assertEqual(
            {
                "skill_script",
                "declared_command",
                "skill_http_prefix",
            },
            {candidate["kind"] for candidate in candidates},
        )
        script = next(
            candidate
            for candidate in candidates
            if candidate["kind"] == "skill_script"
        )
        self.assertEqual("scripts/query.py", script["resource_path"])
        self.assertEqual(script_sha, script["sha256"])
        self.assertEqual(package_sha, script["package_sha256"])
        self.assertEqual(
            ["run_skill_script", "run_skill_python"],
            script["tool_names"],
        )
        command = next(
            candidate
            for candidate in candidates
            if candidate["kind"] == "declared_command"
        )
        self.assertEqual("query-cli", command["command_id"])
        self.assertEqual("python", command["executable"])
        self.assertEqual(["-m", "catalog_client"], command["fixed_argv"])
        self.assertIs(True, command["additional_argv"])
        http_candidates = {
            candidate["http_method"]: candidate
            for candidate in candidates
            if candidate["kind"] == "skill_http_prefix"
        }
        self.assertEqual(get_prefix, http_candidates["GET"]["url_prefix"])
        self.assertEqual(
            ["skill_http_get"],
            http_candidates["GET"]["tool_names"],
        )
        self.assertEqual(
            post_prefix,
            http_candidates["POST JSON"]["url_prefix"],
        )
        self.assertEqual(
            ["skill_http_post_json"],
            http_candidates["POST JSON"]["tool_names"],
        )
        group = plan["groups"][0]
        self.assertEqual(
            {candidate["candidate_id"] for candidate in candidates},
            set(group["candidate_ids"]),
        )
        for candidate in candidates:
            self.assertEqual(skill_main_sha, candidate["skill_md_sha256"])
            self.assertEqual(package_sha, candidate["package_sha256"])

    def test_worker_scoped_bash_selector_binds_only_exact_command_grant(self):
        selector = "Bash(python:*)"
        unscoped = command_grant_from_selector(selector)
        scoped = scope_command_grant(
            unscoped,
            "worker:worker-evidence",
        )
        self.assertIsNotNone(scoped)
        assert scoped is not None
        exact_grant = grant_tuple(OWNER_SKILL, scoped)
        symbolic = _symbolic(_check(
            "worker-command",
            _branch("yes", _group(selector)),
        ))

        unresolved, _ = self._compile(
            symbolic,
            available_tools={"run_declared_command"},
            allowed_commands=(),
        )
        assert unresolved is not None
        self.assertEqual([], unresolved["candidates"])
        self.assertEqual(
            [selector],
            unresolved["groups"][0]["unresolved_selectors"],
        )

        plan, _ = self._compile(
            symbolic,
            available_tools={"run_declared_command"},
            allowed_commands={exact_grant},
        )
        assert plan is not None
        self.assertEqual(1, len(plan["candidates"]))
        candidate = plan["candidates"][0]
        self.assertEqual("declared_command", candidate["kind"])
        self.assertEqual("run_declared_command", candidate["tool_name"])
        self.assertEqual(
            ["run_declared_command"],
            candidate["tool_names"],
        )
        self.assertEqual(scoped["id"], candidate["command_id"])
        self.assertEqual("python", candidate["executable"])
        self.assertEqual([], candidate["fixed_argv"])
        self.assertEqual(
            self.loaded_packages[OWNER_SKILL]["skill_md_sha256"],
            candidate["skill_md_sha256"],
        )
        self.assertEqual(
            snapshot_skill_package(self.owner_root).sha256,
            candidate["package_sha256"],
        )

    def test_supporting_skill_does_not_erase_native_argument_policy(self):
        loaded = copy.deepcopy(self.loaded_packages)
        loaded[ADAPTER_SKILL]["workflow_contract"] = {
            "execution_contract": {
                "environment_contract": {
                    "allowed_tools": ["WebSearch(domain:foo)"],
                },
            },
        }
        plan, _ = self._compile(
            _symbolic(_check(
                "support-policy",
                _branch("yes", _group(f"skill:{ADAPTER_SKILL}")),
            )),
            available_tools={"web_search"},
            loaded_packages=loaded,
        )

        assert plan is not None
        self.assertEqual([], plan["candidates"])
        self.assertEqual(
            [f"skill:{ADAPTER_SKILL}"],
            plan["groups"][0]["unresolved_selectors"],
        )

    def test_supporting_bash_selector_requires_exact_package_command_grant(self):
        selector = "Bash(python:*)"
        unscoped = command_grant_from_selector(selector)
        scoped = scope_command_grant(unscoped, "package")
        self.assertIsNotNone(scoped)
        assert scoped is not None
        exact_grant = grant_tuple(ADAPTER_SKILL, scoped)
        loaded = copy.deepcopy(self.loaded_packages)
        loaded[ADAPTER_SKILL]["workflow_contract"] = {
            "execution_contract": {
                "environment_contract": {
                    "allowed_tools": [selector],
                },
            },
        }
        symbolic = _symbolic(_check(
            "support-command",
            _branch("yes", _group(f"skill:{ADAPTER_SKILL}")),
        ))

        unresolved, _ = self._compile(
            symbolic,
            available_tools={"run_declared_command"},
            loaded_packages=loaded,
            allowed_commands=(),
        )
        assert unresolved is not None
        self.assertEqual([], unresolved["candidates"])

        plan, _ = self._compile(
            symbolic,
            available_tools={"run_declared_command"},
            loaded_packages=loaded,
            allowed_commands={exact_grant},
        )
        assert plan is not None
        self.assertEqual(1, len(plan["candidates"]))
        candidate = plan["candidates"][0]
        self.assertEqual("declared_command", candidate["kind"])
        self.assertEqual(scoped["id"], candidate["command_id"])
        self.assertEqual("python", candidate["executable"])
        self.assertEqual([], candidate["fixed_argv"])
        self.assertEqual(
            ["run_declared_command"],
            candidate["tool_names"],
        )
        self.assertNotEqual("native_tool", candidate["kind"])

    def test_resource_candidate_is_bound_to_current_file_sha(self):
        resource = self.owner_root / "references" / "evidence.md"
        resource.parent.mkdir()
        resource.write_text("bounded evidence\n", encoding="utf-8")
        expected_sha = hashlib.sha256(resource.read_bytes()).hexdigest()
        expected_snapshot = snapshot_skill_package(self.owner_root)
        expected_main_sha = self.loaded_packages[OWNER_SKILL][
            "skill_md_sha256"
        ]

        plan, digest = self._compile(
            _symbolic(_check(
                "local-evidence",
                _branch("yes", _group("references/evidence.md")),
            )),
            available_tools={"skill_view"},
            allowed_resources={
                (OWNER_SKILL, "references/evidence.md"),
            },
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        candidate = plan["candidates"][0]
        self.assertEqual("skill_resource", candidate["kind"])
        self.assertEqual(OWNER_SKILL, candidate["skill_name"])
        self.assertEqual("references/evidence.md", candidate["resource_path"])
        self.assertEqual(expected_sha, candidate["sha256"])
        self.assertEqual([], candidate["tool_names"])
        self.assertEqual(expected_main_sha, candidate["skill_md_sha256"])
        self.assertEqual(
            expected_snapshot.sha256,
            candidate["package_sha256"],
        )

        resource.write_text("changed evidence\n", encoding="utf-8")
        changed_plan, changed_digest = self._compile(
            _symbolic(_check(
                "local-evidence",
                _branch("yes", _group("references/evidence.md")),
            )),
            available_tools={"skill_view"},
            allowed_resources={
                (OWNER_SKILL, "references/evidence.md"),
            },
        )
        assert changed_plan is not None
        self.assertNotEqual(
            candidate["sha256"],
            changed_plan["candidates"][0]["sha256"],
        )
        self.assertNotEqual(digest, changed_digest)

        unauthorized_plan, _ = self._compile(
            _symbolic(_check(
                "local-evidence",
                _branch("yes", _group("references/evidence.md")),
            )),
            available_tools={"skill_view"},
            allowed_resources={
                (OWNER_SKILL, "references/evidence.md"),
            },
            allowed_package_digests=(),
        )
        assert unauthorized_plan is not None
        self.assertEqual([], unauthorized_plan["candidates"])
        self.assertEqual(
            ["references/evidence.md"],
            unauthorized_plan["groups"][0]["unresolved_selectors"],
        )

    def test_resource_runtime_plan_reaches_exact_strict_authority_bundle(self):
        resource_path = "references/gate.md"
        resource = self.owner_root / resource_path
        resource.parent.mkdir()
        resource.write_text("runtime-bound evidence\n", encoding="utf-8")
        snapshot = snapshot_skill_package(self.owner_root)
        plan, digest = self._compile(
            _symbolic(_check(
                "resource-authority",
                _branch("yes", _group(resource_path)),
            )),
            available_tools={"skill_view"},
            allowed_resources={(OWNER_SKILL, resource_path)},
        )
        assert plan is not None and digest is not None
        candidate = plan["candidates"][0]

        strict_plan, strict_digest, strict_error = (
            _strict_knowledge_gate_plan({
                "skill_name": OWNER_SKILL,
                "worker_id": "worker-evidence",
                "knowledge_gate_plan": plan,
                "knowledge_gate_plan_sha256": digest,
            })
        )
        self.assertIsNone(strict_error)
        self.assertEqual(plan, strict_plan)
        self.assertEqual(digest, strict_digest)
        self.assertEqual([], candidate["tool_names"])

        forged_plan = copy.deepcopy(plan)
        forged_plan["candidates"][0].pop("package_sha256")
        _normalized, _forged_digest, identity_error = (
            _strict_knowledge_gate_plan({
                "skill_name": OWNER_SKILL,
                "worker_id": "worker-evidence",
                "knowledge_gate_plan": forged_plan,
                "knowledge_gate_plan_sha256": canonical_json_sha256(
                    forged_plan
                ),
            })
        )
        self.assertIn(
            "must bind both skill_md_sha256 and package_sha256",
            identity_error,
        )

        context = ToolContext(
            user_id="user",
            session_id="session",
            enabled_tools=("skill_view",),
            enabled_user_skills=(OWNER_SKILL,),
            skill_execution_resource_boundary=True,
            allowed_skill_resources=(
                (OWNER_SKILL, "SKILL.md"),
                (OWNER_SKILL, resource_path),
            ),
            allowed_skill_package_digests=(
                (OWNER_SKILL, snapshot.sha256),
            ),
        )
        with patch(
            "skills.scanner.resolve_skill_path",
            return_value=self.owner_root / "SKILL.md",
        ):
            authority, authority_error = (
                _exact_knowledge_gate_candidate_grants(
                    strict_plan,
                    context=context,
                )
            )
        self.assertIsNone(authority_error)
        self.assertEqual(
            [(OWNER_SKILL, resource_path)],
            authority["resource_grants"],
        )
        self.assertEqual(
            [(OWNER_SKILL, snapshot.sha256)],
            authority["package_grants"],
        )
        self.assertEqual(["skill_view"], authority["tool_names"])
        self.assertEqual([candidate], authority["receipt_bindings"])
        self.assertEqual(
            authority,
            validate_knowledge_gate_candidate_authority(
                strict_plan,
                authority,
            ),
        )

        decision = validate_knowledge_gate_decisions(
            strict_plan,
            expected_sha256=strict_digest,
            supplied_sha256=strict_digest,
            decisions=[{
                "check_id": "resource-authority",
                "outcome": "yes",
                "reason": "The declared package resource is required.",
            }],
        )
        self.assertEqual("accepted", decision["status"])
        self.assertEqual(
            authority,
            activated_knowledge_gate_candidate_authority(
                strict_plan,
                decision,
                authority,
            ),
        )

        context_without_package = ToolContext(
            user_id="user",
            session_id="session",
            enabled_tools=("skill_view",),
            enabled_user_skills=(OWNER_SKILL,),
            skill_execution_resource_boundary=True,
            allowed_skill_resources=(
                (OWNER_SKILL, "SKILL.md"),
                (OWNER_SKILL, resource_path),
            ),
        )
        with patch(
            "skills.scanner.resolve_skill_path",
            return_value=self.owner_root / "SKILL.md",
        ):
            empty_authority, package_error = (
                _exact_knowledge_gate_candidate_grants(
                    strict_plan,
                    context=context_without_package,
                )
            )
        self.assertEqual([], empty_authority["receipt_bindings"])
        self.assertIn(
            "package digest is outside the parent grant",
            package_error,
        )

    def test_mcp_candidate_carries_frozen_descriptor_identity(self):
        descriptor = SimpleNamespace(
            schema_sha256="a" * 64,
            descriptor_sha256="b" * 64,
        )
        plan, _digest = self._compile(
            _symbolic(_check(
                "remote-adapter",
                _branch("yes", _group("mcp_catalog_lookup")),
            )),
            available_tools={"mcp_catalog_lookup"},
            frozen_mcp_catalog={"mcp_catalog_lookup": descriptor},
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        candidate = plan["candidates"][0]
        self.assertEqual("mcp_tool", candidate["kind"])
        self.assertEqual("mcp_catalog_lookup", candidate["tool_name"])
        self.assertEqual("a" * 64, candidate["schema_sha256"])
        self.assertEqual("b" * 64, candidate["descriptor_sha256"])

        unresolved_plan, _ = self._compile(
            _symbolic(_check(
                "remote-adapter",
                _branch("yes", _group("mcp_catalog_lookup")),
            )),
            available_tools={"mcp_catalog_lookup"},
            frozen_mcp_catalog={},
        )
        assert unresolved_plan is not None
        self.assertEqual([], unresolved_plan["candidates"])
        self.assertEqual(
            ["mcp_catalog_lookup"],
            unresolved_plan["groups"][0]["unresolved_selectors"],
        )

    def test_plan_digest_is_stable_across_mapping_and_grant_order(self):
        scripts = self.adapter_root / "scripts"
        scripts.mkdir()
        first_path = scripts / "a.py"
        second_path = scripts / "b.py"
        first_path.write_text("def first():\n    return 1\n", encoding="utf-8")
        second_path.write_text("def second():\n    return 2\n", encoding="utf-8")
        first_script = (
            ADAPTER_SKILL,
            "scripts/a.py",
            hashlib.sha256(first_path.read_bytes()).hexdigest(),
        )
        second_script = (
            ADAPTER_SKILL,
            "scripts/b.py",
            hashlib.sha256(second_path.read_bytes()).hexdigest(),
        )
        symbolic = _symbolic(_check(
            "stable",
            _branch("yes", _group(f"skill:{ADAPTER_SKILL}")),
        ))
        reordered_symbolic = dict(reversed([
            (key, copy.deepcopy(value))
            for key, value in symbolic.items()
        ]))
        plan_a, digest_a = self._compile(
            symbolic,
            available_tools=["run_skill_python", "run_skill_script"],
            allowed_scripts=[second_script, first_script],
        )
        plan_b, digest_b = self._compile(
            reordered_symbolic,
            available_tools=["run_skill_script", "run_skill_python"],
            allowed_scripts=[first_script, second_script],
        )

        self.assertEqual(plan_a, plan_b)
        self.assertEqual(digest_a, digest_b)
        self.assertEqual(digest_a, canonical_json_sha256(plan_a))
        self.assertEqual(
            canonical_json_sha256({"b": 2, "a": 1}),
            canonical_json_sha256({"a": 1, "b": 2}),
        )

    def test_each_decision_outcome_activates_only_its_declared_branch(self):
        plan, digest = self._compile(
            _symbolic(_check(
                "sufficiency",
                _branch("yes", _group("web_search")),
                _branch("no", _group("read_file")),
                _branch("unknown", _group("write_file")),
            )),
            available_tools={"web_search", "read_file", "write_file"},
        )
        assert plan is not None and digest is not None
        branch_groups = {
            branch["outcome"]: branch["group_ids"]
            for branch in plan["checks"][0]["branches"]
        }
        expected_tools = {
            "yes": [["web_search"]],
            "no": [["read_file"]],
            "unknown": [["write_file"]],
        }

        for outcome in ("yes", "no", "unknown"):
            with self.subTest(outcome=outcome):
                result = validate_knowledge_gate_decisions(
                    plan,
                    expected_sha256=digest,
                    supplied_sha256=digest,
                    decisions=[{
                        "check_id": "sufficiency",
                        "outcome": outcome,
                        "reason": f"bounded reason for {outcome}",
                    }],
                )
                self.assertEqual("accepted", result["status"])
                self.assertEqual(
                    branch_groups[outcome],
                    result["activated_group_ids"],
                )
                self.assertEqual(
                    expected_tools[outcome],
                    result["required_tool_groups"],
                )
                self.assertEqual(
                    ["sufficiency"] if outcome == "unknown" else [],
                    result["unknown_check_ids"],
                )
        rejected = validate_knowledge_gate_decisions(
            plan,
            expected_sha256=digest,
            supplied_sha256=digest,
            decisions=[{
                "check_id": "sufficiency",
                "outcome": "not_applicable",
                "reason": "Legacy outcome must not bypass a compiled branch.",
            }],
        )
        self.assertEqual("error", rejected["status"])
        self.assertEqual(
            "knowledge_gate_decisions_invalid",
            rejected["error_code"],
        )

    def test_candidates_are_or_within_group_but_groups_remain_independent(self):
        plan, digest = self._compile(
            _symbolic(_check(
                "multi-source",
                _branch(
                    "yes",
                    _group("web_search", "read_file"),
                    _group("write_file"),
                ),
            )),
            available_tools={"web_search", "read_file", "write_file"},
        )
        assert plan is not None and digest is not None
        branch = plan["checks"][0]["branches"][0]
        group_by_id = {group["id"]: group for group in plan["groups"]}
        first_group = group_by_id[branch["group_ids"][0]]
        second_group = group_by_id[branch["group_ids"][1]]
        self.assertEqual(2, len(first_group["candidate_ids"]))
        self.assertEqual(1, len(second_group["candidate_ids"]))
        self.assertTrue(
            set(first_group["candidate_ids"]).isdisjoint(
                second_group["candidate_ids"]
            )
        )

        result = validate_knowledge_gate_decisions(
            plan,
            expected_sha256=digest,
            supplied_sha256=digest,
            decisions=[{
                "check_id": "multi-source",
                "outcome": "yes",
                "reason": "The branch needs two independent evidence groups.",
            }],
        )
        self.assertEqual(branch["group_ids"], result["activated_group_ids"])
        self.assertEqual(
            [["web_search", "read_file"], ["write_file"]],
            result["required_tool_groups"],
        )

    def test_only_wholly_unresolved_groups_are_reported_unresolved(self):
        plan, digest = self._compile(
            _symbolic(_check(
                "gaps",
                _branch(
                    "yes",
                    _group("missing_one", "missing_two"),
                    _group("web_search", "missing_alternative"),
                ),
            )),
            available_tools={"web_search"},
        )
        assert plan is not None and digest is not None
        first_group, second_group = plan["groups"]
        self.assertEqual([], first_group["candidate_ids"])
        self.assertEqual(
            ["missing_one", "missing_two"],
            first_group["unresolved_selectors"],
        )
        self.assertEqual(1, len(second_group["candidate_ids"]))
        self.assertEqual(
            ["missing_alternative"],
            second_group["unresolved_selectors"],
        )

        result = validate_knowledge_gate_decisions(
            plan,
            expected_sha256=digest,
            supplied_sha256=digest,
            decisions=[{
                "check_id": "gaps",
                "outcome": "yes",
                "reason": "One OR group has no executable candidate.",
            }],
        )
        self.assertEqual(
            [first_group["id"]],
            result["unresolved_group_ids"],
        )
        self.assertEqual([[], ["web_search"]], result["required_tool_groups"])

    async def test_control_tool_uses_context_plan_and_rejects_forged_digest(self):
        plan, digest = self._compile(
            _symbolic(_check(
                "context-bound",
                _branch("yes", _group("web_search")),
            )),
            available_tools={"web_search"},
        )
        assert plan is not None and digest is not None
        context = ToolContext(
            user_id="user",
            session_id="session",
            knowledge_gate_plan=plan,
            knowledge_gate_plan_sha256=digest,
        )
        decisions = [{
            "check_id": "context-bound",
            "outcome": "yes",
            "reason": "The exact candidate is available.",
        }]

        accepted = json.loads(await submit_knowledge_gate_decisions(
            digest,
            decisions,
            context=context,
        ))
        forged = json.loads(await submit_knowledge_gate_decisions(
            "f" * 64,
            decisions,
            context=context,
        ))

        self.assertEqual("accepted", accepted["status"])
        self.assertEqual(
            "knowledge_gate_plan_identity_mismatch",
            forged["error_code"],
        )
        self.assertEqual(digest, forged["expected_plan_sha256"])

    def test_duplicate_and_incomplete_decision_sets_fail_closed(self):
        plan, digest = self._compile(
            _symbolic(
                _check("first", _branch("yes", _group("web_search"))),
                _check("second", _branch("no", _group("read_file"))),
            ),
            available_tools={"web_search", "read_file"},
        )
        assert plan is not None and digest is not None
        first = {
            "check_id": "first",
            "outcome": "yes",
            "reason": "first reason",
        }

        duplicate = validate_knowledge_gate_decisions(
            plan,
            expected_sha256=digest,
            supplied_sha256=digest,
            decisions=[first, dict(first)],
        )
        incomplete = validate_knowledge_gate_decisions(
            plan,
            expected_sha256=digest,
            supplied_sha256=digest,
            decisions=[first],
        )

        self.assertEqual(
            "knowledge_gate_decisions_invalid",
            duplicate["error_code"],
        )
        self.assertEqual(
            "knowledge_gate_decisions_incomplete",
            incomplete["error_code"],
        )
        self.assertEqual(["second"], incomplete["missing_check_ids"])

    def test_compiler_enforces_check_group_selector_and_candidate_bounds(self):
        one_check = _check("first", _branch("yes", _group("alpha")))
        two_checks = _symbolic(
            one_check,
            _check("second", _branch("yes", _group("alpha"))),
        )
        with patch.object(runtime, "MAX_GATE_CHECKS", 1):
            with self.assertRaises(KnowledgeGateCompileError) as caught:
                self._compile(two_checks, available_tools={"alpha"})
        self.assertEqual("knowledge_gate_check_limit", caught.exception.code)

        two_groups = _symbolic(_check(
            "groups",
            _branch("yes", _group("alpha"), _group("beta")),
        ))
        with patch.object(runtime, "MAX_GATE_GROUPS", 1):
            with self.assertRaises(KnowledgeGateCompileError) as caught:
                self._compile(
                    two_groups,
                    available_tools={"alpha", "beta"},
                )
        self.assertEqual("knowledge_gate_group_limit", caught.exception.code)

        too_many_selectors = _symbolic(_check(
            "selectors",
            _branch("yes", _group("alpha", "beta")),
        ))
        with patch.object(runtime, "MAX_GATE_SELECTORS_PER_GROUP", 1):
            with self.assertRaises(KnowledgeGateCompileError) as caught:
                self._compile(
                    too_many_selectors,
                    available_tools={"alpha", "beta"},
                )
        self.assertEqual("knowledge_gate_group_invalid", caught.exception.code)

        def resolve_two(_selector, _available):
            return ["alpha", "beta"]

        with patch.object(runtime, "MAX_GATE_CANDIDATES", 1):
            with self.assertRaises(KnowledgeGateCompileError) as caught:
                self._compile(
                    _symbolic(one_check),
                    available_tools={"alpha", "beta"},
                    resolve_tool_selector=resolve_two,
                )
        self.assertEqual("knowledge_gate_candidate_limit", caught.exception.code)

    def test_decision_bounds_and_dynamic_schema_are_exact(self):
        plan, digest = self._compile(
            _symbolic(
                _check("first", _branch("yes", _group("web_search"))),
                _check("second", _branch("no", _group("read_file"))),
            ),
            available_tools={"web_search", "read_file"},
        )
        assert plan is not None and digest is not None
        decisions = [
            {
                "check_id": "first",
                "outcome": "yes",
                "reason": "first reason",
            },
            {
                "check_id": "second",
                "outcome": "no",
                "reason": "second reason",
            },
        ]

        with patch.object(runtime, "MAX_GATE_CHECKS", 1):
            too_many = validate_knowledge_gate_decisions(
                plan,
                expected_sha256=digest,
                supplied_sha256=digest,
                decisions=decisions,
            )
        too_long = validate_knowledge_gate_decisions(
            plan,
            expected_sha256=digest,
            supplied_sha256=digest,
            decisions=[{
                **decisions[0],
                "reason": "x" * (runtime.MAX_GATE_DECISION_REASON_CHARS + 1),
            }, decisions[1]],
        )
        schema = decision_tool_schema(plan)["parameters"]

        self.assertEqual(
            "knowledge_gate_decisions_invalid",
            too_many["error_code"],
        )
        self.assertEqual(
            "knowledge_gate_decisions_invalid",
            too_long["error_code"],
        )
        self.assertEqual(2, schema["properties"]["decisions"]["minItems"])
        self.assertEqual(2, schema["properties"]["decisions"]["maxItems"])
        self.assertEqual(
            ["first", "second"],
            schema["properties"]["decisions"]["items"]["properties"][
                "check_id"
            ]["enum"],
        )
        self.assertEqual(
            ["yes", "no", "unknown"],
            schema["properties"]["decisions"]["items"]["properties"][
                "outcome"
            ]["enum"],
        )
        self.assertEqual(
            ["yes", "no", "unknown"],
            SUBMIT_KNOWLEDGE_GATE_DECISIONS_SCHEMA["parameters"][
                "properties"
            ]["decisions"]["items"]["properties"]["outcome"]["enum"],
        )
        self.assertEqual(
            runtime.MAX_GATE_DECISION_REASON_CHARS,
            schema["properties"]["decisions"]["items"]["properties"][
                "reason"
            ]["maxLength"],
        )

    def test_canonical_digest_rejects_non_finite_metadata(self):
        with self.assertRaises(KnowledgeGateCompileError) as caught:
            canonical_json_sha256({"not_finite": float("nan")})
        self.assertEqual(
            "knowledge_gate_noncanonical_json",
            caught.exception.code,
        )

    def test_forged_loader_symbolic_digest_fails_before_candidate_resolution(self):
        symbolic = _symbolic(_check(
            "signed-loader-ir",
            _branch("yes", _group("web_search")),
        ))
        symbolic["ir_sha256"] = "f" * 64

        with self.assertRaises(KnowledgeGateCompileError) as caught:
            self._compile(symbolic, available_tools={"web_search"})

        self.assertEqual(
            "knowledge_gate_symbolic_identity_mismatch",
            caught.exception.code,
        )


if __name__ == "__main__":
    unittest.main()
