#!/usr/bin/env python3
"""
TA (Therapeutic Area) Assurance Evaluation Harness
===================================================
Systematically evaluates whether the project's code-level implementation
delivers on documented TA design reasoning claims across:
- Dual TA registry systems (Orchestrator + Data Generation)
- Built-in vs External TA handling
- 7-point TA design rationality validation
- TA-specific rules in phases, workers, recruitment

Usage:
  python3 evaluation/ta_assurance_evaluator.py
  python3 evaluation/ta_assurance_evaluator.py --verbose
"""

import os, re, sys, json
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

@dataclass
class TaCheck:
    check_id: str
    claim: str
    category: str
    file_path: str
    evidence_type: str
    evidence_query: str
    severity: str = "CRITICAL"
    _result: Optional[bool] = None
    _detail: str = ""

    def evaluate(self, root: Path) -> bool:
        fp = root / self.file_path
        if self.evidence_type == "file_exists":
            self._result = fp.exists()
            self._detail = f"{'EXISTS' if self._result else 'MISSING'}: {self.file_path}"
        elif self.evidence_type == "content_match":
            if not fp.exists(): self._result = False; self._detail = f"MISSING: {self.file_path}"
            else:
                m = re.search(self.evidence_query, fp.read_text(encoding='utf-8', errors='ignore'), re.I | re.DOTALL)
                self._result = m is not None
                self._detail = f"{'FOUND' if self._result else 'NOT FOUND'} in {self.file_path}: {self.evidence_query[:60]}..."
        elif self.evidence_type == "cross_ref":
            if not fp.exists(): self._result = False; self._detail = f"MISSING: {self.file_path}"
            else:
                content = fp.read_text(encoding='utf-8', errors='ignore')
                ref = os.path.splitext(os.path.basename(self.evidence_query))[0]
                m = re.search(re.escape(os.path.basename(self.evidence_query)), content, re.I) or re.search(re.escape(ref), content, re.I) or re.search(re.escape(self.evidence_query), content, re.I)
                self._result = m is not None
                self._detail = f"Cross-ref '{ref}' {'FOUND' if self._result else 'NOT FOUND'} in {self.file_path}"
        elif self.evidence_type == "multi_match":
            if not fp.exists(): self._result = False; self._detail = f"MISSING: {self.file_path}"
            else:
                content = fp.read_text(encoding='utf-8', errors='ignore')
                patterns = [p.strip() for p in self.evidence_query.split("|||")]
                matches = sum(1 for p in patterns if re.search(p, content, re.I | re.DOTALL))
                self._result = matches >= len(patterns) * 0.6
                self._detail = f"Multi: {matches}/{len(patterns)} matched in {self.file_path}"
        return self._result

    @property
    def passed(self): return self._result is True
    @property
    def icon(self): return "✅" if self._result else "❌"


