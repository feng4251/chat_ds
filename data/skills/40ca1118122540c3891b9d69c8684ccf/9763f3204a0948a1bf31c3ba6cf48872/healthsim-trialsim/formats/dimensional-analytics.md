---
name: dimensional-analytics
description: |
  Dimensional (star schema) specification for clinical trial BI dashboards and
  operational analytics. Defines 7 dimension tables and 6 fact tables for
  DuckDB (primary), PostgreSQL, and Databricks. Covers DDL, ETL from SDTM JSON,
  surrogate key strategy, slowly changing dimensions, sample analytic queries,
  and BI tool integration. Triggers: "star schema", "dimensional model",
  "analytics", "DuckDB", "BI dashboard", "OLAP cube for trials".
---

# Dimensional Analytics — Clinical Trial Star Schema

This specification defines the dimensional star schema for TrialSim-generated clinical trial data. It is the primary reference for building analytical databases that power operational dashboards, safety monitoring, enrollment tracking, and efficacy analysis in BI tools such as Metabase, Tableau, and Streamlit.

---

## For Claude

This is the **canonical dimensional model** for TrialSim. Use this skill whenever a user requests trial data in a form suitable for dashboards, OLAP queries, or operational analytics.

**Always apply this skill when you see:**
- "star schema", "dimensional model", "fact table", "dimension table"
- "DuckDB analytics", "OLAP cube for clinical trials"
- "BI dashboard data model", "trial operations dashboard"
- "enrollment funnel", "AE rate by SOC", "lab shift table"
- Any request to generate data for Metabase, Tableau, or Streamlit
- TrialSim SDTM data that needs to be reshaped for analytics

**Key responsibilities:**
- Produce 13 tables (7 dimension + 6 fact) with complete DDL
- Map every dimension/fact column to source SDTM variables
- Generate DuckDB-compatible SQL (primary target)
- Provide ETL pseudocode for populating from SDTM JSON
- Include executable analytical queries for each fact table
- Document BI tool connection patterns for Metabase, Tableau, Streamlit

**Output behavior:**
- When the user asks for "star schema" or "dimensional" output, generate the full 13-table DDL plus INSERT statements
- When the user asks for a specific dashboard metric, provide the relevant analytical SQL from the examples section
- Always include surrogate key generation (auto-increment) for DuckDB

---

## Star Schema Overview

The model uses a classic star schema with 7 dimension tables surrounding 6 fact tables.

```
                         ┌──────────────────┐
                         │    dim_study      │
                         └────────┬─────────┘
                                  │
         ┌────────────────────────┼────────────────────────┐
         │                        │                        │
         ▼                        ▼                        ▼
┌─────────────────┐    ┌──────────────────┐    ┌───────────────────┐
│    dim_site     │    │ dim_treatment_arm│    │ dim_visit_schedule │
└────────┬────────┘    └────────┬─────────┘    └─────────┬─────────┘
         │                      │                        │
         ▼                      ▼                        ▼
┌──────────────────────────────────────────────────────────────────┐
│                         FACT TABLES                              │
│  fact_enrollment | fact_visit | fact_adverse_event              │
│  fact_exposure   | fact_efficacy | fact_lab_result              │
└──────────────────────────┬───────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
          ▼                ▼                ▼
 ┌───────────────┐ ┌──────────────┐ ┌──────────────┐
 │  dim_subject  │ │  dim_meddra  │ │ dim_lab_test │
 └───────────────┘ └──────────────┘ └──────────────┘
```

### Table Inventory

| Table | Type | Grain | Rows (Phase III, 100 subj) |
|-------|------|-------|----------------------------|
| dim_study | Dimension | 1 row per study | 1 |
| dim_site | Dimension | 1 row per site | ~10 |
| dim_subject | Dimension | 1 row per subject | 100 |
| dim_treatment_arm | Dimension | 1 row per arm per study | 3–4 |
| dim_visit_schedule | Dimension | 1 row per visit per study | ~12 |
| dim_meddra | Dimension | 1 row per MedDRA PT | ~200 |
| dim_lab_test | Dimension | 1 row per lab test | ~30 |
| fact_enrollment | Fact | 1 row per enrollment | 100 |
| fact_visit | Fact | 1 row per actual visit | ~1,200 |
| fact_adverse_event | Fact | 1 row per AE | ~400–800 |
| fact_exposure | Fact | 1 row per dosing record | ~1,000 |
| fact_efficacy | Fact | 1 row per assessment | ~1,200 |
| fact_lab_result | Fact | 1 row per lab result | ~3,000 |

---

## Target Databases

### Primary: DuckDB

DuckDB is the primary target for the dimensional schema. It is an embedded OLAP engine ideal for local analytics, with excellent support for star schema queries, columnar storage, and seamless integration with Python (via `duckdb` package) and R. All DDL in this specification uses DuckDB-compatible SQL.

**Connection:** `duckdb.connect('trial_analytics.duckdb')`

### Secondary: PostgreSQL

For multi-user dashboards, deploy to PostgreSQL 15+. Adjust surrogate keys to `BIGSERIAL`, use `BOOLEAN` instead of DuckDB's native `BOOLEAN`, and replace `CREATE SEQUENCE` patterns with native `SERIAL`/`BIGSERIAL`.

**Connection:** `postgresql://user:pass@host:5432/trial_analytics`

### Tertiary: Databricks (Delta Lake)

For large-scale multi-study analytics on Databricks, use Delta Lake tables with `GENERATED ALWAYS AS IDENTITY` for surrogate keys. Replace DuckDB-specific date functions with Spark SQL equivalents (`DATEDIFF`, `DATE_ADD`, `DATE_TRUNC`).

---

## Dimension Tables

### 1. dim_study — Study Dimension

**Description:** One row per clinical study. Stores protocol metadata, design characteristics, and regulatory context. This is the anchor dimension for all cross-study reporting.

**Grain:** 1 row = 1 study

#### Column Definitions

| Column | Type | Nullable | Description | SDTM Source |
|--------|------|----------|-------------|-------------|
| study_sk | INTEGER | NOT NULL | Surrogate primary key | Generated |
| study_id | VARCHAR(30) | NOT NULL | Study identifier | DM.STUDYID |
| protocol_title | VARCHAR(500) | NULL | Full protocol title | Protocol metadata |
| phase | VARCHAR(20) | NULL | Phase I/II/III/IV | Protocol metadata |
| therapeutic_area | VARCHAR(100) | NULL | Oncology, Cardiology, CNS, etc. | Protocol metadata |
| indication | VARCHAR(200) | NULL | Target indication | Protocol metadata |
| sponsor | VARCHAR(200) | NULL | Sponsor organization | Protocol metadata |
| design | VARCHAR(100) | NULL | Parallel, crossover, factorial, etc. | Trial Design |
| blinding | VARCHAR(50) | NULL | Double-blind, open-label, etc. | Trial Design |
| regulatory_pathway | VARCHAR(20) | NULL | NDA, BLA, 505(b)(2), etc. | Regulatory metadata |
| primary_endpoint | VARCHAR(300) | NULL | Primary endpoint definition | Protocol metadata |
| target_enrollment | INTEGER | NULL | Planned number of subjects | Protocol metadata |
| status | VARCHAR(30) | NULL | Planning, Recruiting, Active, Completed | Study metadata |
| start_date | DATE | NULL | Study start date | DM.RFSTDTC min |
| end_date | DATE | NULL | Study end date | DM.RFENDTC max |

#### DDL

```sql
CREATE SEQUENCE seq_study_sk START 1;

CREATE TABLE dim_study (
    study_sk        INTEGER PRIMARY KEY DEFAULT nextval('seq_study_sk'),
    study_id        VARCHAR(30) NOT NULL UNIQUE,
    protocol_title  VARCHAR(500),
    phase           VARCHAR(20),
    therapeutic_area VARCHAR(100),
    indication      VARCHAR(200),
    sponsor         VARCHAR(200),
    design          VARCHAR(100),
    blinding        VARCHAR(50),
    regulatory_pathway VARCHAR(20),
    primary_endpoint VARCHAR(300),
    target_enrollment INTEGER,
    status          VARCHAR(30),
    start_date      DATE,
    end_date        DATE
);
```

#### Sample INSERT

```sql
INSERT INTO dim_study (study_id, protocol_title, phase, therapeutic_area, indication,
    sponsor, design, blinding, regulatory_pathway, primary_endpoint,
    target_enrollment, status, start_date, end_date)
VALUES (
    'T2DM-301', 'A Phase 3, Randomized, Double-Blind, Placebo-Controlled Study of
    GLP-1/GIP Dual Agonist in Type 2 Diabetes', 'Phase 3', 'Endocrinology',
    'Type 2 Diabetes Mellitus', 'Example Pharma Inc.',
    'Parallel', 'Double-Blind', 'NDA',
    'Change from baseline in HbA1c at Week 26',
    500, 'Active', '2025-01-15', '2026-06-30'
);
```

---

### 2. dim_site — Site Dimension

**Description:** One row per investigative site. Tracks site-level attributes including geography, investigator, activation date, and enrollment performance.

**Grain:** 1 row = 1 site

#### Column Definitions

| Column | Type | Nullable | Description | SDTM Source |
|--------|------|----------|-------------|-------------|
| site_sk | INTEGER | NOT NULL | Surrogate primary key | Generated |
| study_id | VARCHAR(30) | NOT NULL | Study identifier | DM.STUDYID |
| site_id | VARCHAR(10) | NOT NULL | Site identifier | DM.SITEID |
| site_name | VARCHAR(200) | NULL | Site name | Site metadata |
| country | VARCHAR(3) | NULL | ISO 3166-1 alpha-3 | DM.COUNTRY |
| region | VARCHAR(30) | NULL | North America, Europe, Asia-Pacific, etc. | Derived from country |
| site_type | VARCHAR(20) | NULL | ACADEMIC, COMMUNITY, DEDICATED | Site metadata |
| investigator_name | VARCHAR(150) | NULL | Principal investigator | Site metadata |
| activation_date | DATE | NULL | Site activation date | Site metadata |
| target_enrollment | INTEGER | NULL | Planned enrollment at site | Site metadata |
| actual_enrollment | INTEGER | NULL | Actual enrollment at site | COUNT(DM.USUBJID) |

#### DDL

```sql
CREATE SEQUENCE seq_site_sk START 1;

CREATE TABLE dim_site (
    site_sk             INTEGER PRIMARY KEY DEFAULT nextval('seq_site_sk'),
    study_id            VARCHAR(30) NOT NULL,
    site_id             VARCHAR(10) NOT NULL,
    site_name           VARCHAR(200),
    country             VARCHAR(3),
    region              VARCHAR(30),
    site_type           VARCHAR(20),
    investigator_name   VARCHAR(150),
    activation_date     DATE,
    target_enrollment   INTEGER,
    actual_enrollment   INTEGER,
    UNIQUE (study_id, site_id)
);
```

#### Sample INSERT

```sql
INSERT INTO dim_site (study_id, site_id, site_name, country, region,
    site_type, investigator_name, activation_date, target_enrollment, actual_enrollment)
VALUES
    ('T2DM-301', '001', 'University Medical Center', 'USA', 'North America',
     'ACADEMIC', 'Dr. Sarah Chen', '2025-02-01', 50, 48),
    ('T2DM-301', '002', 'Community Research Associates', 'USA', 'North America',
     'COMMUNITY', 'Dr. James Miller', '2025-02-15', 30, 31);
```

---

### 3. dim_subject — Subject Dimension (De-Identified)

**Description:** One row per subject. Contains de-identified demographics and baseline characteristics. The USUBJID is hashed for privacy. This is a **Type 1 slowly changing dimension** (overwrites on change) since subject attributes (age group, BMI category) may be updated if recalculated.

**Grain:** 1 row = 1 subject

#### Column Definitions

