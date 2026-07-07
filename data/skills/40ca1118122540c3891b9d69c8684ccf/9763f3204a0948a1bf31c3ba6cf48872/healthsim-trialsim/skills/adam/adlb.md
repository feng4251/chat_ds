---
name: adam-adlb
description: |
  Generate ADaM ADLB (Laboratory Analysis Dataset) with BDS structure, change 
  from baseline, percent change, toxicity grading (CTCAE v5.0), shift tables, 
  and Hy's Law criteria flags. Derived from SDTM LB domain with analysis flags 
  for key visits. Triggers: "ADLB", "laboratory analysis", "lab shift table", 
  "toxicity grade", "CTCAE", "change from baseline", "Hy's Law", "liver 
  function analysis", "lab parameters".
---

# Laboratory Analysis Dataset (ADLB)

The ADLB dataset transforms SDTM LB records into a BDS-structured analysis dataset with derived variables for change from baseline, toxicity grading, shift tables, and key safety criteria such as Hy's Law for drug-induced liver injury.

---

## For Claude

This is a **BDS-structured ADaM dataset skill** for generating laboratory analysis data. ADLB supports all lab-based safety and efficacy summaries required for regulatory submissions.

**Always apply this skill when you see:**
- Requests for laboratory analysis datasets
- Change from baseline calculations for lab parameters
- Shift table analysis (baseline grade vs post-baseline grade)
- CTCAE toxicity grading for laboratory values
- Hy's Law criteria evaluation for hepatotoxicity
- Analysis flags for specific study visits
- Parameter-level derivations (ANL01FL, BASE, CHG, PCHG)

**Key responsibilities:**
- Create one BDS record per subject per lab parameter per visit
- Derive baseline (BASE/BASEC) as last non-missing value before first dose
- Calculate change from baseline (CHG) and percent change (PCHG)
- Assign CTCAE v5.0 toxicity grades (TOXGR) to numeric lab results
- Build shift tables (SHIFT1) comparing baseline grade to post-baseline grade
- Flag key analysis visits (ANL01FL) for primary analysis
- Apply Hy's Law criteria (CRIT1/CRIT1FL) for liver safety assessment
- Maintain full traceability to SDTM LB records

---

## ADaM Variables

### Required Variables (BDS Structure)

| Variable | Label | Type | Length | Description |
|----------|-------|------|--------|-------------|
| STUDYID | Study Identifier | Char | 20 | Unique study ID |
| USUBJID | Unique Subject Identifier | Char | 40 | From DM domain |
| PARAM | Parameter | Char | 200 | Full lab test name |
| PARAMCD | Parameter Code | Char | 8 | Short code from LB.LBTESTCD |
| AVAL | Analysis Value | Num | 8 | Numeric analysis value |
| AVALC | Analysis Value (Character) | Char | 200 | Character value when not numeric |
| ADT | Analysis Date | Num | 8 | Date of collection as SAS date |
| ADY | Analysis Relative Day | Num | 8 | Study day relative to reference (RFSTDTC) |

### Expected Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| LBTESTCD | Lab Test Code | Char | From LB.LBTESTCD |
| LBTEST | Lab Test Name | Char | From LB.LBTEST |
| LBCAT | Category for Lab Test | Char | CHEMISTRY, HEMATOLOGY, URINALYSIS |
| LBSPEC | Specimen Type | Char | SERUM, PLASMA, URINE, BLOOD |
| LBSTRESU | Standard Units | Char | SI units from LB |
| LBSTNRLO | Reference Range Lower Limit | Num | Normal low from LB |
| LBSTNRHI | Reference Range Upper Limit | Num | Normal high from LB |
| LBNRIND | Reference Range Indicator | Char | NORMAL, HIGH, LOW |
| VISITNUM | Visit Number | Num | Protocol visit number |
| VISIT | Visit Name | Char | Visit description |
| ATPT | Analysis Timepoint | Char | Scheduled visit timepoint |

