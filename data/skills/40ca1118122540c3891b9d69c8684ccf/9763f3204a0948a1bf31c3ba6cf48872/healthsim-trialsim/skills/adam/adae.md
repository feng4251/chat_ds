---
name: adam-adae
description: |
  Generate ADaM ADAE (Adverse Event Analysis Dataset) with BDS structure, 
  treatment-emergent flags, causality grading, duration calculations, first 
  occurrence flags, and SMQ safety topic flags. Derived from SDTM AE domain 
  with full traceability. Triggers: "ADAE", "adverse event analysis", "TEAE", 
  "treatment emergent", "TRTEMFL", "AE duration", "first occurrence", 
  "causality analysis", "safety analysis dataset".
---

# Adverse Event Analysis Dataset (ADAE)

The ADAE dataset transforms SDTM AE records into an analysis-ready BDS structure with treatment-emergent event flags, duration calculations, system organ class level summaries, and safety topic flags for regulatory safety analysis.

---

## For Claude

This is a **BDS-structured ADaM dataset skill** for generating adverse event analysis data. ADAE is critical for safety analysis in all clinical trial submissions.

**Always apply this skill when you see:**
- Requests for adverse event analysis datasets
- Treatment-emergent adverse event (TEAE) derivation
- Summary of adverse events by SOC and preferred term
- Duration of adverse events in days
- First occurrence analysis by event type
- SMQ safety topic flags (e.g., hepatic, renal, cardiac)
- AE causality assessment in analysis format

**Key responsibilities:**
- Derive TRTEMFL (treatment-emergent flag) based on start date relative to treatment period
- Calculate adverse event duration (ADURN/ADURU) in days
- Assign numeric causality grades (AREL 0-4 scale)
- Flag first occurrence of each preferred term per subject (AOCCFL)
- Apply Standardised MedDRA Query (SMQ) safety topic flags
- Maintain full traceability to SDTM AE records via SRCDOM/SRCVAR/SRCSEQ

---

## ADaM Variables

### Required Variables (BDS Structure)

| Variable | Label | Type | Length | Description |
|----------|-------|------|--------|-------------|
| STUDYID | Study Identifier | Char | 20 | Unique study ID |
| USUBJID | Unique Subject Identifier | Char | 40 | From DM domain |
| PARAM | Parameter | Char | 200 | "... - ..." (SOC - PT) |
| PARAMCD | Parameter Code | Char | 8 | AEPREFIX + AEDECOD short code |
| AVAL | Analysis Value | Num | 8 | Numeric analysis value (duration, severity) |
| AVALC | Analysis Value (Character) | Char | 200 | Character analysis value (event term) |
| ADT | Analysis Date | Num | 8 | Analysis date (start date as SAS date) |
| ADY | Analysis Relative Day | Num | 8 | Study day of AE start |
| ASTDT | Analysis Start Date | Num | 8 | Start date as SAS date |
| AENDT | Analysis End Date | Num | 8 | End date as SAS date (imputed if missing) |
| TRTEMFL | Treatment Emergent Analysis Flag | Char | 1 | Y if AE onset within treatment window |

### Expected Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| AEDECOD | Dictionary-Derived Term | Char | MedDRA Preferred Term from AE |
| AEBODSYS | Body System or Organ Class | Char | MedDRA SOC from AE.AEBODSYS |
| AESEV | Severity/Intensity | Char | MILD, MODERATE, SEVERE |
| AESER | Serious Event | Char | Y, N |
| AEREL | Causality | Char | From AE.AEREL |
| AEOUT | Outcome of Adverse Event | Char | From AE.AEOUT |
| AEACN | Action Taken with Study Treatment | Char | From AE.AEACN |
| ADURN | AE Duration (N) | Num | Duration in days: AENDT - ASTDT + 1 |
| ADURU | AE Duration Units | Char | DAYS |

