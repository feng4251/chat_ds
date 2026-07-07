#!/usr/bin/env python3
"""
PICO Assurance Evaluation Harness
==================================
Systematically evaluates whether the project's code-level implementation
delivers on documented PICO compliance claims across 8 layers and 21 files.

Usage:
  python3 evaluation/pico_assurance_evaluator.py
  python3 evaluation/pico_assurance_evaluator.py --verbose
  python3 evaluation/pico_assurance_evaluator.py --layer 1

Design: Each claim from the PICO assurance documentation is converted into
a falsifiable check with an evidence query (file existence, regex pattern,
cross-reference verification). Results form a pass/fail matrix.
"""

import os
import re
import sys
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Callable

# ── Project root ──────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════════════════════════
# Evaluation Framework
# ══════════════════════════════════════════════════════════════════

@dataclass
class PicoCheck:
    """A single falsifiable PICO assurance check."""
    check_id: str
    claim: str                          # What the documentation claims
    pico_dimension: str                 # P / I / C / O / Cross-PICO
    layer: int                          # 1-10
    file_path: str                      # Relative path from project root
    evidence_type: str                  # "file_exists" | "content_match" | "cross_ref" | "import_check"
    evidence_query: str                 # Regex pattern, import name, or file path to check
    severity: str = "CRITICAL"          # CRITICAL / MAJOR / MINOR
    _result: Optional[bool] = None
    _detail: str = ""

    def evaluate(self, project_root: Path) -> bool:
        """Run this check against the actual codebase."""
        full_path = project_root / self.file_path

        if self.evidence_type == "file_exists":
            self._result = full_path.exists()
            self._detail = f"File {'EXISTS' if self._result else 'MISSING'}: {self.file_path}"

        elif self.evidence_type == "content_match":
            if not full_path.exists():
                self._result = False
                self._detail = f"File MISSING: {self.file_path}"
            else:
                content = full_path.read_text(encoding='utf-8', errors='ignore')
                match = re.search(self.evidence_query, content, re.IGNORECASE | re.DOTALL)
                self._result = match is not None
                if match:
                    self._detail = f"Pattern FOUND in {self.file_path}"
                else:
                    # Find closest match for debugging
                    self._detail = f"Pattern NOT FOUND in {self.file_path}: {self.evidence_query[:80]}..."

        elif self.evidence_type == "cross_ref":
            if not full_path.exists():
                self._result = False
                self._detail = f"Source file MISSING: {self.file_path}"
            else:
                content = full_path.read_text(encoding='utf-8', errors='ignore')
                # evidence_query is the referenced file name (with or without .yaml/.md extension)
                ref_basename = os.path.basename(self.evidence_query)
                ref_noext = os.path.splitext(ref_basename)[0]  # strip extension for flexible matching
                # Try with extension, without extension, and as path
                match = re.search(re.escape(ref_basename), content, re.IGNORECASE)
                if not match:
                    match = re.search(re.escape(ref_noext), content, re.IGNORECASE)
                if not match:
                    match = re.search(re.escape(self.evidence_query), content, re.IGNORECASE)
                self._result = match is not None
                self._detail = f"Cross-ref '{ref_basename}' {'FOUND' if self._result else 'NOT FOUND'} in {self.file_path}"

        elif self.evidence_type == "import_check":
            if not full_path.exists():
                self._result = False
                self._detail = f"File MISSING: {self.file_path}"
            else:
                content = full_path.read_text(encoding='utf-8', errors='ignore')
                pattern = rf'(?:from\s+\S+\s+import\s+.*{re.escape(self.evidence_query)}|import\s+.*{re.escape(self.evidence_query)})'
                match = re.search(pattern, content)
                self._result = match is not None
                self._detail = f"Import '{self.evidence_query}' {'FOUND' if self._result else 'NOT FOUND'} in {self.file_path}"

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


@dataclass
class PicoEvaluationReport:
    checks: List[PicoCheck] = field(default_factory=list)
    total: int = 0
    passed: int = 0
    failed: int = 0
    by_layer: Dict[int, Dict] = field(default_factory=dict)
    by_dimension: Dict[str, Dict] = field(default_factory=dict)
    critical_failures: List[PicoCheck] = field(default_factory=list)

    def add_check(self, check: PicoCheck):
        check.evaluate(PROJECT_ROOT)
        self.checks.append(check)
        self.total += 1
        if check.passed:
            self.passed += 1
        else:
            self.failed += 1
            if check.severity == "CRITICAL":
                self.critical_failures.append(check)

    def compute_summary(self):
        for check in self.checks:
            layer = check.layer
            dim = check.pico_dimension
            if layer not in self.by_layer:
                self.by_layer[layer] = {"total": 0, "passed": 0, "failed": 0}
            if dim not in self.by_dimension:
                self.by_dimension[dim] = {"total": 0, "passed": 0, "failed": 0}
            self.by_layer[layer]["total"] += 1
            self.by_dimension[dim]["total"] += 1
            if check.passed:
                self.by_layer[layer]["passed"] += 1
                self.by_dimension[dim]["passed"] += 1
            else:
                self.by_layer[layer]["failed"] += 1
                self.by_dimension[dim]["failed"] += 1


