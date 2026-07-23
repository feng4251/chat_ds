"""Assert that all removed cross-process IPC syscall families fail closed."""

import ctypes
import errno
import platform


libc = ctypes.CDLL(None, use_errno=True)
syscall = libc.syscall
syscall.restype = ctypes.c_long

SYSCALLS = {
    "x86_64": {
        "shmget": 29,
        "shmctl": 31,
        "semget": 64,
        "semop": 65,
        "semctl": 66,
        "msgget": 68,
        "msgsnd": 69,
        "msgrcv": 70,
        "msgctl": 71,
        "mq_open": 240,
        "mq_unlink": 241,
    },
    "aarch64": {
        "mq_open": 180,
        "mq_unlink": 181,
        "msgget": 186,
        "msgctl": 187,
        "msgrcv": 188,
        "msgsnd": 189,
        "semget": 190,
        "semctl": 191,
        "semop": 193,
        "shmget": 194,
        "shmctl": 195,
    },
}
machine = platform.machine().lower()
if machine not in SYSCALLS:
    raise RuntimeError(f"unsupported IPC syscall fixture architecture: {machine}")


def denied(name: str, *arguments: int) -> None:
    ctypes.set_errno(0)
    result = syscall(
        ctypes.c_long(SYSCALLS[machine][name]),
        *(ctypes.c_long(argument) for argument in arguments),
    )
    actual_errno = ctypes.get_errno()
    assert result == -1, f"{name} unexpectedly succeeded"
    assert actual_errno == errno.EPERM, (
        f"{name} returned errno {actual_errno}, expected EPERM"
    )


# Invalid object identities prevent resource creation even if this fixture is
# accidentally run without seccomp. Under the checked profile, filtering
# happens before the kernel validates those identities and returns EPERM.
denied("shmctl", -1, 2, 0)
denied("shmget", 0, 0, 0)
denied("msgctl", -1, 2, 0)
denied("msgget", -1, 0)
denied("msgsnd", -1, 0, 0, 0)
denied("msgrcv", -1, 0, 0, 0, 0)
denied("semctl", -1, 0, 2, 0)
denied("semget", 0, 0, 0)
denied("semop", -1, 0, 0)
denied("mq_open", 0, 0)
denied("mq_unlink", 0)
print("ipc-denied-ok")
