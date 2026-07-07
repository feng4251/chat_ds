---
name: cns
description: |
  Generate CNS (Central Nervous System) clinical trial data including depression
  (MADRS), schizophrenia (PANSS), Alzheimer's disease (ADAS-Cog, MMSE, CDR-SB),
  multiple sclerosis (EDSS), CGI severity/improvement scales, and common CNS
  adverse event patterns. Triggers: "CNS trial", "depression", "schizophrenia",
  "Alzheimer's", "MS clinical trial", "MADRS", "PANSS", "ADAS-Cog", "MMSE",
  "EDSS", "CGI", "neuropsychiatric", "neurological scale".
---

# CNS / Neurology Therapeutic Area

The CNS therapeutic area covers psychiatric disorders (depression, schizophrenia) and neurological conditions (Alzheimer's disease, multiple sclerosis, Parkinson's). Core assessment frameworks include standardized rating scales, clinician global impressions, and CNS-specific adverse event monitoring.

---

## For Claude

This is a **therapeutic area skill** for generating CNS/neurology trial data. CNS trials are characterized by **subjective rating scales, rater training requirements, and high placebo response rates**.

**Always apply this skill when you see:**
- Major Depressive Disorder (MDD) trials using MADRS or HAM-D
- Schizophrenia trials using PANSS (Positive and Negative Syndrome Scale)
- Alzheimer's disease trials using ADAS-Cog, MMSE, or CDR-SB
- Multiple Sclerosis trials using EDSS (Expanded Disability Status Scale)
- Clinical Global Impression (CGI) scales (severity and improvement)
- CNS-specific adverse events (suicidality, EPS, sedation, weight gain)
- Rater training, inter-rater reliability, or site-independent review for CNS scales
- Placebo response mitigation strategies in psychiatric trials

**Key responsibilities:**
- Generate realistic scale scores with correct distributions and ranges
- Apply disease-appropriate rate of change assumptions (decline/stability)
- Model placebo response patterns (magnitude ~2-8 points on MADRS; >20% on PANSS)
- Map CGI-S scores to disease-specific scale score ranges
- Generate CNS-specific AE profiles (metabolic, extrapyramidal, sedative, cognitive)

---

## Depression: MADRS and HAM-D

### MADRS (Montgomery-Asberg Depression Rating Scale)

| Parameter | Value |
|-----------|-------|
| Score Range | 0 to 60 |
| Items | 10 items, each scored 0-6 |
| Severity: Mild | 7-19 |
| Severity: Moderate | 20-34 |
| Severity: Severe | ≥35 |
| Remission | ≤10 (or ≤12, depending on convention) |
| Response | ≥50% reduction from baseline |
| MCID (Minimal Clinically Important Difference) | 2 points (anchor-based) |
| Typical Entry Criterion (MDD) | ≥26 (baseline) |
| Typical Placebo Change at Week 8 | -12 to -16 points (SD ~10) |
| Expected Drug-Placebo Difference | 2-4 points (often ~2.5-3.0 at Week 8) |

### MADRS Item Structure

| Item # | Domain | Scoring |
|--------|--------|---------|
| 1 | Apparent Sadness | 0 (no sadness) to 6 (looks miserable, extreme gloom) |
| 2 | Reported Sadness | 0 to 6 |
| 3 | Inner Tension | 0 to 6 |
| 4 | Reduced Sleep | 0 (sleeps normally) to 6 (>5 hrs reduced vs normal) |
| 5 | Reduced Appetite | 0 to 6 |
| 6 | Concentration Difficulties | 0 (no difficulty) to 6 (unable to read or converse) |
| 7 | Lassitude | 0 to 6 |
| 8 | Inability to Feel | 0 to 6 |
| 9 | Pessimistic Thoughts | 0 to 6 |
| 10 | Suicidal Thoughts | 0 (enjoys life) to 6 (explicit suicide plans when possible) |

### HAM-D (Hamilton Depression Rating Scale)

