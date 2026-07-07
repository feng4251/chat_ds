---
name: adam-adtte
description: |
  Generate ADaM ADTTE (Time-to-Event Analysis Dataset) with censoring rules 
  for Overall Survival (OS), Progression-Free Survival (PFS), Time to 
  Discontinuation (TTD), Time to Dose Modification (TTDM), and Time to 
  Response (TTR). Kaplan-Meier ready with traceability to SDTM source. 
  Triggers: "ADTTE", "time-to-event", "survival analysis", "OS", "PFS", 
  "censoring", "Kaplan-Meier", "progression", "overall survival", 
  "event time", "survival endpoint".
---

# Time-to-Event Analysis Dataset (ADTTE)

The ADTTE dataset provides analysis-ready time-to-event data for survival analysis endpoints. Each record represents one event type (parameter) per subject with origin date, event/censoring date, days to event, and censoring indicator ready for Kaplan-Meier estimation and Cox proportional hazards modeling.

---

## For Claude

This is a **TTE-structured ADaM dataset skill** for generating time-to-event analysis data. ADTTE supports survival analysis endpoints required for oncology and chronic disease trials.

**Always apply this skill when you see:**
- Requests for time-to-event or survival analysis datasets
- Overall Survival (OS) or Progression-Free Survival (PFS) endpoints
- Censoring rules for clinical trial endpoints
- Kaplan-Meier analysis data preparation
- Time to discontinuation, dose modification, or response
- Event-driven endpoints requiring STARTDT, ADT, AVAL, CNSR

**Key responsibilities:**
- Define origin date (STARTDT) as randomization date
- Calculate days from origin to event/censoring date (AVAL)
- Apply endpoint-specific censoring rules for OS, PFS, TTD, TTDM, TTR
- Set CNSR = 0 for events and CNSR = 1 for censored observations
- Document event descriptions (EVNTDESC) and censoring reasons (CNSDTDSC)
- Maintain traceability to source SDTM records (AE, DS, RS, LB)

---

## ADaM Variables

### Required Variables (TTE Structure)

| Variable | Label | Type | Length | Description |
|----------|-------|------|--------|-------------|
| STUDYID | Study Identifier | Char | 20 | Unique study ID |
| USUBJID | Unique Subject Identifier | Char | 40 | From DM domain |
| PARAM | Parameter | Char | 200 | Full parameter description |
| PARAMCD | Parameter Code | Char | 8 | OS, PFS, TTD, TTDM, TTR |
| STARTDT | Time to Event Origin Date | Num | 8 | Randomization date (SAS date) |
| ADT | Analysis Date | Num | 8 | Event date or censoring date (SAS date) |
| AVAL | Analysis Value | Num | 8 | Days from STARTDT to ADT (+1) |
| CNSR | Censoring Indicator | Num | 8 | 0 = event occurred, 1 = censored |
| EVNTDESC | Event or Censoring Description | Char | 200 | Description of event |
| CNSDTDSC | Censoring Reason Description | Char | 200 | Reason for censoring |

### Expected Variables

| Variable | Label | Type | Description |
|----------|-------|------|-------------|
| PARCAT1 | Parameter Category 1 | Char | Categorization of endpoints |
| STARTDTYP | Time to Event Origin Date Type | Char | RANDDATE, FIRSTDOSE |
| ADY | Analysis Relative Day | Num | From ADSL.RFSTDTC to ADT |
| SRCDOM | Source Domain | Char | AE, DS, RS, LB (source of event) |
| SRCVAR | Source Variable | Char | AESEQ, DSSEQ, RSSEQ, LBSEQ |
| SRCSEQ | Source Sequence Number | Num | Sequence number in source domain |

---

## Parameter Definitions (PARAMCD)

| PARAMCD | PARAM | Endpoint Type | Primary Source |
|---------|-------|---------------|----------------|
| OS | Overall Survival (Days) | Death from any cause | DS (DEATH) |
| PFS | Progression-Free Survival (Days) | Disease progression or death | RS, DS, AE |
| TTD | Time to Discontinuation (Days) | Treatment discontinuation | DS (treatment DC) |
| TTDM | Time to Dose Modification (Days) | Dose modification or reduction | EX |
| TTR | Time to Response (Days) | First documented response | RS |

