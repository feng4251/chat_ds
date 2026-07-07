#!/usr/bin/env python3
"""
Phase Study Type Consistency Evaluator
=======================================
Evaluates whether the project's phase-specific study design labels
(e.g., blinding, randomization, control type) are clinically appropriate
for each trial phase, and checks the pipeline for safeguards against
phase-inappropriate defaults.

Core Issue Detected: Phase I SAD/MAD FIH trials should typically be
described as "randomized, placebo-controlled, single-blind" (or
"investigator-blind"), NOT "randomized, double-blind, placebo-controlled."
Phase I prioritizes safety monitoring — investigators need to know
treatment assignments, especially during sentinel dosing.

Usage:
  python3 evaluation/phase_study_type_evaluator.py
  python3 evaluation/phase_study_type_evaluator.py --verbose
  python3 evaluation/phase_study_type_evaluator.py --report path/to/report.md
"""

import os
import re
import sys
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = PROJECT_ROOT / "output_result" / "Resmetirom_Phase_I_II_III_Comprehensive_Development_Plan_for_MASH.md"


# ══════════════════════════════════════════════════════════════════
# Evaluation Framework
# ══════════════════════════════════════════════════════════════════

@dataclass
class PhaseStudyTypeCheck:
    """A single falsifiable check for phase-appropriate study type."""
    check_id: str
    claim: str
    category: str                       # "phase_skill" | "report_output" | "pipeline_gap" | "cross_ref"
    file_path: str
    evidence_type: str                  # "file_exists" | "content_match" | "content_absence" | "cross_ref"
    evidence_query: str
    severity: str = "CRITICAL"          # CRITICAL / MAJOR / MINOR
    expected_result: bool = True        # True = pattern SHOULD match; False = pattern SHOULD NOT match
    _result: Optional[bool] = None
    _detail: str = ""

    def evaluate(self, project_root: Path, report_path: Path = None) -> bool:
        full_path = project_root / self.file_path
        # Allow report_path override for report_output checks
        if self.category == "report_output" and report_path:
            full_path = report_path

        if self.evidence_type == "file_exists":
            self._result = full_path.exists()
            self._detail = f"File {'EXISTS' if self._result else 'MISSING'}: {self.file_path}"

        elif self.evidence_type == "content_match":
            if not full_path.exists():
                self._result = False
                self._detail = f"File MISSING: {self.file_path}"
            else:
                content = full_path.read_text(encoding='utf-8', errors='ignore')
                # Use re.DOTALL only if pattern starts with (?s); otherwise,
                # `pattern.*spanning.*lines` would falsely match across the entire file.
                flags = re.IGNORECASE
                query = self.evidence_query
                if query.startswith("(?s)"):
                    flags |= re.DOTALL
                    query = query[4:]  # Strip the (?s) prefix before compiling
                match = re.search(query, content, flags)
                self._result = match is not None
                actual = "FOUND" if match else "NOT FOUND"
                self._detail = f"Pattern {actual} in {self.file_path}: {self.evidence_query[:100]}..."

        elif self.evidence_type == "content_absence":
            # Pattern SHOULD NOT exist (used to detect inappropriate labels)
            if not full_path.exists():
                self._result = False
                self._detail = f"File MISSING: {self.file_path}"
            else:
                content = full_path.read_text(encoding='utf-8', errors='ignore')
                flags = re.IGNORECASE
                query = self.evidence_query
                if query.startswith("(?s)"):
                    flags |= re.DOTALL
                    query = query[4:]
                match = re.search(query, content, flags)
                self._result = match is None  # PASS = pattern NOT found
                actual = "ABSENT (correct)" if match is None else "PRESENT (violation)"
                self._detail = f"Pattern {actual} in {self.file_path}: {self.evidence_query[:100]}..."

        elif self.evidence_type == "cross_ref":
            if not full_path.exists():
                self._result = False
                self._detail = f"Source file MISSING: {self.file_path}"
            else:
                content = full_path.read_text(encoding='utf-8', errors='ignore')
                refs = self.evidence_query.split("|")
                found_any = False
                for ref in refs:
                    ref_basename = os.path.basename(ref.strip())
                    ref_noext = os.path.splitext(ref_basename)[0]
                    if re.search(re.escape(ref_basename), content, re.IGNORECASE):
                        found_any = True
                        break
                    if re.search(re.escape(ref_noext), content, re.IGNORECASE):
                        found_any = True
                        break
                self._result = found_any
                actual = "FOUND" if found_any else "NOT FOUND"
                self._detail = f"Cross-ref to '{self.evidence_query[:80]}' {actual} in {self.file_path}"

        else:
            self._result = False
            self._detail = f"Unknown evidence_type: {self.evidence_type}"

        return self._result

    @property
    def passed(self) -> bool:
        return self._result is True

    @property
    def status_icon(self) -> str:
        if self._result is None:
            return "⬜"
        return "✅" if self._result else "❌"


