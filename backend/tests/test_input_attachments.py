import base64
import json
import stat
from pathlib import Path

import pytest

from agent_engines.input_attachments import (
    InputAttachmentError,
    materialize_message_attachments,
)


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _data_url(payload: bytes = _PNG_1X1, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64,{base64.b64encode(payload).decode('ascii')}"


def _messages(url: str | None = None) -> list[dict]:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Extract the warehouse label."},
            {
                "type": "image_url",
                "image_url": {"url": url or _data_url()},
            },
        ],
    }]


def test_materializes_content_addressed_image_without_forwarding_base64(
    tmp_path: Path,
):
    workspace = tmp_path / "tenant-a" / "session-a" / "workspace"
    workspace.mkdir(parents=True)

    first = materialize_message_attachments(_messages(), workspace=workspace)
    second = materialize_message_attachments(_messages(), workspace=workspace)

    assert first == second
    assert len(first.attachments) == 1
    receipt = first.attachments[0]
    assert receipt["schema"] == "chatds.input-attachment.v1"
    assert receipt["kind"] == "image"
    assert receipt["media_type"] == "image/png"
    assert receipt["size_bytes"] == len(_PNG_1X1)
    assert receipt["width"] == 1
    assert receipt["height"] == 1
    assert receipt["path"].startswith(".chatds/input-attachments/")
    attachment = workspace / receipt["path"]
    assert attachment.read_bytes() == _PNG_1X1
    assert stat.S_IMODE(attachment.stat().st_mode) == 0o444

    lowered = first.messages[0]["content"][1]
    assert lowered == {"type": "image_file", "image_file": receipt}
    serialized = json.dumps(first.messages, ensure_ascii=False)
    assert "data:image" not in serialized
    assert "base64" not in serialized


def test_deduplicates_repeated_content_but_preserves_message_positions(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    messages = _messages() + [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Compare the renamed inventory label."},
            {"type": "image_url", "image_url": {"url": _data_url()}},
        ],
    }]

    projection = materialize_message_attachments(messages, workspace=workspace)

    assert len(projection.attachments) == 1
    first = projection.messages[0]["content"][1]["image_file"]
    second = projection.messages[1]["content"][1]["image_file"]
    assert first == second == projection.attachments[0]


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://example.invalid/label.png", "attachment_url_not_supported"),
        ("data:image/png;base64,%%%", "attachment_base64_invalid"),
        (_data_url(b"not a png"), "attachment_media_mismatch"),
        (_data_url(_PNG_1X1, "image/svg+xml"), "attachment_media_type_unsupported"),
    ],
)
def test_rejects_ambiguous_or_untrusted_image_transport(
    tmp_path: Path,
    url: str,
    code: str,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(InputAttachmentError) as raised:
        materialize_message_attachments(_messages(url), workspace=workspace)

    assert raised.value.code == code
    assert not (workspace / ".chatds").exists()


def test_content_address_collision_or_mutation_fails_without_overwrite(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projection = materialize_message_attachments(_messages(), workspace=workspace)
    path = workspace / projection.attachments[0]["path"]
    path.chmod(0o600)
    path.write_bytes(b"tampered")

    with pytest.raises(InputAttachmentError) as raised:
        materialize_message_attachments(_messages(), workspace=workspace)

    assert raised.value.code == "attachment_digest_conflict"
    assert path.read_bytes() == b"tampered"


def test_attachment_directory_cannot_escape_through_symlink(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".chatds").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InputAttachmentError) as raised:
        materialize_message_attachments(_messages(), workspace=workspace)

    assert raised.value.code == "attachment_workspace_path_unsafe"
    assert list(outside.iterdir()) == []


def test_identical_images_remain_isolated_between_session_roots(tmp_path: Path):
    first_workspace = tmp_path / "tenant" / "session-one" / "workspace"
    second_workspace = tmp_path / "tenant" / "session-two" / "workspace"
    first_workspace.mkdir(parents=True)
    second_workspace.mkdir(parents=True)

    first = materialize_message_attachments(_messages(), workspace=first_workspace)

    relative = first.attachments[0]["path"]
    assert (first_workspace / relative).is_file()
    assert not (second_workspace / relative).exists()


def test_rejects_excessive_image_dimensions_before_workspace_write(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    oversized_header = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + (40_000).to_bytes(4, "big")
        + (40_000).to_bytes(4, "big")
    )

    with pytest.raises(InputAttachmentError) as raised:
        materialize_message_attachments(
            _messages(_data_url(oversized_header)),
            workspace=workspace,
        )

    assert raised.value.code == "attachment_dimensions_limit"
    assert not (workspace / ".chatds").exists()


def test_rejects_attachment_count_before_creating_an_unbounded_manifest(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = [{"type": "text", "text": "Compare inventory labels."}]
    content.extend(
        {"type": "image_url", "image_url": {"url": _data_url()}}
        for _ in range(17)
    )

    with pytest.raises(InputAttachmentError) as raised:
        materialize_message_attachments(
            [{"role": "user", "content": content}],
            workspace=workspace,
        )

    assert raised.value.code == "attachment_count_limit"
    assert not (workspace / ".chatds").exists()