---

## Key Derivations

### STARTDT (Origin Date)

All time-to-event analyses are anchored to a common origin date:

```
STARTDT = Date of randomization (from ADSL.RANDDT)

If randomization date is missing (rare), use first dose date:
STARTDT = ADSL.TRTSDT

STARTDTYP = "RANDDATE" (primary) or "FIRSTDOSE" (fallback)
```

### ADT (Analysis Date)

```
ADT = Date of event (if CNSR = 0)
ADT = Date of censoring (if CNSR = 1)

For OS:
  Event: Death date from DS where DSDECOD = "DEATH"
  Censor: Last known alive date (last contact date in study)

For PFS:
  Event: Earliest of progression date (from RS) or death date (from DS)
  Censor: Date of last tumor assessment without progression
```

### AVAL (Analysis Value in Days)

```
AVAL = ADT - STARTDT + 1

Note: The +1 ensures subjects with event on the same day have AVAL = 1 (not 0).
This convention aligns with Kaplan-Meier methods.
```

### CNSR (Censoring Flag)

```
CNSR = 0: Event occurred (death, progression, discontinuation)
CNSR = 1: Censored (no event observed at end of follow-up)

Always exactly one of:
  - Event records: CNSR = 0, EVNTDESC populated
  - Censored records: CNSR = 1, CNSDTDSC populated
```

### Censoring Rules by PARAMCD

#### OS (Overall Survival)

```
Event (CNSR = 0):
  - Death from any cause
  - Source: DS record with DSDECOD = "DEATH"
  - ADT = DS.DSSTDTC of death record
  - EVNTDESC = "DEATH"

Censored (CNSR = 1):
  - Subject alive at analysis cutoff date (DATACUTDT)
  - Subject lost to follow-up
  - Subject withdrew consent (and no death data available)
  - ADT = Last known alive date:
    - Last assessment date (from any domain)
    - Last contact date from DS
  - CNSDTDSC = "ALIVE AT ANALYSIS CUTOFF"
             = "LOST TO FOLLOW-UP"
             = "WITHDREW CONSENT"
```

#### PFS (Progression-Free Survival)

```
Event (CNSR = 0):
  - Disease progression (from RS where RSTESTCD = "PROGRESSION")
  - Death from any cause (from DS where DSDECOD = "DEATH")
  - ADT = Earliest of progression date or death date
  - EVNTDESC = "DISEASE PROGRESSION" or "DEATH"

Censored (CNSR = 1):
  - No progression and alive at analysis cutoff
  - No adequate baseline tumor assessment
  - New anti-cancer therapy started before progression
  - Lost to follow-up before progression
  - ADT = Last tumor assessment date without progression
  - CNSDTDSC = "NO PROGRESSION AT CUTOFF"
             = "NO BASELINE ASSESSMENT"
             = "NEW THERAPY STARTED"
             = "LOST TO FOLLOW-UP"
```

#### TTD (Time to Discontinuation)

```
Event (CNSR = 0):
  - Treatment discontinuation for any reason
  - Source: DS record with DSSCAT = "TREATMENT" and DSDECOD not "COMPLETED"
  - EVNTDESC = "DISCONTINUED - " + DSDECOD

Censored (CNSR = 1):
  - Treatment ongoing at data cutoff
  - Subject completed treatment per protocol
  - ADT = Last dose date (from EX) or data cutoff date
  - CNSDTDSC = "ONGOING AT CUTOFF" or "COMPLETED PER PROTOCOL"
```

#### TTDM (Time to Dose Modification)

```
Event (CNSR = 0):
  - First dose reduction or modification
  - Source: EX records with dose change from planned
  - EVNTDESC = "DOSE MODIFIED"
  - ADT = Date of first dose change

Censored (CNSR = 1):
  - No dose modification at cutoff
  - Treatment discontinued (competing risk)
  - ADT = Last dose date or data cutoff
  - CNSDTDSC = "NO MODIFICATION AT CUTOFF" or "DISCONTINUED WITHOUT MODIFICATION"
```