| Parameter | Value |
|-----------|-------|
| Score Range | 0-52 (17-item); 0-54 (21-item) |
| Mild Depression | 8-16 |
| Moderate Depression | 17-23 |
| Severe Depression | ≥24 |
| Remission | ≤7 |
| Typical Entry Criterion | ≥18 (17-item) or ≥22 (21-item) |

---

## Schizophrenia: PANSS

### PANSS (Positive and Negative Syndrome Scale)

| Parameter | Value |
|-----------|-------|
| Score Range | 30 to 210 |
| Number of Items | 30 items, each scored 1 (absent) to 7 (extreme) |
| Subscales | Positive (7 items, 7-49), Negative (7 items, 7-49), General Psychopathology (16 items, 16-112) |
| Mildly Ill | 58 |
| Moderately Ill | 75 |
| Markedly Ill | 95 |
| Severely Ill | 116 |
| Response | ≥20% or ≥30% reduction from baseline (varies by trial) |
| Typical Entry Criterion | ≥70 or ≥80 total; at least 1 positive item ≥4 |
| Placebo Response (Week 6) | -10 to -15 points mean change |
| Drug-Placebo Difference | 5-10 points |

### PANSS Factor Models

| Factor | Items | Interpretation |
|--------|-------|----------------|
| Positive Factor | P1, P3, P5, G9 | Delusions, hallucinatory behavior, grandiosity, unusual thought content |
| Negative Factor | N1, N2, N3, N4, N6, G7, G16 | Blunted affect, emotional withdrawal, poor rapport, passive/apathetic social withdrawal, lack of spontaneity, motor retardation, active social avoidance |
| Disorganized Factor | P2, N5, G5, G10, G11, G12, G13, G15 | Conceptual disorganization, abstract thinking difficulty, mannerisms/posturing, disorientation, poor attention, judgment/insight loss, volition disturbance, preoccupation |
| Excited Factor | P4, P7, G8, G14 | Excitement, hostility, uncooperativeness, poor impulse control |
| Anxiety/Depression Factor | G1, G2, G3, G4, G6 | Somatic concern, anxiety, guilt feelings, tension, depression |

---

## Alzheimer's Disease: ADAS-Cog, MMSE, CDR-SB

### ADAS-Cog (Alzheimer's Disease Assessment Scale - Cognitive Subscale)

| Parameter | Value |
|-----------|-------|
| Score Range | 0 to 70 (11-item); 0 to 85 (13 or 14-item) |
| Higher Score | Worse cognitive impairment |
| Typical Baseline (Mild-Mod AD) | 18-26 |
| Annual Decline (Untreated) | 5-8 points |
| MCID | 2-3 points (anchor-based) |
| Typical Entry Criterion | ≥12 or ≥18 (varies by prodromal vs mild AD) |
| Drug-Placebo Difference at 18 Months | 1.5-2.5 points (anti-amyloid agents) |

### ADAS-Cog Domains

| Subscale | Items | Scoring |
|----------|-------|---------|
| Memory | Word recall (0-10), Word recognition (0-12), Recall test instructions (0-5), Orientation (0-8) | Higher = worse |
| Language | Naming objects/fingers (0-5), Commands (0-5), Comprehension (0-5), Word finding (0-5), Spoken language (0-5) | Higher = worse |
| Praxis | Constructional praxis (0-5), Ideational praxis (0-5) | Higher = worse |

### MMSE (Mini-Mental State Examination)

| Parameter | Value |
|-----------|-------|
| Score Range | 0 to 30 |
| Lower Score | Worse cognitive impairment |
| Normal | 24-30 |
| Mild Cognitive Impairment | 19-23 |
| Mild Alzheimer's | 19-23 |
| Moderate Alzheimer's | 10-18 |
| Severe Alzheimer's | <10 |
| Typical Entry Criterion (Mild-Mod AD) | 16 to 26 |
| Annual Decline (Untreated Mild-Mod AD) | 2-4 points |
| MCID | 1 point |
| Copyright | Proprietary (PAR); requires license for use |

### CDR-SB (Clinical Dementia Rating - Sum of Boxes)