# ══════════════════════════════════════════════════════════════════
# Check Definitions
# ══════════════════════════════════════════════════════════════════

def build_checks() -> List[PhaseStudyTypeCheck]:
    """Build the complete set of phase study type consistency checks."""
    checks = []

    # ── Category 1: Phase I Skill File Checks ──
    cat1 = "phase_skill"

    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-001", "phase1-dose-escalation.md exists", cat1,
        "phase1-dose-escalation.md", "file_exists", "",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-002", "Phase 1 skill defines SAD/MAD design characteristics", cat1,
        "phase1-dose-escalation.md", "content_match",
        r"(?:SAD|Single Ascending Dose|Multiple Ascending Dose|MAD)",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-003", "Phase 1 skill mentions sentinel dosing for safety", cat1,
        "phase1-dose-escalation.md", "content_match",
        r"(?:sentinel|Sentinel dosing)",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-004", "Phase 1 skill mentions placebo subjects for blinding within cohorts", cat1,
        "phase1-dose-escalation.md", "content_match",
        r"Placebo subjects for blinding",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-005", "Phase 1 skill does NOT mandate double-blind for FIH/SAD/MAD", cat1,
        "phase1-dose-escalation.md", "content_absence",
        r"(?:Phase\s*1.*must be double.blind|Phase\s*I.*must be double.blind|FIH.*double.blind|first.in.human.*double.blind required|SAD.*must be double.blind)",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-006", "Phase 1 skill explicitly lists blinding options (open-label/single-blind/double-blind)", cat1,
        "phase1-dose-escalation.md", "content_match",
        r"(?:open.label|single.blind|investigator.blind|double.blind|observer.blind|blinding)",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-007a", "Phase 1 generation pattern 1 uses 'phase1_dose_escalation' as study_type (not RCT)", cat1,
        "phase1-dose-escalation.md", "content_match",
        r'"study_type":\s*"phase1_dose_escalation"',
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-007b", "Phase 1 Example 1 design field says 'randomized_placebo_controlled' (no double-blind)", cat1,
        "phase1-dose-escalation.md", "content_match",
        r'"design":\s*"randomized_placebo_controlled"',
        "MINOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-008", "Phase 1 skill section on 'Phase 1 Trial Characteristics' defines correct population", cat1,
        "phase1-dose-escalation.md", "content_match",
        r"(?:Healthy volunteers|healthy.volunteers|patients.*indication-dependent)",
        "MAJOR", True
    ))

    # ── Category 2: Phase II Skill File Checks (control group) ──
    checks.append(PhaseStudyTypeCheck(
        "PST-PH2-001", "Phase 2 skill acknowledges Phase 2a may be single-arm (no blinding requirement)", cat1,
        "phase2-proof-of-concept.md", "content_match",
        r"(?:Single.arm|single.arm|no control|historical control|often none)",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH2-002", "Phase 2b skill specifies randomized + placebo control is appropriate", cat1,
        "phase2-proof-of-concept.md", "content_match",
        r"(?:placebo.control|active.or.placebo|randomized.*multiple doses)",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH2-003", "Phase 2b example specifies double-blind (appropriate for dose-ranging)", cat1,
        "phase2-proof-of-concept.md", "content_match",
        r'"blinding":\s*"double_blind"',
        "MINOR", True
    ))

    # ── Category 3: Phase III Skill File Checks (control group) ──
    checks.append(PhaseStudyTypeCheck(
        "PST-PH3-001", "Phase 3 skill exists", cat1,
        "phase3-pivotal.md", "file_exists", "",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH3-002", "Phase 3 skill specifies randomized, controlled design", cat1,
        "phase3-pivotal.md", "content_match",
        r"(?:Randomized.*controlled|randomized.*controlled|double.blind|placebo.controlled)",
        "MAJOR", True
    ))

    # ── Category 4: Report Output Checks ──
    cat2 = "report_output"
    report = str(DEFAULT_REPORT.relative_to(PROJECT_ROOT)) if DEFAULT_REPORT.exists() else ""

    # -- Phase I in report --
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-001", "Report has a Phase I section", cat2,
        report, "content_match",
        r"(?s)Part A:.*Phase\s*I",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-002", "Report has Phase I A1 study design table", cat2,
        report, "content_match",
        r"A1\.\s*Study Design Overview",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-003",
        "VIOLATION: Phase I A1 study type should NOT be 'double-blind' (FIH SAD/MAD should be single-blind or investigator-blind)",
        cat2, report, "content_absence",
        r"(?s)Part A:.*?Phase\s*I[\s\S]{0,200}?###\s*A1[\s\S]{0,500}?Study\s*Type[^\n]{0,200}?double.blind",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-004", "Phase I correctly mentions sentinel dosing", cat2,
        report, "content_match",
        r"(?s)Part A:.*?Phase\s*I[\s\S]{0,2000}?Sentinel\s*Dosing",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-005", "Phase I correctly mentions SAD/MAD design", cat2,
        report, "content_match",
        r"(?s)Part A:.*?Phase\s*I[\s\S]{0,2000}?(?:SAD|Single Ascending Dose|MAD|Multiple Ascending Dose)",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-006", "Phase I correctly mentions DLT criteria", cat2,
        report, "content_match",
        r"(?s)Part A:.*?Phase\s*I[\s\S]{0,3000}?DLT\s*(?:Definitions|Criteria|Observation)",
        "MAJOR", True
    ))

    # -- Phase II in report --
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-007", "Phase II B1 study type uses double-blind (appropriate for dose-ranging Phase IIb)", cat2,
        report, "content_match",
        r"(?s)Part B:.*?Phase\s*II[\s\S]{0,2000}?B1.*?Study\s*Type.*?double.blind",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-008", "Phase II correctly mentions MCP-Mod", cat2,
        report, "content_match",
        r"(?s)Part B:.*?Phase\s*II[\s\S]{0,3000}?MCP.Mod",
        "MINOR", True
    ))

    # -- Phase III in report --
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-009", "Report has a Phase III section", cat2,
        report, "content_match",
        r"(?s)Part C:.*?Phase\s*III",
        "CRITICAL", True
    ))

    # -- Cross-phase: Phase I should NOT reference DSMB/iDMC (Phase III concept) as primary safety mechanism --
    checks.append(PhaseStudyTypeCheck(
        "PST-REP-010", "Phase I uses SRC (Safety Review Committee), not DSMB as primary gate", cat2,
        report, "content_match",
        r"(?s)Part A:.*?Phase\s*I[\s\S]{0,5000}?(?:SRC|Safety Review Committee|cohort.review)",
        "MINOR", True
    ))

    # ── Category 5: Pipeline Gap Checks ──
    cat3 = "pipeline_gap"

    checks.append(PhaseStudyTypeCheck(
        "PST-GAP-001", "Worker B extracts blinding level (open-label/single-blind/double-blind)", cat3,
        "orchestration/workers/worker-pico-standards.yaml", "content_match",
        r"Blinding level.*open.label.*single.blind.*double.blind",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-GAP-002", "Worker B extracts study design type and phase separately", cat3,
        "orchestration/workers/worker-pico-standards.yaml", "content_match",
        r"(?:Phase:.*I.*II.*III.*IV|Design type:.*parallel.*crossover)",
        "CRITICAL", True
    ))
    # These GAP checks use content_match for what SHOULD exist.
    # Since the rules are genuinely missing, these will FAIL — correctly identifying the gap.
    checks.append(PhaseStudyTypeCheck(
        "PST-GAP-003",
        "Worker B FUNCTION 1e SHOULD include phase-blinding default rule (Phase I → single-blind/investigator-blind for FIH)",
        cat3, "orchestration/workers/worker-pico-standards.yaml", "content_match",
        r"(?s)1e\.\s*STUDY DESIGN EXTRACTION[\s\S]{0,600}?(?:Phase\s*I.*[Ss]ingle|Phase\s*I.*[Ii]nvestigator|early.phase.*[Ss]ingle|dose.escalation.*[Ss]ingle|FIH.*[Ss]ingle|[Bb]linding.*default.*[Pp]hase|[Pp]hase.appropriate.*[Bb]linding)",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-GAP-004",
        "Orchestrator cross_worker_consistency_checks SHOULD include BLINDING_VS_PHASE",
        cat3, "orchestration/orchestrator.yaml", "content_match",
        r"BLINDING_VS_PHASE|STUDY_TYPE_VS_PHASE|DESIGN_TYPE_VS_PHASE",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-GAP-005", "Phase skills are loaded by orchestrator as design references", cat3,
        "orchestration/orchestrator.yaml", "content_match",
        r"phase1-dose-escalation\.md.*phase2-proof-of-concept\.md.*phase3-pivotal\.md",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-GAP-006",
        "Worker B SHOULD cross-validate study_type label against phase-specific clinical norms",
        cat3, "orchestration/workers/worker-pico-standards.yaml", "content_match",
        r"(?:phase-appropriate|phase.specific.blinding|blinding.default.for.*phase|blinding should be.*phase|for Phase I.*single.blind|for Phase I.*investigator.blind|for Phase I.*open.label)",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-GAP-007",
        "Orchestrator report template SHOULD have phase-appropriate study type guidance per section",
        cat3, "orchestration/orchestrator.yaml", "content_match",
        r"(?:Phase\s*I.*open.label|Phase\s*I.*single.blind|Phase\s*I.*investigator.blind|Phase\s*I.*not.*double.blind|phase.specific.*study.type)",
        "MAJOR", True
    ))

    # ── Category 6: Phase 1 File Does Not Incorrectly Prescribe Double-Blind ──
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-009", "Phase 1 skill Example 1 does NOT say 'double-blind' in the title/design", cat1,
        "phase1-dose-escalation.md", "content_absence",
        r"(?s)Example 1[\s\S]{0,500}?(?:double.blind|double_blind)",
        "MAJOR", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-PH1-010", "Phase 1 skill business rule on blinding is conditional not prescriptive", cat1,
        "phase1-dose-escalation.md", "content_match",
        r"Blinding.*Maintain until database lock.*placebo.controlled",
        "MINOR", True
    ))

    # ── Category 7: Direct Detection of the Bug in the Report ──
    checks.append(PhaseStudyTypeCheck(
        "PST-BUG-001",
        "FIX VERIFIED: Phase I A1 no longer uses 'double-blind' (correct: single-blind, investigator-unblinded)",
        cat2, report, "content_match",
        r"(?s)###\s*A1\.[\s\S]{0,300}?Study\s*Type[^\n]{0,200}?single.blind",
        "CRITICAL", True
    ))
    checks.append(PhaseStudyTypeCheck(
        "PST-BUG-002", "Phase II B1 correctly says double-blind (this should PASS)", cat2,
        report, "content_match",
        r"(?s)###\s*B1\.[\s\S]{0,300}?Study\s*Type[^\n]{0,200}?double.blind",
        "MAJOR", True
    ))

    return checks


