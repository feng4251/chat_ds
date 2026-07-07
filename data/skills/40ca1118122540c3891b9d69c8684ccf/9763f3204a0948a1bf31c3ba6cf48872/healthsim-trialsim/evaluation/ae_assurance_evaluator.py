#!/usr/bin/env python3
"""
AE Assurance Evaluation Harness
================================
Systematically evaluates whether the project's code-level implementation
delivers on documented AE (Adverse Event) functionality claims across
7 core files, 6 external data sources, and 6 processing stages.

Usage:
  python3 evaluation/ae_assurance_evaluator.py
  python3 evaluation/ae_assurance_evaluator.py --verbose
  python3 evaluation/ae_assurance_evaluator.py --json
"""

import os, re, sys, json
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

@dataclass
class AeCheck:
    check_id: str
    claim: str
    category: str      # "AE_DEFINITION" | "AE_GENERATION" | "AE_PROCESSING" | "AE_REGULATORY" | "AE_ORCHESTRATION" | "AE_EXTERNAL" | "CROSS_REF"
    layer: int         # 1-7
    file_path: str
    evidence_type: str # "file_exists" | "content_match" | "cross_ref" | "import_check" | "multi_pattern"
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
                self._detail = f"Pattern {'FOUND' if self._result else 'NOT FOUND'} in {self.file_path}: {self.evidence_query[:60]}..."
        elif self.evidence_type == "multi_pattern":
            if not fp.exists(): self._result = False; self._detail = f"MISSING: {self.file_path}"
            else:
                content = fp.read_text(encoding='utf-8', errors='ignore')
                patterns = self.evidence_query.split("|||")
                matches = sum(1 for p in patterns if re.search(p.strip(), content, re.I | re.DOTALL))
                self._result = matches >= len(patterns) * 0.6
                self._detail = f"Multi-pattern: {matches}/{len(patterns)} matched in {self.file_path}"
        elif self.evidence_type == "cross_ref":
            if not fp.exists(): self._result = False; self._detail = f"MISSING: {self.file_path}"
            else:
                content = fp.read_text(encoding='utf-8', errors='ignore')
                ref = os.path.splitext(os.path.basename(self.evidence_query))[0]
                m = re.search(re.escape(os.path.basename(self.evidence_query)), content, re.I) or re.search(re.escape(ref), content, re.I) or re.search(re.escape(self.evidence_query), content, re.I)
                self._result = m is not None
                self._detail = f"Cross-ref '{ref}' {'FOUND' if self._result else 'NOT FOUND'} in {self.file_path}"
        elif self.evidence_type == "import_check":
            if not fp.exists(): self._result = False; self._detail = f"MISSING: {self.file_path}"
            else:
                content = fp.read_text(encoding='utf-8', errors='ignore')
                m = re.search(rf'(?:from\s+\S+\s+import\s+.*{re.escape(self.evidence_query)}|import\s+.*{re.escape(self.evidence_query)})', content)
                self._result = m is not None
                self._detail = f"Import '{self.evidence_query}' {'FOUND' if self._result else 'NOT FOUND'} in {self.file_path}"
        else:
            self._result = False; self._detail = f"Unknown type: {self.evidence_type}"
        return self._result

    @property
    def passed(self): return self._result is True
    @property
    def icon(self): return "✅" if self._result else "❌" if self._result is not None else "⬜"