| Parameter | Value |
|-----------|-------|
| Score Range | 0 to 18 |
| Higher Score | Worse |
| CDR Global 0 (Normal) | CDR-SB 0 |
| CDR Global 0.5 (Questionable/MCI) | CDR-SB 0.5-4.0 |
| CDR Global 1 (Mild Dementia) | CDR-SB 4.5-9.0 |
| CDR Global 2 (Moderate Dementia) | CDR-SB 9.5-15.5 |
| CDR Global 3 (Severe Dementia) | CDR-SB 16.0-18.0 |
| Typical Entry (Early AD/MCI) | CDR Global 0.5, CDR-SB ≥0.5 |
| Annual Decline (Mild AD) | 1.5-2.5 points |
| Drug-Placebo Difference at 18M | 0.4-0.7 points |

---

## Multiple Sclerosis: EDSS

### EDSS (Expanded Disability Status Scale)

| Parameter | Value |
|-----------|-------|
| Score Range | 0.0 to 10.0 (in 0.5 increments) |
| 0.0 | Normal neurological exam |
| 1.0-2.5 | Minimal to mild disability in one or more functional systems |
| 3.0-4.5 | Moderate disability; fully ambulatory |
| 5.0-5.5 | Disability impairs full daily activities; walk 200m without aid |
| 6.0 | Requires unilateral assistance to walk 100m |
| 6.5 | Requires bilateral assistance to walk 20m |
| 7.0-7.5 | Wheelchair-bound; unable to walk >5m |
| 8.0-8.5 | Restricted to bed/chair |
| 9.0-9.5 | Helpless bed patient |
| 10.0 | Death due to MS |

### EDSS Functional Systems

| Functional System (FS) | Score Range |
|------------------------|------------|
| Pyramidal | 0-6 |
| Cerebellar | 0-5 |
| Brainstem | 0-5 |
| Sensory | 0-6 |
| Bowel and Bladder | 0-6 |
| Visual | 0-6 |
| Cerebral (Mental) | 0-5 |
| Ambulation | Determined by distance; anchors EDSS ≥4.0 |

### Key MS Trial Endpoints by Subtype

| MS Subtype | Typical Primary Endpoint | Typical Duration |
|------------|-------------------------|------------------|
| RRMS (Relapsing-Remitting) | Annualized Relapse Rate (ARR) | 2 years |
| RRMS | Confirmed Disability Progression (CDP, ≥1.0 EDSS sustained 12/24w) | 2 years |
| PPMS (Primary Progressive) | CDP (≥1.0 EDSS sustained 12/24w) | 2+ years |
| SPMS (Secondary Progressive) | CDP (≥1.0 EDSS sustained 12/24w) | 2+ years |

---

## CGI Scales

### CGI-S (Clinical Global Impression - Severity)

| Score | Label | MADRS Range | PANSS Range | Description |
|-------|-------|-------------|-------------|-------------|
| 1 | Normal, not at all ill | 0-6 | 30-35 | No symptoms |
| 2 | Borderline mentally ill | 7-12 | 36-45 | Subtle symptoms |
| 3 | Mildly ill | 13-22 | 46-65 | Mild symptoms; functional |
| 4 | Moderately ill | 23-30 | 66-85 | Obvious symptoms; some dysfunction |
| 5 | Markedly ill | 31-44 | 86-110 | Significant symptoms; impaired function |
| 6 | Severely ill | 45-55 | 111-140 | Severe; requires supervision |
| 7 | Among the most extremely ill | 56-60 | 141-210 | Extreme; constant supervision |

### CGI-I (Clinical Global Impression - Improvement)

| Score | Label | Description |
|-------|-------|-------------|
| 1 | Very much improved | Near complete remission |
| 2 | Much improved | Significant improvement; residual symptoms |
| 3 | Minimally improved | Slight meaningful improvement |
| 4 | No change | Essentially unchanged |
| 5 | Minimally worse | Slight worsening |
| 6 | Much worse | Clinically significant worsening |
| 7 | Very much worse | Marked worsening |

---

## CNS Adverse Event Patterns

