"""Shared, conservative Skill bundle identity projection.

New uploads persist explicit bundle metadata.  Historic rows are projected
only when one exact upload cohort has a unique root; ambiguous rows remain
independent Skills.  This module is shared by the public Skill API and the
Backend-to-Harness routing registry so UI grouping and execution routing use
the same identity rules.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable


def legacy_bundle_projection(skills: Iterable[Any]) -> dict[str, dict[str, str]]:
    """Infer legacy bundle identity only for an unambiguous upload cohort."""

    cohorts: dict[tuple[str | None, object], list[Any]] = {}
    for skill in skills:
        if getattr(skill, "bundle_id", None) or getattr(
            skill,
            "created_at",
            None,
        ) is None:
            continue
        cohorts.setdefault(
            (
                getattr(skill, "session_id", None),
                getattr(skill, "created_at"),
            ),
            [],
        ).append(skill)

    projected: dict[str, dict[str, str]] = {}
    for (_scope, _created_at), cohort in cohorts.items():
        children = [
            skill
            for skill in cohort
            if getattr(skill, "category", None) == "skills-bundle"
        ]
        primary_candidates = [
            skill
            for skill in cohort
            if getattr(skill, "category", None) != "skills-bundle"
        ]
        if not children or len(primary_candidates) != 1:
            continue
        primary = primary_candidates[0]
        identity = (
            "legacy-skill-bundle\0"
            f"{getattr(primary, 'user_id', '')}\0"
            f"{getattr(primary, 'session_id', None) or 'user'}\0"
            f"{getattr(primary, 'id', '')}"
        )
        legacy_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        for skill in cohort:
            skill_id = str(getattr(skill, "id", ""))
            projected[skill_id] = {
                "bundle_id": legacy_id,
                "bundle_role": (
                    "primary"
                    if skill_id == str(getattr(primary, "id", ""))
                    else "supporting"
                ),
                "bundle_root_name": str(getattr(primary, "name", "")),
                "bundle_source_path": "",
            }
    return projected


def resolved_bundle_metadata(
    skill: Any,
    projected: dict[str, dict[str, str]],
) -> dict[str, str | None]:
    """Resolve persisted metadata first, then one conservative projection."""

    legacy = projected.get(str(getattr(skill, "id", "")), {})
    source_path = getattr(skill, "bundle_source_path", None)
    return {
        "bundle_id": (
            getattr(skill, "bundle_id", None)
            or legacy.get("bundle_id")
        ),
        "bundle_role": (
            getattr(skill, "bundle_role", None)
            or legacy.get("bundle_role")
        ),
        "bundle_root_name": (
            getattr(skill, "bundle_root_name", None)
            or legacy.get("bundle_root_name")
        ),
        "bundle_source_path": (
            source_path
            if source_path is not None
            else legacy.get("bundle_source_path")
        ),
    }


def skill_bundle_registry_rows(skills: Iterable[Any]) -> list[dict[str, str]]:
    """Build the bounded identity-only registry sent to the Harness.

    Input ordering defines precedence and callers pass newest rows first.
    Standalone or ambiguous historic Skills are omitted: absence means
    top-level, while a supporting role is never invented without a primary.
    """

    skill_rows = list(skills)
    projected = legacy_bundle_projection(skill_rows)
    seen: set[tuple[str | None, str]] = set()
    output: list[dict[str, str]] = []
    for skill in skill_rows:
        name = str(getattr(skill, "name", "") or "")
        session_id = getattr(skill, "session_id", None)
        key = (session_id, name)
        if not name or key in seen:
            continue
        seen.add(key)
        metadata = resolved_bundle_metadata(skill, projected)
        bundle_id = metadata.get("bundle_id")
        bundle_role = metadata.get("bundle_role")
        bundle_root_name = metadata.get("bundle_root_name")
        if (
            not isinstance(bundle_id, str)
            or not isinstance(bundle_role, str)
            or not isinstance(bundle_root_name, str)
            or bundle_role not in {"primary", "supporting"}
        ):
            continue
        output.append({
            "name": name,
            "scope": "session" if session_id else "user",
            "bundle_id": bundle_id,
            "bundle_role": bundle_role,
            "bundle_root_name": bundle_root_name,
        })
    return output
