---
name: adam-adeff
description: |
  Generate ADaM ADEFF (Efficacy Analysis Dataset) with BDS structure, responder 
  flags, change from baseline, and key efficacy endpoint derivations for 
  diabetes, cardiovascular, and other therapeutic areas. Derived from SDTM LB 
  and VS domains. Triggers: "ADEFF", "efficacy analysis", "responder", 
  "endpoint", "HBA1C", "FPG", "change from baseline efficacy", "clinical 
  endpoint", "primary endpoint", "key secondary endpoint".
---

# Efficacy Analysis Dataset (ADEFF)

The ADEFF dataset provides analysis-ready efficacy data with responder flags, change from baseline calculations, and endpoint-specific derivations. It supports primary and key secondary efficacy endpoint analyses across therapeutic areas.

---

## For Claude

This is a **BDS-structured ADaM dataset skill** for generating efficacy analysis data. ADEFF supports the primary and secondary efficacy endpoint analyses required for regulatory approval.

**Always apply this skill when you see:**
- Requests for efficacy analysis datasets
- Primary or secondary endpoint analysis
- Responder definitions (HbA1c < 7.0%, weight loss >= 5%)
- Change from baseline for efficacy parameters
- Therapeutic area-specific endpoint derivations
- Criteria flags for clinically meaningful response

**Key responsibilities:**
- Derive analysis values (AVAL) from SDTM LB or VS domains
- Calculate baseline (BASE) and change from baseline (CHG)
- Apply responder criteria (RESPFL) based on protocol-defined thresholds
- Flag records for key analysis visits (ANL01FL)
- Support flexible PARAMCD definitions per therapeutic area
- Maintain full traceability to SDTM source records

---

## ADaM Variables

### Required Variables (BDS Structure)

| Variable | Label | Type | Length | Description |
|----------|-------|------|--------|-------------|
| STUDYID | Study Identifier | Char | 20 | Unique study ID |
| USUBJID | Unique Subject Identifier | Char | 40 | From DM domain |
| PARAM | Parameter | Char | 200 | Full efficacy parameter description |
| PARAMCD | Parameter Code | Char | 8 | Short parameter code (max 8 chars) |
| AVAL | Analysis Value | Num | 8 | Numeric analysis value |
| AVALC | Analysis Value (Character) | Char | 200 | Character value when needed |
| ADT | Analysis Date | Num | 8 | Date of assessment (SAS date) |
| ADY | Analysis Relative Day | Num | 8 | Study day relative to reference |

### Expected Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| BASE | Baseline Value | Num | Last non-missing AVAL before TRTSDT |
| CHG | Change from Baseline | Num | AVAL - BASE |
| PCHG | Percent Change from Baseline | Num | (AVAL - BASE) / BASE * 100 |
| AVISITN | Analysis Visit Number | Num | Numeric analysis visit |
| AVISIT | Analysis Visit | Char | Analysis visit description |
| ATPT | Analysis Timepoint | Char | Scheduled timepoint |
| ABLFL | Baseline Record Flag | Char | Y for baseline record |
| ANL01FL | Analysis Flag 01 | Char | Y for primary endpoint records |
| ANL02FL | Analysis Flag 02 | Char | Y for key secondary endpoint records |
| DTYPE | Derivation Type | Char | LOCF, WOCF, AVERAGE |

### Responder and Criterion Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| RESPFL | Responder Flag | Char | Y if subject meets responder criteria |
| CRIT1 | Criterion 1 | Char | Description of criterion |
| CRIT1FL | Criterion 1 Flag | Char | Y if criterion 1 met |
| CRIT2 | Criterion 2 | Char | Description of criterion |
| CRIT2FL | Criterion 2 Flag | Char | Y if criterion 2 met |

### Traceability Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| SRCDOM | Source Domain | Char | LB, VS, QS |
| SRCVAR | Source Variable | Char | LBSEQ, VSSEQ, QSSEQ |
| SRCSEQ | Source Sequence Number | Num | Sequence number in source domain |

---

## Parameter Definitions by Therapeutic Area

### Diabetes (Type 2)

| PARAMCD | PARAM | Unit | Source | Responder Criteria |
|---------|-------|------|--------|--------------------|
| HBA1C | Glycated Hemoglobin (HbA1c) | % | LB | < 7.0% at endpoint |
| FPG | Fasting Plasma Glucose | mmol/L | LB | < 7.0 mmol/L |
| WEIGHT | Body Weight | kg | VS | >= 5% reduction |
| SBP | Systolic Blood Pressure | mmHg | VS | < 130 mmHg |
| DBP | Diastolic Blood Pressure | mmHg | VS | < 80 mmHg |