### By Drug Class

| Drug Class | Characteristic AEs | Incidence | Clinical Concern |
|------------|-------------------|-----------|------------------|
| SSRIs/SNRIs | Nausea, Sexual dysfunction, Insomnia, Somnolence, Weight gain | 15-35% | QTc prolongation (citalopram >40mg); Serotonin syndrome (rare) |
| Atypical Antipsychotics | Weight gain, Sedation, EPS, Metabolic syndrome, Hyperprolactinemia | 20-50% | Metabolic monitoring required (glucose, lipids, weight) |
| Typical Antipsychotics | EPS (akathisia, dystonia, parkinsonism), Tardive dyskinesia, Hyperprolactinemia | 30-60% | Tardive dyskinesia (cumulative, potentially irreversible) |
| Anti-Amyloid mAbs | ARIA-E (edema), ARIA-H (microhemorrhage), Infusion reactions | 10-35% | MRI monitoring; APOE4 screening; contra-indicated with anticoagulation |
| MS DMTs (immunomodulators) | Injection site reactions (30%), Flu-like symptoms (50%) | 30-50% | LFT monitoring; PML risk (natalizumab) |
| MS DMTs (oral) | Flushing/GI (dimethyl fumarate), Lymphopenia, LFT elevation | 10-30% | PML risk; teratogenicity |
| Benzodiazepines | Sedation, Cognitive impairment, Dependence, Withdrawal | 20-50% | Risk of abuse and dependence; fall risk in elderly |
| Stimulants (ADHD) | Insomnia, Appetite suppression, Weight loss, BP/HR increase | 15-30% | Abuse potential; cardiovascular monitoring |

### CNS-Specific Safety Assessments

| Domain | Assessment | Frequency |
|--------|------------|-----------|
| Suicidality | C-SSRS (Columbia Suicide Severity Rating Scale) | Every visit |
| Extrapyramidal Symptoms | AIMS (Abnormal Involuntary Movement Scale), SAS (Simpson-Angus Scale), BAS (Barnes Akathisia Scale) | Screening, Week 6, Week 12, EOT |
| Metabolic Monitoring | Fasting glucose, HbA1c, Lipid panel, Weight, Waist circumference | Screening, Week 12, 24, 48 |
| Sedation/Somnolence | ESS (Epworth Sleepiness Scale) or direct questioning | Every visit |
| Cognitive Effects | Specific cognitive battery or screening instrument | Screening, End of study |
| QTc Prolongation | 12-lead ECG | Screening, PK timepoints, EOT |

---

## Examples

### Example 1: Depression Trial (MADRS)

**Request:** "Generate MADRS trial data for a Phase III MDD study with SSRI augmentation"

**Output:**

```json
{
  "therapeutic_area": "cns",
  "indication": "Major Depressive Disorder",
  "design": {
    "phase": "Phase III",
    "randomization": "1:1",
    "primary_endpoint": "Change from baseline in MADRS total score at Week 8",
    "key_secondary": ["MADRS response rate (≥50% reduction)", "MADRS remission rate (≤10)", "CGI-I responder (score 1 or 2)"],
    "n_planned": 520,
    "treatment_duration_weeks": 12,
    "taper_period_weeks": 2
  },
  "inclusion_criteria": {
    "madrs_baseline": "≥26 at both screening and baseline",
    "mdd_diagnosis": "DSM-5 criteria, current MDE ≥8 weeks, recurrent or chronic",
    "age": "18-65",
    "cgi_s_baseline": "≥4 (moderately ill or worse)",
    "stability": "No more than 20% improvement in MADRS between screen and baseline"
  },
  "sample_patient": {
    "USUBJID": "MDD-001-002-0034",
    "demographics": {"age": 42, "sex": "Female", "race": "White"},
    "baseline": {
      "madrs_total": 34,
      "cgi_s": 5,
      "qids_sr16": 18,
      "sheehan_disability_scale": 22
    },
    "assessments": [
      {"week": 1, "madrs_total": 29, "cgi_i": 3},
      {"week": 2, "madrs_total": 24, "cgi_i": 3},
      {"week": 4, "madrs_total": 18, "cgi_i": 2},
      {"week": 6, "madrs_total": 14, "cgi_i": 2},
      {"week": 8, "madrs_total": 10, "cgi_i": 1}
    ],
    "response": true,
    "remission": true,
    "week_8_madrs_change": -24,
    "week_8_madrs_percent_change": -70.6
  },
  "efficacy_summary": {
    "active_arm": {
      "n": 260,
      "madrs_mean_change_week8": -14.8,
      "madrs_response_rate_pct": 54.2,
      "madrs_remission_rate_pct": 38.5,
      "cgi_i_responder_pct": 52.0,
      "placebo_adjusted_difference": -3.2
    },
    "placebo_arm": {
      "n": 260,
      "madrs_mean_change_week8": -11.6,
      "madrs_response_rate_pct": 38.5,
      "madrs_remission_rate_pct": 25.4,
      "cgi_i_responder_pct": 35.8
    }
  }
}
```

