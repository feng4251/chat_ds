"""Platform-owned I/O capabilities and native-engine diagnostics.

Claude Code and DeepSeek Harness own their complete internal tool graphs.
Only deployment I/O projected as MCP/controller boundaries is selectable here;
the Web application must never emulate or narrow native file, shell, Skill,
planning, or multi-agent tools.
"""

from __future__ import annotations


PLATFORM_IO_CAPABILITIES: tuple[str, ...] = (
    "web_search",
    "market_quote",
    "cronjob",
)

PLATFORM_IO_CAPABILITY_SET = frozenset(PLATFORM_IO_CAPABILITIES)

# DeepSeek Harness owns its complete upstream model-facing tool graph.  The
# Web adapter may project this catalog for diagnostics, but must not derive it
# from the retired ChatDS capability vocabulary: group-level translation made
# a read grant accidentally expose write/edit tools and disabled native
# delegation features that a conforming Skill is entitled to use.  Filesystem
# and process authority remains bounded by the native permission preset and
# the one-Session container mount.
DEEPSEEK_HARNESS_NATIVE_TOOLS: tuple[str, ...] = (
    "bash",
    "job_output",
    "job_list",
    "job_kill",
    "read",
    "write",
    "edit",
    "glob",
    "grep",
    "str_replace_editor",
    "skill",
    "subagent",
    "subagent_fork",
    "send_message",
    "interrupt_agent",
    "list_agents",
    "workflow",
    "ralph",
    "ask_user_question",
    "todo_write",
    "get_goal",
    "create_goal",
    "update_goal",
    "web_search",
)


def deepseek_harness_native_tools() -> tuple[str, ...]:
    """Return the immutable upstream DSH tool catalog exposed by this image."""

    return DEEPSEEK_HARNESS_NATIVE_TOOLS


# A model may observe MCP-qualified platform tool names. Translate only those
# deployment-owned identities; native built-ins and foreign MCP prefixes can
# never mint platform I/O authority.
SCHEDULE_PLATFORM_CAPABILITY_ALIASES: dict[str, str] = {
    "mcp__chatds-web-search__web_search": "web_search",
    "mcp__chatds-market-data__market_quote": "market_quote",
    "mcp__chatds-schedule__schedule_create": "cronjob",
}


def canonicalize_scheduled_platform_capabilities(
    values: list[str] | None,
    *,
    allowed_capabilities: set[str] | frozenset[str],
) -> tuple[str, ...]:
    """Bind a scheduled capability subset to the current Session authority.

    ``None`` means "use the safe unattended default intersected with the
    current Session".  An explicit empty list remains empty; it must not be
    widened later by truthiness-based defaulting.
    """

    allowed = frozenset(allowed_capabilities)
    if allowed - PLATFORM_IO_CAPABILITY_SET:
        raise ValueError("schedule_control_allowed_capabilities_invalid")
    if values is None:
        return tuple(
            name for name in UNATTENDED_DEFAULT_PLATFORM_IO_CAPABILITIES
            if name in allowed
        )
    if not isinstance(values, list):
        raise ValueError("schedule_control_capabilities_invalid")
    canonical: list[str] = []
    observed: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("schedule_control_capabilities_invalid")
        mapped = SCHEDULE_PLATFORM_CAPABILITY_ALIASES.get(value, value)
        if mapped not in PLATFORM_IO_CAPABILITY_SET:
            raise ValueError("schedule_control_capability_unknown")
        if mapped not in allowed:
            raise ValueError("schedule_control_capability_unauthorized")
        if mapped not in observed:
            observed.add(mapped)
            canonical.append(mapped)
    return tuple(canonical)

# Platform scheduling is excluded from the unattended default to prevent
# recursive jobs. Native agent/subagent/file/shell authority is independently
# owned by the selected engine and its Session permission preset.
UNATTENDED_DEFAULT_PLATFORM_IO_CAPABILITIES: tuple[str, ...] = tuple(
    name
    for name in PLATFORM_IO_CAPABILITIES
    if name != "cronjob"
)
