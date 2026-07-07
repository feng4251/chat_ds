---
name: cdisc-sdtm
description: |
  Master CDISC SDTM format specification for regulatory submission data generation.
  Covers SDTM IG 3.4 observation classes, variable naming conventions, domain-level
  variable tables for DM, AE, CM, VS, LB, EX, DS, and MH, controlled terminology
  references, define.xml generation, SQL DDL mapping, and submission package structure.
  Triggers: "SDTM format", "CDISC submission", "define.xml", "XPT", "SDTM mapping",
  "regulatory data format".
---

# CDISC SDTM Format Specification

Master specification for generating Study Data Tabulation Model (SDTM) compliant datasets for regulatory submission (FDA/PMDA/EMA).

---

## For Claude

This is the **authoritative SDTM format reference** for TrialSim. When generating CDISC-compliant output, apply the conventions and variable definitions in this document. For individual domain variable tables, cross-reference the domain skills in [domains/README.md](../domains/README.md).

**Always apply this specification when:**
- Generating SDTM-formatted output from TrialSim
- Mapping canonical entities to SDTM variables
- Producing define.xml metadata
- Converting TrialSim output to XPT (SAS Transport) format
- Building SQL DDL from SDTM variable definitions

---

## SDTM Version Reference

This specification follows **CDISC SDTM Implementation Guide (IG) v3.4** (Study Data Tabulation Model v2.2), the current FDA-accepted version for regulatory submissions.

### Observation Classes

SDTM domains are grouped into four observation classes that reflect the data structure:

#### Special Purpose Domains

| Domain | Name | Description |
|--------|------|-------------|
| **DM** | Demographics | One record per subject; required for all studies |
| **SE** | Subject Elements | Subject epoch/arm changes over time |
| **SV** | Subject Visits | Actual visit occurrences per subject |

#### Interventions Domains

| Domain | Name | Description |
|--------|------|-------------|
| **EX** | Exposure | Study drug administration records |
| **CM** | Concomitant/Prior Medications | Non-study medications |
| **EC** | Exposure as Collected | Collected exposure data (complement to EX) |
| **SU** | Substance Use | Tobacco, alcohol, drug use |

#### Events Domains

| Domain | Name | Description |
|--------|------|-------------|
| **AE** | Adverse Events | Safety events during the trial |
| **DS** | Disposition | Protocol milestones and subject status |
| **MH** | Medical History | Pre-existing conditions |
| **DV** | Protocol Deviations | Protocol violation records |
| **CE** | Clinical Events | Non-adverse clinical occurrences |

#### Findings Domains

| Domain | Name | Description |
|--------|------|-------------|
| **VS** | Vital Signs | Blood pressure, heart rate, temperature, weight |
| **LB** | Laboratory Test Results | Chemistry, hematology, urinalysis panels |
| **EG** | ECG Test Results | Electrocardiogram measurements |
| **PE** | Physical Examination | Physical exam findings |
| **QS** | Questionnaires | Patient-reported outcomes, scales |

---

## Variable Naming Convention

All SDTM variables follow the **2-character domain prefix** convention:

```
<DOMAIN_PREFIX><DESCRIPTIVE_SUFFIX>

Examples:
  AE + AESEV   = AESEV   (AE Severity)
  AE + AESER   = AESER   (AE Seriousness)
  CM + CMDECOD = CMDECOD (CM Dictionary-Derived Term)
  LB + LBTEST  = LBTEST  (LB Test Name)
  VS + VSORRES = VSORRES (VS Original Result)
  EX + EXDOSE  = EXDOSE  (EX Dose)
  DS + DSTERM  = DSTERM  (DS Reported Term)
  MH + MHDECOD = MHDECOD (MH Dictionary-Derived Term)
```

When referencing variables generically across all domains, use `--SEQ` (meaning AESEQ, CMSEQ, LBSEQ, etc.) to indicate the sequence number pattern.

---

## Core Variables (All Domains)

Every SDTM domain must include the following identifier variables:

| Variable | Label | Type | Length | Description |
|----------|-------|------|--------|-------------|
| STUDYID | Study Identifier | Char | 20 | Unique study identifier |
| DOMAIN | Domain Abbreviation | Char | 2 | Two-character domain code |
| USUBJID | Unique Subject Identifier | Char | 40 | Globally unique subject ID (STUDYID-SITEID-SUBJID) |
| `--SEQ` | Sequence Number | Num | 8 | Unique within USUBJID + DOMAIN |
| `--STDTC` | Start Date/Time of Observation | Char | 19 | ISO 8601 |
| `--ENDTC` | End Date/Time of Observation | Char | 19 | ISO 8601 |