#### TTR (Time to Response)

```
Event (CNSR = 0):
  - First documented objective response
  - Source: RS where RSTESTCD = "RESPONSE" and RSSTRESC = "CR" or "PR"
  - Must be confirmed by subsequent assessment (per RECIST)
  - EVNTDESC = "OBJECTIVE RESPONSE"
  - ADT = Date of first response assessment

Censored (CNSR = 1):
  - No response at cutoff
  - Death before response
  - New therapy before response
  - ADT = Last disease assessment date
  - CNSDTDSC = "NO RESPONSE AT CUTOFF"
             = "DIED WITHOUT RESPONSE"
             = "NEW THERAPY BEFORE RESPONSE"
```

---

## Generation Patterns

### Oncology Survival Analysis

```json
{
  "dataset": "ADTTE",
  "study": {
    "studyid": "ONC-SURV-001",
    "datacutdt": "2025-01-15"
  },
  "parameters": [
    {
      "paramcd": "OS",
      "param": "Overall Survival (Days)",
      "event_source": "DS where DSDECOD='DEATH'",
      "censoring_source": "Last known alive date from any domain"
    },
    {
      "paramcd": "PFS",
      "param": "Progression-Free Survival (Days)",
      "event_source": "RS progression + DS death",
      "censoring_source": "Last tumor assessment without progression"
    }
  ],
  "origin_date": {
    "startdt": "ADSL.RANDDT",
    "description": "Date of randomization"
  },
  "censoring_patterns": {
    "os_expected_censor_rate": 0.62,
    "pfs_expected_censor_rate": 0.48,
    "pfs_events_breakdown": {
      "progression": 0.75,
      "death_without_progression": 0.25
    }
  }
}
```

### Chronic Disease TTE Endpoints

```json
{
  "dataset": "ADTTE",
  "study": {
    "studyid": "CARD-HF-001",
    "therapeutic_area": "Heart Failure"
  },
  "parameters": [
    {
      "paramcd": "OS",
      "param": "Overall Survival (Days)",
      "event_source": "DS where DSDECOD='DEATH'"
    },
    {
      "paramcd": "TTD",
      "param": "Time to Discontinuation (Days)",
      "event_source": "DS treatment discontinuation events"
    },
    {
      "paramcd": "TTR",
      "param": "Time to Response (Days)",
      "event_source": "LB or VS showing clinical response"
    }
  ]
}
```

---

## Examples

### Example 1: Generate ADTTE for Oncology Phase 3 Trial

**Request:** "Generate ADTTE for 200 subjects with OS and PFS endpoints, including censoring rules, for a randomized oncology trial"

**Output:**

