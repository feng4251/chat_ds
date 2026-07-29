"""Approval / dangerous-operation detection for Web (non-interactive) agents.

In the CLI, dangerous operations prompt the user for confirmation.  In a Web
multi-user environment that model doesn't work — we must auto-deny dangerous
operations and return a clear explanation so the agent can adjust.

Checks:
  - **Dangerous shell commands:** ``rm -rf /``, ``curl|bash``, ``sudo``, ``mkfs``, etc.
  - **URL safety:** cloud metadata endpoints, private/internal IPs (RFC 1918, etc.)
  - **File write safety:** path traversal, sensitive system paths.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Dangerous command patterns
# ---------------------------------------------------------------------------

# Ordered by severity — first match wins.
_DANGER_PATTERNS: list[tuple[str, str]] = [
    # Filesystem destruction
    (r"\brm\s+-rf\s+/", "rm -rf / — filesystem destruction"),
    (r"\brm\s+-rf\s+\/\*", "rm -rf /* — filesystem destruction"),
    (r"\brm\s+-rf\s+~", "rm -rf ~ — home directory deletion"),
    (r"\brm\s+-rf\s+\$HOME", "rm -rf $HOME — home directory deletion"),
    (r"\brm\s+-fr\s+/", "rm -fr / — filesystem destruction"),
    # Fork bombs
    (r":\(\)\s*\{", "Fork bomb detected — denial of service"),
    (r"\)\s*\{[^}]*\}\s*;", "Fork bomb pattern detected"),
    # Privilege escalation
    (r"\bsudo\b", "sudo — privilege escalation not permitted"),
    (r"\bsu\s+-", "su — user switching not permitted"),
    (r"\bchmod\s+777", "chmod 777 — world-writable permission escalation"),
    (r"\bchmod\s+-R\s+777", "chmod -R 777 — recursive world-writable"),
    (r"\bchown\s+root", "chown root — ownership change not permitted"),
    # Pipe-to-shell / remote code execution
    (r"curl.*\|.*(?:ba)?sh", "curl|sh — remote code execution risk"),
    (r"wget.*\|.*(?:ba)?sh", "wget|sh — remote code execution risk"),
    (r"curl.*\|.*bash", "curl|bash — remote code execution risk"),
    (r"wget.*-O\s*-\s*\|", "wget|pipe — remote code execution risk"),
    # Device files
    (r"/dev/(sda|sd|hd|nvme|dm-|loop|mmcblk|ram|zero|null|random)", "Direct device access — /dev/ manipulation"),
    (r"\bmount\s+/dev/", "Mounting raw devices not permitted"),
    # Filesystem/mkfs
    (r"\bmkfs\.", "mkfs — filesystem creation not permitted"),
    (r"\bmke2fs\b", "mke2fs — filesystem creation not permitted"),
    (r"\bmkdosfs\b", "mkdosfs — filesystem creation not permitted"),
    (r"\bdd\s+if=", "dd — raw disk copying not permitted"),
    (r"\bsponge\b", "sponge — file overwrite risk"),
    # System destabilization
    (r"\bshutdown\b", "shutdown — system halt not permitted"),
    (r"\breboot\b", "reboot — system restart not permitted"),
    (r"\binit\s+[06]", "init runlevel change not permitted"),
    (r"\bsystemctl\s+(stop|disable|mask)", "systemctl destructive operation"),
    (r"\bkillall\b", "killall — mass process termination"),
    (r"\bpkill\b", "pkill — mass process termination"),
    # Network dangerous
    (r"\biptables\b", "iptables — firewall manipulation not permitted"),
    (r"\bnft\b", "nft — firewall manipulation not permitted"),
    (r"\bnc\s+-l", "netcat listener — unauthorized service"),
    # Overwrite critical files
    (r">\s*/etc/passwd", "Overwriting /etc/passwd"),
    (r">\s*/etc/shadow", "Overwriting /etc/shadow"),
    (r">\s*/etc/hosts", "Overwriting /etc/hosts"),
    (r">\s*(?:/root/|/etc/|/boot/)", "Overwriting system files"),
    # Cryptomining / abuse patterns
    (r"\bminer(?:d)?\b.*(?:--url|--server|stratum)", "Potential crypto-mining activity"),
    # Reverse shells
    (r"(?:bash|sh|nc|python|perl)\s+-[ci]\s+.*(?:/dev/tcp|/dev/udp)", "Reverse shell detected"),
]

# Additional patterns that warrant a warning but not outright blocking
_WARN_PATTERNS: list[tuple[str, str]] = [
    (r"\bos\.system\(", "os.system() — prefer subprocess for safety"),
    (r"\bsubprocess\.Popen\(", "subprocess.Popen() detected"),
    (r"\beval\(", "eval() — arbitrary code execution risk"),
    (r"\bexec\(", "exec() — arbitrary code execution risk"),
    (r"__import__\s*\(", "__import__() — dynamic import detected"),
    (r"\.decode\s*\(\s*['\"]base64", "base64 decode — suspicious encoding"),
]


def check_code_danger(code: str) -> str | None:
    """Check Python/shell code for dangerous operations.

    Returns:
        Error message string if dangerous, ``None`` if safe.
    """
    if not code or not code.strip():
        return "Empty code block"

    # Check for dangerous patterns
    for pattern, reason in _DANGER_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE | re.MULTILINE):
            return f"Blocked dangerous operation: {reason}"

    return None


def check_code_warnings(code: str) -> list[str]:
    """Return non-blocking warnings about code patterns."""
    warnings = []
    for pattern, reason in _WARN_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE | re.MULTILINE):
            warnings.append(reason)
    return warnings


# ---------------------------------------------------------------------------
# URL safety (shared with browser tools)
# ---------------------------------------------------------------------------

# Always-blocked cloud metadata endpoints
_ALWAYS_BLOCKED_HOSTS: set[str] = {
    "metadata.google.internal",
    "metadata.goog",
}

_ALWAYS_BLOCKED_IPS: set[str] = {
    "169.254.169.254",  # AWS / GCP / Azure metadata
    "169.254.170.2",    # AWS ECS task metadata
    "169.254.169.253",  # Azure IMDS
    "fd00:ec2::254",    # AWS IPv6
    "100.100.100.200",  # Alibaba Cloud metadata
}

_ALWAYS_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::ffff:169.254.0.0/112"),
]

# Private / internal network ranges
_PRIVATE_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),   # CGNAT
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking / synthetic DNS
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

# Deployment configuration may narrow, but never widen beyond, these address
# families commonly reserved by transparent egress proxies for synthetic DNS.
# A literal URL using one of these addresses remains blocked regardless.
_SUPPORTED_SYNTHETIC_DNS_SUPERNETS: tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
] = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fc00::/18"),
)


def _synthetic_dns_networks(
    configured: str | tuple[str, ...] | list[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse a deployment-owned, tightly bounded synthetic-DNS policy."""

    values = (
        re.split(r"[\s,;]+", configured)
        if isinstance(configured, str)
        else [str(value) for value in configured]
    )
    accepted: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in values:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        try:
            network = ipaddress.ip_network(candidate, strict=True)
        except ValueError:
            continue
        if not any(
            network.version == parent.version and network.subnet_of(parent)
            for parent in _SUPPORTED_SYNTHETIC_DNS_SUPERNETS
        ):
            continue
        if network not in accepted:
            accepted.append(network)
    return tuple(accepted)


