"""Shared, conservative Skill bundle identity projection.

New uploads persist explicit bundle metadata.  Historic rows are projected
only when one exact upload cohort has a unique root; ambiguous rows remain
independent Skills.  This module is shared by the public Skill API and the
Backend-to-Harness routing registry so UI grouping and execution routing use
the same identity rules.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Iterable


def _safe_single_path_component(value: Any) -> str | None:
    component = str(value or "")
    if (
        not component
        or component in {".", ".."}
        or len(component.encode("utf-8", "replace")) > 255
        or Path(component).parts != (component,)
        or Path(component).name != component
        or "/" in component
        or "\\" in component
    ):
        return None
    return component


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
    """Build the bounded package identity registry sent to the Harness.

    Input ordering defines precedence and callers pass newest rows first.
    Explicit/unambiguous bundle members retain their shared identity. Every
    other visible package receives a deterministic one-member primary group,
    so Backend and Harness can reconcile the complete DB-backed inventory
    without inventing a relationship between independent Skills.
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
        valid_bundle = (
            isinstance(bundle_id, str)
            and isinstance(bundle_role, str)
            and isinstance(bundle_root_name, str)
            and bundle_role in {"primary", "supporting"}
        )
        declared_bundle_metadata = any(
            value is not None
            for value in (bundle_id, bundle_role, bundle_root_name)
        )
        if not valid_bundle and not declared_bundle_metadata:
            identity = (
                "standalone-skill-package\0"
                f"{getattr(skill, 'user_id', '')}\0"
                f"{str(session_id) if session_id is not None else 'user'}\0"
                f"{getattr(skill, 'id', '')}\0{name}"
            )
            bundle_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            bundle_role = "primary"
            bundle_root_name = name
        elif not valid_bundle:
            # Preserve partially populated/corrupt persisted metadata so the
            # Harness validator can fail the ingress contract explicitly.
            # Reclassifying it as standalone would hide storage corruption.
            bundle_id = str(bundle_id or "")
            bundle_role = str(bundle_role or "")
            bundle_root_name = str(bundle_root_name or "")
        output.append({
            "name": name,
            "scope": "session" if session_id is not None else "user",
            "bundle_id": bundle_id,
            "bundle_role": bundle_role,
            "bundle_root_name": bundle_root_name,
        })
    return output


def content_address_skill_bundle_registry_rows(
    registry: Iterable[dict[str, Any]],
    skills: Iterable[Any],
    skills_data_dir: Path,
) -> list[dict[str, Any]]:
    """Bind Backend registry identities to exact on-disk SKILL.md bytes.

    Missing, unsafe, or unreadable manifests deliberately produce a null
    digest.  The Harness treats a content-addressed row without one valid
    digest as an ingress integrity failure before model/tool dispatch.  This
    makes a split Backend/Harness data mount observable instead of silently
    degrading an installed Skill to direct chat.
    """

    rows = list(skills)
    by_key: dict[tuple[str, str], Any] = {}
    for skill in rows:
        name = str(getattr(skill, "name", "") or "")
        scope = (
            "session"
            if getattr(skill, "session_id", None) is not None
            else "user"
        )
        if name:
            by_key.setdefault((scope, name), skill)

    output: list[dict[str, Any]] = []
    for raw_row in registry:
        row = dict(raw_row)
        key = (
            str(row.get("scope") or ""),
            str(row.get("name") or ""),
        )
        skill = by_key.get(key)
        digest: str | None = None
        name = _safe_single_path_component(key[1])
        user_id = _safe_single_path_component(
            getattr(skill, "user_id", "") if skill is not None else ""
        )
        raw_session_id = (
            getattr(skill, "session_id", None)
            if skill is not None else None
        )
        session_id = (
            None
            if raw_session_id is None
            else _safe_single_path_component(raw_session_id)
        )
        if (
            skill is not None
            and name is not None
            and user_id is not None
            and (raw_session_id is None or session_id is not None)
        ):
            root = Path(skills_data_dir) / user_id
            if session_id is not None:
                root = root / str(session_id)
            manifest = root / name / "SKILL.md"
            try:
                resolved_root = root.resolve(strict=True)
                resolved_manifest = manifest.resolve(strict=True)
                resolved_manifest.relative_to(resolved_root)
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(manifest, flags)
                try:
                    metadata = os.fstat(descriptor)
                    if stat.S_ISREG(metadata.st_mode):
                        hasher = hashlib.sha256()
                        while chunk := os.read(descriptor, 1024 * 1024):
                            hasher.update(chunk)
                        digest = hasher.hexdigest()
                finally:
                    os.close(descriptor)
            except (OSError, RuntimeError, ValueError):
                digest = None
        row["skill_md_sha256"] = digest
        output.append(row)
    return output