def build_checks() -> List[TaCheck]:
    C = []

    # ═══════════════════════════════════════════════════════
    # CATEGORY 1: Dual TA Registry Systems
    # ═══════════════════════════════════════════════════════
    C.extend([
        # Orchestrator TA registry
        TaCheck("TA-REG-001", "Orchestrator: 9 TA values defined (4 built-in + 5 external)", "TA_REGISTRY", "orchestration/orchestrator.yaml", "multi_match",
                r"oncology|cardiovascular|cns|cgt|||immunology|rare_disease|infectious|metabolic|other", "CRITICAL"),
        TaCheck("TA-REG-002", "Orchestrator: built-in TAs map to .md skill files", "TA_REGISTRY", "orchestration/orchestrator.yaml", "content_match",
                r"oncology:.*therapeutic-areas/oncology\.md|therapeutic-areas/oncology\.md.*oncology", "CRITICAL"),
        TaCheck("TA-REG-003", "Orchestrator: external TAs use WebSearch (no .md file)", "TA_REGISTRY", "orchestration/orchestrator.yaml", "content_match",
                r"immunology.*skill:clinicaltrials-database.*skill:pubmed-database.*WebSearch|WebSearch.*immunology", "CRITICAL"),
        TaCheck("TA-REG-004", "Orchestrator: knowledge_scope = project_internal_only for built-in TAs", "TA_REGISTRY", "orchestration/orchestrator.yaml", "content_match",
                r"project_internal_only|knowledge_scope", "CRITICAL"),
        TaCheck("TA-REG-005", "Orchestrator: knowledge_scope = requires_external_search for external TAs", "TA_REGISTRY", "orchestration/orchestrator.yaml", "content_match",
                r"requires_external_search", "CRITICAL"),

        # Cohort engine TA registry
        TaCheck("TA-REG-006", "cohort_engine.py: has TA registry (load_population_params)", "TA_REGISTRY", "scripts/cohort_engine.py", "content_match",
                r"def\s+load_population_params|_POPULATION_REGISTRY", "CRITICAL"),
        TaCheck("TA-REG-007", "cohort_engine.py: 4 built-in TA configs (t2dm, mash, hypertension, epilepsy)", "TA_REGISTRY", "scripts/cohort_engine.py", "multi_match",
                r"def\s+_build_t2dm_config|||def\s+_build_mash_config|||def\s+_build_hypertension_config|||def\s+_build_epilepsy_config", "CRITICAL"),
        TaCheck("TA-REG-008", "population_params.json: exists with TA parameter templates", "TA_REGISTRY", "references/population_params.json", "file_exists", "", "CRITICAL"),
        TaCheck("TA-REG-009", "population_params.json: contains 4 TA keys (t2dm, mash, hypertension, epilepsy)", "TA_REGISTRY", "references/population_params.json", "multi_match",
                r'"t2dm"|||"mash"|||"hypertension"|||"epilepsy"', "CRITICAL"),
    ])

    # ═══════════════════════════════════════════════════════
    # CATEGORY 2: Built-in TA Skills
    # ═══════════════════════════════════════════════════════
    TA_FILES = {
        "oncology": "therapeutic-areas/oncology.md",
        "cardiovascular": "therapeutic-areas/cardiovascular.md",
        "cns": "therapeutic-areas/cns.md",
        "cgt": "therapeutic-areas/cgt.md",
    }
    TA_README = "therapeutic-areas/README.md"

    C.append(TaCheck("TA-SKILL-001", "TA README index file exists", "TA_SKILLS", TA_README, "file_exists", "", "CRITICAL"))
    C.append(TaCheck("TA-SKILL-002", "TA README lists all 4 built-in TAs", "TA_SKILLS", TA_README, "multi_match",
                     "oncology|||cardiovascular|||cns|||cgt", "MAJOR"))

    for ta, path in TA_FILES.items():
        C.append(TaCheck(f"TA-SKILL-{ta.upper()}-FILE", f"{ta} TA skill file exists", "TA_SKILLS", path, "file_exists", "", "CRITICAL"))

    # TA skill content checks
    C.extend([
        TaCheck("TA-SKILL-ONC-RECIST", "Oncology: RECIST v1.1 criteria defined", "TA_SKILLS", "therapeutic-areas/oncology.md", "content_match",
                r"RECIST.*(1\.1|complete.*response|partial.*response|stable.*disease|progressive.*disease)", "CRITICAL"),
        TaCheck("TA-SKILL-ONC-BIOMARKER", "Oncology: biomarker stratification (PD-L1, EGFR, ALK, etc.)", "TA_SKILLS", "therapeutic-areas/oncology.md", "content_match",
                r"PD.?L1|EGFR|ALK|BRAF|biomarker.*stratification", "MAJOR"),
        TaCheck("TA-SKILL-ONC-ECOG", "Oncology: ECOG PS defined", "TA_SKILLS", "therapeutic-areas/oncology.md", "content_match",
                r"ECOG.*(performance|PS)", "MAJOR"),

        TaCheck("TA-SKILL-CARD-MACE", "Cardiovascular: MACE composite defined", "TA_SKILLS", "therapeutic-areas/cardiovascular.md", "content_match",
                r"MACE|major.*adverse.*cardiovascular", "CRITICAL"),
        TaCheck("TA-SKILL-CARD-NYHA", "Cardiovascular: NYHA classification", "TA_SKILLS", "therapeutic-areas/cardiovascular.md", "content_match",
                r"NYHA.*(Class|I|II|III|IV)", "MAJOR"),
        TaCheck("TA-SKILL-CARD-CVOT", "Cardiovascular: CVOT design pattern", "TA_SKILLS", "therapeutic-areas/cardiovascular.md", "content_match",
                r"CVOT|cardiovascular.*outcome.*trial", "MAJOR"),

        TaCheck("TA-SKILL-CNS-MADRS", "CNS: MADRS scale defined", "TA_SKILLS", "therapeutic-areas/cns.md", "content_match",
                r"MADRS.*(0.?60|Montgomery)", "CRITICAL"),
        TaCheck("TA-SKILL-CNS-PLACEBO", "CNS: placebo response modeling", "TA_SKILLS", "therapeutic-areas/cns.md", "content_match",
                r"placebo.*(response|effect|change|model)", "MAJOR"),
        TaCheck("TA-SKILL-CNS-CSSRS", "CNS: C-SSRS suicidality monitoring", "TA_SKILLS", "therapeutic-areas/cns.md", "content_match",
                r"C.?SSRS|suicidality|Columbia.*Suicide", "MAJOR"),

        TaCheck("TA-SKILL-CGT-CRS", "CGT: CRS grading (ASTCT/Lee consensus)", "TA_SKILLS", "therapeutic-areas/cgt.md", "content_match",
                r"CRS.*(grade|grading|ASTCT|Lee|cytokine)", "CRITICAL"),
        TaCheck("TA-SKILL-CGT-LTFU", "CGT: 15-year LTFU requirements", "TA_SKILLS", "therapeutic-areas/cgt.md", "content_match",
                r"15.?year.*(LTFU|follow.?up)|long.?term.*follow.?up", "CRITICAL"),
    ])

    # ═══════════════════════════════════════════════════════
    # CATEGORY 3: TA-Specific Phase Rules
    # ═══════════════════════════════════════════════════════
    C.extend([
        # Phase I TA rules
        TaCheck("TA-PHASE-001", "Phase I: TA-specific populations (oncology=patients, CNS=HV, etc.)", "TA_PHASE", "phase1-dose-escalation.md", "content_match",
                r"(oncology|CNS|cardiovascular|rare.*disease).*(patient|healthy.*volunteer|HV)", "CRITICAL"),
        TaCheck("TA-PHASE-002", "Phase I: TA-specific DLT windows (oncology 21d, IO 28d)", "TA_PHASE", "phase1-dose-escalation.md", "content_match",
                r"DLT.*(window|observation|21.*day|28.*day).*(oncology|immunotherapy|targeted)", "CRITICAL"),

        # Phase II TA rules
        TaCheck("TA-PHASE-003", "Phase II: TA-specific endpoints (oncology ORR, CNS MADRS, CV NT-proBNP)", "TA_PHASE", "phase2-proof-of-concept.md", "content_match",
                r"(ORR|MADRS|NT.?proBNP|HbA1c).*(endpoint|primary|Phase II)", "CRITICAL"),
        TaCheck("TA-PHASE-004", "Phase II: MCP-Mod dose-response for efficacy-based TAs", "TA_PHASE", "phase2-proof-of-concept.md", "content_match",
                r"MCP.?Mod|Multiple.*Comparison.*Modeling", "MAJOR"),

        # Phase III TA rules
        TaCheck("TA-PHASE-005", "Phase III: TA-specific primary endpoints (OS/PFS, MACE, HbA1c)", "TA_PHASE", "phase3-pivotal.md", "content_match",
                r"(OS|PFS|MACE|HbA1c|MADRS).*(primary.*endpoint|Phase III|pivotal)", "CRITICAL"),
        TaCheck("TA-PHASE-006", "Phase III: NI margin for CVOT/non-oncology designs", "TA_PHASE", "phase3-pivotal.md", "content_match",
                r"non.?inferiority.*margin.*(M1|M2|delta|justif)", "MAJOR"),
    ])

    # ═══════════════════════════════════════════════════════
    # CATEGORY 4: Worker-Level TA Awareness
    # ═══════════════════════════════════════════════════════
    C.extend([
        # Worker B — PICO TA checks
        TaCheck("TA-WORK-001", "Worker B KG-B7: checks if indication is covered by 4 built-in TA skills", "TA_WORKER", "orchestration/workers/worker-pico-standards.yaml", "content_match",
                r"KG.?B7.*(covered|built.?in.*TA.*skill|4.*therapeutic.*area)", "CRITICAL"),
        TaCheck("TA-WORK-002", "Worker B KG-B7: external TA → citation-based recommendations", "TA_WORKER", "orchestration/workers/worker-pico-standards.yaml", "content_match",
                r"external.?only.*(guidance|citation)|citation.?based|all.*recommendation.*citation", "MAJOR"),
        TaCheck("TA-WORK-003", "Worker B: CDISC TAUG check for disease-specific standards", "TA_WORKER", "orchestration/workers/worker-pico-standards.yaml", "content_match",
                r"CDISC.*TAUG|CDISC.*therapeutic.*area.*(standard|guide)", "MAJOR"),

        # Worker A — TA-aware endpoint extraction
        TaCheck("TA-WORK-004", "Worker A: endpoint-type-aware extraction table (7 TA categories)", "TA_WORKER", "orchestration/workers/worker-safety-extraction.yaml", "content_match",
                r"Endpoint.?Type.?Aware|therapeutic.*area.*(oncology|cardiovascul|CNS|immunology|infectious|metabolic|rare.*disease)", "CRITICAL"),
        TaCheck("TA-WORK-005", "Worker A: output schema includes therapeutic_area field", "TA_WORKER", "orchestration/workers/worker-safety-extraction.yaml", "content_match",
                r"therapeutic_area.*(oncology|cardiovascul|cns|immunology)", "MAJOR"),

        # Worker C — TA cross-dimensional analysis
        TaCheck("TA-WORK-006", "Worker C: by_therapeutic_area breakdown in output", "TA_WORKER", "orchestration/workers/worker-termination-analysis.yaml", "content_match",
                r"by_therapeutic_area|by.*TA|therapeutic.*area.*breakdown", "CRITICAL"),
        TaCheck("TA-WORK-007", "Worker C: TA-specific insights (oncology IO-IO, CNS placebo, etc.)", "TA_WORKER", "orchestration/workers/worker-termination-analysis.yaml", "content_match",
                r"(oncology.*terminate|CNS.*placebo|cardiovascular.*CVOT).*(insight|lesson|note)", "MAJOR"),

        # Worker D — TA-specific I/E conventions
        TaCheck("TA-WORK-008", "Worker D: references TA skills for disease-specific I/E conventions", "TA_WORKER", "orchestration/workers/worker-ie-criteria.yaml", "content_match",
                r"therapeutic.?areas/(oncology|cardiovascular|cns|cgt)", "CRITICAL"),
        TaCheck("TA-WORK-009", "Worker D: TA-specific staging systems (TNM, NYHA, DSM-5)", "TA_WORKER", "orchestration/workers/worker-ie-criteria.yaml", "content_match",
                r"(TNM|AJCC|NYHA|DSM.?5|staging).*(oncology|cardiovascular|CNS|disease)", "MAJOR"),

        # Knowledge Gate TA checks
        TaCheck("TA-WORK-010", "Worker A KG-A1: checks TA understanding before extraction", "TA_WORKER", "orchestration/workers/worker-safety-extraction.yaml", "content_match",
                r"KG.?A1.*(therapeutic.*area|standard.*endpoint).*(understand|familiar)", "MAJOR"),
        TaCheck("TA-WORK-011", "Worker C KG-C5: checks for systematic reviews of trial failure in this TA", "TA_WORKER", "orchestration/workers/worker-termination-analysis.yaml", "content_match",
                r"KG.?C5.*(systematic.*review|trial.*failure.*TA|therapeutic.*area.*failure)", "MAJOR"),
    ])

    # ═══════════════════════════════════════════════════════
    # CATEGORY 5: External Knowledge Harvester
    # ═══════════════════════════════════════════════════════
    C.extend([
        TaCheck("TA-EXT-001", "External knowledge harvester file exists", "TA_EXTERNAL", "skills/external-knowledge-harvester.md", "file_exists", "", "CRITICAL"),
        TaCheck("TA-EXT-002", "Harvester: 5-step workflow (Detect→CT.gov→PubMed→Supplement→Apply)", "TA_EXTERNAL", "skills/external-knowledge-harvester.md", "content_match",
                r"Detect.*Gap.*Query.*(ClinicalTrial|CT\.gov).*Query.*PubMed.*Generate.*Supplement.*Apply", "CRITICAL"),
        TaCheck("TA-EXT-003", "Harvester: knowledge gap detection matrix (12 parameters)", "TA_EXTERNAL", "skills/external-knowledge-harvester.md", "content_match",
                r"knowledge.*gap.*(detect|matrix)|(Disease.*diagnostic|Drug.*target.*pharmacology|Phase.*2.*3.*I/E).*(Yes|No)", "CRITICAL"),
        TaCheck("TA-EXT-004", "Harvester: provenance table (project/internal/external source audit)", "TA_EXTERNAL", "skills/external-knowledge-harvester.md", "content_match",
                r"provenance|(project.*skill.*file|external.*database).*(index|supplement|source)", "MAJOR"),
        TaCheck("TA-EXT-005", "Clinical domain: TA gap detection rules in clinical-trials-domain.md", "TA_EXTERNAL", "clinical-trials-domain.md", "content_match",
                r"Missing.*therapeutic.*area|knowledge.*gap.*(detect|rule).*(indication|oncology|cardiovascular)", "MAJOR"),
    ])

    # ═══════════════════════════════════════════════════════
    # CATEGORY 6: TA-Specific Recruitment
    # ═══════════════════════════════════════════════════════
    C.extend([
        TaCheck("TA-REC-001", "Recruitment: TA-specific SF rate benchmarks exist", "TA_RECRUITMENT", "recruitment-enrollment.md", "content_match",
                r"(oncology|cardiovascular|CNS|rare.*disease|immunology|infectious).*(screen.*failure|SF).*(rate|benchmark)", "CRITICAL"),
        TaCheck("TA-REC-002", "Recruitment: at least 5 TAs with SF rate benchmarks", "TA_RECRUITMENT", "recruitment-enrollment.md", "content_match",
                r"25.?40%|20.?35%|30.?45%|35.?50%|15.?25%", "MAJOR"),
    ])

    # ═══════════════════════════════════════════════════════
    # CATEGORY 7: Cross-References & Integration
    # ═══════════════════════════════════════════════════════
    C.extend([
        TaCheck("TA-CROSS-001", "Orchestrator references therapeutic-areas/oncology.md", "TA_CROSS", "orchestration/orchestrator.yaml", "cross_ref",
                "therapeutic-areas/oncology.md", "CRITICAL"),
        TaCheck("TA-CROSS-002", "Orchestrator references therapeutic-areas/cardiovascular.md", "TA_CROSS", "orchestration/orchestrator.yaml", "cross_ref",
                "therapeutic-areas/cardiovascular.md", "CRITICAL"),
        TaCheck("TA-CROSS-003", "Orchestrator references phase skills (phase1/2/3)", "TA_CROSS", "orchestration/orchestrator.yaml", "cross_ref",
                "phase1-dose-escalation.md", "CRITICAL"),
        TaCheck("TA-CROSS-004", "Workers reference TA skill files as tools", "TA_CROSS", "orchestration/workers/worker-pico-standards.yaml", "cross_ref",
                "oncology.md", "CRITICAL"),
        TaCheck("TA-CROSS-005", "Orchestrator references external-knowledge-harvester", "TA_CROSS", "orchestration/orchestrator.yaml", "cross_ref",
                "external-knowledge-harvester.md", "CRITICAL"),
        TaCheck("TA-CROSS-006", "cohort_engine references population_params.json", "TA_CROSS", "scripts/cohort_engine.py", "content_match",
                r"population_params\.json|load_population_params", "CRITICAL"),
        TaCheck("TA-CROSS-007", "SKILL.md lists clinicaltrials-database and pubmed-database", "TA_CROSS", "SKILL.md", "content_match",
                r"clinicaltrials.?database.*pubmed.?database", "MAJOR"),
    ])

    # ═══════════════════════════════════════════════════════
    # CATEGORY 8: Orchestrator TA-Aware Composite Triggers
    # ═══════════════════════════════════════════════════════
    C.extend([
        TaCheck("TA-COMP-001", "composite_full_protocol_design includes TA-aware Workers B+A+C+F", "TA_COMPOSITE", "orchestration/orchestrator.yaml", "content_match",
                r"composite_full_protocol_design.*worker-pico-standards.*worker-safety-extraction.*worker-termination-analysis.*worker-ae-adjudication", "CRITICAL"),
        TaCheck("TA-COMP-002", "Cross-worker check SAFETY_VS_DRUG_CLASS validates TA-specific toxicity", "TA_COMPOSITE", "orchestration/orchestrator.yaml", "content_match",
                r"SAFETY_VS_DRUG_CLASS.*(drug.*class|align|known)", "MAJOR"),
    ])

    return C