### Cardiovascular

| PARAMCD | PARAM | Unit | Source | Responder Criteria |
|---------|-------|------|--------|--------------------|
| LDL | LDL Cholesterol | mmol/L | LB | < 1.8 mmol/L |
| HDL | HDL Cholesterol | mmol/L | LB | >= 1.0 mmol/L (M), >= 1.3 (F) |
| TRIG | Triglycerides | mmol/L | LB | < 1.7 mmol/L |
| SBP | Systolic Blood Pressure | mmHg | VS | < 130 mmHg |
| DBP | Diastolic Blood Pressure | mmHg | VS | < 80 mmHg |

### Obesity / Weight Management

| PARAMCD | PARAM | Unit | Source | Responder Criteria |
|---------|-------|------|--------|--------------------|
| WEIGHT | Body Weight | kg | VS | >= 5% reduction |
| BMI | Body Mass Index | kg/m^2 | Derived | < 30 kg/m^2 |
| WAISTC | Waist Circumference | cm | VS | Reduction > 5 cm |

---

## Key Derivations

### AVAL (Analysis Value)

```
AVAL = LB.LBSTRESN (for lab-based parameters)
AVAL = VS.VSSTRESN (for vital sign parameters)

When multiple assessments exist at the same scheduled visit (same AVISIT):
  Use the value closest to the scheduled visit date with ANL01FL = "Y"
```

### BASE (Baseline Value)

```
BASE = Last non-missing AVAL before or on first dose date (ADSL.TRTSDT)

For each USUBJID and PARAMCD:
  1. Filter to records where ADY <= 1 (before or on first dose)
  2. Select the record with the latest ADT
  3. BASE = AVAL from that record
  4. ABLFL = "Y" on the baseline record

If no pre-dose value exists: BASE = null, ABLFL = null on all records.
```

### CHG (Change from Baseline)

```
CHG = AVAL - BASE
Applies to post-baseline records only (ADY > 1).
For baseline records: CHG = null.
```

### PCHG (Percent Change)

```
PCHG = (AVAL - BASE) / BASE * 100
Null if BASE = 0 or BASE is missing.
```

### RESPFL (Responder Flag)

Responder definitions vary by therapeutic area and parameter. Examples:

```
Diabetes - HbA1c:
  RESPFL = "Y" when:
    PARAMCD = "HBA1C"
    AND AVISIT = primary endpoint visit (e.g., WEEK 24)
    AND AVAL < 7.0

Weight Management:
  RESPFL = "Y" when:
    PARAMCD = "WEIGHT"
    AND AVISIT = primary endpoint visit
    AND PCHG <= -5.0  (at least 5% weight loss)

Blood Pressure:
  RESPFL = "Y" when:
    PARAMCD = "SBP"
    AND AVISIT = primary endpoint visit
    AND AVAL < 130  (systolic target)
```

### CRIT1FL (Criterion 1 Met Flag)

Secondary criteria for clinically meaningful response:

```
CRIT1 (for HbA1c):
  CRIT1FL = "Y" when AVAL <= 6.5 (tight glycemic control)

CRIT1 (for Weight):
  CRIT1FL = "Y" when PCHG <= -10.0 (>= 10% weight loss)

CRIT1 (for LDL):
  CRIT1FL = "Y" when PCHG <= -50.0 (>= 50% LDL reduction)
```

### ANL01FL (Primary Analysis Flag)

```
For each USUBJID and PARAMCD:
  ANL01FL = "Y" for the record closest to the protocol-specified primary endpoint visit.
  
  Example: If primary endpoint is Week 24 (scheduled at ADY=168):
    The record with ADT closest to (RANDDT + 168 days) gets ANL01FL = "Y"

  Only one record per USUBJID per PARAMCD gets ANL01FL = "Y".
```

---

## Generation Patterns

### Diabetes Efficacy Analysis (HbA1c Primary Endpoint)

