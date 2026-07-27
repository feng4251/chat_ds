import asyncio
import io
import stat
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import skill_frontmatter
from models import SkillPackage
from routers import skill_router


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class _Rows:
    def all(self):
        return []


class _FakeDb:
    def __init__(self):
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, _statement):
        return _Rows()

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def test_unknown_and_binary_extensions_are_preserved_as_inert_resources(tmp_path: Path):
    skill_md = (
        b"---\nname: resource-package\ndescription: binary fixture\n---\n"
        b"Use the declared assets exactly.\n"
    )
    pdf = b"%PDF-1.7\x00fixture"
    workbook = b"PK\x03\x04xlsx-fixture"
    opaque = b"\x00\xff\x10opaque"
    contents = _zip_bytes(
        {
            "package/SKILL.md": skill_md,
            "package/assets/source.pdf": pdf,
            "package/templates/model.xlsx": workbook,
            "package/assets/custom.opaque": opaque,
        }
    )
    db = _FakeDb()
    empty_mcp = {
        "registered": [],
        "skipped": [],
        "errors": [],
        "runtime": None,
    }

    with (
        patch.object(skill_router, "SKILLS_DATA_DIR", tmp_path),
        patch.object(skill_router, "_invalidate_skills_cache"),
        patch.object(
            skill_router,
            "_auto_register_mcp",
            new=AsyncMock(return_value=empty_mcp),
        ),
    ):
        result = asyncio.run(
            skill_router._process_skill_zip(
                contents,
                "resource-package.zip",
                None,
                None,
                SimpleNamespace(id="user-1"),
                db,
            )
        )

    root = tmp_path / "user-1" / "resource-package"
    assert result["success"] is True
    assert db.committed is True
    assert (root / "assets/source.pdf").read_bytes() == pdf
    assert (root / "templates/model.xlsx").read_bytes() == workbook
    assert (root / "assets/custom.opaque").read_bytes() == opaque


def test_nested_reference_skill_without_frontmatter_stays_in_parent_upload(tmp_path: Path):
    nested_example = b"# Example SKILL.md\n\nThis is tutorial input, not a package.\n"
    contents = _zip_bytes({
        "parent/SKILL.md": (
            b"---\nname: parent-skill\ndescription: Parent package fixture.\n---\n"
            b"Read references/example/SKILL.md as an example.\n"
        ),
        "parent/references/example/SKILL.md": nested_example,
    })
    db = _FakeDb()

    with (
        patch.object(skill_router, "SKILLS_DATA_DIR", tmp_path),
        patch.object(skill_router, "_invalidate_skills_cache"),
        patch.object(
            skill_router,
            "_auto_register_mcp",
            new=AsyncMock(return_value={
                "registered": [], "skipped": [], "errors": [], "runtime": None,
            }),
        ),
    ):
        result = asyncio.run(
            skill_router._process_skill_zip(
                contents,
                "parent-skill.zip",
                None,
                None,
                SimpleNamespace(id="user-1"),
                db,
            )
        )

    installed = tmp_path / "user-1" / "parent-skill"
    assert result["installed_count"] == 1
    assert [skill["name"] for skill in result["skills"]] == ["parent-skill"]
    assert (installed / "references/example/SKILL.md").read_bytes() == nested_example


def test_nested_reference_skill_with_frontmatter_is_not_split_from_parent(tmp_path: Path):
    nested_example = (
        b"---\nname: nested-example\ndescription: Valid but inert example manifest.\n---\n"
        b"# Example only\n"
    )
    contents = _zip_bytes({
        "parent/SKILL.md": (
            b"---\nname: parent-skill\ndescription: Parent package fixture.\n---\n"
            b"Read references/example/SKILL.md as an example.\n"
        ),
        "parent/references/example/SKILL.md": nested_example,
    })
    db = _FakeDb()

    with (
        patch.object(skill_router, "SKILLS_DATA_DIR", tmp_path),
        patch.object(skill_router, "_invalidate_skills_cache"),
        patch.object(
            skill_router,
            "_auto_register_mcp",
            new=AsyncMock(return_value={
                "registered": [], "skipped": [], "errors": [], "runtime": None,
            }),
        ),
    ):
        result = asyncio.run(
            skill_router._process_skill_zip(
                contents,
                "parent-skill.zip",
                None,
                None,
                SimpleNamespace(id="user-1"),
                db,
            )
        )

    installed = tmp_path / "user-1" / "parent-skill"
    assert result["installed_count"] == 1
    assert [skill["name"] for skill in result["skills"]] == ["parent-skill"]
    assert (installed / "references/example/SKILL.md").read_bytes() == nested_example
    assert not (tmp_path / "user-1" / "nested-example").exists()


