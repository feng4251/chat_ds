"""Mandatory Chromium proxy wrapper used by Playwright and ChromeDriver."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Iterable

from .policy import ProxyPolicy, load_proxy_environment, proxy_environment


REAL_CHROMIUM = Path("/usr/lib/chatds-browser-runtime/chromium.debian")
_INTROSPECTION = frozenset({"--version", "--product-version", "--help"})
_FORBIDDEN_ARGUMENTS = frozenset(
    {
        "--no-proxy-server",
        "--proxy-auto-detect",
        "--proxy-server",
        "--proxy-bypass-list",
        "--proxy-pac-url",
        "--host-resolver-rules",
        "--remote-debugging-address",
        "--remote-debugging-port",
        "--disable-blink-features",
        "--disable-web-security",
        "--disable-seccomp-filter-sandbox",
        "--disable-namespace-sandbox",
        "--disable-gpu-sandbox",
        "--disable-zygote-sandbox",
    }
)
_FORBIDDEN_PREFIXES = (
    "--proxy-server=",
    "--proxy-bypass-list=",
    "--proxy-pac-url=",
    "--host-resolver-rules=",
    "--remote-debugging-address=",
    "--remote-debugging-port=",
    "--disable-blink-features=",
)


def controlled_arguments(
    arguments: Iterable[str],
    policy: ProxyPolicy,
    *,
    trusted_chromedriver_parent: bool = False,
) -> list[str]:
    """Reject caller proxy overrides and append the runtime-owned controls."""

    received: list[str] = []
    for argument in arguments:
        # Playwright defaults chromiumSandbox to false and injects this flag
        # even when the deployment provides a working non-root namespace
        # sandbox. Remove it unconditionally so framework defaults and Skill
        # arguments cannot weaken the controller's browser policy.
        if argument == "--no-sandbox":
            continue
        # ChromeDriver itself needs an ephemeral loopback control endpoint.
        # The wrapper permits only its generated port=0 form and only when the
        # direct parent is the baked-in driver binary. Model/Skill arguments
        # are checked with the default fail-closed path.
        if (
            trusted_chromedriver_parent
            and argument == "--remote-debugging-port=0"
        ):
            received.append(argument)
            continue
        if argument in _FORBIDDEN_ARGUMENTS or argument.startswith(_FORBIDDEN_PREFIXES):
            raise ValueError("caller-controlled Chromium proxy/security flags are forbidden")
        received.append(argument)
    return [
        *received,
        f"--proxy-server={policy.proxy_url}",
        "--proxy-bypass-list=<-loopback>",
        "--disable-quic",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--disable-dev-shm-usage",
        "--ozone-platform=wayland",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        # no-new-privileges makes Debian's setuid helper unavailable. The
        # container security profile permits Chromium's unprivileged user
        # namespace bootstrap; renderer seccomp remains enabled.
        "--disable-setuid-sandbox",
    ]


def sanitized_environment(policy: ProxyPolicy) -> dict[str, str]:
    """Remove Chromium flag injection and install the trusted proxy variables."""

    environment = dict(os.environ)
    for name in (
        "CHROMIUM_FLAGS",
        "CHROME_FLAGS",
        "GOOGLE_CHROME_SHIM",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    ):
        environment.pop(name, None)
    environment.update(proxy_environment(policy))
    environment["CHROMIUM_FLAGS"] = ""
    return environment


def _is_chromedriver_parent() -> bool:
    try:
        return Path(f"/proc/{os.getppid()}/exe").samefile("/usr/bin/chromedriver")
    except OSError:
        return False


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if len(arguments) == 1 and arguments[0] in _INTROSPECTION:
        os.execv(str(REAL_CHROMIUM), [str(REAL_CHROMIUM), *arguments])

    try:
        policy = load_proxy_environment()
        command = [
            str(REAL_CHROMIUM),
            *controlled_arguments(
                arguments,
                policy,
                trusted_chromedriver_parent=_is_chromedriver_parent(),
            ),
        ]
    except (RuntimeError, ValueError) as exc:
        print(f"chatds chromium policy error: {exc}", file=sys.stderr)
        return 78
    os.execve(str(REAL_CHROMIUM), command, sanitized_environment(policy))
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