Additional timing variables used across domains:

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| `--DY` | Study Day of Observation | Num | Days relative to RFSTDTC (DM) |
| VISITNUM | Visit Number | Num | Numeric visit identifier |
| VISIT | Visit Name | Char | Visit description |
| VISITDY | Planned Study Day of Visit | Num | Protocol-scheduled day |
| `--TPT` | Planned Time Point Name | Char | Time point within visit |
| `--TPTNUM` | Planned Time Point Number | Num | Numeric time point |

---

## Domain Variable Tables

### DM: Demographics (Special Purpose)

**Reference:** [domains/demographics-dm.md](../domains/demographics-dm.md)

#### Required Variables

| Variable | Label | Type | Length | Controlled Terminology |
|----------|-------|------|--------|------------------------|
| STUDYID | Study Identifier | Char | 20 | -- |
| DOMAIN | Domain Abbreviation | Char | 2 | Fixed: "DM" |
| USUBJID | Unique Subject Identifier | Char | 40 | -- |
| SUBJID | Subject Identifier for the Study | Char | 20 | -- |
| RFSTDTC | Subject Reference Start Date/Time | Char | 19 | ISO 8601 |
| RFENDTC | Subject Reference End Date/Time | Char | 19 | ISO 8601 |
| SITEID | Study Site Identifier | Char | 10 | -- |
| AGE | Age | Num | 8 | -- |
| AGEU | Age Units | Char | 6 | "YEARS" |
| SEX | Sex | Char | 1 | C66731: M, F, U |
| RACE | Race | Char | 60 | C74457 |
| ETHNIC | Ethnicity | Char | 40 | C66790 |
| ARMCD | Planned Arm Code | Char | 20 | -- |
| ARM | Description of Planned Arm | Char | 200 | -- |
| COUNTRY | Country | Char | 3 | ISO 3166-1 alpha-3 |

#### Expected Variables

| Variable | Label | Type | Controlled Terminology |
|----------|-------|------|------------------------|
| BRTHDTC | Date/Time of Birth | Char | ISO 8601 |
| RFICDTC | Date/Time of Informed Consent | Char | ISO 8601 |
| RFPENDTC | Date/Time of End of Participation | Char | ISO 8601 |
| DTHFL | Subject Death Flag | Char | Y, (null) |
| DTHDTC | Date/Time of Death | Char | ISO 8601 |
| ACTARMCD | Actual Arm Code | Char | -- |
| ACTARM | Description of Actual Arm | Char | -- |

#### Permissible Variables

| Variable | Label | Type |
|----------|-------|------|
| DOMAIN | (value "DM") | Char |
| RFXSTDTC | Date/Time of First Study Treatment | Char |
| RFXENDTC | Date/Time of Last Study Treatment | Char |
| DMCOHORT | Cohort Number | Num |
| DMDTC | Date/Time of Data Collection | Char |
| DMDY | Study Day of Collection | Num |

---

### AE: Adverse Events (Events)

**Reference:** [domains/adverse-events-ae.md](../domains/adverse-events-ae.md)

#### Required Variables

| Variable | Label | Type | Length | Controlled Terminology |
|----------|-------|------|--------|------------------------|
| STUDYID | Study Identifier | Char | 20 | -- |
| DOMAIN | Domain Abbreviation | Char | 2 | Fixed: "AE" |
| USUBJID | Unique Subject Identifier | Char | 40 | -- |
| AESEQ | Sequence Number | Num | 8 | -- |
| AETERM | Reported Term for the Adverse Event | Char | 200 | -- |
| AEDECOD | Dictionary-Derived Term | Char | 200 | MedDRA Preferred Term |
| AESTDTC | Start Date/Time | Char | 19 | ISO 8601 |

#### Expected Variables