def test_multi_skill_zip_persists_stable_primary_and_supporting_identity(
    tmp_path: Path,
):
    contents = _zip_bytes({
        "main/SKILL.md": (
            b"---\nname: main-skill\ndescription: Main workflow.\n---\n"
            b"Run the workflow.\n"
        ),
        "skills-bundle/helper/SKILL.md": (
            b"---\nname: helper-skill\ndescription: Supporting capability.\n---\n"
            b"Support the main workflow.\n"
        ),
    })
    db = _FakeDb()

    with (
        patch.object(skill_router, "SKILLS_DATA_DIR", tmp_path),
        patch.object(skill_router, "_invalidate_skills_cache"),
        patch.object(
            skill_router,
            "_auto_register_mcp",
            new=AsyncMock(return_value={
                "registered": [], "skipped": [], "errors": [], "runtime": None,
            }),
        ),
    ):
        result = asyncio.run(
            skill_router._process_skill_zip(
                contents,
                "workflow.zip",
                None,
                None,
                SimpleNamespace(id="user-1"),
                db,
            )
        )

    by_name = {skill["name"]: skill for skill in result["skills"]}
    assert by_name["main-skill"]["bundle_role"] == "primary"
    assert by_name["helper-skill"]["bundle_role"] == "supporting"
    assert (
        by_name["main-skill"]["bundle_id"]
        == by_name["helper-skill"]["bundle_id"]
    )
    assert by_name["helper-skill"]["bundle_root_name"] == "main-skill"
    assert (
        by_name["helper-skill"]["bundle_source_path"]
        == "skills-bundle/helper/SKILL.md"
    )

    rows = {skill.name: skill for skill in db.added}
    assert rows["main-skill"].bundle_role == "primary"
    assert rows["helper-skill"].bundle_role == "supporting"
    assert rows["helper-skill"].bundle_id == rows["main-skill"].bundle_id


def test_equal_depth_multi_skill_zip_does_not_invent_a_primary():
    contents = _zip_bytes({
        "one/SKILL.md": (
            b"---\nname: skill-one\ndescription: First independent Skill.\n---\n"
        ),
        "two/SKILL.md": (
            b"---\nname: skill-two\ndescription: Second independent Skill.\n---\n"
        ),
    })
    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        manifests = skill_router._discover_skill_manifests(
            archive,
            skill_router._zip_file_entries(archive),
        )

    metadata = skill_router._bundle_manifest_metadata(contents, manifests)
    assert metadata["skill-one"]["bundle_role"] == "primary"
    assert metadata["skill-two"]["bundle_role"] == "primary"
    assert (
        metadata["skill-one"]["bundle_id"]
        != metadata["skill-two"]["bundle_id"]
    )


def test_legacy_bundle_projection_requires_one_root_in_exact_upload_cohort():
    created_at = datetime(2026, 7, 24, 8, 36, 49)
    primary = SimpleNamespace(
        id="main",
        user_id="user",
        session_id="conversation",
        name="main-skill",
        category=None,
        bundle_id=None,
        created_at=created_at,
    )
    child = SimpleNamespace(
        id="child",
        user_id="user",
        session_id="conversation",
        name="helper-skill",
        category="skills-bundle",
        bundle_id=None,
        created_at=created_at,
    )
    independent = SimpleNamespace(
        id="independent",
        user_id="user",
        session_id="conversation",
        name="browser-skill",
        category=None,
        bundle_id=None,
        created_at=datetime(2026, 7, 27, 0, 52, 50),
    )

    projected = skill_router.legacy_bundle_projection(
        [primary, child, independent]
    )
    assert projected["main"]["bundle_role"] == "primary"
    assert projected["child"]["bundle_role"] == "supporting"
    assert projected["main"]["bundle_id"] == projected["child"]["bundle_id"]
    assert "independent" not in projected

    second_root = SimpleNamespace(
        **{
            **primary.__dict__,
            "id": "second-root",
            "name": "second-root",
        }
    )
    assert skill_router.legacy_bundle_projection(
        [primary, second_root, child]
    ) == {}


def test_skill_package_schema_exposes_bundle_identity_columns():
    columns = SkillPackage.__table__.c
    assert columns.bundle_id.type.length == 64
    assert columns.bundle_role.type.length == 16
    assert columns.bundle_root_name.type.length == 128
    assert columns.bundle_source_path.type.length == 512


def test_zip_resource_bounds_fail_closed():
    contents = _zip_bytes({"package/SKILL.md": b"abc", "package/a.bin": b"123"})
    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        with patch.object(skill_router, "MAX_FILE_SIZE", 2):
            with pytest.raises(HTTPException, match="resource too large"):
                skill_router._zip_file_entries(archive)

    contents = _zip_bytes({"package/a.bin": b"123", "package/b.bin": b"456"})
    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        with patch.object(skill_router, "MAX_UNCOMPRESSED_ZIP_SIZE", 5):
            with pytest.raises(HTTPException, match="uncompressed size limit"):
                skill_router._zip_file_entries(archive)


