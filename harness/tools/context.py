"""Explicit execution context for session-aware agent tools.

Tool routing must never infer tenant/session identity from model arguments or
handler signatures. The runtime owns this context and passes it alongside
every dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    # Standard Agent Skills have a free-form Markdown body.  After that body
    # is disclosed in full, the model may narrow this finite runtime-owned
    # catalog through submit_skill_capability_plan.  The catalog contains no
    # ambient filesystem/command authority and is never accepted from model
    # arguments.
    skill_capability_catalog: dict[str, Any] | None = None
    allowed_read_paths: tuple[str, ...] = ()
    # Artifact-synthesis delegates receive a runtime-owned closed write set.
    # This is separate from the read/resource boundary: model-authored task
    # arguments can narrow the set but can never widen it.  An enabled boundary
    # with an empty tuple deliberately authorizes no direct artifact writes.
    artifact_write_boundary: bool = False
    allowed_artifact_write_patterns: tuple[str, ...] = ()
