"""Bounded identity proof for a container-visible persistent storage root."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


_IDENTITY_DOMAIN = b"chatds.storage-root.v1\0"


def storage_root_attestation(path: str | os.PathLike[str]) -> dict[str, object]:
    """Return a path-free identity for one directory inode.

    Backend and Harness run in separate containers, so comparing configured
    path strings is insufficient: two relative bind mounts can both be named
    ``/app/data`` while resolving to different host directories.  A bind mount
    of the same host directory exposes the same device/inode pair to both
    containers.  Hashing that pair provides a stable comparison value without
    publishing the host path.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(Path(path), flags)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return {
            "version": 1,
            "available": False,
            "identity_sha256": "",
        }
    try:
        try:
            metadata = os.fstat(fd)
        except OSError:
            return {
                "version": 1,
                "available": False,
                "identity_sha256": "",
            }
        if not stat.S_ISDIR(metadata.st_mode):
            return {
                "version": 1,
                "available": False,
                "identity_sha256": "",
            }
        identity = hashlib.sha256(
            _IDENTITY_DOMAIN
            + str(int(metadata.st_dev)).encode("ascii")
            + b"\0"
            + str(int(metadata.st_ino)).encode("ascii")
        ).hexdigest()
        return {
            "version": 1,
            "available": True,
            "identity_sha256": identity,
        }
    finally:
        os.close(fd)


def storage_attestations_match(
    local: object,
    remote: object,
) -> bool:
    """Strictly compare two available v1 storage attestations."""

    if not isinstance(local, dict) or not isinstance(remote, dict):
        return False
    local_identity = local.get("identity_sha256")
    remote_identity = remote.get("identity_sha256")
    return bool(
        local.get("version") == 1
        and remote.get("version") == 1
        and local.get("available") is True
        and remote.get("available") is True
        and isinstance(local_identity, str)
        and local_identity
        and isinstance(remote_identity, str)
        and remote_identity
        and local_identity == remote_identity
    )