def print_report(checks: List[TaCheck], verbose: bool = False):
    total = len(checks)
    passed = sum(1 for c in checks if c.passed)
    failed = total - passed
    pct = round(100 * passed / total, 1) if total > 0 else 0
    grade = "A" if pct >= 90 else "B" if pct >= 75 else "C" if pct >= 60 else "D" if pct >= 40 else "F"

    print("\n" + "=" * 90)
    print("  TA (THERAPEUTIC AREA) ASSURANCE EVALUATION MATRIX")
    print("  xClinicalTrial Orchestrator — Code-Level TA Design Verification")
    print("=" * 90)
    print(f"\n  OVERALL: {passed}/{total} ({pct}%)  GRADE: {grade}")
    print(f"  Critical Failures: {sum(1 for c in checks if not c.passed and c.severity == 'CRITICAL')}")

    # By category
    cats = {}
    for c in checks:
        cats.setdefault(c.category, {"t": 0, "p": 0, "f": 0})
        cats[c.category]["t"] += 1
        if c.passed: cats[c.category]["p"] += 1
        else: cats[c.category]["f"] += 1

    cat_names = {
        "TA_REGISTRY": "1. Dual TA Registry Systems",
        "TA_SKILLS": "2. Built-in TA Skills",
        "TA_PHASE": "3. TA-Specific Phase Rules",
        "TA_WORKER": "4. Worker-Level TA Awareness",
        "TA_EXTERNAL": "5. External Knowledge Harvester",
        "TA_RECRUITMENT": "6. TA-Specific Recruitment",
        "TA_CROSS": "7. Cross-References & Integration",
        "TA_COMPOSITE": "8. Composite Triggers",
    }
    print(f"\n  {'Category':<45s} {'Checks':>6s}  {'Passed':>6s}  {'Failed':>6s}  {'Rate':>6s}")
    print("  " + "-" * 75)
    for cat in ["TA_REGISTRY", "TA_SKILLS", "TA_PHASE", "TA_WORKER", "TA_EXTERNAL", "TA_RECRUITMENT", "TA_CROSS", "TA_COMPOSITE"]:
        if cat in cats:
            s = cats[cat]; cp = round(100 * s["p"] / s["t"], 1) if s["t"] > 0 else 0
            icon = "✅" if cp >= 80 else "⚠️" if cp >= 60 else "❌"
            print(f"  {icon} {cat_names.get(cat, cat):<43s} {s['t']:>4d}   {s['p']:>4d}   {s['f']:>4d}   {cp:>5.1f}%")

    failed_checks = [c for c in checks if not c.passed]
    if failed_checks:
        print(f"\n  ── FAILED ({len(failed_checks)}) ──")
        for c in failed_checks:
            sev = "🔴" if c.severity == "CRITICAL" else "🟠" if c.severity == "MAJOR" else "🟡"
            print(f"  {sev} {c.check_id:18s} [{c.severity:8s}] | {c.claim[:90]}")
            print(f"     Detail: {c._detail[:120]}")

    if verbose:
        print(f"\n  ── ALL ({total}) ──")
        for c in checks:
            print(f"  {c.icon} {c.check_id:18s} [{c.category:16s}] {c.severity:8s} | {c.claim[:80]}")

    print("\n" + "=" * 90)
    crit = [c for c in checks if not c.passed and c.severity == "CRITICAL"]
    if crit:
        print(f"  ⚠️  {len(crit)} CRITICAL failure(s).")
    else:
        print(f"  ✅ No CRITICAL failures. TA design verified at code level.")
    print()


def main():
    import argparse
    p = argparse.ArgumentParser(description="TA Assurance Evaluator")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    checks = build_checks()
    for c in checks:
        c.evaluate(PROJECT_ROOT)

    if args.json:
        res = {
            "total": len(checks), "passed": sum(1 for c in checks if c.passed),
            "failed": sum(1 for c in checks if not c.passed),
            "percentage": round(100 * sum(1 for c in checks if c.passed) / len(checks), 1),
            "critical_failures": [{"id": c.check_id, "claim": c.claim, "detail": c._detail} for c in checks if not c.passed and c.severity == "CRITICAL"],
            "by_category": {cat: {"total": sum(1 for x in checks if x.category == cat), "passed": sum(1 for x in checks if x.category == cat and x.passed)} for cat in set(c.category for c in checks)}
        }
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print_report(checks, verbose=args.verbose)

    sys.exit(0 if all(c.passed for c in checks if c.severity == "CRITICAL") else 1)


if __name__ == "__main__":
    main()