### Derived Analysis Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| AREL | Causality (N) | Num | 0=NOT RELATED, 1=UNLIKELY, 2=POSSIBLE, 3=PROBABLE, 4=DEFINITE |
| ASEV | Severity (N) | Num | 1=MILD, 2=MODERATE, 3=SEVERE |
| AOCCFL | First Occurrence Flag | Char | Y for first occurrence of this PT within subject |
| AOCC01FL | First Occurrence Flag 01 | Char | Y for first occurrence in treatment period |
| TRTEMFL | Treatment Emergent Flag | Char | Y if AESTDTC in [TRTSDT, TRTEDT+30d] |
| SMQ01FL | SMQ Hepatic Disorders Flag | Char | Y if AE maps to hepatic SMQ |
| SMQ02FL | SMQ Renal Disorders Flag | Char | Y if AE maps to renal SMQ |
| SMQ03FL | SMQ Cardiac Disorders Flag | Char | Y if AE maps to cardiac SMQ |

### Traceability Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| SRCDOM | Source Domain | Char | "AE" |
| SRCVAR | Source Variable | Char | "AESEQ" |
| SRCSEQ | Source Sequence Number | Num | AE.AESEQ value |

---

## Key Derivations

### ASTDT / AENDT (Analysis Dates with Imputation)

```
ASTDT = input(AESTDTC, yymmdd10.)  -- Direct copy from AE
AENDT = input(AEENDTC, yymmdd10.)  -- Direct copy if available
If AENDT is missing and AEOUT = "RECOVERED/RESOLVED":
  AENDT = ASTDT + 7 (impute as typical duration)
If AENDT is missing and AE ongoing:
  AENDT = ADSL.TRTEDT + 30 (impute as end of follow-up)
```

### ADURN / ADURU (Duration)

```
If AENDT is not missing:
  ADURN = AENDT - ASTDT + 1
  ADURU = "DAYS"
Else:
  ADURN = null
  ADURU = null
```

### TRTEMFL (Treatment-Emergent Flag)

An adverse event is treatment-emergent if the onset date falls within the treatment observation window:

```
TRTEMFL = "Y" if:
  AESTDTC >= TRTSDT (from ADSL)
  AND AESTDTC <= TRTEDT + 30 days (from ADSL)
Else:
  TRTEMFL = "N"

Note: Pre-existing conditions that worsen during treatment are also considered treatment-emergent.
```

### AREL (Causality Numeric)

Mapping from AE.AEREL character values to numeric for analysis:

```
AREL = 0 when AEREL = "NOT RELATED"
AREL = 1 when AEREL = "UNLIKELY RELATED"
AREL = 2 when AEREL = "POSSIBLY RELATED"
AREL = 3 when AEREL = "PROBABLY RELATED"
AREL = 4 when AEREL = "DEFINITELY RELATED"
```

### AOCCFL (First Occurrence Flag)

```
For each USUBJID and AEDECOD combination:
  Sort by ASTDT ascending
  The record with the earliest ASTDT gets AOCCFL = "Y"
  All other records with the same USUBJID + AEDECOD get AOCCFL = null
```

### SMQFLAGS (Standardised MedDRA Query Flags)

SMQ flags identify events belonging to specific safety topics of interest:

```
SMQ01FL = "Y" when AEDECOD is in the Hepatic Disorders SMQ (broad scope)
SMQ02FL = "Y" when AEDECOD is in the Renal Disorders SMQ (broad scope)
SMQ03FL = "Y" when AEDECOD is in the Cardiac Disorders SMQ (broad scope)
```

---

## Generation Patterns

### Standard Safety Analysis Dataset

```json
{
  "dataset": "ADAE",
  "source": {
    "domain": "AE",
    "merge_with": "ADSL"
  },
  "teae_window": {
    "start": "TRTSDT from ADSL",
    "end": "TRTEDT from ADSL + 30 days"
  },
  "duration_imputation": {
    "resolved_events": "ASTDT + 7 days if end date missing",
    "ongoing_events": "TRTEDT + 30 days as end date"
  },
  "causality_mapping": {
    "NOT RELATED": 0,
    "UNLIKELY RELATED": 1,
    "POSSIBLY RELATED": 2,
    "PROBABLY RELATED": 3,
    "DEFINITELY RELATED": 4
  },
  "smq_topics": [
    "SMQ01: Hepatic Disorders (broad)",
    "SMQ02: Renal Disorders (broad)",
    "SMQ03: Cardiac Disorders (broad)"
  ]
}
```

### Event Distribution Pattern (Checkpoint Inhibitor Example)