### Example 2: Alzheimer's Trial (ADAS-Cog + MMSE + CDR-SB)

**Request:** "Generate an 18-month Alzheimer's disease trial with anti-amyloid monoclonal antibody"

**Output:**

```json
{
  "therapeutic_area": "cns",
  "indication": "Early Alzheimer's Disease (MCI due to AD or Mild AD Dementia)",
  "design": {
    "phase": "Phase III",
    "randomization": "1:1",
    "primary_endpoint": "Change from baseline in CDR-SB at Week 78 (18 months)",
    "key_secondary": [
      "ADAS-Cog13 change at Week 78",
      "ADCS-ADL-MCI change at Week 78",
      "Amyloid PET reduction (Centiloid) at Week 78"
    ],
    "n_planned": 1800,
    "treatment_duration_weeks": 78,
    "dosing": "IV infusion q4w",
    "titration": "4-week initial titration"
  },
  "inclusion_criteria": {
    "cdr_global": "0.5",
    "cdr_sb": "≥0.5",
    "mmse": "22-30",
    "adas_cog13": "≥10",
    "amyloid_pet": "Positive (visual read or Centiloid >24)",
    "age": "60-85",
    "apoe": "Genotyping required; safety analyses by APOE4 carrier status"
  },
  "sample_patient": {
    "USUBJID": "AD-001-001-0019",
    "demographics": {"age": 74, "sex": "Female", "race": "White", "apoe": "E3/E4", "education_years": 14},
    "baseline": {
      "cdr_sb": 3.5,
      "mmse": 24,
      "adas_cog13": 28.5,
      "adas_cog11": 22.0,
      "adcs_adl_mci": 46,
      "amyloid_pet_centiloid": 85.2,
      "tau_pet_temporal_meta_roi_suvr": 1.42
    },
    "assessments": [
      {"week": 12, "cdr_sb": 3.5, "mmse": 24, "adas_cog13": 28.0},
      {"week": 26, "cdr_sb": 3.8, "mmse": 23, "adas_cog13": 29.0},
      {"week": 52, "cdr_sb": 4.2, "mmse": 23, "adas_cog13": 30.5},
      {"week": 78, "cdr_sb": 4.5, "mmse": 21, "adas_cog13": 32.0}
    ],
    "cdr_sb_change_at_78w": 1.0,
    "adas_cog13_change_at_78w": 3.5,
    "aria_monitoring": {
      "aria_e": "None",
      "aria_h": "1 microhemorrhage (Week 26)",
      "aria_management": "Continued dosing per protocol; no dose interruption"
    }
  },
  "efficacy_summary": {
    "active_arm": {
      "n": 900,
      "cdr_sb_change_week78": 1.21,
      "adas_cog13_change_week78": 3.28,
      "amyloid_centiloid_change_week78": -55.0,
      "cdr_sb_difference_vs_placebo": -0.45
    },
    "placebo_arm": {
      "n": 900,
      "cdr_sb_change_week78": 1.66,
      "adas_cog13_change_week78": 5.18,
      "amyloid_centiloid_change_week78": 3.2
    }
  },
  "safety_summary": {
    "aria_e": {"active": 12.6, "placebo": 1.7},
    "aria_h": {"active": 17.3, "placebo": 6.5},
    "aria_symptomatic": {"active": 3.2, "placebo": 0.0},
    "discontinuation_due_to_ae": {"active": 7.3, "placebo": 2.8}
  }
}
```