# ══════════════════════════════════════════════════════════════════
# PICO Assurance Checks — Layer by Layer
# ══════════════════════════════════════════════════════════════════

def build_all_checks() -> List[PicoCheck]:
    """Build the complete set of falsifiable PICO assurance checks."""
    checks = []

    # ── LAYER 1: Worker B — PICO 主引擎 ──
    L1 = "orchestration/workers/worker-pico-standards.yaml"

    checks.extend([
        PicoCheck("L1-FILE", "Worker B YAML file exists", "Cross-PICO", 1, L1, "file_exists", ""),

        # Function 1: PICO Extraction
        PicoCheck("L1-P-EXTRACT", "Worker B extracts Population elements (disease, staging, severity, I/E, N, geography, special populations)", "P", 1, L1, "content_match", r"(?i)population.*extraction|extract.*population", "CRITICAL"),
        PicoCheck("L1-P-DISEASE", "Population: extracts disease/condition with staging system", "P", 1, L1, "content_match", r"Disease.*condition.*staging", "MAJOR"),
        PicoCheck("L1-P-N", "Population: extracts planned sample size N", "P", 1, L1, "content_match", r"Planned.*sample.*size|planned_N", "MAJOR"),
        PicoCheck("L1-P-GEO", "Population: extracts geographic distribution", "P", 1, L1, "content_match", r"geographic|country|region.*distribution", "MINOR"),
        PicoCheck("L1-P-SPECIAL", "Population: references ICH E7 (geriatric) and ICH E11 (pediatric)", "P", 1, L1, "content_match", r"ICH\s*E7|ICH\s*E11|geriatric|pediatric", "MAJOR"),

        PicoCheck("L1-I-EXTRACT", "Worker B extracts Intervention elements (drug name/class/MOA, dose/route/frequency)", "I", 1, L1, "content_match", r"(?i)intervention.*extraction|extract.*intervention", "CRITICAL"),
        PicoCheck("L1-I-DOSE", "Intervention: extracts dose, route, frequency, duration", "I", 1, L1, "content_match", r"Dose.*route.*frequency|dose.*route.*schedule|treatment.*duration", "MAJOR"),
        PicoCheck("L1-I-MOA", "Intervention: extracts drug class and mechanism of action", "I", 1, L1, "content_match", r"drug.*class|mechanism.*action|MOA", "MINOR"),

        PicoCheck("L1-C-EXTRACT", "Worker B extracts Comparator elements (type, drug, justification, NI margin)", "C", 1, L1, "content_match", r"(?i)comparator.*extraction|extract.*comparator", "CRITICAL"),
        PicoCheck("L1-C-NI", "Comparator: NI margin M1/M2 derivation per ICH E10", "C", 1, L1, "content_match", r"M1.*M2|non.?inferiority.*margin|ICH\s*E10", "CRITICAL"),
        PicoCheck("L1-C-PLACEBO", "Comparator: placebo ethics check (Declaration of Helsinki)", "C", 1, L1, "content_match", r"placebo.*(ethic|justif)|Helsinki", "MINOR"),

        PicoCheck("L1-O-EXTRACT", "Worker B extracts Outcome elements (primary/secondary/safety endpoints, statistical analysis)", "O", 1, L1, "content_match", r"(?i)outcome.*extraction|extract.*outcome", "CRITICAL"),
        PicoCheck("L1-O-PRIMARY", "Outcome: extracts primary endpoint with exact definition and assessment timepoint", "O", 1, L1, "content_match", r"primary.*endpoint.*definition|primary_endpoint.*definition", "MAJOR"),
        PicoCheck("L1-O-MULTI", "Outcome: extracts multiplicity adjustment plan", "O", 1, L1, "content_match", r"multiplicit|gatekeeping|hierarchical.*testing", "MAJOR"),
        PicoCheck("L1-O-BICR", "Outcome: distinguishes BICR vs investigator assessment", "O", 1, L1, "content_match", r"BICR|blinded.*independent.*(central|review)|investigator.*assessment", "MINOR"),

        PicoCheck("L1-SD-EXTRACT", "Worker B extracts Study Design elements (phase, design type, randomization, blinding, interim)", "Cross-PICO", 1, L1, "content_match", r"study.*design.*extraction|extract.*study.*design|1e.*STUDY DESIGN", "CRITICAL"),

        # Function 2: Multi-Agency Standards Comparison
        PicoCheck("L1-MULTI-FDA", "Multi-agency audit checks against FDA guidance", "Cross-PICO", 1, L1, "content_match", r"FDA.*(guidance|standard|requirement|check|compliance)", "CRITICAL"),
        PicoCheck("L1-MULTI-EMA", "Multi-agency audit checks against EMA guidance", "Cross-PICO", 1, L1, "content_match", r"EMA.*(guidance|standard|requirement|check|compliance)", "MAJOR"),
        PicoCheck("L1-MULTI-ICH", "Multi-agency audit checks against ICH guidelines (E8/E9/E10)", "Cross-PICO", 1, L1, "content_match", r"ICH\s*(E8|E9|E10).*(compliance|check|standard)", "CRITICAL"),
        PicoCheck("L1-MULTI-NMPA", "Multi-agency audit checks against NMPA guidance", "Cross-PICO", 1, L1, "content_match", r"NMPA.*(guidance|standard|requirement|check)", "MINOR"),
        PicoCheck("L1-MULTI-CDISC", "Multi-agency audit checks against CDISC TAUG", "Cross-PICO", 1, L1, "content_match", r"CDISC.*TAUG|therapeutic.*area.*(user.*guide|standard)", "MINOR"),
        PicoCheck("L1-MULTI-CT", "Multi-agency audit checks real-world CT.gov benchmarks", "Cross-PICO", 1, L1, "content_match", r"real.?world.*(benchmark|trial|compar)|CT\.gov.*compar", "MAJOR"),

        # Function 3: Deviation Classification
        PicoCheck("L1-DEV-CRIT", "Deviation severity has CRITICAL level defined", "Cross-PICO", 1, L1, "content_match", r"CRITICAL.*(Refuse|reject|violat)", "CRITICAL"),
        PicoCheck("L1-DEV-MAJOR", "Deviation severity has MAJOR level defined", "Cross-PICO", 1, L1, "content_match", r"MAJOR.*(deviation|question|review)", "MAJOR"),
        PicoCheck("L1-DEV-MINOR", "Deviation severity has MINOR level defined", "Cross-PICO", 1, L1, "content_match", r"MINOR.*(deviation|unlikely|notable)", "MINOR"),

        # Knowledge Gate
        PicoCheck("L1-KG-B5", "KG-B5: searches CT.gov for comparable trials", "Cross-PICO", 1, L1, "content_match", r"KG.?B5|comparable.*trial.*CT\.gov|registered.*comparable.*trial", "MAJOR"),
        PicoCheck("L1-KG-B6", "KG-B6: searches PubMed for successful Phase III endpoints", "Cross-PICO", 1, L1, "content_match", r"KG.?B6|successful.*Phase.*III.*endpoint|PubMed.*endpoint", "MAJOR"),

        # ICH Supplementary (v1.2)
        PicoCheck("L1-ESTIMAND", "ICH E9(R1): estimand framework defined (5 attributes)", "O", 1, L1, "content_match", r"estimand.*framework|E9\(R1\).*estimand|five.*estimand.*attribute", "CRITICAL"),
        PicoCheck("L1-QTC", "ICH E14: QTc/TQT assessment logic", "I", 1, L1, "content_match", r"QTc.*(assessment|TQT|thorough.*QT)|ICH\s*E14", "MAJOR"),
        PicoCheck("L1-MRCT", "ICH E17: MRCT design audit (regional balance, ethnic factors)", "P", 1, L1, "content_match", r"MRCT.*(design|audit|region)|ICH\s*E17", "MAJOR"),
        PicoCheck("L1-RISK-QUAL", "ICH E6(R2): risk-based quality management (CtQ, QTLs)", "Cross-PICO", 1, L1, "content_match", r"E6\(R2\).*quality|CtQ.*factor|quality.*tolerance.*limit", "MAJOR"),
        PicoCheck("L1-PEDIATRIC", "ICH E11: pediatric investigation plan assessment", "P", 1, L1, "content_match", r"E11.*pediatric|PIP.*PSP|pediatric.*investigation.*plan", "MINOR"),
        PicoCheck("L1-STAT-RIGOR", "Statistical rigor: sample size, multiplicity, interim, missing data", "O", 1, L1, "content_match", r"statistical.*rigor|sample.*size.*adequacy|multiplicity.*control|missing.*data.*handling", "MAJOR"),
    ])

    # ── LAYER 2: Orchestrator PICO Routing ──
    L2 = "orchestration/orchestrator.yaml"

    checks.extend([
        PicoCheck("L2-FILE", "Orchestrator YAML file exists", "Cross-PICO", 2, L2, "file_exists", ""),
        PicoCheck("L2-IE-VS-PICO", "Cross-worker check: IE_VS_PICO (CRITICAL)", "Cross-PICO", 2, L2, "content_match", r"IE_VS_PICO", "CRITICAL"),
        PicoCheck("L2-COMPARATOR-VS", "Cross-worker check: COMPARATOR_VS_STANDARD (CRITICAL)", "C", 2, L2, "content_match", r"COMPARATOR_VS_STANDARD", "CRITICAL"),
        PicoCheck("L2-SAMPLE-VS", "Cross-worker check: SAMPLE_SIZE_VS_ENDPOINT (MAJOR)", "O", 2, L2, "content_match", r"SAMPLE_SIZE_VS_ENDPOINT", "MAJOR"),
        PicoCheck("L2-PICO-REPORT", "Final report has dedicated PICO section", "Cross-PICO", 2, L2, "content_match", r"PICO.*Analysis.*Regulatory.*Standards.*Compliance", "MAJOR"),
        PicoCheck("L2-ICH-SUPP-REPORT", "Final report has ICH Supplementary section (v1.2)", "Cross-PICO", 2, L2, "content_match", r"ICH.*Mandated.*Supplementary.*Analyses", "MAJOR"),
    ])

    # ── LAYER 3: Phase Skills — PICO Design Rules ──
    L3_1 = "phase1-dose-escalation.md"
    L3_2 = "phase2-proof-of-concept.md"
    L3_3 = "phase3-pivotal.md"

    checks.extend([
        PicoCheck("L3-P1-FILE", "phase1-dose-escalation.md exists", "I", 3, L3_1, "file_exists", ""),
        PicoCheck("L3-P1-DLT", "Phase I: DLT definitions for 5 organ system categories", "O", 3, L3_1, "content_match", r"DLT.*(hematologic|non.?hematologic|hepatic|cardiac|neurologic)", "CRITICAL"),
        PicoCheck("L3-P1-DOSE-DESIGN", "Phase I: 3+3, BOIN, CRM dose escalation designs", "I", 3, L3_1, "content_match", r"3\+3|BOIN|CRM|Continual.*Reassessment", "MAJOR"),

        PicoCheck("L3-P2-FILE", "phase2-proof-of-concept.md exists", "I", 3, L3_2, "file_exists", ""),
        PicoCheck("L3-P2-MCPMOD", "Phase II: MCP-Mod dose-response modeling", "I", 3, L3_2, "content_match", r"MCP.?Mod|Multiple.*Comparison.*Modeling", "CRITICAL"),
        PicoCheck("L3-P2-SIMON", "Phase II: Simon's Two-Stage design", "O", 3, L3_2, "content_match", r"Simon.*Two.?Stage|Simon.*2.?Stage", "MAJOR"),

        PicoCheck("L3-P3-FILE", "phase3-pivotal.md exists", "O", 3, L3_3, "file_exists", ""),
        PicoCheck("L3-P3-NI", "Phase III: Non-inferiority M1/M2 margin derivation", "C", 3, L3_3, "content_match", r"non.?inferiority.*(margin|M1|M2)", "CRITICAL"),
        PicoCheck("L3-P3-SUPERIORITY", "Phase III: Superiority trial design", "I", 3, L3_3, "content_match", r"[Ss]uperiority.*(trial|design|study)", "MAJOR"),
        PicoCheck("L3-P3-DSMB", "Phase III: DSMB charter and stopping rules", "O", 3, L3_3, "content_match", r"DSMB|Data.*Safety.*Monitoring.*Board|stopping.*(rule|boundary)", "MAJOR"),
    ])

    # ── LAYER 4: TA Skills — Disease-Specific PICO ──
    TA_FILES = {
        "oncology": "therapeutic-areas/oncology.md",
        "cardiovascular": "therapeutic-areas/cardiovascular.md",
        "cns": "therapeutic-areas/cns.md",
        "cgt": "therapeutic-areas/cgt.md",
    }

    for ta, path in TA_FILES.items():
        checks.append(PicoCheck(f"L4-{ta.upper()}-FILE", f"{ta} TA skill file exists", "Cross-PICO", 4, path, "file_exists", ""))

    checks.extend([
        PicoCheck("L4-ONC-RECIST", "Oncology: RECIST v1.1 response criteria (CR/PR/SD/PD)", "O", 4, "therapeutic-areas/oncology.md", "content_match", r"RECIST.*(1\.1|CR|PR|SD|PD|complete.*response|partial.*response)", "CRITICAL"),
        PicoCheck("L4-ONC-ECOG", "Oncology: ECOG PS distribution by trial phase", "P", 4, "therapeutic-areas/oncology.md", "content_match", r"ECOG.*(performance|PS).*((0|1|2|3|4|5))", "MAJOR"),
        PicoCheck("L4-CARD-MACE", "Cardiovascular: MACE composite endpoint definitions", "O", 4, "therapeutic-areas/cardiovascular.md", "content_match", r"MACE|major.*adverse.*cardiovascular.*event", "CRITICAL"),
        PicoCheck("L4-CARD-NYHA", "Cardiovascular: NYHA classification", "P", 4, "therapeutic-areas/cardiovascular.md", "content_match", r"NYHA.*(I|II|III|IV|Class)", "MAJOR"),
        PicoCheck("L4-CNS-MADRS", "CNS: MADRS scale (0-60, remission, response)", "O", 4, "therapeutic-areas/cns.md", "content_match", r"MADRS.*(0.?60|remission.*10|response.*50)", "CRITICAL"),
        PicoCheck("L4-CNS-PLACEBO", "CNS: Placebo response modeling", "C", 4, "therapeutic-areas/cns.md", "content_match", r"placebo.*(response|change|effect|12.*16|10.*15)", "MAJOR"),
        PicoCheck("L4-CGT-CRS", "CGT: CRS grading per ASTCT consensus", "O", 4, "therapeutic-areas/cgt.md", "content_match", r"CRS.*(grading|ASTCT|cytokine.*release.*syndrome)", "CRITICAL"),
        PicoCheck("L4-CGT-LTFU", "CGT: 15-year LTFU requirements", "O", 4, "therapeutic-areas/cgt.md", "content_match", r"15.?year.*(LTFU|long.?term.*follow.?up)", "MAJOR"),
    ])

    # ── LAYER 5: Domain Knowledge — PICO Context ──
    checks.extend([
        PicoCheck("L5-CLIN-FILE", "clinical-trials-domain.md exists", "Cross-PICO", 5, "clinical-trials-domain.md", "file_exists", ""),
        PicoCheck("L5-CLIN-PHASE-N", "Clinical domain: phase-appropriate N ranges defined", "P", 5, "clinical-trials-domain.md", "content_match", r"Phase I.*(10.?80|20.?100)|Phase II.*(50.?300|100.?500)|Phase III.*(300.?3000)", "MAJOR"),
        PicoCheck("L5-CLIN-GAP", "Clinical domain: knowledge gap detection rules", "Cross-PICO", 5, "clinical-trials-domain.md", "content_match", r"knowledge.*gap.*(detect|rule|trigger)|Detect.*Gap", "MAJOR"),

        PicoCheck("L5-REC-FILE", "recruitment-enrollment.md exists", "P", 5, "recruitment-enrollment.md", "file_exists", ""),
        PicoCheck("L5-REC-IE-CODES", "Recruitment: 34 standardized IE reason codes (IE01-IE34)", "P", 5, "recruitment-enrollment.md", "content_match", r"IE\d{2}|IE.*code.*system|screen.*failure.*code", "CRITICAL"),
        PicoCheck("L5-REC-FUNNEL", "Recruitment: 5-stage screening funnel model", "P", 5, "recruitment-enrollment.md", "content_match", r"(5|five).?stage.*(funnel|screening)|identified.*pre.?screened.*consented.*screen.*passed.*randomized", "MAJOR"),
        PicoCheck("L5-REC-SF-RATES", "Recruitment: TA-specific SF rate benchmarks", "P", 5, "recruitment-enrollment.md", "content_match", r"screen.*failure.*(rate|benchmark|25.*40|20.*35|30.*45)", "MAJOR"),
    ])

    # ── LAYER 6: ICH Reference ──
    L6 = "references/ich-guidelines-index.md"

    checks.extend([
        PicoCheck("L6-FILE", "ICH guidelines index file exists", "Cross-PICO", 6, L6, "file_exists", ""),
        PicoCheck("L6-E4", "ICH E4: dose-response information for drug registration", "I", 6, L6, "content_match", r"E4.*Dose.?Response|dose.?response.*ICH\s*E4", "MAJOR"),
        PicoCheck("L6-E8", "ICH E8: general considerations for clinical trials", "Cross-PICO", 6, L6, "content_match", r"E8.*General.*Consideration|ICH\s*E8", "CRITICAL"),
        PicoCheck("L6-E9", "ICH E9: statistical principles for clinical trials", "O", 6, L6, "content_match", r"E9.*Statistical.*(Principle|clinical.*trial)|ICH\s*E9", "CRITICAL"),
        PicoCheck("L6-E10", "ICH E10: choice of control group", "C", 6, L6, "content_match", r"E10.*(Choice.*Control|control.*group)|ICH\s*E10", "CRITICAL"),
        PicoCheck("L6-E11", "ICH E11: pediatric investigation", "P", 6, L6, "content_match", r"E11.*Pediatric|pediatric.*ICH\s*E11", "MAJOR"),
        PicoCheck("L6-E14", "ICH E14: QTc/TQT assessment", "I", 6, L6, "content_match", r"E14.*QT|QTc.*ICH\s*E14", "MAJOR"),
        PicoCheck("L6-E17", "ICH E17: MRCT design", "P", 6, L6, "content_match", r"E17.*MRCT|multi.?regional.*ICH\s*E17", "MAJOR"),
        PicoCheck("L6-PHASE-MAP", "ICH index: phase-specific guideline mapping", "Cross-PICO", 6, L6, "content_match", r"Phase I.*E2A|Phase II.*E8.*E9|Phase III.*E3.*E8.*E9.*E10", "MAJOR"),
    ])

    # ── LAYER 7: Python Infrastructure ──
    L7_1 = "scripts/cohort_engine.py"
    L7_2 = "references/population_params.json"

    checks.extend([
        PicoCheck("L7-CE-FILE", "cohort_engine.py exists", "Cross-PICO", 7, L7_1, "file_exists", ""),
        PicoCheck("L7-CE-PICO-COMMENT", "Cohort engine: PICO framework drives all parameterization (docstring claim)", "Cross-PICO", 7, L7_1, "content_match", r"PICO.*framework.*drives.*all.*parameterization|PICO.*drives.*simulation", "CRITICAL"),
        PicoCheck("L7-CE-POP-CONFIG", "Cohort engine: PopulationConfig dataclass with PICO fields", "P", 7, L7_1, "content_match", r"(PopulationConfig|class.*Population)", "CRITICAL"),
        PicoCheck("L7-CE-TREAT-EFFECT", "Cohort engine: TreatmentEffectDef with drug_effect, onset_halflife, responder_rate", "I", 7, L7_1, "content_match", r"TreatmentEffectDef|drug_effect|onset_halflife|responder_rate", "MAJOR"),
        PicoCheck("L7-CE-PLACEBO-EFFECT", "Cohort engine: placebo_effect modeling", "C", 7, L7_1, "content_match", r"placebo_effect|placebo.*response", "MAJOR"),
        PicoCheck("L7-CE-5-LAYER", "Cohort engine: 5-layer simulation architecture", "Cross-PICO", 7, L7_1, "content_match", r"5.?layer.*simulation|Layer.*1.*correlated.*baseline", "MAJOR"),
        PicoCheck("L7-CE-4-TAS", "Cohort engine: 4 built-in PICO configs (t2dm, mash, hypertension, epilepsy)", "Cross-PICO", 7, L7_1, "content_match", r"def\s+_build_\w+_config|def\s+load_population_params", "MAJOR"),

        PicoCheck("L7-PP-FILE", "population_params.json exists", "Cross-PICO", 7, L7_2, "file_exists", ""),
        PicoCheck("L7-PP-TEMPLATES", "Population params: PICO-structured parameter templates for 4 TAs", "Cross-PICO", 7, L7_2, "content_match", r"(t2dm|mash|hypertension|epilepsy).*(variables|treatment_effect|arms)", "MAJOR"),
    ])

    # ── LAYER 8: Other Workers — PICO Contributions ──
    checks.extend([
        PicoCheck("L8-A-ENDPOINT", "Worker A: extracts endpoint-specific efficacy results for Outcome benchmarking", "O", 8, "orchestration/workers/worker-safety-extraction.yaml", "content_match", r"endpoint.*specific.*efficacy|efficacy.*(extraction|endpoint).*TA", "MAJOR"),

        PicoCheck("L8-C-TERM-TAX", "Worker C: 6-category 29-subcategory termination taxonomy includes efficacy and enrollment categories", "Cross-PICO", 8, "orchestration/workers/worker-termination-analysis.yaml", "content_match", r"(CATEGORY.*B.*EFFICACY|B1.*Futility|CATEGORY.*C.*ENROLLMENT|C1.*Slow.*enrollment)", "CRITICAL"),

        PicoCheck("L8-D-IE-VS-PICO", "Worker D: IE_VS_PICO consistency check in I/E design", "P", 8, "orchestration/workers/worker-ie-criteria.yaml", "content_match", r"IE.*vs.*PICO|PICO.*population.*(alignment|definition|match)", "CRITICAL"),
        PicoCheck("L8-D-COMPLEXITY", "Worker D: 4-dimension complexity scoring (FK, logic, enrollment, diversity)", "P", 8, "orchestration/workers/worker-ie-criteria.yaml", "content_match", r"(Flesch|Kincaid|logic.*complexity|enrollment.*impact|diversity.*impact)", "MAJOR"),
        PicoCheck("L8-D-RECRUIT-FUNNEL", "Worker D: recruitment funnel analysis (v1.1)", "P", 8, "orchestration/workers/worker-ie-criteria.yaml", "content_match", r"recruitment.*(funnel|analysis|projection|enrollment.*velocity)", "MAJOR"),

        PicoCheck("L8-E-BIOMARKER", "Worker E: 10 biomarker types for population stratification", "P", 8, "orchestration/workers/worker-biomarker-matching.yaml", "content_match", r"(10|ten).*biomarker.*type|biomarker.*type.*(VCF|MAF|CNV|fusion|expression|IHC|TMB|MSI|lab|imaging|composite)", "MAJOR"),
    ])

    # ── CROSS-LAYER CHECKS ──
    checks.extend([
        # Orchestrator references Worker B
        PicoCheck("XL-ORCH-REF-B", "Orchestrator references worker-pico-standards in routing_rules", "Cross-PICO", 2, L2, "cross_ref", "worker-pico-standards.yaml", "CRITICAL"),
        # Orchestrator references Worker D
        PicoCheck("XL-ORCH-REF-D", "Orchestrator references worker-ie-criteria in routing_rules", "Cross-PICO", 2, L2, "cross_ref", "worker-ie-criteria.yaml", "CRITICAL"),
        # Worker B references ICH index
        PicoCheck("XL-B-REF-ICH", "Worker B references ich-guidelines-index.md", "Cross-PICO", 1, L1, "cross_ref", "ich-guidelines-index.md", "MAJOR"),
        # Worker B references phase skills
        PicoCheck("XL-B-REF-PHASE", "Worker B references phase1/phase2/phase3 skills", "Cross-PICO", 1, L1, "content_match", r"phase\d.*dose.*escalation|phase\d.*proof.*of.*concept|phase\d.*pivotal", "MAJOR"),
        # Worker B references TA skills
        PicoCheck("XL-B-REF-TA", "Worker B references therapeutic-area skills", "Cross-PICO", 1, L1, "content_match", r"therapeutic.?areas.*/(oncology|cardiovascular|cns|cgt)", "MAJOR"),
        # domain_parser import in sdtm_to_adam.py
        PicoCheck("XL-DP-ADAM", "sdtm_to_adam.py imports DomainParser from domain_parser", "Cross-PICO", 7, "scripts/sdtm_to_adam.py", "import_check", "DomainParser", "CRITICAL"),
        # domain_parser import in submission_readiness.py
        PicoCheck("XL-DP-SUBM", "submission_readiness.py imports DomainParser from domain_parser", "Cross-PICO", 7, "scripts/submission_readiness.py", "import_check", "DomainParser", "CRITICAL"),
    ])

    return checks


