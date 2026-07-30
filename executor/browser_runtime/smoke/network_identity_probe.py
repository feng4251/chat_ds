"""Prove worker identity and the networkless/public-proxy topology in one lease."""

import json
import os
from pathlib import Path
import socket
import ssl
import sys
from urllib.parse import urlsplit


def direct_dial_is_blocked(host: str, port: int) -> str:
    try:
        connection = socket.create_connection((host, port), timeout=0.5)
    except OSError as exc:
        return type(exc).__name__
    connection.close()
    raise AssertionError(f"direct network dial unexpectedly succeeded: {host}:{port}")


def unix_authority_is_blocked(path: str) -> str:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(0.5)
    try:
        connection.connect(path)
    except OSError as exc:
        return type(exc).__name__
    finally:
        connection.close()
    raise AssertionError(f"worker unexpectedly opened controller authority: {path}")


def proxy_connect(
    proxy_host: str,
    proxy_port: int,
    host: str,
    port: int,
) -> tuple[socket.socket, bytes]:
    connection = socket.create_connection((proxy_host, proxy_port), timeout=5)
    connection.sendall(
        (
            f"CONNECT {host}:{port} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
    )
    response = bytearray()
    while b"\r\n\r\n" not in response and len(response) < 16_384:
        chunk = connection.recv(4096)
        if not chunk:
            break
        response.extend(chunk)
    return connection, bytes(response)


def rejected_proxy_connect(
    proxy_host: str,
    proxy_port: int,
    host: str,
    port: int,
) -> bytes:
    """Prove an unauthorized CONNECT cannot yield an upstream request.

    Policy v3 may acknowledge the local CONNECT before inspecting its inner
    HTTP request.  That 200 establishes only the worker-to-proxy inspection
    channel: DNS and the upstream connection remain deferred.  Complete the
    proxy-authenticated TLS leg and require the inner request to receive 403
    instead of mistaking the CONNECT acknowledgement for destination access.
    """

    connection, response = proxy_connect(
        proxy_host,
        proxy_port,
        host,
        port,
    )
    if response.startswith(b"HTTP/1.1 403"):
        connection.close()
        return response
    assert response.startswith(b"HTTP/1.1 200"), response
    context = ssl.create_default_context()
    with context.wrap_socket(connection, server_hostname=host) as tls:
        tls.settimeout(5)
        tls.sendall(
            (
                "HEAD / HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        return tls.recv(4096)


status = {}
for name, host in (
    ("public", "1.1.1.1"),
    ("private", "10.0.0.1"),
    ("metadata", "169.254.169.254"),
):
    status[f"direct_{name}"] = direct_dial_is_blocked(host, 443)

interfaces = sorted(path.name for path in Path("/sys/class/net").iterdir())
assert interfaces == ["lo"], interfaces
route_rows = Path("/proc/net/route").read_text(encoding="ascii").splitlines()
assert len(route_rows) == 1, route_rows

status["proxy_uds"] = unix_authority_is_blocked(
    "/run/chatds-skill-egress/proxy.sock"
)
status["controller_uds"] = unix_authority_is_blocked(
    "/run/chat-ds-executor/executor.sock"
)

proxy_url = urlsplit(os.environ["SKILL_EGRESS_PROXY_URL"])
assert proxy_url.scheme == "http", proxy_url
assert proxy_url.hostname in {"127.0.0.1", "localhost"}, proxy_url
assert proxy_url.port is not None, proxy_url
proxy_host = proxy_url.hostname
proxy_port = proxy_url.port

for name, host in (
    ("loopback", "127.0.0.1"),
    ("private", "10.0.0.1"),
    ("metadata", "169.254.169.254"),
):
    response = rejected_proxy_connect(
        proxy_host,
        proxy_port,
        host,
        80,
    )
    assert response.startswith(b"HTTP/1.1 403"), (name, response)
    status[f"proxy_{name}"] = "403"

tunnel, response = proxy_connect(proxy_host, proxy_port, "example.com", 443)
assert response.startswith(b"HTTP/1.1 200"), response
context = ssl.create_default_context()
with context.wrap_socket(tunnel, server_hostname="example.com") as tls:
    tls.settimeout(10)
    tls.sendall(
        b"HEAD / HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n"
    )
    public_response = tls.recv(4096)
assert public_response.startswith(b"HTTP/1.1 "), public_response
status["proxy_public"] = public_response.split(b"\r\n", 1)[0].decode("ascii")

if len(sys.argv) == 3:
    private_host = sys.argv[1]
    private_port = int(sys.argv[2])
    allowed_private, response = proxy_connect(
        proxy_host,
        proxy_port,
        private_host,
        private_port,
    )
    allowed_private.close()
    assert response.startswith(b"HTTP/1.1 200"), response
    status["proxy_allowlisted_private"] = "200"
elif len(sys.argv) != 1:
    raise AssertionError("expected no args or private host/port")

proc_status = {}
for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
    if ":" in line:
        key, value = line.split(":", 1)
        proc_status[key] = value.strip()
assert os.geteuid() == 65529
assert os.getegid() == 65529
assert os.getgroups() == []
for capability in ("CapPrm", "CapEff", "CapAmb"):
    assert int(proc_status[capability], 16) == 0, (capability, proc_status[capability])
assert "EXECUTOR_V2_AUTH_TOKEN" not in os.environ
assert os.environ["SKILL_EGRESS_PROXY_URL"] == (
    f"http://{proxy_host}:{proxy_port}"
)

status.update(
    {
        "euid": os.geteuid(),
        "egid": os.getegid(),
        "groups": os.getgroups(),
        "cap_eff": proc_status["CapEff"],
        "interfaces": interfaces,
        "route_rows": len(route_rows) - 1,
    }
)
print(json.dumps(status, sort_keys=True))
print("network-identity-ok")