| Variable | Label | Type | Controlled Terminology |
|----------|-------|------|------------------------|
| AEBODSYS | Body System or Organ Class | Char | MedDRA SOC |
| AESOC | Primary System Organ Class | Char | MedDRA SOC code |
| AEHLGT | High Level Group Term | Char | MedDRA HLGT |
| AEHLT | High Level Term | Char | MedDRA HLT |
| AELLT | Lowest Level Term | Char | MedDRA LLT |
| AESEV | Severity/Intensity | Char | C66769: MILD, MODERATE, SEVERE |
| AESER | Serious Event | Char | C66742: Y, N |
| AEACN | Action Taken with Study Treatment | Char | C66726: DOSE REDUCED, DRUG INTERRUPTED, DRUG WITHDRAWN, DOSE INCREASED, NOT APPLICABLE |
| AEREL | Causality | Char | C66732: NOT RELATED, POSSIBLY RELATED, RELATED |
| AEOUT | Outcome of Adverse Event | Char | C66728: RECOVERED/RESOLVED, RECOVERING/RESOLVING, NOT RECOVERED/NOT RESOLVED, RECOVERED/RESOLVED WITH SEQUELAE, FATAL |
| AEENDTC | End Date/Time | Char | ISO 8601 |
| AEDUR | Duration | Char | ISO 8601 duration |
| AETOXGR | Toxicity Grade | Char | C68802: 1, 2, 3, 4, 5 (CTCAE) |
| AESHOSP | Requires or Prolongs Hospitalization | Char | C66742: Y, N |

#### Permissible Variables

| Variable | Label | Type |
|----------|-------|------|
| AECAT | Category | Char |
| AESCONG | Congenital Anomaly or Birth Defect | Char |
| AESDISAB | Persistently or Significantly Disabling/Incapacitating | Char |
| AESDTH | Results in Death | Char |
| AESLIFE | Life Threatening | Char |
| AESMIE | Other Medically Important Serious Event | Char |
| AESPID | Sponsor-Defined Identifier | Char |

---

### CM: Concomitant Medications (Interventions)

**Reference:** [domains/concomitant-meds-cm.md](../domains/concomitant-meds-cm.md)

#### Required Variables

| Variable | Label | Type | Length | Controlled Terminology |
|----------|-------|------|--------|------------------------|
| STUDYID | Study Identifier | Char | 20 | -- |
| DOMAIN | Domain Abbreviation | Char | 2 | Fixed: "CM" |
| USUBJID | Unique Subject Identifier | Char | 40 | -- |
| CMSEQ | Sequence Number | Num | 8 | -- |
| CMTRT | Reported Name of Drug | Char | 200 | -- |
| CMDECOD | Standardized Medication Name | Char | 200 | WHO Drug Global |
| CMSTDTC | Start Date/Time of Medication | Char | 19 | ISO 8601 |

#### Expected Variables

| Variable | Label | Type | Controlled Terminology |
|----------|-------|------|------------------------|
| CMCAT | Category for Medication | Char | C66790: PRIOR, CONCOMITANT |
| CMSCAT | Subcategory | Char | C66790 |
| CMDOSE | Dose per Administration | Num | Numeric dose |
| CMDOSU | Dose Units | Char | C66789: mg, mcg, g, mL, IU |
| CMDOSFRQ | Dosing Frequency per Interval | Char | C66795: QD, BID, TID, QID, QOD, PRN, ONCE |
| CMROUTE | Route of Administration | Char | C66726: ORAL, INTRAVENOUS, SUBCUTANEOUS, INTRAMUSCULAR, TOPICAL, INHALED |
| CMDOSFRM | Dose Form | Char | C66726: TABLET, CAPSULE, INJECTION, CREAM |
| CMINDC | Indication | Char | MedDRA or ICD |
| CMENDTC | End Date/Time of Medication | Char | ISO 8601 |
| CMONGO | Ongoing | Char | Y, N |
| CMATC | ATC Classification | Char | WHO ATC code |
| CMSTDY | Study Day of Start | Num | Relative to RFSTDTC |
| CMENDY | Study Day of End | Num | Relative to RFSTDTC |

#### Permissible Variables

| Variable | Label | Type |
|----------|-------|------|
| CMREASND | Reason for Discontinuation | Char |
| CMOCCUR | Occurrence | Char |
| CMPRESP | Pre-Specified | Char |

---

### VS: Vital Signs (Findings)

**Reference:** [domains/vital-signs-vs.md](../domains/vital-signs-vs.md)

#### Required Variables

| Variable | Label | Type | Length | Controlled Terminology |
|----------|-------|------|--------|------------------------|
| STUDYID | Study Identifier | Char | 20 | -- |
| DOMAIN | Domain Abbreviation | Char | 2 | Fixed: "VS" |
| USUBJID | Unique Subject Identifier | Char | 40 | -- |
| VSSEQ | Sequence Number | Num | 8 | -- |
| VSTESTCD | Vital Signs Test Short Name | Char | 8 | SYSBP, DIABP, HR, RESP, TEMP, HEIGHT, WEIGHT, BMI |
| VSTEST | Vital Signs Test Name | Char | 40 | CDISC CT for Vital Signs Test Name |
| VSORRES | Result or Finding in Original Units | Char | 200 | -- |
| VSORRESU | Original Units | Char | 20 | mmHg, beats/min, breaths/min, C, cm, kg, kg/m2 |
| VSSTDTC | Start Date/Time | Char | 19 | ISO 8601 |