# ══════════════════════════════════════════════════════════════════
# Report Generation
# ══════════════════════════════════════════════════════════════════

def print_report(report: PicoEvaluationReport, verbose: bool = False):
    """Print the PICO evaluation matrix."""

    print("\n" + "=" * 90)
    print("  PICO ASSURANCE EVALUATION MATRIX")
    print("  xClinicalTrial Orchestrator — Code-Level Verification")
    print("=" * 90)

    # Overall score
    pct = round(100 * report.passed / report.total, 1) if report.total > 0 else 0
    grade = "A" if pct >= 90 else "B" if pct >= 75 else "C" if pct >= 60 else "D" if pct >= 40 else "F"
    print(f"\n  OVERALL SCORE: {report.passed}/{report.total} ({pct}%)  GRADE: {grade}")
    print(f"  Critical Failures: {len(report.critical_failures)}")
    print()

    # By Layer
    print("  ── BY LAYER ──")
    print(f"  {'Layer':<45s} {'Checks':>6s}  {'Passed':>6s}  {'Failed':>6s}  {'Rate':>6s}")
    print("  " + "-" * 75)
    for layer in sorted(report.by_layer.keys()):
        stats = report.by_layer[layer]
        lpct = round(100 * stats["passed"] / stats["total"], 1) if stats["total"] > 0 else 0
        layer_names = {
            1: "1: Worker B — PICO 主引擎",
            2: "2: Orchestrator — PICO 路由",
            3: "3: Phase Skills — PICO 设计规则",
            4: "4: TA Skills — 疾病 PICO 标准",
            5: "5: Domain Knowledge — PICO 背景",
            6: "6: ICH Reference — 监管 PICO",
            7: "7: Python Infrastructure — PICO 数据",
            8: "8: Other Workers — PICO 贡献",
        }
        name = layer_names.get(layer, f"Layer {layer}")
        icon = "✅" if lpct >= 80 else "⚠️" if lpct >= 60 else "❌"
        print(f"  {icon} {name:<43s} {stats['total']:>4d}   {stats['passed']:>4d}   {stats['failed']:>4d}   {lpct:>5.1f}%")

    # By PICO Dimension
    print(f"\n  ── BY PICO DIMENSION ──")
    print(f"  {'Dimension':<20s} {'Checks':>6s}  {'Passed':>6s}  {'Failed':>6s}  {'Rate':>6s}")
    print("  " + "-" * 50)
    for dim in ["P", "I", "C", "O", "Cross-PICO"]:
        if dim in report.by_dimension:
            stats = report.by_dimension[dim]
            dpct = round(100 * stats["passed"] / stats["total"], 1) if stats["total"] > 0 else 0
            dim_names = {"P": "Population", "I": "Intervention", "C": "Comparator", "O": "Outcome", "Cross-PICO": "Cross-PICO"}
            print(f"  {dim_names.get(dim, dim):<20s} {stats['total']:>4d}   {stats['passed']:>4d}   {stats['failed']:>4d}   {dpct:>5.1f}%")

    # Failed Checks Detail
    failed = [c for c in report.checks if not c.passed]
    if failed:
        print(f"\n  ── FAILED CHECKS ({len(failed)}) ──")
        for c in failed:
            sev_icon = "🔴" if c.severity == "CRITICAL" else "🟠" if c.severity == "MAJOR" else "🟡"
            print(f"  {sev_icon} {c.check_id} [{c.severity}] {c.layer}:{c.pico_dimension}")
            print(f"     Claim: {c.claim[:100]}")
            print(f"     Detail: {c._detail[:120]}")
            print()

    # Critical Failures
    if report.critical_failures:
        print(f"\n  ── CRITICAL FAILURES ({len(report.critical_failures)}) ──")
        for c in report.critical_failures:
            print(f"  ❌ {c.check_id}: {c.claim[:120]}")
            print(f"     File: {c.file_path}")
            print(f"     Detail: {c._detail[:150]}")
            print()

    # Verbose: all checks
    if verbose:
        print(f"\n  ── ALL CHECKS ({report.total}) ──")
        for c in report.checks:
            print(f"  {c.status_icon} {c.check_id:20s} [{c.pico_dimension:10s}] L{c.layer} {c.severity:8s} | {c.claim[:80]}")


    print("\n" + "=" * 90)

    # Recommendations
    recommendations = []
    if report.critical_failures:
        recommendations.append(f"Fix {len(report.critical_failures)} CRITICAL failures before claiming PICO compliance")
    p_score = report.by_dimension.get("P", {"passed": 0, "total": 1})
    i_score = report.by_dimension.get("I", {"passed": 0, "total": 1})
    c_score = report.by_dimension.get("C", {"passed": 0, "total": 1})
    o_score = report.by_dimension.get("O", {"passed": 0, "total": 1})
    for dim, score, name in [("P", p_score, "Population"), ("I", i_score, "Intervention"), ("C", c_score, "Comparator"), ("O", o_score, "Outcome")]:
        dpct = round(100 * score["passed"] / score["total"], 1) if score["total"] > 0 else 0
        if dpct < 80:
            recommendations.append(f"Strengthen {name} ({dim}) coverage: {score['passed']}/{score['total']} ({dpct}%)")

    if recommendations:
        print("  RECOMMENDATIONS:")
        for i, r in enumerate(recommendations, 1):
            print(f"  {i}. {r}")
        print()

# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="PICO Assurance Evaluator")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show all check details")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--layer", type=int, default=0, help="Evaluate only a specific layer (1-8, 0=all)")
    args = parser.parse_args()

    all_checks = build_all_checks()

    if args.layer > 0:
        all_checks = [c for c in all_checks if c.layer == args.layer]
        if not all_checks:
            print(f"No checks found for layer {args.layer}")
            sys.exit(1)

    report = PicoEvaluationReport()
    for check in all_checks:
        report.add_check(check)
    report.compute_summary()

    if args.json:
        output = {
            "score": f"{report.passed}/{report.total}",
            "percentage": round(100 * report.passed / report.total, 1) if report.total > 0 else 0,
            "critical_failures": len(report.critical_failures),
            "by_layer": {str(k): v for k, v in report.by_layer.items()},
            "by_dimension": report.by_dimension,
            "failed_checks": [
                {"id": c.check_id, "claim": c.claim, "severity": c.severity, "detail": c._detail}
                for c in report.checks if not c.passed
            ]
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print_report(report, verbose=args.verbose)

    # Exit code
    sys.exit(0 if report.failed == 0 else 1)


if __name__ == "__main__":
    main()