| Column | Type | Nullable | Description | SDTM Source |
|--------|------|----------|-------------|-------------|
| subject_sk | INTEGER | NOT NULL | Surrogate primary key | Generated |
| usubjid | VARCHAR(64) | NOT NULL | Hashed unique subject ID | SHA256(DM.USUBJID) |
| site_id | VARCHAR(10) | NULL | Site identifier | DM.SITEID |
| age | INTEGER | NULL | Age in years at RFSTDTC | DM.AGE |
| age_group | VARCHAR(20) | NULL | <45, 45-54, 55-64, 65-74, 75+ | Derived from DM.AGE |
| sex | VARCHAR(1) | NULL | M, F, U | DM.SEX |
| race | VARCHAR(60) | NULL | CDISC race terminology | DM.RACE |
| ethnicity | VARCHAR(40) | NULL | HISPANIC OR LATINO / NOT HISPANIC OR LATINO | DM.ETHNIC |
| country | VARCHAR(3) | NULL | ISO 3166-1 alpha-3 | DM.COUNTRY |
| treatment_arm | VARCHAR(20) | NULL | Actual treatment arm code | DM.ACTARMCD |
| randomization_date | DATE | NULL | Date of randomization | DM.RFSTDTC |
| bmi_category | VARCHAR(20) | NULL | Underweight, Normal, Overweight, Obese | Derived from VS.VSORRES |
| diabetes_duration_category | VARCHAR(20) | NULL | <5 years, 5-10 years, >10 years | Derived from MH |

#### DDL

```sql
CREATE SEQUENCE seq_subject_sk START 1;

CREATE TABLE dim_subject (
    subject_sk                  INTEGER PRIMARY KEY DEFAULT nextval('seq_subject_sk'),
    usubjid                     VARCHAR(64) NOT NULL UNIQUE,
    site_id                     VARCHAR(10),
    age                         INTEGER,
    age_group                   VARCHAR(20),
    sex                         VARCHAR(1),
    race                        VARCHAR(60),
    ethnicity                   VARCHAR(40),
    country                     VARCHAR(3),
    treatment_arm               VARCHAR(20),
    randomization_date          DATE,
    bmi_category                VARCHAR(20),
    diabetes_duration_category  VARCHAR(20)
);

-- Age group derivation
-- CASE
--   WHEN age < 45 THEN '<45'
--   WHEN age BETWEEN 45 AND 54 THEN '45-54'
--   WHEN age BETWEEN 55 AND 64 THEN '55-64'
--   WHEN age BETWEEN 65 AND 74 THEN '65-74'
--   ELSE '75+'
-- END
```

#### Sample INSERT

```sql
INSERT INTO dim_subject (usubjid, site_id, age, age_group, sex, race,
    ethnicity, country, treatment_arm, randomization_date, bmi_category,
    diabetes_duration_category)
VALUES (
    SHA256('T2DM-301-001-001001'), '001', 58, '55-64', 'M',
    'WHITE', 'NOT HISPANIC OR LATINO', 'USA', 'DOSE10',
    '2025-03-15', 'Obese', '5-10 years'
);
```

---

### 4. dim_treatment_arm — Treatment Arm Dimension

**Description:** One row per treatment arm per study. Describes the investigational product, dose, route, and whether the arm is a placebo or active comparator.

**Grain:** 1 row = 1 arm per study

#### Column Definitions

| Column | Type | Nullable | Description | SDTM Source |
|--------|------|----------|-------------|-------------|
| arm_sk | INTEGER | NOT NULL | Surrogate primary key | Generated |
| study_id | VARCHAR(30) | NOT NULL | Study identifier | DM.STUDYID |
| arm_code | VARCHAR(20) | NOT NULL | Arm code (e.g., DOSE10) | DM.ARMCD |
| arm_name | VARCHAR(200) | NULL | Arm description | DM.ARM |
| drug_name | VARCHAR(200) | NULL | Drug substance name | EX.EXTRT |
| dose | VARCHAR(50) | NULL | Dose amount and unit | EX.EXDOSE |
| route | VARCHAR(50) | NULL | Route of administration | EX.EXROUTE |
| frequency | VARCHAR(50) | NULL | Dosing frequency | EX.EXDOSFRQ |
| is_active | BOOLEAN | NOT NULL | True if active treatment | Derived |
| is_placebo | BOOLEAN | NOT NULL | True if placebo arm | Derived |

#### DDL

```sql
CREATE SEQUENCE seq_arm_sk START 1;

CREATE TABLE dim_treatment_arm (
    arm_sk      INTEGER PRIMARY KEY DEFAULT nextval('seq_arm_sk'),
    study_id    VARCHAR(30) NOT NULL,
    arm_code    VARCHAR(20) NOT NULL,
    arm_name    VARCHAR(200),
    drug_name   VARCHAR(200),
    dose        VARCHAR(50),
    route       VARCHAR(50),
    frequency   VARCHAR(50),
    is_active   BOOLEAN NOT NULL DEFAULT FALSE,
    is_placebo  BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (study_id, arm_code)
);
```

#### Sample INSERT

```sql
INSERT INTO dim_treatment_arm (study_id, arm_code, arm_name, drug_name,
    dose, route, frequency, is_active, is_placebo)
VALUES
    ('T2DM-301', 'DOSE5',  'GLP-1/GIP Dual Agonist 5 mg',
     'Tirzepatide Analog', '5 mg', 'SUBCUTANEOUS', 'QW', TRUE, FALSE),
    ('T2DM-301', 'DOSE10', 'GLP-1/GIP Dual Agonist 10 mg',
     'Tirzepatide Analog', '10 mg', 'SUBCUTANEOUS', 'QW', TRUE, FALSE),
    ('T2DM-301', 'DOSE15', 'GLP-1/GIP Dual Agonist 15 mg',
     'Tirzepatide Analog', '15 mg', 'SUBCUTANEOUS', 'QW', TRUE, FALSE),
    ('T2DM-301', 'PBO',    'Placebo',
     'Placebo', '0 mg', 'SUBCUTANEOUS', 'QW', FALSE, TRUE);
```

---

### 5. dim_visit_schedule — Visit Schedule Dimension

**Description:** One row per protocol-defined visit per study. Defines the expected visit timeline, windowing rules, and epoch relationship. Used to assess visit compliance.

**Grain:** 1 row = 1 visit per study

#### Column Definitions

| Column | Type | Nullable | Description | SDTM Source |
|--------|------|----------|-------------|-------------|
| visit_sk | INTEGER | NOT NULL | Surrogate primary key | Generated |
| study_id | VARCHAR(30) | NOT NULL | Study identifier | TV.STUDYID |
| visitnum | NUMERIC(10,1) | NOT NULL | Visit number (e.g., 1.0, 2.0) | TV.VISITNUM |
| visit_label | VARCHAR(200) | NULL | Visit description | TV.VISIT |
| target_day | INTEGER | NULL | Target study day (relative to Day 1) | TV.TVSTDY |
| window_before_days | INTEGER | NULL | Allowed days before target | Derived from protocol |
| window_after_days | INTEGER | NULL | Allowed days after target | Derived from protocol |
| epoch | VARCHAR(50) | NULL | SCREENING, TREATMENT, FOLLOW-UP | TV.EPOCH |
| is_primary_endpoint_visit | BOOLEAN | NOT NULL | True if primary endpoint assessed here | Derived |

#### DDL

```sql
CREATE SEQUENCE seq_visit_sk START 1;

CREATE TABLE dim_visit_schedule (
    visit_sk                    INTEGER PRIMARY KEY DEFAULT nextval('seq_visit_sk'),
    study_id                    VARCHAR(30) NOT NULL,
    visitnum                    NUMERIC(10,1) NOT NULL,
    visit_label                 VARCHAR(200),
    target_day                  INTEGER,
    window_before_days          INTEGER,
    window_after_days           INTEGER,
    epoch                       VARCHAR(50),
    is_primary_endpoint_visit   BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (study_id, visitnum)
);
```

#### Sample INSERT

```sql
INSERT INTO dim_visit_schedule (study_id, visitnum, visit_label, target_day,
    window_before_days, window_after_days, epoch, is_primary_endpoint_visit)
VALUES
    ('T2DM-301', 1.0,  'Screening',            -28, 7,  7,  'SCREENING',  FALSE),
    ('T2DM-301', 2.0,  'Baseline/Day 1',       0,   0,  0,  'TREATMENT',  FALSE),
    ('T2DM-301', 3.0,  'Week 2',               14,  3,  3,  'TREATMENT',  FALSE),
    ('T2DM-301', 4.0,  'Week 4',               28,  3,  3,  'TREATMENT',  FALSE),
    ('T2DM-301', 5.0,  'Week 8',               56,  5,  5,  'TREATMENT',  FALSE),
    ('T2DM-301', 6.0,  'Week 12',              84,  5,  5,  'TREATMENT',  FALSE),
    ('T2DM-301', 7.0,  'Week 16',              112, 5,  5,  'TREATMENT',  FALSE),
    ('T2DM-301', 8.0,  'Week 20',              140, 5,  5,  'TREATMENT',  FALSE),
    ('T2DM-301', 9.0,  'Week 26 (Primary)',    182, 7,  7,  'TREATMENT',  TRUE),
    ('T2DM-301', 10.0, 'Week 36',              252, 10, 10, 'TREATMENT',  FALSE),
    ('T2DM-301', 11.0, 'Week 52 (End of Tx)',  364, 14, 14, 'TREATMENT',  FALSE),
    ('T2DM-301', 12.0, 'Follow-up Week 4',     392, 7,  7,  'FOLLOW-UP',  FALSE);
```

---

### 6. dim_meddra — MedDRA Terminology Dimension

**Description:** One row per MedDRA Preferred Term (PT). Includes the full hierarchy up to System Organ Class (SOC) and a flag for serious-event risk. Used to dimension the fact_adverse_event table.

**Grain:** 1 row = 1 MedDRA PT

#### Column Definitions

| Column | Type | Nullable | Description | SDTM Source |
|--------|------|----------|-------------|-------------|
| meddra_sk | INTEGER | NOT NULL | Surrogate primary key | Generated |
| pt_code | VARCHAR(8) | NOT NULL | MedDRA PT code | AE.AEDECOD |
| pt_name | VARCHAR(200) | NOT NULL | Preferred Term name | AE.AEDECOD |
| hlt_name | VARCHAR(200) | NULL | High Level Term | MedDRA hierarchy |
| hlgt_name | VARCHAR(200) | NULL | High Level Group Term | MedDRA hierarchy |
| soc_name | VARCHAR(200) | NULL | System Organ Class | AE.AEBODSYS |
| is_serious_risk | BOOLEAN | NOT NULL | Flag for serious event PT | MedDRA SMQ / custom |

#### DDL

```sql
CREATE SEQUENCE seq_meddra_sk START 1;

CREATE TABLE dim_meddra (
    meddra_sk       INTEGER PRIMARY KEY DEFAULT nextval('seq_meddra_sk'),
    pt_code         VARCHAR(8) NOT NULL UNIQUE,
    pt_name         VARCHAR(200) NOT NULL,
    hlt_name        VARCHAR(200),
    hlgt_name       VARCHAR(200),
    soc_name        VARCHAR(200),
    is_serious_risk BOOLEAN NOT NULL DEFAULT FALSE
);
```

#### Sample INSERT

```sql
INSERT INTO dim_meddra (pt_code, pt_name, hlt_name, hlgt_name, soc_name, is_serious_risk)
VALUES
    ('10045213', 'Nausea', 'Nausea and vomiting symptoms',
     'Gastrointestinal signs and symptoms', 'Gastrointestinal disorders', FALSE),
    ('10053982', 'Blood glucose increased', 'Carbohydrate tolerance analyses',
     'Metabolic investigations', 'Investigations', FALSE),
    ('10011938', 'Acute pancreatitis', 'Pancreatic disorders NEC',
     'Pancreatic conditions', 'Hepatobiliary disorders', TRUE),
    ('10007515', 'Cardiac arrest', 'Cardiac arrest',
     'Fatal outcomes', 'Cardiac disorders', TRUE),
    ('10012686', 'Diarrhoea', 'Diarrhoea (excl infective)',
     'Gastrointestinal signs and symptoms', 'Gastrointestinal disorders', FALSE),
    ('10017947', 'Hypoglycaemia', 'Hypoglycaemic conditions NEC',
     'Glucose metabolism disorders', 'Metabolism and nutrition disorders', TRUE);
```

