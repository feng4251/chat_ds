# ADaM Analysis Dataset Skills

Skills for generating CDISC ADaM-compliant analysis datasets for clinical trial submissions. Each skill produces analysis-ready data with full traceability to SDTM source domains.

---

## Quick Reference

| Dataset | Skill | Class | Description | Triggers |
|---------|-------|-------|-------------|----------|
| **ADSL** | [adsl.md](adsl.md) | Subject-Level | Subject-level analysis data with population flags, baseline characteristics, and treatment assignments | "ADSL", "subject-level", "population flags", "SAFFL", "ITTFL" |
| **ADAE** | [adae.md](adae.md) | BDS | Adverse event analysis with treatment-emergent flags, duration, and first occurrence | "ADAE", "adverse event analysis", "TEAE", "TRTEMFL", "safety analysis" |
| **ADLB** | [adlb.md](adlb.md) | BDS | Laboratory analysis with change from baseline, shift tables, and toxicity grading | "ADLB", "lab analysis", "change from baseline", "shift table", "toxicity grade" |
| **ADEFF** | [adeff.md](adeff.md) | BDS | Efficacy analysis with responder flags, change from baseline, and key endpoint derivations | "ADEFF", "efficacy analysis", "responder", "endpoint", "HBA1C" |
| **ADTTE** | [adtte.md](adtte.md) | TTE | Time-to-event analysis with censoring rules for survival endpoints | "ADTTE", "time-to-event", "survival", "PFS", "OS", "censoring" |

---

## Implementation Status

| Dataset | Status | Notes |
|---------|--------|-------|
| ADSL | Complete | Subject-level analysis dataset - required for all studies |
| ADAE | Complete | Adverse event analysis with TEAE flags and MedDRA coding |
| ADLB | Complete | Laboratory analysis with CTCAE toxicity grading |
| ADEFF | Complete | Efficacy analysis with responder criteria and endpoint derivations |
| ADTTE | Complete | Time-to-event analysis with Kaplan-Meier ready censoring rules |

---

## SDTM to ADaM Source Mapping

Each ADaM dataset derives from one or more SDTM domains:

| ADaM Dataset | Source SDTM Domains | Relationship |
|--------------|---------------------|--------------|
| **ADSL** | DM, DS, EX, MH, SV | One record per subject; merges demographic, disposition, and exposure data |
| **ADAE** | AE, DM (for treatment dates), SMQ (for safety topics) | One record per AE; adds analysis flags and derivations |
| **ADLB** | LB, DM (for baseline reference) | One record per lab test per visit; adds change, shift, grade |
| **ADEFF** | LB, VS, QS, DM (for baseline) | One record per efficacy assessment per visit |
| **ADTTE** | AE, DS, RS, LB, TU, TR, DM | One record per event type per subject; combines event and censoring data |

---

## ADaM General Principles

### Dataset Classes

ADaM defines three main dataset classes, each with its own structural rules:

| Class | Structure | Key Rule | Datasets |
|-------|-----------|----------|----------|
| **ADSL** | One record per subject | Non-repeating; subject-level only | ADSL |
| **BDS** | One record per subject per parameter per visit | Must contain PARAM, PARAMCD, AVAL/AVALC, ADT, ADY | ADAE, ADLB, ADEFF |
| **ADTTE** | One record per subject per event type | Must contain STARTDT, ADT, AVAL, CNSR | ADTTE |

### Traceability Requirement

Every ADaM record must be traceable to its SDTM source via SRCDOM, SRCVAR, and SRCSEQ:

```
ADAE record → SRCDOM="AE" → SRCVAR="AESEQ" → SRCSEQ=5
             → traces back to AE.AESEQ=5
```

### Population Flags

All ADaM datasets include core population flags:

| Flag | Variable | Definition |
|------|----------|------------|
| Safety | SAFFL | Received at least one dose of study drug |
| ITT | ITTFL | Randomized to treatment |
| Per-Protocol | PPROTFL | No major protocol deviations |
| Completer | COMPLFL | Completed the study per protocol |

---

## Common ADaM Variables

### Identifier Variables

| Variable | Label | Description |
|----------|-------|-------------|
| STUDYID | Study Identifier | Unique study identifier |
| USUBJID | Unique Subject ID | Study + Site + Subject (globally unique) |

### Treatment Variables

| Variable | Label | Description |
|----------|-------|-------------|
| TRT01P | Planned Treatment Period 01 | Planned arm description |
| TRT01PN | Planned Treatment Period 01 (N) | Numeric arm code |
| TRT01A | Actual Treatment Period 01 | Actual arm description |
| TRT01AN | Actual Treatment Period 01 (N) | Numeric actual arm code |

### BDS Structure Variables (ADAE, ADLB, ADEFF)

| Variable | Label | Description |
|----------|-------|-------------|
| PARAM | Parameter | Full parameter description |
| PARAMCD | Parameter Code | Short parameter code (max 8 chars) |
| AVAL | Analysis Value | Numeric analysis value |
| AVALC | Analysis Value (C) | Character analysis value |
| ADT | Analysis Date | Date of observation (SAS date) |
| ADY | Analysis Relative Day | Study day relative to reference |

### Derivation Variables (BDS)

| Variable | Label | Description |
|----------|-------|-------------|
| BASE | Baseline Value | Last non-missing value before first dose |
| BASEC | Baseline Value (C) | Character baseline value |
| CHG | Change from Baseline | AVAL - BASE |
| PCHG | Percent Change | (AVAL - BASE) / BASE * 100 |
| SHIFT1 | Shift from Baseline | Baseline grade to visit grade shift |
| DTYPE | Derivation Type | Method of record creation |

### TTE Variables (ADTTE)

| Variable | Label | Description |
|----------|-------|-------------|
| PARAMCD | Parameter Code | OS, PFS, TTD, TTDM, TTR |
| STARTDT | Start Date | Origin date for time-to-event |
| ADT | Analysis Date | Event or censoring date |
| AVAL | Analysis Value | Days from STARTDT to ADT |
| CNSR | Censoring Flag | 0=event, 1=censored |
| EVNTDESC | Event Description | Description of the event |
| CNSDTDSC | Censoring Description | Reason for censoring |

---

## Related Resources

### TrialSim Formats
- [../../formats/cdisc-adam.md](../../formats/cdisc-adam.md) - ADaM format specification

### TrialSim SDTM Domains
- [../../domains/README.md](../../domains/README.md) - SDTM domain skills (data sources)

### TrialSim Core
- [../../clinical-trials-domain.md](../../clinical-trials-domain.md) - Domain knowledge
- [../../phase3-pivotal.md](../../phase3-pivotal.md) - Phase 3 trial patterns

### External References
- [CDISC ADaM IG 1.2](https://www.cdisc.org/standards/foundational/adam) - Official implementation guide
- [CDISC ADaM BDS](https://www.cdisc.org/standards/foundational/adam/bds) - Basic Data Structure

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12 | Initial ADaM skills directory with ADSL, ADAE, ADLB, ADEFF, ADTTE |
