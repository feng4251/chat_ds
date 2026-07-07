#!/usr/bin/env python3
"""
Submission Readiness Reporter
=============================
Generates a comprehensive submission readiness assessment for FDA/EMA regulatory
submissions. Scores each dimension and produces a human-readable report.

Usage:
  python submission_readiness.py --sdtm-dir sdtm_json/ --output readiness_report.json
  python submission_readiness.py --sdtm-dir sdtm_json/ --adam-dir adam_json/ --format pdf
"""

import json
import sys
import os
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field, asdict

# ── Import shared domain parser (single source of truth) ──────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from domain_parser import DomainParser


# ═══════════════════════════════════════════════════════════════════════════════
# Assessment Framework
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DimensionScore:
    name: str
    max_score: int
    score: int = 0
    checks: List[Dict] = field(default_factory=list)
    passed: bool = True



def _get_domain_names(project_root: str = None) -> List[str]:
    """Get SDTM domain names from domain_parser (single source of truth)."""
    dp = DomainParser(project_root) if project_root else DomainParser()
    return dp.get_all_domains()


def assess_sdtm_compliance(sdtm_dir: str, project_root: str = None) -> DimensionScore:
    """Assess SDTM IG 3.4 compliance. Reads expected domains from domain_parser."""
    dim = DimensionScore(name="SDTM IG 3.4 合规性", max_score=35)
    expected_domains = _get_domain_names(project_root)
    found_domains = set()

    # Cache parser for per-domain variable checks
    dp = DomainParser(project_root) if project_root else DomainParser()

    if not os.path.isdir(sdtm_dir):
        dim.checks.append({"check": "SDTM directory exists", "passed": False, "detail": f"Directory not found: {sdtm_dir}"})
        dim.passed = False
        return dim

    for domain in expected_domains:
        json_path = os.path.join(sdtm_dir, f"{domain.lower()}.json")
        if os.path.exists(json_path):
            found_domains.add(domain)
            try:
                with open(json_path) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    records = data.get("records", data.get("data", []))
                else:
                    records = data
                count = len(records)
                # Check required variables (sourced from domain_parser)
                required_vars = [v.name for v in dp.get_variables(domain) if v.required]
                if required_vars and records:
                    missing_req = sum(1 for r in records[:10] if not all(v in r for v in required_vars))
                    dim.checks.append({
                        "check": f"{domain}: Required variables present",
                        "passed": missing_req == 0,
                        "detail": f"{count} records; required vars: {required_vars}"
                    })
                dim.checks.append({"check": f"{domain}: Records present", "passed": count > 0, "detail": f"{count} records"})
            except Exception as e:
                dim.checks.append({"check": f"{domain}: Valid JSON", "passed": False, "detail": str(e)})
        else:
            dim.checks.append({"check": f"{domain}: File exists", "passed": False, "detail": "Missing JSON file"})

    # Score: all domains found = full points
    missing = set(expected_domains) - found_domains
    if missing:
        dim.score = int(35 * (len(found_domains) / len(expected_domains)))
        dim.passed = False
    else:
        dim.score = 35
        dim.passed = True

    return dim


def assess_adam_compliance(adam_dir: Optional[str]) -> DimensionScore:
    """Assess ADaM IG 1.2 compliance."""
    dim = DimensionScore(name="ADaM IG 1.2 合规性与可追溯性", max_score=25)
    if not adam_dir or not os.path.isdir(adam_dir):
        dim.checks.append({"check": "ADaM datasets present", "passed": False, "detail": "No ADaM directory provided"})
        dim.score = 0
        dim.passed = False
        return dim

    expected_adam = {"ADSL", "ADAE", "ADLB", "ADEFF", "ADTTE"}
    found_adam = set()
    for ds in expected_adam:
        path = os.path.join(adam_dir, f"{ds.lower()}.json")
        if os.path.exists(path):
            found_adam.add(ds)
            try:
                with open(path) as f:
                    data = json.load(f)
                records = data if isinstance(data, list) else data.get("records", [])
                # Check traceability variables
                if records:
                    has_src = all("SRCDOM" in r and "SRCVAR" in r for r in records[:5])
                    dim.checks.append({"check": f"{ds}: Traceability variables present", "passed": has_src, "detail": f"{len(records)} records"})
            except Exception as e:
                dim.checks.append({"check": f"{ds}: Valid JSON", "passed": False, "detail": str(e)})

    dim.score = int(25 * (len(found_adam) / len(expected_adam)))
    dim.passed = len(found_adam) == len(expected_adam)
    return dim