---

### 7. dim_lab_test — Laboratory Test Dimension

**Description:** One row per laboratory test. Includes LOINC coding, normal ranges, and organ system flags for liver and kidney function monitoring.

**Grain:** 1 row = 1 lab test

#### Column Definitions

| Column | Type | Nullable | Description | SDTM Source |
|--------|------|----------|-------------|-------------|
| lab_test_sk | INTEGER | NOT NULL | Surrogate primary key | Generated |
| test_code | VARCHAR(8) | NOT NULL | Lab test code | LB.LBTESTCD |
| test_name | VARCHAR(200) | NOT NULL | Lab test name | LB.LBTEST |
| loinc_code | VARCHAR(10) | NULL | LOINC code | LB.LBLOINC |
| category | VARCHAR(30) | NULL | CHEMISTRY, HEMATOLOGY, URINALYSIS | LB.LBCAT |
| unit | VARCHAR(30) | NULL | Standard unit | LB.LBORRESU |
| lower_normal_range | DOUBLE | NULL | Lower limit of normal | LB.LBSTNRLO |
| upper_normal_range | DOUBLE | NULL | Upper limit of normal | LB.LBSTNRHI |
| is_liver_function | BOOLEAN | NOT NULL | True if liver function test | Derived |
| is_kidney_function | BOOLEAN | NOT NULL | True if kidney function test | Derived |

#### DDL

```sql
CREATE SEQUENCE seq_lab_test_sk START 1;

CREATE TABLE dim_lab_test (
    lab_test_sk         INTEGER PRIMARY KEY DEFAULT nextval('seq_lab_test_sk'),
    test_code           VARCHAR(8) NOT NULL UNIQUE,
    test_name           VARCHAR(200) NOT NULL,
    loinc_code          VARCHAR(10),
    category            VARCHAR(30),
    unit                VARCHAR(30),
    lower_normal_range  DOUBLE,
    upper_normal_range  DOUBLE,
    is_liver_function   BOOLEAN NOT NULL DEFAULT FALSE,
    is_kidney_function  BOOLEAN NOT NULL DEFAULT FALSE
);
```

#### Sample INSERT

```sql
INSERT INTO dim_lab_test (test_code, test_name, loinc_code, category, unit,
    lower_normal_range, upper_normal_range, is_liver_function, is_kidney_function)
VALUES
    ('ALT', 'Alanine Aminotransferase', '1742-6', 'CHEMISTRY',
     'U/L', 7, 56, TRUE, FALSE),
    ('AST', 'Aspartate Aminotransferase', '1920-8', 'CHEMISTRY',
     'U/L', 10, 40, TRUE, FALSE),
    ('ALP', 'Alkaline Phosphatase', '6768-6', 'CHEMISTRY',
     'U/L', 44, 147, TRUE, FALSE),
    ('BILI', 'Bilirubin', '1975-2', 'CHEMISTRY',
     'mg/dL', 0.1, 1.2, TRUE, FALSE),
    ('CREAT', 'Creatinine', '2160-0', 'CHEMISTRY',
     'mg/dL', 0.6, 1.3, FALSE, TRUE),
    ('BUN', 'Blood Urea Nitrogen', '3094-0', 'CHEMISTRY',
     'mg/dL', 6, 20, FALSE, TRUE),
    ('HBA1C', 'Hemoglobin A1c', '4548-4', 'CHEMISTRY',
     '%', 4.0, 5.6, FALSE, FALSE),
    ('GLUC', 'Glucose', '2345-7', 'CHEMISTRY',
     'mg/dL', 70, 99, FALSE, FALSE),
    ('WBC', 'White Blood Cell Count', '6690-2', 'HEMATOLOGY',
     '10^3/uL', 4.0, 11.0, FALSE, FALSE),
    ('HGB', 'Hemoglobin', '718-7', 'HEMATOLOGY',
     'g/dL', 12.0, 16.0, FALSE, FALSE),
    ('PLT', 'Platelets', '777-3', 'HEMATOLOGY',
     '10^3/uL', 150, 400, FALSE, FALSE);
```

---

## Fact Tables

### 1. fact_enrollment — Enrollment Fact

**Description:** One row per subject enrollment event. Captures when and where each subject was enrolled, which arm they were assigned to, and whether they passed screening.

**Grain:** 1 row = 1 subject enrollment

#### Column Definitions

| Column | Type | Nullable | Description | FK Reference |
|--------|------|----------|-------------|-------------|
| enrollment_sk | INTEGER | NOT NULL | Surrogate primary key | — |
| study_sk | INTEGER | NOT NULL | Study dimension FK | dim_study.study_sk |
| site_sk | INTEGER | NOT NULL | Site dimension FK | dim_site.site_sk |
| subject_sk | INTEGER | NOT NULL | Subject dimension FK | dim_subject.subject_sk |
| arm_sk | INTEGER | NOT NULL | Treatment arm FK | dim_treatment_arm.arm_sk |
| enrollment_date | DATE | NULL | Date of enrollment | DM.RFSTDTC |
| days_since_first_enrollment | INTEGER | NULL | Days since first subject enrolled | Derived |
| is_screen_failure | BOOLEAN | NOT NULL | True if subject failed screening | DS |

#### DDL

```sql
CREATE TABLE fact_enrollment (
    enrollment_sk               INTEGER,
    study_sk                    INTEGER NOT NULL,
    site_sk                     INTEGER NOT NULL,
    subject_sk                  INTEGER NOT NULL,
    arm_sk                      INTEGER NOT NULL,
    enrollment_date             DATE,
    days_since_first_enrollment INTEGER,
    is_screen_failure           BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (enrollment_sk, study_sk),
    FOREIGN KEY (study_sk)   REFERENCES dim_study(study_sk),
    FOREIGN KEY (site_sk)    REFERENCES dim_site(site_sk),
    FOREIGN KEY (subject_sk) REFERENCES dim_subject(subject_sk),
    FOREIGN KEY (arm_sk)     REFERENCES dim_treatment_arm(arm_sk)
);
```

#### Sample Analytical Queries

**Enrollment by site and month:**
```sql
SELECT
    s.site_name,
    DATE_TRUNC('month', e.enrollment_date) AS enrollment_month,
    COUNT(DISTINCT e.subject_sk) AS subjects_enrolled,
    SUM(CASE WHEN e.is_screen_failure THEN 0 ELSE 1 END) AS randomized_subjects,
    SUM(CASE WHEN e.is_screen_failure THEN 1 ELSE 0 END) AS screen_failures,
    ROUND(100.0 * SUM(CASE WHEN e.is_screen_failure THEN 1 ELSE 0 END)
        / COUNT(*), 1) AS screen_failure_rate_pct
FROM fact_enrollment e
JOIN dim_site s ON e.site_sk = s.site_sk
GROUP BY s.site_name, DATE_TRUNC('month', e.enrollment_date)
ORDER BY enrollment_month, s.site_name;
```

**Cumulative enrollment curve (for enrollment dashboards):**
```sql
SELECT
    enrollment_date,
    SUM(COUNT(*)) OVER (ORDER BY enrollment_date) AS cumulative_enrolled,
    SUM(SUM(CASE WHEN NOT is_screen_failure THEN 1 ELSE 0 END))
        OVER (ORDER BY enrollment_date) AS cumulative_randomized
FROM fact_enrollment
WHERE NOT is_screen_failure
GROUP BY enrollment_date
ORDER BY enrollment_date;
```

**Actual vs. target enrollment by site:**
```sql
SELECT
    s.site_name,
    s.target_enrollment,
    s.actual_enrollment,
    s.actual_enrollment - s.target_enrollment AS delta,
    CASE WHEN s.target_enrollment > 0
        THEN ROUND(100.0 * s.actual_enrollment / s.target_enrollment, 1)
        ELSE NULL END AS pct_of_target
FROM dim_site s
ORDER BY pct_of_target ASC;
```

---

### 2. fact_visit — Visit Fact

**Description:** One row per actual subject visit occurrence. Tracks whether each scheduled visit happened, when it occurred, and its compliance with the protocol window.

**Grain:** 1 row = 1 actual visit occurrence

#### Column Definitions

| Column | Type | Nullable | Description | FK Reference |
|--------|------|----------|-------------|-------------|
| visit_sk_surr | INTEGER | NOT NULL | Surrogate primary key | — |
| study_sk | INTEGER | NOT NULL | Study dimension FK | dim_study.study_sk |
| site_sk | INTEGER | NOT NULL | Site dimension FK | dim_site.site_sk |
| subject_sk | INTEGER | NOT NULL | Subject dimension FK | dim_subject.subject_sk |
| visit_schedule_sk | INTEGER | NOT NULL | Visit schedule FK | dim_visit_schedule.visit_sk |
| actual_visit_date | DATE | NULL | Actual date of visit | SV.SVSTDTC |
| days_from_target | INTEGER | NULL | Actual day minus target day | Derived |
| is_within_window | BOOLEAN | NOT NULL | True if within allowed window | Derived |
| visit_status | VARCHAR(20) | NULL | COMPLETED, MISSED, EARLY_TERM | Derived |

#### DDL

```sql
CREATE TABLE fact_visit (
    visit_sk_surr       INTEGER,
    study_sk            INTEGER NOT NULL,
    site_sk             INTEGER NOT NULL,
    subject_sk          INTEGER NOT NULL,
    visit_schedule_sk   INTEGER NOT NULL,
    actual_visit_date   DATE,
    days_from_target    INTEGER,
    is_within_window    BOOLEAN NOT NULL DEFAULT TRUE,
    visit_status        VARCHAR(20),
    PRIMARY KEY (visit_sk_surr, study_sk),
    FOREIGN KEY (study_sk)          REFERENCES dim_study(study_sk),
    FOREIGN KEY (site_sk)           REFERENCES dim_site(site_sk),
    FOREIGN KEY (subject_sk)        REFERENCES dim_subject(subject_sk),
    FOREIGN KEY (visit_schedule_sk) REFERENCES dim_visit_schedule(visit_sk)
);
```

#### Sample Analytical Queries

**Visit compliance by site:**
```sql
SELECT
    s.site_name,
    vs.visit_label,
    COUNT(*) AS total_expected,
    SUM(CASE WHEN v.visit_status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed,
    SUM(CASE WHEN v.visit_status = 'MISSED' THEN 1 ELSE 0 END) AS missed,
    ROUND(100.0 * SUM(CASE WHEN v.visit_status = 'COMPLETED' THEN 1 ELSE 0 END)
        / COUNT(*), 1) AS completion_rate_pct,
    ROUND(AVG(CASE WHEN v.is_within_window THEN 1.0 ELSE 0.0 END) * 100, 1)
        AS window_compliance_pct
FROM fact_visit v
JOIN dim_site s ON v.site_sk = s.site_sk
JOIN dim_visit_schedule vs ON v.visit_schedule_sk = vs.visit_sk
GROUP BY s.site_name, vs.visit_label
ORDER BY s.site_name, vs.visitnum;
```

**Subject retention (visit completion over time):**
```sql
SELECT
    vs.visitnum,
    vs.visit_label,
    COUNT(DISTINCT v.subject_sk) AS subjects_with_visit,
    ROUND(100.0 * COUNT(DISTINCT v.subject_sk)
        / (SELECT COUNT(*) FROM dim_subject), 1) AS retention_pct
FROM fact_visit v
JOIN dim_visit_schedule vs ON v.visit_schedule_sk = vs.visit_sk
WHERE v.visit_status = 'COMPLETED'
GROUP BY vs.visitnum, vs.visit_label
ORDER BY vs.visitnum;
```

