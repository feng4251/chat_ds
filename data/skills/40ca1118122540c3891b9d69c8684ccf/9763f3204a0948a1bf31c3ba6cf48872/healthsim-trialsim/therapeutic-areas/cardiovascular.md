---
name: cardiovascular
description: |
  Generate cardiovascular clinical trial data with MACE composite endpoints,
  NYHA classification, LVEF measurement, blood pressure endpoints, heart failure
  hospitalization, 6-minute walk distance, NT-proBNP biomarkers, CVOT design
  patterns, and MACE adjudication. Triggers: "cardiovascular", "MACE", "heart
  failure", "CVOT", "NYHA", "LVEF", "NT-proBNP", "blood pressure", "lipid
  lowering", "anticoagulation".
---

# Cardiovascular Therapeutic Area

The cardiovascular therapeutic area covers heart failure, atherosclerotic cardiovascular disease (ASCVD), hypertension, atrial fibrillation, and related conditions. Core assessment frameworks include MACE composite endpoints, NYHA functional classification, cardiac biomarkers, and CV outcomes trial (CVOT) design.

---

## For Claude

This is a **therapeutic area skill** for generating cardiovascular trial data. Cardiovascular trials are characterized by **large sample sizes, long follow-up, and MACE composite endpoints**.

**Always apply this skill when you see:**
- Cardiovascular outcomes trials (CVOT) or cardiovascular safety studies
- MACE (Major Adverse Cardiovascular Events) composite endpoints
- Heart failure trials (HFrEF or HFpEF)
- NYHA functional classification (Class I-IV)
- LVEF (Left Ventricular Ejection Fraction) measurement
- Blood pressure endpoints (SBP/DBP change, BP control rates)
- NT-proBNP or BNP biomarker assessments
- 6-minute walk distance (6MWD) for HF patients
- Anticoagulation or antiplatelet trials (stroke, bleeding endpoints)
- Lipid-lowering trials (LDL-C, non-HDL-C endpoints)
- MACE Clinical Endpoint Committee (CEC) adjudication processes

**Key responsibilities:**
- Define MACE composite components (CV death, MI, stroke) correctly
- Generate realistic NYHA and LVEF distributions
- Apply appropriate NT-proBNP thresholds and change patterns
- Model time-to-event endpoints with competing risks
- Structure CEC adjudication workflows

---

## MACE Composite Endpoint

### Standard MACE Definitions

MACE (Major Adverse Cardiovascular Events) has several common definitions depending on the trial context:

| MACE Variant | Components | Typical Use |
|--------------|------------|-------------|
| 3-point MACE | CV death, Non-fatal MI, Non-fatal stroke | Standard CVOT |
| 4-point MACE | CV death, MI, Stroke, Hospitalization for unstable angina | ACS trials |
| 5-point MACE | CV death, MI, Stroke, Coronary revascularization, Unstable angina | PCI/stent trials |
| HF-MACE | CV death, HF hospitalization, Worsening HF requiring IV therapy | Heart failure trials |

### Component Definitions

| Component | Definition | Source Documentation |
|-----------|------------|---------------------|
| Cardiovascular Death | Death due to MI, sudden cardiac death, HF, stroke, CV procedures, CV hemorrhage, or other CV cause | CEC adjudication charter |
| Myocardial Infarction | Per Fourth Universal Definition: cTn rise/fall + ≥1 of: ischemic symptoms, ECG changes, imaging evidence, angiographic findings | Type 1 spontaneous; Type 2 supply/demand; Types 3-5 procedure-related |
| Stroke | Acute focal neurological deficit ≥24h (or to death) with imaging confirmation or clinical diagnosis; classified as ischemic, hemorrhagic, or undetermined | Must distinguish from TIA (<24h, no imaging lesion) |
| HF Hospitalization | Admission with ≥24h stay + documented new/worsening HF symptoms + objective evidence (exam, biomarkers, imaging) + HF-specific therapy (diuretics) | Requires CEC adjudication for CVOTs |

### MACE Event Rates for Sample Size Planning

| Population | Annualized 3P-MACE Rate (Placebo) | Expected Risk Reduction |
|------------|------------------------------------|------------------------|
| ASCVD established (secondary prevention) | 3-5% | 12-15% |
| ASCVD + diabetes | 4-6% | 10-15% |
| Recent ACS (≤1 year) | 6-10% | 15-20% |
| Chronic HF (HFrEF) | 8-12% (CV death or HF hosp) | 20-26% |
| Atrial fibrillation | 3-5% (stroke/SEE) | 60-70% (DOACs vs warfarin) |