#### Expected Variables

| Variable | Label | Type | Controlled Terminology |
|----------|-------|------|------------------------|
| VSSTRESC | Character Result/Finding in Std Format | Char | -- |
| VSSTRESN | Numeric Result/Finding in Standard Units | Num | -- |
| VSSTRESU | Standard Units | Char | C66789 |
| VSSTAT | Completion Status | Char | C66744: NOT DONE |
| VSREASND | Reason Not Done | Char | -- |
| VSPOS | Position of Subject | Char | C66791: SITTING, STANDING, SUPINE |
| VSLOC | Location of Measurement | Char | C66766: ARM, LEG |
| VSLAT | Laterality | Char | C66766: LEFT, RIGHT, BILATERAL |
| VSMETHOD | Method of Test or Examination | Char | -- |
| VSBLFL | Baseline Flag | Char | C66742: Y |
| VSNRIND | Reference Range Indicator | Char | C66747: NORMAL, ABNORMAL, HIGH, LOW |
| VSLOINC | LOINC Code for Test | Char | LOINC |

#### Permissible Variables

| Variable | Label | Type |
|----------|-------|------|
| VSDTC | Date/Time of Collection | Char |
| VISITNUM | Visit Number | Num |
| VISIT | Visit Name | Char |
| VSDY | Study Day | Num |
| VSTPT | Planned Time Point Name | Char |
| VSTPTNUM | Planned Time Point Number | Num |

---

### LB: Laboratory Test Results (Findings)

**Reference:** [domains/laboratory-lb.md](../domains/laboratory-lb.md)

#### Required Variables

| Variable | Label | Type | Length | Controlled Terminology |
|----------|-------|------|--------|------------------------|
| STUDYID | Study Identifier | Char | 20 | -- |
| DOMAIN | Domain Abbreviation | Char | 2 | Fixed: "LB" |
| USUBJID | Unique Subject Identifier | Char | 40 | -- |
| LBSEQ | Sequence Number | Num | 8 | -- |
| LBTESTCD | Lab Test Short Name | Char | 8 | LOINC or sponsor-defined |
| LBTEST | Lab Test Name | Char | 40 | CDISC CT for Lab Test Name |
| LBORRES | Result or Finding in Original Units | Char | 200 | -- |
| LBORRESU | Original Units | Char | 20 | C66789 |
| LBSTDTC | Start Date/Time | Char | 19 | ISO 8601 |

#### Expected Variables

| Variable | Label | Type | Controlled Terminology |
|----------|-------|------|------------------------|
| LBSTRESC | Character Result Std Format | Char | -- |
| LBSTRESN | Numeric Result Std Units | Num | -- |
| LBSTRESU | Standard Units | Char | C66789: mg/dL, U/L, mmol/L, 10^9/L, g/dL |
| LBSTNRLO | Reference Range Lower Limit-Std Units | Num | -- |
| LBSTNRHI | Reference Range Upper Limit-Std Units | Num | -- |
| LBNRIND | Reference Range Indicator | Char | C66747: NORMAL, HIGH, LOW, ABNORMAL |
| LBSTAT | Completion Status | Char | C66744: NOT DONE |
| LBREASND | Reason Not Done | Char | -- |
| LBMETHOD | Method of Test or Examination | Char | -- |
| LBBLFL | Baseline Flag | Char | C66742: Y |
| LBLOINC | LOINC Code | Char | LOINC |
| LBTOXGR | Toxicity Grade | Char | C68802: 1, 2, 3, 4 |
| LBCAT | Category for Lab Test | Char | CHEMISTRY, HEMATOLOGY, URINALYSIS |

#### Permissible Variables

| Variable | Label | Type |
|----------|-------|------|
| LDLTFL | Lab Data Alert Flag | Char |
| LBFAST | Fasting Status | Char |
| LBSPEC | Specimen Type | Char |
| LBMETHOD | Test Method | Char |

---

### EX: Exposure (Interventions)

**Reference:** [domains/exposure-ex.md](../domains/exposure-ex.md)

#### Required Variables