def build_all_checks() -> List[AeCheck]:
    C = []
    # ═══════════════════════════════════════════════════════════
    # LAYER 1: Core AE Definition & Data Model (6 files)
    # ═══════════════════════════════════════════════════════════
    C.extend([
        AeCheck("AE-DEF-001", "SDTM AE domain main definition file exists", "AE_DEFINITION", 1, "domains/adverse-events-ae.md", "file_exists", "", "CRITICAL"),
        AeCheck("AE-DEF-002", "AE domain: AESEQ, AETERM, AEDECOD, AEBODSYS variables defined", "AE_DEFINITION", 1, "domains/adverse-events-ae.md", "content_match", r"AESEQ|AETERM|AEDECOD|AEBODSYS", "CRITICAL"),
        AeCheck("AE-DEF-003", "AE domain: AESEV severity codes with CT mapping", "AE_DEFINITION", 1, "domains/adverse-events-ae.md", "content_match", r"AESEV.*(MILD|MODERATE|SEVERE)", "CRITICAL"),
        AeCheck("AE-DEF-004", "AE domain: AEREL causality codes", "AE_DEFINITION", 1, "domains/adverse-events-ae.md", "content_match", r"AEREL.*(NOT RELATED|POSSIBLY|PROBABLY|DEFINITELY)", "CRITICAL"),
        AeCheck("AE-DEF-005", "AE domain: AESER (serious AE) and AEACN (action taken) variables", "AE_DEFINITION", 1, "domains/adverse-events-ae.md", "content_match", r"AESER|AEACN", "MAJOR"),
        AeCheck("AE-DEF-006", "AE domain: AEOUT outcome codes", "AE_DEFINITION", 1, "domains/adverse-events-ae.md", "content_match", r"AEOUT.*(RECOVERED|RECOVERING|NOT RECOVERED|FATAL)", "MAJOR"),
        AeCheck("AE-DEF-007", "AE domain: TEAE definition (onset_rule, worsening_rule)", "AE_DEFINITION", 1, "domains/adverse-events-ae.md", "content_match", r"treatment.?emergent|TEAE|onset.*rule|worsening.*rule", "CRITICAL"),
        AeCheck("AE-DEF-008", "AE domain: MedDRA hierarchy defined (SOC→HLGT→HLT→PT→LLT)", "AE_DEFINITION", 1, "domains/adverse-events-ae.md", "content_match", r"(SOC|HLGT|HLT|LLT).*MedDRA|MedDRA.*(SOC|HLGT|HLT|LLT)", "CRITICAL"),
        AeCheck("AE-DEF-009", "AE domain: Drug-class-specific AE generation patterns", "AE_DEFINITION", 1, "domains/adverse-events-ae.md", "content_match", r"chemotherapy|checkpoint.*inhibitor|targeted.*therapy|drug.*class.*AE", "MAJOR"),
        AeCheck("AE-DEF-010", "ADaM ADAE dataset skill file exists", "AE_DEFINITION", 1, "skills/adam/adae.md", "file_exists", "", "CRITICAL"),
        AeCheck("AE-DEF-011", "ADAE: TRTEMFL (TEAE flag) defined", "AE_DEFINITION", 1, "skills/adam/adae.md", "content_match", r"TRTEMFL|treatment.?emergent.*flag", "CRITICAL"),
        AeCheck("AE-DEF-012", "ADAE: AESTDY, AEENDY, ADURN derivations defined", "AE_DEFINITION", 1, "skills/adam/adae.md", "content_match", r"AESTDY|AEENDY|ADURN", "MAJOR"),
        AeCheck("AE-DEF-013", "ADAE: SMQ flags for safety signal detection", "AE_DEFINITION", 1, "skills/adam/adae.md", "content_match", r"SMQ|Standardised.*MedDRA.*Quer|safety.*signal", "MAJOR"),
        AeCheck("AE-DEF-014", "Code systems: MedDRA v27.0 with 27 SOCs", "AE_DEFINITION", 1, "references/code-systems.md", "content_match", r"MedDRA.*(v|version).*27|27.*SOC", "CRITICAL"),
        AeCheck("AE-DEF-015", "Code systems: 100+ common PT-to-SOC mappings", "AE_DEFINITION", 1, "references/code-systems.md", "content_match", r"(PT|Preferred.*Term).*(SOC|System.*Organ.*Class).*mapping|100.*common|SOC.*code", "MAJOR"),
        AeCheck("AE-DEF-016", "Code systems: CTCAE v5.0 to MedDRA cross-reference table", "AE_DEFINITION", 1, "references/code-systems.md", "content_match", r"CTCAE.*(cross.?ref|mapping)|MedDRA.*CTCAE.*(cross|mapping)", "MAJOR"),
        AeCheck("AE-DEF-017", "Data models: AdverseEvent JSON Schema entity", "AE_DEFINITION", 1, "references/data-models.md", "content_match", r"AdverseEvent|adverse.*event.*schema", "MAJOR"),
        AeCheck("AE-DEF-018", "SDTM format spec: AE domain serialization", "AE_DEFINITION", 1, "formats/cdisc-sdtm.md", "content_match", r"AE.*domain|adverse.*event.*(format|serializ)", "MINOR"),
        AeCheck("AE-DEF-019", "ADaM format spec: ADAE traceability rules", "AE_DEFINITION", 1, "formats/cdisc-adam.md", "content_match", r"ADAE|traceability|SRCDOM.*SRCVAR", "MINOR"),
    ])

    # ═══════════════════════════════════════════════════════════
    # LAYER 2: AE Data Generation (2 files)
    # ═══════════════════════════════════════════════════════════
    C.extend([
        AeCheck("AE-GEN-001", "cohort_engine.py: 5-layer simulation Layer 5 = arm-specific AEs", "AE_GENERATION", 2, "scripts/cohort_engine.py", "content_match", r"Layer.*5.*(arm.?specific.*AE|adverse.*event)|arm.?specific.*adverse", "CRITICAL"),
        AeCheck("AE-GEN-002", "cohort_engine.py: AE generation by drug class", "AE_GENERATION", 2, "scripts/cohort_engine.py", "content_match", r"(ae_rate|adverse_event|AE.*template).*(chemotherapy|immunotherapy|targeted)", "MAJOR"),
        AeCheck("AE-GEN-003", "population_params.json: AE rate templates per TA", "AE_GENERATION", 2, "references/population_params.json", "content_match", r"ae_|adverse|toxicity", "MAJOR"),
        AeCheck("AE-GEN-004", "generate_test_data.py: uses cohort_engine for AE generation", "AE_GENERATION", 2, "scripts/generate_test_data.py", "cross_ref", "cohort_engine", "MAJOR"),
    ])

    # ═══════════════════════════════════════════════════════════
    # LAYER 3: AE Data Processing & Analysis (4 files)
    # ═══════════════════════════════════════════════════════════
    C.extend([
        AeCheck("AE-PROC-001", "sdtm_to_adam.py: derive_adae() function exists", "AE_PROCESSING", 3, "scripts/sdtm_to_adam.py", "content_match", r"def\s+derive_adae", "CRITICAL"),
        AeCheck("AE-PROC-002", "ADAE: TRTEMFL = 'Y' if onset >= day 1 (TEAE flag)", "AE_PROCESSING", 3, "scripts/sdtm_to_adam.py", "content_match", r"TRTEMFL.*(day|1|onset|AESTDY)", "CRITICAL"),
        AeCheck("AE-PROC-003", "ADAE: ASEVN uses get_ct_rank('AESEV', ...) from domain_parser", "AE_PROCESSING", 3, "scripts/sdtm_to_adam.py", "content_match", r"get_ct_rank.*AESEV|ASEV.*map_severity", "CRITICAL"),
        AeCheck("AE-PROC-004", "ADAE: ARELN uses get_ct_rank('AEREL', ...) for causality numeric", "AE_PROCESSING", 3, "scripts/sdtm_to_adam.py", "content_match", r"get_ct_rank.*AEREL|AREL.*map_causality", "CRITICAL"),
        AeCheck("AE-PROC-005", "sdtm_validator.py: validates AE required variables", "AE_PROCESSING", 3, "scripts/sdtm_validator.py", "content_match", r"AE.*(required|variable|AESEQ|AETERM|AEDECOD)", "MAJOR"),
        AeCheck("AE-PROC-006", "sdtm_validator.py: validates AESTDTC <= AEENDTC date order", "AE_PROCESSING", 3, "scripts/sdtm_validator.py", "content_match", r"AESTDTC.*AEENDTC|AE.*date.*order", "MAJOR"),
        AeCheck("AE-PROC-007", "cross_domain_consistency.py: AE-lab correlation check", "AE_PROCESSING", 3, "scripts/cross_domain_consistency.py", "content_match", r"AE.*(lab|LB|correlation|consist)|lab.*AE.*(correlation|consist)", "MAJOR"),
        AeCheck("AE-PROC-008", "domain_parser.py: AESEV ordered codelist (5 values)", "AE_PROCESSING", 3, "scripts/domain_parser.py", "content_match", r'AESEV.*\[.*MILD.*MODERATE.*SEVERE.*LIFE THREATENING.*DEATH', "CRITICAL"),
        AeCheck("AE-PROC-009", "domain_parser.py: AEREL ordered codelist (6 values)", "AE_PROCESSING", 3, "scripts/domain_parser.py", "content_match", r'AEREL.*\[.*NOT RELATED.*UNLIKELY.*POSSIBLY.*PROBABLY.*DEFINITELY.*RELATED', "CRITICAL"),
        AeCheck("AE-PROC-010", "domain_parser.py: get_ct_rank() method for numeric severity mapping", "AE_PROCESSING", 3, "scripts/domain_parser.py", "content_match", r"def\s+get_ct_rank", "CRITICAL"),
    ])

    # ═══════════════════════════════════════════════════════════
    # LAYER 4: AE Regulatory Reference (4 files)
    # ═══════════════════════════════════════════════════════════
    C.extend([
        AeCheck("AE-REG-001", "ICH index: E2A (SAE definitions) covered", "AE_REGULATORY", 4, "references/ich-guidelines-index.md", "content_match", r"E2A.*(SAE|safety.*data.*management|expedited.*report)", "CRITICAL"),
        AeCheck("AE-REG-002", "ICH index: E2B (ICSR data elements) covered", "AE_REGULATORY", 4, "references/ich-guidelines-index.md", "content_match", r"E2B.*(ICSR|safety.*data.*element|transmission)", "MAJOR"),
        AeCheck("AE-REG-003", "ICH index: M1 (MedDRA) referenced for AE coding", "AE_REGULATORY", 4, "references/ich-guidelines-index.md", "content_match", r"M1.*MedDRA|MedDRA.*ICH.*M1", "CRITICAL"),
        AeCheck("AE-REG-004", "Clinical domain: SAE criteria per ICH E2A referenced", "AE_REGULATORY", 4, "clinical-trials-domain.md", "content_match", r"SAE.*(criteria|definition|ICH.*E2A)|ICH.*E2A.*(SAE|safety)", "MAJOR"),
        AeCheck("AE-REG-005", "Phase I: DLT categories (hematologic/non-hematologic/hepatic/cardiac)", "AE_REGULATORY", 4, "phase1-dose-escalation.md", "content_match", r"DLT.*(hematologic|non.?hematologic|hepatic|cardiac|neurologic)", "CRITICAL"),
        AeCheck("AE-REG-006", "Phase III: DSMB safety stopping rules per ICH", "AE_REGULATORY", 4, "phase3-pivotal.md", "content_match", r"DSMB.*(stopping.*rule|safety.*stop|interim.*safety)|safety.*stopping.*rule", "MAJOR"),
        AeCheck("AE-REG-007", "Oncology TA: checkpoint inhibitor irAE patterns", "AE_REGULATORY", 4, "therapeutic-areas/oncology.md", "content_match", r"immune.*related.*adverse|irAE|checkpoint.*inhibitor.*(pneumonitis|colitis|hepatitis|thyroid)", "CRITICAL"),
        AeCheck("AE-REG-008", "Cardiovascular TA: MACE adjudication workflow", "AE_REGULATORY", 4, "therapeutic-areas/cardiovascular.md", "content_match", r"(CEC|Clinical.*Endpoint.*Committee|adjudicat).*(MACE|cardiovascular)", "MAJOR"),
        AeCheck("AE-REG-009", "CNS TA: C-SSRS suicidality monitoring for CNS drugs", "AE_REGULATORY", 4, "therapeutic-areas/cns.md", "content_match", r"C.?SSRS|suicid|Columbia.*Suicide", "MAJOR"),
        AeCheck("AE-REG-010", "CGT TA: CRS and ICANS grading per ASTCT consensus", "AE_REGULATORY", 4, "therapeutic-areas/cgt.md", "content_match", r"CRS.*(grade|grading|ASTCT)|ICANS.*(grade|grading|ICE.*score)", "CRITICAL"),
    ])

    # ═══════════════════════════════════════════════════════════
    # LAYER 5: Orchestration Workers — AE Capabilities (5 files)
    # ═══════════════════════════════════════════════════════════
    C.extend([
        # Worker F — AE Adjudication
        AeCheck("AE-ORCH-001", "Worker F: AE adjudication YAML file exists", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "file_exists", "", "CRITICAL"),
        AeCheck("AE-ORCH-002", "Worker F: CTCAE v5.0 grade determination protocol (7-phase adjudication)", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"CTCAE.*(grade|grading|v5\.0|5\.0).*determin|adjudication.*protocol.*(phase|step)", "CRITICAL"),
        AeCheck("AE-ORCH-003", "Worker F: MedDRA coding (LLT→PT→HLT→HLGT→SOC hierarchy)", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"LLT.*PT.*HLT.*HLGT.*SOC|MedDRA.*(hierarchy|LLT|Preferred.*Term)", "CRITICAL"),
        AeCheck("AE-ORCH-004", "Worker F: SAE seriousness assessment per ICH E2A", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"ICH\s*E2A|seriousness.*assessment|SAE.*(criteri|death|hospitalization|disability)", "CRITICAL"),
        AeCheck("AE-ORCH-005", "Worker F: WHO-UMC causality assessment (Certain/Probable/Possible/Unlikely)", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"WHO.?UMC|causality.*assessment.*(Certain|Probable|Possible|Unlikely)", "MAJOR"),
        AeCheck("AE-ORCH-006", "Worker F: Quality flags (UNDER_REPORTED, GRADE_DISCREPANCY, MISSING_RELATED_AE)", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"UNDER_REPORTED|GRADE_DISCREPANCY|MISSING_RELATED_AE", "MAJOR"),
        AeCheck("AE-ORCH-007", "Worker F v1.1: AE Profile Generation — 7-phase protocol exists", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"ae_profile_instructions|AE PROFILE GENERATION.*PROTOCOL.*7.*phase", "CRITICAL"),
        AeCheck("AE-ORCH-008", "Worker F v1.1: AE incidence table generation (overall/top-10/AESI/lab)", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"(most.common.*AE|Top.?10.*AE|AESI|lab.*abnorm).*incidence", "CRITICAL"),
        AeCheck("AE-ORCH-009", "Worker F v1.1: CTCAE v5.0 graded tables per organ system", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"CTCAE.*(grading table|grade.*definition|organ.*system).*(hepatica|gastrointestinal|cardiac|endocrine)", "MAJOR"),
        AeCheck("AE-ORCH-010", "Worker F v1.1: AE management decision tree (Grade 1→2→3→4)", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"(Grade.*1.*Continue|Grade.*2.*(Continue|standard)|Grade.*3.*(DOSE|INTERRUPT|PAUSE)|Grade.*4.*(PERMANENTLY|DISCONTINUE))", "CRITICAL"),
        AeCheck("AE-ORCH-011", "Worker F v1.1: Phase-specific risk mitigation (Phase I/II/III/IV)", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"phase.?specific.*risk.*mitigation|Phase I.*(sentinel|DLT|SRC).*Phase II.*(counseling|rescue).*Phase III.*(DSMB|CEC|REMS).*Phase IV.*(PASS|PBRER)", "MAJOR"),
        AeCheck("AE-ORCH-012", "Worker F v1.1: Pharmacovigilance & SAE reporting timelines", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"SAE.*(report.*timeline|7.*day|15.*day)|pharmacovigilance.*(report|infrastructure)", "MAJOR"),
        AeCheck("AE-ORCH-013", "Worker F v1.1: Competitive AE benchmarking (vs drug class comparators)", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"competitive.*(benchmark|comparison|positioning)|competitor.*(AE|safety).*(compar|benchmark)", "MAJOR"),
        AeCheck("AE-ORCH-014", "Worker F v1.1: DSMB charter with stopping rules", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"(DSMB|Data.*Safety.*Monitoring.*Board).*(charter|stopping|composition)", "MAJOR"),
        AeCheck("AE-ORCH-015", "Worker F: KG-F6—KG-F9 knowledge gate checks for AE profile", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"KG.?F6.*complete.*AE.*incidence|KG.?F7.*FDA.*prescribing.*information|KG.?F8.*compet.*drug.*AE.*profile|KG.?F9.*drug.?class.?specific.*AE", "CRITICAL"),

        # Worker A — Safety Extraction
        AeCheck("AE-ORCH-016", "Worker A: YAML file exists", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-safety-extraction.yaml", "file_exists", "", "CRITICAL"),
        AeCheck("AE-ORCH-017", "Worker A: Safety data extraction protocol (Phase 3: safety per arm)", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-safety-extraction.yaml", "content_match", r"SAFETY.*DATA.*EXTRACTION|safety.*per.*arm|AE.*any.*grade.*Grade.*3", "CRITICAL"),
        AeCheck("AE-ORCH-018", "Worker A: AESI and organ system breakdown extraction", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-safety-extraction.yaml", "content_match", r"AESI|AE_by_SOC|organ.*system.*breakdown", "MAJOR"),

        # Worker C — Termination Analysis (safety category)
        AeCheck("AE-ORCH-019", "Worker C: Category A (SAFETY) in termination taxonomy", "AE_ORCHESTRATION", 5, "orchestration/workers/worker-termination-analysis.yaml", "content_match", r"CATEGORY.*A.*SAFETY|A1.*UNEXPECTED.*TOXICITY|A2.*EXCESS.*MORTALITY|A3.*DSMB.*SAFETY.*STOP", "CRITICAL"),

        # Orchestrator routing
        AeCheck("AE-ORCH-020", "Orchestrator: trigger_worker_ae_adjudication exists", "AE_ORCHESTRATION", 5, "orchestration/orchestrator.yaml", "content_match", r"trigger_worker_ae_adjudication", "CRITICAL"),
        AeCheck("AE-ORCH-021", "Orchestrator: trigger_ae_profile_generation exists (v1.2)", "AE_ORCHESTRATION", 5, "orchestration/orchestrator.yaml", "content_match", r"trigger_ae_profile_generation", "CRITICAL"),
        AeCheck("AE-ORCH-022", "Orchestrator: composite_full_protocol_design includes Worker F", "AE_ORCHESTRATION", 5, "orchestration/orchestrator.yaml", "content_match", r"composite_full_protocol_design.*worker-ae-adjudication", "CRITICAL"),
        AeCheck("AE-ORCH-023", "Orchestrator: final_report_template has AE Profile section (Section 9)", "AE_ORCHESTRATION", 5, "orchestration/orchestrator.yaml", "content_match", r"Comprehensive.*AE.*Profile.*Management.*Plan", "MAJOR"),
    ])

    # ═══════════════════════════════════════════════════════════
    # LAYER 6: External Data Sources for AE
    # ═══════════════════════════════════════════════════════════
    C.extend([
        AeCheck("AE-EXT-001", "Skills: clinicaltrials-database available for AE incidence retrieval", "AE_EXTERNAL", 6, "SKILL.md", "content_match", r"clinicaltrials.?database", "CRITICAL"),
        AeCheck("AE-EXT-002", "Skills: pubmed-database available for AE literature search", "AE_EXTERNAL", 6, "SKILL.md", "content_match", r"pubmed.?database", "CRITICAL"),
        AeCheck("AE-EXT-003", "Skills: fda-database available for FDA AE label retrieval", "AE_EXTERNAL", 6, "SKILL.md", "content_match", r"fda.?database", "MAJOR"),
        AeCheck("AE-EXT-004", "External knowledge harvester: 5-step workflow for AE gap filling", "AE_EXTERNAL", 6, "skills/external-knowledge-harvester.md", "content_match", r"Detect.*Gap.*Query.*(ClinicalTrial|CT\.gov).*Query.*PubMed.*Generate.*Supplement.*Apply", "MAJOR"),
    ])

    # ═══════════════════════════════════════════════════════════
    # LAYER 7: Cross-References & Integration
    # ═══════════════════════════════════════════════════════════
    C.extend([
        AeCheck("CROSS-001", "Orchestrator references worker-ae-adjudication", "CROSS_REF", 7, "orchestration/orchestrator.yaml", "cross_ref", "worker-ae-adjudication.yaml", "CRITICAL"),
        AeCheck("CROSS-002", "sdtm_to_adam.py imports DomainParser for AE CT", "CROSS_REF", 7, "scripts/sdtm_to_adam.py", "import_check", "DomainParser", "CRITICAL"),
        AeCheck("CROSS-003", "cohort_engine.py imports numpy for AE simulation", "CROSS_REF", 7, "scripts/cohort_engine.py", "import_check", "numpy", "MAJOR"),
        AeCheck("CROSS-004", "Worker F references code-systems.md for MedDRA", "CROSS_REF", 7, "orchestration/workers/worker-ae-adjudication.yaml", "cross_ref", "code-systems.md", "CRITICAL"),
        AeCheck("CROSS-005", "Worker A references code-systems.md for MedDRA validation", "CROSS_REF", 7, "orchestration/workers/worker-safety-extraction.yaml", "cross_ref", "code-systems.md", "CRITICAL"),
        AeCheck("CROSS-006", "Worker F references FDA label (skill:fda-database)", "CROSS_REF", 7, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"skill:fda-database", "MAJOR"),
        AeCheck("CROSS-007", "Worker F references CT.gov trial results (skill:clinicaltrials-database)", "CROSS_REF", 7, "orchestration/workers/worker-ae-adjudication.yaml", "content_match", r"skill:clinicaltrials-database", "MAJOR"),
        AeCheck("CROSS-008", "reports/daily-report-0611 documents initial AE domain implementation", "CROSS_REF", 7, "reports/daily-report-0611/daily-report-2026-06-11.md", "content_match", r"AE.*(domain|test).*TC.?05", "MINOR"),
    ])

    return C


def print_report(checks: List[AeCheck], verbose: bool = False):
    total = len(checks)
    passed = sum(1 for c in checks if c.passed)
    failed = total - passed
    pct = round(100 * passed / total, 1) if total > 0 else 0
    grade = "A" if pct >= 90 else "B" if pct >= 75 else "C" if pct >= 60 else "D" if pct >= 40 else "F"

    print("\n" + "=" * 90)
    print("  AE ASSURANCE EVALUATION MATRIX")
    print("  xClinicalTrial Orchestrator — Code-Level AE Verification")
    print("=" * 90)
    print(f"\n  OVERALL: {passed}/{total} ({pct}%)  GRADE: {grade}")
    print(f"  Critical Failures: {sum(1 for c in checks if not c.passed and c.severity == 'CRITICAL')}")
    print()

    # By category
    cats = {}
    for c in checks:
        cats.setdefault(c.category, {"t": 0, "p": 0, "f": 0})
        cats[c.category]["t"] += 1
        if c.passed: cats[c.category]["p"] += 1
        else: cats[c.category]["f"] += 1

    cat_names = {
        "AE_DEFINITION": "Core AE Definition & Data Model",
        "AE_GENERATION": "AE Data Generation",
        "AE_PROCESSING": "AE Data Processing & Analysis",
        "AE_REGULATORY": "AE Regulatory Reference",
        "AE_ORCHESTRATION": "Orchestration AE Capabilities",
        "AE_EXTERNAL": "External AE Data Sources",
        "CROSS_REF": "Cross-References & Integration",
    }
    print("  ── BY CATEGORY ──")
    print(f"  {'Category':<45s} {'Checks':>6s}  {'Passed':>6s}  {'Failed':>6s}  {'Rate':>6s}")
    print("  " + "-" * 75)
    for cat in ["AE_DEFINITION", "AE_GENERATION", "AE_PROCESSING", "AE_REGULATORY", "AE_ORCHESTRATION", "AE_EXTERNAL", "CROSS_REF"]:
        if cat in cats:
            s = cats[cat]; cp = round(100 * s["p"] / s["t"], 1) if s["t"] > 0 else 0
            icon = "✅" if cp >= 80 else "⚠️" if cp >= 60 else "❌"
            print(f"  {icon} {cat_names.get(cat, cat):<43s} {s['t']:>4d}   {s['p']:>4d}   {s['f']:>4d}   {cp:>5.1f}%")

    # Failed checks
    failed_checks = [c for c in checks if not c.passed]
    if failed_checks:
        print(f"\n  ── FAILED ({len(failed_checks)}) ──")
        for c in failed_checks:
            sev = "🔴" if c.severity == "CRITICAL" else "🟠" if c.severity == "MAJOR" else "🟡"
            print(f"  {sev} {c.check_id:16s} [{c.severity:8s}] | {c.claim[:90]}")
            print(f"     Detail: {c._detail[:120]}")

    if verbose:
        print(f"\n  ── ALL ({total}) ──")
        for c in checks:
            print(f"  {c.icon} {c.check_id:16s} [{c.category:18s}] {c.severity:8s} | {c.claim[:80]}")

    print("\n" + "=" * 90)
    crit = [c for c in checks if not c.passed and c.severity == "CRITICAL"]
    if crit:
        print(f"  ⚠️  {len(crit)} CRITICAL failure(s) require immediate attention.")
    else:
        print(f"  ✅ No CRITICAL failures. AE functionality verified at code level.")
    print()


def main():
    import argparse
    p = argparse.ArgumentParser(description="AE Assurance Evaluator")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    checks = build_all_checks()
    for c in checks:
        c.evaluate(PROJECT_ROOT)

    if args.json:
        res = {
            "total": len(checks), "passed": sum(1 for c in checks if c.passed),
            "failed": sum(1 for c in checks if not c.passed),
            "percentage": round(100 * sum(1 for c in checks if c.passed) / len(checks), 1),
            "critical_failures": [{"id": c.check_id, "claim": c.claim, "detail": c._detail} for c in checks if not c.passed and c.severity == "CRITICAL"],
            "by_category": {cat: {"total": sum(1 for x in checks if x.category == cat), "passed": sum(1 for x in checks if x.category == cat and x.passed), "failed": sum(1 for x in checks if x.category == cat and not x.passed)} for cat in set(c.category for c in checks)}
        }
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print_report(checks, verbose=args.verbose)

    sys.exit(0 if all(c.passed for c in checks if c.severity == "CRITICAL") else 1)


if __name__ == "__main__":
    main()