```json
{
  "dataset": "ADAE",
  "event_distribution": {
    "teae_rates": {
      "any_teae": 0.85,
      "grade_3_4": 0.18,
      "sae": 0.12,
      "drug_related": 0.55,
      "drug_related_grade_3_4": 0.08
    },
    "common_events": [
      { "aedecod": "Fatigue", "rate": 0.32, "grade_3_4_rate": 0.03 },
      { "aedecod": "Nausea", "rate": 0.28, "grade_3_4_rate": 0.02 },
      { "aedecod": "Rash", "rate": 0.18, "grade_3_4_rate": 0.02 },
      { "aedecod": "Diarrhoea", "rate": 0.15, "grade_3_4_rate": 0.03 },
      { "aedecod": "Pruritus", "rate": 0.12, "grade_3_4_rate": 0.01 },
      { "aedecod": "Pneumonitis", "rate": 0.04, "grade_3_4_rate": 0.02 }
    ]
  }
}
```

---

## Examples

### Example 1: Generate ADAE for Oncology Immunotherapy Trial

**Request:** "Generate ADAE for 50 subjects on a checkpoint inhibitor showing TEAE flags, first occurrence, and causality grades"

**Output:**

```json
{
  "dataset": "ADAE",
  "metadata": {
    "studyid": "IO-TRIAL-001",
    "description": "Adverse Event Analysis Dataset - Immunotherapy Trial",
    "n_subjects": 50,
    "n_records": 187
  },
  "records": [
    {
      "STUDYID": "IO-TRIAL-001",
      "USUBJID": "IO-TRIAL-001-001-0005",
      "PARAM": "Skin and subcutaneous tissue disorders - Rash pruritic",
      "PARAMCD": "AERASHP",
      "AEDECOD": "Rash pruritic",
      "AEBODSYS": "Skin and subcutaneous tissue disorders",
      "AESEV": "MODERATE",
      "ASEV": 2,
      "AESER": "N",
      "AEREL": "RELATED",
      "AREL": 4,
      "AEOUT": "RECOVERED/RESOLVED",
      "AEACN": "DRUG INTERRUPTED",
      "ASTDT": "2024-05-02",
      "AENDT": "2024-05-18",
      "ADURN": 17,
      "ADURU": "DAYS",
      "ADT": "2024-05-02",
      "ADY": 37,
      "TRTEMFL": "Y",
      "AOCCFL": "Y",
      "AOCC01FL": "Y",
      "SMQ01FL": null,
      "SMQ02FL": null,
      "SMQ03FL": null,
      "SRCDOM": "AE",
      "SRCVAR": "AESEQ",
      "SRCSEQ": 2
    },
    {
      "STUDYID": "IO-TRIAL-001",
      "USUBJID": "IO-TRIAL-001-001-0005",
      "PARAM": "General disorders and administration site conditions - Fatigue",
      "PARAMCD": "AEFATIG",
      "AEDECOD": "Fatigue",
      "AEBODSYS": "General disorders and administration site conditions",
      "AESEV": "MILD",
      "ASEV": 1,
      "AESER": "N",
      "AEREL": "POSSIBLY RELATED",
      "AREL": 2,
      "AEOUT": "NOT RECOVERED/NOT RESOLVED",
      "AEACN": "DOSE NOT CHANGED",
      "ASTDT": "2024-04-10",
      "AENDT": null,
      "ADURN": null,
      "ADURU": null,
      "ADT": "2024-04-10",
      "ADY": 15,
      "TRTEMFL": "Y",
      "AOCCFL": "Y",
      "AOCC01FL": "Y",
      "SMQ01FL": null,
      "SMQ02FL": null,
      "SMQ03FL": null,
      "SRCDOM": "AE",
      "SRCVAR": "AESEQ",
      "SRCSEQ": 1
    },
    {
      "STUDYID": "IO-TRIAL-001",
      "USUBJID": "IO-TRIAL-001-002-0012",
      "PARAM": "Respiratory, thoracic and mediastinal disorders - Pneumonitis",
      "PARAMCD": "AEPNEUM",
      "AEDECOD": "Pneumonitis",
      "AEBODSYS": "Respiratory, thoracic and mediastinal disorders",
      "AESEV": "SEVERE",
      "ASEV": 3,
      "AESER": "Y",
      "AEREL": "RELATED",
      "AREL": 4,
      "AEOUT": "RECOVERED/RESOLVED WITH SEQUELAE",
      "AEACN": "DRUG WITHDRAWN",
      "ASTDT": "2024-06-15",
      "AENDT": "2024-07-20",
      "ADURN": 36,
      "ADURU": "DAYS",
      "ADT": "2024-06-15",
      "ADY": 75,
      "TRTEMFL": "Y",
      "AOCCFL": "Y",
      "AOCC01FL": "Y",
      "SMQ01FL": "Y",
      "SMQ02FL": null,
      "SMQ03FL": null,
      "SRCDOM": "AE",
      "SRCVAR": "AESEQ",
      "SRCSEQ": 1
    },
    {
      "STUDYID": "IO-TRIAL-001",
      "USUBJID": "IO-TRIAL-001-003-0025",
      "PARAM": "Hepatobiliary disorders - Hepatitis",
      "PARAMCD": "AEHEPAT",
      "AEDECOD": "Hepatitis",
      "AEBODSYS": "Hepatobiliary disorders",
      "AESEV": "MODERATE",
      "ASEV": 2,
      "AESER": "N",
      "AEREL": "PROBABLY RELATED",
      "AREL": 3,
      "AEOUT": "RECOVERING/RESOLVING",
      "AEACN": "DRUG INTERRUPTED",
      "ASTDT": "2024-05-20",
      "AENDT": null,
      "ADURN": null,
      "ADURU": null,
      "ADT": "2024-05-20",
      "ADY": 47,
      "TRTEMFL": "Y",
      "AOCCFL": "Y",
      "AOCC01FL": "Y",
      "SMQ01FL": "Y",
      "SMQ02FL": null,
      "SMQ03FL": null,
      "SRCDOM": "AE",
      "SRCVAR": "AESEQ",
      "SRCSEQ": 1
    }
  ],
  "summary": {
    "teae_analysis": {
      "any_teae": { "n": 42, "pct": 84.0 },
      "drug_related_teae": { "n": 28, "pct": 56.0 },
      "grade_3_4_teae": { "n": 9, "pct": 18.0 },
      "serious_teae": { "n": 6, "pct": 12.0 },
      "discontinued_due_to_ae": { "n": 4, "pct": 8.0 }
    },
    "by_soc": {
      "Skin and subcutaneous tissue disorders": 42,
      "General disorders and administration site conditions": 38,
      "Gastrointestinal disorders": 32,
      "Respiratory, thoracic and mediastinal disorders": 18,
      "Hepatobiliary disorders": 10,
      "Investigations": 8
    },
    "smq_analysis": {
      "SMQ01_Hepatic_Disorders": 12,
      "SMQ02_Renal_Disorders": 5,
      "SMQ03_Cardiac_Disorders": 2
    }
  }
}
```