| Variable | Label | Type | Length | Controlled Terminology |
|----------|-------|------|--------|------------------------|
| STUDYID | Study Identifier | Char | 20 | -- |
| DOMAIN | Domain Abbreviation | Char | 2 | Fixed: "EX" |
| USUBJID | Unique Subject Identifier | Char | 40 | -- |
| EXSEQ | Sequence Number | Num | 8 | -- |
| EXTRT | Name of Treatment | Char | 200 | Sponsor-defined |
| EXDOSE | Dose | Num | 8 | -- |
| EXDOSU | Dose Units | Char | 40 | C66789: mg, mcg, mL |
| EXDOSFRM | Dose Form | Char | 40 | C66726: TABLET, CAPSULE, INJECTION, INFUSION |
| EXROUTE | Route of Administration | Char | 40 | C66726: ORAL, INTRAVENOUS, SUBCUTANEOUS |
| EXSTDTC | Start Date/Time of Treatment | Char | 19 | ISO 8601 |

#### Expected Variables

| Variable | Label | Type | Controlled Terminology |
|----------|-------|------|------------------------|
| EXENDTC | End Date/Time of Treatment | Char | ISO 8601 |
| EXDOSFRQ | Dosing Frequency per Interval | Char | C66795: QD, BID, TID, QID, ONCE |
| EXLOT | Lot Number | Char | -- |
| EXLOC | Location of Dose Administration | Char | C66766 |
| EXLAT | Laterality | Char | C66766 |
| EPOCH | Epoch | Char | SCREENING, TREATMENT, FOLLOW-UP |
| EXDOSTXT | Dose Description | Char | Free text description |
| EXTRTCD | Treatment Code | Char | Sponsor-defined |
| EXSTDY | Study Day of Dose Start | Num | Relative to RFSTDTC |
| EXENDY | Study Day of Dose End | Num | Relative to RFSTDTC |
| EXVAMT | Vial Amount | Num | Amount drawn from vial |
| EXVAMTU | Vial Amount Units | Char | C66789 |
| EXRATE | Rate of Administration | Num | Infusion rate |
| EXRATEU | Rate Unit | Char | C66789: mL/hr, mg/hr |

#### Permissible Variables

| Variable | Label | Type |
|----------|-------|------|
| EXADJ | Adjustment | Char |
| EXDUR | Duration | Char |
| EXDOSFRQTY | Dosing Frequency per Interval Quantity | Num |
| EXCMTRT | Concomitant or Rescue Treatment | Char |
| EXCRIT | Criteria for Dose Administration | Char |

---

### DS: Disposition (Events)

**Reference:** [domains/disposition-ds.md](../domains/disposition-ds.md)

#### Required Variables

| Variable | Label | Type | Length | Controlled Terminology |
|----------|-------|------|--------|------------------------|
| STUDYID | Study Identifier | Char | 20 | -- |
| DOMAIN | Domain Abbreviation | Char | 2 | Fixed: "DS" |
| USUBJID | Unique Subject Identifier | Char | 40 | -- |
| DSSEQ | Sequence Number | Num | 8 | -- |
| DSTERM | Reported Term | Char | 200 | -- |
| DSDECOD | Standardized Disposition Term | Char | 200 | C66728: COMPLETED, ADVERSE EVENT, WITHDRAWAL BY SUBJECT, LOST TO FOLLOW-UP, DEATH, PROTOCOL DEVIATION |
| DSCAT | Category for Disposition | Char | 40 | C66790: DISPOSITION EVENT, PROTOCOL MILESTONE |
| DSSCAT | Subcategory for Disposition | Char | 40 | STUDY, TREATMENT |
| DSSTDTC | Start Date/Time | Char | 19 | ISO 8601 |

#### Expected Variables

| Variable | Label | Type | Controlled Terminology |
|----------|-------|------|------------------------|
| EPOCH | Epoch | Char | SCREENING, TREATMENT, FOLLOW-UP |
| VISITNUM | Visit Number | Num | -- |
| VISIT | Visit Name | Char | -- |
| DSDY | Study Day of Disposition | Num | Relative to RFSTDTC |
| DSTERM | Reported Term for Disposition | Char | -- |
| DSDECOD | Dictionary-Derived Term | Char | C66728 |

#### Permissible Variables

| Variable | Label | Type |
|----------|-------|------|
| DSREASND | Reason Not Done | Char |
| DSOCCUR | Occurrence | Char |

---

### MH: Medical History (Events)