def test_zip_symlink_and_invalid_utf8_skill_manifest_are_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        link = zipfile.ZipInfo("package/assets/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        with pytest.raises(HTTPException, match="Symbolic links"):
            skill_router._zip_file_entries(archive)

    contents = _zip_bytes({"package/SKILL.md": b"---\nname: bad\n---\n\xff"})
    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        entries = skill_router._zip_file_entries(archive)
        with pytest.raises(HTTPException, match="valid UTF-8"):
            skill_router._discover_skill_manifests(archive, entries)


def test_file_policy_is_extension_agnostic_but_reports_known_binary_hint():
    assert skill_router._is_allowed_skill_file("assets/source.pdf") == (True, True)
    assert skill_router._is_allowed_skill_file("templates/model.xlsx") == (True, True)
    assert skill_router._is_allowed_skill_file("assets/custom.opaque") == (True, False)


def test_skill_manifest_uses_root_yaml_metadata_and_block_scalar_description():
    contents = _zip_bytes({
        "bundle/SKILL.md": (
            b"---\n"
            b"name: canonical-skill\n"
            b"description: |\n"
            b"  First description line.\n"
            b"  Second line: punctuation is preserved.\n"
            b"version: '2.3'\n"
            b"metadata:\n"
            b"  name: nested-name-must-not-win\n"
            b"  description: nested-description-must-not-win\n"
            b"---\n"
            b"Follow the Skill instructions.\n"
        ),
    })

    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        manifests = skill_router._discover_skill_manifests(
            archive,
            skill_router._zip_file_entries(archive),
        )

    assert len(manifests) == 1
    assert manifests[0]["name"] == "canonical-skill"
    assert manifests[0]["description"] == (
        "First description line.\nSecond line: punctuation is preserved."
    )
    assert manifests[0]["version"] == "2.3"


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("- name\n- not-a-mapping", "mapping at its root"),
        ("name: [unterminated", "Could not parse YAML frontmatter"),
        ("name: first\nname: second", "duplicate key"),
        ("metadata:\n  name: nested-only", "must have a 'name' field"),
    ],
)
def test_invalid_or_non_root_skill_metadata_fails_closed(
    frontmatter: str,
    message: str,
):
    contents = _zip_bytes({
        "bundle/SKILL.md": f"---\n{frontmatter}\n---\nbody\n".encode(),
    })

    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        entries = skill_router._zip_file_entries(archive)
        with pytest.raises(HTTPException, match=message) as exc_info:
            skill_router._discover_skill_manifests(archive, entries)

    assert exc_info.value.status_code == 400


def test_skill_frontmatter_source_and_alias_graph_are_bounded():
    oversized = "---\nname: bounded-skill\ndescription: " + ("x" * 80) + "\n---\n"
    with patch.object(skill_frontmatter, "MAX_FRONTMATTER_SOURCE_CHARS", 32):
        with pytest.raises(
            skill_frontmatter.SkillFrontmatterError,
            match="source-size limit",
        ):
            skill_router._parse_frontmatter(oversized)

    cyclic = (
        "---\n"
        "name: bounded-skill\n"
        "metadata: &metadata\n"
        "  self: *metadata\n"
        "---\n"
    )
    with pytest.raises(
        skill_frontmatter.SkillFrontmatterError,
        match="recursive alias cycle",
    ):
        skill_router._parse_frontmatter(cyclic)


@pytest.mark.parametrize(
    ("frontmatter", "message"),
    [
        ("name: Bad_Name\ndescription: valid", "Invalid skill name"),
        ("name: missing-description", "non-empty string 'description'"),
        (
            "name: bad-compatibility\ndescription: valid\ncompatibility: []",
            "compatibility.*1-500 character string",
        ),
        (
            "name: bad-metadata\ndescription: valid\nmetadata:\n  owner:\n    nested: value",
            "metadata.*values must be strings",
        ),
        (
            "name: bad-tools\ndescription: valid\nallowed-tools:\n  nested: value",
            "allowed-tools.*space-separated",
        ),
    ],
)
def test_nonstandard_manifest_semantics_fail_at_upload_boundary(
    frontmatter: str,
    message: str,
):
    contents = _zip_bytes({
        "bundle/SKILL.md": f"---\n{frontmatter}\n---\nbody\n".encode(),
    })
    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        entries = skill_router._zip_file_entries(archive)
        with pytest.raises(HTTPException, match=message) as exc_info:
            skill_router._discover_skill_manifests(archive, entries)
    assert exc_info.value.status_code == 400