**CRF page completion rate by site (visit form completeness):**
```sql
SELECT
    s.site_name,
    vs.epoch,
    COUNT(*) AS expected_forms,
    SUM(CASE WHEN v.visit_status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed_forms,
    ROUND(100.0 * SUM(CASE WHEN v.visit_status = 'COMPLETED' THEN 1 ELSE 0 END)
        / COUNT(*), 1) AS crf_completion_pct
FROM fact_visit v
JOIN dim_site s ON v.site_sk = s.site_sk
JOIN dim_visit_schedule vs ON v.visit_schedule_sk = vs.visit_sk
GROUP BY s.site_name, vs.epoch
ORDER BY s.site_name, vs.epoch;
```

---

### 3. fact_adverse_event — Adverse Event Fact

**Description:** One row per adverse event occurrence. Captures event timing, severity, seriousness, causality assessment, and resolution. Connected to MedDRA for hierarchical roll-up analysis.

**Grain:** 1 row = 1 AE occurrence

#### Column Definitions

| Column | Type | Nullable | Description | FK Reference |
|--------|------|----------|-------------|-------------|
| ae_sk | INTEGER | NOT NULL | Surrogate primary key | — |
| study_sk | INTEGER | NOT NULL | Study dimension FK | dim_study.study_sk |
| subject_sk | INTEGER | NOT NULL | Subject dimension FK | dim_subject.subject_sk |
| meddra_sk | INTEGER | NOT NULL | MedDRA PT dimension FK | dim_meddra.meddra_sk |
| visit_schedule_sk | INTEGER | NOT NULL | Onset visit FK | dim_visit_schedule.visit_sk |
| ae_start_date | DATE | NULL | AE start date | AE.AESTDTC |
| ae_end_date | DATE | NULL | AE end date | AE.AEENDTC |
| severity_numeric | SMALLINT | NULL | CTCAE grade 1–5 | AE.AETOXGR |
| is_serious | BOOLEAN | NOT NULL | True if SAE | AE.AESER |
| is_treatment_emergent | BOOLEAN | NOT NULL | True if TEAE | Derived |
| is_drug_related | BOOLEAN | NOT NULL | True if related to study drug | AE.AEREL |
| days_to_resolution | INTEGER | NULL | Days from start to end date | Derived |

#### DDL

```sql
CREATE TABLE fact_adverse_event (
    ae_sk                   INTEGER,
    study_sk                INTEGER NOT NULL,
    subject_sk              INTEGER NOT NULL,
    meddra_sk               INTEGER NOT NULL,
    visit_schedule_sk       INTEGER NOT NULL,
    ae_start_date           DATE,
    ae_end_date             DATE,
    severity_numeric        SMALLINT,
    is_serious              BOOLEAN NOT NULL DEFAULT FALSE,
    is_treatment_emergent   BOOLEAN NOT NULL DEFAULT TRUE,
    is_drug_related         BOOLEAN NOT NULL DEFAULT FALSE,
    days_to_resolution      INTEGER,
    PRIMARY KEY (ae_sk, study_sk),
    FOREIGN KEY (study_sk)          REFERENCES dim_study(study_sk),
    FOREIGN KEY (subject_sk)        REFERENCES dim_subject(subject_sk),
    FOREIGN KEY (meddra_sk)         REFERENCES dim_meddra(meddra_sk),
    FOREIGN KEY (visit_schedule_sk) REFERENCES dim_visit_schedule(visit_sk)
);
```

#### Sample Analytical Queries

**AE rate by treatment arm and SOC:**
```sql
SELECT
    a.arm_name,
    m.soc_name,
    COUNT(DISTINCT ae.subject_sk) AS subjects_with_ae,
    COUNT(*) AS total_events,
    ROUND(100.0 * COUNT(DISTINCT ae.subject_sk)
        / (SELECT COUNT(*) FROM fact_enrollment fe
           JOIN dim_treatment_arm da ON fe.arm_sk = da.arm_sk
           WHERE da.arm_code = a.arm_code AND NOT fe.is_screen_failure), 1)
        AS incidence_pct
FROM fact_adverse_event ae
JOIN dim_meddra m ON ae.meddra_sk = m.meddra_sk
JOIN dim_subject subj ON ae.subject_sk = subj.subject_sk
JOIN dim_treatment_arm a ON subj.treatment_arm = a.arm_code
WHERE ae.is_treatment_emergent
GROUP BY a.arm_name, m.soc_name
ORDER BY a.arm_name, incidence_pct DESC;
```

**Serious AE (SAE) rate by treatment arm:**
```sql
SELECT
    a.arm_name,
    COUNT(*) AS total_aes,
    SUM(CASE WHEN ae.is_serious THEN 1 ELSE 0 END) AS serious_aes,
    COUNT(DISTINCT CASE WHEN ae.is_serious THEN ae.subject_sk END) AS subjects_with_sae,
    ROUND(100.0 * SUM(CASE WHEN ae.is_serious THEN 1 ELSE 0 END)
        / COUNT(*), 1) AS sae_rate_pct
FROM fact_adverse_event ae
JOIN dim_subject subj ON ae.subject_sk = subj.subject_sk
JOIN dim_treatment_arm a ON subj.treatment_arm = a.arm_code
WHERE ae.is_treatment_emergent
GROUP BY a.arm_name
ORDER BY a.arm_name;
```

**Drug-related TEAE by preferred term (top 10):**
```sql
SELECT
    m.pt_name,
    m.soc_name,
    COUNT(DISTINCT ae.subject_sk) AS n_subjects,
    COUNT(*) AS n_events,
    ROUND(AVG(ae.severity_numeric), 1) AS mean_severity
FROM fact_adverse_event ae
JOIN dim_meddra m ON ae.meddra_sk = m.meddra_sk
WHERE ae.is_treatment_emergent
  AND ae.is_drug_related
GROUP BY m.pt_name, m.soc_name
ORDER BY n_subjects DESC
LIMIT 10;
```

---

### 4. fact_exposure — Exposure Fact

**Description:** One row per dosing record. Tracks the amount, frequency, and duration of study drug exposure. Supports cumulative dose calculations and compliance monitoring.

**Grain:** 1 row = 1 dosing record

#### Column Definitions

| Column | Type | Nullable | Description | FK Reference |
|--------|------|----------|-------------|-------------|
| exposure_sk | INTEGER | NOT NULL | Surrogate primary key | — |
| study_sk | INTEGER | NOT NULL | Study dimension FK | dim_study.study_sk |
| subject_sk | INTEGER | NOT NULL | Subject dimension FK | dim_subject.subject_sk |
| arm_sk | INTEGER | NOT NULL | Treatment arm FK | dim_treatment_arm.arm_sk |
| dose_date | DATE | NULL | Date of dose administration | EX.EXSTDTC |
| dose_amount | DOUBLE | NULL | Amount per dose in mg | EX.EXDOSE |
| cumulative_dose | DOUBLE | NULL | Running total dose in mg | Derived |
| days_on_treatment | INTEGER | NULL | Cumulative days receiving drug | Derived |
| dose_modification_count | INTEGER | NOT NULL | Number of dose modifications to date | EX |
| compliance_pct | DOUBLE | NULL | Percent of planned doses received | Derived |

#### DDL

```sql
CREATE TABLE fact_exposure (
    exposure_sk             INTEGER,
    study_sk                INTEGER NOT NULL,
    subject_sk              INTEGER NOT NULL,
    arm_sk                  INTEGER NOT NULL,
    dose_date               DATE,
    dose_amount             DOUBLE,
    cumulative_dose         DOUBLE,
    days_on_treatment       INTEGER,
    dose_modification_count INTEGER NOT NULL DEFAULT 0,
    compliance_pct          DOUBLE,
    PRIMARY KEY (exposure_sk, study_sk),
    FOREIGN KEY (study_sk)   REFERENCES dim_study(study_sk),
    FOREIGN KEY (subject_sk) REFERENCES dim_subject(subject_sk),
    FOREIGN KEY (arm_sk)     REFERENCES dim_treatment_arm(arm_sk)
);
```

#### Sample Analytical Queries

**Cumulative dose by treatment arm over time:**
```sql
SELECT
    a.arm_name,
    DATE_TRUNC('week', e.dose_date) AS dose_week,
    AVG(e.cumulative_dose) AS mean_cumulative_dose,
    STDDEV(e.cumulative_dose) AS sd_cumulative_dose
FROM fact_exposure e
JOIN dim_treatment_arm a ON e.arm_sk = a.arm_sk
WHERE a.is_active
GROUP BY a.arm_name, DATE_TRUNC('week', e.dose_date)
ORDER BY a.arm_name, dose_week;
```

**Treatment duration (Kaplan-Meier data for discontinuation):**
```sql
SELECT
    subj.treatment_arm,
    e.subject_sk,
    MAX(e.days_on_treatment) AS last_day_on_treatment,
    CASE WHEN ds.dsdecod LIKE '%DISCONTINUED%' OR ds.dsdecod LIKE '%WITHDREW%'
         THEN 1 ELSE 0 END AS event_occurred
FROM fact_exposure e
JOIN dim_subject subj ON e.subject_sk = subj.subject_sk
LEFT JOIN (SELECT DISTINCT subject_sk, dsdecod FROM fact_visit ...) ds
    ON e.subject_sk = ds.subject_sk
GROUP BY subj.treatment_arm, e.subject_sk, ds.dsdecod
ORDER BY subj.treatment_arm, last_day_on_treatment;
```

**Compliance distribution by arm:**
```sql
SELECT
    a.arm_name,
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY e.compliance_pct) AS p25,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY e.compliance_pct) AS median,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY e.compliance_pct) AS p75,
    AVG(e.compliance_pct) AS mean_compliance,
    SUM(CASE WHEN e.compliance_pct >= 80 THEN 1 ELSE 0 END) AS compliant_subjects,
    COUNT(*) AS total_subjects,
    ROUND(100.0 * SUM(CASE WHEN e.compliance_pct >= 80 THEN 1 ELSE 0 END)
        / COUNT(*), 1) AS compliant_pct
FROM (
    SELECT subject_sk, arm_sk, AVG(compliance_pct) AS compliance_pct
    FROM fact_exposure
    GROUP BY subject_sk, arm_sk
) e
JOIN dim_treatment_arm a ON e.arm_sk = a.arm_sk
GROUP BY a.arm_name
ORDER BY a.arm_name;
```

---

### 5. fact_efficacy — Efficacy Fact

**Description:** One row per efficacy assessment per subject per visit. Captures the primary and secondary endpoint measurements with baseline and change-from-baseline calculations.

**Grain:** 1 row = 1 efficacy assessment per subject per visit

#### Column Definitions

| Column | Type | Nullable | Description | FK Reference |
|--------|------|----------|-------------|-------------|
| efficacy_sk | INTEGER | NOT NULL | Surrogate primary key | — |
| study_sk | INTEGER | NOT NULL | Study dimension FK | dim_study.study_sk |
| subject_sk | INTEGER | NOT NULL | Subject dimension FK | dim_subject.subject_sk |
| visit_schedule_sk | INTEGER | NOT NULL | Visit schedule FK | dim_visit_schedule.visit_sk |
| arm_sk | INTEGER | NOT NULL | Treatment arm FK | dim_treatment_arm.arm_sk |
| baseline_value | DOUBLE | NULL | Baseline measurement | ADaM ABLFL |
| assessment_value | DOUBLE | NULL | Current visit measurement | ADaM AVAL |
| change_from_baseline | DOUBLE | NULL | Assessment minus baseline | ADaM CHG |
| pct_change_from_baseline | DOUBLE | NULL | (Change / Baseline) * 100 | ADaM PCHG |
| is_responder | BOOLEAN | NOT NULL | True if meets responder criteria | ADaM CRIT1FL |
| is_primary_endpoint_visit | BOOLEAN | NOT NULL | True if this is the primary endpoint assessment | Derived |

#### DDL

