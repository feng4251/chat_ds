from pathlib import Path

from native_security.artifact_contract import (
    validate_artifact_contracts,
    workspace_snapshot,
)
from deepseek_runner.native_artifacts import (
    compile_deepseek_artifact_projection,
    evaluate_deepseek_artifact_projection,
)


def _contract(skill_name: str):
    return {
        "skill_name": skill_name,
        "declared_final_artifact": "{NAME}_FINAL.md",
        "declared_modular_files": ["01_*.md", "02_*.md"],
        "expected_min_bytes": 64,
        "expected_max_bytes": 4096,
        "expected_min_lines": 4,
        "declared_section_count": 2,
    }


def test_artifact_receipt_requires_this_turn_commit_and_declared_modules(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "stale_FINAL.md").write_text(
        "# Stale\n\n## Existing\n\nunchanged\n", encoding="utf-8"
    )
    before = workspace_snapshot(workspace)
    (workspace / "warehouse_FINAL.md").write_text(
        "# Warehouse\n\n## Findings\n\n" + "verified evidence " * 5 + "\n",
        encoding="utf-8",
    )
    (workspace / "01_inventory.md").write_text("inventory", encoding="utf-8")
    (workspace / "02_receipts.md").write_text("receipts", encoding="utf-8")
    after = workspace_snapshot(workspace)
    receipt = validate_artifact_contracts(
        contracts=[_contract("warehouse-audit")],
        active_skill_name="warehouse-audit",
        before=before,
        after=after,
        workspace_root=workspace,
    )
    assert receipt["status"] == "passed"
    assert receipt["validated"][0]["path"] == "warehouse_FINAL.md"


def test_artifact_receipt_fails_closed_after_cross_domain_rename(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = workspace_snapshot(workspace)
    (workspace / "museum_FINAL.md").write_text(
        "# Museum\n\n## Findings\n\nshort\n", encoding="utf-8"
    )
    (workspace / "01_east.md").write_text("east", encoding="utf-8")
    after = workspace_snapshot(workspace)
    receipt = validate_artifact_contracts(
        contracts=[_contract("museum-catalog")],
        active_skill_name="museum-catalog",
        before=before,
        after=after,
        workspace_root=workspace,
    )
    assert receipt["status"] == "failed"
    codes = {finding["code"] for finding in receipt["findings"]}
    assert "artifact_min_bytes_not_met" in codes
    assert "artifact_declared_module_missing" in codes


def test_multiple_native_skill_receipts_activate_only_named_contracts(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = workspace_snapshot(workspace)
    for stem in ("factory", "museum"):
        (workspace / f"{stem}_FINAL.md").write_text(
            f"# {stem}\n\n## Findings\n\n" + "verified evidence " * 5 + "\n",
            encoding="utf-8",
        )
    (workspace / "01_inventory.md").write_text("inventory", encoding="utf-8")
    (workspace / "02_receipts.md").write_text("receipts", encoding="utf-8")
    after = workspace_snapshot(workspace)
    factory = _contract("factory-inspection")
    factory["declared_final_artifact"] = "factory_FINAL.md"
    museum = _contract("museum-catalog")
    museum["declared_final_artifact"] = "museum_FINAL.md"
    receipt = validate_artifact_contracts(
        contracts=[factory, museum, _contract("inactive-warehouse")],
        active_skill_name=None,
        active_skill_names=("factory-inspection", "museum-catalog"),
        before=before,
        after=after,
        workspace_root=workspace,
    )
    assert receipt["status"] == "passed"
    assert receipt["activated_contract_count"] == 2
    assert {row["skill_name"] for row in receipt["validated"]} == {
        "factory-inspection", "museum-catalog"
    }


def test_native_artifact_gate_preserves_frontier_across_domain_rename(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    before = workspace_snapshot(workspace)
    contract = _contract("museum-catalog")
    projection = compile_deepseek_artifact_projection(
        contracts=[contract],
        bound_skill_name="museum-catalog",
        workflow_projection=None,
        native_session_id=f"chatds-{'4' * 32}",
        workspace_before=before,
    )
    failed = evaluate_deepseek_artifact_projection(
        projection=projection,
        invoked_skill_names=(),
        workspace_root=workspace,
    )
    assert failed["status"] == "failed"
    assert failed["findings"][0]["code"] == "artifact_final_missing"

    (workspace / "museum_FINAL.md").write_text(
        "# Museum\n\n## Findings\n\n" + "verified evidence " * 5 + "\n",
        encoding="utf-8",
    )
    (workspace / "01_east.md").write_text("east", encoding="utf-8")
    (workspace / "02_west.md").write_text("west", encoding="utf-8")
    passed = evaluate_deepseek_artifact_projection(
        projection=projection,
        invoked_skill_names=(),
        workspace_root=workspace,
    )
    assert passed["status"] == "passed"