def assess_data_integrity(sdtm_dir: str, project_root: str = None) -> DimensionScore:
    """Assess cross-domain data integrity. Reads domain names from domain_parser."""
    dim = DimensionScore(name="跨域数据完整性", max_score=15)
    if not os.path.isdir(sdtm_dir):
        dim.score = 0
        dim.passed = False
        return dim

    # Get DM domain name from parser (instead of hardcoding "dm.json")
    dp = DomainParser(project_root) if project_root else DomainParser()
    all_domains = dp.get_all_domains()
    dm_domain = next((d for d in all_domains if d.upper() == "DM"), "DM")
    non_dm_domains = [d for d in all_domains if d.upper() != "DM"]

    dm_path = os.path.join(sdtm_dir, f"{dm_domain.lower()}.json")
    if not os.path.exists(dm_path):
        dim.checks.append({"check": "DM domain exists", "passed": False, "detail": "Cannot verify cross-domain references without DM"})
        dim.score = 0
        return dim

    try:
        with open(dm_path) as f:
            dm_data = json.load(f)
        dm_records = dm_data if isinstance(dm_data, list) else dm_data.get("records", [])
        dm_usubjids = {r["USUBJID"] for r in dm_records if "USUBJID" in r}
    except Exception as e:
        dim.checks.append({"check": "DM: Valid JSON", "passed": False, "detail": str(e)})
        return dim

    # Check DM uniqueness
    unique_count = len(dm_usubjids)
    record_count = len(dm_records)
    dim.checks.append({"check": "DM: USUBJID uniqueness", "passed": unique_count == record_count, "detail": f"{unique_count} unique of {record_count} records"})

    # Check cross-domain USUBJID referential integrity (sourced from parser)
    checks_passed = 0
    total_checks = 0
    for domain in non_dm_domains:
        path = os.path.join(sdtm_dir, f"{domain.lower()}.json")
        if not os.path.exists(path):
            continue
        total_checks += 1
        try:
            with open(path) as f:
                d_data = json.load(f)
            d_records = d_data if isinstance(d_data, list) else d_data.get("records", [])
            orphan_count = sum(1 for r in d_records if r.get("USUBJID", "") not in dm_usubjids)
            if orphan_count == 0:
                checks_passed += 1
                dim.checks.append({"check": f"{domain}: USUBJID references valid", "passed": True, "detail": f"{len(d_records)} records, 0 orphans"})
            else:
                dim.checks.append({"check": f"{domain}: USUBJID references valid", "passed": False, "detail": f"{orphan_count} orphan references"})
        except Exception:
            pass

    if total_checks > 0:
        dim.score = int(15 * (checks_passed / total_checks))
        dim.passed = checks_passed == total_checks
    return dim


def assess_file_formats(sdtm_dir: str, project_root: str = None) -> DimensionScore:
    """Assess file format and naming compliance. Reads domain names from domain_parser."""
    dim = DimensionScore(name="文件格式与命名标准", max_score=10)
    if not os.path.isdir(sdtm_dir):
        dim.score = 0
        dim.passed = False
        return dim

    dp = DomainParser(project_root) if project_root else DomainParser()
    expected_domains = [d.lower() for d in dp.get_all_domains()]
    checks_passed = 0
    for domain in expected_domains:
        json_ok = os.path.exists(os.path.join(sdtm_dir, f"{domain}.json"))
        csv_ok = os.path.exists(os.path.join(sdtm_dir, f"{domain}.csv"))
        xpt_ok = os.path.exists(os.path.join(sdtm_dir, f"{domain}.xpt"))
        if json_ok:
            checks_passed += 1
        dim.checks.append({"check": f"{domain}: Format availability", "passed": json_ok or csv_ok, "detail": f"JSON={'Y' if json_ok else 'N'}, CSV={'Y' if csv_ok else 'N'}, XPT={'Y' if xpt_ok else 'N'}"})

    dim.score = int(10 * (checks_passed / len(expected_domains)))
    dim.passed = checks_passed == len(expected_domains)
    return dim


