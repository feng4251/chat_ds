---
name: cdisc-adam-format
description: |
  Master ADaM format specification for analysis dataset generation. ADaM IG 1.2 
  compliant datasets (ADSL, ADAE, ADLB, ADEFF, ADTTE) with traceability to 
  SDTM source domains. Triggers: "ADaM", "analysis dataset", "ADSL", "ADAE", 
  "ADLB", "ADEFF", "ADTTE", "analysis data model", "BDS structure", 
  "subject-level analysis", "time-to-event", "efficacy analysis".
---

# CDISC ADaM Format

The Analysis Data Model (ADaM) specifies the structure and content of analysis-ready datasets derived from SDTM source data. ADaM datasets support the statistical analysis plan (SAP) with traceability back to SDTM records and are organized by analysis purpose rather than data collection domain.

---

## For Claude

This is the **master ADaM format specification** for generating analysis datasets. All ADaM dataset skills reference this format.

**Always apply this skill when you see:**
- Requests for analysis-ready clinical trial data
- ADaM dataset generation (ADSL, ADAE, ADLB, ADEFF, ADTTE)
- Statistical analysis of clinical trial endpoints
- CDISC submission package assembly
- Traceability requirements from SDTM to analysis
- Derived variable specifications (change from baseline, treatment-emergent flags)

**Key responsibilities:**
- Enforce ADaM IG 1.2 core principles: Traceability, Analyzability, Metadata-driven generation
- Define standard ADaM dataset classes and their structure
- Specify traceability rules from every ADaM record back to SDTM source
- Govern variable naming conventions across all ADaM datasets
- Define derivation types: Copy, Simple, and Complex

---

## ADaM Core Principles

### 1. Traceability

Every ADaM record must be traceable back to its SDTM source records. This enables regulators to verify how analysis results are derived:

| Traceability Element | Description | Source |
|----------------------|-------------|--------|
| SRCDOM | Source SDTM domain | Two-character domain code (AE, LB, DS, etc.) |
| SRCVAR | Source SDTM variable | Variable that primary value came from |
| SRCSEQ | Source sequence number | The sequence number in the source domain |
| DTYPE | Derivation Type | Describes how record was created |

### 2. Analyzability

Datasets must be structured for immediate statistical analysis:
- One record per subject per analysis parameter per analysis visit (BDS structure)
- Numeric variables for all analysis values
- Ready-to-use flags for population subsets (SAFFL, ITTFL, PPROTFL)
- Standardized analysis visit windows

### 3. Metadata-Driven Generation

All variable derivations must be documented in a Define-XML metadata specification:
- Variable-level metadata (label, type, length, derivation)
- Value-level metadata (codelist, computation method)
- Analysis parameter definitions (PARAM, PARAMCD)

---

## ADaM Dataset Classes

### ADSL - Subject-Level Analysis Dataset

**Structure:** One record per subject. Flat, non-BDS structure.
**Purpose:** Subject-level population flags, baseline characteristics, treatment assignment, and disposition information.
**Source SDTM Domains:** DM, DS, EX, MH

### BDS - Basic Data Structure

**Structure:** One record per subject per analysis parameter per analysis visit.
**Purpose:** Repeated measures with analysis-ready derivation variables.
**Datasets using BDS:**
- **ADAE** - Adverse Event Analysis Dataset (source: AE)
- **ADLB** - Laboratory Analysis Dataset (source: LB)
- **ADEFF** - Efficacy Analysis Dataset (source: LB, VS, QS)

**BDS Required Variables:**

| Variable | Label | Type | Length | Description |
|----------|-------|------|--------|-------------|
| STUDYID | Study Identifier | Char | 20 | Study identifier |
| USUBJID | Unique Subject Identifier | Char | 40 | Unique subject ID from DM |
| PARAM | Parameter | Char | 200 | Description of analysis parameter |
| PARAMCD | Parameter Code | Char | 8 | Short parameter code |
| AVAL | Analysis Value | Num | 8 | Analysis value |
| AVALC | Analysis Value (Character) | Char | 200 | Analysis value (character) |
| ADT | Analysis Date | Num | 8 | Analysis date (SAS date) |
| ADY | Analysis Relative Day | Num | 8 | Study day relative to reference |

### ADTTE - Time-to-Event Analysis Dataset

**Structure:** One record per subject per event type.
**Purpose:** Time-to-event endpoints for survival analysis.
**Source SDTM Domains:** AE, DS, RS, LB, TU

---

## Standard ADaM Variables (Across All Datasets)

These variables appear in every ADaM dataset:

| Variable | Label | Type | Length | Description |
|----------|-------|------|--------|-------------|
| STUDYID | Study Identifier | Char | 20 | Unique study identifier |
| USUBJID | Unique Subject Identifier | Char | 40 | STUDYID-SITEID-SUBJID |
| SUBJID | Subject Identifier for Study | Char | 20 | Subject ID within study |
| SITEID | Study Site Identifier | Char | 10 | Site number |
| TRT01P | Planned Treatment for Period 01 | Char | 200 | Planned treatment description |
| TRT01PN | Planned Treatment for Period 01 (N) | Num | 8 | Numeric treatment code |
| TRT01A | Actual Treatment for Period 01 | Char | 200 | Actual treatment description |
| TRT01AN | Actual Treatment for Period 01 (N) | Num | 8 | Numeric actual treatment code |
| SAFFL | Safety Population Flag | Char | 1 | Y = included in safety population |
| ITTFL | Intent-to-Treat Population Flag | Char | 1 | Y = included in ITT population |
| PPROTFL | Per-Protocol Population Flag | Char | 1 | Y = included in per-protocol population |
| COMPLFL | Completers Population Flag | Char | 1 | Y = completed study per protocol |