```json
{
  "dataset": "ADEFF",
  "study": {
    "studyid": "T2DM-PH3-001",
    "therapeutic_area": "Type 2 Diabetes",
    "primary_endpoint": "Change from baseline in HbA1c at Week 24",
    "key_secondary": [
      "Change from baseline in FPG at Week 24",
      "Change from baseline in body weight at Week 24",
      "Proportion of subjects achieving HbA1c < 7.0%"
    ]
  },
  "parameters": [
    {
      "paramcd": "HBA1C",
      "param": "Glycated Hemoglobin (HbA1c)",
      "unit": "%",
      "source_domain": "LB",
      "visits": ["BASELINE", "WEEK 4", "WEEK 12", "WEEK 24", "WEEK 36", "WEEK 48"],
      "responder_threshold": 7.0,
      "responder_description": "HbA1c < 7.0%"
    },
    {
      "paramcd": "FPG",
      "param": "Fasting Plasma Glucose",
      "unit": "mmol/L",
      "source_domain": "LB",
      "responder_threshold": 7.0,
      "responder_description": "FPG < 7.0 mmol/L"
    },
    {
      "paramcd": "WEIGHT",
      "param": "Body Weight",
      "unit": "kg",
      "source_domain": "VS",
      "responder_threshold_pct": -5.0,
      "responder_description": ">= 5% weight loss"
    }
  ],
  "analysis_flags": {
    "ANL01FL": "WEEK 24 (primary endpoint)",
    "ANL02FL": "WEEK 12 (interim analysis)"
  },
  "missing_data": {
    "intercurrent_events": {
      "rescue_medication": "Apply rescue medication handling per estimand strategy",
      "early_discontinuation": "Apply LOCF or mixed model per SAP"
    }
  }
}
```

### Responder Analysis Configuration

```json
{
  "dataset": "ADEFF",
  "responder_config": {
    "hba1c": {
      "threshold": 7.0,
      "direction": "less_than",
      "target_visit": "WEEK 24",
      "expected_rate_treatment": 0.65,
      "expected_rate_placebo": 0.20
    },
    "weight": {
      "threshold": -5.0,
      "direction": "pct_change",
      "target_visit": "WEEK 24",
      "expected_rate_treatment": 0.45,
      "expected_rate_placebo": 0.10
    }
  }
}
```

---

## Examples

### Example 1: Generate ADEFF for Type 2 Diabetes Phase 3 Trial

**Request:** "Generate ADEFF for a diabetes trial with HbA1c, FPG, and weight as endpoints. Show baseline, week 12, week 24 with responder flags."

**Output:**