def generate_report(sdtm_dir: str, adam_dir: Optional[str] = None, project_root: str = None) -> Dict:
    """Generate comprehensive submission readiness report.

    Args:
        sdtm_dir: Path to SDTM JSON data directory.
        adam_dir: Path to ADaM JSON data directory (optional).
        project_root: Path to TrialSim project root for domain_parser.
                      If None, auto-detected from script location.
    """
    dimensions = {
        "sdtm_compliance": assess_sdtm_compliance(sdtm_dir, project_root),
        "adam_compliance": assess_adam_compliance(adam_dir),
        "data_integrity": assess_data_integrity(sdtm_dir, project_root),
        "format_compliance": assess_file_formats(sdtm_dir, project_root),
    }

    # Define.xml is scored based on existence check
    define_xml_path = os.path.join(sdtm_dir, "..", "define.xml") if sdtm_dir else None
    define_xml_exists = define_xml_path and os.path.exists(define_xml_path)
    dim_define = DimensionScore(name="Define.xml 完整性", max_score=15)
    dim_define.checks.append({"check": "define.xml exists", "passed": define_xml_exists, "detail": f"Path: {define_xml_path}" if define_xml_path else "No path"})
    dim_define.score = 15 if define_xml_exists else 0
    dim_define.passed = define_xml_exists
    dimensions["define_xml"] = dim_define

    # Compute total
    total_score = sum(d.score for d in dimensions.values())
    max_score = sum(d.max_score for d in dimensions.values())
    all_passed = all(d.passed for d in dimensions.values())

    readiness = {
        "report_title": "Submission Readiness Assessment",
        "timestamp": datetime.now().isoformat(),
        "sdtm_directory": sdtm_dir,
        "adam_directory": adam_dir,
        "total_score": total_score,
        "max_score": max_score,
        "percentage": round(100 * total_score / max_score, 1) if max_score > 0 else 0,
        "overall_grade": "A" if total_score >= 90 else "B" if total_score >= 75 else "C" if total_score >= 60 else "D" if total_score >= 40 else "F",
        "overall_status": "可提交" if all_passed and total_score >= 80 else "仍需完善",
        "dimensions": {key: asdict(val) for key, val in dimensions.items()},
    }

    # Recommendations
    recommendations = []
    if not all_passed:
        for key, dim in dimensions.items():
            for check in dim.checks:
                if not check["passed"]:
                    recommendations.append(f"[{key}] {check['check']}: {check.get('detail', '')}")
    readiness["recommendations"] = recommendations
    readiness["recommendation_count"] = len(recommendations)

    return readiness


def print_report(readiness: Dict):
    """打印格式化的提交就绪性报告。"""
    print("\n" + "=" * 80)
    print("  提交就绪性评估")
    print("  FDA/EMA 法规提交（eCTD模块5）")
    print("=" * 80)
    print(f"  日期: {readiness['timestamp']}")
    print(f"  SDTM 目录: {readiness['sdtm_directory']}")
    print(f"  ADaM 目录: {readiness['adam_directory'] or '未提供'}")
    print()
    print(f"  总分: {readiness['total_score']}/{readiness['max_score']} ({readiness['percentage']}%)")
    print(f"  等级: {readiness['overall_grade']}")
    print(f"  状态: {readiness['overall_status']}")
    print()

    for key, dim in readiness["dimensions"].items():
        icon = "✅" if dim["passed"] else "❌"
        print(f"  {icon} {dim['name']:50s} {dim['score']:2d}/{dim['max_score']}")
        for check in dim["checks"]:
            c_icon = "  ✅" if check["passed"] else "  ❌"
            print(f"     {c_icon} {check['check']}: {check['detail']}")

    if readiness["recommendations"]:
        print(f"\n  ── 改进建议 ({readiness['recommendation_count']}条) ──")
        for i, rec in enumerate(readiness["recommendations"], 1):
            print(f"  {i}. {rec}")

    print("=" * 80)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Submission Readiness Assessment Reporter")
    parser.add_argument("--sdtm-dir", required=True, help="Path to SDTM data directory")
    parser.add_argument("--adam-dir", help="Path to ADaM data directory (optional)")
    parser.add_argument("--project-root", help="Path to TrialSim project root (for domain_parser). Auto-detected if omitted.")
    parser.add_argument("--output", "-o", help="Output JSON report file")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress printed output")
    args = parser.parse_args()

    readiness = generate_report(args.sdtm_dir, args.adam_dir, args.project_root)

    if not args.quiet:
        print_report(readiness)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(readiness, f, indent=2, default=str)
        print(f"\n报告已保存至 {args.output}")

    # Exit 1 if not ready
    sys.exit(0 if readiness["overall_status"] == "可提交" else 1)


if __name__ == "__main__":
    main()
