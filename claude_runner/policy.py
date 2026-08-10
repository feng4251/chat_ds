"""Compile one exact, auditable outbound policy for a Claude Turn."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit

from skills.http_grants import (
    compile_loaded_skill_sandbox_egress_rules,
    compile_user_sandbox_egress_urls,
)
from tools.session_sandbox_policy import (
    normalize_http_origin,
    normalize_http_url_prefix,
    normalize_session_sandbox_egress_rules,
)


class ClaudeEgressPolicyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedSkillView:
    """Process-local attestation receipt for one immutable Skill view."""

    root: Path
    sha256: str
    manifest: dict[str, Any]
    policy_resources: dict[str, bytes]


def compile_turn_egress_policy(
    *,
    skill_view_root: Path,
    skill_view_sha256: str,
    verified_skill_view: VerifiedSkillView | None = None,
    user_turn_text: str,
    provider_base_url: str,
    configured_private_origins: Iterable[str],
    budget_scope_sha256: str,
    call_id_sha256: str,
    limits: dict[str, int],
) -> dict[str, Any]:
    # The Supervisor attests the immutable view before compiling execution
    # authority.  Accept that exact receipt so one start transaction never
    # re-reads and re-hashes an arbitrarily large/NFS-backed Skill tree.  The
    # standalone compiler still verifies for callers that do not already own
    # an attestation receipt.
    receipt = (
        verify_skill_view(skill_view_root, skill_view_sha256)
        if verified_skill_view is None
        else verified_skill_view
    )
    if (
        not isinstance(receipt, VerifiedSkillView)
        or receipt.root != Path(skill_view_root)
        or receipt.sha256 != skill_view_sha256
        or receipt.manifest.get("sha256") != skill_view_sha256
    ):
        raise ClaudeEgressPolicyError("Verified Skill view receipt is invalid")
    manifest = receipt.manifest
    verified_resources = receipt.policy_resources
    prefix_rows: list[dict[str, Any]] = []
    harness_capability_origins: set[str] = set()
    plugin_skills = Path(skill_view_root) / "plugin" / "skills"
    for skill in manifest.get("skills", []):
        if not isinstance(skill, dict):
            raise ClaudeEgressPolicyError("Skill view manifest is malformed")
        name = str(skill.get("name") or "")
        root = plugin_skills / name
        try:
            relative_instruction = f"plugin/skills/{name}/SKILL.md"
            cached = (
                verified_resources.get(relative_instruction)
                if isinstance(verified_resources, dict)
                else None
            )
            content = (
                bytes(cached).decode("utf-8")
                if isinstance(cached, (bytes, bytearray))
                else (root / "SKILL.md").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError) as exc:
            raise ClaudeEgressPolicyError("Skill instructions are unavailable") from exc
        allowed_paths = tuple(
            str(row.get("path") or "")
            for row in skill.get("files", [])
            if isinstance(row, dict)
        )
        loaded = {
            "_chatds_scope": "session",
            "content": content,
            "resource_graph": {"skill_root": str(root)},
        }
        for _skill_name, prefix, methods in compile_loaded_skill_sandbox_egress_rules(
            name, loaded, allowed_paths
        ):
            prefix_rows.append({"url_prefix": prefix, "methods": list(methods)})

    harness_rules = manifest.get("harness_egress_rules", [])
    if not isinstance(harness_rules, list):
        raise ClaudeEgressPolicyError("Harness capability rules are malformed")
    for row in harness_rules:
        if (
            not isinstance(row, dict)
            or set(row) != {"capability", "url_prefix", "methods"}
            or row.get("capability") != "web_search"
            or row.get("methods") != ["GET"]
        ):
            raise ClaudeEgressPolicyError(
                "Harness capability rule is malformed"
            )
        prefix = normalize_http_url_prefix(str(row.get("url_prefix") or ""))
        prefix_rows.append({"url_prefix": prefix, "methods": ["GET"]})
        harness_capability_origins.add(normalize_http_origin(prefix))

    # The Runner loads this generated MCP file explicitly and rejects ambient
    # MCP configuration. Remote transports are themselves an explicit Skill authority, but receive only
    # the protocol methods needed by streamable HTTP/SSE rather than an
    # origin-wide grant. The exact endpoint remains the path-prefix boundary.
    mcp_path = Path(skill_view_root) / "plugin" / ".mcp.json"
    cached_mcp = (
        verified_resources.get("plugin/.mcp.json")
    )
    if cached_mcp is not None:
        try:
            mcp_payload = json.loads(
                bytes(cached_mcp).decode("utf-8")
                if isinstance(cached_mcp, (bytes, bytearray))
                else mcp_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise ClaudeEgressPolicyError("Compiled MCP configuration is invalid") from exc
        mcp_servers = mcp_payload.get("mcpServers") if isinstance(mcp_payload, dict) else None
        if not isinstance(mcp_servers, dict):
            raise ClaudeEgressPolicyError("Compiled MCP configuration is invalid")
        for config in mcp_servers.values():
            if not isinstance(config, dict) or config.get("type") not in {"http", "sse"}:
                continue
            prefix = normalize_http_url_prefix(str(config.get("url") or ""))
            prefix_rows.append({
                "url_prefix": prefix,
                "methods": ["GET", "HEAD", "OPTIONS", "POST", "DELETE"],
            })

    # Current-user URLs are exact retrieval coordinates, not origin-wide or
    # write authority. POST/PUT/PATCH/DELETE require an explicit Skill binding.
    user_urls = compile_user_sandbox_egress_urls(user_turn_text)
    exact_query_rows = [
        {"url_prefix": prefix, "methods": ["GET", "HEAD"]}
        for prefix in user_urls
    ]

    # Claude Code 2.x uses the Anthropic SDK's beta transport coordinate and
    # may ask the provider to count tokens before sending a long Messages
    # request. Grant only those four exact protocol coordinates; in
    # particular, ``beta=true`` is not widened into arbitrary query access.
    provider_prefixes = tuple(
        normalize_http_url_prefix(
            provider_base_url.rstrip("/") + suffix
        )
        for suffix in (
            "/v1/messages",
            "/v1/messages?beta=true",
            "/v1/messages/count_tokens",
            "/v1/messages/count_tokens?beta=true",
        )
    )
    exact_query_rows.extend(
        {"url_prefix": prefix, "methods": ["POST"]}
        for prefix in provider_prefixes
    )
    prefix_rules = normalize_session_sandbox_egress_rules(prefix_rows)
    exact_query_rules = normalize_session_sandbox_egress_rules(exact_query_rows)
    rule_payloads = [rule.as_payload() for rule in prefix_rules]
    rule_payloads.extend({
        **rule.as_payload(),
        # A model must not append a covert query string to a user-provided
        # retrieval coordinate or to the deployment-owned Provider endpoint.
        # Skill/MCP rules intentionally retain prefix semantics because their
        # authored protocol may require query parameters.
        "query_exact": True,
    } for rule in exact_query_rules)
    origins = tuple(dict.fromkeys(
        normalize_http_origin(str(rule["url_prefix"]))
        for rule in rule_payloads
    ))

    configured = {
        normalize_http_origin(value)
        for value in configured_private_origins
        if _private_origin_eligible(value)
    }
    user_origins = {
        normalize_http_origin(value)
        for value in user_urls
        if _private_origin_eligible(value)
    }
    provider_origin = normalize_http_origin(provider_prefixes[0])
    private_origins = tuple(
        origin
        for origin in origins
        if _origin_is_private(origin)
        and origin in configured
        and (
            origin in user_origins
            or origin == provider_origin
            or origin in harness_capability_origins
        )
    )
    return {
        "policy_version": 3,
        "origin_allowlist": list(origins),
        "egress_rules": rule_payloads,
        "private_origins": list(private_origins),
        "budget_scope_sha256": _sha256_identity(budget_scope_sha256),
        "call_id_sha256": _sha256_identity(call_id_sha256),
        "limits": _validated_limits(limits),
    }


def verify_skill_view(root: Path, expected_sha256: str) -> VerifiedSkillView:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""):
        raise ClaudeEgressPolicyError("Skill view digest is invalid")
    try:
        root_stat = os.lstat(root)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ClaudeEgressPolicyError("Skill view is unavailable") from exc
    if root.is_symlink() or not root.is_dir() or root_stat.st_mode & 0o222:
        raise ClaudeEgressPolicyError("Skill view is not immutable")
    if not isinstance(manifest, dict) or manifest.get("sha256") != expected_sha256:
        raise ClaudeEgressPolicyError("Skill view identity mismatch")
    identity = dict(manifest)
    identity.pop("sha256", None)
    if _canonical_sha256(identity) != expected_sha256:
        raise ClaudeEgressPolicyError("Skill view manifest digest mismatch")
    verified_resources: dict[str, bytes] = {}
    for row in manifest.get("files", []):
        if not isinstance(row, dict):
            raise ClaudeEgressPolicyError("Skill view file manifest is malformed")
        relative = PurePosixPath(str(row.get("path") or ""))
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ClaudeEgressPolicyError("Skill view path is unsafe")
        path = root.joinpath(*relative.parts)
        try:
            path_stat = os.lstat(path)
            payload = path.read_bytes()
        except OSError as exc:
            raise ClaudeEgressPolicyError("Skill view resource is unavailable") from exc
        if path.is_symlink() or not path.is_file() or path_stat.st_mode & 0o222:
            raise ClaudeEgressPolicyError("Skill view resource is mutable or non-regular")
        if len(payload) != row.get("size") or hashlib.sha256(payload).hexdigest() != row.get("sha256"):
            raise ClaudeEgressPolicyError("Skill view resource digest mismatch")
        relative_name = relative.as_posix()
        if (
            relative_name == "plugin/.mcp.json"
            or relative_name.startswith("plugin/skills/")
            and relative_name.endswith("/SKILL.md")
        ):
            verified_resources[relative_name] = payload
    return VerifiedSkillView(
        root=Path(root),
        sha256=expected_sha256,
        manifest=manifest,
        policy_resources=verified_resources,
    )


def _private_origin_eligible(value: str) -> bool:
    host = ""
    try:
        origin = normalize_http_origin(value)
        parsed = urlsplit(origin)
        host = parsed.hostname or ""
        address = ipaddress.ip_address(host)
    except (ValueError, TypeError):
        # Hostnames are re-resolved and checked by the fixed proxy.  Exclude
        # obvious local/metadata spellings here; the proxy remains authoritative.
        return bool(host and host not in {"localhost", "metadata.google.internal"})
    return not (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or str(address) == "169.254.169.254"
    )


def _origin_is_private(value: str) -> bool:
    host = urlsplit(value).hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # The policy proxy performs DNS pinning and decides whether a hostname
        # resolves privately. Passing only configured/user-intersected names is
        # conservative and does not itself grant routing authority.
        return True
    return address.is_private and not (address.is_loopback or address.is_link_local)


def _validated_limits(value: dict[str, int]) -> dict[str, int]:
    expected = {"max_outbound_bytes", "max_requests", "max_response_wire_bytes"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ClaudeEgressPolicyError("Egress budgets are invalid")
    maximums = {
        "max_outbound_bytes": 1024 * 1024 * 1024,
        "max_requests": 65_536,
        "max_response_wire_bytes": 16 * 1024 * 1024 * 1024,
    }
    result: dict[str, int] = {}
    for name in sorted(expected):
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= maximums[name]:
            raise ClaudeEgressPolicyError("Egress budgets are invalid")
        result[name] = item
    return result


def _sha256_identity(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value or "") is None:
        raise ClaudeEgressPolicyError("Egress identity is invalid")
    return value


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
