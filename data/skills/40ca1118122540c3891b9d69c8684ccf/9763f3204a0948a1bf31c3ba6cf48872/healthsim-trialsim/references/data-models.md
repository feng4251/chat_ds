---
name: data-models
description: |
  Canonical JSON Schema (draft-2020-12) definitions for all 15 TrialSim entity
  schemas: Subject, Study, Site, TreatmentArm, VisitSchedule, ActualVisit,
  Randomization, AdverseEvent, Exposure, ConcomitantMed, TrialLab,
  EfficacyAssessment, MedicalHistory, DispositionEvent, and ProtocolDeviation.
  Includes SDTM variable mappings, cross-product foreign keys, and cross-entity
  relationship diagram. Triggers: "data model", "entity schema", "JSON Schema",
  "canonical model", "cross-entity mapping".
---

# Data Models Reference

Canonical entity schemas for TrialSim clinical trial synthetic data generation. Each entity is defined as a JSON Schema (draft-2020-12) with SDTM variable mapping and cross-product integration references.

---

## For Claude

This is the **authoritative data model reference** for TrialSim. When generating synthetic clinical trial data, use these entity schemas as the canonical definitions. Map entity fields to SDTM variables as documented in each entity's mapping table.

**Always apply these schemas when:**
- Generating trial data from canonical entities
- Mapping between TrialSim entities and SDTM domains
- Validating generated data against entity constraints
- Integrating with PatientSim, NetworkSim, or PopulationSim products
- Building data pipelines that transform canonical JSON to CDISC formats

---

## Entity Overview

TrialSim defines 15 canonical entity schemas covering the full clinical trial data lifecycle:

| # | Entity | SDTM Domain | Description | Key Identifier |
|---|--------|-------------|-------------|----------------|
| 1 | **Subject** | DM | Trial participant (extends Person) | `usubjid` |
| 2 | **Study** | TS | Protocol definition | `study_id` |
| 3 | **Site** | -- | Investigational site | `site_id` |
| 4 | **TreatmentArm** | TA | Study arm definition | `armcd` |
| 5 | **VisitSchedule** | TV | Protocol visits | `visitnum` |
| 6 | **ActualVisit** | SV | Subject visit occurrence | `visitnum + usubjid` |
| 7 | **Randomization** | DM/SE | Subject randomization | `usubjid + epoch` |
| 8 | **AdverseEvent** | AE | Safety events with MedDRA | `aeseq` |
| 9 | **Exposure** | EX | Study drug dosing | `exseq` |
| 10 | **ConcomitantMed** | CM | Prior/concomitant meds with ATC | `cmseq` |
| 11 | **TrialLab** | LB | Lab results with LOINC | `lbseq` |
| 12 | **EfficacyAssessment** | RS/TR | Response assessments | `rsseq` |
| 13 | **MedicalHistory** | MH | Pre-existing conditions | `mhseq` |
| 14 | **DispositionEvent** | DS | Subject disposition | `dsseq` |
| 15 | **ProtocolDeviation** | DV | Protocol deviations | `dvseq` |

---

## 1. Subject

**SDTM Domain:** DM (Demographics)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/subject",
  "title": "Subject",
  "description": "A trial participant enrolled in a clinical study. One record per subject. Extends PatientSim Person with trial-specific identifiers and treatment assignment.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "subject_id",
    "site_id",
    "country",
    "sex",
    "age",
    "age_unit",
    "armcd",
    "arm"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Unique study identifier (STUDYID). Format: SPONSOR-STUDY-NNN."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Globally unique subject ID. Format: STUDYID-SITEID-SUBJID.",
      "pattern": "^[A-Z0-9-]+\\-[A-Z0-9]+\\-[A-Z0-9]+$"
    },
    "subject_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Subject identifier within the study (SUBJID). Numeric or alphanumeric."
    },
    "site_id": {
      "type": "string",
      "maxLength": 10,
      "description": "Investigational site identifier (SITEID)."
    },
    "country": {
      "type": "string",
      "minLength": 3,
      "maxLength": 3,
      "description": "Country of enrollment, ISO 3166-1 alpha-3 (COUNTRY).",
      "examples": ["USA", "DEU", "JPN", "GBR"]
    },
    "sex": {
      "type": "string",
      "enum": ["M", "F", "U"],
      "description": "Biological sex (SEX). CDISC CT C66731."
    },
    "race": {
      "type": "string",
      "enum": [
        "WHITE",
        "BLACK OR AFRICAN AMERICAN",
        "ASIAN",
        "AMERICAN INDIAN OR ALASKA NATIVE",
        "NATIVE HAWAIIAN OR OTHER PACIFIC ISLANDER",
        "MULTIPLE",
        "OTHER",
        "UNKNOWN",
        "NOT REPORTED"
      ],
      "description": "Race category (RACE). CDISC CT C74457."
    },
    "ethnicity": {
      "type": "string",
      "enum": [
        "HISPANIC OR LATINO",
        "NOT HISPANIC OR LATINO",
        "NOT REPORTED",
        "UNKNOWN"
      ],
      "description": "Ethnicity (ETHNIC). CDISC CT C66790."
    },
    "age": {
      "type": "integer",
      "minimum": 0,
      "maximum": 150,
      "description": "Age in age_unit at the reference start date (AGE)."
    },
    "age_unit": {
      "type": "string",
      "enum": ["YEARS", "MONTHS", "DAYS"],
      "description": "Unit for age (AGEU). Use YEARS for adults."
    },
    "birth_date": {
      "type": "string",
      "format": "date",
      "description": "Date of birth, ISO 8601 (BRTHDTC)."
    },
    "armcd": {
      "type": "string",
      "maxLength": 20,
      "description": "Planned treatment arm code (ARMCD). Must match a TreatmentArm.armcd."
    },
    "arm": {
      "type": "string",
      "maxLength": 200,
      "description": "Planned treatment arm description (ARM)."
    },
    "actual_armcd": {
      "type": "string",
      "maxLength": 20,
      "description": "Actual treatment arm code if different from planned (ACTARMCD)."
    },
    "actual_arm": {
      "type": "string",
      "maxLength": 200,
      "description": "Actual treatment arm description (ACTARM)."
    },
    "reference_start_date": {
      "type": "string",
      "format": "date",
      "description": "Reference start date/time, ISO 8601 (RFSTDTC). First study treatment date."
    },
    "reference_end_date": {
      "type": "string",
      "format": "date",
      "description": "Reference end date/time, ISO 8601 (RFENDTC). Null if subject is ongoing."
    },
    "informed_consent_date": {
      "type": "string",
      "format": "date",
      "description": "Date of informed consent signature (RFICDTC)."
    },
    "death_flag": {
      "type": "string",
      "enum": ["Y"],
      "description": "Subject death flag (DTHFL). Only present if subject died."
    },
    "death_date": {
      "type": "string",
      "format": "date",
      "description": "Date of death (DTHDTC). Required if death_flag = 'Y'."
    },
    "status": {
      "type": "string",
      "enum": ["Screened", "Randomized", "Active", "Completed", "Discontinued", "Lost to Follow-up", "Died"],
      "description": "Current subject status in the study."
    },
    "screening_date": {
      "type": "string",
      "format": "date",
      "description": "Date subject entered screening."
    },
    "randomization_date": {
      "type": "string",
      "format": "date",
      "description": "Date subject was randomized (if applicable)."
    },
    "patient_ref": {
      "type": "string",
      "description": "Cross-product reference to PatientSim Patient (e.g., MRN or Patient UUID)."
    }
  },
  "allOf": [
    {
      "if": { "properties": { "death_flag": { "const": "Y" } } },
      "then": { "required": ["death_date"] }
    }
  ]
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `study_id` | STUDYID | Direct mapping |
| `usubjid` | USUBJID | Direct mapping; global unique key |
| `subject_id` | SUBJID | Subject ID within study |
| `site_id` | SITEID | Foreign key to Site |
| `country` | COUNTRY | ISO 3166-1 alpha-3 |
| `sex` | SEX | C66731 controlled terminology |
| `race` | RACE | C74457 controlled terminology |
| `ethnicity` | ETHNIC | C66790 controlled terminology |
| `age` | AGE | Calculated at reference_start_date |
| `age_unit` | AGEU | YEARS for adults |
| `birth_date` | BRTHDTC | ISO 8601 format |
| `armcd` | ARMCD | Foreign key to TreatmentArm |
| `arm` | ARM | Arm description |
| `actual_armcd` | ACTARMCD | May differ from ARMCD |
| `actual_arm` | ACTARM | May differ from ARM |
| `reference_start_date` | RFSTDTC | ISO 8601 |
| `reference_end_date` | RFENDTC | Null if ongoing |
| `informed_consent_date` | RFICDTC | Must precede RFSTDTC |
| `death_flag` | DTHFL | "Y" or absent |
| `death_date` | DTHDTC | Required if DTHFL="Y" |

