"""Typed, controller-owned current-Turn capability audit receipt.

The receipt is diagnostic/control-plane state only. It is never rendered into
Claude's system or user prompt: native tool discovery, Skill instructions and
MCP schemas remain the model-facing authority.
"""

from __future__ import annotations

import re
from typing import Any


RUNTIME_CAPABILITY_SCHEMA = "chatds.runtime-capabilities.v1"
MAX_STRUCTURED_CAPABILITIES = 128
_SAFE_CAPABILITY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_PUBLIC_READ_METHODS = frozenset({"GET", "HEAD"})


def _invalid() -> RuntimeError:
    return RuntimeError("runtime_capability_contract_invalid")


def _normalize_contract(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "structured_capabilities",
        "public_http_read",
    }:
        raise _invalid()
    if value.get("schema") != RUNTIME_CAPABILITY_SCHEMA:
        raise _invalid()
    capabilities = value.get("structured_capabilities")
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > MAX_STRUCTURED_CAPABILITIES
        or any(
            not isinstance(name, str)
            or _SAFE_CAPABILITY.fullmatch(name) is None
            for name in capabilities
        )
        or capabilities != sorted(set(capabilities))
    ):
        raise _invalid()
    public_read = value.get("public_http_read")
    if not isinstance(public_read, dict) or set(public_read) != {
        "enabled",
        "methods",
        "ports",
    }:
        raise _invalid()
    enabled = public_read.get("enabled")
    methods = public_read.get("methods")
    ports = public_read.get("ports")
    if (
        type(enabled) is not bool
        or not isinstance(methods, list)
        or not isinstance(ports, list)
        or any(
            not isinstance(method, str)
            or method not in _PUBLIC_READ_METHODS
            for method in methods
        )
        or methods != [
            method for method in ("GET", "HEAD") if method in set(methods)
        ]
        or any(type(port) is not int or not 1 <= port <= 65_535 for port in ports)
        or ports != sorted(set(ports))
        or enabled != bool(methods and ports)
    ):
        raise _invalid()
    return {
        "schema": RUNTIME_CAPABILITY_SCHEMA,
        "structured_capabilities": list(capabilities),
        "public_http_read": {
            "enabled": enabled,
            "methods": list(methods),
            "ports": list(ports),
        },
    }


def compile_runtime_capability_contract(
    *,
    manifest: object,
    egress_policy: object,
) -> dict[str, Any]:
    """Compile current controller authority without consulting model prose."""

    if not isinstance(manifest, dict) or not isinstance(egress_policy, dict):
        raise _invalid()
    rules = manifest.get("platform_egress_rules", [])
    if not isinstance(rules, list) or len(rules) > MAX_STRUCTURED_CAPABILITIES:
        raise _invalid()
    capabilities: set[str] = set()
    declared_capabilities = manifest.get("platform_capabilities", [])
    if (
        not isinstance(declared_capabilities, list)
        or len(declared_capabilities) > MAX_STRUCTURED_CAPABILITIES
        or any(
            not isinstance(name, str)
            or _SAFE_CAPABILITY.fullmatch(name) is None
            for name in declared_capabilities
        )
        or declared_capabilities != sorted(set(declared_capabilities))
    ):
        raise _invalid()
    capabilities.update(declared_capabilities)
    for row in rules:
        if not isinstance(row, dict):
            raise _invalid()
        name = row.get("capability")
        if not isinstance(name, str) or _SAFE_CAPABILITY.fullmatch(name) is None:
            raise _invalid()
        capabilities.add(name)

    raw_public_read = egress_policy.get("public_read")
    if raw_public_read is None:
        public_http_read = {
            "enabled": False,
            "methods": [],
            "ports": [],
        }
    else:
        if not isinstance(raw_public_read, dict):
            raise _invalid()
        raw_methods = raw_public_read.get("methods")
        raw_ports = raw_public_read.get("ports")
        if (
            not isinstance(raw_methods, list)
            or not isinstance(raw_ports, list)
            or any(
                not isinstance(method, str)
                or method not in _PUBLIC_READ_METHODS
                for method in raw_methods
            )
            or any(
                type(port) is not int or not 1 <= port <= 65_535
                for port in raw_ports
            )
        ):
            raise _invalid()
        public_http_read = {
            "enabled": bool(raw_methods and raw_ports),
            "methods": [
                method
                for method in ("GET", "HEAD")
                if method in set(raw_methods)
            ],
            "ports": sorted(set(raw_ports)),
        }
        if public_http_read["methods"] != raw_methods:
            raise _invalid()

    return _normalize_contract({
        "schema": RUNTIME_CAPABILITY_SCHEMA,
        "structured_capabilities": sorted(capabilities),
        "public_http_read": public_http_read,
    })


def validate_runtime_capability_contract(contract: object) -> dict[str, Any]:
    """Validate and normalize a durable audit receipt without prompting."""

    return _normalize_contract(contract)
