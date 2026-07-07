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
from urllib.parse import urlparse

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
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


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


def _is_private_ip(hostname: str) -> bool:
    """Check if the hostname resolves to a private/internal IP address."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.getaddrinfo(hostname, None)[0][4][0])
        except (socket.gaierror, IndexError, ValueError):
            return True  # fail-closed: can't resolve → block

    for net in _PRIVATE_NETWORKS:
        if ip in net:
            return True
    return False


def check_url_safety(url: str) -> str | None:
    """Validate URL safety. Returns an error message if blocked, ``None`` if safe.

    Blocks:
      - Cloud metadata endpoints (169.254.169.254, etc.)
      - Private / internal IP addresses (RFC 1918, loopback, link-local, CGNAT)
      - Malformed or hostless URLs
    """
    if not url or not isinstance(url, str):
        return "Invalid URL"

    try:
        parsed = urlparse(url)
    except Exception:
        return f"Cannot parse URL: {url[:200]}"

    hostname = parsed.hostname or ""
    if not hostname:
        return f"No hostname in URL: {url[:200]}"

    if _is_always_blocked(hostname):
        return f"Blocked: {hostname} is a cloud metadata endpoint"

    if _is_private_ip(hostname):
        return f"Blocked: {hostname} resolves to a private/internal IP"

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