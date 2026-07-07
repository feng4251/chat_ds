---
name: csv-export
description: |
  CSV export specification for regulatory submission datasets. Covers SDTM/ADaM
  variable name headers, delimiter and quoting rules, UTF-8 without BOM encoding,
  LF line endings per FDA eCTD standards, null/missing value representation, one
  file per domain convention, data dictionary CSV format, row chunking for large
  datasets, and validation checks. Triggers: "CSV export", "flat file", "data
  dictionary", "spreadsheet format", "tabular export".
---

# CSV Export Specification

Specification for exporting SDTM and ADaM clinical trial datasets as comma-separated value (CSV) files compliant with FDA regulatory submission standards.

---

## For Claude

This is the **CSV export format specification** for TrialSim. When generating CSV files for regulatory submission or spreadsheet analysis, follow the conventions in this document.

**Always apply this specification when:**
- Exporting SDTM domain data as CSV files
- Generating data dictionaries for regulatory submission
- Preparing datasets for spreadsheet-based review
- Chunking large domain files to stay within row limits
- Validating CSV output for eCTD compliance

---

## Column Header Format

### SDTM Variable Names as Headers

CSV files must use **SDTM variable names as column headers** in the first row. All headers must be uppercase and must match the variable names defined in the [CDISC SDTM specification](cdisc-sdtm.md).

**Example header row (DM domain):**

```
STUDYID,DOMAIN,USUBJID,SUBJID,RFSTDTC,RFENDTC,SITEID,AGE,AGEU,SEX,RACE,ETHNIC,ARMCD,ARM,COUNTRY
```

**Example header row (AE domain):**

```
STUDYID,DOMAIN,USUBJID,AESEQ,AETERM,AEDECOD,AEBODSYS,AESEV,AESER,AEREL,AEOUT,AEACN,AESTDTC,AEENDTC
```

### Column Ordering

For regulatory compliance, follow the SDTM IG variable order within each domain:
1. Identifier variables (STUDYID, DOMAIN, USUBJID)
2. Topic variables (AETERM, CMTRT, LBTEST)
3. Qualifier variables (AESEV, CMDOSE, LBNRIND)
4. Timing variables (AESTDTC, VISITNUM, VISIT)

---

## Delimiter

| Property | Value |
|----------|-------|
| **Default delimiter** | Comma (`,`) |
| **Alternative delimiter** | Tab (`\t`) -- available as a CLI option |
| **Tab-delimited extension** | `.tsv` |

**Rule:** Use the comma delimiter by default for regulatory submissions. Tab-delimited output is available for data review and analytics, but the primary submission format uses commas.

### Tab-Delimited Option

When requested (e.g., for easy import into spreadsheet tools), output tab-separated files with `.tsv` extension:

```
"Generate AE domain as tab-separated file"
→ ae.tsv with tab (\t) delimiter
```

---

## Quote Character

| Property | Value |
|----------|-------|
| **Quote character** | Double quote (`"`) |
| **Escape sequence** | Double-double quote (`""`) within quoted strings |

### Quoting Rules

Quoting is **required** when a cell value contains any of the following characters:

| Character | Example | Quoted Form |
|-----------|---------|-------------|
| Comma (,) | `Headache, tension-type` | `"Headache, tension-type"` |
| Newline (\n) | Multi-line text | `"Line 1\nLine 2"` |
| Double quote (") | `He said "stop"` | `"He said ""stop"""` |
| Leading/trailing space | ` moderate ` | `" moderate "` |

Fields that do not contain these special characters may be unquoted.

### Implementation

```python
def csv_quote(value: str) -> str:
    """Quote a CSV cell value per FDA SDTCG rules."""
    if value is None or value == "":
        return ""  # Empty field (no quoting needed)
    s = str(value)
    if any(c in s for c in [',', '\n', '"']) or s != s.strip():
        return '"' + s.replace('"', '""') + '"'
    return s
```

---

## Encoding

| Property | Value |
|----------|-------|
| **Character encoding** | UTF-8 without BOM |
| **Reference** | FDA SDTCG (Study Data Technical Conformance Guide) Section 3.1 |
| **Rationale** | BOM can cause issues with FDA's Janus Clinical Trial Repository ingestion tools |

### Verification

Confirm encoding with:
```bash
file -I dm.csv
# Expected: dm.csv: text/plain; charset=utf-8
```

**Rule:** Never include a Byte Order Mark (BOM). The first byte of the file must be the first character of the first column header.

---

## Line Endings

| Property | Value |
|----------|-------|
| **Line ending** | LF (`\n`, Unix-style, ASCII 0x0A) |
| **Forbidden** | CRLF (`\r\n`, Windows-style) |
| **Reference** | FDA eCTD M4 specification, ICH M8 guideline |

### Rationale

While the eCTD specification technically accepts either line ending, the FDA's Janus Data Validation tools are built on Unix/Linux systems and LF is the recommended format. Consistent use of LF avoids ingestion errors.

### Verification

Confirm line endings with:
```bash
file dm.csv
# Expected: dm.csv: UTF-8 Unicode text
# NOT: UTF-8 Unicode text, with CRLF line terminators

# Alternative check:
cat -A dm.csv | head -1
# Expect: STUDYID,DOMAIN,USUBJID$  ( $ indicates LF, not ^M$ )
```

---

## Missing Value Representation

### Null/Missing Cells

| Scenario | Representation | Example |
|----------|---------------|---------|
| **True null** (no value) | Empty field `,,` | `,` between adjacent commas |
| **Character string with null flavor** | SDTM null flavor value | `"NOT DONE"` |
| **Numeric missing** | Empty field `,,` | Not `NA` or `NULL` |

### SDTM Null Flavors

When a reason for missing data is known, use the appropriate SDTM null flavor text:

| Null Flavor Code | Meaning | Usage |
|------------------|---------|-------|
| NOT DONE | Procedure not performed | LBSTAT, VSSTAT |
| UNKNOWN | Value not known | Any variable |
| NOT COLLECTED | Data not collected per protocol | Any variable |
| NOT APPLICABLE | Not relevant for this subject | Any variable |
| PENDING | Data pending | Any variable |

**Example (LB domain):**
```
LBORRES,,LBTEST
,NOT DONE,ALT
,UNKNOWN,GLUC
```

An empty field without a null flavor is acceptable when no information about the missing value is available.

---

## One File Per Domain

Each SDTM domain must be exported to a separate CSV file:

| Domain | Filename | Description |
|--------|----------|-------------|
| DM | `dm.csv` | Demographics |
| AE | `ae.csv` | Adverse Events |
| CM | `cm.csv` | Concomitant Medications |
| VS | `vs.csv` | Vital Signs |
| LB | `lb.csv` | Laboratory Test Results |
| EX | `ex.csv` | Exposure |
| DS | `ds.csv` | Disposition |
| MH | `mh.csv` | Medical History |
| DV | `dv.csv` | Protocol Deviations |
| SE | `se.csv` | Subject Elements |
| SV | `sv.csv` | Subject Visits |
| SUPP-- | `suppXX.csv` | Supplemental Qualifiers (one per parent domain) |

### File Naming Convention

```
<domain_code>.csv

All lowercase domain codes.
Supplemental qualifier files: suppXX.csv where XX is the parent domain code.
Example: suppae.csv for AE supplemental qualifiers.
```

---

## Data Dictionary CSV Format

Each domain CSV export must be accompanied by a data dictionary CSV defining all variables. The data dictionary follows this schema:

### Data Dictionary Columns

| Column | Description | Example |
|--------|-------------|---------|
| **Variable** | SDTM variable name | AESEV |
| **Label** | Human-readable description | Severity/Intensity |
| **Type** | Data type | Char, Num |
| **Length** | Variable length or precision | 200 |
| **Codelist** | Controlled terminology reference | C66769 |
| **Origin** | Source of the data | CRF, Derived, Assigned |
| **Role** | SDTM variable role | Qualifier |
| **Domain** | Parent domain code | AE |
| **Notes** | Any additional notes | Null flavor allowed: NOT DONE |

### Example: AE Data Dictionary

```csv
Variable,Label,Type,Length,Codelist,Origin,Role,Domain,Notes
STUDYID,Study Identifier,Char,20,,Assigned,Identifier,AE,
DOMAIN,Domain Abbreviation,Char,2,,Assigned,Identifier,AE,Fixed value "AE"
USUBJID,Unique Subject Identifier,Char,40,Derived,Identifier,AE,"From DM domain"
AESEQ,Sequence Number,Num,8,,Assigned,Identifier,AE,Unique within subject
AETERM,Reported Term for AE,Char,200,,CRF,Topic,AE,Verbatim term
AEDECOD,Dictionary-Derived Term,Char,200,MedDRA,Derived,Topic,AE,MedDRA Preferred Term
AEBODSYS,Body System Organ Class,Char,200,MedDRA,Derived,Qualifier,AE,MedDRA SOC
AESEV,Severity Intensity,Char,20,C66769,CRF,Qualifier,AE,MILD MODERATE SEVERE
AESER,Serious Event,Char,1,C66742,CRF,Qualifier,AE,Y N
```

### Data Dictionary Naming

```
<domain_code>_dict.csv

Example: ae_dict.csv, dm_dict.csv
```

---

## Row Limits and Chunking

### Row Capacity

| Limit | Value | Rationale |
|-------|-------|-----------|
| **Recommended max rows per file** | 1,000,000 | Practical limit for regulatory review tools |
| **Microsoft Excel limit** | 1,048,576 | Including header row |
| **SAS dataset limit** | 2,147,483,647 | Theoretical; practical limits are smaller |

### Chunking Strategy for Large Datasets

When a domain exceeds 1,000,000 rows, split into chunks:

```
lb_part1.csv    (rows 1-1,000,000)
lb_part2.csv    (rows 1,000,001-2,000,000)
lb_part3.csv    (rows 2,000,001-N)
```

**Chunking rules:**
1. Each chunk includes the full header row
2. Chunk boundaries must not split a subject's records (all records for a given USUBJID stay together in the same chunk)
3. Chunk files are named `<domain>_partN.csv`
4. The data dictionary covers all chunks (a single `lb_dict.csv` is sufficient)

### Large Domain Considerations

| Domain | Typical Rows Per Subject | Potential for Large Files |
|--------|--------------------------|---------------------------|
| DM | 1 | Low -- one record per subject |
| AE | 1-20 | Medium |
| CM | 1-15 | Medium |
| VS | 5-50 (per visit, per test) | High |
| LB | 10-40 (per visit, per test) | **High** (most likely to need chunking) |
| EX | 1-60 (per dosing event) | Medium-High |
| DS | 2-10 | Low |
| MH | 1-20 | Medium |

---

## Validation

### Validation Checks

Before finalizing CSV exports, run the following validation:

#### 1. Row Count Verification

```
Expected: N rows in CSV = N records in source data + 1 header row
Check:    wc -l dm.csv → should equal N + 1
```

#### 2. Column Count Consistency

```
Check: Every data row has the same number of columns as the header row.
Use:   awk -F',' '{print NF}' dm.csv | sort -u
       → Must return a single number (all rows match header column count)
```

#### 3. Header Validation

```
Check: All required variables are present in the header.
For DM: STUDYID, DOMAIN, USUBJID must be present.
For AE: STUDYID, DOMAIN, USUBJID, AESEQ, AETERM, AEDECOD must be present.
```

#### 4. Encoding Verification

```
Check: No BOM present.
Verify: hexdump -C dm.csv | head -1
        → First bytes should be "S" (0x53), not EF BB BF
```

#### 5. Line Ending Check

```
Check: No carriage returns (\r) present.
Verify: grep -c $'\r' dm.csv
        → Must return 0
```

#### 6. USUBJID Referential Integrity

```
Check: Every USUBJID in child domains exists in DM.
Pseudocode:
  dm_usubjids = set of USUBJID from dm.csv
  ae_usubjids = set of USUBJID from ae.csv
  orphans = ae_usubjids - dm_usubjids
  assert len(orphans) == 0
```

#### 7. Controlled Terminology Compliance

```
Check: All controlled-terminology fields contain valid codes.
For AE.SEX: must be in {M, F, U}
For AE.AESEV: must be in {MILD, MODERATE, SEVERE}
```

### Validation Script

```python
import csv
import os

def validate_domain_csv(filepath: str, required_cols: list[str],
                        controlled_terms: dict[str, set[str]] = None) -> dict:
    """Validate a domain CSV file for regulatory compliance."""
    results = {"errors": [], "warnings": [], "row_count": 0}

    # Check BOM
    with open(filepath, 'rb') as f:
        if f.read(3) == b'\xef\xbb\xbf':
            results["errors"].append("UTF-8 BOM detected -- remove BOM")

    # Check CRLF
    with open(filepath, 'rb') as f:
        content = f.read()
        if b'\r\n' in content:
            results["errors"].append("CRLF line endings detected -- use LF only")

    # Parse CSV
    with open(filepath, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames

        if not headers:
            results["errors"].append("No headers found")
            return results

        # Check required columns
        missing = [c for c in required_cols if c not in headers]
        if missing:
            results["errors"].append(f"Missing required columns: {missing}")

        results["row_count"] = sum(1 for _ in reader)

    return results
```

---

## Related Specifications

| Topic | File | Description |
|-------|------|-------------|
| SDTM Master Format | [formats/cdisc-sdtm.md](cdisc-sdtm.md) | CDISC SDTM variable definitions |
| ADaM Format | [formats/cdisc-adam.md](cdisc-adam.md) | Analysis dataset format |
| Dimensional Analytics | [formats/dimensional-analytics.md](dimensional-analytics.md) | Star schema for BI tools |
| Data Models | [references/data-models.md](../references/data-models.md) | Canonical entity schemas |

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-06 | Initial CSV export specification for regulatory submission |
