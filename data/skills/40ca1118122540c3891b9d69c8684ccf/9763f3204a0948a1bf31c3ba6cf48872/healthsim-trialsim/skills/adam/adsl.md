---
name: adam-adsl
description: |
  Generate ADaM ADSL (Subject-Level Analysis Dataset) with population flags, 
  baseline characteristics, treatment assignments, and disposition data. One 
  record per subject, derived from DM, DS, EX, and MH SDTM domains. Required 
  for all clinical trial analysis. Triggers: "ADSL", "subject-level analysis", 
  "population flags", "SAFFL", "ITTFL", "baseline characteristics", 
  "subject demographics analysis", "ADSL dataset".
---

# Subject-Level Analysis Dataset (ADSL)

ADSL is the foundational ADaM dataset containing one record per subject. It combines subject-level information from multiple SDTM domains (DM, DS, EX) into analysis-ready population flags, baseline characteristics, treatment assignments, and disposition outcomes.

---

## For Claude

This is a **core ADaM dataset skill** for generating subject-level analysis data. ADSL is **required for every clinical trial analysis** and provides the population flags that all other ADaM datasets inherit.

**Always apply this skill when you see:**
- Requests for subject-level analysis data
- ADSL dataset generation
- Population flag derivation (SAFFL, ITTFL, PPROTFL)
- Baseline characteristics for subjects
- Treatment arm assignment for analysis
- Derived variables like age groups, baseline labs, disposition status

**Key responsibilities:**
- Derive population flags from SDTM DM, DS, and EX domains
- Calculate age groups (AGEGR1/AGEGR1N) from birth date and reference start date
- Map treatment assignments (TRT01P/TRT01PN) from DM.ARM/ARMCD
- Derive baseline characteristics (e.g., BASE_HBA1C, BASE_BMI) from SDTM findings
- Populate completer/discontinuation flags from DS domain

---

## ADaM Variables

### Required Variables (One Record Per Subject)

| Variable | Label | Type | Length | Description |
|----------|-------|------|--------|-------------|
| STUDYID | Study Identifier | Char | 20 | Unique study ID |
| USUBJID | Unique Subject Identifier | Char | 40 | STUDYID-SITEID-SUBJID |
| SUBJID | Subject ID for Study | Char | 20 | Subject ID within study |
| SITEID | Study Site Identifier | Char | 10 | Site number |
| AGE | Age | Num | 8 | Age at reference start date |
| AGEU | Age Units | Char | 6 | YEARS |
| SEX | Sex | Char | 1 | M, F, U |
| RACE | Race | Char | 60 | CDISC controlled terminology |
| ETHNIC | Ethnicity | Char | 40 | HISPANIC OR LATINO, NOT HISPANIC OR LATINO |
| TRT01P | Planned Treatment for Period 01 | Char | 200 | From DM.ARM |
| TRT01PN | Planned Treatment for Period 01 (N) | Num | 8 | 1, 2, 3, etc. |
| TRT01A | Actual Treatment for Period 01 | Char | 200 | May differ from planned |
| TRT01AN | Actual Treatment for Period 01 (N) | Num | 8 | Numeric actual treatment code |
| SAFFL | Safety Population Flag | Char | 1 | Y if received >= 1 dose |
| ITTFL | Intent-To-Treat Population Flag | Char | 1 | Y if randomized |
| PPROTFL | Per-Protocol Population Flag | Char | 1 | Y if no major protocol deviations |
| COMPLFL | Completers Population Flag | Char | 1 | Y if completed study per DS |

