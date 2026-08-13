"""Lower untrusted chat image transport into Session-scoped input files.

The database/UI representation remains a data URL so historical messages can
be rendered by the browser.  Agent engines receive only content-addressed
workspace receipts: raw image bytes never cross the Backend-to-Runner control
plane, and a model is never told that an attachment exists unless the file was
committed and verified first.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from workspace import atomic_write_bytes, safe_workspace_path_in_root


ATTACHMENT_SCHEMA = "chatds.input-attachment.v1"
ATTACHMENT_DIRECTORY = ".chatds/input-attachments"
MAX_IMAGE_ATTACHMENTS = 16
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_DIMENSION = 32_768
MAX_IMAGE_PIXELS = 100_000_000

_MEDIA_TYPES: dict[str, tuple[str, str]] = {
    "image/jpeg": ("jpeg", "jpg"),
    "image/jpg": ("jpeg", "jpg"),
    "image/png": ("png", "png"),
    "image/gif": ("gif", "gif"),
    "image/webp": ("webp", "webp"),
}
_CANONICAL_MEDIA_TYPE = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


class InputAttachmentError(ValueError):
    """Stable, non-secret rejection at the attachment ingress boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AttachmentProjection:
    messages: tuple[dict[str, Any], ...]
    attachments: tuple[dict[str, Any], ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _decode_data_url(value: object) -> tuple[str, str, bytes]:
    if not isinstance(value, str) or not value.startswith("data:"):
        raise InputAttachmentError(
            "attachment_url_not_supported",
            "Image attachments must be uploaded with the chat request.",
        )
    header, separator, encoded = value.partition(",")
    if not separator or not header.lower().endswith(";base64"):
        raise InputAttachmentError(
            "attachment_data_url_invalid",
            "The image attachment data URL is malformed.",
        )
    media_type = header[5:-7].strip().lower()
    media = _MEDIA_TYPES.get(media_type)
    if media is None:
        raise InputAttachmentError(
            "attachment_media_type_unsupported",
            "The uploaded image format is not supported.",
        )
    if not encoded or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4:
        raise InputAttachmentError(
            "attachment_size_limit",
            "The uploaded image exceeds the per-file limit.",
        )
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InputAttachmentError(
            "attachment_base64_invalid",
            "The uploaded image is not valid base64 data.",
        ) from exc
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise InputAttachmentError(
            "attachment_size_limit",
            "The uploaded image exceeds the per-file limit.",
        )
    kind, extension = media
    return kind, extension, payload


def _jpeg_dimensions(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 4 or payload[:3] != b"\xff\xd8\xff":
        return None
    offset = 2
    start_of_frame = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset < len(payload):
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            return None
        marker = payload[offset]
        offset += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if marker in {0xD9, 0xDA} or offset + 2 > len(payload):
            return None
        segment_length = int.from_bytes(payload[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            return None
        if marker in start_of_frame:
            if segment_length < 7:
                return None
            height = int.from_bytes(payload[offset + 3:offset + 5], "big")
            width = int.from_bytes(payload[offset + 5:offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def _webp_dimensions(payload: bytes) -> tuple[int, int] | None:
    if (
        len(payload) < 30
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WEBP"
    ):
        return None
    chunk = payload[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(payload[24:27], "little")
        height = 1 + int.from_bytes(payload[27:30], "little")
        return width, height
    if chunk == b"VP8L" and payload[20] == 0x2F:
        bits = int.from_bytes(payload[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if chunk == b"VP8 " and payload[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(payload[26:28], "little") & 0x3FFF
        height = int.from_bytes(payload[28:30], "little") & 0x3FFF
        return width, height
    return None


def _image_dimensions(kind: str, payload: bytes) -> tuple[int, int] | None:
    if kind == "png":
        if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return (
            int.from_bytes(payload[16:20], "big"),
            int.from_bytes(payload[20:24], "big"),
        )
    if kind == "gif":
        if len(payload) < 10 or payload[:6] not in {b"GIF87a", b"GIF89a"}:
            return None
        return (
            int.from_bytes(payload[6:8], "little"),
            int.from_bytes(payload[8:10], "little"),
        )
    if kind == "jpeg":
        return _jpeg_dimensions(payload)
    if kind == "webp":
        return _webp_dimensions(payload)
    return None


def _validated_image(value: object) -> tuple[str, str, bytes, int, int]:
    kind, extension, payload = _decode_data_url(value)
    dimensions = _image_dimensions(kind, payload)
    if dimensions is None:
        raise InputAttachmentError(
            "attachment_media_mismatch",
            "The image bytes do not match the declared media type.",
        )
    width, height = dimensions
    if (
        width < 1
        or height < 1
        or width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise InputAttachmentError(
            "attachment_dimensions_limit",
            "The uploaded image dimensions exceed the safety limit.",
        )
    return _CANONICAL_MEDIA_TYPE[kind], extension, payload, width, height


def _persist_attachment(
    *,
    workspace: Path,
    relative_path: str,
    payload: bytes,
    digest: str,
) -> None:
    try:
        destination = safe_workspace_path_in_root(workspace, relative_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputAttachmentError(
            "attachment_workspace_path_unsafe",
            "The Session attachment directory is unsafe.",
        ) from exc
    try:
        info = destination.lstat()
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise InputAttachmentError(
            "attachment_workspace_unavailable",
            "The Session attachment directory is unavailable.",
        ) from exc
    if info is not None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != len(payload)
            or _sha256_file(destination) != digest
        ):
            raise InputAttachmentError(
                "attachment_digest_conflict",
                "A content-addressed Session attachment was modified.",
            )
    else:
        try:
            atomic_write_bytes(destination, payload)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InputAttachmentError(
                "attachment_workspace_unavailable",
                "The Session attachment could not be committed.",
            ) from exc
    try:
        # The Backend commits as root, while the disposable Claude process
        # deliberately drops to an unprivileged worker identity.  Make the
        # immutable input readable (never writable) across that identity
        # boundary; the Turn additionally receives this child directory as a
        # read-only bind mount.
        os.chmod(destination, 0o444, follow_symlinks=False)
    except OSError as exc:
        raise InputAttachmentError(
            "attachment_workspace_unavailable",
            "The Session attachment could not be sealed.",
        ) from exc


def materialize_message_attachments(
    messages: Sequence[Mapping[str, Any]],
    *,
    workspace: Path,
) -> AttachmentProjection:
    """Replace image data URLs with verified Session workspace receipts.

    The caller must hold the Session workspace mutation lock. Repeating the
    operation is idempotent because paths are derived solely from content.
    """

    projected = copy.deepcopy(list(messages))
    receipts_by_digest: dict[str, dict[str, Any]] = {}
    attachment_count = 0
    total_bytes = 0
    pending: list[
        tuple[list[Any], int, str, str, bytes, int, int]
    ] = []
    for message in projected:
        if not isinstance(message, dict):
            raise InputAttachmentError(
                "attachment_message_invalid",
                "The engine message containing an attachment is invalid.",
            )
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for index, item in enumerate(content):
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            attachment_count += 1
            if attachment_count > MAX_IMAGE_ATTACHMENTS:
                raise InputAttachmentError(
                    "attachment_count_limit",
                    "The chat request contains too many image attachments.",
                )
            transport = item.get("image_url")
            url = transport.get("url") if isinstance(transport, dict) else None
            media_type, extension, payload, width, height = _validated_image(url)
            total_bytes += len(payload)
            if total_bytes > MAX_TOTAL_IMAGE_BYTES:
                raise InputAttachmentError(
                    "attachment_total_size_limit",
                    "The chat request contains too much image data.",
                )
            pending.append(
                (content, index, media_type, extension, payload, width, height)
            )

    # Validation is intentionally complete before the first workspace write,
    # so a malformed later attachment cannot leave a partial ingress commit.
    for content, index, media_type, extension, payload, width, height in pending:
        digest = hashlib.sha256(payload).hexdigest()
        receipt = receipts_by_digest.get(digest)
        if receipt is None:
            relative_path = (
                f"{ATTACHMENT_DIRECTORY}/{digest}.{extension}"
            )
            _persist_attachment(
                workspace=workspace,
                relative_path=relative_path,
                payload=payload,
                digest=digest,
            )
            receipt = {
                "schema": ATTACHMENT_SCHEMA,
                "kind": "image",
                "path": relative_path,
                "sha256": digest,
                "media_type": media_type,
                "size_bytes": len(payload),
                "width": width,
                "height": height,
            }
            receipts_by_digest[digest] = receipt
        content[index] = {
            "type": "image_file",
            "image_file": receipt,
        }
    return AttachmentProjection(
        messages=tuple(projected),
        attachments=tuple(receipts_by_digest.values()),
    )
