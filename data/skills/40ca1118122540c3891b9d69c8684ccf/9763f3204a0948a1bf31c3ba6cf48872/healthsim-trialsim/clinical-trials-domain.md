---
name: clinical-trials-domain
author: "Yinan Chen (陈翼男)"
description: "Core domain knowledge for clinical trial data generation including trial phases, CDISC standards, regulatory requirements, safety/efficacy patterns, and cross-product integration. Referenced by all TrialSim cohort skills."
---

# Clinical Trials Domain Knowledge

This skill provides foundational domain knowledge for clinical trial synthetic data generation.

## Trial Phases

| Phase | Purpose | Typical Size | Duration |
|-------|---------|--------------|----------|
| Phase 1 | Safety, dosing | 20-100 | 6-12 months |
| Phase 2 | Efficacy signal, dose optimization | 100-500 | 1-2 years |
| Phase 3 | Confirmatory efficacy, safety | 300-3000+ | 2-4 years |
| Phase 4 | Post-marketing surveillance | 1000+ | Ongoing |

## CDISC Standards Overview

### SDTM (Study Data Tabulation Model)
Standard format for submitting clinical trial data to FDA/regulatory agencies.

Key domains:
- **DM** - Demographics
- **AE** - Adverse Events
- **CM** - Concomitant Medications
- **EX** - Exposure
- **LB** - Laboratory Results
- **VS** - Vital Signs
- **DS** - Disposition
- **MH** - Medical History
- **EG** - ECG Results
- **PE** - Physical Examination

### ADaM (Analysis Data Model)
Analysis-ready datasets derived from SDTM.

Key datasets:
- **ADSL** - Subject-Level Analysis Dataset
- **ADAE** - Adverse Event Analysis Dataset
- **ADLB** - Laboratory Analysis Dataset
- **ADEFF** - Efficacy Analysis Dataset
- **ADTTE** - Time-to-Event Analysis Dataset

## Regulatory Considerations

### ICH-GCP Compliance
- Informed consent documentation
- Protocol deviation tracking
- Source data verification
- Audit trail requirements

### Safety Reporting
- SAE (Serious Adverse Event) timelines
- SUSAR (Suspected Unexpected Serious Adverse Reaction)
- DSMB (Data Safety Monitoring Board) reviews

## Cross-Product Integration

### PatientSim → TrialSim
When converting a PatientSim patient to a TrialSim subject:
- Add informed consent record
- Add screening assessments
- Add randomization assignment
- Map diagnoses to inclusion/exclusion criteria
- Convert encounters to study visits

### NetworkSim → TrialSim
When using NetworkSim provider as investigator:
- Add medical license verification
- Add GCP training certification
- Add site delegation log entries
- Add financial disclosure

## See Also

- [Phase 3 Pivotal Trials](phase3-pivotal.md)
- [Recruitment & Enrollment](recruitment-enrollment.md)
- [SDTM Format](../../formats/cdisc-sdtm.md)
- [ADaM Format](../../formats/cdisc-adam.md)

---

## Validation Guidelines

When generating clinical trial data, validate against these domain rules:

### Phase-Appropriate Design Rules

| Phase | Typical N | Design Constraints |
|-------|-----------|-------------------|
| Phase I | 10-80 | Dose escalation, healthy volunteers or patients |
| Phase II | 50-300 | Randomization optional, proof-of-concept focus |
| Phase III | 300-3000+ | Must be randomized, adequate power for endpoints |
| Phase IV | Variable | Post-marketing, real-world setting |

### Regulatory Compliance

| Rule | Requirement |
|------|-------------|
| ICF timing | Informed consent must precede all study procedures |
| Randomization | Must occur after eligibility confirmed |
| SUSAR reporting | Within 7 days for fatal/life-threatening, 15 days otherwise |
| Protocol deviations | Must be documented with reason and impact |

### CDISC Standards

| Standard | Validation |
|----------|------------|
| USUBJID | Must be unique across all studies (STUDYID-SITEID-SUBJID format) |
| Date formats | ISO 8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS) |
| Controlled terms | Must use CDISC CT where applicable |
| MedDRA coding | AE terms must map to valid MedDRA PT |

## Knowledge Gaps & External Resources

当项目内技能文件无法覆盖特定治疗领域或药物时，通过外部数据库补充知识。

### Knowledge Gap Detection

| Gap Type | Detection Rule | Action |
|----------|---------------|--------|
| Missing therapeutic area | Indication not in oncology/cardiovascular/CNS/CGT | Invoke `clinicaltrials-database` + `pubmed-database` |
| Missing disease criteria | No diagnostic/staging criteria in project files | Search PubMed for validation studies |
| Missing drug pharmacology | No mechanism-of-action doc in project | Search PubMed for pharmacology reviews |
| Missing I/E thresholds | No specific numeric thresholds for exclusion | Search ClinicalTrials.gov for real trial criteria |
| Missing effect sizes | Unknown mean/SD for sample size calculation | Search PubMed for published Phase 2 results |

### External Database Integration

| Database | Skill | Data Retrieved |
|----------|-------|---------------|
| **ClinicalTrials.gov** | `clinicaltrials-database` | Real trial design parameters (N, arms, endpoints), I/E thresholds, enrollment, locations |
| **PubMed** | `pubmed-database` | Published effect sizes, diagnostic criteria validation, pharmacology mechanisms, natural history epidemiology |

### Integration Workflow

See [skills/external-knowledge-harvester.md](skills/external-knowledge-harvester.md) for the complete 5-step workflow (Detect → Query CT.gov → Query PubMed → Generate Supplement → Apply).