### Expected Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| BRTHDTC | Date/Time of Birth | Char | From DM.BRTHDTC |
| RFSTDTC | Subject Reference Start Date/Time | Char | From DM.RFSTDTC |
| RFENDTC | Subject Reference End Date/Time | Char | From DM.RFENDTC |
| AGEGR1 | Pooled Age Group 1 | Char | <65, >=65 |
| AGEGR1N | Pooled Age Group 1 (N) | Num | 1, 2 |
| RANDDT | Date of Randomization | Num | From DS with DSDECOD=RANDOMIZED |
| TRTSDT | Date of First Exposure | Num | From EX |
| TRTEDT | Date of Last Exposure | Num | From EX |
| DCSREAS | Discontinuation Reason | Char | From DS |
| DTHFL | Subject Death Flag | Char | Y or null |

### Baseline Characteristic Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| BASE_HBA1C | Baseline HbA1c | Num | Last non-missing LB before first dose |
| BASE_BMI | Baseline BMI | Num | Weight(kg)/Height(m)^2 at baseline |
| BASE_EGFR | Baseline eGFR | Num | Last non-missing LB before first dose |
| BASE_ALT | Baseline ALT | Num | Last non-missing ALT before first dose |
| BASE_WEIGHT | Baseline Weight | Num | Last VS weight before first dose |

---

## Key Derivations

### AGE, AGEGR1, AGEGR1N from DM.BRTHDTC

```
AGE = floor((RFSTDTC - BRTHDTC) / 365.25)
AGEGR1 = ">=65" when AGE >= 65, otherwise "<65"
AGEGR1N = 1 when AGE < 65, 2 when AGE >= 65
```

### TRT01P/TRT01PN from DM.ARMCD/ARM

```
TRT01P = DM.ARM (i.e., copied directly)
TRT01PN = 1 for placebo, 2 for low dose, 3 for high dose, etc.
TRT01A = DM.ACTARM if available, otherwise DM.ARM
TRT01AN = corresponding numeric code
```

### Population Flag Derivations

| Flag | Derivation Logic |
|------|------------------|
| SAFFL | "Y" if subject has at least one EX record (received study drug); else "N" |
| ITTFL | "Y" if subject has DS record with DSDECOD = "RANDOMIZED"; else "N" |
| PPROTFL | "Y" if ITTFL = "Y" and no major protocol deviations recorded; else "N" |
| COMPLFL | "Y" if subject has DS record with DSDECOD = "COMPLETED" at study level; else "N" |

### DCSREAS (Discontinuation Reason)

```
If DS has record with DSCAT = "DISPOSITION EVENT" and DSSCAT = "STUDY PARTICIPATION"
  and DS record is the last such record with DSDECOD not "COMPLETED":

DCSREAS = DSDECOD of the last non-completion disposition event

Otherwise: DCSREAS = null (subject completed study)
```

### Baseline Characteristics

```
BASE_HBA1C = AVAL from ADLB where PARAMCD = "HBA1C" and ADY <= 1 and ANL01FL = "Y"
BASE_BMI   = (BASE_WEIGHT / (BASE_HEIGHT/100)^2) from VS at baseline visit
BASE_EGFR  = AVAL from ADLB where PARAMCD = "EGFR" and ANL01FL = "Y"
```

---

## Generation Patterns

### Standard Two-Arm Phase 3 Trial

```json
{
  "dataset": "ADSL",
  "study": {
    "studyid": "T2DM-PH3-001",
    "arms": [
      { "trt01p": "Tirzepatide 15mg QW", "trt01pn": 1 },
      { "trt01p": "Placebo", "trt01pn": 2 }
    ]
  },
  "population": {
    "saffl_expected_rate": 0.98,
    "ittfl_expected_rate": 1.00,
    "pprotfl_expected_rate": 0.85,
    "complfl_expected_rate": 0.82
  },
  "age_groups": {
    "agegr1_cutoff": 65,
    "agegr1_labels": { "1": "<65", "2": ">=65" }
  },
  "baseline_characteristics": {
    "base_hba1c": { "mean": 8.1, "sd": 0.9, "min": 7.0, "max": 10.5 },
    "base_bmi": { "mean": 32.5, "sd": 5.2, "min": 25.0, "max": 45.0 },
    "base_egfr": { "mean": 85.0, "sd": 18.0, "min": 60.0, "max": 130.0 }
  }
}
```