```sql
CREATE TABLE fact_efficacy (
    efficacy_sk                 INTEGER,
    study_sk                    INTEGER NOT NULL,
    subject_sk                  INTEGER NOT NULL,
    visit_schedule_sk           INTEGER NOT NULL,
    arm_sk                      INTEGER NOT NULL,
    baseline_value              DOUBLE,
    assessment_value            DOUBLE,
    change_from_baseline        DOUBLE,
    pct_change_from_baseline    DOUBLE,
    is_responder                BOOLEAN NOT NULL DEFAULT FALSE,
    is_primary_endpoint_visit   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (efficacy_sk, study_sk),
    FOREIGN KEY (study_sk)          REFERENCES dim_study(study_sk),
    FOREIGN KEY (subject_sk)        REFERENCES dim_subject(subject_sk),
    FOREIGN KEY (visit_schedule_sk) REFERENCES dim_visit_schedule(visit_sk),
    FOREIGN KEY (arm_sk)            REFERENCES dim_treatment_arm(arm_sk)
);
```

#### Sample Analytical Queries

**Primary endpoint analysis — LS mean change from baseline at Week 26 by arm:**
```sql
SELECT
    a.arm_name,
    COUNT(*) AS n,
    ROUND(AVG(e.change_from_baseline), 2) AS mean_change,
    ROUND(STDDEV(e.change_from_baseline), 3) AS sd_change,
    ROUND(AVG(e.pct_change_from_baseline), 1) AS mean_pct_change,
    SUM(CASE WHEN e.is_responder THEN 1 ELSE 0 END) AS responders,
    ROUND(100.0 * SUM(CASE WHEN e.is_responder THEN 1 ELSE 0 END)
        / COUNT(*), 1) AS responder_rate_pct
FROM fact_efficacy e
JOIN dim_treatment_arm a ON e.arm_sk = a.arm_sk
WHERE e.is_primary_endpoint_visit
GROUP BY a.arm_name
ORDER BY a.arm_name;
```

**Efficacy waterfall plot data (best % change from baseline per subject):**
```sql
SELECT
    subj.usubjid AS subject_id,
    a.arm_name,
    MIN(e.pct_change_from_baseline) AS best_pct_change_from_baseline
FROM fact_efficacy e
JOIN dim_subject subj ON e.subject_sk = subj.subject_sk
JOIN dim_treatment_arm a ON e.arm_sk = a.arm_sk
WHERE a.is_active
  AND e.pct_change_from_baseline IS NOT NULL
GROUP BY subj.usubjid, a.arm_name
ORDER BY best_pct_change_from_baseline ASC;
```

**Longitudinal mean change from baseline by visit (spaghetti plot data):**
```sql
SELECT
    vs.visitnum,
    vs.visit_label,
    a.arm_name,
    ROUND(AVG(e.change_from_baseline), 2) AS mean_change,
    ROUND(STDERR(e.change_from_baseline), 3) AS se_change,
    COUNT(*) AS n
FROM fact_efficacy e
JOIN dim_visit_schedule vs ON e.visit_schedule_sk = vs.visit_sk
JOIN dim_treatment_arm a ON e.arm_sk = a.arm_sk
WHERE vs.epoch = 'TREATMENT'
GROUP BY vs.visitnum, vs.visit_label, a.arm_name
ORDER BY vs.visitnum, a.arm_name;
```

---

### 6. fact_lab_result — Lab Results Fact

**Description:** One row per laboratory test result. Includes baseline comparison, abnormality flags, toxicity grading (CTCAE), and clinical significance assessment.

**Grain:** 1 row = 1 lab result

#### Column Definitions

| Column | Type | Nullable | Description | FK Reference |
|--------|------|----------|-------------|-------------|
| lab_result_sk | INTEGER | NOT NULL | Surrogate primary key | — |
| study_sk | INTEGER | NOT NULL | Study dimension FK | dim_study.study_sk |
| subject_sk | INTEGER | NOT NULL | Subject dimension FK | dim_subject.subject_sk |
| lab_test_sk | INTEGER | NOT NULL | Lab test dimension FK | dim_lab_test.lab_test_sk |
| visit_schedule_sk | INTEGER | NOT NULL | Visit schedule FK | dim_visit_schedule.visit_sk |
| result_value | DOUBLE | NULL | Numeric result | LB.LBSTRESN |
| result_text | VARCHAR(200) | NULL | Character result (if non-numeric) | LB.LBSTRESC |
| baseline_value | DOUBLE | NULL | Baseline result for this test | ADaM BASE |
| change_from_baseline | DOUBLE | NULL | Current minus baseline | ADaM CHG |
| pct_change | DOUBLE | NULL | Percent change from baseline | Derived |
| is_abnormal | BOOLEAN | NOT NULL | Outside normal range (L/H) | LB.LBNRIND |
| toxicity_grade | SMALLINT | NULL | CTCAE grade 0–4 | LB.LBTOXGR |
| is_clinically_significant | BOOLEAN | NOT NULL | Clinically significant per investigator | LB.LBCLSIG |

#### DDL

```sql
CREATE TABLE fact_lab_result (
    lab_result_sk           INTEGER,
    study_sk                INTEGER NOT NULL,
    subject_sk              INTEGER NOT NULL,
    lab_test_sk             INTEGER NOT NULL,
    visit_schedule_sk       INTEGER NOT NULL,
    result_value            DOUBLE,
    result_text             VARCHAR(200),
    baseline_value          DOUBLE,
    change_from_baseline    DOUBLE,
    pct_change              DOUBLE,
    is_abnormal             BOOLEAN NOT NULL DEFAULT FALSE,
    toxicity_grade          SMALLINT,
    is_clinically_significant BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (lab_result_sk, study_sk),
    FOREIGN KEY (study_sk)          REFERENCES dim_study(study_sk),
    FOREIGN KEY (subject_sk)        REFERENCES dim_subject(subject_sk),
    FOREIGN KEY (lab_test_sk)       REFERENCES dim_lab_test(lab_test_sk),
    FOREIGN KEY (visit_schedule_sk) REFERENCES dim_visit_schedule(visit_sk)
);
```

#### Sample Analytical Queries

**Lab toxicity shift analysis (baseline to Week 26):**
```sql
WITH baseline AS (
    SELECT lr.subject_sk, lr.lab_test_sk,
        CASE WHEN lr.toxicity_grade IS NULL THEN 0 ELSE lr.toxicity_grade END AS baseline_grade
    FROM fact_lab_result lr
    JOIN dim_visit_schedule vs ON lr.visit_schedule_sk = vs.visit_sk
    WHERE vs.visitnum = 2.0  -- Baseline
),
week26 AS (
    SELECT lr.subject_sk, lr.lab_test_sk,
        CASE WHEN lr.toxicity_grade IS NULL THEN 0 ELSE lr.toxicity_grade END AS post_grade
    FROM fact_lab_result lr
    JOIN dim_visit_schedule vs ON lr.visit_schedule_sk = vs.visit_sk
    WHERE vs.visitnum = 9.0  -- Week 26
)
SELECT
    lt.test_name,
    b.baseline_grade AS grade_baseline,
    w.post_grade AS grade_week26,
    COUNT(*) AS n,
    CASE WHEN w.post_grade > b.baseline_grade THEN 'WORSENED'
         WHEN w.post_grade < b.baseline_grade THEN 'IMPROVED'
         ELSE 'STABLE' END AS shift_direction
FROM baseline b
JOIN week26 w ON b.subject_sk = w.subject_sk AND b.lab_test_sk = w.lab_test_sk
JOIN dim_lab_test lt ON b.lab_test_sk = lt.lab_test_sk
WHERE lt.is_liver_function OR lt.is_kidney_function
GROUP BY lt.test_name, b.baseline_grade, w.post_grade,
    CASE WHEN w.post_grade > b.baseline_grade THEN 'WORSENED'
         WHEN w.post_grade < b.baseline_grade THEN 'IMPROVED'
         ELSE 'STABLE' END
ORDER BY lt.test_name, b.baseline_grade, w.post_grade;
```

**Liver function monitoring — maximum ALT value by subject:**
```sql
SELECT
    subj.usubjid,
    lt.test_name,
    MAX(lr.result_value) AS max_value,
    lt.upper_normal_range AS uln,
    ROUND(MAX(lr.result_value) / lt.upper_normal_range, 1) AS x_uln,
    CASE
        WHEN MAX(lr.result_value) < lt.upper_normal_range THEN 'Normal'
        WHEN MAX(lr.result_value) < 3 * lt.upper_normal_range THEN 'Grade 1'
        WHEN MAX(lr.result_value) < 5 * lt.upper_normal_range THEN 'Grade 2'
        WHEN MAX(lr.result_value) < 20 * lt.upper_normal_range THEN 'Grade 3'
        ELSE 'Grade 4' END AS toxicity_category
FROM fact_lab_result lr
JOIN dim_subject subj ON lr.subject_sk = subj.subject_sk
JOIN dim_lab_test lt ON lr.lab_test_sk = lt.lab_test_sk
WHERE lt.is_liver_function
  AND lt.test_code IN ('ALT', 'AST')
GROUP BY subj.usubjid, lt.test_name, lt.upper_normal_range
HAVING MAX(lr.result_value) > lt.upper_normal_range
ORDER BY max_value DESC;
```

**HbA1c mean over time by treatment arm (longitudinal lab):**
```sql
SELECT
    vs.visitnum,
    vs.visit_label,
    a.arm_name,
    ROUND(AVG(lr.result_value), 2) AS mean_hba1c,
    ROUND(AVG(lr.change_from_baseline), 2) AS mean_change_hba1c,
    COUNT(*) AS n
FROM fact_lab_result lr
JOIN dim_lab_test lt ON lr.lab_test_sk = lt.lab_test_sk
JOIN dim_visit_schedule vs ON lr.visit_schedule_sk = vs.visit_sk
JOIN dim_subject subj ON lr.subject_sk = subj.subject_sk
JOIN dim_treatment_arm a ON subj.treatment_arm = a.arm_code
WHERE lt.test_code = 'HBA1C'
  AND vs.epoch = 'TREATMENT'
GROUP BY vs.visitnum, vs.visit_label, a.arm_name
ORDER BY vs.visitnum, a.arm_name;
```

---

## ETL Logic — Populating from SDTM JSON

The following pseudocode describes the ETL pipeline for populating the star schema from TrialSim-generated SDTM JSON data. The logic handles surrogate key generation, slowly changing dimensions, and derived measure calculations.

### ETL Pipeline Overview

```
SDTM JSON (DM, AE, VS, LB, EX, DS, TV, MH)
       │
       ▼
  ┌──────────┐    ┌───────────────┐    ┌──────────────────┐
  │ Extract  │───▶│  Transform to │───▶│  Load Dimensions │
  │ SDTM     │    │  Star Schema  │    │  then Facts       │
  └──────────┘    └───────────────┘    └──────────────────┘
```

### Surrogate Key Generation Strategy

All surrogate keys use DuckDB `CREATE SEQUENCE` with `nextval()`. This provides auto-incrementing integer keys that are efficient for joins and indexing.

```sql
-- Standard pattern for all dimension and fact tables:
CREATE SEQUENCE seq_<table>_sk START 1;

-- In INSERT statements:
INSERT INTO dim_xxx (xxx_sk, ...)
SELECT nextval('seq_xxx_sk'), ... FROM source;
```

**Alternative (for PostgreSQL or Databricks):**
- PostgreSQL: Use `BIGSERIAL` (auto-increment column type)
- Databricks: Use `GENERATED ALWAYS AS IDENTITY (START WITH 1 INCREMENT BY 1)`

### SCD Strategy

| Dimension | SCD Type | Strategy |
|-----------|----------|----------|
| dim_study | Type 0 (no changes) | Insert only, never update |
| dim_site | Type 1 (overwrite) | UPDATE existing row on change |
| dim_subject | Type 1 (overwrite) | UPDATE on recalc of age_group, bmi_category |
| dim_treatment_arm | Type 0 | Insert only |
| dim_visit_schedule | Type 0 | Insert only |
| dim_meddra | Type 1 | Upsert via pt_code |
| dim_lab_test | Type 1 | Upsert via test_code |

### Step-by-Step ETL

#### Step 1: Populate dim_study

```
LOAD SDTM DM JSON
EXTRACT DISTINCT STUDYID
FOR each study:
    INSERT INTO dim_study (study_sk, study_id, protocol_title, ...)
    SELECT nextval('seq_study_sk'), study_id, protocol_title, phase,
           therapeutic_area, indication, sponsor, design, blinding,
           regulatory_pathway, primary_endpoint, target_enrollment,
           status,
           MIN(rfstdtc) AS start_date,
           MAX(rfendtc) AS end_date
    FROM dm_data
    GROUP BY study_id;
```

