"""Explicit execution context for session-aware agent tools.

Tool routing must never infer tenant/session identity from model arguments or
handler signatures. The runtime owns this context and passes it alongside
every dispatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Identity and routing information for one agent turn."""

    user_id: str = "default"
    session_id: str = "default"
    model_id: str = ""
    provider_config: dict[str, Any] | None = None
    fallback_configs: tuple[dict[str, Any], ...] = ()
    enabled_tools: tuple[str, ...] = ()
    source: str = "chat"
    enabled_user_skills: tuple[str, ...] = ()
    run_id: str | None = None
    # Some compatibility entry points do not assign a durable AgentRun ID.
    # This runtime-generated token still gives browser state an unforgeable,
    # per-run isolation dimension. ``dataclasses.replace`` preserves it as the
    # tool surface narrows during one run.
    browser_run_scope_id: str = field(
        default_factory=lambda: uuid.uuid4().hex,
        repr=False,
    )
    root_run_id: str | None = None
    parent_run_id: str | None = None
    agent_kind: str = "primary"
    agent_name: str | None = None
    depth: int = 0
    workspace_scope: str = "shared_session"
    event_sink: Any | None = None
    # Compiled workflow delegates receive their complete prerequisites through
    # deterministic harness preloading.  Keep the model-visible reader tools
    # useful for exact, declared re-reads while preventing them from becoming
    # an ambient browser over the parent Skill or persisted tool history.
    delegated_resource_boundary: bool = False
    # A primary turn that explicitly executes one session Skill also gets a
    # closed Skill-package capability boundary.  Unlike the delegated-child
    # boundary, it does not constrain ordinary workspace reads: it only
    # prevents the model from switching to an undeclared Skill resource or
    # executable after the exact package was selected and compiled.
    skill_execution_resource_boundary: bool = False
    allowed_skill_resources: tuple[tuple[str, str], ...] = ()
    # Primary-only progressive-disclosure roots.  A name enters this set only
    # after the exact selected Skill main has been read successfully.  Registry
    # dispatch may then revalidate one requested linked resource against the
    # current canonical manifest.  Delegates never inherit this package-browse
    # capability and continue to require explicit per-file grants.
    selected_skill_browse_roots: tuple[str, ...] = ()
    # Executable resources are a separate, exact capability.  Each entry is
    # (canonical Skill name, package-relative script path, sha256 at compile/
    # dispatch boundary).  Reading SKILL.md never grants script execution.
    allowed_skill_scripts: tuple[tuple[str, str, str], ...] = ()
    # Browser-profile scripts share the exact script ledger above because the
    # persistent process adapter reuses it, but these triples may never cross
    # a one-shot/base-executor bridge.
    process_only_skill_scripts: tuple[tuple[str, str, str], ...] = ()
    # Standard-Skill reference amendments additionally bind each script grant
    # to its complete instruction chain:
    # (skill, root_sha256, declaring_resource, declaring_sha256,
    #  script_resource, script_sha256).
    # Legacy compiler grants may leave this empty; amended grants never do.
    allowed_skill_script_authorities: tuple[
        tuple[str, str, str, str, str, str], ...
    ] = ()
    # Persistent process execution snapshots the complete Skill package.
    # Each runtime-owned entry is (canonical Skill name, canonical package
    # SHA-256).  This closes the helper-file/additional-file mutation gap left
    # by an entrypoint-only digest chain.
    allowed_skill_package_digests: tuple[tuple[str, str], ...] = ()
    # Declarative command authority is separate from script authority. Each
    # entry is (canonical Skill name, stable grant id, PATH executable,
    # immutable argv prefix). The model never supplies the executable.
    allowed_skill_commands: tuple[
        tuple[str, str, str, tuple[str, ...]], ...
    ] = ()
    # Literal HTTPS URL prefixes compiled from the exact selected/capability
    # Skill resource closure. Entries are (canonical Skill name, URL prefix).
    # This is separate from general web browsing and cannot be model-authored.
    allowed_skill_http_prefixes: tuple[tuple[str, str], ...] = ()
    # Method-level subset of the literal HTTPS ledger. A prefix enters this
    # set only when canonical Skill text explicitly declares POST/GraphQL.
    allowed_skill_http_post_prefixes: tuple[tuple[str, str], ...] = ()
    # Runtime-only network authority for exact Skill scripts and declared
    # commands.  Keep this separate from the method-level HTTP tool ledgers
    # above: selecting a sandboxed entrypoint must not make skill_http_get or
    # skill_http_post_json callable for any additional URL.
    allowed_skill_sandbox_egress_prefixes: tuple[
        tuple[str, str], ...
    ] = ()
    # Method-preserving sandbox authority. Each entry is
    # (canonical Skill name, canonical HTTP(S) URL prefix, canonical method
    # tuple). The legacy prefix ledger above remains a compatibility view only;
    # executor/proxy authorization uses these exact rules when present.
    allowed_skill_sandbox_egress_rules: tuple[
        tuple[str, str, tuple[str, ...]], ...
    ] = ()
    # Method-free exact URL identities compiled only from the bounded current
    # user context.  These are not permissions and are never projected into a
    # capability plan.  An exact Skill runner may consume them only at its
    # final dispatch boundary by proving one schema-v2 binding against the
    # actual invocation arguments.
    user_url_authorization_urls: tuple[str, ...] = ()
    # Runtime-owned HTTP(S) authority for the native browser. Each entry is
    # (canonical URL prefix, canonical method tuple). The primary compiler
    # derives this only from explicit URLs in the current original user turn;
    # selected frozen Skill rules are projected separately from the exact
    # Skill ledger at dispatch. Delegates receive only a parent-intersected
    # immutable projection and never reinterpret their model-authored task.
    allowed_browser_egress_rules: tuple[
        tuple[str, tuple[str, ...]], ...
    ] = ()
    # Exact private HTTP(S) origins compiled from the intersection of the
    # deployment allowlist and explicit URLs in this primary user turn.  This
    # never comes from Skill prose or model-authored tool arguments.
    allowed_browser_private_origins: tuple[str, ...] = ()
    # Standard Agent Skills have a free-form Markdown body.  After that body
    # is disclosed in full, the model may narrow this finite runtime-owned
    # catalog through submit_skill_capability_plan.  The catalog contains no
    # ambient filesystem/command authority and is never accepted from model
    # arguments.
    skill_capability_catalog: dict[str, Any] | None = None
    # Declarative workers may carry conditional evidence gates.  The loader
    # first compiles their syntax and the run boundary then resolves every
    # selector against frozen exact grants.  This immutable plan is available
    # only to the typed decision control tool; model arguments cannot replace
    # it or widen its candidate set.
    knowledge_gate_plan: dict[str, Any] | None = field(
        default=None,
        repr=False,
    )
    knowledge_gate_plan_sha256: str = field(default="", repr=False)
    allowed_read_paths: tuple[str, ...] = ()
    # Artifact-synthesis delegates receive a runtime-owned closed write set.
    # This is separate from the read/resource boundary: model-authored task
    # arguments can narrow the set but can never widen it.  An enabled boundary
    # with an empty tuple deliberately authorizes no direct artifact writes.
    artifact_write_boundary: bool = False
    allowed_artifact_write_patterns: tuple[str, ...] = ()
    # Runtime-only UUID derived from the current provider/native tool_call_id.
    # It is replaced immediately before a real dispatch and is never accepted
    # from model arguments or serialized into conversation history. Stateful
    # adapters use it to make a transport retry idempotent.
    tool_operation_id: str | None = field(default=None, repr=False)
    # Delegated execution authority is independent from asyncio task
    # cancellation.  The runtime binds a mutable fence plus its immutable
    # generation to every child context; dataclasses.replace preserves both
    # while narrowing tool/resource grants.  A revoked or generation-mismatched
    # child cannot cross another dispatch or commit boundary even if it catches
    # CancelledError.
    execution_fence: Any | None = field(default=None, repr=False)
    execution_fence_generation: int | None = field(default=None, repr=False)
    # Optional run-frozen MCP authority. Primary orchestration installs the
    # exact catalog shown to the parent model; delegation intersects it with
    # current live state and propagates only unchanged parent descriptors.
    # ``Any`` avoids coupling the generic tool context to the optional MCP SDK.
    # Kept at the end so older positional compatibility callers do not shift.
    frozen_mcp_catalog: Any | None = field(default=None, repr=False)
