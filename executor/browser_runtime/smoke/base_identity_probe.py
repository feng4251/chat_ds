"""Assert the generic networkless base executor identity boundary."""

import os
from pathlib import Path
import socket


assert os.geteuid() == 65528
assert os.getegid() == 65528
assert os.getgroups() == []
assert "EXECUTOR_V2_AUTH_TOKEN" not in os.environ
assert "SKILL_EGRESS_PROXY_URL" not in os.environ
assert sorted(path.name for path in Path("/sys/class/net").iterdir()) == ["lo"]
assert len(Path("/proc/net/route").read_text().splitlines()) == 1
for root in ("/tmp", "/dev/shm"):
    assert not os.access(root, os.W_OK), root

status = Path("/proc/self/status").read_text()
for field in ("CapPrm", "CapEff", "CapAmb"):
    value = next(
        line.split()[1]
        for line in status.splitlines()
        if line.startswith(f"{field}:")
    )
    assert int(value, 16) == 0, (field, value)

for address in (("1.1.1.1", 443), ("10.0.0.1", 443), ("169.254.169.254", 80)):
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        probe.connect(address)
    except OSError:
        pass
    else:
        raise AssertionError(f"base worker reached direct destination {address}")
    finally:
        probe.close()

print("base-identity-ok")