### Cross-Product Foreign Key

| Field | Target | Description |
|-------|--------|-------------|
| `patient_ref` | PatientSim `Patient.patient_id` | Links trial Subject to source patient record |

---

## 2. Study

**SDTM Domain:** TS (Trial Summary)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/study",
  "title": "Study",
  "description": "A clinical study protocol definition including trial design, treatment arms, endpoints, and regulatory identifiers.",
  "type": "object",
  "required": [
    "study_id",
    "protocol_title",
    "phase",
    "therapeutic_area",
    "indication",
    "design",
    "sponsor",
    "arms"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Unique study identifier. Format: SPONSOR-STUDY-NNN."
    },
    "protocol_title": {
      "type": "string",
      "maxLength": 500,
      "description": "Full protocol title including phase, design, and indication."
    },
    "short_title": {
      "type": "string",
      "maxLength": 100,
      "description": "Abbreviated trial name or acronym."
    },
    "phase": {
      "type": "string",
      "enum": ["Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4"],
      "description": "Clinical trial phase."
    },
    "therapeutic_area": {
      "type": "string",
      "enum": [
        "Oncology", "Cardiovascular", "CNS", "Infectious Disease",
        "Endocrinology", "Immunology", "Respiratory", "Rare Disease",
        "CGT (Cell & Gene Therapy)", "Metabolic"
      ],
      "description": "Primary therapeutic area."
    },
    "indication": {
      "type": "string",
      "maxLength": 200,
      "description": "Target disease or condition."
    },
    "design": {
      "type": "string",
      "enum": [
        "Randomized, Double-Blind, Placebo-Controlled",
        "Open-Label, Single Arm",
        "Randomized, Open-Label",
        "Randomized, Double-Blind, Active-Controlled",
        "Single-Blind",
        "Non-Randomized",
        "Crossover",
        "Factorial",
        "Dose-Escalation",
        "Basket",
        "Umbrella",
        "Platform"
      ],
      "description": "Trial design description (controlled vocabulary)."
    },
    "sponsor": {
      "type": "string",
      "maxLength": 200,
      "description": "Sponsor organization name."
    },
    "primary_endpoint": {
      "type": "string",
      "maxLength": 200,
      "description": "Primary efficacy endpoint."
    },
    "secondary_endpoints": {
      "type": "array",
      "items": { "type": "string", "maxLength": 200 },
      "description": "Secondary efficacy endpoints."
    },
    "target_enrollment": {
      "type": "integer",
      "minimum": 1,
      "maximum": 50000,
      "description": "Planned number of subjects to enroll."
    },
    "arms": {
      "type": "array",
      "minItems": 1,
      "maxItems": 10,
      "items": {
        "type": "object",
        "required": ["arm_id", "armcd", "arm", "allocation_ratio"],
        "properties": {
          "arm_id": { "type": "string" },
          "armcd": { "type": "string", "maxLength": 20 },
          "arm": { "type": "string", "maxLength": 200 },
          "allocation_ratio": { "type": "integer", "minimum": 1 }
        }
      },
      "description": "Treatment arms defined for the study."
    },
    "status": {
      "type": "string",
      "enum": ["Planned", "Recruiting", "Ongoing", "Completed", "Terminated", "Suspended"],
      "description": "Current study status."
    },
    "nct_id": {
      "type": "string",
      "pattern": "^NCT\\d{8}$",
      "description": "ClinicalTrials.gov identifier."
    },
    "eudract_id": {
      "type": "string",
      "description": "EU Clinical Trials Register EudraCT number."
    },
    "ind_number": {
      "type": "string",
      "description": "FDA Investigational New Drug (IND) application number."
    },
    "start_date": {
      "type": "string",
      "format": "date",
      "description": "Study start date (first subject first visit)."
    },
    "end_date": {
      "type": "string",
      "format": "date",
      "description": "Study end date (last subject last visit)."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `study_id` | STUDYID | Across all domains |
| `phase` | TSPARMCD = "PHASE" | TS domain |
| `design` | TSPARMCD = "DESIGN" | TS domain |
| `arms[*].armcd` | ARMCD (DM, TA) | Treatment arm code |
| `arms[*].arm` | ARM (DM, TA) | Treatment arm description |
| `nct_id` | TSPARMCD = "NCTID" | TS domain |

### Cross-Product Foreign Key

Study is the top-level entity; no upstream reference. All other entities reference Study via `study_id`.

---

## 3. Site

**SDTM Domain:** (No single SDTM domain -- referenced in DM.SITEID and other domains)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/site",
  "title": "Site",
  "description": "An investigational site where clinical trial activities are conducted. Includes site identifiers, location, investigator, and enrollment capacity.",
  "type": "object",
  "required": [
    "site_id",
    "study_id",
    "site_name",
    "country",
    "site_type",
    "principal_investigator",
    "institution"
  ],
  "properties": {
    "site_id": {
      "type": "string",
      "maxLength": 10,
      "description": "Site identifier within the study (SITEID)."
    },
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study to which this site belongs."
    },
    "site_name": {
      "type": "string",
      "maxLength": 200,
      "description": "Full site/institution name."
    },
    "country": {
      "type": "string",
      "minLength": 3,
      "maxLength": 3,
      "description": "Country, ISO 3166-1 alpha-3."
    },
    "region": {
      "type": "string",
      "maxLength": 50,
      "description": "Geographic region for stratification."
    },
    "site_type": {
      "type": "string",
      "enum": [
        "Academic Medical Center",
        "Community Hospital",
        "Private Practice",
        "Research Institute",
        "CRO Managed",
        "Government Hospital"
      ],
      "description": "Type of investigational site."
    },
    "principal_investigator": {
      "type": "object",
      "required": ["name", "credentials"],
      "properties": {
        "name": { "type": "string", "maxLength": 200 },
        "credentials": { "type": "string", "maxLength": 50 },
        "email": { "type": "string", "format": "email" },
        "phone": { "type": "string" },
        "provider_ref": {
          "type": "string",
          "description": "Cross-product reference to NetworkSim Provider."
        }
      },
      "description": "Principal investigator details."
    },
    "institution": {
      "type": "string",
      "maxLength": 200,
      "description": "Institution or hospital name."
    },
    "address": {
      "type": "object",
      "properties": {
        "street": { "type": "string" },
        "city": { "type": "string" },
        "state": { "type": "string" },
        "postal_code": { "type": "string" },
        "country": { "type": "string", "minLength": 3, "maxLength": 3 }
      }
    },
    "catchment_population": {
      "type": "integer",
      "minimum": 0,
      "description": "Estimated catchment area population for feasibility."
    },
    "planned_enrollment": {
      "type": "integer",
      "minimum": 0,
      "description": "Planned number of subjects to enroll at this site."
    },
    "actual_enrollment": {
      "type": "integer",
      "minimum": 0,
      "description": "Actual number of subjects enrolled."
    },
    "activation_date": {
      "type": "string",
      "format": "date",
      "description": "Date site was activated/initiated."
    },
    "closeout_date": {
      "type": "string",
      "format": "date",
      "description": "Date site was closed out."
    },
    "irb_name": {
      "type": "string",
      "maxLength": 200,
      "description": "IRB/EC overseeing this site."
    },
    "coordinates": {
      "type": "object",
      "properties": {
        "latitude": { "type": "number", "minimum": -90, "maximum": 90 },
        "longitude": { "type": "number", "minimum": -180, "maximum": 180 }
      },
      "description": "Geographic coordinates for site location."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `site_id` | SITEID (DM, VS, LB, etc.) | Referenced across all domains |
| `country` | COUNTRY (DM) | Site's country |
| `institution` | DM variable (free text) | Institution name |

### Cross-Product Foreign Key

| Field | Target | Description |
|-------|--------|-------------|
| `principal_investigator.provider_ref` | NetworkSim `Provider.provider_id` | Links PI to provider credentials system |

---

## 4. TreatmentArm

**SDTM Domain:** TA (Trial Arms)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/treatmentarm",
  "title": "TreatmentArm",
  "description": "Definition of a treatment arm in a clinical study including drug, dose, route, and allocation parameters.",
  "type": "object",
  "required": [
    "study_id",
    "armcd",
    "arm",
    "arm_type",
    "allocation_ratio"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "armcd": {
      "type": "string",
      "maxLength": 20,
      "description": "Arm code (short identifier, e.g., TRT, PBO, LOW)."
    },
    "arm": {
      "type": "string",
      "maxLength": 200,
      "description": "Arm full description (e.g., 'Pembrolizumab 200mg Q3W')."
    },
    "arm_type": {
      "type": "string",
      "enum": [
        "Experimental",
        "Active Comparator",
        "Placebo Comparator",
        "Sham Comparator",
        "No Intervention"
      ],
      "description": "Type of arm for regulatory classification."
    },
    "allocation_ratio": {
      "type": "integer",
      "minimum": 1,
      "description": "Ratio for randomization (e.g., 2:1 means value 2 vs 1)."
    },
    "intervention_code": {
      "type": "string",
      "maxLength": 40,
      "description": "Standardized intervention code (e.g., WHO Drug, ATC, INN)."
    },
    "dose": {
      "type": "number",
      "description": "Dose amount per administration."
    },
    "dose_unit": {
      "type": "string",
      "enum": ["mg", "mcg", "g", "mL", "IU", "mg/kg", "mg/m2"],
      "description": "Dose unit."
    },
    "dose_frequency": {
      "type": "string",
      "enum": ["QD", "BID", "TID", "QID", "Q3W", "Q4W", "ONCE", "PRN"],
      "description": "Dosing frequency. CDISC CT C66795."
    },
    "route": {
      "type": "string",
      "enum": ["ORAL", "INTRAVENOUS", "SUBCUTANEOUS", "INTRAMUSCULAR", "TOPICAL", "INHALED"],
      "description": "Route of administration. CDISC CT C66726."
    },
    "treatment_duration_weeks": {
      "type": "integer",
      "minimum": 1,
      "description": "Planned treatment duration in weeks."
    },
    "blinding": {
      "type": "string",
      "enum": ["Open", "Single-blind", "Double-blind", "Triple-blind"],
      "description": "Blinding level for this arm."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `armcd` | ARMCD (DM, TA) | Treatment arm code |
| `arm` | ARM (DM, TA) | Treatment arm description |
| `arm_type` | TAETORD (TA) | Element order / arm type |
| `dose` | EXDOSE (EX) | Dose per administration |
| `dose_unit` | EXDOSU (EX) | Dose unit |
| `dose_frequency` | EXDOSFRQ (EX) | Dosing frequency |
| `route` | EXROUTE (EX) | Route of administration |

---

## 5. VisitSchedule

**SDTM Domain:** TV (Trial Visits)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/visitschedule",
  "title": "VisitSchedule",
  "description": "Protocol-defined visit schedule specifying the timing, assessments, and windows for each study visit.",
  "type": "object",
  "required": [
    "study_id",
    "visitnum",
    "visit",
    "epoch",
    "target_day",
    "window_before_days",
    "window_after_days"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "visitnum": {
      "type": "number",
      "description": "Visit number (TVVISITNUM). Unique sequencing of visits within the study."
    },
    "visit": {
      "type": "string",
      "maxLength": 40,
      "description": "Visit name (VISIT). e.g., SCREENING, BASELINE, WEEK 4, END OF TREATMENT."
    },
    "visit_short": {
      "type": "string",
      "maxLength": 8,
      "description": "Short visit name (VISITNUM alternative)."
    },
    "epoch": {
      "type": "string",
      "enum": ["SCREENING", "BASELINE", "TREATMENT", "FOLLOW-UP", "SURVIVAL FOLLOW-UP"],
      "description": "Study epoch (EPOCH)."
    },
    "target_day": {
      "type": "integer",
      "description": "Protocol-scheduled study day for this visit (VISITDY). Day 1 = first dose."
    },
    "window_before_days": {
      "type": "integer",
      "minimum": 0,
      "description": "Acceptable window before target day."
    },
    "window_after_days": {
      "type": "integer",
      "minimum": 0,
      "description": "Acceptable window after target day."
    },
    "visit_type": {
      "type": "string",
      "enum": ["SCREENING", "BASELINE", "TREATMENT", "END OF TREATMENT", "SAFETY FOLLOW-UP", "LONG-TERM FOLLOW-UP", "UNSCHEDULED"],
      "description": "Type of visit."
    },
    "required_assessments": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "assessment_type": { "type": "string" },
          "domain": { "type": "string", "minLength": 2, "maxLength": 2 },
          "required": { "type": "boolean" }
        }
      },
      "description": "List of assessments collected at this visit."
    },
    "is_key_visit": {
      "type": "boolean",
      "description": "Whether this is a key analysis visit (e.g., primary endpoint assessment)."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `study_id` | STUDYID | All domains |
| `visitnum` | VISITNUM (all domains) | Numeric visit identifier |
| `visit` | VISIT (all domains) | Visit name |
| `epoch` | EPOCH | Study epoch |
| `target_day` | VISITDY | Planned study day of visit |

---

## 6. ActualVisit

**SDTM Domain:** SV (Subject Visits)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/actualvisit",
  "title": "ActualVisit",
  "description": "An actual subject visit occurrence recording when a subject presented at the study site, including date, visit status, and deviations from schedule.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "visitnum",
    "visit",
    "svstdtc",
    "epoch"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier, referencing Subject.usubjid."
    },
    "svseq": {
      "type": "integer",
      "minimum": 1,
      "description": "Sequence number unique within subject (SVSEQ)."
    },
    "visitnum": {
      "type": "number",
      "description": "Visit number (VISITNUM)."
    },
    "visit": {
      "type": "string",
      "maxLength": 40,
      "description": "Visit name (VISIT)."
    },
    "epoch": {
      "type": "string",
      "enum": ["SCREENING", "BASELINE", "TREATMENT", "FOLLOW-UP", "SURVIVAL FOLLOW-UP"],
      "description": "Study epoch (EPOCH)."
    },
    "svstdtc": {
      "type": "string",
      "format": "date",
      "description": "Actual date/time of visit (SVSTDTC). ISO 8601."
    },
    "svendtc": {
      "type": "string",
      "format": "date",
      "description": "End date/time of visit (SVENDTC). ISO 8601."
    },
    "svstat": {
      "type": "string",
      "enum": ["COMPLETED", "IN PROGRESS", "NOT DONE", "SWITCHED TO REMOTE"],
      "description": "Visit completion status."
    },
    "svreasnd": {
      "type": "string",
      "maxLength": 200,
      "description": "Reason visit was not done (if SVSTAT = 'NOT DONE')."
    },
    "svmode": {
      "type": "string",
      "enum": ["IN-PERSON", "REMOTE", "PHONE", "HYBRID"],
      "description": "Mode of visit conduct."
    },
    "study_day": {
      "type": "integer",
      "description": "Actual study day of visit (SVSTDY), relative to RFSTDTC."
    },
    "planned_day": {
      "type": "integer",
      "description": "Planned study day per VisitSchedule.target_day."
    },
    "deviation_days": {
      "type": "integer",
      "description": "Days deviation from planned visit day (actual - planned). Positive = late, negative = early."
    },
    "is_within_window": {
      "type": "boolean",
      "description": "Whether the visit falls within the protocol-specified window."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `study_id` | STUDYID | All domains |
| `usubjid` | USUBJID | Foreign key to DM |
| `svseq` | SVSEQ | Sequence number |
| `visitnum` | VISITNUM | Matches TV.VISITNUM |
| `visit` | VISIT | Visit name |
| `svstdtc` | SVSTDTC | Actual visit date/time |
| `svendtc` | SVENDTC | Visit end date/time |
| `svstat` | SVSTAT | Completion status |

---

## 7. Randomization

**SDTM Domain:** DM (ARMCD, ACTARMCD) / SE (Subject Elements)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/randomization",
  "title": "Randomization",
  "description": "A subject's randomization event recording the assignment to a treatment arm, including stratification factors and randomization details.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "assigned_armcd",
    "randomization_date",
    "randomization_method",
    "stratum"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier."
    },
    "assigned_armcd": {
      "type": "string",
      "maxLength": 20,
      "description": "Arm code assigned by randomization (ARMCD). Must reference TreatmentArm.armcd."
    },
    "assigned_arm": {
      "type": "string",
      "maxLength": 200,
      "description": "Arm description assigned by randomization (ARM)."
    },
    "randomization_date": {
      "type": "string",
      "format": "date",
      "description": "Date of randomization, ISO 8601."
    },
    "randomization_method": {
      "type": "string",
      "enum": ["IWRS", "Central", "Envelope", "Site Stratified"],
      "description": "Method of randomization."
    },
    "stratum": {
      "type": "object",
      "required": ["stratum_name", "stratum_value"],
      "properties": {
        "stratum_name": { "type": "string" },
        "stratum_value": { "type": "string" }
      },
      "description": "Stratification factor (e.g., disease stage, prior treatment, biomarker status)."
    },
    "randomization_number": {
      "type": "string",
      "maxLength": 20,
      "description": "Randomization number from IWRS/IRT system."
    },
    "epoch": {
      "type": "string",
      "enum": ["SCREENING", "RANDOMIZATION", "TREATMENT"],
      "description": "Epoch associated with this randomization (SE domain)."
    },
    "element": {
      "type": "string",
      "maxLength": 40,
      "description": "Study element label (SE domain)."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `usubjid` | USUBJID (DM) | Subject identifier |
| `assigned_armcd` | ARMCD (DM) | Treatment arm code |
| `assigned_arm` | ARM (DM) | Treatment arm description |
| `randomization_date` | RFSTDTC (DM) | Key reference date |
| `element` | ELEMENT (SE) | Subject element |
| `epoch` | EPOCH (SE) | Epoch association |
| `randomization_number` | RFICDTC / comment | IRT reference |

---

## 8. AdverseEvent

**SDTM Domain:** AE (Adverse Events)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/adverseevent",
  "title": "AdverseEvent",
  "description": "An adverse event experienced by a subject during the clinical trial, coded with MedDRA terminology and graded for severity, seriousness, and causality.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "aeseq",
    "aeterm",
    "aedecod",
    "aestdtc"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier."
    },
    "aeseq": {
      "type": "integer",
      "minimum": 1,
      "description": "Sequence number unique within subject (AESEQ)."
    },
    "aeterm": {
      "type": "string",
      "maxLength": 200,
      "description": "Verbatim term as reported by investigator (AETERM)."
    },
    "aedecod": {
      "type": "string",
      "maxLength": 200,
      "description": "MedDRA Preferred Term (AEDECOD). Dictionary-derived standardized term."
    },
    "aebodsys": {
      "type": "string",
      "maxLength": 200,
      "description": "MedDRA System Organ Class (AEBODSYS)."
    },
    "aesoc": {
      "type": "string",
      "maxLength": 40,
      "description": "Primary SOC code (AESOC). MedDRA SOC code."
    },
    "aehlt": {
      "type": "string",
      "maxLength": 200,
      "description": "MedDRA High Level Term (AEHLT)."
    },
    "aehlgt": {
      "type": "string",
      "maxLength": 200,
      "description": "MedDRA High Level Group Term (AEHLGT)."
    },
    "aellt": {
      "type": "string",
      "maxLength": 200,
      "description": "MedDRA Lowest Level Term (AELLT)."
    },
    "aesev": {
      "type": "string",
      "enum": ["MILD", "MODERATE", "SEVERE"],
      "description": "Severity/Intensity (AESEV). CDISC CT C66769."
    },
    "aeser": {
      "type": "string",
      "enum": ["Y", "N"],
      "description": "Serious event flag (AESER). CDISC CT C66742."
    },
    "aeshosp": {
      "type": "string",
      "enum": ["Y", "N"],
      "description": "Required or prolonged hospitalization (yes/no)."
    },
    "aerel": {
      "type": "string",
      "enum": ["NOT RELATED", "POSSIBLY RELATED", "RELATED", "PROBABLY RELATED", "DEFINITELY RELATED"],
      "description": "Causality assessment (AEREL). CDISC CT C66732."
    },
    "aeacn": {
      "type": "string",
      "enum": [
        "NONE",
        "DOSE INCREASED",
        "DOSE REDUCED",
        "DRUG INTERRUPTED",
        "DRUG WITHDRAWN",
        "NOT APPLICABLE",
        "UNKNOWN"
      ],
      "description": "Action taken with study treatment (AEACN). CDISC CT C66726."
    },
    "aeout": {
      "type": "string",
      "enum": [
        "RECOVERED/RESOLVED",
        "RECOVERING/RESOLVING",
        "NOT RECOVERED/NOT RESOLVED",
        "RECOVERED/RESOLVED WITH SEQUELAE",
        "FATAL",
        "UNKNOWN"
      ],
      "description": "Outcome of adverse event (AEOUT). CDISC CT C66728."
    },
    "aetoxgr": {
      "type": "string",
      "enum": ["1", "2", "3", "4", "5"],
      "description": "CTCAE toxicity grade (AETOXGR). CDISC CT C68802."
    },
    "astdtc": {
      "type": "string",
      "format": "date",
      "description": "Start date/time of AE (AESTDTC). ISO 8601."
    },
    "aeendtc": {
      "type": "string",
      "format": "date",
      "description": "End date/time of AE (AEENDTC). ISO 8601."
    },
    "aedur": {
      "type": "string",
      "description": "Duration of AE in ISO 8601 duration format."
    },
    "aeongo": {
      "type": "string",
      "enum": ["Y"],
      "description": "Ongoing flag. Present if AE is still ongoing at data cutoff."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `usubjid` | USUBJID | Foreign key to DM |
| `aeseq` | AESEQ | Sequence within subject |
| `aeterm` | AETERM | Verbatim term |
| `aedecod` | AEDECOD | MedDRA Preferred Term |
| `aebodsys` | AEBODSYS | MedDRA SOC |
| `aesoc` | AESOC | SOC code |
| `aehlt` | AEHLT | MedDRA HLT |
| `aehlgt` | AEHLGT | MedDRA HLGT |
| `aellt` | AELLT | MedDRA LLT |
| `aesev` | AESEV | Severity |
| `aeser` | AESER | Seriousness |
| `aerel` | AEREL | Causality |
| `aeacn` | AEACN | Action taken |
| `aeout` | AEOUT | Outcome |
| `aetoxgr` | AETOXGR | CTCAE grade |
| `astdtc` | AESTDTC | Start date/time |
| `aeendtc` | AEENDTC | End date/time |

---

## 9. Exposure

**SDTM Domain:** EX (Exposure)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/exposure",
  "title": "Exposure",
  "description": "Record of study drug administration to a subject including dose, route, timing, and any modifications or interruptions.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "exseq",
    "extrt",
    "exdose",
    "exdosu",
    "exroute",
    "exstdtc"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier."
    },
    "exseq": {
      "type": "integer",
      "minimum": 1,
      "description": "Sequence number unique within subject (EXSEQ)."
    },
    "extrt": {
      "type": "string",
      "maxLength": 200,
      "description": "Name of study treatment administered (EXTRT)."
    },
    "extrtcd": {
      "type": "string",
      "maxLength": 40,
      "description": "Treatment code (EXTRTCD). Sponsor-defined."
    },
    "exdose": {
      "type": "number",
      "description": "Dose amount administered (EXDOSE)."
    },
    "exdosu": {
      "type": "string",
      "enum": ["mg", "mcg", "g", "mL", "IU", "mg/kg", "mg/m2"],
      "description": "Dose units (EXDOSU). CDISC CT C66789."
    },
    "exdosfrm": {
      "type": "string",
      "enum": ["TABLET", "CAPSULE", "INJECTION", "INFUSION", "ORAL SOLUTION", "TOPICAL"],
      "description": "Dose form (EXDOSFRM). CDISC CT C66726."
    },
    "exroute": {
      "type": "string",
      "enum": ["ORAL", "INTRAVENOUS", "SUBCUTANEOUS", "INTRAMUSCULAR", "TOPICAL"],
      "description": "Route of administration (EXROUTE). CDISC CT C66726."
    },
    "exdosfrq": {
      "type": "string",
      "enum": ["QD", "BID", "TID", "QID", "Q3W", "Q4W", "ONCE", "PRN", "UNKNOWN"],
      "description": "Dosing frequency (EXDOSFRQ). CDISC CT C66795."
    },
    "exstdtc": {
      "type": "string",
      "format": "date",
      "description": "Start date/time of administration (EXSTDTC). ISO 8601."
    },
    "exendtc": {
      "type": "string",
      "format": "date",
      "description": "End date/time of administration (EXENDTC). ISO 8601."
    },
    "epoch": {
      "type": "string",
      "enum": ["SCREENING", "TREATMENT", "FOLLOW-UP"],
      "description": "Study epoch (EPOCH)."
    },
    "exlot": {
      "type": "string",
      "maxLength": 50,
      "description": "Drug lot/batch number (EXLOT)."
    },
    "exrate": {
      "type": "number",
      "description": "Infusion rate for IV administration (EXRATE)."
    },
    "exrateu": {
      "type": "string",
      "enum": ["mL/hr", "mg/hr", "mcg/min"],
      "description": "Rate units (EXRATEU)."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `usubjid` | USUBJID | Foreign key to DM |
| `exseq` | EXSEQ | Sequence within subject |
| `extrt` | EXTRT | Treatment name |
| `exdose` | EXDOSE | Dose amount |
| `exdosu` | EXDOSU | Dose unit |
| `exdosfrm` | EXDOSFRM | Dose form |
| `exroute` | EXROUTE | Route |
| `exdosfrq` | EXDOSFRQ | Frequency |
| `exstdtc` | EXSTDTC | Start date/time |
| `exendtc` | EXENDTC | End date/time |
| `exlot` | EXLOT | Lot number |
| `exrate` | EXRATE | Infusion rate |
| `exrateu` | EXRATEU | Rate unit |

---

## 10. ConcomitantMed

**SDTM Domain:** CM (Concomitant/Prior Medications)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/concomitantmed",
  "title": "ConcomitantMed",
  "description": "A non-study medication taken by the subject prior to or during the trial, coded with WHO Drug Dictionary and ATC classification.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "cmseq",
    "cmtrt",
    "cmdecod",
    "cmcat"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier."
    },
    "cmseq": {
      "type": "integer",
      "minimum": 1,
      "description": "Sequence number unique within subject (CMSEQ)."
    },
    "cmtrt": {
      "type": "string",
      "maxLength": 200,
      "description": "Reported (verbatim) medication name (CMTRT)."
    },
    "cmdecod": {
      "type": "string",
      "maxLength": 200,
      "description": "Standardized medication name from WHO Drug Global (CMDECOD)."
    },
    "cmcat": {
      "type": "string",
      "enum": ["PRIOR", "CONCOMITANT", "RESCUE", "PROPHYLACTIC"],
      "description": "Medication category relative to study (CMCAT)."
    },
    "cmindc": {
      "type": "string",
      "maxLength": 200,
      "description": "Indication for medication (CMINDC). MedDRA or ICD coded."
    },
    "cmdose": {
      "type": "number",
      "description": "Dose amount (CMDOSE)."
    },
    "cmdosu": {
      "type": "string",
      "enum": ["mg", "mcg", "g", "mL", "IU", "tsp", "tbsp"],
      "description": "Dose units (CMDOSU)."
    },
    "cmdosfrq": {
      "type": "string",
      "enum": ["QD", "BID", "TID", "QID", "QOD", "PRN", "ONCE", "UNKNOWN"],
      "description": "Dosing frequency (CMDOSFRQ). CDISC CT C66795."
    },
    "cmroute": {
      "type": "string",
      "enum": ["ORAL", "INTRAVENOUS", "SUBCUTANEOUS", "INTRAMUSCULAR", "TOPICAL", "INHALED", "OPHTHALMIC", "OTIC", "RECTAL", "VAGINAL"],
      "description": "Route of administration (CMROUTE). CDISC CT C66726."
    },
    "cmstdtc": {
      "type": "string",
      "format": "date",
      "description": "Start date/time of medication (CMSTDTC). ISO 8601."
    },
    "cmendtc": {
      "type": "string",
      "format": "date",
      "description": "End date/time of medication (CMENDTC). ISO 8601."
    },
    "cmongo": {
      "type": "string",
      "enum": ["Y"],
      "description": "Ongoing flag. Present if medication is still being taken."
    },
    "cmatc": {
      "type": "string",
      "maxLength": 7,
      "pattern": "^[A-Z][0-9]{2}[A-Z]{2}[0-9]{2}$",
      "description": "WHO ATC classification code (ATC Level 5)."
    },
    "cmatclevel1": {
      "type": "string",
      "maxLength": 200,
      "description": "ATC Level 1 anatomical main group."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `usubjid` | USUBJID | Foreign key to DM |
| `cmseq` | CMSEQ | Sequence within subject |
| `cmtrt` | CMTRT | Reported medication name |
| `cmdecod` | CMDECOD | WHO Drug preferred name |
| `cmcat` | CMCAT | PRIOR or CONCOMITANT |
| `cmindc` | CMINDC | Indication |
| `cmdose` | CMDOSE | Dose amount |
| `cmdosu` | CMDOSU | Dose unit |
| `cmdosfrq` | CMDOSFRQ | Frequency |
| `cmroute` | CMROUTE | Route |
| `cmstdtc` | CMSTDTC | Start date |
| `cmendtc` | CMENDTC | End date |
| `cmatc` | (CM domain qualifier) | ATC code |

---

## 11. TrialLab

**SDTM Domain:** LB (Laboratory Test Results)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/triallab",
  "title": "TrialLab",
  "description": "A clinical laboratory test result for a subject at a specific time point, coded with LOINC and including reference ranges and abnormality flags.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "lbseq",
    "lbtestcd",
    "lbtest",
    "lborres",
    "lbstdtc"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier."
    },
    "lbseq": {
      "type": "integer",
      "minimum": 1,
      "description": "Sequence number unique within subject (LBSEQ)."
    },
    "lbtestcd": {
      "type": "string",
      "maxLength": 8,
      "description": "Lab test code (LBTESTCD). Sponsor-defined or LOINC-derived short name."
    },
    "lbtest": {
      "type": "string",
      "maxLength": 40,
      "description": "Full lab test name (LBTEST)."
    },
    "lbcat": {
      "type": "string",
      "enum": ["CHEMISTRY", "HEMATOLOGY", "URINALYSIS", "COAGULATION", "IMMUNOLOGY", "MICROBIOLOGY", "ENDOCRINE", "CARDIAC", "ONCOLOGY", "GENETICS"],
      "description": "Lab test category (LBCAT)."
    },
    "lborres": {
      "type": "string",
      "maxLength": 200,
      "description": "Result in original units (LBORRES). String to accommodate text results."
    },
    "lborresu": {
      "type": "string",
      "maxLength": 20,
      "description": "Original result units (LBORRESU)."
    },
    "lbstresc": {
      "type": "string",
      "maxLength": 200,
      "description": "Standardized character result (LBSTRESC)."
    },
    "lbstresn": {
      "type": "number",
      "description": "Standardized numeric result (LBSTRESN)."
    },
    "lbstresu": {
      "type": "string",
      "maxLength": 20,
      "description": "Standard result units (LBSTRESU). SI units preferred."
    },
    "lbstnrlo": {
      "type": "number",
      "description": "Reference range lower limit (LBSTNRLO)."
    },
    "lbstnrhi": {
      "type": "number",
      "description": "Reference range upper limit (LBSTNRHI)."
    },
    "lbnrind": {
      "type": "string",
      "enum": ["NORMAL", "LOW", "HIGH", "ABNORMAL", "N/A"],
      "description": "Reference range indicator (LBNRIND). CDISC CT C66747."
    },
    "lbstat": {
      "type": "string",
      "enum": ["NOT DONE"],
      "description": "Completion status (LBSTAT). Only present when test was not done."
    },
    "lbreasnd": {
      "type": "string",
      "maxLength": 200,
      "description": "Reason not done (LBREASND). Required if LBSTAT = 'NOT DONE'."
    },
    "lbstdtc": {
      "type": "string",
      "format": "date",
      "description": "Specimen collection date/time (LBSTDTC). ISO 8601."
    },
    "lbendtc": {
      "type": "string",
      "format": "date",
      "description": "End date/time of specimen collection (LBENDTC)."
    },
    "lbmethod": {
      "type": "string",
      "maxLength": 200,
      "description": "Method of testing (LBMETHOD)."
    },
    "lbloinc": {
      "type": "string",
      "maxLength": 20,
      "description": "LOINC code for the test (LBLOINC). e.g., '1742-6' for ALT."
    },
    "lbblfl": {
      "type": "string",
      "enum": ["Y"],
      "description": "Baseline flag (LBBLFL). Y for baseline record."
    },
    "lbtoxgr": {
      "type": "string",
      "enum": ["0", "1", "2", "3", "4"],
      "description": "CTCAE toxicity grade (LBTOXGR). CDISC CT C68802."
    },
    "visitnum": {
      "type": "number",
      "description": "Visit number (VISITNUM)."
    },
    "visit": {
      "type": "string",
      "maxLength": 40,
      "description": "Visit name (VISIT)."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `usubjid` | USUBJID | Foreign key to DM |
| `lbseq` | LBSEQ | Sequence within subject |
| `lbtestcd` | LBTESTCD | Test code |
| `lbtest` | LBTEST | Test name |
| `lbcat` | LBCAT | Test category |
| `lborres` | LBORRES | Original result |
| `lborresu` | LBORRESU | Original units |
| `lbstresc` | LBSTRESC | Standardized result (char) |
| `lbstresn` | LBSTRESN | Standardized result (num) |
| `lbstresu` | LBSTRESU | Standardized units |
| `lbstnrlo` | LBSTNRLO | Lower reference limit |
| `lbstnrhi` | LBSTNRHI | Upper reference limit |
| `lbnrind` | LBNRIND | Normal range indicator |
| `lbstdtc` | LBSTDTC | Collection date/time |
| `lbloinc` | LBLOINC | LOINC code |

---

## 12. EfficacyAssessment

**SDTM Domain:** RS (Disease Response) / TR (Tumor Results)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/efficacyassessment",
  "title": "EfficacyAssessment",
  "description": "An efficacy endpoint assessment for a subject, including response criteria (RECIST, iRECIST, RANO, etc.), measurement data, and derived response categories.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "rsseq",
    "rstestcd",
    "rstest",
    "rsorres",
    "rsdtc",
    "visitnum"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier."
    },
    "rsseq": {
      "type": "integer",
      "minimum": 1,
      "description": "Sequence number unique within subject (RSSEQ)."
    },
    "rstestcd": {
      "type": "string",
      "maxLength": 8,
      "description": "Response assessment test code (RSTESTCD). e.g., OVRRESP, TUMDIM."
    },
    "rstest": {
      "type": "string",
      "maxLength": 40,
      "description": "Response assessment test name (RSTEST). e.g., Overall Response."
    },
    "rscat": {
      "type": "string",
      "maxLength": 40,
      "description": "Response category (RSCAT). e.g., RECIST 1.1, iRECIST."
    },
    "rsorres": {
      "type": "string",
      "maxLength": 200,
      "description": "Original response result (RSORRES)."
    },
    "rsstresc": {
      "type": "string",
      "enum": [
        "CR", "PR", "SD", "PD", "NE", "NOT DONE",
        "iCR", "iPR", "iSD", "iUPD", "iCPD",
        "MCR", "MR", "VGPR", "NM",
        "Responder", "Non-Responder"
      ],
      "description": "Standardized response category (RSSTRESC)."
    },
    "rsdtc": {
      "type": "string",
      "format": "date",
      "description": "Date of assessment (RSDTC). ISO 8601."
    },
    "rsstat": {
      "type": "string",
      "enum": ["NOT DONE"],
      "description": "Completion status. Only if assessment was not done."
    },
    "visitnum": {
      "type": "number",
      "description": "Visit number (VISITNUM)."
    },
    "visit": {
      "type": "string",
      "maxLength": 40,
      "description": "Visit name (VISIT)."
    },
    "epoch": {
      "type": "string",
      "enum": ["BASELINE", "TREATMENT", "END OF TREATMENT", "FOLLOW-UP"],
      "description": "Study epoch (EPOCH)."
    },
    "is_baseline": {
      "type": "boolean",
      "description": "Whether this is the baseline assessment."
    },
    "is_best_response": {
      "type": "boolean",
      "description": "Whether this assessment represents the subject's best overall response."
    },
    "is_confirmed": {
      "type": "boolean",
      "description": "Whether response has been confirmed per protocol (e.g., CR confirmed at least 4 weeks later)."
    },
    "target_lesion_sum_diameter": {
      "type": "number",
      "description": "Sum of diameters of target lesions (mm) for RECIST assessment."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `usubjid` | USUBJID | Foreign key to DM |
| `rsseq` | RSSEQ | Sequence within subject |
| `rstestcd` | RSTESTCD | Test code |
| `rstest` | RSTEST | Test name |
| `rscat` | RSCAT | Response category (e.g., RECIST 1.1) |
| `rsorres` | RSORRES | Original response |
| `rsstresc` | RSSTRESC | Standardized response |
| `rsdtc` | RSDTC | Assessment date |
| `target_lesion_sum_diameter` | TRSTRESN (TR domain) | Tumor measurement detail in TR |

---

## 13. MedicalHistory

**SDTM Domain:** MH (Medical History)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/medicalhistory",
  "title": "MedicalHistory",
  "description": "A pre-existing medical condition or surgical procedure for a subject, documented at baseline and coded with MedDRA terminology. Used for eligibility assessment and baseline characterization.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "mhseq",
    "mhterm",
    "mhdecod",
    "mhcat"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier."
    },
    "mhseq": {
      "type": "integer",
      "minimum": 1,
      "description": "Sequence number unique within subject (MHSEQ)."
    },
    "mhterm": {
      "type": "string",
      "maxLength": 200,
      "description": "Reported term for the condition (MHTERM). Verbatim as reported."
    },
    "mhdecod": {
      "type": "string",
      "maxLength": 200,
      "description": "Dictionary-derived term (MHDECOD). MedDRA Preferred Term."
    },
    "mhcat": {
      "type": "string",
      "enum": [
        "CARDIOVASCULAR", "RESPIRATORY", "ENDOCRINE", "GASTROINTESTINAL",
        "GENITOURINARY", "NEUROLOGIC", "PSYCHIATRIC",
        "MUSCULOSKELETAL", "DERMATOLOGIC", "HEMATOLOGIC",
        "ONCOLOGIC", "IMMUNOLOGIC", "INFECTIOUS",
        "SURGICAL HISTORY", "ALLERGY", "GENERAL MEDICAL HISTORY"
      ],
      "description": "Category for the medical history condition (MHCAT)."
    },
    "mhbodsys": {
      "type": "string",
      "maxLength": 200,
      "description": "Body system/organ class (MHBODSYS). MedDRA SOC."
    },
    "mhseve": {
      "type": "string",
      "enum": ["MILD", "MODERATE", "SEVERE"],
      "description": "Severity (MHSEV). CDISC CT C66769."
    },
    "mhstdtc": {
      "type": "string",
      "format": "date",
      "description": "Start/onset date of the condition (MHSTDTC). ISO 8601."
    },
    "mhendtc": {
      "type": "string",
      "format": "date",
      "description": "End/resolution date of the condition (MHENDTC). ISO 8601."
    },
    "mhongo": {
      "type": "string",
      "enum": ["Y"],
      "description": "Ongoing flag (MHONGO). Y if condition is still active at baseline."
    },
    "mhpresp": {
      "type": "string",
      "enum": ["Y", "N"],
      "description": "Pre-specified condition (MHPRESP). Y if pre-specified on CRF."
    },
    "mhoccur": {
      "type": "string",
      "enum": ["Y", "N"],
      "description": "Occurrence (MHOCCUR). Y if condition occurred."
    },
    "mhllt": {
      "type": "string",
      "maxLength": 200,
      "description": "MedDRA Lowest Level Term (MHLLT)."
    },
    "mhhlt": {
      "type": "string",
      "maxLength": 200,
      "description": "MedDRA High Level Term (MHHLT)."
    },
    "mhhlgt": {
      "type": "string",
      "maxLength": 200,
      "description": "MedDRA High Level Group Term (MHHLGT)."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `usubjid` | USUBJID | Foreign key to DM |
| `mhseq` | MHSEQ | Sequence within subject |
| `mhterm` | MHTERM | Reported condition term |
| `mhdecod` | MHDECOD | MedDRA Preferred Term |
| `mhcat` | MHCAT | Condition category |
| `mhbodsys` | MHBODSYS | MedDRA SOC |
| `mhseve` | MHSEV | Severity |
| `mhstdtc` | MHSTDTC | Onset date |
| `mhendtc` | MHENDTC | Resolution date |
| `mhpresp` | MHPRESP | Pre-specified flag |
| `mhoccur` | MHOCCUR | Occurrence flag |

---

## 14. DispositionEvent

**SDTM Domain:** DS (Disposition)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/dispositionevent",
  "title": "DispositionEvent",
  "description": "A protocol milestone or subject status event documenting the subject's journey through the study, including informed consent, randomization, treatment completion, and study discontinuation.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "dsseq",
    "dsterm",
    "dsdecod",
    "dscat",
    "dsstdtc"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier."
    },
    "dsseq": {
      "type": "integer",
      "minimum": 1,
      "description": "Sequence number unique within subject (DSSEQ)."
    },
    "dsterm": {
      "type": "string",
      "maxLength": 200,
      "description": "Reported disposition term (DSTERM)."
    },
    "dsdecod": {
      "type": "string",
      "maxLength": 200,
      "enum": [
        "COMPLETED",
        "ADVERSE EVENT",
        "DEATH",
        "LOST TO FOLLOW-UP",
        "WITHDRAWAL BY SUBJECT",
        "PROTOCOL DEVIATION",
        "LACK OF EFFICACY",
        "PHYSICIAN DECISION",
        "PREGNANCY",
        "STUDY TERMINATED BY SPONSOR",
        "NON-COMPLIANCE",
        "SCREEN FAILURE",
        "INFORMED CONSENT OBTAINED",
        "RANDOMIZED",
        "TREATMENT STARTED",
        "TREATMENT COMPLETED",
        "TREATMENT DISCONTINUED"
      ],
      "description": "Standardized disposition term (DSDECOD). CDISC CT C66728."
    },
    "dscat": {
      "type": "string",
      "enum": ["DISPOSITION EVENT", "PROTOCOL MILESTONE"],
      "description": "Category for disposition (DSCAT)."
    },
    "dsscat": {
      "type": "string",
      "enum": ["STUDY", "TREATMENT"],
      "description": "Subcategory for disposition (DSSCAT)."
    },
    "dsstdtc": {
      "type": "string",
      "format": "date",
      "description": "Date of disposition event (DSSTDTC). ISO 8601."
    },
    "epoch": {
      "type": "string",
      "enum": ["SCREENING", "TREATMENT", "FOLLOW-UP"],
      "description": "Study epoch (EPOCH) when the event occurred."
    },
    "dsdy": {
      "type": "integer",
      "description": "Study day of disposition (DSDY), relative to RFSTDTC."
    },
    "visitnum": {
      "type": "number",
      "description": "Visit number at which disposition occurred."
    },
    "visit": {
      "type": "string",
      "maxLength": 40,
      "description": "Visit name (VISIT)."
    },
    "dsreasnd": {
      "type": "string",
      "maxLength": 200,
      "description": "Reason for disposition (DSREASND). Free text elaboration."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `usubjid` | USUBJID | Foreign key to DM |
| `dsseq` | DSSEQ | Sequence within subject |
| `dsterm` | DSTERM | Reported term |
| `dsdecod` | DSDECOD | Standardized term |
| `dscat` | DSCAT | DISPOSITION EVENT or PROTOCOL MILESTONE |
| `dsscat` | DSSCAT | STUDY or TREATMENT |
| `dsstdtc` | DSSTDTC | Date of event |
| `dsdy` | DSDY | Study day |

---

## 15. ProtocolDeviation

**SDTM Domain:** DV (Protocol Deviations)

### JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "trialsim://schemas/protocoldeviation",
  "title": "ProtocolDeviation",
  "description": "A documented departure from the approved protocol that occurred during the study, including classification, impact assessment, and corrective actions.",
  "type": "object",
  "required": [
    "study_id",
    "usubjid",
    "dvseq",
    "dvterm",
    "dvdecod",
    "dvcat",
    "dvstdtc"
  ],
  "properties": {
    "study_id": {
      "type": "string",
      "maxLength": 20,
      "description": "Study identifier."
    },
    "usubjid": {
      "type": "string",
      "maxLength": 40,
      "description": "Unique subject identifier."
    },
    "dvseq": {
      "type": "integer",
      "minimum": 1,
      "description": "Sequence number unique within subject (DVSEQ)."
    },
    "dvterm": {
      "type": "string",
      "maxLength": 200,
      "description": "Reported deviation term (DVTERM). Description as reported."
    },
    "dvdecod": {
      "type": "string",
      "maxLength": 200,
      "description": "Standardized deviation term (DVDECOD)."
    },
    "dvcat": {
      "type": "string",
      "enum": [
        "INCLUSION/EXCLUSION",
        "VISIT SCHEDULE",
        "STUDY TREATMENT",
        "ASSESSMENT",
        "INFORMED CONSENT",
        "SAFETY REPORTING",
        "GCP/REGULATORY",
        "OTHER"
      ],
      "description": "Category of deviation (DVCAT)."
    },
    "dvscat": {
      "type": "string",
      "enum": ["MINOR", "MAJOR", "CRITICAL"],
      "description": "Severity/classification of deviation (DVSCAT)."
    },
    "dvstdtc": {
      "type": "string",
      "format": "date",
      "description": "Date of deviation (DVSTDTC). ISO 8601."
    },
    "dvisimp": {
      "type": "string",
      "enum": ["N", "Y"],
      "description": "Important deviation flag (DVISIMP). Impacts data integrity or subject safety."
    },
    "dvactions": {
      "type": "string",
      "maxLength": 500,
      "description": "Actions taken to address the deviation."
    },
    "dvcaus": {
      "type": "string",
      "maxLength": 200,
      "description": "Root cause or contributing factors."
    },
    "dvisprevented": {
      "type": "boolean",
      "description": "Whether the deviation was preventable."
    },
    "epoch": {
      "type": "string",
      "enum": ["SCREENING", "TREATMENT", "FOLLOW-UP"],
      "description": "Study epoch when deviation occurred."
    },
    "visitnum": {
      "type": "number",
      "description": "Visit number associated with deviation."
    },
    "visit": {
      "type": "string",
      "maxLength": 40,
      "description": "Visit name (VISIT)."
    },
    "protocol_section": {
      "type": "string",
      "maxLength": 200,
      "description": "Referenced protocol section that was deviated from."
    }
  }
}
```

### SDTM Variable Mapping

| Entity Field | SDTM Variable | Notes |
|-------------|---------------|-------|
| `usubjid` | USUBJID | Foreign key to DM |
| `dvseq` | DVSEQ | Sequence within subject |
| `dvterm` | DVTERM | Reported term |
| `dvdecod` | DVDECOD | Standardized term |
| `dvcat` | DVCAT | Deviation category |
| `dvscat` | DVSCAT | Minor/Major/Critical |
| `dvstdtc` | DVSTDTC | Date of deviation |
| `dvisimp` | DVISIMP | Important deviation flag |

---

## Cross-Entity Relationship Diagram

```
                          ┌──────────────┐
                          │    Study     │
                          │   (TS/TA)    │
                          └──────┬───────┘
                                 │ study_id
            ┌────────────────────┼────────────────────┐
            │                    │                    │
     ┌──────▼──────┐     ┌──────▼──────┐      ┌──────▼──────┐
     │TreatmentArm │     │    Site     │      │VisitSchedule│
     │    (TA)     │     │             │      │    (TV)     │
     └──────┬──────┘     └──────┬──────┘      └──────┬──────┘
            │ armcd             │ site_id            │ visitnum
            │                   │                    │
            │            ┌──────▼──────────────┐     │
            └────────────┤                    │     │
                    ┌────┤     Subject (DM)   ├─────┘
                    │    │                    │
                    │    └────┬────┬────┬─────┘
                    │         │    │    │ usubjid
                    │    ┌────┘    │    └──────────────────┐
                    │    │         │                       │
     ┌──────────────┐    │    ┌────▼────────┐    ┌─────────▼────────┐
     │Randomization │    │    │ActualVisit  │    │DispositionEvent  │
     │   (DM/SE)    │    │    │    (SV)     │    │      (DS)        │
     └──────────────┘    │    └─────────────┘    └──────────────────┘
                         │
          ┌──────────────┼──────────────┬─────────────────┬──────────────┐
          │              │              │                 │              │
    ┌─────▼─────┐ ┌──────▼──────┐ ┌─────▼─────┐ ┌───────▼──────┐ ┌─────▼─────┐
    │AdverseEvnt│ │ConcomitMed │ │  TrialLab │ │   Exposure   │ │MedHistory │
    │   (AE)    │ │    (CM)    │ │   (LB)    │ │     (EX)     │ │   (MH)    │
    └───────────┘ └────────────┘ └───────────┘ └──────────────┘ └───────────┘
                         │
                ┌────────▼────────┐      ┌─────────────────┐
                │EfficacyAssmnt │      │ProtocolDeviation│
                │   (RS/TR)     │      │      (DV)       │
                └───────────────┘      └─────────────────┘
```

**Key Foreign Key Relationships:**

| Child Entity | Parent Entity | Join Key(s) |
|-------------|---------------|-------------|
| Site | Study | `study_id` |
| TreatmentArm | Study | `study_id` |
| VisitSchedule | Study | `study_id` |
| Subject | Study, Site, TreatmentArm | `study_id`, `site_id`, `armcd` |
| Randomization | Subject, TreatmentArm | `usubjid`, `assigned_armcd` = `armcd` |
| ActualVisit | Subject, VisitSchedule | `usubjid`, `visitnum` |
| AdverseEvent | Subject | `usubjid` |
| ConcomitantMed | Subject | `usubjid` |
| TrialLab | Subject | `usubjid` |
| Exposure | Subject | `usubjid` |
| MedicalHistory | Subject | `usubjid` |
| EfficacyAssessment | Subject | `usubjid` |
| DispositionEvent | Subject | `usubjid` |
| ProtocolDeviation | Subject | `usubjid` |

---

## Cross-Product Integration Mappings

### PatientSim Patient to TrialSim Subject

TrialSim subjects extend PatientSim patients with trial-specific data. The integration mapping:

| PatientSim Field | TrialSim Subject Field | Transformation |
|------------------|------------------------|----------------|
| `patient_id` | `patient_ref` | Direct reference (foreign key) |
| `mrn` | (stored in `patient_ref`) | Medical Record Number preserved |
| `birth_date` | `birth_date` | Direct copy, format: ISO 8601 |
| `sex` | `sex` | Map to CDISC CT C66731: Male→M, Female→F |
| `race` | `race` | Map to CDISC CT C74457 |
| `ethnicity` | `ethnicity` | Map to CDISC CT C66790 |
| `diagnosis` | (screening criteria) | Used for I/E criteria evaluation |
| `prior_treatments` | (baseline context) | Used for MedicalHistory |

**Integration Pattern:**
```json
{
  "patientsim_patient": {
    "patient_id": "PAT-2024-0012345",
    "mrn": "MRN-12345",
    "birth_date": "1958-05-15",
    "sex": "Male",
    "race": "White",
    "ethnicity": "Not Hispanic or Latino",
    "diagnosis": "Non-Small Cell Lung Cancer, Stage IV",
    "prior_treatments": ["Carboplatin + Pemetrexed", "Immunotherapy (Nivolumab)"]
  },
  "trialsim_subject": {
    "usubjid": "ONCO-001-US01-0001",
    "subject_id": "0001",
    "patient_ref": "PAT-2024-0012345",
    "birth_date": "1958-05-15",
    "sex": "M",
    "race": "WHITE",
    "ethnicity": "NOT HISPANIC OR LATINO",
    "age": 66,
    "site_id": "US01",
    "armcd": "TRT",
    "reference_start_date": "2025-01-22",
    "status": "Active"
  }
}
```

### NetworkSim Provider to TrialSim Investigator

| NetworkSim Field | TrialSim Site Field | Transformation |
|------------------|---------------------|----------------|
| `provider_id` | `principal_investigator.provider_ref` | Direct reference (foreign key) |
| `npi_number` | (stored in PI object) | National Provider Identifier |
| `full_name` | `principal_investigator.name` | Direct copy |
| `credentials` | `principal_investigator.credentials` | e.g., MD, PhD |
| `specialties` | (eligibility for PI role) | Match to therapeutic area |
| `state_license` | (stored in site) | State medical license number |
| `facility_id` | (linked to Institution) | NetworkSim Facility → Site Institution |
| `gcp_trained` | (required for PI) | GCP training verification |
| `financial_disclosure` | (required for FDA) | Form 3455/1572 compliance |

### PopulationSim Demographics to TrialSim Subject Pool

| PopulationSim Field | TrialSim Application | Usage |
|--------------------|----------------------|-------|
| `prevalence_rate` | Site feasibility | Eligible patient pool sizing |
| `demographic_distribution` | Demographics (DM) | Realistic race/ethnicity/age distributions |
| `geographic_clusters` | Site selection | Catchment area optimization |
| `svi_score` | Diversity planning | FDA diversity guidance compliance |
| `adi_national_rank` | Site access assessment | Screen failure risk adjustment |
| `county_fips` | Site.location | County-level data linkage |
| `total_population` | Enrollment targets | Power and sample size validation |

**Integration Data Flow:**
```
PopulationSim (CDC PLACES/SVI data)
  ↓ provide prevalence & diversity context
TrialSim Site Feasibility Model
  ↓ determine site eligibility and enrollment targets
TrialSim Subject Generation
  ↓ sample from realistic demographic distributions
DM domain output (CDISC compliant)
```

---

## Related Resources

| Resource | File | Description |
|----------|------|-------------|
| TrialSim Skill | [../SKILL.md](../SKILL.md) | Master TrialSim skill |
| SDTM Format | [../formats/cdisc-sdtm.md](../formats/cdisc-sdtm.md) | CDISC SDTM format specification |
| CSV Export | [../formats/csv.md](../formats/csv.md) | Regulatory CSV export format |
| Domain Skills Index | [../domains/README.md](../domains/README.md) | All SDTM domain skills |
| Code Systems | [code-systems.md](code-systems.md) | MedDRA, LOINC, ATC references |
| PatientSim | [../../patientsim/SKILL.md](../../patientsim/SKILL.md) | Patient data product |
| NetworkSim | [../../networksim/SKILL.md](../../networksim/SKILL.md) | Provider network product |

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-06 | Initial canonical 15-entity schema definitions with JSON Schema (draft-2020-12), SDTM mappings, and cross-product integrations |