```json
{
  "dataset": "ADEFF",
  "metadata": {
    "studyid": "T2DM-PH3-001",
    "description": "Efficacy Analysis Dataset - Type 2 Diabetes Phase 3",
    "n_subjects": 500,
    "n_records": 7500
  },
  "records": [
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-001-0001",
      "PARAM": "Glycated Hemoglobin (HbA1c)",
      "PARAMCD": "HBA1C",
      "AVAL": 8.2,
      "AVISITN": 2,
      "AVISIT": "BASELINE",
      "ADT": "2024-03-15",
      "ADY": 1,
      "ABLFL": "Y",
      "BASE": 8.2,
      "CHG": null,
      "PCHG": null,
      "RESPFL": null,
      "ANL01FL": null,
      "ANL02FL": null,
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 5
    },
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-001-0001",
      "PARAM": "Glycated Hemoglobin (HbA1c)",
      "PARAMCD": "HBA1C",
      "AVAL": 7.5,
      "AVISITN": 8,
      "AVISIT": "WEEK 12",
      "ADT": "2024-06-07",
      "ADY": 85,
      "ABLFL": null,
      "BASE": 8.2,
      "CHG": -0.7,
      "PCHG": -8.54,
      "RESPFL": null,
      "ANL01FL": null,
      "ANL02FL": "Y",
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 25
    },
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-001-0001",
      "PARAM": "Glycated Hemoglobin (HbA1c)",
      "PARAMCD": "HBA1C",
      "AVAL": 6.4,
      "AVISITN": 12,
      "AVISIT": "WEEK 24",
      "ADT": "2024-08-30",
      "ADY": 169,
      "ABLFL": null,
      "BASE": 8.2,
      "CHG": -1.8,
      "PCHG": -21.95,
      "RESPFL": "Y",
      "CRIT1": "HbA1c <= 6.5%",
      "CRIT1FL": "Y",
      "ANL01FL": "Y",
      "ANL02FL": null,
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 45
    },
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-001-0001",
      "PARAM": "Body Weight",
      "PARAMCD": "WEIGHT",
      "AVAL": 88.5,
      "AVISITN": 2,
      "AVISIT": "BASELINE",
      "ADT": "2024-03-15",
      "ADY": 1,
      "ABLFL": "Y",
      "BASE": 88.5,
      "CHG": null,
      "PCHG": null,
      "RESPFL": null,
      "ANL01FL": null,
      "SRCDOM": "VS",
      "SRCVAR": "VSSEQ",
      "SRCSEQ": 3
    },
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-001-0001",
      "PARAM": "Body Weight",
      "PARAMCD": "WEIGHT",
      "AVAL": 82.1,
      "AVISITN": 12,
      "AVISIT": "WEEK 24",
      "ADT": "2024-08-30",
      "ADY": 169,
      "ABLFL": null,
      "BASE": 88.5,
      "CHG": -6.4,
      "PCHG": -7.23,
      "RESPFL": "Y",
      "CRIT1": ">= 10% weight loss",
      "CRIT1FL": "N",
      "ANL01FL": "Y",
      "SRCDOM": "VS",
      "SRCVAR": "VSSEQ",
      "SRCSEQ": 25
    },
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-002-0073",
      "PARAM": "Glycated Hemoglobin (HbA1c)",
      "PARAMCD": "HBA1C",
      "AVAL": 9.1,
      "AVISITN": 2,
      "AVISIT": "BASELINE",
      "ADT": "2024-03-25",
      "ADY": 1,
      "ABLFL": "Y",
      "BASE": 9.1,
      "CHG": null,
      "PCHG": null,
      "ANL01FL": null,
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 10
    },
    {
      "STUDYID": "T2DM-PH3-001",
      "USUBJID": "T2DM-PH3-001-002-0073",
      "PARAM": "Glycated Hemoglobin (HbA1c)",
      "PARAMCD": "HBA1C",
      "AVAL": 8.3,
      "AVISITN": 12,
      "AVISIT": "WEEK 24",
      "ADT": "2024-09-08",
      "ADY": 169,
      "ABLFL": null,
      "BASE": 9.1,
      "CHG": -0.8,
      "PCHG": -8.79,
      "RESPFL": "N",
      "CRIT1FL": "N",
      "ANL01FL": "Y",
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 42
    }
  ],
  "summary": {
    "primary_endpoint": {
      "parameter": "HBA1C",
      "timepoint": "WEEK 24",
      "treatment": {
        "n": 250,
        "baseline_mean": 8.15,
        "week24_mean": 6.85,
        "mean_change": -1.30,
        "responder_rate": 68.0,
        "pct_confidence_interval": [63.1, 72.9]
      },
      "placebo": {
        "n": 250,
        "baseline_mean": 8.12,
        "week24_mean": 7.95,
        "mean_change": -0.17,
        "responder_rate": 18.4,
        "pct_confidence_interval": [14.2, 22.6]
      },
      "treatment_difference": {
        "mean_change_diff": -1.13,
        "p_value": "<0.0001"
      }
    },
    "key_secondary": {
      "weight_change_week24": {
        "treatment_mean_change": -5.8,
        "placebo_mean_change": -1.1,
        "weight_loss_5pct_responders": 45.2
      }
    }
  }
}
```

### Example 2: Cardiovascular Efficacy Endpoints

**Request:** "Generate ADEFF for a lipid-lowering trial with LDL, HDL, and triglycerides at baseline, week 12, and week 24"

**Output:**

```json
{
  "dataset": "ADEFF",
  "metadata": {
    "studyid": "LIPID-LOW-001",
    "description": "Efficacy Analysis - Lipid Lowering Trial",
    "therapeutic_area": "Cardiovascular"
  },
  "records": [
    {
      "STUDYID": "LIPID-LOW-001",
      "USUBJID": "LIPID-LOW-001-001-0020",
      "PARAM": "LDL Cholesterol",
      "PARAMCD": "LDL",
      "AVAL": 4.2,
      "AVISITN": 2,
      "AVISIT": "BASELINE",
      "ADT": "2024-05-10",
      "ADY": 1,
      "ABLFL": "Y",
      "BASE": 4.2,
      "CHG": null,
      "PCHG": null,
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 8
    },
    {
      "STUDYID": "LIPID-LOW-001",
      "USUBJID": "LIPID-LOW-001-001-0020",
      "PARAM": "LDL Cholesterol",
      "PARAMCD": "LDL",
      "AVAL": 1.8,
      "AVISITN": 12,
      "AVISIT": "WEEK 24",
      "ADT": "2024-10-25",
      "ADY": 169,
      "ABLFL": null,
      "BASE": 4.2,
      "CHG": -2.4,
      "PCHG": -57.14,
      "RESPFL": "Y",
      "CRIT1": ">= 50% LDL reduction",
      "CRIT1FL": "Y",
      "ANL01FL": "Y",
      "SRCDOM": "LB",
      "SRCVAR": "LBSEQ",
      "SRCSEQ": 48
    }
  ],
  "summary": {
    "ldl_cholesterol": {
      "treatment_mean_pct_change": -52.5,
      "placebo_mean_pct_change": -2.1,
      "ldl_lt_1_8_responder_rate": 72.0
    }
  }
}
```