```sql
INSERT INTO dim_study (study_sk, study_id, phase, therapeutic_area, indication,
    sponsor, design, blinding, regulatory_pathway, primary_endpoint,
    target_enrollment, status, start_date, end_date)
SELECT
    nextval('seq_study_sk'),
    STUDYID,
    'Phase 3',
    'Endocrinology',
    'Type 2 Diabetes Mellitus',
    'Example Pharma Inc.',
    'Parallel',
    'Double-Blind',
    'NDA',
    'Change from baseline in HbA1c at Week 26',
    500,
    'Active',
    MIN(RFSTDTC::DATE),
    MAX(RFENDTC::DATE)
FROM dm_data
GROUP BY STUDYID;
```

#### Step 2: Populate dim_site

```
LOAD SDTM DM JSON
FOR each distinct (STUDYID, SITEID):
    IF NOT EXISTS in dim_site:
        INSERT INTO dim_site
    ELSE:
        UPDATE dim_site (SCD Type 1 — overwrite enrollment counts)
```

```sql
INSERT INTO dim_site (site_sk, study_id, site_id, country, region, site_type,
    investigator_name, activation_date, target_enrollment, actual_enrollment)
SELECT
    nextval('seq_site_sk'),
    d.STUDYID,
    d.SITEID,
    MAX(d.COUNTRY),
    CASE MAX(d.COUNTRY)
        WHEN 'USA' THEN 'North America'
        WHEN 'CAN' THEN 'North America'
        WHEN 'GBR' THEN 'Europe'
        WHEN 'DEU' THEN 'Europe'
        WHEN 'FRA' THEN 'Europe'
        WHEN 'JPN' THEN 'Asia-Pacific'
        WHEN 'KOR' THEN 'Asia-Pacific'
        ELSE 'Rest of World'
    END,
    'COMMUNITY',
    NULL,
    MIN(d.RFSTDTC::DATE),
    NULL,
    COUNT(DISTINCT d.USUBJID)
FROM dm_data d
GROUP BY d.STUDYID, d.SITEID;
```

#### Step 3: Populate dim_subject

```
LOAD SDTM DM JSON (demographics)
LOAD SDTM VS JSON (vital signs — for BMI)
LOAD SDTM MH JSON (medical history — for disease duration)

FOR each subject:
    HASH USUBJID with SHA256
    CALCULATE age_group from AGE
    CALCULATE bmi_category from VS data (BMI = weight / height^2)
    CALCULATE diabetes_duration_category from MH
    IF EXISTS:
        UPDATE dim_subject (SCD Type 1)
    ELSE:
        INSERT INTO dim_subject
```

```sql
INSERT INTO dim_subject (subject_sk, usubjid, site_id, age, age_group, sex,
    race, ethnicity, country, treatment_arm, randomization_date,
    bmi_category, diabetes_duration_category)
SELECT
    nextval('seq_subject_sk'),
    SHA256(d.USUBJID),
    d.SITEID,
    d.AGE,
    CASE
        WHEN d.AGE < 45 THEN '<45'
        WHEN d.AGE BETWEEN 45 AND 54 THEN '45-54'
        WHEN d.AGE BETWEEN 55 AND 64 THEN '55-64'
        WHEN d.AGE BETWEEN 65 AND 74 THEN '65-74'
        ELSE '75+'
    END,
    d.SEX,
    d.RACE,
    d.ETHNIC,
    d.COUNTRY,
    COALESCE(d.ACTARMCD, d.ARMCD),
    d.RFSTDTC::DATE,
    CASE
        WHEN bmi < 18.5 THEN 'Underweight'
        WHEN bmi < 25   THEN 'Normal'
        WHEN bmi < 30   THEN 'Overweight'
        ELSE 'Obese'
    END,
    CASE
        WHEN diabetes_years < 5  THEN '<5 years'
        WHEN diabetes_years <= 10 THEN '5-10 years'
        ELSE '>10 years'
    END
FROM dm_data d
LEFT JOIN subject_bmi b ON d.USUBJID = b.usubjid
LEFT JOIN subject_dm_dur h ON d.USUBJID = h.usubjid;
```

#### Step 4: Populate dim_treatment_arm

```
EXTRACT DISTINCT ARMCD and ARM from DM
FOR each (STUDYID, ARMCD):
    DETERMINE is_active (ARMCD not like 'PBO')
    DETERMINE is_placebo (ARMCD like 'PBO')
    INSERT INTO dim_treatment_arm
```

```sql
INSERT INTO dim_treatment_arm (arm_sk, study_id, arm_code, arm_name,
    drug_name, dose, route, frequency, is_active, is_placebo)
SELECT
    nextval('seq_arm_sk'),
    STUDYID,
    ARMCD,
    ARM,
    'Tirzepatide Analog',
    CASE ARMCD
        WHEN 'DOSE5'  THEN '5 mg'
        WHEN 'DOSE10' THEN '10 mg'
        WHEN 'DOSE15' THEN '15 mg'
        WHEN 'PBO'    THEN '0 mg'
    END,
    'SUBCUTANEOUS',
    'QW',
    ARMCD != 'PBO',
    ARMCD = 'PBO'
FROM (SELECT DISTINCT STUDYID, ARMCD, ARM FROM dm_data);
```

#### Step 5: Populate dim_visit_schedule

```
LOAD protocol visit schedule (TV domain or protocol metadata)
FOR each visit in the schedule:
    INSERT INTO dim_visit_schedule
    SET is_primary_endpoint_visit = TRUE for Week 26
```

```sql
INSERT INTO dim_visit_schedule (visit_sk, study_id, visitnum, visit_label,
    target_day, window_before_days, window_after_days, epoch,
    is_primary_endpoint_visit)
SELECT
    nextval('seq_visit_sk'),
    'T2DM-301',
    VISITNUM,
    VISIT,
    target_day,
    window_before,
    window_after,
    EPOCH,
    VISITNUM = 9.0
FROM (VALUES
    (1.0,  'Screening',            -28, 7,  7,  'SCREENING'),
    (2.0,  'Baseline/Day 1',       0,   0,  0,  'TREATMENT'),
    (3.0,  'Week 2',               14,  3,  3,  'TREATMENT'),
    (4.0,  'Week 4',               28,  3,  3,  'TREATMENT'),
    (5.0,  'Week 8',               56,  5,  5,  'TREATMENT'),
    (6.0,  'Week 12',              84,  5,  5,  'TREATMENT'),
    (7.0,  'Week 16',              112, 5,  5,  'TREATMENT'),
    (8.0,  'Week 20',              140, 5,  5,  'TREATMENT'),
    (9.0,  'Week 26 (Primary)',    182, 7,  7,  'TREATMENT'),
    (10.0, 'Week 36',              252, 10, 10, 'TREATMENT'),
    (11.0, 'Week 52',              364, 14, 14, 'TREATMENT'),
    (12.0, 'Follow-up Week 4',     392, 7,  7,  'FOLLOW-UP')
) AS t(VISITNUM, VISIT, target_day, window_before, window_after, EPOCH);
```

#### Step 6: Populate dim_meddra

```
LOAD SDTM AE JSON
EXTRACT DISTINCT AEBODSYS, AEDECOD
FOR each distinct PT:
    IF NOT EXISTS in dim_meddra:
        INSERT (with HLT/HLGT from MedDRA hierarchy lookup)
```

```sql
INSERT INTO dim_meddra (meddra_sk, pt_code, pt_name, hlt_name, hlgt_name,
    soc_name, is_serious_risk)
SELECT DISTINCT
    nextval('seq_meddra_sk'),
    LEFT(MD5(AEDECOD), 8),
    AEDECOD,
    NULL,  -- Populate from MedDRA hierarchy if available
    NULL,
    AEBODSYS,
    CASE AEDECOD
        WHEN 'Acute pancreatitis'  THEN TRUE
        WHEN 'Cardiac arrest'      THEN TRUE
        WHEN 'Hypoglycaemia'       THEN TRUE
        WHEN 'Anaphylactic reaction' THEN TRUE
        WHEN 'Hepatic failure'     THEN TRUE
        WHEN 'Acute kidney injury' THEN TRUE
        ELSE FALSE
    END
FROM ae_data;
```

#### Step 7: Populate dim_lab_test

```
LOAD SDTM LB JSON
EXTRACT DISTINCT LBTESTCD, LBTEST, LBCAT, LBLOINC, LBORRESU
FOR each distinct test:
    IF EXISTS: UPDATE (SCD Type 1)
    ELSE: INSERT
```

```sql
INSERT INTO dim_lab_test (lab_test_sk, test_code, test_name, loinc_code,
    category, unit, lower_normal_range, upper_normal_range,
    is_liver_function, is_kidney_function)
SELECT DISTINCT
    nextval('seq_lab_test_sk'),
    LBTESTCD,
    LBTEST,
    LBLOINC,
    LBCAT,
    LBORRESU,
    LBSTNRLO,
    LBSTNRHI,
    LBTESTCD IN ('ALT', 'AST', 'ALP', 'BILI', 'GGT', 'LDH'),
    LBTESTCD IN ('CREAT', 'BUN', 'EGFR', 'CYSC')
FROM lb_data;
```

#### Step 8: Populate fact_enrollment

```
FOR each subject in DM:
    LOOKUP study_sk, site_sk, subject_sk, arm_sk via natural keys
    COMPUTE days_since_first_enrollment = enrollment_date - MIN(enrollment_date)
    DETERMINE is_screen_failure from DS domain
    INSERT INTO fact_enrollment
```

```sql
INSERT INTO fact_enrollment (enrollment_sk, study_sk, site_sk, subject_sk,
    arm_sk, enrollment_date, days_since_first_enrollment, is_screen_failure)
SELECT
    nextval('seq_enrollment_sk'),
    st.study_sk,
    si.site_sk,
    su.subject_sk,
    a.arm_sk,
    d.RFSTDTC::DATE,
    d.RFSTDTC::DATE - first_enrollment.first_date,
    EXISTS (
        SELECT 1 FROM ds_data ds
        WHERE ds.USUBJID = d.USUBJID AND ds.DSDECOD = 'SCREEN FAILURE'
    )
FROM dm_data d
JOIN dim_study st         ON d.STUDYID = st.study_id
JOIN dim_site si          ON d.STUDYID = si.study_id AND d.SITEID = si.site_id
JOIN dim_subject su       ON SHA256(d.USUBJID) = su.usubjid
JOIN dim_treatment_arm a  ON d.STUDYID = a.study_id
                            AND COALESCE(d.ACTARMCD, d.ARMCD) = a.arm_code
CROSS JOIN (SELECT MIN(RFSTDTC::DATE) AS first_date FROM dm_data) first_enrollment;
```

#### Step 9: Populate fact_visit

```
LOAD SDTM SV JSON (subject visits) or derive from VS/LB/AE timestamps
FOR each subject visit occurrence:
    MATCH to dim_visit_schedule by study_id and visitnum
    COMPUTE days_from_target = actual_date - target_day
    SET is_within_window = days_from_target BETWEEN -window_before AND window_after
    SET visit_status = 'COMPLETED', 'MISSED', or 'EARLY_TERM'
```

```sql
INSERT INTO fact_visit (visit_sk_surr, study_sk, site_sk, subject_sk,
    visit_schedule_sk, actual_visit_date, days_from_target, is_within_window,
    visit_status)
SELECT
    nextval('seq_visit_fact_sk'),
    st.study_sk,
    si.site_sk,
    su.subject_sk,
    vs.visit_sk,
    sv.visit_date,
    sv.study_day - vs.target_day,
    (sv.study_day - vs.target_day) BETWEEN -vs.window_before_days
                                      AND vs.window_after_days,
    'COMPLETED'
FROM visit_data sv
JOIN dim_study st                ON sv.study_id = st.study_id
JOIN dim_site si                 ON sv.study_id = si.study_id
                                    AND sv.site_id = si.site_id
JOIN dim_subject su              ON SHA256(sv.usubjid) = su.usubjid
JOIN dim_visit_schedule vs       ON sv.study_id = vs.study_id
                                    AND sv.visitnum = vs.visitnum;
```