```json
{
  "dataset": "ADTTE",
  "metadata": {
    "studyid": "ONC-PH3-001",
    "description": "Time-to-Event Analysis Dataset - Oncology Phase 3",
    "n_subjects": 200,
    "n_records": 400,
    "datacutdt": "2025-03-31",
    "min_followup_days": 180
  },
  "records": [
    {
      "STUDYID": "ONC-PH3-001",
      "USUBJID": "ONC-PH3-001-001-0042",
      "PARAM": "Overall Survival (Days)",
      "PARAMCD": "OS",
      "STARTDT": "2024-02-01",
      "ADT": "2024-11-15",
      "AVAL": 288,
      "CNSR": 0,
      "EVNTDESC": "DEATH",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": "DS",
      "SRCVAR": "DSSEQ",
      "SRCSEQ": 3
    },
    {
      "STUDYID": "ONC-PH3-001",
      "USUBJID": "ONC-PH3-001-001-0042",
      "PARAM": "Progression-Free Survival (Days)",
      "PARAMCD": "PFS",
      "STARTDT": "2024-02-01",
      "ADT": "2024-09-20",
      "AVAL": 232,
      "CNSR": 0,
      "EVNTDESC": "DISEASE PROGRESSION",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": "RS",
      "SRCVAR": "RSSEQ",
      "SRCSEQ": 5
    },
    {
      "STUDYID": "ONC-PH3-001",
      "USUBJID": "ONC-PH3-001-001-0058",
      "PARAM": "Overall Survival (Days)",
      "PARAMCD": "OS",
      "STARTDT": "2024-02-05",
      "ADT": "2025-03-31",
      "AVAL": 420,
      "CNSR": 1,
      "EVNTDESC": "CENSORED",
      "CNSDTDSC": "ALIVE AT ANALYSIS CUTOFF",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": null,
      "SRCVAR": null,
      "SRCSEQ": null
    },
    {
      "STUDYID": "ONC-PH3-001",
      "USUBJID": "ONC-PH3-001-001-0058",
      "PARAM": "Progression-Free Survival (Days)",
      "PARAMCD": "PFS",
      "STARTDT": "2024-02-05",
      "ADT": "2025-03-15",
      "AVAL": 404,
      "CNSR": 1,
      "EVNTDESC": "CENSORED",
      "CNSDTDSC": "NO PROGRESSION AT CUTOFF",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": null,
      "SRCVAR": null,
      "SRCSEQ": null
    },
    {
      "STUDYID": "ONC-PH3-001",
      "USUBJID": "ONC-PH3-001-002-0105",
      "PARAM": "Overall Survival (Days)",
      "PARAMCD": "OS",
      "STARTDT": "2024-02-18",
      "ADT": "2024-08-05",
      "AVAL": 169,
      "CNSR": 0,
      "EVNTDESC": "DEATH",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": "DS",
      "SRCVAR": "DSSEQ",
      "SRCSEQ": 2
    },
    {
      "STUDYID": "ONC-PH3-001",
      "USUBJID": "ONC-PH3-001-002-0105",
      "PARAM": "Progression-Free Survival (Days)",
      "PARAMCD": "PFS",
      "STARTDT": "2024-02-18",
      "ADT": "2024-06-30",
      "AVAL": 133,
      "CNSR": 0,
      "EVNTDESC": "DISEASE PROGRESSION",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": "RS",
      "SRCVAR": "RSSEQ",
      "SRCSEQ": 4
    },
    {
      "STUDYID": "ONC-PH3-001",
      "USUBJID": "ONC-PH3-001-003-0152",
      "PARAM": "Overall Survival (Days)",
      "PARAMCD": "OS",
      "STARTDT": "2024-03-01",
      "ADT": "2025-03-31",
      "AVAL": 396,
      "CNSR": 1,
      "EVNTDESC": "CENSORED",
      "CNSDTDSC": "ALIVE AT ANALYSIS CUTOFF",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": null,
      "SRCVAR": null,
      "SRCSEQ": null
    },
    {
      "STUDYID": "ONC-PH3-001",
      "USUBJID": "ONC-PH3-001-003-0152",
      "PARAM": "Progression-Free Survival (Days)",
      "PARAMCD": "PFS",
      "STARTDT": "2024-03-01",
      "ADT": "2025-03-31",
      "AVAL": 396,
      "CNSR": 1,
      "EVNTDESC": "CENSORED",
      "CNSDTDSC": "NO PROGRESSION AT CUTOFF",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": null,
      "SRCVAR": null,
      "SRCSEQ": null
    }
  ],
  "summary": {
    "os": {
      "n": 200,
      "n_events": 76,
      "n_censored": 124,
      "censor_rate": 0.62,
      "median_survival_days": null,
      "median_followup_days": 365
    },
    "pfs": {
      "n": 200,
      "n_events": 104,
      "n_censored": 96,
      "censor_rate": 0.48,
      "event_breakdown": {
        "disease_progression": 78,
        "death_without_progression": 26
      }
    }
  }
}
```

### Example 2: ADTTE with Lost to Follow-Up Censoring

**Request:** "Generate ADTTE showing OS and PFS for a subject who was lost to follow-up"

**Output:**