### Derivation Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| BASE | Baseline Value | Num | Last non-missing AVAL before TRTSDT |
| BASEC | Baseline Value (Character) | Char | Last non-missing AVALC before TRTSDT |
| CHG | Change from Baseline | Num | AVAL - BASE |
| PCHG | Percent Change from Baseline | Num | (AVAL - BASE) / BASE * 100 |
| SHIFT1 | Toxicity Grade Shift | Char | Baseline grade "→" analysis grade (e.g., 0→2) |
| ANL01FL | Analysis Visit 01 Flag | Char | Y for primary analysis timepoint records |
| ANL02FL | Analysis Visit 02 Flag | Char | Y for secondary analysis timepoint records |
| TOXGR | Toxicity Grade | Num | 0-5 per CTCAE v5.0 |
| TOXGRDESC | Toxicity Grade Description | Char | Grade 0=None, 1=Mild, 2=Moderate, 3=Severe, 4=Life-threatening, 5=Death |
| CRIT1 | Hy's Law Criterion 1 | Char | ALT > 3x ULN |
| CRIT1FL | Hy's Law Criterion 1 Flag | Char | Y if criterion met |
| CRIT2 | Hy's Law Criterion 2 | Char | BILI > 2x ULN |
| CRIT2FL | Hy's Law Criterion 2 Flag | Char | Y if criterion met |
| CRIT3 | Hy's Law Criterion 3 | Char | ALP < 2x ULN (no cholestasis) |
| CRIT3FL | Hy's Law Criterion 3 Flag | Char | Y if criterion met |
| HYSLAWFL | Hy's Law Case Flag | Char | Y if CRIT1FL=Y AND CRIT2FL=Y AND CRIT3FL=Y |
| DTYPE | Derivation Type | Char | LOCF, AVERAGE, etc. |

### Traceability Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| SRCDOM | Source Domain | Char | "LB" |
| SRCVAR | Source Variable | Char | "LBSEQ" |
| SRCSEQ | Source Sequence Number | Num | LB.LBSEQ value |

---

## Key Derivations

### PARAM/PARAMCD (Parameter Name/Code)

```
PARAM = LB.LBTEST (e.g., "Alanine Aminotransferase")
PARAMCD = LB.LBTESTCD (e.g., "ALT")
```

### AVAL/AVALC (Analysis Value Numeric/Character)

```
AVAL = LB.LBSTRESN (when result is numeric)
AVALC = LB.LBSTRESC (when result is character, e.g., "<1.0", "POSITIVE")
```

### BASE/BASEC (Baseline Value)

```
Definition: Last non-missing value (AVAL or AVALC) before or on the first dose date (ADSL.TRTSDT).

For each USUBJID and PARAMCD:
  1. Select all records where ADY <= 1 (relative to TRTSDT)
  2. Take the record with the maximum ADT (closest to TRTSDT but not after)
  3. BASE = AVAL from that record
  4. BASEC = AVALC from that record

If no pre-dose record exists, BASE/BASEC = null.
```

### CHG (Change from Baseline)

```
CHG = AVAL - BASE (where both are numeric)
CHG = null (if BASE or AVAL is missing)

Note: CHG is calculated for post-baseline records only.
```

### PCHG (Percent Change from Baseline)

```
PCHG = (AVAL - BASE) / BASE * 100
PCHG = null (if BASE = 0 or missing)

Note: Multiply by 100 to express as percentage.
```

### SHIFT1 (Toxicity Grade Shift)

```
If both baseline and post-baseline TOXGR values are available:
  SHIFT1 = TOXGR_baseline || "→" || TOXGR_postbaseline
  Example: "0→1" (Grade 0 at baseline, Grade 1 at analysis visit)

Primary use: Shift table generation (baseline vs maximum post-baseline).
```

### ANL01FL (Analysis Flag for Visit 01)

```
For each USUBJID and PARAMCD at a given analysis visit:
  ANL01FL = "Y" for the record closest to the protocol-scheduled visit date
  All other records for that USUBJID/PARAMCD/visit get ANL01FL = null

Typically ANL01FL marks the primary endpoint visit (e.g., Week 24).
```

### CRIT1/CRIT1FL (Hy's Law Criteria for Liver Tests)

Hy's Law identifies potential drug-induced liver injury:

```
CRIT1 (ALT > 3x ULN): AVAL > 3 * LB.LBSTNRHI where PARAMCD = "ALT"
  CRIT1FL = "Y" if CRIT1 is true

CRIT2 (BILI > 2x ULN): AVAL > 2 * LB.LBSTNRHI where PARAMCD = "BILI"
  CRIT2FL = "Y" if CRIT2 is true

CRIT3 (ALP < 2x ULN): AVAL < 2 * LB.LBSTNRHI where PARAMCD = "ALP"
  CRIT3FL = "Y" if CRIT3 is true

HYSLAWFL = "Y" if:
  CRIT1FL = "Y" AND CRIT2FL = "Y" AND CRIT3FL = "Y"
  (All three criteria met at the same visit for the same subject)

Note: Hy's Law assessment requires checking these criteria on the same specimen collection date.
```

### TOXGR (CTCAE v5.0 Toxicity Grade)

Laboratory toxicity grading per CTCAE v5.0 for common parameters:

```
Parameter: ALT
  Grade 0: ≤ ULN
  Grade 1: > ULN to 3.0 x ULN
  Grade 2: > 3.0 to 5.0 x ULN
  Grade 3: > 5.0 to 20.0 x ULN
  Grade 4: > 20.0 x ULN

Parameter: AST
  Grade 0: ≤ ULN
  Grade 1: > ULN to 3.0 x ULN
  Grade 2: > 3.0 to 5.0 x ULN
  Grade 3: > 5.0 to 20.0 x ULN
  Grade 4: > 20.0 x ULN

Parameter: BILI (Total Bilirubin)
  Grade 0: ≤ ULN
  Grade 1: > ULN to 1.5 x ULN
  Grade 2: > 1.5 to 3.0 x ULN
  Grade 3: > 3.0 to 10.0 x ULN
  Grade 4: > 10.0 x ULN

Parameter: CREAT (Creatinine)
  Grade 0: ≤ ULN
  Grade 1: > ULN to 1.5 x ULN
  Grade 2: > 1.5 to 3.0 x ULN
  Grade 3: > 3.0 to 6.0 x ULN
  Grade 4: > 6.0 x ULN

Parameter: HGB (Hemoglobin)
  Grade 0: ≥ LLN
  Grade 1: < LLN to 100 g/L
  Grade 2: < 100 to 80 g/L
  Grade 3: < 80 to 65 g/L
  Grade 4: < 65 g/L (life-threatening)

Parameter: PLAT (Platelets)
  Grade 0: ≥ LLN
  Grade 1: < LLN to 75 x 10^9/L
  Grade 2: < 75 to 50 x 10^9/L
  Grade 3: < 50 to 25 x 10^9/L
  Grade 4: < 25 x 10^9/L
```

---

## Generation Patterns

### Liver Function Panel Baseline to Week 24

```json
{
  "dataset": "ADLB",
  "source": {
    "domain": "LB",
    "merge_with": "ADSL"
  },
  "parameters": [
    { "paramcd": "ALT", "param": "Alanine Aminotransferase", "unit": "U/L" },
    { "paramcd": "AST", "param": "Aspartate Aminotransferase", "unit": "U/L" },
    { "paramcd": "ALP", "param": "Alkaline Phosphatase", "unit": "U/L" },
    { "paramcd": "BILI", "param": "Total Bilirubin", "unit": "umol/L" }
  ],
  "visits": [
    { "visitnum": 2, "visit": "BASELINE", "ady": 1 },
    { "visitnum": 4, "visit": "WEEK 4", "ady": 29 },
    { "visitnum": 8, "visit": "WEEK 12", "ady": 85 },
    { "visitnum": 12, "visit": "WEEK 24", "ady": 169 }
  ],
  "analysis_flags": {
    "ANL01FL": "WEEK 24 (primary endpoint)",
    "ANL02FL": "WEEK 12 (secondary timepoint)"
  },
  "baseline_definition": {
    "window": "Last value with ADY <= 1 before TRTSDT",
    "missing_rule": "Leave BASE null if no pre-dose value"
  },
  "toxicity_grading": "CTCAE v5.0"
}
```

### Hy's Law Assessment Pattern

```json
{
  "dataset": "ADLB",
  "hys_law_assessment": {
    "description": "Cross-parameter evaluation for potential drug-induced liver injury",
    "criteria": {
      "crit1": { "paramcd": "ALT", "threshold": "> 3x ULN" },
      "crit2": { "paramcd": "BILI", "threshold": "> 2x ULN" },
      "crit3": { "paramcd": "ALP", "threshold": "< 2x ULN" }
    },
    "timing": "All three criteria must be met on the same specimen collection date",
    "exclusion": "Must rule out cholestasis (ALP < 2x ULN)",
    "result": "HYSLAWFL = 'Y' when all three criteria satisfied simultaneously"
  }
}
```

