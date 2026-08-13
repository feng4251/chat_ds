"""Trusted re-attestation for Session-scoped Claude input attachments."""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from typing import Any


MAX_INPUT_ATTACHMENTS = 16
MAX_INPUT_ATTACHMENT_BYTES = 20 * 1024 * 1024
INPUT_ATTACHMENT_DIRECTORY = ".chatds/input-attachments"
INPUT_ATTACHMENT_PATH = re.compile(
    r"^\.chatds/input-attachments/([0-9a-f]{64})\.(jpg|png|gif|webp)$"
)


def _attachment_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_input_attachments(
    *,
    attachments: list[dict[str, Any]],
    workspace: Path,
    messages: list[dict[str, Any]] | None = None,
) -> None:
    """Re-attest receipts against one exact Session workspace.

    The Supervisor supplies ``messages`` to prove every manifest row is
    referenced by the lowered prompt. The PID 1 controller repeats the file
    attestation after acquiring the long Session mutation lease; it consumes
    the already-compiled prompt and therefore needs only the durable manifest.
    """

    if len(attachments) > MAX_INPUT_ATTACHMENTS:
        raise RuntimeError("input_attachment_count_invalid")
    expected_keys = {
        "schema", "kind", "path", "sha256", "media_type", "size_bytes",
        "width", "height",
    }
    extension_media = {
        "jpg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
    }
    receipts: dict[str, dict[str, Any]] = {}
    root = workspace.resolve(strict=True)
    root_info = workspace.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or workspace.is_symlink():
        raise RuntimeError("input_attachment_workspace_invalid")
    for receipt in attachments:
        if not isinstance(receipt, dict) or set(receipt) != expected_keys:
            raise RuntimeError("input_attachment_receipt_invalid")
        relative = receipt.get("path")
        match = INPUT_ATTACHMENT_PATH.fullmatch(
            relative if isinstance(relative, str) else ""
        )
        digest = receipt.get("sha256")
        size = receipt.get("size_bytes")
        width = receipt.get("width")
        height = receipt.get("height")
        if (
            match is None
            or receipt.get("schema") != "chatds.input-attachment.v1"
            or receipt.get("kind") != "image"
            or not isinstance(digest, str)
            or digest != match.group(1)
            or receipt.get("media_type") != extension_media[match.group(2)]
            or type(size) is not int
            or size < 1
            or size > MAX_INPUT_ATTACHMENT_BYTES
            or type(width) is not int
            or type(height) is not int
            or width < 1
            or height < 1
            or width > 32_768
            or height > 32_768
            or width * height > 100_000_000
            or digest in receipts
        ):
            raise RuntimeError("input_attachment_receipt_invalid")
        candidate = workspace / relative
        current = root
        for component in Path(relative).parts[:-1]:
            current = current / component
            try:
                info = current.lstat()
            except OSError as exc:
                raise RuntimeError("input_attachment_path_invalid") from exc
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise RuntimeError("input_attachment_path_invalid")
        try:
            candidate_info = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("input_attachment_path_invalid") from exc
        if (
            not stat.S_ISREG(candidate_info.st_mode)
            or stat.S_ISLNK(candidate_info.st_mode)
            or resolved != root.joinpath(*Path(relative).parts)
            or candidate_info.st_size != size
            or _attachment_sha256(candidate) != digest
        ):
            raise RuntimeError("input_attachment_digest_invalid")
        receipts[digest] = receipt

    if messages is None:
        return
    referenced: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise RuntimeError("input_attachment_message_invalid")
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image_url":
                raise RuntimeError("input_attachment_transport_unlowered")
            if item.get("type") != "image_file":
                continue
            receipt = item.get("image_file")
            if not isinstance(receipt, dict):
                raise RuntimeError("input_attachment_receipt_invalid")
            digest = receipt.get("sha256")
            if not isinstance(digest, str) or receipts.get(digest) != receipt:
                raise RuntimeError("input_attachment_receipt_invalid")
            referenced.add(digest)
    if referenced != set(receipts):
        raise RuntimeError("input_attachment_manifest_unreferenced")