---

## NYHA Functional Classification

### Class Definitions

| Class | Label | Description | 6MWD Typical Range |
|-------|-------|-------------|---------------------|
| I | No limitation | Ordinary physical activity does not cause undue fatigue, palpitation, or dyspnea | >450 m |
| II | Slight limitation | Comfortable at rest; ordinary physical activity results in fatigue, palpitation, dyspnea | 300-450 m |
| III | Marked limitation | Comfortable at rest; less than ordinary activity causes fatigue, palpitation, or dyspnea | 150-300 m |
| IV | Unable to carry on activity | Symptoms at rest; any physical activity increases discomfort | <150 m |

### NYHA Distribution by HF Population

| NYHA Class | Ambulatory HFrEF | HFpEF | Decompensated/Hospitalized |
|------------|-----------------|-------|---------------------------|
| I | 8-12% | 5-10% | 0% |
| II | 50-60% | 45-55% | 0% |
| III | 25-35% | 30-40% | 0% (prior to admission) |
| IV | 2-5% | 2-5% | 100% (at admission) |

### Assessment Timing

```json
{
  "nyha_assessment_schedule": {
    "screening": "Required; must be II-IV for most HF trials",
    "randomization_baseline": "Required; confirm stability if screening-to-randomization >14 days",
    "week_4": "Standard",
    "week_12": "Standard",
    "every_12_weeks_thereafter": "Standard",
    "end_of_treatment": "Required",
    "worsening_hf_event": "Required within 24h of event onset"
  }
}
```

---

## Cardiac Biomarkers

### NT-proBNP and BNP

| Parameter | NT-proBNP | BNP |
|-----------|-----------|-----|
| Clinical Cutoff (HF) | >125 pg/mL (sinus rhythm) / >365 pg/mL (AF) | >35 pg/mL |
| Prognostic Threshold | >1000 pg/mL (high risk) | >200 pg/mL |
| Half-life | 60-120 minutes | 20 minutes |
| Age cutoffs (HF diagnosis) | <50y: >450; 50-75y: >900; >75y: >1800 pg/mL | <100 pg/mL (all ages, rule-out) |

### NT-proBNP Expected Changes

| Scenario | Expected Change | Time Frame |
|----------|----------------|------------|
| Effective HF therapy | -30% to -60% from baseline | 4-12 weeks |
| Worsening HF | +50% to +200% from prior stable level | Acute (hours to days) |
| HF hospitalization discharge | -30% reduction from admission (goal) | At discharge |
| ARNI therapy initiation | Rapid decline, then plateau | 2-4 weeks |
| Prognostically meaningful reduction | ≥30% from baseline | 8-12 weeks |

### Other Key Cardiac Biomarkers

| Biomarker | Cutoff | Clinical Use |
|-----------|--------|--------------|
| High-sensitivity Troponin I/T | >99th percentile URL | MI diagnosis, risk stratification in stable CAD |
| hs-CRP | <2 mg/L (low), 2-10 (moderate) | Residual inflammatory risk |
| LDL-C | <70 or <55 mg/dL (high risk) | Lipid-lowering target |
| Lipoprotein(a) | >125 nmol/L or >50 mg/dL | ASCVD risk; emerging therapies |
| Galectin-3 | >25.9 ng/mL | HF fibrosis marker |

---

## LVEF and HF Classification

### HF Categories by LVEF

| Category | LVEF Range | Prevalence |
|----------|------------|------------|
| HFrEF (HF with Reduced EF) | ≤40% | 50-55% |
| HFmrEF (HF with Mildly Reduced EF) | 41-49% | 15-20% |
| HFpEF (HF with Preserved EF) | ≥50% | 30-35% |
| HFimpEF (HF with Improved EF) | Previously ≤40%, now >40% by ≥10 points | 10-20% of HFrEF |

### LVEF Measurement Modality