**Reference:** [domains/medical-history-mh.md](../domains/medical-history-mh.md)

#### Required Variables

| Variable | Label | Type | Length | Controlled Terminology |
|----------|-------|------|--------|------------------------|
| STUDYID | Study Identifier | Char | 20 | -- |
| DOMAIN | Domain Abbreviation | Char | 2 | Fixed: "MH" |
| USUBJID | Unique Subject Identifier | Char | 40 | -- |
| MHSEQ | Sequence Number | Num | 8 | -- |
| MHTERM | Reported Term for the Medical History | Char | 200 | -- |
| MHDECOD | Dictionary-Derived Term | Char | 200 | MedDRA Preferred Term |
| MHCAT | Category for Medical History | Char | 40 | C66790 |
| MHSTDTC | Start Date/Time | Char | 19 | ISO 8601 |

#### Expected Variables

| Variable | Label | Type | Controlled Terminology |
|----------|-------|------|------------------------|
| MHBODSYS | Body System or Organ Class | Char | MedDRA SOC |
| MHENDTC | End Date/Time | Char | ISO 8601 |
| MHONGO | Ongoing | Char | Y, N |
| MHPRESP | Pre-Specified | Char | Y, N |
| MHOCCUR | Occurrence | Char | Y, N |
| MHSEV | Severity | Char | C66769: MILD, MODERATE, SEVERE |
| MHLLT | Lowest Level Term | Char | MedDRA LLT |
| MHHLT | High Level Term | Char | MedDRA HLT |
| MHHLGT | High Level Group Term | Char | MedDRA HLGT |

#### Permissible Variables

| Variable | Label | Type |
|----------|-------|------|
| MHSTAT | Completion Status | Char |
| MHREASND | Reason Not Done | Char |
| MHGRPID | Group ID | Char |
| MHDY | Study Day | Num |

---

## Date Format

All SDTM date/datetime variables use **ISO 8601** format:

| Format | Pattern | Example | Usage |
|--------|---------|---------|-------|
| Date only | `YYYY-MM-DD` | `2025-01-15` | RFSTDTC, BRTHDTC |
| Date & time | `YYYY-MM-DDThh:mm:ss` | `2025-01-15T09:30:00` | --DTC with time component |
| Date & time with timezone | `YYYY-MM-DDThh:mm:ss+HH:MM` | `2025-01-15T09:30:00-05:00` | When timezone is known |
| Partial date (unknown month) | `YYYY` | `2025` | -- |
| Partial date (unknown day) | `YYYY-MM` | `2025-01` | -- |

**Rule:** Always prefer the most complete date available. For trial events where exact time is captured, use the full datetime format.

---

## Cross-Domain Referential Integrity

SDTM datasets form a relational model with the following integrity constraints:

### Primary Key Rule

```
DM.USUBJID is the foreign key for all other domains.
Every record in AE, CM, VS, LB, EX, DS, MH must reference a valid DM.USUBJID.
```

### Sequence Uniqueness Rule

```
--SEQ must be unique within (USUBJID, DOMAIN).
Example: For subject "ABC-001-001-0001":
  AESEQ values: 1, 2, 3, ... (unique in AE domain)
  CMSEQ values: 1, 2, 3, ... (unique in CM domain)
  --SEQ values ARE allowed to repeat across domains for the same subject.
```

### Date Consistency Rules

```
1. RFSTDTC <= --STDTC for all records in other domains
   (No observation may start before the subject's reference start date).

2. --STDTC <= --ENDTC for any record with an end date.

3. For DM: RFSTDTC <= RFENDTC (or RFENDTC may be null for ongoing subjects).

4. DTHDTC (if populated) >= RFSTDTC
   Death cannot occur before the subject entered the study.

5. For AE: AESTDTC >= RFSTDTC
   Adverse events must have onset after the subject's reference start date.

6. DS.INFORMED CONSENT date must be <= RFSTDTC
   Consent must precede any study procedures.
```

### Cross-Domain Linkage

| Child Domain | Parent Reference | Join Variable |
|--------------|-----------------|---------------|
| AE, CM, VS, LB, EX, DS, MH | DM | USUBJID |
| SV | DM | USUBJID |
| AE (action taken) | EX (treatment) | USUBJID + timing |
| DS (disposition) | EX (exposure) | USUBJID + EPOCH |

---

## SDTM Submission Package Structure

An eCTD-compliant SDTM submission package follows this directory structure:

```
Module-5/
  datasets/
    sdtm/
      data/
        dm.xpt          # Demographics
        ae.xpt          # Adverse Events
        cm.xpt          # Concomitant Medications
        vs.xpt          # Vital Signs
        lb.xpt          # Laboratory Results
        ex.xpt          # Exposure
        ds.xpt          # Disposition
        mh.xpt          # Medical History
        dv.xpt          # Protocol Deviations
        se.xpt          # Subject Elements
        sv.xpt          # Subject Visits
        suppae.xpt      # Supplemental Qualifiers for AE
        suppdm.xpt      # Supplemental Qualifiers for DM
      define.xml        # Data Definition Document (v2.1)
      define2-0-0.xsl   # Stylesheet for define.xml rendering
      sdtm-annotated-crf.pdf  # SDTM annotated CRF
      Reviewer-Guide.pdf      # cSDRG (Clinical Study Data Reviewer's Guide)
```

### XPT (SAS Transport) Format

| Property | Value |
|----------|-------|
| Format | SAS Transport File v5 (.xpt) |
| Variable naming | Uppercase, max 8 characters |
| Variable labels | Max 40 characters (SAS transport limit) |
| Missing values | SAS numeric missing (`.`) or character blank (`""`) |
| Encoding | ASCII-compatible (for FDA compliance) |

### define.xml v2.1

define.xml v2.1 is the required metadata standard. Key components:

| Component | Element | Purpose |
|-----------|---------|---------|
| ItemGroupDef | Domain metadata | One per SDTM domain/dataset |
| ItemDef | Variable metadata | One per variable |
| CodeList | Controlled terminology | Standard and sponsor-defined codelists |
| MethodDef | Algorithm descriptions | Derived variable methodology |
| ValueListDef | Value-level metadata | For supplemental qualifiers |
| WhereClauseDef | Conditional logic | Variable relationship rules |

---

## Variable Metadata Table Format

For define.xml generation and data dictionaries, each variable is documented with the following metadata attributes:

| Attribute | Description | Example |
|-----------|-------------|---------|
| **Variable** | SDTM variable name (max 8 chars) | AESEV |
| **Label** | Human-readable variable description | Severity/Intensity |
| **Type** | Data type | Char, Num |
| **Length** | Maximum length (Char) or precision (Num) | 200, 8 |
| **Controlled Terminology** | CDISC CT codelist reference | C66769 |
| **Origin** | Source of the data | CRF, Derived, Assigned, Protocol, eDT |
| **Role** | SDTM variable role | Identifier, Topic, Timing, Qualifier, Rule |

### Role Definitions

| Role | Description | Examples |
|------|-------------|----------|
| **Identifier** | Uniquely identifies a record | STUDYID, USUBJID, --SEQ |
| **Topic** | Primary subject of observation | AETERM, CMTRT, LBTEST |
| **Qualifier** | Describes or qualifies the topic | AESEV, CMDOSE, LBNRIND |
| **Timing** | Describes timing of the observation | --STDTC, VISITNUM |
| **Rule** | Algorithm or rule for derivation | Variables derived computationally |

---

## SQL DDL Mapping Rules

When converting SDTM variable definitions to SQL DDL, apply the following type mappings:

| SDTM Type | SQL Type | Notes |
|-----------|----------|-------|
| Char(N) | VARCHAR(N) | Variable-length string; use N from SDTM Length column |
| Num | NUMERIC or DECIMAL | When precision is specified (Num=8 means NUMERIC(8,0)) |
| ISO 8601 Date (Char) | DATE | Date-only SDTM variables (e.g., BRTHDTC) |
| ISO 8601 DateTime (Char) | TIMESTAMP | DateTime SDTM variables (e.g., --DTC) |

### Default DDL Template per Domain

```sql
-- Example: DM domain table
CREATE TABLE dm (
    studyid     VARCHAR(20)    NOT NULL,
    domain      VARCHAR(2)     NOT NULL DEFAULT 'DM',
    usubjid     VARCHAR(40)    NOT NULL,
    subjid      VARCHAR(20),
    rfstdtc     DATE,
    rfendtc     DATE,
    siteid      VARCHAR(10),
    age         NUMERIC(8,0),
    ageu        VARCHAR(6),
    sex         VARCHAR(1),
    race        VARCHAR(60),
    ethnic      VARCHAR(40),
    armcd       VARCHAR(20),
    arm         VARCHAR(200),
    country     VARCHAR(3),
    PRIMARY KEY (studyid, usubjid)
);

-- Foreign key pattern for child domains
ALTER TABLE ae ADD CONSTRAINT fk_ae_dm
    FOREIGN KEY (studyid, usubjid) REFERENCES dm(studyid, usubjid);
```