#### Step 10: Populate fact_adverse_event

```
LOAD SDTM AE JSON
FOR each AE record:
    MAP AEBODSYS + AEDECOD to dim_meddra
    DETERMINE is_treatment_emergent (AE onset >= first dose date)
    DETERMINE is_drug_related from AEREL
    COMPUTE days_to_resolution = AEENDTC - AESTDTC
    INSERT
```

```sql
INSERT INTO fact_adverse_event (ae_sk, study_sk, subject_sk, meddra_sk,
    visit_schedule_sk, ae_start_date, ae_end_date, severity_numeric,
    is_serious, is_treatment_emergent, is_drug_related, days_to_resolution)
SELECT
    nextval('seq_ae_sk'),
    st.study_sk,
    su.subject_sk,
    m.meddra_sk,
    COALESCE(vs.visit_sk, (SELECT visit_sk FROM dim_visit_schedule
        WHERE study_id = ae.STUDYID AND visitnum = 1.0)),
    ae.AESTDTC::DATE,
    ae.AEENDTC::DATE,
    CASE ae.AETOXGR
        WHEN '1' THEN 1 WHEN '2' THEN 2 WHEN '3' THEN 3
        WHEN '4' THEN 4 WHEN '5' THEN 5 ELSE NULL END,
    ae.AESER = 'Y',
    ae.AESTDTC::DATE >= COALESCE(su.randomization_date, '1900-01-01'),
    ae.AEREL IN ('POSSIBLE', 'PROBABLE', 'DEFINITE'),
    ae.AEENDTC::DATE - ae.AESTDTC::DATE
FROM ae_data ae
JOIN dim_study st       ON ae.STUDYID = st.study_id
JOIN dim_subject su     ON SHA256(ae.USUBJID) = su.usubjid
JOIN dim_meddra m       ON ae.AEBODSYS = m.soc_name
                           AND ae.AEDECOD = m.pt_name
LEFT JOIN dim_visit_schedule vs
    ON ae.STUDYID = vs.study_id
    AND vs.visitnum = (SELECT MAX(v2.visitnum) FROM dim_visit_schedule v2
        WHERE v2.study_id = ae.STUDYID AND v2.target_day <=
            (ae.AESTDTC::DATE - su.randomization_date)
    );
```

#### Step 11: Populate fact_exposure

```
LOAD SDTM EX JSON
FOR each exposure record:
    COMPUTE cumulative_dose as running sum of dose_amount per subject
    COMPUTE days_on_treatment as (current_date - first_dose_date)
    COMPUTE dose_modification_count from EX records with dose changes
    COMPUTE compliance_pct = doses_received / doses_planned * 100
```

```sql
INSERT INTO fact_exposure (exposure_sk, study_sk, subject_sk, arm_sk,
    dose_date, dose_amount, cumulative_dose, days_on_treatment,
    dose_modification_count, compliance_pct)
SELECT
    nextval('seq_exposure_sk'),
    st.study_sk,
    su.subject_sk,
    a.arm_sk,
    ex.EXSTDTC::DATE,
    ex.EXDOSE::DOUBLE,
    SUM(ex.EXDOSE::DOUBLE) OVER (
        PARTITION BY ex.USUBJID ORDER BY ex.EXSTDTC::DATE
    ),
    ex.EXSTDTC::DATE - su.randomization_date,
    COUNT(CASE WHEN ex.EXDOSCHG = 'Y' THEN 1 END) OVER (
        PARTITION BY ex.USUBJID ORDER BY ex.EXSTDTC::DATE
    ),
    ROUND(100.0 * COUNT(*) OVER (PARTITION BY ex.USUBJID)
        / (ex.EXSTDTC::DATE - su.randomization_date + 1), 1)
FROM ex_data ex
JOIN dim_study st        ON ex.STUDYID = st.study_id
JOIN dim_subject su      ON SHA256(ex.USUBJID) = su.usubjid
JOIN dim_treatment_arm a ON ex.STUDYID = a.study_id
                            AND COALESCE(ex.EXARMCD, su.treatment_arm) = a.arm_code;
```

#### Step 12: Populate fact_efficacy

```
LOAD ADaM ADLB/ADVS or derived efficacy JSON
FOR each efficacy assessment:
    COMPUTE change_from_baseline = assessment_value - baseline_value
    COMPUTE pct_change = (change / baseline) * 100
    DETERMINE is_responder (e.g., HbA1c reduction >= 0.5% AND >= 7% absolute)
    SET is_primary_endpoint_visit from visit_schedule
```

```sql
INSERT INTO fact_efficacy (efficacy_sk, study_sk, subject_sk,
    visit_schedule_sk, arm_sk, baseline_value, assessment_value,
    change_from_baseline, pct_change_from_baseline, is_responder,
    is_primary_endpoint_visit)
SELECT
    nextval('seq_efficacy_sk'),
    st.study_sk,
    su.subject_sk,
    vs.visit_sk,
    a.arm_sk,
    base.hba1c AS baseline_value,
    eff.aval AS assessment_value,
    eff.aval - base.hba1c AS change_from_baseline,
    ROUND(100.0 * (eff.aval - base.hba1c) / NULLIF(base.hba1c, 0), 2),
    (eff.aval <= 7.0 OR (eff.aval - base.hba1c) <= -0.5),
    vs.is_primary_endpoint_visit
FROM efficacy_data eff
JOIN dim_study st            ON eff.study_id = st.study_id
JOIN dim_subject su          ON SHA256(eff.usubjid) = su.usubjid
JOIN dim_visit_schedule vs   ON eff.study_id = vs.study_id
                                AND eff.visitnum = vs.visitnum
JOIN dim_treatment_arm a     ON eff.study_id = a.study_id
                                AND eff.arm_code = a.arm_code
LEFT JOIN baseline_efficacy base
    ON eff.usubjid = base.usubjid AND eff.test_code = base.test_code;
```

#### Step 13: Populate fact_lab_result

```
LOAD SDTM LB JSON
FOR each lab result:
    MAP LBTESTCD to dim_lab_test
    DETERMINE is_abnormal from LBNRIND (LOW/HIGH)
    DETERMINE toxicity_grade from LBTOXGR or compute from result vs normal range
    SET is_clinically_significant from LBCLSIG = 'Y'
```

```sql
INSERT INTO fact_lab_result (lab_result_sk, study_sk, subject_sk,
    lab_test_sk, visit_schedule_sk, result_value, result_text,
    baseline_value, change_from_baseline, pct_change, is_abnormal,
    toxicity_grade, is_clinically_significant)
SELECT
    nextval('seq_lab_result_sk'),
    st.study_sk,
    su.subject_sk,
    lt.lab_test_sk,
    vs.visit_sk,
    lb.LBSTRESN::DOUBLE,
    lb.LBSTRESC,
    base.base_val,
    lb.LBSTRESN::DOUBLE - base.base_val,
    ROUND(100.0 * (lb.LBSTRESN::DOUBLE - base.base_val)
        / NULLIF(base.base_val, 0), 2),
    lb.LBNRIND IN ('L', 'LOW', 'H', 'HIGH'),
    lb.LBTOXGR::SMALLINT,
    lb.LBCLSIG = 'Y'
FROM lb_data lb
JOIN dim_study st          ON lb.STUDYID = st.study_id
JOIN dim_subject su        ON SHA256(lb.USUBJID) = su.usubjid
JOIN dim_lab_test lt       ON lb.LBTESTCD = lt.test_code
JOIN dim_visit_schedule vs ON lb.STUDYID = vs.study_id
                              AND lb.VISITNUM = vs.visitnum
LEFT JOIN baseline_labs base
    ON lb.USUBJID = base.usubjid
    AND lb.LBTESTCD = base.lbtestcd
    AND base.visitnum = 2.0;
```

---

## Example Analytical Queries

These are ready-to-execute DuckDB SQL queries for common clinical trial analytics.

### Query 1: Subject Enrollment by Site and Month

```sql
-- Monthly enrollment trends per site — for enrollment dashboard
SELECT
    si.site_name,
    DATE_TRUNC('month', fe.enrollment_date) AS month,
    COUNT(*) AS enrolled,
    SUM(CASE WHEN NOT fe.is_screen_failure THEN 1 ELSE 0 END) AS randomized,
    ROUND(100.0 * SUM(CASE WHEN fe.is_screen_failure THEN 1 ELSE 0 END)
        / COUNT(*), 1) AS screen_fail_pct
FROM fact_enrollment fe
JOIN dim_site si ON fe.site_sk = si.site_sk
GROUP BY si.site_name, DATE_TRUNC('month', fe.enrollment_date)
ORDER BY month, si.site_name;
```

### Query 2: Adverse Event Rate by Treatment Arm and SOC

```sql
-- AE incidence by system organ class and arm — for safety review dashboard
SELECT
    a.arm_name,
    m.soc_name,
    COUNT(DISTINCT ae.subject_sk) AS subjects_with_ae,
    COUNT(*) AS event_count,
    ROUND(COUNT(*)::DOUBLE / COUNT(DISTINCT ae.subject_sk), 1) AS events_per_subject,
    ROUND(100.0 * COUNT(DISTINCT ae.subject_sk)
        / NULLIF(
            (SELECT COUNT(DISTINCT fe.subject_sk)
             FROM fact_enrollment fe
             JOIN dim_subject s ON fe.subject_sk = s.subject_sk
             WHERE s.treatment_arm = a.arm_code AND NOT fe.is_screen_failure
            ), 0), 1) AS incidence_pct
FROM fact_adverse_event ae
JOIN dim_meddra m ON ae.meddra_sk = m.meddra_sk
JOIN dim_subject subj ON ae.subject_sk = subj.subject_sk
JOIN dim_treatment_arm a ON subj.treatment_arm = a.arm_code
WHERE ae.is_treatment_emergent
GROUP BY a.arm_name, m.soc_name
ORDER BY incidence_pct DESC;
```

### Query 3: Lab Toxicity Shift Analysis (Baseline to Week 26)

```sql
-- Shift table: baseline toxicity grade vs. post-baseline grade
-- For liver function tests, comparing baseline to Week 26
WITH baseline AS (
    SELECT lr.subject_sk, lt.test_code, lt.test_name,
        COALESCE(lr.toxicity_grade, 0) AS baseline_tox
    FROM fact_lab_result lr
    JOIN dim_lab_test lt ON lr.lab_test_sk = lt.lab_test_sk
    JOIN dim_visit_schedule vs ON lr.visit_schedule_sk = vs.visit_sk
    WHERE vs.visitnum = 2.0 AND lt.is_liver_function
),
week26 AS (
    SELECT lr.subject_sk, lt.test_code,
        COALESCE(lr.toxicity_grade, 0) AS post_tox
    FROM fact_lab_result lr
    JOIN dim_lab_test lt ON lr.lab_test_sk = lt.lab_test_sk
    JOIN dim_visit_schedule vs ON lr.visit_schedule_sk = vs.visit_sk
    WHERE vs.visitnum = 9.0 AND lt.is_liver_function
)
SELECT
    b.test_name,
    b.baseline_tox AS "Baseline Grade",
    w.post_tox AS "Week 26 Grade",
    COUNT(*) AS n
FROM baseline b
JOIN week26 w ON b.subject_sk = w.subject_sk AND b.test_code = w.test_code
GROUP BY b.test_name, b.baseline_tox, w.post_tox
ORDER BY b.test_name, b.baseline_tox, w.post_tox;
```

### Query 4: Efficacy Waterfall Plot Data (Best % Change from Baseline)

