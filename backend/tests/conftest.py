import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _reset_workspace_reconciler_state() -> None:
    """Close discovery descriptors and discard process-global test state."""

    import workspace_reconciler

    with workspace_reconciler._DISCOVERY_LOCK:
        cursor = workspace_reconciler._DISCOVERY_CURSOR
        workspace_reconciler._DISCOVERY_CURSOR = None
        if cursor is not None:
            cursor.close()
        workspace_reconciler._DEFERRED_RETRY.clear()
        workspace_reconciler._DEFERRED_RETRY_TURN.clear()


@pytest.fixture(autouse=True)
def isolate_backend_storage(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
):
    """Keep every Backend test away from production and repository storage."""

    import workspace
    from routers import conv_router, skill_router

    storage_root = tmp_path_factory.mktemp("backend-storage")
    workspace_root = storage_root / "workspaces"
    skill_root = storage_root / "skills"
    storage_root.chmod(0o700)

    _reset_workspace_reconciler_state()
    for variable in (
        "WORKSPACE_MUTATION_LOCK_ROOT",
        "WORKSPACE_MUTATION_LOCK_REQUIRE_MOUNTPOINT",
        "WORKSPACE_MUTATION_LOCK_TIMEOUT_SECONDS",
        "WORKSPACE_ORPHAN_RECONCILE_LIMIT",
        "WORKSPACE_ORPHAN_LOCK_TIMEOUT_SECONDS",
        "WORKSPACE_ORPHAN_RECONCILE_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(workspace, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(conv_router, "SANDBOX_BASE", workspace_root)
    monkeypatch.setattr(skill_router, "SKILLS_DATA_DIR", skill_root)

    yield

    _reset_workspace_reconciler_state()