### Example 2: ADAE with Pre-Treatment vs Treatment-Emergent Events

**Request:** "Generate ADAE showing distinction between pre-existing conditions and treatment-emergent events"

**Output:**

```json
{
  "dataset": "ADAE",
  "metadata": {
    "studyid": "PRE-TRT-001",
    "description": "Pre-treatment vs Treatment-Emergent AE Pattern"
  },
  "records": [
    {
      "STUDYID": "PRE-TRT-001",
      "USUBJID": "PRE-TRT-001-001-0008",
      "PARAM": "Nervous system disorders - Headache",
      "PARAMCD": "AEHEADC",
      "AEDECOD": "Headache",
      "AEBODSYS": "Nervous system disorders",
      "AESEV": "MILD",
      "ASEV": 1,
      "AREL": 0,
      "ASTDT": "2024-01-05",
      "AENDT": "2024-01-06",
      "ADURN": 2,
      "ADURU": "DAYS",
      "ADT": "2024-01-05",
      "ADY": -9,
      "TRTEMFL": "N",
      "AOCCFL": "Y",
      "SRCDOM": "AE",
      "SRCVAR": "AESEQ",
      "SRCSEQ": 1,
      "notes": "Pre-treatment event: occurred before first dose on 2024-01-14"
    },
    {
      "STUDYID": "PRE-TRT-001",
      "USUBJID": "PRE-TRT-001-001-0008",
      "PARAM": "Nervous system disorders - Headache",
      "PARAMCD": "AEHEADC",
      "AEDECOD": "Headache",
      "AEBODSYS": "Nervous system disorders",
      "AESEV": "MODERATE",
      "ASEV": 2,
      "AREL": 2,
      "ASTDT": "2024-02-01",
      "AENDT": "2024-02-05",
      "ADURN": 5,
      "ADURU": "DAYS",
      "ADT": "2024-02-01",
      "ADY": 18,
      "TRTEMFL": "Y",
      "AOCCFL": null,
      "SRCDOM": "AE",
      "SRCVAR": "AESEQ",
      "SRCSEQ": 2,
      "notes": "Treatment-emergent: onset after first dose"
    }
  ],
  "summary": {
    "pre_treatment_events": 15,
    "treatment_emergent_events": 62,
    "worsened_preexisting": 3
  }
}
```