---

## Controlled Terminology References

### CDISC Controlled Terminology (CT)

All controlled terminology is versioned by CDISC CT publication date. References use CDISC Submission Value format.

| Codelist Code | Description | Domains | Key Values |
|---------------|-------------|---------|------------|
| C66731 | SEX | DM | M (MALE), F (FEMALE), U (UNKNOWN) |
| C74457 | RACE | DM | WHITE, BLACK OR AFRICAN AMERICAN, ASIAN, AMERICAN INDIAN OR ALASKA NATIVE, NATIVE HAWAIIAN OR OTHER PACIFIC ISLANDER, MULTIPLE, OTHER, UNKNOWN |
| C66790 | ETHNIC | DM | HISPANIC OR LATINO, NOT HISPANIC OR LATINO, NOT REPORTED, UNKNOWN |
| C66769 | SEV | AE, MH | MILD, MODERATE, SEVERE |
| C66742 | NY | AE, DS | Y, N |
| C66726 | ACN | AE | DOSE REDUCED, DRUG INTERRUPTED, DRUG WITHDRAWN, DOSE INCREASED, NOT APPLICABLE |
| C66732 | REL | AE | NOT RELATED, POSSIBLY RELATED, RELATED |
| C66728 | OUT | AE, DS | RECOVERED/RESOLVED, RECOVERING/RESOLVING, NOT RECOVERED/NOT RESOLVED, RECOVERED/RESOLVED WITH SEQUELAE, FATAL |
| C68802 | TOXGR | AE, LB | 1, 2, 3, 4, 5 |
| C66795 | FREQ | EX, CM | QD, BID, TID, QID, QOD, PRN, ONCE |
| C66726 | ROUTE | EX, CM | ORAL, INTRAVENOUS, SUBCUTANEOUS, INTRAMUSCULAR, TOPICAL |
| C66789 | UNIT | LB, VS, EX | mg, mcg, g, mL, mmHg, beats/min |
| C66747 | NRIND | LB, VS | NORMAL, HIGH, LOW, ABNORMAL |
| C66744 | STAT | LB, VS | NOT DONE |
| C66791 | POSITION | VS | SITTING, STANDING, SUPINE |

### External Terminologies

| Terminology | Domains | Purpose |
|-------------|---------|---------|
| MedDRA | AE, MH | Preferred Term (PT), System Organ Class (SOC) coding |
| LOINC | LB, VS | Laboratory and vital sign test identification |
| WHO Drug Global | CM | Standardized medication names |
| WHO ATC | CM | Anatomical Therapeutic Chemical classification |
| ISO 3166-1 alpha-3 | DM | Country codes |
| CTCAE v5.0 | AE, LB | Toxicity/safety grading |

---

## References to TrialSim Domain Skills

| SDTM Domain | TrialSim Skill | File |
|-------------|---------------|------|
| DM | Demographics | [domains/demographics-dm.md](../domains/demographics-dm.md) |
| AE | Adverse Events | [domains/adverse-events-ae.md](../domains/adverse-events-ae.md) |
| CM | Concomitant Medications | [domains/concomitant-meds-cm.md](../domains/concomitant-meds-cm.md) |
| VS | Vital Signs | [domains/vital-signs-vs.md](../domains/vital-signs-vs.md) |
| LB | Laboratory | [domains/laboratory-lb.md](../domains/laboratory-lb.md) |
| EX | Exposure | [domains/exposure-ex.md](../domains/exposure-ex.md) |
| DS | Disposition | [domains/disposition-ds.md](../domains/disposition-ds.md) |
| MH | Medical History | [domains/medical-history-mh.md](../domains/medical-history-mh.md) |
| Domain Index | All Domains | [domains/README.md](../domains/README.md) |

### Related Format Specifications

| Format | File | Description |
|--------|------|-------------|
| SDTM (this file) | [formats/cdisc-sdtm.md](cdisc-sdtm.md) | Regulatory submission format |
| ADaM | [formats/cdisc-adam.md](cdisc-adam.md) | Analysis dataset format |
| Dimensions | [formats/dimensional-analytics.md](dimensional-analytics.md) | Star schema for BI/analytics |
| CSV | [formats/csv.md](csv.md) | Flat file export specification |

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-06 | Initial SDTM master format specification covering IG 3.4 domains DM, AE, CM, VS, LB, EX, DS, MH |
