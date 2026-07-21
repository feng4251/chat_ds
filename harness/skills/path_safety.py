"""Filesystem boundary checks for Skill package resources.

Skill packages are data supplied to the harness.  A path that merely reports
``is_file()`` is not sufficient: a symlink in the file itself or in any
package-relative ancestor can redirect a read outside the package.  This
module centralizes the stricter invariant used by discovery, compilation, and
resource loading.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class SkillPathCheck:
    """Result of validating a package root or package resource."""

    path: Path | None
    code: str | None = None
    message: str | None = None

    @property
    def valid(self) -> bool:
        return self.path is not None and self.code is None


def _absolute_lexical(path: Path) -> Path:
    """Return an absolute, normalized path without resolving symlinks."""
    return Path(os.path.abspath(os.fspath(path)))


def _first_symlink_component(path: Path) -> Path | None:
    """Return the first existing symlink component, including ``path``."""
    absolute = _absolute_lexical(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return current
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            return current
    return None


def validate_skill_root(skill_root: Path) -> SkillPathCheck:
    """Establish a canonical, real, non-symlink Skill package root.

    The lexical package path and each of its existing ancestors must be real
    directories.  Rejecting a symlink at this boundary prevents a caller from
    presenting a package alias whose apparent location differs from the files
    that will actually be read.
    """
    lexical_root = _absolute_lexical(Path(skill_root))
    symlink = _first_symlink_component(lexical_root)
    if symlink is not None:
        return SkillPathCheck(
            None,
            "symlink_skill_root",
            f"Skill package root contains a symlink component: {symlink}",
        )
    try:
        mode = os.lstat(lexical_root).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return SkillPathCheck(None, "missing_skill_root", "Skill package root does not exist.")
    except OSError as exc:
        return SkillPathCheck(None, "unreadable_skill_root", f"Cannot inspect Skill package root: {exc}")
    if not stat.S_ISDIR(mode):
        return SkillPathCheck(None, "invalid_skill_root", "Skill package root is not a directory.")
    try:
        canonical = lexical_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return SkillPathCheck(None, "unreadable_skill_root", f"Cannot resolve Skill package root: {exc}")
    return SkillPathCheck(canonical)


def validate_skill_resource(
    skill_root: Path,
    resource: Path | str,
    *,
    expected_kind: str = "file",
    require_relative: bool = False,
) -> SkillPathCheck:
    """Validate one resource against a canonical Skill package root.

    ``expected_kind`` is ``"file"`` or ``"directory"``.  Every lexical path
    component below the root is checked with ``lstat`` before resolution, then
    the resolved target is checked for containment and regular file type.
    """
    root_check = validate_skill_root(Path(skill_root))
    if not root_check.valid:
        return root_check
    root = root_check.path
    assert root is not None

    supplied = Path(resource)
    if require_relative and supplied.is_absolute():
        return SkillPathCheck(
            None,
            "absolute_resource_path",
            "Skill resource paths must be relative to the Skill package root.",
        )
    if require_relative and any(part == ".." for part in supplied.parts):
        return SkillPathCheck(
            None,
            "traversal_resource_path",
            "Skill resource path contains a parent-directory traversal.",
        )

    lexical = _absolute_lexical(supplied if supplied.is_absolute() else root / supplied)
    try:
        relative = lexical.relative_to(root)
    except ValueError:
        return SkillPathCheck(
            None,
            "outside_skill_root",
            "Skill resource path is outside the Skill package root.",
        )
    if any(part in {"", ".", ".."} for part in relative.parts):
        return SkillPathCheck(
            None,
            "traversal_resource_path",
            "Skill resource path contains an unsafe path component.",
        )

    current = root
    for part in relative.parts:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except (FileNotFoundError, NotADirectoryError):
            return SkillPathCheck(None, "missing_resource", "Skill resource does not exist.")
        except OSError as exc:
            return SkillPathCheck(None, "unreadable_resource", f"Cannot inspect Skill resource: {exc}")
        if stat.S_ISLNK(mode):
            return SkillPathCheck(
                None,
                "symlink_resource_path",
                "Skill resource path contains a symlink component.",
            )

    try:
        canonical = lexical.resolve(strict=True)
        canonical.relative_to(root)
    except ValueError:
        return SkillPathCheck(
            None,
            "outside_skill_root",
            "Resolved Skill resource is outside the Skill package root.",
        )
    except (OSError, RuntimeError) as exc:
        return SkillPathCheck(None, "unreadable_resource", f"Cannot resolve Skill resource: {exc}")

    try:
        mode = os.lstat(canonical).st_mode
    except OSError as exc:
        return SkillPathCheck(None, "unreadable_resource", f"Cannot inspect Skill resource: {exc}")
    if expected_kind == "file":
        if not stat.S_ISREG(mode):
            return SkillPathCheck(None, "non_regular_resource", "Skill resource is not a regular file.")
    elif expected_kind == "directory":
        if not stat.S_ISDIR(mode):
            return SkillPathCheck(None, "non_directory_resource", "Skill resource is not a directory.")
    else:
        raise ValueError(f"Unsupported expected_kind: {expected_kind}")
    return SkillPathCheck(canonical)


def iter_safe_regular_files(
    skill_root: Path,
    directory: Path | str,
    *,
    excluded_dirs: set[str] | frozenset[str] = frozenset(),
) -> Iterator[Path]:
    """Yield sorted, contained, non-symlink regular files below a directory."""
    root_check = validate_skill_root(Path(skill_root))
    if not root_check.valid:
        return
    root = root_check.path
    assert root is not None
    directory_check = validate_skill_resource(root, directory, expected_kind="directory")
    if not directory_check.valid:
        return
    start = directory_check.path
    assert start is not None

    files: list[Path] = []
    for walk_root, dirs, names in os.walk(start, followlinks=False):
        walk_path = Path(walk_root)
        retained_dirs: list[str] = []
        for name in dirs:
            if name in excluded_dirs:
                continue
            check = validate_skill_resource(
                root,
                walk_path / name,
                expected_kind="directory",
            )
            if check.valid:
                retained_dirs.append(name)
        dirs[:] = retained_dirs
        for name in names:
            check = validate_skill_resource(root, walk_path / name, expected_kind="file")
            if check.valid and check.path is not None:
                files.append(check.path)
    yield from sorted(files, key=lambda path: str(path.relative_to(root)))