```sql
-- Best percent change from baseline per subject — for waterfall plot
SELECT
    subj.usubjid,
    a.arm_name,
    MIN(eff.pct_change_from_baseline) AS best_pct_change
FROM fact_efficacy eff
JOIN dim_subject subj ON eff.subject_sk = subj.subject_sk
JOIN dim_treatment_arm a ON eff.arm_sk = a.arm_sk
WHERE a.is_active
  AND eff.pct_change_from_baseline IS NOT NULL
GROUP BY subj.usubjid, a.arm_name
ORDER BY best_pct_change ASC;
```

### Query 5: Kaplan-Meier Data for Discontinuation Time

```sql
-- Time to discontinuation data (KM input) per subject
-- Requires disposition data — adapt to your DS source
WITH subject_status AS (
    SELECT DISTINCT
        subj.subject_sk,
        subj.usubjid,
        subj.treatment_arm,
        COALESCE(MAX(e.days_on_treatment), 0) AS last_day,
        MAX(CASE WHEN ds.DSDECOD IN ('DISCONTINUED', 'WITHDREW CONSENT',
                     'ADVERSE EVENT', 'LOST TO FOLLOW-UP', 'DEATH')
            THEN 1 ELSE 0 END) AS event_flag
    FROM dim_subject subj
    LEFT JOIN fact_exposure e ON subj.subject_sk = e.subject_sk
    LEFT JOIN ds_data ds ON subj.usubjid = SHA256(ds.USUBJID)
    GROUP BY subj.subject_sk, subj.usubjid, subj.treatment_arm
)
SELECT
    treatment_arm,
    last_day,
    event_flag,
    COUNT(*) OVER (PARTITION BY treatment_arm ORDER BY last_day) AS n_at_risk
FROM subject_status
ORDER BY treatment_arm, last_day;
```

### Query 6: CRF Page Completion Rate by Site

```sql
-- Case Report Form completion — visits completed vs. expected, by site
SELECT
    si.site_name,
    vs.epoch,
    COUNT(*) AS expected_visits,
    SUM(CASE WHEN fv.visit_status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed,
    SUM(CASE WHEN fv.visit_status = 'MISSED' THEN 1 ELSE 0 END) AS missed,
    ROUND(100.0 * SUM(CASE WHEN fv.visit_status = 'COMPLETED' THEN 1 ELSE 0 END)
        / COUNT(*), 1) AS crf_completion_pct,
    ROUND(AVG(CASE WHEN fv.is_within_window THEN 1.0 ELSE 0.0 END) * 100, 1)
        AS window_compliance_pct
FROM fact_visit fv
JOIN dim_site si ON fv.site_sk = si.site_sk
JOIN dim_visit_schedule vs ON fv.visit_schedule_sk = vs.visit_sk
GROUP BY si.site_name, vs.epoch
ORDER BY si.site_name, vs.epoch;
```

---

## BI Tool Integration

### Metabase

Metabase connects natively to DuckDB via the DuckDB JDBC driver or through a PostgreSQL proxy.

**Connection steps:**
1. Install `duckdb_jdbc` driver in Metabase plugins directory
2. Add DuckDB data source with path to `trial_analytics.duckdb`
3. Create questions using the SQL editor or Query Builder
4. Useful for: enrollment dashboards, AE monitoring, lab shift tables

**Recommended dashboard layout:**
- **Enrollment tab**: Monthly enrollment, site activation, screen failure rate (from fact_enrollment + dim_site)
- **Safety tab**: AE incidence by SOC, severity heatmap, SAE listing (from fact_adverse_event + dim_meddra)
- **Labs tab**: Shift tables for LFTs, KFTs, HbA1c over time (from fact_lab_result + dim_lab_test)
- **Efficacy tab**: Waterfall plot data export, mean change over time (from fact_efficacy + dim_treatment_arm)

### Tableau

Tableau connects via ODBC or a DuckDB-to-PostgreSQL bridge.

**Connection steps:**
1. Use DuckDB's `ATTACH` to mount a PostgreSQL instance: `ATTACH 'postgresql://...' AS pg (TYPE POSTGRES);`
2. Copy tables: `CREATE TABLE pg.dim_study AS SELECT * FROM dim_study;`
3. Point Tableau to the PostgreSQL database
4. Define relationships in Tableau Data Source (star schema joins auto-detected)
5. Create calculated fields for: incidence rates, KM survival curves, forest plot data

**Recommended workbook structure:**
- **Data Source**: DuckDB live connection (read-only) with all 13 tables
- **Relationships**: Define in Tableau Data Source pane (dimension facts)
- **Custom SQL**: Pass-through for complex queries (e.g., shift tables require CTEs)

### Streamlit

Streamlit connects via the `duckdb` Python package directly.

```python
import streamlit as st
import duckdb
import pandas as pd
import plotly.express as px

# Connect to DuckDB
con = duckdb.connect('trial_analytics.duckdb')

# Enrollment over time
df_enrollment = con.execute("""
    SELECT DATE_TRUNC('month', fe.enrollment_date) AS month,
           COUNT(*) AS enrolled
    FROM fact_enrollment fe
    WHERE NOT fe.is_screen_failure
    GROUP BY month ORDER BY month
""").df()

st.line_chart(df_enrollment.set_index('month'))

# AE by SOC — treemap
df_ae = con.execute("""
    SELECT m.soc_name, COUNT(*) AS n
    FROM fact_adverse_event ae
    JOIN dim_meddra m ON ae.meddra_sk = m.meddra_sk
    WHERE ae.is_treatment_emergent
    GROUP BY m.soc_name
""").df()

fig = px.treemap(df_ae, path=['soc_name'], values='n')
st.plotly_chart(fig)

# Lab shift table
df_shift = con.execute("""
    WITH baseline AS (...), week26 AS (...)
    SELECT * FROM baseline JOIN week26 ...
""").df()

st.dataframe(df_shift)
```

### Connecting from Python (General)

```python
import duckdb

# In-memory or persistent
con = duckdb.connect('trial_analytics.duckdb')

# Load DDL (run CREATE TABLE statements)
with open('dimensional-analytics-ddl.sql') as f:
    con.execute(f.read())

# Run ETL and populate
con.execute("INSERT INTO dim_study SELECT ...")

# Query for dashboards
df = con.execute("""
    SELECT a.arm_name, COUNT(*) AS n_ae
    FROM fact_adverse_event ae
    JOIN dim_subject subj ON ae.subject_sk = subj.subject_sk
    JOIN dim_treatment_arm a ON subj.treatment_arm = a.arm_code
    GROUP BY a.arm_name
""").df()
```

---

## Data Quality Checks

Run these queries after each ETL load to verify referential integrity and data completeness.

```sql
-- Check 1: All fact foreign keys resolve
SELECT 'fact_enrollment' AS table_name,
    COUNT(*) AS total,
    SUM(CASE WHEN st.study_sk IS NULL THEN 1 ELSE 0 END) AS orphan_fks
FROM fact_enrollment fe
LEFT JOIN dim_study st ON fe.study_sk = st.study_sk;

-- Check 2: Subject count matches between dim and fact
SELECT
    (SELECT COUNT(*) FROM dim_subject) AS dim_subjects,
    (SELECT COUNT(DISTINCT subject_sk) FROM fact_enrollment) AS fact_subjects,
    (SELECT ABS(dim_subjects - fact_subjects)) AS delta;

-- Check 3: No future dates in AE
SELECT COUNT(*) AS future_aes
FROM fact_adverse_event
WHERE ae_start_date > CURRENT_DATE;

-- Check 4: Visit compliance summary
SELECT
    epoch,
    COUNT(*) AS total_visits,
    SUM(CASE WHEN visit_status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed,
    ROUND(100.0 * SUM(CASE WHEN visit_status = 'COMPLETED' THEN 1 ELSE 0 END)
        / COUNT(*), 1) AS completion_pct
FROM fact_visit
JOIN dim_visit_schedule ON fact_visit.visit_schedule_sk = dim_visit_schedule.visit_sk
GROUP BY epoch;
```

---

## Related Skills

| Skill | Path | Relationship |
|-------|------|-------------|
| TrialSim Core | `../../SKILL.md` | Parent skill — generates the clinical trial data |
| Demographics DM | `../../domains/demographics-dm.md` | Source for dim_subject, dim_site, dim_study |
| Adverse Events AE | `../../domains/adverse-events-ae.md` | Source for fact_adverse_event, dim_meddra |
| Laboratory LB | `../../domains/laboratory-lb.md` | Source for fact_lab_result, dim_lab_test |
| Exposure EX | `../../domains/exposure-ex.md` | Source for fact_exposure, dim_treatment_arm |
| Disposition DS | `../../domains/disposition-ds.md` | Source for enrollment status, discontinuation |
| Vital Signs VS | `../../domains/vital-signs-vs.md` | Source for BMI category, efficacy data |
| Medical History MH | `../../domains/medical-history-mh.md` | Source for disease duration, comorbidity data |
| CDISC SDTM | `../../formats/cdisc-sdtm.md` | Source format for ETL input |
| CDISC ADaM | `../../formats/cdisc-adam.md` | Source for baseline and change-from-baseline measures |
| Data Models | `../../references/data-models.md` | Canonical entity schemas (Subject, Study, Site, etc.) |

---

## Performance Optimization (DuckDB)

For large multi-study deployments, apply these optimizations:

```sql
-- Create indexes (DuckDB creates them automatically for PKs; add for FK columns)
CREATE INDEX idx_fact_enrollment_study   ON fact_enrollment(study_sk);
CREATE INDEX idx_fact_enrollment_subject ON fact_enrollment(subject_sk);
CREATE INDEX idx_fact_ae_subject         ON fact_adverse_event(subject_sk);
CREATE INDEX idx_fact_ae_meddra          ON fact_adverse_event(meddra_sk);
CREATE INDEX idx_fact_lab_subject        ON fact_lab_result(subject_sk);
CREATE INDEX idx_fact_lab_test           ON fact_lab_result(lab_test_sk);
CREATE INDEX idx_fact_efficacy_subject   ON fact_efficacy(subject_sk);
CREATE INDEX idx_fact_visit_subject      ON fact_visit(subject_sk);

-- Partition large fact tables by study_sk (for multi-study deployments)
-- DuckDB supports Hive-style partitioning on write

-- Enable parallel query execution (default in DuckDB)
PRAGMA threads=4;
PRAGMA memory_limit='4GB';
```

---

## Appendix: Complete DDL Script

To create the entire star schema in one operation, execute all DDL statements in this file sequentially against a DuckDB instance. The combined script produces a ready-to-query analytics database with all 13 tables.

```sql
-- TrialSim Clinical Trial Analytics — Complete Star Schema DDL for DuckDB
-- Run: duckdb trial_analytics.duckdb < complete_ddl.sql

-- ==================== SEQUENCES ====================
CREATE SEQUENCE seq_study_sk START 1;
CREATE SEQUENCE seq_site_sk START 1;
CREATE SEQUENCE seq_subject_sk START 1;
CREATE SEQUENCE seq_arm_sk START 1;
CREATE SEQUENCE seq_visit_sk START 1;
CREATE SEQUENCE seq_meddra_sk START 1;
CREATE SEQUENCE seq_lab_test_sk START 1;
CREATE SEQUENCE seq_enrollment_sk START 1;
CREATE SEQUENCE seq_visit_fact_sk START 1;
CREATE SEQUENCE seq_ae_sk START 1;
CREATE SEQUENCE seq_exposure_sk START 1;
CREATE SEQUENCE seq_efficacy_sk START 1;
CREATE SEQUENCE seq_lab_result_sk START 1;

-- ==================== DIMENSION TABLES ====================
-- DDL for dim_study, dim_site, dim_subject, dim_treatment_arm,
-- dim_visit_schedule, dim_meddra, dim_lab_test as defined above

-- ==================== FACT TABLES ====================
-- DDL for fact_enrollment, fact_visit, fact_adverse_event,
-- fact_exposure, fact_efficacy, fact_lab_result as defined above
```

---

## Schema Versioning

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2025-06-10 | Initial star schema: 7 dimensions, 6 facts |
| 1.0.1 | 2025-06-10 | Added BI integration notes, ETL pseudocode, data quality checks |

---

*Generated by TrialSim — Synthetic Clinical Trial Data Engine*
