import asyncio
import base64
import hashlib
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import workspace
from routers import skill_router


def _skill_zip(*, name: str = "workspace-skill") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "bundle/SKILL.md",
            (
                "---\n"
                f"name: {name}\n"
                "description: Workspace archive fixture.\n"
                "version: '1.0'\n"
                "---\n"
                "Follow the fixture instructions.\n"
            ),
        )
        archive.writestr("bundle/references/evidence.txt", b"source evidence\n")
    return buffer.getvalue()


def _install_result(*, name: str = "workspace-skill") -> dict:
    skill = {
        "name": name,
        "description": "Workspace archive fixture.",
        "category": None,
        "version": "1.0",
        "session_id": "session-1",
    }
    return {
        "success": True,
        "skill": skill,
        "skills": [skill],
        "installed_count": 1,
        "mcp": {"registered": [], "skipped": [], "errors": [], "runtime": []},
        "mcp_by_skill": {},
        "runtime": [],
    }


class _ConversationResult:
    def __init__(self, conversation):
        self._conversation = conversation

    def scalar_one_or_none(self):
        return self._conversation


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _ConversationDb:
    def __init__(self):
        self.execute_count = 0
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count == 1:
            return _ConversationResult(
                SimpleNamespace(id="session-1", user_id="user-1")
            )
        return _RowsResult([])

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


class _RowsDb:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _RowsResult(self.rows)


@pytest.mark.parametrize(
    "filename",
    [
        "../skill.zip",
        "nested/skill.zip",
        r"nested\skill.zip",
        " skill.zip",
        "skill.zip ",
        "skill\n.zip",
        "not-a-skill.txt",
    ],
)
def test_skill_archive_filename_rejects_unsafe_or_non_zip_names(filename: str):
    with pytest.raises(HTTPException) as exc_info:
        skill_router._validate_skill_archive_filename(filename)
    assert exc_info.value.status_code == 400


def test_skill_archive_base64_decode_is_strict_and_bounded():
    assert skill_router._decode_skill_archive_base64("YWJj") == b"abc"
    for invalid in ("YWJj\n", "YWJj!", "not base64"):
        with pytest.raises(HTTPException, match="Invalid base64"):
            skill_router._decode_skill_archive_base64(invalid)

    with (
        patch.object(skill_router, "MAX_ZIP_SIZE", 2),
        patch.object(skill_router, "MAX_ZIP_BASE64_SIZE", 4),
    ):
        with pytest.raises(HTTPException, match="too large"):
            skill_router._decode_skill_archive_base64("YWJj")


def test_workspace_archive_create_repeat_and_conflict_are_no_clobber(tmp_path: Path):
    contents = _skill_zip()
    with patch.object(workspace, "WORKSPACE_ROOT", tmp_path):
        attachment, destination = skill_router._persist_session_skill_archive(
            contents=contents,
            filename="临床设计-v2.3.zip",
            user_id="user-1",
            session_id="session-1",
        )
        assert attachment == {
            "kind": "skill_archive",
            "filename": "临床设计-v2.3.zip",
            "path": "临床设计-v2.3.zip",
            "size": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(),
            "status": "created",
        }
        assert destination.read_bytes() == contents
        listed = workspace.list_workspace_files("user-1", "session-1")
        assert any(item["path"] == "临床设计-v2.3.zip" for item in listed)

        repeated, repeated_path = skill_router._persist_session_skill_archive(
            contents=contents,
            filename="临床设计-v2.3.zip",
            user_id="user-1",
            session_id="session-1",
        )
        assert repeated_path == destination
        assert repeated["status"] == "unchanged"

        with pytest.raises(HTTPException) as exc_info:
            skill_router._persist_session_skill_archive(
                contents=_skill_zip(name="different-skill"),
                filename="临床设计-v2.3.zip",
                user_id="user-1",
                session_id="session-1",
            )
        assert exc_info.value.status_code == 409
        assert destination.read_bytes() == contents


def test_json_session_upload_persists_archive_before_canonical_install(tmp_path: Path):
    contents = _skill_zip()
    body = skill_router.SkillUploadJson(
        filename="source-skill.zip",
        content_base64=base64.b64encode(contents).decode("ascii"),
        session_id="session-1",
    )
    expected_path = (
        tmp_path / "user-1" / "session-1" / "workspace" / "source-skill.zip"
    )

    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(skill_router, "SKILLS_DATA_DIR", tmp_path / "skills"),
        patch.object(skill_router, "_invalidate_skills_cache"),
        patch.object(
            skill_router,
            "_auto_register_mcp",
            new=AsyncMock(return_value={
                "registered": [],
                "skipped": [],
                "errors": [],
                "runtime": None,
            }),
        ),
    ):
        result = asyncio.run(
            skill_router.upload_skill_json(
                body,
                SimpleNamespace(id="user-1"),
                _ConversationDb(),
            )
        )

    assert result["installation_status"] == "installed"
    assert result["idempotent"] is False
    assert result["workspace_attachment"]["path"] == "source-skill.zip"
    assert result["workspace_attachment"]["status"] == "created"
    assert result["workspace_attachment"]["sha256"] == hashlib.sha256(contents).hexdigest()
    assert expected_path.read_bytes() == contents