### Multi-Arm Dose-Ranging Trial

```json
{
  "dataset": "ADSL",
  "study": {
    "studyid": "DOSE-RANGE-001",
    "arms": [
      { "trt01p": "Drug X 50mg BID", "trt01pn": 1 },
      { "trt01p": "Drug X 100mg BID", "trt01pn": 2 },
      { "trt01p": "Drug X 200mg BID", "trt01pn": 3 },
      { "trt01p": "Placebo", "trt01pn": 4 }
    ]
  },
  "population": {
    "saffl_expected_rate": 0.97,
    "ittfl_expected_rate": 1.00,
    "pprotfl_expected_rate": 0.80,
    "complfl_expected_rate": 0.78
  }
}
```

---

## Examples

### Example 1: Generate ADSL for Type 2 Diabetes Phase 3 Trial

**Request:** "Generate ADSL for a Phase 3 diabetes trial with 500 subjects, 1:1 randomization to treatment vs placebo, with baseline HbA1c between 7.0-10.5%"

**Output:**

```json
{
  "dataset": "ADSL",
  "metadata": {
    "studyid": "T2DM-PH3-001",
    "description": "Subject-Level Analysis Dataset - Type 2 Diabetes Phase 3",
    "n_subjects": 500
  },
  "records": [
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-001-0001",
      "SUBJID": "0001",
      "SITEID": "001",
      "AGE": 58,
      "AGEU": "YEARS",
      "SEX": "F",
      "RACE": "WHITE",
      "ETHNIC": "NOT HISPANIC OR LATINO",
      "TRT01P": "Tirzepatide 15mg QW",
      "TRT01PN": 1,
      "TRT01A": "Tirzepatide 15mg QW",
      "TRT01AN": 1,
      "AGEGR1": "<65",
      "AGEGR1N": 1,
      "SAFFL": "Y",
      "ITTFL": "Y",
      "PPROTFL": "Y",
      "COMPLFL": "Y",
      "RANDDT": "2024-03-15",
      "TRTSDT": "2024-03-15",
      "TRTEDT": "2024-09-20",
      "DCSREAS": null,
      "DTHFL": null,
      "BASE_HBA1C": 8.2,
      "BASE_BMI": 33.1,
      "BASE_EGFR": 78.5,
      "BASE_ALT": 24,
      "BASE_WEIGHT": 88.5
    },
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-001-0002",
      "SUBJID": "0002",
      "SITEID": "001",
      "AGE": 67,
      "AGEU": "YEARS",
      "SEX": "M",
      "RACE": "BLACK OR AFRICAN AMERICAN",
      "ETHNIC": "NOT HISPANIC OR LATINO",
      "TRT01P": "Placebo",
      "TRT01PN": 2,
      "TRT01A": "Placebo",
      "TRT01AN": 2,
      "AGEGR1": ">=65",
      "AGEGR1N": 2,
      "SAFFL": "Y",
      "ITTFL": "Y",
      "PPROTFL": "Y",
      "COMPLFL": "Y",
      "RANDDT": "2024-03-18",
      "TRTSDT": "2024-03-18",
      "TRTEDT": "2024-09-22",
      "DCSREAS": null,
      "DTHFL": null,
      "BASE_HBA1C": 8.7,
      "BASE_BMI": 29.8,
      "BASE_EGFR": 66.2,
      "BASE_ALT": 31,
      "BASE_WEIGHT": 92.0
    },
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-002-0073",
      "SUBJID": "0073",
      "SITEID": "002",
      "AGE": 52,
      "AGEU": "YEARS",
      "SEX": "F",
      "RACE": "ASIAN",
      "ETHNIC": "HISPANIC OR LATINO",
      "TRT01P": "Tirzepatide 15mg QW",
      "TRT01PN": 1,
      "TRT01A": "Tirzepatide 15mg QW",
      "TRT01AN": 1,
      "AGEGR1": "<65",
      "AGEGR1N": 1,
      "SAFFL": "Y",
      "ITTFL": "Y",
      "PPROTFL": "N",
      "COMPLFL": "N",
      "RANDDT": "2024-03-25",
      "TRTSDT": "2024-03-25",
      "TRTEDT": "2024-05-10",
      "DCSREAS": "ADVERSE EVENT",
      "DTHFL": null,
      "BASE_HBA1C": 9.1,
      "BASE_BMI": 35.4,
      "BASE_EGFR": 92.0,
      "BASE_ALT": 28,
      "BASE_WEIGHT": 76.3
    }
  ],
  "summary": {
    "by_arm": {
      "Tirzepatide 15mg QW": 250,
      "Placebo": 250
    },
    "by_agegr1": {
      "<65": 312,
      ">=65": 188
    },
    "population_flags": {
      "SAFFL_Y": 490,
      "SAFFL_N": 10,
      "ITTFL_Y": 500,
      "ITTFL_N": 0,
      "PPROTFL_Y": 425,
      "PPROTFL_N": 75,
      "COMPLFL_Y": 410,
      "COMPLFL_N": 90
    },
    "baseline_stats": {
      "hba1c": { "mean": 8.1, "sd": 0.9, "min": 7.0, "max": 10.4 },
      "bmi": { "mean": 32.5, "sd": 5.2, "min": 25.1, "max": 44.8 },
      "age": { "mean": 54.8, "sd": 11.2, "min": 22, "max": 75 }
    }
  }
}
```