### Shift Table Generation Pattern

```json
{
  "dataset": "ADLB",
  "shift_table": {
    "description": "Baseline vs maximum post-baseline toxicity grade shift",
    "rows": "Baseline TOXGR (0, 1, 2, 3, 4)",
    "columns": "Maximum post-baseline TOXGR (0, 1, 2, 3, 4)",
    "cells": "Number of subjects with that shift",
    "example": {
      "paramcd": "ALT",
      "data": {
        "0_to_0": 210,
        "0_to_1": 45,
        "0_to_2": 12,
        "0_to_3": 3,
        "1_to_2": 5,
        "1_to_3": 1
      }
    }
  }
}
```

---

## Examples

### Example 1: Generate ADLB Liver Function Panel with Hy's Law Assessment

**Request:** "Generate ADLB for a hepatic safety trial showing ALT, AST, ALP, BILI at baseline, week 4, week 12, and week 24 with change from baseline, toxicity grades, and Hy's Law flags"

**Output:**

```json
{
  "dataset": "ADLB",
  "metadata": {
    "studyid": "HEP-SAFE-001",
    "description": "Laboratory Analysis Dataset - Hepatic Safety Trial",
    "n_subjects": 300,
    "n_records": 4800
  },
  "records": [
    {
      "STUDYID": "HEP-SAFE-001",
      "USUBJID": "HEP-SAFE-001-001-0025",
      "PARAM": "Alanine Aminotransferase",
      "PARAMCD": "ALT",
      "AVAL": 32,
      "LBCAT": "CHEMISTRY",
      "LBSPEC": "SERUM",
      "LBSTRESU": "U/L",
      "LBSTNRLO": 7,
      "LBSTNRHI": 56,
      "LBNRIND": "NORMAL",
      "ADT": "2024-03-01",
      "ADY": 1,
      "VISITNUM": 2,
      "VISIT": "BASELINE",
      "TOXGR": 0,
      "TOXGRDESC": "Grade 0: Within normal limits",
      "BASE": 32,
      "CHG": null,
      "PCHG": null,
      "ANL01FL": null,
      "SHIFT1": null,
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 1
    },
    {
      "STUDYID": "HEP-SAFE-001",
      "USUBJID": "HEP-SAFE-001-001-0025",
      "PARAM": "Alanine Aminotransferase",
      "PARAMCD": "ALT",
      "AVAL": 48,
      "LBCAT": "CHEMISTRY",
      "LBSPEC": "SERUM",
      "LBSTRESU": "U/L",
      "LBSTNRLO": 7,
      "LBSTNRHI": 56,
      "LBNRIND": "NORMAL",
      "ADT": "2024-03-29",
      "ADY": 29,
      "VISITNUM": 4,
      "VISIT": "WEEK 4",
      "TOXGR": 0,
      "TOXGRDESC": "Grade 0: Within normal limits",
      "BASE": 32,
      "CHG": 16,
      "PCHG": 50.0,
      "ANL01FL": null,
      "SHIFT1": null,
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 5
    },
    {
      "STUDYID": "HEP-SAFE-001",
      "USUBJID": "HEP-SAFE-001-001-0025",
      "PARAM": "Alanine Aminotransferase",
      "PARAMCD": "ALT",
      "AVAL": 188,
      "LBCAT": "CHEMISTRY",
      "LBSPEC": "SERUM",
      "LBSTRESU": "U/L",
      "LBSTNRLO": 7,
      "LBSTNRHI": 56,
      "LBNRIND": "HIGH",
      "ADT": "2024-05-25",
      "ADY": 85,
      "VISITNUM": 8,
      "VISIT": "WEEK 12",
      "TOXGR": 3,
      "TOXGRDESC": "Grade 3: > 5.0-20.0 x ULN",
      "BASE": 32,
      "CHG": 156,
      "PCHG": 487.5,
      "SHIFT1": "0→3",
      "ANL01FL": null,
      "CRIT1": "ALT > 3x ULN",
      "CRIT1FL": "Y",
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 9
    },
    {
      "STUDYID": "HEP-SAFE-001",
      "USUBJID": "HEP-SAFE-001-001-0025",
      "PARAM": "Alanine Aminotransferase",
      "PARAMCD": "ALT",
      "AVAL": 298,
      "LBCAT": "CHEMISTRY",
      "LBSPEC": "SERUM",
      "LBSTRESU": "U/L",
      "LBSTNRLO": 7,
      "LBSTNRHI": 56,
      "LBNRIND": "HIGH",
      "ADT": "2024-08-17",
      "ADY": 169,
      "VISITNUM": 12,
      "VISIT": "WEEK 24",
      "TOXGR": 3,
      "TOXGRDESC": "Grade 3: > 5.0-20.0 x ULN",
      "BASE": 32,
      "CHG": 266,
      "PCHG": 831.25,
      "SHIFT1": "0→3",
      "ANL01FL": "Y",
      "CRIT1": "ALT > 3x ULN",
      "CRIT1FL": "Y",
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 13
    },
    {
      "STUDYID": "HEP-SAFE-001",
      "USUBJID": "HEP-SAFE-001-001-0025",
      "PARAM": "Total Bilirubin",
      "PARAMCD": "BILI",
      "AVAL": 45,
      "LBCAT": "CHEMISTRY",
      "LBSPEC": "SERUM",
      "LBSTRESU": "umol/L",
      "LBSTNRLO": 3,
      "LBSTNRHI": 21,
      "LBNRIND": "HIGH",
      "ADT": "2024-08-17",
      "ADY": 169,
      "VISITNUM": 12,
      "VISIT": "WEEK 24",
      "TOXGR": 3,
      "TOXGRDESC": "Grade 3: > 3.0-10.0 x ULN",
      "BASE": 11,
      "CHG": 34,
      "PCHG": 309.09,
      "ANL01FL": "Y",
      "CRIT2": "BILI > 2x ULN",
      "CRIT2FL": "Y",
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 14
    },
    {
      "STUDYID": "HEP-SAFE-001",
      "USUBJID": "HEP-SAFE-001-001-0025",
      "PARAM": "Alkaline Phosphatase",
      "PARAMCD": "ALP",
      "AVAL": 125,
      "LBCAT": "CHEMISTRY",
      "LBSPEC": "SERUM",
      "LBSTRESU": "U/L",
      "LBSTNRLO": 44,
      "LBSTNRHI": 147,
      "LBNRIND": "NORMAL",
      "ADT": "2024-08-17",
      "ADY": 169,
      "VISITNUM": 12,
      "VISIT": "WEEK 24",
      "TOXGR": 0,
      "TOXGRDESC": "Grade 0: Within normal limits",
      "BASE": 68,
      "CHG": 57,
      "PCHG": 83.82,
      "ANL01FL": "Y",
      "CRIT3": "ALP < 2x ULN",
      "CRIT3FL": "Y",
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 15
    }
  ],
  "summary": {
    "hys_law_cases": 2,
    "hys_law_subjects": ["HEP-SAFE-001-001-0025", "HEP-SAFE-001-002-0108"],
    "by_parameter": {
      "ALT": {
        "n_records": 1200,
        "subjects_with_grade_3_4": 8,
        "mean_baseline": 30.5,
        "mean_week24_chg": 15.2
      }
    },
    "shift_table_alt": {
      "grade0_to_grade0": 210,
      "grade0_to_grade1": 35,
      "grade0_to_grade2": 8,
      "grade0_to_grade3": 3,
      "grade0_to_grade4": 0
    }
  }
}
```