| Method | Variability | Recommended by Guidelines |
|--------|-------------|--------------------------|
| 2D Echocardiography (Simpson's Biplane) | ±5-8% inter-observer | First-line |
| 3D Echocardiography | ±3-5% | When available |
| Cardiac MRI | ±2-4% | Gold standard; used when echo is inadequate |
| MUGA (Radionuclide) | ±3-5% | Cardio-oncology LVEF monitoring |

---

## Blood Pressure Measurement

### BP Measurement Standards

| Parameter | Requirement |
|-----------|-------------|
| Rest time before measurement | ≥5 minutes seated |
| Cuff size | Appropriate for arm circumference |
| Number of readings | 3 readings, 1-2 minutes apart; average of last 2 |
| Timing | Same time of day, pre-dose if on anti-hypertensives |
| Device | Validated automated oscillometric device |
| Primary endpoint | Change from baseline to Month 3, 6, or 12 |

### BP Categories and Targets

| Category | SBP (mmHg) | DBP (mmHg) |
|----------|------------|-------------|
| Normal | <120 | <80 |
| Elevated | 120-129 | <80 |
| Stage 1 HTN | 130-139 | 80-89 |
| Stage 2 HTN | ≥140 | ≥90 |
| Hypertensive Crisis | >180 | >120 |
| Treatment Target (standard) | <140 | <90 |
| Treatment Target (intensive) | <130 or <120 | <80 |

---

## 6-Minute Walk Distance (6MWD)

### Reference Ranges

| Population | Mean 6MWD (m) | SD |
|------------|--------------|----|
| Healthy adults (20-50y) | 600-700 | 50-80 |
| Healthy adults (60-70y) | 500-620 | 50-90 |
| NYHA I | 450-550 | 80-100 |
| NYHA II | 350-450 | 80-100 |
| NYHA III | 200-350 | 80-100 |
| NYHA IV | <200 | 50-80 |

### Clinically Meaningful Change

| Threshold | Interpretation | Source |
|-----------|----------------|--------|
| ≥30 m | Minimal clinically important difference (MCID) | MGP meta-analysis |
| ≥45 m | Moderate meaningful change | HF-ACTION |
| ≥20% worsening | Significant deterioration | KCCQ correlation |

---

## CVOT Design Patterns

### Standard CVOT Architecture

```json
{
  "cvot_design": {
    "design_type": "Non-inferiority for safety → Superiority for efficacy",
    "type_of_trial": "Cardiovascular Outcomes Trial (CVOT)",
    "population": {
      "disease": "Type 2 Diabetes",
      "cv_risk": "Established ASCVD or high cardiovascular risk",
      "age": "≥40 years",
      "n_planned": 3180,
      "n_sites": 310,
      "median_follow_up_years": 3.5
    },
    "primary_endpoint": {
      "composite": "3-point MACE",
      "components": ["CV death", "Non-fatal MI", "Non-fatal stroke"],
      "analysis": "Time-to-first-event",
      "hypothesis": "Non-inferiority (upper 95% CI of HR <1.30) then superiority"
    },
    "key_secondary": {
      "components": [
        "Individual MACE components",
        "All-cause mortality",
        "HF hospitalization",
        "Renal composite (eGFR decline, ESKD, renal death)"
      ],
      "testing": "Hierarchical gatekeeping with alpha preservation"
    },
    "adjudication": {
      "committee": "Clinical Endpoint Committee (CEC)",
      "blinding": "Fully blinded to treatment assignment",
      "process": "Two independent adjudicators; disagreement resolved by full committee",
      "adjudication_package": "De-identified medical records, ECGs, lab reports, imaging reports, discharge summaries",
      "target_completeness": "≥95% of events adjudicated within 90 days"
    },
    "dsmb": {
      "interim_analyses": "After ~25%, ~50%, and ~75% of planned events",
      "stopping_rules": "Group sequential design with O'Brien-Fleming spending function"
    },
    "regulatory_context": "FDA 2008 CV Safety Guidance for T2D drugs; FDA 2018 guidance update"
  }
}
```

---

## Examples

### Example 1: Heart Failure Trial (HFrEF)

**Request:** "Generate data for a Phase III HFrEF trial with NT-proBNP, LVEF, NYHA, and 6MWD endpoints"

**Output:**

```json
{
  "therapeutic_area": "cardiovascular",
  "indication": "Heart Failure with Reduced Ejection Fraction (HFrEF)",
  "design": {
    "phase": "Phase III",
    "randomization": "1:1",
    "stratification_factors": ["NYHA class (II vs III)", "NT-proBNP (≤ vs > median)", "eGFR (< vs ≥60)"],
    "primary_endpoint": "Composite of CV death or first HF hospitalization",
    "tested_sequentially": ["All-cause mortality", "KCCQ-OSS change at Month 12", "6MWD change at Month 12"],
    "n_planned": 4200,
    "median_follow_up_months": 24
  },
  "inclusion_criteria": {
    "lvfe": "≤40% by echocardiography",
    "nyha_class": "II-IV",
    "nt_probnp": "≥1000 pg/mL (sinus rhythm) or ≥1500 pg/mL (AF)",
    "stable_medication": "ACEi/ARB/ARNI + beta-blocker ± MRA; stable dose ≥4 weeks",
    "egfr": "≥30 mL/min/1.73m²",
    "age": "≥18 years",
    "6mwd": "≥100 m"
  },
  "sample_patient": {
    "USUBJID": "HFrEF-001-000128",
    "demographics": {
      "age": 68,
      "sex": "Male",
      "race": "White",
      "weight_kg": 84.5,
      "bmi": 28.9
    },
    "baseline_assessments": {
      "lvfe_percent": 28,
      "nyha_class": "II",
      "nt_probnp_pg_per_ml": 1850,
      "egfr_ml_per_min": 58,
      "sbp_mmHg": 122,
      "dbp_mmHg": 76,
      "heart_rate_bpm": 72,
      "6mwd_meters": 365,
      "kccq_oss": 62.4
    },
    "concomitant_medications": {
      "sacubitril_valsartan_97_103_mg_bid": true,
      "bisoprolol_10_mg_qd": true,
      "empagliflozin_10_mg_qd": true,
      "furosemide_40_mg_qd": true
    },
    "longitudinal_assessments": [
      {"visit": "Month 4", "nt_probnp": 1280, "6mwd": 385, "kccq_oss": 71.2, "nyha": "II"},
      {"visit": "Month 8", "nt_probnp": 940, "6mwd": 402, "kccq_oss": 76.8, "nyha": "I"},
      {"visit": "Month 12", "nt_probnp": 780, "6mwd": 415, "kccq_oss": 79.5, "nyha": "I"},
      {"visit": "Month 18", "nt_probnp": 720, "6mwd": 408, "kccq_oss": 77.3, "nyha": "I"},
      {"visit": "Month 24", "nt_probnp": 690, "6mwd": 412, "kccq_oss": 78.9, "nyha": "I"}
    ],
    "events": {
      "primary_endpoint_event": false,
      "hf_hospitalization": 0,
      "cv_death": false,
      "all_cause_death": false
    }
  },
  "efficacy_summary": {
    "active_arm": {
      "n": 2100,
      "primary_composite_hr": 0.74,
      "primary_composite_95ci": ["0.65", "0.84"],
      "p_value": 0.0001,
      "all_cause_mortality_hr": 0.83,
      "kccq_oss_change_at_month12": 8.4,
      "6mwd_change_meters_at_month12": 32
    },
    "placebo_arm": {
      "n": 2100,
      "kccq_oss_change_at_month12": 3.2,
      "6mwd_change_meters_at_month12": 5
    }
  }
}
```

### Example 2: MACE Adjudication Record

**Request:** "Generate a MACE adjudication record for a CVOT DMC submission"

**Output:**

```json
{
  "adjudication_record": {
    "event_id": "MACE-2024-00842",
    "study_id": "CVOT-GLP1-001",
    "site_id": "S042",
    "subject_id": "CVOT-GLP1-001-042-0188",
    "treatment_blinded": true,
    "event_type_reported": "Myocardial Infarction",
    "event_date": "2024-11-18",
    "event_source": {
      "primary_source": "Hospital discharge summary",
      "supporting_documents": ["ECG (ischemic ST elevation)", "Serial troponin (hs-cTnI: 56→1240→5800 ng/L)", "Cath report (LAD 95%, stented)", "Admission note", "Discharge medications list"]
    },
    "adjudication": {
      "adjudicator_1": {
        "decision": "MI - Type 1 (spontaneous)",
        "confidence": "High",
        "rationale": "Spontaneous plaque rupture; troponin rise/fall with ≥1 value >99th percentile URL; ischemic ST elevation on ECG; angiographic LAD occlusion; no procedural or supply-demand context"
      },
      "adjudicator_2": {
        "decision": "MI - Type 1 (spontaneous)",
        "confidence": "High",
        "rationale": "Agree with Type 1 classification. Clear spontaneous presentation with angiographic confirmation."
      },
      "adjudicator_agreement": true,
      "final_adjudication": "MI - Type 1 (spontaneous)",
      "adjudication_date": "2025-01-14",
      "adjudication_time_days_from_event": 57
    },
    "classification_details": {
      "mi_type": "Type 1 (spontaneous)",
      "mi_location": "Anterior (LAD territory)",
      "stemi_vs_nstemi": "STEMI",
      "peak_troponin_ng_per_L": 5.8,
      "peak_troponin_x_url": 145,
      "revascularization": "PCI with DES to proximal LAD",
      "klimip_status": "Killip Class I"
    }
  }
}
```

---

## Validation Rules

| Rule | Requirement | Example |
|------|-------------|---------|
| LVEF Measurement | Report modality and date of LVEF assessment | "2D Echo Simpson's Biplane; 2024-03-15" |
| NYHA Consistency | NYHA IV cannot have 6MWD >200 m (screening anomaly trigger) | NYHA IV + 6MWD = 250 m → flag |
| NT-proBNP Timing | Must be measured at screening (±7 days), baseline, and per schedule | Screen NT-proBNP = 1850 (Day -10) |
| MACE Adjudication | All potential MACE events must be submitted to CEC within 30 days | Reported 2024-11-20, adjudicated 2025-01-14 |
| CEC Blinding | Adjudicators must not have access to treatment assignment | "Blinded adjudication confirmed" |
| BP Measurement Protocol | 3 readings; use average of last 2 | Reading: 134/82, 130/80, 128/80 → 129/80 |
| 6MWD Standardization | Standardized instructions, same corridor, same time of day | "10:00 AM, hospital corridor B, no encouragement" |
| CV Death Definition | Must specify whether sudden, HF, MI, stroke, procedural, or other | "Sudden cardiac death (unwitnessed, within 1h of symptoms)" |
| Event Prioritization | If multiple events on same day, adjudicate highest severity first | MI + HF hospitalization same day → adjudicate MI first |
| Competing Risks | Death precludes subsequent non-fatal event counting for primary analysis | Death → no further MACE components counted |

### Business Rules

- **MACE is time-to-first-event**: Only the first occurrence of any MACE component counts in the primary analysis
- **CV Death Hierarchy**: If a subject dies of CV cause, subsequent non-fatal events are not counted for MACE
- **HF Hospitalization Definition**: Must include ≥24h admission + documented HF worsening + specific HF therapy (≥1 dose IV diuretic)
- **MI Definition**: Must use Fourth Universal Definition of MI; distinction between Type 1 (spontaneous plaque rupture) and Type 2 (supply-demand mismatch) is required
- **NYHA Assessment**: Must be performed by a qualified clinician unblinded to NYHA but blinded to treatment assignment where possible; inter-rater reliability should be documented
- **CEC Charter**: All MACE endpoints must be defined in a CEC charter before first subject enrolled; no post-hoc changes to definitions
- **BP Variability**: Visit-to-visit variability of SBP (SD ~8-10 mmHg) must be accounted for in sample size; 24h ambulatory BP monitoring preferred in some designs

---

## Related Skills

### TrialSim Domains
- [adverse-events-ae.md](../domains/adverse-events-ae.md) - AE domain with CV-specific MedDRA coding
- [demographics-dm.md](../domains/demographics-dm.md) - Demographics with CV risk factors
- [concomitant-meds-cm.md](../domains/concomitant-meds-cm.md) - CV background therapy (ACEi, statins, antiplatelets)
- [vital-signs-vs.md](../domains/vital-signs-vs.md) - BP, HR, weight for CV trials
- [laboratory-lb.md](../domains/laboratory-lb.md) - Lipid panels, cardiac biomarkers

### TrialSim Core
- [../clinical-trials-domain.md](../clinical-trials-domain.md) - Core trial design concepts
- [../phase3-pivotal.md](../phase3-pivotal.md) - Phase III pivotal cohort designs

### Therapeutic Areas
- [oncology.md](oncology.md) - Cardio-oncology LVEF monitoring

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-06 | Initial CV therapeutic area skill with MACE composite, NYHA classification, LVEF, BP measurement, HF hospitalization, 6MWD, NT-proBNP, CVOT design, and MACE adjudication |