### Example 2: ADSL with Death and Discontinuation

**Request:** "Generate ADSL for an oncology trial showing subjects with SAFFL, ITTFL, and death/completion status"

**Output:**

```json
{
  "dataset": "ADSL",
  "metadata": {
    "studyid": "ONC-SURV-001",
    "description": "Subject-Level Analysis Dataset - Oncology Survival Trial",
    "n_subjects": 200
  },
  "records": [
    {
      "STUDYID": "ONC-SURV-001",
      "USUBJID": "ONC-SURV-001-001-0042",
      "SUBJID": "0042",
      "SITEID": "001",
      "AGE": 72,
      "AGEU": "YEARS",
      "SEX": "M",
      "RACE": "WHITE",
      "ETHNIC": "NOT HISPANIC OR LATINO",
      "TRT01P": "Pembrolizumab 200mg Q3W",
      "TRT01PN": 1,
      "TRT01A": "Pembrolizumab 200mg Q3W",
      "TRT01AN": 1,
      "AGEGR1": ">=65",
      "AGEGR1N": 2,
      "SAFFL": "Y",
      "ITTFL": "Y",
      "PPROTFL": "Y",
      "COMPLFL": "N",
      "RANDDT": "2024-02-01",
      "TRTSDT": "2024-02-01",
      "TRTEDT": "2024-07-15",
      "DCSREAS": "DEATH",
      "DTHFL": "Y",
      "DTHDTC": "2024-07-20",
      "BASE_WEIGHT": 74.2,
      "BASE_ALT": 22
    },
    {
      "STUDYID": "ONC-SURV-001",
      "USUBJID": "ONC-SURV-001-002-0108",
      "SUBJID": "0108",
      "SITEID": "002",
      "AGE": 61,
      "AGEU": "YEARS",
      "SEX": "F",
      "RACE": "ASIAN",
      "ETHNIC": "NOT HISPANIC OR LATINO",
      "TRT01P": "Chemotherapy Control",
      "TRT01PN": 2,
      "TRT01A": "Chemotherapy Control",
      "TRT01AN": 2,
      "AGEGR1": "<65",
      "AGEGR1N": 1,
      "SAFFL": "Y",
      "ITTFL": "Y",
      "PPROTFL": "N",
      "COMPLFL": "N",
      "RANDDT": "2024-02-10",
      "TRTSDT": "2024-02-10",
      "TRTEDT": "2024-05-30",
      "DCSREAS": "ADVERSE EVENT",
      "DTHFL": null,
      "BASE_WEIGHT": 58.7,
      "BASE_ALT": 19
    }
  ],
  "summary": {
    "population_flags": {
      "SAFFL_Y": 198,
      "ITTFL_Y": 200,
      "PPROTFL_Y": 168,
      "COMPLFL_N": 55,
      "death_count": 32
    }
  }
}
```