### Example 2: Shift Table Analysis for Creatinine

**Request:** "Generate ADLB creatinine data across visits to build a shift table comparing baseline to maximum post-baseline toxicity grade"

**Output:**

```json
{
  "dataset": "ADLB",
  "metadata": {
    "studyid": "RENAL-001",
    "description": "Creatinine shift table data across study visits",
    "parameter": "CREAT"
  },
  "shift_table_ready": {
    "parameter": "Creatinine",
    "paramcd": "CREAT",
    "unit": "umol/L",
    "toxicity_grading": "CTCAE v5.0",
    "matrix": {
      "baseline_grade_0": {
        "post_max_grade_0": 235,
        "post_max_grade_1": 18,
        "post_max_grade_2": 5,
        "post_max_grade_3": 2,
        "post_max_grade_4": 0
      },
      "baseline_grade_1": {
        "post_max_grade_0": 12,
        "post_max_grade_1": 20,
        "post_max_grade_2": 4,
        "post_max_grade_3": 1,
        "post_max_grade_4": 0
      },
      "baseline_grade_2": {
        "post_max_grade_0": 0,
        "post_max_grade_1": 2,
        "post_max_grade_2": 5,
        "post_max_grade_3": 1,
        "post_max_grade_4": 0
      }
    },
    "total_n": 300
  }
}
```