---

## Validation Rules

| Rule | Requirement | Example |
|------|-------------|---------|
| MADRS Range | Total score must be 0-60 | MADRS = 58 valid; MADRS = 62 invalid |
| PANSS Range | Total score must be 30-210 | PANSS = 85 valid; PANSS = 28 invalid |
| PANSS Item Range | Each item 1-7 | Item = 1 valid; Item = 0 invalid |
| ADAS-Cog Direction | Higher = worse; annual decline in untreated mild AD 5-8 points | If ADAS-Cog drops >20 points on placebo over 78w → flag |
| MMSE Direction | Lower = worse; MMSE 0-30 | MMSE increasing >3 points from baseline without learning effect rationale → query |
| CGI-S/CGI-I Consistency | Improvement on disease scale should generally map to CGI-I improvement | MADRS -20 and CGI-I = 6 → data quality flag |
| EDSS Scoring | Must be 0.0-10.0 in 0.5 increments; EDSS ≥4.0 requires ambulation assessment | EDSS = 4.5 valid; EDSS = 4.3 invalid |
| Baseline Severity | Entry criteria must be met at both screen and baseline (unless protocol-specified exception) | MADRS screen = 27, MADRS baseline = 24 → screen fail or protocol deviation |
| Placebo Response Check | Large placebo improvement may indicate site quality issue | ≥35% of subjects at a site with MADRS improvement >50% on placebo → flag |
| Suicidality Monitoring | C-SSRS must be administered at every study visit; any Type 4-5 ideation or any suicidal behavior requires immediate safety alert | C-SSRS at screening, baseline, each visit, EOT, safety follow-up |

### Business Rules

- **Rater Training**: All raters must complete scale-specific training and pass certification before administering primary outcome measures; rater reliability should be documented and periodically re-assessed
- **Rater Consistency**: The same rater should assess a given subject across visits whenever possible; change of rater must be documented
- **Scale Administration Order**: MADRS should be administered before CGI to avoid carryover bias
- **Source Document Verification**: 100% source document verification for primary endpoint data; secondary endpoints by risk-based monitoring
- **Suicidality Alerts**: Any worsening in C-SSRS since last visit must trigger an automated alert to the principal investigator and medical monitor within 24 hours
- **ARIA Monitoring**: For anti-amyloid trials, scheduled MRIs at Weeks 6, 12, 26, 52, 78 with central radiology review; unscheduled MRI for any new neurological symptoms
- **APOE4 Stratification**: APOE4 genotype must be assessed for all subjects in anti-amyloid trials; safety findings stratified by carrier status; disclosure of genotype status per local IRB requirements

---

## Related Skills

### TrialSim Domains
- [adverse-events-ae.md](../domains/adverse-events-ae.md) - AE domain with CNS-specific MedDRA coding
- [demographics-dm.md](../domains/demographics-dm.md) - Demographics including education level for cognitive trials
- [concomitant-meds-cm.md](../domains/concomitant-meds-cm.md) - Psychotropic concomitant medications and washout
- [laboratory-lb.md](../domains/laboratory-lb.md) - Metabolic monitoring labs (glucose, lipids)

### TrialSim Core
- [../clinical-trials-domain.md](../clinical-trials-domain.md) - Core trial design concepts
- [../phase3-pivotal.md](../phase3-pivotal.md) - Phase III pivotal cohort designs

### Therapeutic Areas
- [cardiovascular.md](cardiovascular.md) - QTc monitoring overlap

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-06 | Initial CNS therapeutic area skill with MADRS, PANSS, ADAS-Cog, MMSE, CDR-SB, EDSS, CGI scales, and CNS-specific adverse event patterns |