```json
{
  "dataset": "ADTTE",
  "metadata": {
    "studyid": "LOST-FU-001",
    "description": "ADTTE records with lost-to-follow-up censoring pattern"
  },
  "records": [
    {
      "STUDYID": "LOST-FU-001",
      "USUBJID": "LOST-FU-001-001-0015",
      "PARAM": "Overall Survival (Days)",
      "PARAMCD": "OS",
      "STARTDT": "2024-01-10",
      "ADT": "2024-06-30",
      "AVAL": 172,
      "CNSR": 1,
      "EVNTDESC": "CENSORED",
      "CNSDTDSC": "LOST TO FOLLOW-UP",
      "STARTDTYP": "RANDDATE",
      "notes": "Subject lost to follow-up after month 6 visit. Last known alive on 2024-06-30."
    },
    {
      "STUDYID": "LOST-FU-001",
      "USUBJID": "LOST-FU-001-001-0015",
      "PARAM": "Progression-Free Survival (Days)",
      "PARAMCD": "PFS",
      "STARTDT": "2024-01-10",
      "ADT": "2024-06-15",
      "AVAL": 157,
      "CNSR": 1,
      "EVNTDESC": "CENSORED",
      "CNSDTDSC": "LOST TO FOLLOW-UP",
      "STARTDTYP": "RANDDATE",
      "notes": "Last tumor assessment without progression on 2024-06-15."
    }
  ]
}
```

### Example 3: Time to Discontinuation for Dose-Finding Study

**Request:** "Generate ADTTE for a dose-finding study with TTD and TTDM endpoints"

**Output:**

```json
{
  "dataset": "ADTTE",
  "metadata": {
    "studyid": "DOSE-FIND-002",
    "description": "Time-to-event for treatment continuation and dose modification"
  },
  "records": [
    {
      "STUDYID": "DOSE-FIND-002",
      "USUBJID": "DOSE-FIND-002-001-0008",
      "PARAM": "Time to Discontinuation (Days)",
      "PARAMCD": "TTD",
      "STARTDT": "2024-04-01",
      "ADT": "2024-07-15",
      "AVAL": 106,
      "CNSR": 0,
      "EVNTDESC": "DISCONTINUED - ADVERSE EVENT",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": "DS",
      "SRCVAR": "DSSEQ",
      "SRCSEQ": 3
    },
    {
      "STUDYID": "DOSE-FIND-002",
      "USUBJID": "DOSE-FIND-002-001-0008",
      "PARAM": "Time to Dose Modification (Days)",
      "PARAMCD": "TTDM",
      "STARTDT": "2024-04-01",
      "ADT": "2024-05-30",
      "AVAL": 60,
      "CNSR": 0,
      "EVNTDESC": "DOSE MODIFIED",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": "EX",
      "SRCVAR": "EXSEQ",
      "SRCSEQ": 4
    },
    {
      "STUDYID": "DOSE-FIND-002",
      "USUBJID": "DOSE-FIND-002-002-0033",
      "PARAM": "Time to Discontinuation (Days)",
      "PARAMCD": "TTD",
      "STARTDT": "2024-04-05",
      "ADT": "2024-12-31",
      "AVAL": 271,
      "CNSR": 1,
      "EVNTDESC": "CENSORED",
      "CNSDTDSC": "ONGOING AT CUTOFF",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": null,
      "SRCVAR": null,
      "SRCSEQ": null
    },
    {
      "STUDYID": "DOSE-FIND-002",
      "USUBJID": "DOSE-FIND-002-002-0033",
      "PARAM": "Time to Dose Modification (Days)",
      "PARAMCD": "TTDM",
      "STARTDT": "2024-04-05",
      "ADT": "2024-12-31",
      "AVAL": 271,
      "CNSR": 1,
      "EVNTDESC": "CENSORED",
      "CNSDTDSC": "COMPLETED WITHOUT MODIFICATION",
      "STARTDTYP": "RANDDATE",
      "SRCDOM": null,
      "SRCVAR": null,
      "SRCSEQ": null
    }
  ]
}
```

---

## Validation Rules