---

## Validation Rules

| Rule | Requirement | Example |
|------|-------------|---------|
| PARAMCD | Max 8 characters, unique per analysis parameter | AERASHP |
| TRTEMFL | "Y" only if ASTDT within treatment window | Y |
| AREL | Integer 0-4 with documented mapping | 3=PROBABLY RELATED |
| ADURN | Positive integer when AENDT populated | 17 |
| AOCCFL | Only one "Y" per USUBJID+AEDECOD combination | Y on earliest record |
| AOCC01FL | Only one "Y" per USUBJID+AEDECOD in treatment period | Y |
| SRCDOM | Must be "AE" for all records | AE |
| SRCVAR | Must be "AESEQ" | AESEQ |
| SRCSEQ | Must match existing AE.AESEQ value | 2 |
| SMQxxFL | "Y" only for MedDRA terms mapped to that SMQ | Y for hepatic events |

### Business Rules

- **TEAE Window**: The standard treatment-emergent window is from first dose date (TRTSDT) to last dose date + 30 days (TRTEDT + 30). This captures events with delayed onset.
- **Non-TEAE Records**: Events that start outside the TEAE window must have TRTEMFL = "N". These are included for completeness but typically excluded from TEAE summary tables.
- **Duration Imputation**: For resolved events with missing end dates, a standard imputation (e.g., 7 days) should be applied. The imputation method must be documented.
- **Causality Mapping**: The AREL numeric mapping must be consistent across the dataset. All records from the same AEREL character value must have the same AREL numeric value.
- **First Occurrence**: AOCCFL identifies the first occurrence of each preferred term within a subject. This is used for incidence tables where each subject is counted once per event type.
- **SMQ Flag Maintenance**: SMQ flags must be maintained against the current version of MedDRA and the SMQ. Terms may be added or removed between MedDRA versions.
- **Serious AEs**: Records with AESER = "Y" must have complete SAE criterion flags.

---

## Related Skills

### TrialSim ADaM Datasets
- [README.md](README.md) - ADaM skills directory
- [adsl.md](adsl.md) - ADSL (population flags, treatment dates used by ADAE)
- [adlb.md](adlb.md) - ADLB (lab abnormalities correlate with AEs)
- [adeff.md](adeff.md) - ADEFF (efficacy analysis, benefit-risk context)
- [adtte.md](adtte.md) - ADTTE (time to first AE, safety endpoints)

### TrialSim SDTM Domains
- [../../domains/adverse-events-ae.md](../../domains/adverse-events-ae.md) - AE domain (source data for ADAE)
- [../../domains/demographics-dm.md](../../domains/demographics-dm.md) - DM domain (USUBJID, treatment assignment)
- [../../domains/exposure-ex.md](../../domains/exposure-ex.md) - EX domain (dosing for TEAE window)

### Formats
- [../../../formats/cdisc-adam.md](../../../formats/cdisc-adam.md) - ADaM format specification

> **Integration Pattern:** ADAE generates analysis-ready AE records by joining AE domain data with ADSL treatment dates. The TEAE window is defined by ADSL.TRTSDT and ADSL.TRTEDT. Population flags from ADSL (SAFFL) determine which subjects are included in safety analysis summaries.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12 | Initial ADAE dataset skill with TEAE flags, SMQ safety topics, and full traceability |