def _is_always_blocked(hostname: str, ip_addr: str | None = None) -> bool:
    """Check always-blocked cloud metadata endpoints (never bypassable)."""
    if hostname.lower() in _ALWAYS_BLOCKED_HOSTS:
        return True
    if ip_addr:
        try:
            ip = ipaddress.ip_address(ip_addr)
            if str(ip) in _ALWAYS_BLOCKED_IPS:
                return True
            for net in _ALWAYS_BLOCKED_NETWORKS:
                if ip in net:
                    return True
        except ValueError:
            pass
    return False


def _resolved_ip_addresses(hostname: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Resolve every address for one host, failing closed on ambiguity."""

    try:
        return (ipaddress.ip_address(hostname),)
    except ValueError:
        try:
            rows = socket.getaddrinfo(
                hostname,
                None,
                type=socket.SOCK_STREAM,
            )
        except (socket.gaierror, OSError):
            return ()
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for row in rows:
        try:
            value = str(row[4][0]).split("%", 1)[0]
            address = ipaddress.ip_address(value)
        except (IndexError, TypeError, ValueError):
            continue
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _is_private_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return any(address in network for network in _PRIVATE_NETWORKS)


def canonical_http_origin(
    url: str,
    *,
    require_origin_only: bool = False,
) -> str | None:
    """Return a stable HTTP(S) origin without accepting URL credentials."""

    if not isinstance(url, str) or not url or url != url.strip():
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    hostname = parsed.hostname
    if not hostname or any(ord(char) < 32 for char in hostname):
        return None
    if require_origin_only and (
        parsed.path not in {"", "/"} or parsed.query or parsed.fragment
    ):
        return None
    try:
        address = ipaddress.ip_address(hostname)
        canonical_host = address.compressed
        if isinstance(address, ipaddress.IPv6Address):
            canonical_host = f"[{canonical_host}]"
    except ValueError:
        try:
            canonical_host = hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        except (UnicodeError, ValueError):
            return None
        if not canonical_host:
            return None
    default_port = 80 if scheme == "http" else 443
    port_suffix = "" if port in {None, default_port} else f":{port}"
    return f"{scheme}://{canonical_host}{port_suffix}"


def _never_allowlisted_address(
    hostname: str,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return bool(
        _is_always_blocked(hostname, str(address))
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
        # ``IPv6Address.is_global`` still reports the deprecated fec0::/10
        # site-local range as global on supported Python versions.  Treat it
        # as internal explicitly; no deployment allowlist may grant it.
        or (
            isinstance(address, ipaddress.IPv6Address)
            and address.is_site_local
        )
    )


def _private_origin_is_allowlist_eligible(origin: str) -> bool:
    parsed = urlsplit(origin)
    hostname = parsed.hostname or ""
    addresses = _resolved_ip_addresses(hostname)
    return bool(
        addresses
        and not any(
            _never_allowlisted_address(hostname, address)
            for address in addresses
        )
        # A deployment entry advertised as a private origin must not be a
        # split-horizon/mixed public+private answer.  Mixed answers stay on the
        # ordinary fail-closed path instead of receiving private reachability.
        and all(_is_private_address(address) for address in addresses)
    )


_EXPLICIT_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}\u3002\uff0c\uff1b\uff1a\uff01\uff1f\uff09\u3011\u300b"
_MAX_USER_BROWSER_EGRESS_ORIGINS = 32
_DEFAULT_USER_BROWSER_METHODS = ("GET", "HEAD", "OPTIONS", "POST")


def compile_user_private_origin_grants(
    user_text: str,
    configured_origins: str | list[str] | tuple[str, ...],
) -> tuple[str, ...]:
    """Intersect explicit user URL origins with a deployment-owned allowlist.

    A Skill body, model argument, redirect, or bare hostname cannot create a
    grant.  Always-blocked metadata, loopback, link-local, malformed, and
    credential-bearing origins are excluded even when configuration is wrong.
    """

    if isinstance(configured_origins, str):
        configured_values = re.split(r"[\s,;]+", configured_origins)
    else:
        configured_values = [
            str(value) for value in configured_origins
            if isinstance(value, str)
        ]
    configured: set[str] = set()
    for value in configured_values:
        candidate = str(value or "").strip()
        if not candidate:
            continue
        origin = canonical_http_origin(candidate, require_origin_only=True)
        if origin and _private_origin_is_allowlist_eligible(origin):
            configured.add(origin)

    grants: list[str] = []
    for match in _EXPLICIT_HTTP_URL_RE.finditer(str(user_text or "")):
        value = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        origin = canonical_http_origin(value)
        if origin in configured and origin not in grants:
            grants.append(origin)
    return tuple(grants)


def compile_user_browser_egress_rules(
    user_text: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Compile same-origin browser authority from bounded user-authored URLs.

    This compiler is intentionally syntactic and side-effect free. DNS/IP
    safety and the independent private-origin deployment intersection are
    revalidated immediately before navigation and for every browser request.
    Skill prose, assistant/tool turns, redirects, and model-authored tool
    arguments are not inputs.  The Harness may supply the latest user turn
    plus one bounded nearest user URL turn when that latest turn explicitly
    refers to continuing the prior URL/site/Skill.
    """

    from tools.session_sandbox_policy import (
        SessionSandboxPolicyError,
        browser_egress_rule_tuples,
        normalize_http_url_prefix,
    )

    rows: list[tuple[str, tuple[str, ...]]] = []
    seen_origins: set[str] = set()
    for match in _EXPLICIT_HTTP_URL_RE.finditer(str(user_text or "")):
        value = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        origin = canonical_http_origin(value)
        if origin is None or origin in seen_origins:
            continue
        try:
            prefix = normalize_http_url_prefix(origin + "/")
        except SessionSandboxPolicyError:
            continue
        rows.append((prefix, _DEFAULT_USER_BROWSER_METHODS))
        seen_origins.add(origin)
        if len(rows) >= _MAX_USER_BROWSER_EGRESS_ORIGINS:
            break
    return browser_egress_rule_tuples(rows)


def check_url_safety(
    url: str,
    *,
    allowed_private_origins: tuple[str, ...] | list[str] = (),
    synthetic_public_ranges: str | tuple[str, ...] | list[str] = (),
) -> str | None:
    """Validate URL safety. Returns an error message if blocked, ``None`` if safe.

    Blocks:
      - Cloud metadata endpoints (169.254.169.254, etc.)
      - Private / internal IP addresses (RFC 1918, loopback, link-local, CGNAT)
      - Malformed or hostless URLs
    """
    if not url or not isinstance(url, str):
        return "Invalid URL"

    origin = canonical_http_origin(url)
    if origin is None:
        return (
            "Blocked: URL must use http(s), contain no credentials, and have "
            "a valid hostname/port"
        )
    hostname = urlsplit(origin).hostname or ""
    try:
        ipaddress.ip_address(hostname)
        hostname_is_literal_ip = True
    except ValueError:
        hostname_is_literal_ip = False
    addresses = _resolved_ip_addresses(hostname)
    if not addresses:
        return f"Blocked: cannot safely resolve hostname {hostname[:200]}"
    configured_synthetic = _synthetic_dns_networks(synthetic_public_ranges)

    def is_synthetic_public(
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> bool:
        return bool(
            not hostname_is_literal_ip
            and any(address in network for network in configured_synthetic)
        )

    if any(
        _never_allowlisted_address(hostname, address)
        and not is_synthetic_public(address)
        for address in addresses
    ):
        return f"Blocked: {hostname} is metadata, loopback, link-local, or otherwise non-routable"

    allowed = {
        normalized
        for value in allowed_private_origins
        if (
            normalized := canonical_http_origin(
                str(value), require_origin_only=True
            )
        ) is not None
        and _private_origin_is_allowlist_eligible(normalized)
    }
    for address in addresses:
        if is_synthetic_public(address):
            continue
        if _is_private_address(address):
            if origin not in allowed:
                return f"Blocked: {hostname} resolves to a private/internal IP"
            continue
        # Fail closed for IANA special-purpose/documentation/this-network
        # space that is neither one of our explicitly modeled private ranges
        # nor a truly globally routable address.  This also protects mixed DNS
        # answers: every returned address must be safe independently.
        if not address.is_global:
            return f"Blocked: {hostname} resolves to a non-global IP"

    return None


# ---------------------------------------------------------------------------
# File-write safety
# ---------------------------------------------------------------------------

# Extensions that should never be written by agent tools
_BLOCKED_EXTENSIONS: set[str] = {".so", ".dylib", ".dll", ".exe", ".bin", ".elf"}

# Sensitive path segments — reject writes into these directories
_SENSITIVE_PATH_SEGMENTS: set[str] = {
    "/etc", "/root", "/boot", "/proc", "/sys", "/dev", "/run", "/var/run",
}


def check_file_write_safety(filepath: str) -> str | None:
    """Check whether a file write is safe (called AFTER path is validated in sandbox).

    The path is already resolved inside the sandbox by ``validate_path()``,
    so this is an additional layer to catch suspicious extensions or
    content-agnostic patterns.
    """
    import os

    # Block certain binary extensions
    _, ext = os.path.splitext(filepath)
    if ext.lower() in _BLOCKED_EXTENSIONS:
        return f"Writing {ext} files is not allowed"

    # Block writes to hidden dot-prefixed files in the sandbox root
    # (aggressive but safe — the model shouldn't need .env, .gitconfig, etc.)
    # Relaxed: only block truly system-sensitive ones
    basename = os.path.basename(filepath)
    if basename in {".bashrc", ".profile", ".bash_profile", ".zshrc", ".ssh", ".gnupg"}:
        return f"Writing {basename} is not allowed"

    return None