| Rule | Requirement | Example |
|------|-------------|---------|
| PARAMCD | Must be one of: OS, PFS, TTD, TTDM, TTR | OS |
| STARTDT | Non-null, consistent per subject across parameters | 2024-02-01 |
| ADT | Must be >= STARTDT for event and censored records | 2024-11-15 |
| AVAL | ADT - STARTDT + 1, must be >= 1 | 288 |
| CNSR | 0 or 1 only; must match EVNTDESC presence | 0 |
| EVNTDESC | Populated when CNSR=0; "CENSORED" when CNSR=1 | DEATH |
| CNSDTDSC | Populated only when CNSR=1 | ALIVE AT ANALYSIS CUTOFF |
| Traceability | SRCDOM/SRCVAR/SRCSEQ populated when CNSR=0 | DS/DSSEQ/3 |
| Per subject | Two records per subject when OS+PFS both defined | 1 OS, 1 PFS |
| STARTDTYP | From CDISC controlled terminology | RANDDATE |

### Business Rules

- **Two Records Per Subject (Oncology)**: Each subject typically has one OS record and one PFS record. Additional parameters (TTD, TTDM, TTR) add more records per subject.
- **STARTDT Consistency**: All TTE parameters for the same subject must share the same STARTDT value. This ensures consistent time origin across endpoints.
- **Censoring Date Hierarchy**: For analysis cutoff censoring, ADT = data cutoff date (DATACUTDT). For loss-to-follow-up, ADT = last known contact date. The censoring date must be the earliest of the last known alive date and the data cutoff date.
- **OS Events Before PFS Events**: If a subject dies, the death date is the ADT for OS (CNSR=0). For PFS, if progression was not previously documented, the death date becomes the PFS event date (CNSR=0, EVNTDESC="DEATH").
- **Competing Risks**: Events that preclude observation of the primary endpoint (e.g., new therapy before progression) are handled as censored observations, not events. The censoring reason must be documented.
- **+1 Convention**: AVAL = ADT - STARTDT + 1 ensures subjects with same-day events have AVAL = 1 (important for Kaplan-Meier method). Confirm this convention matches the SAP.
- **Traceability for Events**: Every event record (CNSR=0) must be traceable to at least one SDTM source record. Censored records (CNSR=1) may have null SRCDOM/SRCVAR/SRCSEQ if the censoring is due to data cutoff.
- **Follow-Up Minimum**: Regulatory oncology submissions typically require a minimum follow-up time (e.g., 6 months). AVAL values should reflect adequate follow-up.

---

## Related Skills

### TrialSim ADaM Datasets
- [README.md](README.md) - ADaM skills directory
- [adsl.md](adsl.md) - ADSL (RANDDT, SAFFL, COMPLFL used by ADTTE)
- [adae.md](adae.md) - ADAE (AEs leading to death/discontinuation)
- [adeff.md](adeff.md) - ADEFF (tumor response assessments, RECIST data)

### TrialSim SDTM Domains
- [../../domains/disposition-ds.md](../../domains/disposition-ds.md) - DS domain (DEATH, discontinuation events)
- [../../domains/adverse-events-ae.md](../../domains/adverse-events-ae.md) - AE domain (AESDTH for death events)
- [../../domains/exposure-ex.md](../../domains/exposure-ex.md) - EX domain (dose modifications)

### TrialSim Core
- [../../clinical-trials-domain.md](../../clinical-trials-domain.md) - Oncology trial endpoints
- [../../phase3-pivotal.md](../../phase3-pivotal.md) - Phase 3 survival analysis patterns

### Formats
- [../../../formats/cdisc-adam.md](../../../formats/cdisc-adam.md) - ADaM format specification

> **Integration Pattern:** ADTTE combines event data from multiple SDTM domains (DS for death, RS for progression, EX for dose changes) into a unified TTE structure. The STARTDT (= RANDDT from ADSL) serves as the common origin for all time-to-event calculations. Censoring rules must be pre-specified in the SAP.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12 | Initial ADTTE dataset skill with OS, PFS, TTD, TTDM, TTR endpoints and comprehensive censoring rules |