---

## Validation Rules

| Rule | Requirement | Example |
|------|-------------|---------|
| PARAMCD | Must match LB.LBTESTCD exactly | ALT |
| AVAL | Numeric when LBSTRESN is numeric | 32 |
| AVALC | Character when LBSTRESC is non-numeric | "<1.0" |
| BASE | Last non-missing AVAL with ADY <= 1 | 32 |
| CHG | AVAL - BASE for post-baseline records | 16 |
| PCHG | (AVAL - BASE) / BASE * 100 | 50.0 |
| TOXGR | Integer 0-5 per CTCAE v5.0 parameter rules | 3 |
| SHIFT1 | Format: baseline_grade→post_baseline_grade | 0→3 |
| ANL01FL | Only one Y per USUBJID per PARAMCD per analysis visit | Y |
| HYSLAWFL | Y only if CRIT1FL=Y, CRIT2FL=Y, and CRIT3FL=Y on same visit | Y |
| SRCDOM | Must be "LB" | LB |
| SRCVAR | Must be "LBSEQ" | LBSEQ |

### Business Rules

- **One Record Per Parameter Per Visit Per Subject**: Each USUBJID has one ADLB record per PARAMCD per VISITNUM. If multiple samples exist for the same visit, the closest to the scheduled date gets ANL01FL.
- **Baseline Definition**: BASE is defined as the last non-missing value with date ≤ first dose date. If no pre-dose value exists, BASE is null.
- **Change from Baseline**: CHG and PCHG are calculated only for post-baseline records (ADY > 1). For baseline records, CHG and PCHG are null.
- **Toxicity Grade Derivation**: TOXGR must be calculated per CTCAE v5.0 parameter-specific rules. The grading rules use multiples of ULN for chemistry parameters and absolute thresholds for hematology.
- **Hy's Law Assessment**: CRIT1FL, CRIT2FL, and CRIT3FL are assessed within the same specimen collection date. HYSLAWFL is only Y when all three criteria are met simultaneously.
- **Shift Table Construction**: SHIFT1 compares baseline grade to post-baseline grade. For maximum shift tables, use the highest post-baseline TOXGR across all visits.
- **Missing Data Handling**: Records with missing AVAL should maintain all metadata (visit, parameter) but have null values for derived variables. Do not drop missing records.

---

## Related Skills

### TrialSim ADaM Datasets
- [README.md](README.md) - ADaM skills directory
- [adsl.md](adsl.md) - ADSL (baseline reference dates, population flags)
- [adae.md](adae.md) - ADAE (lab abnormalities may trigger AEs)
- [adeff.md](adeff.md) - ADEFF (lab-based efficacy endpoints like HbA1c)
- [adtte.md](adtte.md) - ADTTE (time to lab abnormality)

### TrialSim SDTM Domains
- [../../domains/laboratory-lb.md](../../domains/laboratory-lb.md) - LB domain (source data for ADLB)
- [../../domains/demographics-dm.md](../../domains/demographics-dm.md) - DM domain (USUBJID, reference dates)

### Formats
- [../../../formats/cdisc-adam.md](../../../formats/cdisc-adam.md) - ADaM format specification

> **Integration Pattern:** ADLB generates analysis-ready lab records by merging LB domain data with ADSL.TRTSDT for baseline derivation. CTCAE v5.0 toxicity grades are assigned to all post-baseline results. Hy's Law assessment requires cross-parameter evaluation within the same visit date.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12 | Initial ADLB dataset skill with CTCAE v5.0 grading, Hy's Law criteria, and shift table derivations |