---

## Traceability Rules

Every ADaM record that is derived from SDTM must carry traceability variables:

| ADaM Variable | Maps To | Description |
|---------------|---------|-------------|
| SRCDOM | SDTM Domain | Two-character domain code: AE, LB, DS, VS, RS |
| SRCVAR | SDTM Variable | The variable holding the source value (e.g., AESTDTC, LBSTRESN) |
| SRCSEQ | SDTM Sequence | The --SEQ value in the source domain |

**Traceability Rule:** For each ADaM record that is a direct copy from SDTM:
```
SRCDOM = Source domain abbreviation
SRCVAR = Source variable name
SRCSEQ = Source sequence number
```

For derived records (not a direct copy):
```
SRCDOM = [blank if purely derived across sources]
SRCVAR = [blank if purely derived]
DTYPE = Derivation type (LOCF, AVERAGE, etc.)
```

---

## ADaM Variable Naming Conventions

| Convention | Pattern | Examples |
|------------|---------|----------|
| Population Flags | XXXFL (Y/N) | SAFFL, ITTFL, PPROTFL |
| Baseline Values | BASE, BASEC | BASE (numeric), BASEC (character) |
| Change from Baseline | CHG | AVAL - BASE |
| Percent Change | PCHG | (AVAL - BASE) / BASE * 100 |
| Record-Level Flags | XXyyFL | ANL01FL (analysis visit 01), TRTEMFL (treatment emergent) |
| Sequence Numbers | XXSEQ | ASEQ, LBSEQ |
| Numeric Equivalents | XXyN (N suffix) | AGEGR1N, TRT01PN |
| Shift Variables | SHIFTx | SHIFT1 (baseline to analysis shift) |
| Criterion Flags | CRITx, CRITxFL | CRIT1, CRIT1FL |

---

## Derivation Types

### Copy from SDTM
Direct copy without transformation. Example: ADAE.AETERM copied from AE.AETERM.

### Simple Derivation
Univariate transformation: age grouping, change from baseline, flag derivation.
Example: ADSL.AGEGR1 = "<65" when AGE < 65.

### Complex Derivation (Algorithm Required)
Multi-source derivation requiring documented algorithm. Example: ADSL.BASE_HBA1C derived from last non-missing LB result before first dose date.

---

## ADaM Submission Package Structure

```
adam/
  datasets/
    adsl.xpt        - Subject-Level Analysis Dataset
    adae.xpt        - Adverse Events Analysis Dataset
    adlb.xpt        - Laboratory Analysis Dataset
    adeff.xpt       - Efficacy Analysis Dataset
    adtte.xpt       - Time-to-Event Analysis Dataset
  define/
    define_adam.xml - ADaM Define-XML metadata
  programs/
    adsl.sas        - ADSL generation program
    adae.sas        - ADAE generation program
    adlb.sas        - ADLB generation program
    adeff.sas       - ADEFF generation program
    adtte.sas       - ADTTE generation program
```

---

## Controlled Terminology

### DTYPE (Derivation Type) - ADaM Codelist

| Value | Description |
|-------|-------------|
| [blank] | Direct copy from SDTM |
| LOCF | Last Observation Carried Forward |
| WOCF | Worst Observation Carried Forward |
| AVERAGE | Average of multiple values |
| MINIMUM | Minimum of multiple values |
| MAXIMUM | Maximum of multiple values |
| DERIVED | Complex derivation from multiple sources |

### Population Flag Values

| Value | Meaning |
|-------|---------|
| Y | Included in population |
| N | Not included in population |

---

## Related Skills

### TrialSim ADaM Dataset Skills
- [../skills/adam/README.md](../skills/adam/README.md) - ADaM skills directory
- [../skills/adam/adsl.md](../skills/adam/adsl.md) - Subject-Level Analysis Dataset
- [../skills/adam/adae.md](../skills/adam/adae.md) - Adverse Event Analysis Dataset
- [../skills/adam/adlb.md](../skills/adam/adlb.md) - Laboratory Analysis Dataset
- [../skills/adam/adeff.md](../skills/adam/adeff.md) - Efficacy Analysis Dataset
- [../skills/adam/adtte.md](../skills/adam/adtte.md) - Time-to-Event Analysis Dataset

### SDTM Domain Skills (Source Data)
- [../domains/README.md](../domains/README.md) - SDTM domains overview
- [../domains/demographics-dm.md](../domains/demographics-dm.md) - DM domain (feeds ADSL)
- [../domains/adverse-events-ae.md](../domains/adverse-events-ae.md) - AE domain (feeds ADAE, ADTTE)
- [../domains/laboratory-lb.md](../domains/laboratory-lb.md) - LB domain (feeds ADLB, ADEFF)
- [../domains/disposition-ds.md](../domains/disposition-ds.md) - DS domain (feeds ADSL, ADTTE)
- [../domains/vital-signs-vs.md](../domains/vital-signs-vs.md) - VS domain (feeds ADEFF)

### External References
- [CDISC ADaM IG 1.2](https://www.cdisc.org/standards/foundational/adam) - Official implementation guide
- [CDISC ADaM OCCDS](https://www.cdisc.org/standards/foundational/adam/adam-occds) - Occurrence Data Structure

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12 | Initial ADaM format specification covering IG 1.2 core principles, dataset classes, traceability rules, and variable naming conventions |