def test_json_session_upload_rolls_back_new_archive_on_install_failure(tmp_path: Path):
    contents = _skill_zip()
    body = skill_router.SkillUploadJson(
        filename="source-skill.zip",
        content_base64=base64.b64encode(contents).decode("ascii"),
        session_id="session-1",
    )

    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            skill_router,
            "_process_skill_zip",
            new=AsyncMock(side_effect=HTTPException(409, "skill conflict")),
        ),
        patch.object(
            skill_router,
            "_exact_existing_bundle_result",
            new=AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                skill_router.upload_skill_json(
                    body,
                    SimpleNamespace(id="user-1"),
                    _ConversationDb(),
                )
            )

    assert exc_info.value.status_code == 409
    assert not (
        tmp_path / "user-1" / "session-1" / "workspace" / "source-skill.zip"
    ).exists()


def test_json_session_upload_keeps_archive_for_exact_idempotent_install(tmp_path: Path):
    contents = _skill_zip()
    body = skill_router.SkillUploadJson(
        filename="source-skill.zip",
        content_base64=base64.b64encode(contents).decode("ascii"),
        session_id="session-1",
    )
    idempotent_result = {
        **_install_result(),
        "idempotent": True,
        "installation_status": "already_installed",
    }

    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(
            skill_router,
            "_process_skill_zip",
            new=AsyncMock(return_value={
                **idempotent_result,
                "workspace_attachment": {
                    "kind": "skill_archive",
                    "filename": "source-skill.zip",
                    "path": "source-skill.zip",
                    "size": len(contents),
                    "sha256": hashlib.sha256(contents).hexdigest(),
                    "status": "unchanged",
                },
            }),
        ),
    ):
        result = asyncio.run(
            skill_router.upload_skill_json(
                body,
                SimpleNamespace(id="user-1"),
                _ConversationDb(),
            )
        )

    assert result["idempotent"] is True
    assert result["installation_status"] == "already_installed"
    assert result["workspace_attachment"]["status"] == "unchanged"


def test_json_session_upload_does_not_persist_invalid_skill_archive(tmp_path: Path):
    contents = b"not a zip"
    body = skill_router.SkillUploadJson(
        filename="invalid.zip",
        content_base64=base64.b64encode(contents).decode("ascii"),
        session_id="session-1",
    )
    installer = AsyncMock()

    with (
        patch.object(workspace, "WORKSPACE_ROOT", tmp_path),
        patch.object(skill_router, "_process_skill_zip", new=installer),
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                skill_router.upload_skill_json(
                    body,
                    SimpleNamespace(id="user-1"),
                    _ConversationDb(),
                )
            )

    assert exc_info.value.status_code == 400
    installer.assert_not_awaited()
    assert not (tmp_path / "user-1" / "session-1" / "workspace").exists()


def test_exact_existing_bundle_is_the_only_idempotent_install(tmp_path: Path):
    contents = _skill_zip()
    manifests, metadata = skill_router._validate_skill_archive(contents)
    manifest = manifests[0]
    member = metadata["workspace-skill"]
    canonical = tmp_path / "user-1" / "session-1" / "workspace-skill"
    (canonical / "references").mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        (canonical / "SKILL.md").write_bytes(archive.read("bundle/SKILL.md"))
    (canonical / "references/evidence.txt").write_bytes(b"source evidence\n")
    row = SimpleNamespace(
        name="workspace-skill",
        description="Workspace archive fixture.",
        category=None,
        version="1.0",
        bundle_id=member["bundle_id"],
        bundle_role=member["bundle_role"],
        bundle_root_name=member["bundle_root_name"],
        bundle_source_path=member["bundle_source_path"],
    )

    with patch.object(skill_router, "SKILLS_DATA_DIR", tmp_path):
        result = asyncio.run(
            skill_router._exact_existing_bundle_result(
                contents=contents,
                manifests=manifests,
                bundle_metadata=metadata,
                category=None,
                session_id="session-1",
                user=SimpleNamespace(id="user-1"),
                db=_RowsDb([row]),
            )
        )
        assert result is not None
        assert result["idempotent"] is True
        assert result["installation_status"] == "already_installed"

        (canonical / "unexpected.txt").write_text("drift", encoding="utf-8")
        mismatch = asyncio.run(
            skill_router._exact_existing_bundle_result(
                contents=contents,
                manifests=manifests,
                bundle_metadata=metadata,
                category=None,
                session_id="session-1",
                user=SimpleNamespace(id="user-1"),
                db=_RowsDb([row]),
            )
        )
        assert mismatch is None