---

## Validation Rules

| Rule | Requirement | Example |
|------|-------------|---------|
| USUBJID | Must match DM.USUBJID exactly | T2DM-PH3-001-001-0001 |
| SAFFL | "Y" only if EX domain has dose record for subject | Y |
| ITTFL | "Y" only if DS has RANDOMIZED record | Y |
| PPROTFL | "Y" only if ITTFL = "Y" and no major deviations | Y |
| AGEGR1 | Derived from AGE; must match AGEGR1N | <65 paired with 1 |
| TRT01PN | Unique integer per arm, consistent across all subjects | 1, 2, 3, 4 |
| COMPLFL | "Y" only if DS has study-level COMPLETED record | Y |
| DCSREAS | Must be from CDISC DS.DSDECOD when non-null | ADVERSE EVENT |
| DTHFL | "Y" must agree with DM.DTHFL | Y |
| BASE_HBA1C | Must be from last non-missing LB before TRTSDT | 8.2 |

### Business Rules

- **One Record Per Subject**: ADSL must have exactly one record per USUBJID
- **SAFFL Implies Exposure**: If SAFFL = "Y", at least one EX record must exist for the subject
- **ITTFL Implies Randomization**: If ITTFL = "Y", subject must have DS record with DSDECOD = "RANDOMIZED"
- **Population Nesting**: PPROTFL = "Y" implies ITTFL = "Y" implies SAFFL = "Y" (per-protocol subjects are a subset of ITT, which are a subset of safety)
- **Age Group Consistency**: AGEGR1 and AGEGR1N must be consistent across the study
- **Treatment Assignment**: TRT01P must uniquely identify the treatment arm; TRT01PN must be a consistent numeric code across the dataset
- **Discontinuation Completeness**: If COMPLFL = "N", DCSREAS should generally be non-null
- **Death Handling**: If DTHFL = "Y", the death date must be documented

---

## Related Skills

### TrialSim ADaM Datasets
- [README.md](README.md) - ADaM skills directory
- [adae.md](adae.md) - ADAE references TRT01P, SAFFL from ADSL
- [adlb.md](adlb.md) - ADLB references TRT01P, SAFFL from ADSL
- [adeff.md](adeff.md) - ADEFF references TRT01P, ITTFL from ADSL
- [adtte.md](adtte.md) - ADTTE references TRT01P, SAFFL from ADSL

### TrialSim SDTM Domains
- [../../domains/demographics-dm.md](../../domains/demographics-dm.md) - DM domain (source of subject identifiers, demographics, treatment arms)
- [../../domains/disposition-ds.md](../../domains/disposition-ds.md) - DS domain (source of randomization, completion, discontinuation)
- [../../domains/exposure-ex.md](../../domains/exposure-ex.md) - EX domain (source of SAFFL, TRTSDT, TRTEDT)

### TrialSim Core
- [../../clinical-trials-domain.md](../../clinical-trials-domain.md) - Trial design and population definitions
- [../../phase3-pivotal.md](../../phase3-pivotal.md) - Phase 3 population flag patterns

### Formats
- [../../../formats/cdisc-adam.md](../../../formats/cdisc-adam.md) - ADaM format specification

> **Integration Pattern:** ADSL should be generated first before any other ADaM dataset because all BDS and TTE datasets reference ADSL population flags (SAFFL, ITTFL, PPROTFL) to subset their analysis populations.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12 | Initial ADSL dataset skill with population flags, baseline characteristics, and derivation rules |
