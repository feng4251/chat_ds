import unittest
from datetime import datetime
from types import SimpleNamespace

from skill_bundles import (
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
        self.assertEqual([], skill_bundle_registry_rows(rows))


if __name__ == "__main__":
    unittest.main()