---

## Validation Rules

| Rule | Requirement | Example |
|------|-------------|---------|
| PARAMCD | Max 8 characters, consistent within study | HBA1C |
| AVAL | Numeric, within biologically plausible range | 6.4 |
| BASE | Last non-missing AVAL before TRTSDT | 8.2 |
| CHG | AVAL - BASE for post-baseline records | -1.8 |
| PCHG | (AVAL - BASE) / BASE * 100 | -21.95 |
| ABLFL | Only one Y per USUBJID per PARAMCD | Y |
| ANL01FL | Only one Y per USUBJID per PARAMCD | Y |
| RESPFL | Y only at primary endpoint visit | Y |
| CRIT1FL | Y only when criterion is met | Y |
| SRCDOM | Must match source domain (LB or VS) | LB |
| SRCVAR | Must match source sequence variable | LBSEQ |

### Business Rules

- **One Record Per Parameter Per Visit Per Subject**: Each USUBJID has one ADEFF record per PARAMCD per AVISITN. When multiple assessments exist at the same visit, the record closest to the scheduled date gets the analysis flag.
- **Baseline Definition**: BASE is the last non-missing AVAL with ADY <= 1 (before or on first dose date). For subjects with no pre-dose assessment, BASE is null and baseline characteristics should be noted as missing.
- **Primary Endpoint Timepoint**: The primary efficacy analysis uses ANL01FL = "Y" records. Only one record per parameter per subject can have ANL01FL = "Y" (the record closest to the protocol-scheduled primary endpoint visit).
- **Responder Analysis**: RESPFL is populated only at the primary endpoint visit (ANL01FL = "Y"). Non-responder subjects at this visit have RESPFL = "N", not null.
- **Missing Data Handling**: For subjects who discontinue before the primary endpoint visit, apply the pre-specified missing data handling strategy (e.g., LOCF, MI, or mixed model) per the SAP and estimand framework.
- **Intercurrent Events**: Rescue medication initiation, treatment discontinuation, or death are intercurrent events that should be handled according to the predefined estimand strategy (treatment policy, composite, hypothetical, or principal stratum).
- **Therapeutic Area Flexibility**: PARAMCD values are not fixed across studies. Each study defines its own PARAMCD list based on the therapeutic area and protocol-specific endpoints.

---

## Related Skills

### TrialSim ADaM Datasets
- [README.md](README.md) - ADaM skills directory
- [adsl.md](adsl.md) - ADSL (population flags, baseline dates, treatment assignments)
- [adlb.md](adlb.md) - ADLB (lab-based efficacy source data)
- [adae.md](adae.md) - ADAE (safety context for benefit-risk assessment)
- [adtte.md](adtte.md) - ADTTE (time-to-event efficacy endpoints)

### TrialSim SDTM Domains
- [../../domains/laboratory-lb.md](../../domains/laboratory-lb.md) - LB domain (lab-based efficacy parameters)
- [../../domains/vital-signs-vs.md](../../domains/vital-signs-vs.md) - VS domain (weight, blood pressure efficacy)

### TrialSim Core
- [../../clinical-trials-domain.md](../../clinical-trials-domain.md) - Clinical trial endpoint definitions
- [../../phase3-pivotal.md](../../phase3-pivotal.md) - Phase 3 efficacy endpoint patterns

### Formats
- [../../../formats/cdisc-adam.md](../../../formats/cdisc-adam.md) - ADaM format specification

> **Integration Pattern:** ADEFF merges SDTM LB and VS data with ADSL for baseline dates and population flags. The primary endpoint analysis uses ANL01FL = "Y" records at the protocol-specified visit. RESPFL is derived from AVAL at the primary endpoint visit per protocol-defined thresholds.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12 | Initial ADEFF dataset skill with diabetes, cardiovascular, and weight management efficacy parameters |
