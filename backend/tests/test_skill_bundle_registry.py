import hashlib
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from skill_bundles import (
    content_address_skill_bundle_registry_rows,
    legacy_bundle_projection,
    skill_bundle_registry_rows,
)


def _skill(
    name: str,
    *,
    row_id: str,
    category: str | None = None,
    session_id: str | None = "session",
    created_at: datetime | None = None,
    bundle_id: str | None = None,
    bundle_role: str | None = None,
    bundle_root_name: str | None = None,
):
    return SimpleNamespace(
        id=row_id,
        user_id="user",
        name=name,
        category=category,
        session_id=session_id,
        created_at=created_at,
        bundle_id=bundle_id,
        bundle_role=bundle_role,
        bundle_root_name=bundle_root_name,
        bundle_source_path=None,
    )


class SkillBundleRegistryTests(unittest.TestCase):
    def test_explicit_bundle_rows_are_shared_with_harness_registry(self):
        bundle_id = "a" * 64
        rows = [
            _skill(
                "root",
                row_id="root-id",
                bundle_id=bundle_id,
                bundle_role="primary",
                bundle_root_name="root",
            ),
            _skill(
                "support",
                row_id="support-id",
                bundle_id=bundle_id,
                bundle_role="supporting",
                bundle_root_name="root",
            ),
        ]

        registry = skill_bundle_registry_rows(rows)

        self.assertEqual(["root", "support"], [row["name"] for row in registry])
        self.assertEqual(
            ["primary", "supporting"],
            [row["bundle_role"] for row in registry],
        )
        self.assertTrue(all(row["scope"] == "session" for row in registry))

    def test_unambiguous_legacy_cohort_projects_one_primary(self):
        created_at = datetime(2026, 7, 27, 12, 0, 0)
        rows = [
            _skill("root", row_id="root-id", created_at=created_at),
            _skill(
                "support",
                row_id="support-id",
                category="skills-bundle",
                created_at=created_at,
            ),
        ]

        projected = legacy_bundle_projection(rows)
        registry = skill_bundle_registry_rows(rows)

        self.assertEqual("primary", projected["root-id"]["bundle_role"])
        self.assertEqual(
            "supporting",
            projected["support-id"]["bundle_role"],
        )
        self.assertEqual(2, len(registry))

    def test_ambiguous_legacy_cohort_remains_independent(self):
        created_at = datetime(2026, 7, 27, 12, 0, 0)
        rows = [
            _skill("root-a", row_id="a", created_at=created_at),
            _skill("root-b", row_id="b", created_at=created_at),
            _skill(
                "support",
                row_id="support",
                category="skills-bundle",
                created_at=created_at,
            ),
        ]

        self.assertEqual({}, legacy_bundle_projection(rows))
        registry = skill_bundle_registry_rows(rows)
        self.assertEqual(3, len(registry))
        self.assertTrue(all(
            row["bundle_role"] == "primary"
            and row["bundle_root_name"] == row["name"]
            for row in registry
        ))
        self.assertEqual(3, len({row["bundle_id"] for row in registry}))

    def test_standalone_package_receives_stable_one_member_identity(self):
        row = _skill("standalone", row_id="standalone-id")

        first = skill_bundle_registry_rows([row])
        second = skill_bundle_registry_rows([row])

        self.assertEqual(first, second)
        self.assertEqual(1, len(first))
        self.assertEqual("primary", first[0]["bundle_role"])
        self.assertEqual("standalone", first[0]["bundle_root_name"])
        self.assertEqual(64, len(first[0]["bundle_id"]))

    def test_partial_bundle_metadata_is_not_silently_reclassified(self):
        row = _skill(
            "partial",
            row_id="partial-id",
            bundle_id="a" * 64,
        )

        registry = skill_bundle_registry_rows([row])

        self.assertEqual("a" * 64, registry[0]["bundle_id"])
        self.assertEqual("", registry[0]["bundle_role"])
        self.assertEqual("", registry[0]["bundle_root_name"])

    def test_registry_rows_are_bound_to_exact_manifest_bytes(self):
        row = _skill(
            "root",
            row_id="root-id",
            bundle_id="a" * 64,
            bundle_role="primary",
            bundle_root_name="root",
        )
        with TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "user" / "session" / "root" / "SKILL.md"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("# bounded workflow\n", encoding="utf-8")

            registry = content_address_skill_bundle_registry_rows(
                skill_bundle_registry_rows([row]),
                [row],
                Path(temp_dir),
            )

        self.assertEqual(
            hashlib.sha256(b"# bounded workflow\n").hexdigest(),
            registry[0]["skill_md_sha256"],
        )

    def test_missing_manifest_is_explicitly_unavailable(self):
        row = _skill(
            "root",
            row_id="root-id",
            bundle_id="a" * 64,
            bundle_role="primary",
            bundle_root_name="root",
        )
        with TemporaryDirectory() as temp_dir:
            registry = content_address_skill_bundle_registry_rows(
                skill_bundle_registry_rows([row]),
                [row],
                Path(temp_dir),
            )

        self.assertIsNone(registry[0]["skill_md_sha256"])

    def test_symlink_manifest_is_not_content_addressed(self):
        row = _skill(
            "root",
            row_id="root-id",
            bundle_id="a" * 64,
            bundle_role="primary",
            bundle_root_name="root",
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / "outside.md"
            outside.write_text("private", encoding="utf-8")
            manifest = root / "user" / "session" / "root" / "SKILL.md"
            manifest.parent.mkdir(parents=True)
            manifest.symlink_to(outside)
            registry = content_address_skill_bundle_registry_rows(
                skill_bundle_registry_rows([row]),
                [row],
                root,
            )

        self.assertIsNone(registry[0]["skill_md_sha256"])


if __name__ == "__main__":
    unittest.main()