# ══════════════════════════════════════════════════════════════════
# Report Generation
# ══════════════════════════════════════════════════════════════════

def generate_report(checks: List[PhaseStudyTypeCheck], verbose: bool = False) -> str:
    """Generate a markdown evaluation report."""
    total = len(checks)
    passed = sum(1 for c in checks if c.passed)
    failed = total - passed
    critical_fails = sum(1 for c in checks if not c.passed and c.severity == "CRITICAL")
    major_fails = sum(1 for c in checks if not c.passed and c.severity == "MAJOR")
    pass_rate = (passed / total * 100) if total > 0 else 0

    # Grade
    if pass_rate >= 95 and critical_fails == 0:
        grade = "A"
    elif pass_rate >= 85 and critical_fails == 0:
        grade = "B"
    elif pass_rate >= 70:
        grade = "C"
    else:
        grade = "D/F"

    lines = []
    lines.append("# Phase Study Type Consistency — Evaluation Report")
    lines.append("")
    lines.append(f"**评估日期**: 2026-06-29")
    lines.append(f"**评估工具**: `evaluation/phase_study_type_evaluator.py`")
    lines.append(f"**评估方法**: {total} 项可证伪检查 × 4 个类别（Phase Skill 文件、Report Output、Pipeline Gap、Bug Detection）")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 评估摘要")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|:---:|")
    lines.append(f"| 总检查数 | {total} |")
    lines.append(f"| 通过 | **{passed}** |")
    lines.append(f"| 未通过 | **{failed}** |")
    lines.append(f"| CRITICAL 失败 | **{critical_fails}** |")
    lines.append(f"| MAJOR 失败 | **{major_fails}** |")
    lines.append(f"| 通过率 | **{pass_rate:.1f}%** |")
    lines.append(f"| 评级 | **{grade}** |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 按类别评估结果")
    lines.append("")

    categories = {
        "phase_skill": "Phase Skill 文件 (phase1/2/3-*.md)",
        "report_output": "Protocol Report 输出检查",
        "pipeline_gap": "Pipeline Gap 检测",
    }

    for cat_key, cat_name in categories.items():
        cat_checks = [c for c in checks if c.category == cat_key]
        cat_total = len(cat_checks)
        cat_pass = sum(1 for c in cat_checks if c.passed)
        cat_rate = (cat_pass / cat_total * 100) if cat_total > 0 else 0
        cat_status = "✅" if cat_pass == cat_total else "⚠️"
        lines.append(f"| **{cat_name}** | {cat_total} | {cat_pass} | {cat_rate:.0f}% | {cat_status} |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Detailed results by category
    for cat_key, cat_name in list(categories.items()) + [("bug_detection", "Bug Detection")]:
        cat_checks = [c for c in checks if c.category == cat_key or (cat_key == "bug_detection" and c.check_id.startswith("PST-BUG"))]
        if not cat_checks:
            continue
        # Avoid double-counting bug checks
        if cat_key == "bug_detection":
            pass
        lines.append(f"## {cat_name}")
        lines.append("")
        lines.append("| 检查 ID | 声明 | 严重级别 | 状态 | 详情 |")
        lines.append("|:---|------|:---:|:---:|------|")
        for c in cat_checks:
            detail = c._detail[:120] if c._detail else "-"
            lines.append(f"| {c.check_id} | {c.claim[:100]} | {c.severity} | {c.status_icon} | {detail} |")
        lines.append("")

    lines.append("---")
    lines.append("")

    # Root Cause Analysis
    lines.append("## 根因分析")
    lines.append("")
    lines.append("### 已确认的问题")
    lines.append("")
    lines.append("**Phase I FIH/SAD/MAD 试验被错误标注为 'double-blind, placebo-controlled'。**")
    lines.append("")
    lines.append("在 Resmetirom 方案报告的 Part A, Section A1 (Phase I) 中，研究类型被标注为")
    lines.append("`Single-center, randomized, double-blind, placebo-controlled`，")
    lines.append("这对于一个包含前哨给药（sentinel dosing）的首次人体 Phase I SAD/MAD 试验")
    lines.append("在临床上是不合适的。")
    lines.append("")
    lines.append("### 临床依据")
    lines.append("")
    lines.append("| Phase | 典型试验类型 | 适当的盲法 | 依据 |")
    lines.append("|-------|----------|-----------|------|")
    lines.append("| **Phase I FIH/SAD/MAD** | 剂量递增、安全性/耐受性 | **Single-blind** 或 **Investigator-blind** | 研究者需要了解给药情况以进行安全性监测，尤其在有前哨给药的情况下 |")
    lines.append("| **Phase I 扩展队列**（如肿瘤） | 初步疗效信号 | Open-label 或 Single-blind | 单臂或仅与历史对照比较 |")
    lines.append("| **Phase IIa (POC)** | 概念验证 | Open-label 或 Single-blind | 可使用历史对照 |")
    lines.append("| **Phase IIb (Dose-Ranging)** | 剂量探索 | **Double-blind** | 多臂安慰剂对照，需减少偏倚 |")
    lines.append("| **Phase III (Pivotal)** | 确证性 | **Double-blind** | 监管要求（ICH E9），确证性证据 |")
    lines.append("")
    lines.append("### 问题的根本原因")
    lines.append("")
    lines.append("该问题有 **两个层面** 的原因：")
    lines.append("")
    lines.append("1. **直接原因（在报告生成层）**：在生成 Resmetirom 方案报告的 Part A 时，")
    lines.append("   未对 Phase I 的特殊性进行区分，直接将 Phase II/III 的默认描述模板")
    lines.append("   `randomized, double-blind, placebo-controlled` 应用到了 Phase I 上。")
    lines.append("")
    lines.append("2. **系统性原因（在管线层）**：项目中**缺少一个阶段特异性的研究类型验证规则**。")
    lines.append("   具体来说：")
    lines.append("   - `worker-pico-standards.yaml` 的 FUNCTION 1e (Study Design Extraction) ")
    lines.append("     会提取盲法级别（open-label / single-blind / double-blind），")
    lines.append("     但**没有将盲法与试验阶段关联的验证规则**。")
    lines.append("   - `orchestrator.yaml` 的 cross-worker consistency checks 中包含了 ")
    lines.append("     IE_VS_PICO、COMPARATOR_VS_STANDARD、SAMPLE_SIZE_VS_ENDPOINT 等检查，")
    lines.append("     但**缺少 STUDY_TYPE_VS_PHASE 或 BLINDING_VS_PHASE 检查**。")
    lines.append("   - `phase1-dose-escalation.md` 文件本身**没有明确声明** Phase I FIH ")
    lines.append("     试验的盲法要求（仅提到'placebo subjects for blinding'和")
    lines.append("     'Maintain until database lock'），没有提供默认的 phase-appropriate 盲法指导。")
    lines.append("")
    lines.append("### 受影响的具体代码位置")
    lines.append("")
    lines.append("| 位置 | 问题 |")
    lines.append("|------|------|")
    lines.append("| `output_result/Resmetirom_*_MASH.md:61` | Phase I A1 研究类型标注为 'randomized, double-blind, placebo-controlled'（应改为 single-blind 或 investigator-blind） |")
    lines.append("| `phase1-dose-escalation.md` (全文) | 未明确规定 Phase I FIH 的默认盲法级别 |")
    lines.append("| `orchestration/workers/worker-pico-standards.yaml` (FUNCTION 1e) | 提取盲法但不做 phase-specific 验证 |")
    lines.append("| `orchestration/orchestrator.yaml` (cross-worker checks) | 缺少 STUDY_TYPE_VS_PHASE / BLINDING_VS_PHASE 检查 |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Recommendations
    lines.append("## 修复建议")
    lines.append("")
    lines.append("### 修复 1：更正报告中的 Phase I A1 研究类型（立即修复）")
    lines.append("")
    lines.append("将报告 line 61 的：")
    lines.append("```")
    lines.append("| **Study Type** | Single-center, randomized, double-blind, placebo-controlled |")
    lines.append("```")
    lines.append("改为：")
    lines.append("```")
    lines.append("| **Study Type** | Single-center, randomized, placebo-controlled, single-blind (investigator-unblinded for safety monitoring) |")
    lines.append("```")
    lines.append("")
    lines.append("**理由**: Phase I FIH SAD/MAD 试验的典型设计是：")
    lines.append("- **Randomized**: 是（受试者被随机分配到活性药组或安慰剂组）")
    lines.append("- **Placebo-controlled**: 是（每个队列包含安慰剂对照受试者）")
    lines.append("- **Single-blind/investigator-blind**: 是（受试者不知道分组，但研究者需要知道谁接受了活性药以进行安全性监测，尤其在有前哨给药策略的情况下）")
    lines.append("- **NOT double-blind**: Phase I 中，双盲通常不适用，因为研究者必须知道给药情况以评估 DLT 和决定是否继续递增剂量。即使采用双盲，也通常会设置一个非盲的安全性审查委员会（SRC）。")
    lines.append("")
    lines.append("### 修复 2：在 `phase1-dose-escalation.md` 中添加明确的盲法指导（源头修复）")
    lines.append("")
    lines.append("在 'Phase 1 Trial Characteristics' 表格中添加一行：")
    lines.append("```markdown")
    lines.append("| **Blinding** | Single-blind or investigator-blind (SAD/MAD); Open-label (oncology); Double-blind optional with unblinded SRC |")
    lines.append("```")
    lines.append("")
    lines.append("### 修复 3：在 Worker B 中添加 Phase-Specific Blinding 验证规则（管线修复）")
    lines.append("")
    lines.append("在 `worker-pico-standards.yaml` 的 FUNCTION 1e 之后添加：")
    lines.append("```yaml")
    lines.append("  BLINDING_VS_PHASE_VALIDATION:")
    lines.append("    rule: |")
    lines.append("      Phase I FIH/SAD/MAD → Default: single-blind or investigator-blind")
    lines.append("      Phase I Oncology → Default: open-label")
    lines.append("      Phase IIa POC → Default: open-label or single-blind")
    lines.append("      Phase IIb Dose-Ranging → Default: double-blind")
    lines.append("      Phase III Pivotal → Default: double-blind")
    lines.append("    violation: MAJOR_DEVIATION if Phase I uses double-blind without unblinded SRC justification")
    lines.append("```")
    lines.append("")
    lines.append("### 修复 4：在 Orchestrator 中添加 BLINDING_VS_PHASE 跨 Worker 检查（管线修复）")
    lines.append("")
    lines.append("在 `orchestrator.yaml` 的 `cross_worker_consistency_checks` 中添加：")
    lines.append("```yaml")
    lines.append("  - check: BLINDING_VS_PHASE")
    lines.append("    description: \"Is the blinding level appropriate for the trial phase?\"")
    lines.append("    severity: MAJOR")
    lines.append("    rule: |")
    lines.append("      Phase I (SAD/MAD, healthy volunteers) with double-blind → MAJOR_DEVIATION")
    lines.append("      (unless explicitly justified with unblinded SRC structure)")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*Contributor: 陈翼男 (Yinan Chen)*")
    lines.append("*评估工具: `evaluation/phase_study_type_evaluator.py`*")
    lines.append("*可复现: `python3 evaluation/phase_study_type_evaluator.py --verbose`*")
    lines.append("*2026-06-29*")
    lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main():
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    # Parse optional report path
    report_path = DEFAULT_REPORT
    for i, arg in enumerate(sys.argv):
        if arg == "--report" and i + 1 < len(sys.argv):
            report_path = Path(sys.argv[i + 1])
            break

    if not report_path.exists():
        print(f"⚠️  Report file not found: {report_path}")
        print("   Specify with --report path/to/report.md")
        print("   Some report_output checks will fail.\n")

    print("=" * 70)
    print("  Phase Study Type Consistency Evaluator")
    print("  Protocol Report:", report_path.name)
    print("=" * 70)

    checks = build_checks()

    # Run all checks
    for check in checks:
        check.evaluate(PROJECT_ROOT, report_path)
        if verbose:
            print(f"  {check.status_icon} {check.check_id}: {check._detail[:120]}")

    # Generate and print report
    report = generate_report(checks, verbose)

    # Save report
    output_path = PROJECT_ROOT / "output_result" / "phase_study_type_evaluation_report.md"
    output_path.write_text(report, encoding='utf-8')

    # Summary to stdout
    total = len(checks)
    passed = sum(1 for c in checks if c.passed)
    failed_checks = [c for c in checks if not c.passed]
    critical_fails = [c for c in failed_checks if c.severity == "CRITICAL"]

    print(f"\n  Results: {passed}/{total} passed ({passed/total*100:.1f}%)")
    print(f"  Failed: {len(failed_checks)} ({len(critical_fails)} CRITICAL)")
    print()

    if failed_checks:
        print("  Failed Checks:")
        for c in failed_checks:
            print(f"    {c.status_icon} [{c.severity}] {c.check_id}: {c.claim[:120]}")
            if verbose:
                print(f"       Detail: {c._detail[:150]}")
        print()

    if critical_fails:
        print(f"  ❗ {len(critical_fails)} CRITICAL failure(s) detected.")
        print()

    # Always show key summary
    print(f"  Phase I A1 study type: {'✅ CORRECT (single-blind)' if any(c.check_id == 'PST-BUG-001' and c.passed for c in checks) else '⚠️  Needs review'}")
    print(f"  Worker B BLINDING_VS_PHASE rule: {'✅ PRESENT (v1.4)' if any(c.check_id == 'PST-GAP-003' and c.passed for c in checks) else '❌ MISSING'}")
    print(f"  Orchestrator BLINDING_VS_PHASE check: {'✅ PRESENT (v2.1)' if any(c.check_id == 'PST-GAP-004' and c.passed for c in checks) else '❌ MISSING'}")
    print()

    print(f"  Full report saved to: {output_path}")
    print()

    return 0 if not critical_fails else 1


if __name__ == "__main__":
    sys.exit(main())
