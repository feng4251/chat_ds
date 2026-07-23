"""Validate the runtime-injected egress proxy used by browser workers."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping
from urllib.parse import urlsplit


PROXY_ENVIRONMENT_VARIABLE = "SKILL_EGRESS_PROXY_URL"


class PolicyError(RuntimeError):
    """The runtime proxy environment is absent or malformed."""


@dataclass(frozen=True)
class ProxyPolicy:
    policy_id: str
    proxy_url: str


def _validate_proxy_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyError("proxy_url must be a non-empty string")
    if len(value) > 1024:
        raise PolicyError("proxy_url is too long")
    if any(char.isspace() or ord(char) < 0x21 for char in value):
        raise PolicyError("proxy_url contains whitespace or control characters")

    parsed = urlsplit(value)
    if parsed.scheme != "http":
        raise PolicyError("proxy_url must use the internal HTTP CONNECT proxy")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PolicyError("proxy_url has an invalid port") from exc
    if not parsed.hostname or port is None:
        raise PolicyError("proxy_url must include a host and explicit port")
    if parsed.username is not None or parsed.password is not None:
        raise PolicyError("proxy credentials are forbidden in the worker policy")
    if (
        parsed.path
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise PolicyError("proxy_url may not contain a path, query, or fragment")
    return f"http://{parsed.netloc}"


def load_proxy_environment(
    environment: Mapping[str, str] | None = None,
) -> ProxyPolicy:
    """Read the fixed runtime-owned environment key.

    This launch control is defense in depth. The worker's ``network_mode:
    none`` namespace remains the authority that makes direct egress
    impossible if untrusted code changes its own child environment.
    """

    source = os.environ if environment is None else environment
    return ProxyPolicy(
        policy_id="runtime-egress-proxy",
        proxy_url=_validate_proxy_url(source.get(PROXY_ENVIRONMENT_VARIABLE)),
    )


def proxy_environment(policy: ProxyPolicy) -> dict[str, str]:
    """Return proxy variables while keeping only local driver control direct."""

    local_only = "localhost,127.0.0.1,[::1]"
    return {
        "HTTP_PROXY": policy.proxy_url,
        "HTTPS_PROXY": policy.proxy_url,
        "ALL_PROXY": policy.proxy_url,
        "http_proxy": policy.proxy_url,
        "https_proxy": policy.proxy_url,
        "all_proxy": policy.proxy_url,
        "NO_PROXY": local_only,
        "no_proxy": local_only,
        "CHATDS_EGRESS_POLICY_ID": policy.policy_id,
        PROXY_ENVIRONMENT_VARIABLE: policy.proxy_url,
    }
