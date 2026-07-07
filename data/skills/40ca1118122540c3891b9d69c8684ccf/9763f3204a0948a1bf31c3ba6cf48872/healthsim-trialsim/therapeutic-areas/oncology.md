---
name: oncology
description: |
  Generate oncology clinical trial data with RECIST v1.1 response assessment,
  survival endpoints (PFS/OS), biomarker stratification, AE patterns by drug
  class (chemotherapy, checkpoint inhibitors, targeted therapy), and ECOG
  performance status. Triggers: "oncology", "cancer trial", "RECIST", "tumor
  response", "ORR", "PFS", "immunotherapy", "targeted therapy".
---

# Oncology Therapeutic Area

The oncology therapeutic area covers solid tumor and hematologic malignancy trials. Core assessment frameworks include RECIST v1.1 for response evaluation, survival endpoints for efficacy, and drug-class-specific safety profiles.

---

## For Claude

This is a **therapeutic area skill** for generating oncology trial data. Oncology is the **largest therapeutic area** in clinical development and requires specific endpoint structures.

**Always apply this skill when you see:**
- Cancer clinical trials (any phase, any indication)
- RECIST v1.1 or iRECIST assessment
- Tumor response endpoints (ORR, DCR, PFS, OS, DoR, TTP)
- Biomarker-driven trial designs (PD-L1, EGFR, ALK, BRCA, MSI-H)
- Chemotherapy, immunotherapy, or targeted therapy safety profiles
- ECOG performance status or disease staging requirements
- Imaging-based endpoints and blinded independent central review (BICR)

**Key responsibilities:**
- Generate RECIST v1.1 target/non-target lesion assessments with correct response derivation
- Produce realistic survival endpoint distributions per cancer type and line of therapy
- Apply biomarker prevalence rates and stratification logic
- Model drug-class-specific adverse event patterns (chemo vs IO vs targeted)
- Correlate ECOG PS with outcomes and dosing decisions

---

## RECIST v1.1 Response Assessment

### Response Categories

RECIST v1.1 defines four response categories for target lesions:

| Category | Code | Definition | Sum of Diameters Change |
|----------|------|------------|-------------------------|
| Complete Response | CR | Disappearance of all target lesions | -100% |
| Partial Response | PR | ≥30% decrease from baseline | ≤-30% |
| Progressive Disease | PD | ≥20% increase and ≥5mm absolute increase from nadir, or new lesion(s) | ≥+20% |
| Stable Disease | SD | Neither PR nor PD | -29.9% to +19.9% |

### Non-Target Lesion Assessment

| Category | Code | Definition |
|----------|------|------------|
| Complete Response | CR | Disappearance of all non-target lesions; normalization of tumor markers |
| Non-CR/Non-PD | SD | Persistence of one or more non-target lesions |
| Progressive Disease | PD | Unequivocal progression of existing non-target lesions |

### New Lesion Rule

The appearance of any new malignant lesion constitutes PD, regardless of target lesion response.

### Overall Response Derivation

| Target Response | Non-Target Response | New Lesions | Overall Response |
|----------------|---------------------|-------------|-----------------|
| CR | CR | No | CR |
| CR | Non-CR/Non-PD | No | PR |
| CR | Not Evaluated | No | PR |
| PR | Non-PD / Not Evaluated | No | PR |
| SD | Non-PD / Not Evaluated | No | SD |
| Not All Evaluated | Non-PD | No | NE |
| PD | Any | Any | PD |
| Any | PD | Any | PD |
| Any | Any | Yes | PD |

---

## Core Endpoints

### Primary Endpoints

| Endpoint | Acronym | Definition | Typical Use |
|----------|---------|------------|-------------|
| Overall Response Rate | ORR | Proportion achieving CR or PR | Phase II, Accelerated Approval |
| Progression-Free Survival | PFS | Time from randomization to PD or death | Phase III (advanced disease) |
| Overall Survival | OS | Time from randomization to death (any cause) | Phase III (registrational) |
| Pathologic Complete Response | pCR | No residual invasive cancer (resected tissue) | Neoadjuvant/adjuvant |

### Secondary Endpoints

| Endpoint | Acronym | Definition |
|----------|---------|------------|
| Disease Control Rate | DCR | Proportion achieving CR + PR + SD ≥12 weeks |
| Duration of Response | DoR | Time from first CR/PR to PD or death |
| Time to Progression | TTP | Time from randomization to PD (death censored) |
| Time to Response | TTR | Time from randomization to first CR/PR |
| Clinical Benefit Rate | CBR | CR + PR + SD ≥24 weeks |

### Endpoint Generability by Cancer Type

| Cancer Type | Expected ORR | Median PFS (months) | Median OS (months) |
|-------------|-------------|---------------------|---------------------|
| NSCLC (1L, IO monotherapy, PD-L1 ≥50%) | 40-45% | 10-12 | 26-30 |
| NSCLC (2L, chemo) | 8-12% | 3-4 | 7-9 |
| Melanoma (1L, IO combo) | 55-60% | 11-14 | NR |
| Breast (HER2+, 1L) | 65-70% | 14-18 | 48-60 |
| Colorectal (MSS, 3L) | 2-5% | 2-3 | 6-8 |
| Renal Cell (1L, IO combo) | 40-50% | 12-15 | NR |
| Urothelial (2L, IO) | 15-20% | 2-3 | 10-12 |

---

## Biomarker Stratification

### Standard Biomarker Panels

| Biomarker | Method | Prevalence | Clinical Utility |
|-----------|--------|------------|------------------|
| PD-L1 (TPS) | IHC 22C3/28-8/SP142 | Varies by assay/cutoff | IO response prediction (NSCLC, UC, HNSCC) |
| EGFR mutation | PCR/NGS (exons 18-21) | 10-15% (NSCLC, Western); 40-50% (Asian) | TKI eligibility (osimertinib, etc.) |
| ALK rearrangement | FISH/IHC/NGS | 3-7% (NSCLC) | ALK TKI eligibility |
| ROS1 | FISH/NGS | 1-2% (NSCLC) | ROS1 TKI eligibility |
| BRAF V600E | PCR/NGS | 40-50% (melanoma); 5-10% (CRC); 2-3% (NSCLC) | BRAF/MEK inhibitor eligibility |
| MSI-H/dMMR | IHC/PCR/NGS | 15% (CRC stage II-III); 3-5% (CRC metastatic); rare in others | IO eligibility (agnostic) |
| HER2 (ERBB2) | IHC ± FISH | 15-20% (breast); 15-25% (gastric) | Anti-HER2 therapy eligibility |
| BRCA1/2 | NGS | 5-10% (ovarian); 5% (breast); 8% (pancreatic) | PARP inhibitor eligibility |
| NTRK | NGS | <1% (pan-cancer); >90% (secretory breast, infantile fibrosarcoma) | NTRK inhibitor (agnostic) |

### Biomarker Prevalence Table

```json
{
  "biomarker_prevalence": {
    "nsclc": {
      "PD-L1_TPS_50plus": 0.28,
      "PD-L1_TPS_1to49": 0.30,
      "PD-L1_TPS_less1": 0.42,
      "EGFR_mutation": 0.12,
      "ALK_rearrangement": 0.04,
      "ROS1_rearrangement": 0.015,
      "BRAF_V600E": 0.02,
      "KRAS_G12C": 0.13,
      "MET_exon14_skip": 0.03,
      "RET_fusion": 0.015
    },
    "melanoma": {
      "BRAF_V600E": 0.42,
      "BRAF_V600K": 0.08,
      "NRAS_mutation": 0.20,
      "cKIT_mutation": 0.02
    },
    "breast": {
      "HR_positive_HER2_negative": 0.68,
      "HER2_positive": 0.17,
      "triple_negative": 0.15,
      "BRCA1_mutation": 0.03,
      "BRCA2_mutation": 0.03,
      "PIK3CA_mutation": 0.35,
      "gBRCA1_2_mutation": 0.05
    }
  }
}
```

---

## Drug Class AE Patterns

### Chemotherapy

| AE Term | MedDRA PT | All-Grade Incidence | Grade ≥3 Rate | Median Onset (days) |
|---------|-----------|--------------------|---------------|---------------------|
| Fatigue | Fatigue | 50-70% | 5-15% | 7-14 |
| Nausea | Nausea | 60-80% | 3-8% | 1-3 |
| Vomiting | Vomiting | 30-50% | 3-8% | 1-3 |
| Neutropenia | Neutropenia | 40-60% | 20-40% | 7-10 |
| Anemia | Anaemia | 30-50% | 5-15% | 14-28 |
| Thrombocytopenia | Thrombocytopenia | 20-40% | 5-10% | 10-14 |
| Alopecia | Alopecia | 50-70% | 0% | 14-21 |
| Peripheral Neuropathy | Neuropathy peripheral | 30-40% | 3-10% | 30-60 (cumulative) |
| Mucositis | Stomatitis | 20-40% | 3-7% | 5-10 |

### Checkpoint Inhibitors (Anti-PD-1/PD-L1, Anti-CTLA-4)

| irAE Category | MedDRA SOC | All-Grade | Grade 3-4 | Median Onset (weeks) |
|---------------|------------|-----------|-----------|----------------------|
| Fatigue | General disorders | 25-40% | 2-5% | 2-6 |
| Rash | Skin | 15-30% | 1-3% | 3-6 |
| Pruritus | Skin | 10-20% | <1% | 2-8 |
| Diarrhea/Colitis | Gastrointestinal | 10-20% | 2-5% | 6-8 |
| Hypothyroidism | Endocrine | 8-15% | <1% | 8-12 |
| Pneumonitis | Respiratory | 3-8% | 1-4% | 12-16 |
| Hepatitis | Hepatobiliary | 3-8% | 1-4% | 6-12 |
| Myocarditis | Cardiac | 1-2% | <1% | 4-8 |

**Key irAE characteristics:**
- Onset typically delayed compared to chemotherapy (weeks vs days)
- Combination anti-PD-1 + anti-CTLA-4 increases grade 3-4 irAE rate to 50-60%
- Endocrine irAEs are often permanent; most others resolve with immunosuppression
- Toxicity management uses CTCAE grading with an irAE-specific grading modification

### Targeted Therapy (Small Molecules + ADCs)

| Drug Class | Example Drugs | Characteristic AEs | Incidence |
|------------|---------------|--------------------|-----------|
| EGFR TKIs | Osimertinib, Gefitinib | Rash acneiform, Diarrhea, Paronychia | 40-80% |
| ALK TKIs | Alectinib, Lorlatinib | Constipation, Edema, Hypercholesterolemia | 20-40% |
| BRAF+MEK | Dabrafenib+Trametinib | Pyrexia, Rash, Photosensitivity | 30-50% |
| CDK4/6 inh | Palbociclib, Ribociclib | Neutropenia, Fatigue, Nausea | 60-80% |
| PARP inh | Olaparib, Niraparib | Fatigue, Nausea, Anemia | 40-60% |
| HER2 ADC | T-DXd, T-DM1 | Nausea, ILD/pneumonitis, LVEF decrease | 10-20% |
| VEGF | Bevacizumab | Hypertension, Proteinuria, Bleeding | 20-30% |

---

## ECOG Performance Status

### Scale Definition

| Grade | Code | Description |
|-------|------|-------------|
| 0 | Fully active | Fully active, able to carry on all pre-disease performance without restriction |
| 1 | Restricted in strenuous activity | Restricted in physically strenuous activity but ambulatory; light or sedentary work |
| 2 | Ambulatory, capable of self-care | Ambulatory and capable of all self-care but unable to work; up >50% of waking hours |
| 3 | Capable of limited self-care | Capable of only limited self-care; confined to bed or chair >50% of waking hours |
| 4 | Completely disabled | Completely disabled; cannot carry on any self-care; totally confined to bed or chair |
| 5 | Dead | Dead |

### ECOG Distribution by Trial Phase

| ECOG | Phase I | Phase II | Phase III (1L) | Phase III (2L+) |
|------|---------|----------|-----------------|-----------------|
| 0 | 30-40% | 40-50% | 50-60% | 35-45% |
| 1 | 50-60% | 45-55% | 35-45% | 45-55% |
| 2 | 5-15% | 5-10% | 0-5% | 5-10% |
| 3+ | Exclusion typical | Rare (if allowed) | Exclusion | Rare (if allowed) |

---

## Examples

### Example 1: NSCLC Immunotherapy Trial with RECIST and Biomarkers

**Request:** "Generate tumor response data for a Phase III NSCLC trial comparing pembrolizumab vs docetaxel in PD-L1 TPS≥1% patients"

**Output:**

```json
{
  "therapeutic_area": "oncology",
  "indication": "Non-Small Cell Lung Cancer",
  "design": {
    "phase": "Phase III",
    "randomization": "1:1",
    "stratification_factors": ["ECOG PS (0 vs 1)", "PD-L1 TPS (<50%, ≥50%)", "Histology (squamous vs non-squamous)"],
    "primary_endpoint": "PFS",
    "key_secondary": ["OS", "ORR", "DoR", "Safety"],
    "n_planned": 600,
    "assessment_interval": "q9w"
  },
  "biomarker_stratification": {
    "PD-L1_TPS_50plus": {"n": 180, "percentage": 30.0},
    "PD-L1_TPS_1to49": {"n": 420, "percentage": 70.0}
  },
  "sample_patient": {
    "USUBJID": "NSCLC-RECIST-001-00042",
    "biomarkers": {
      "PD-L1_TC_expression": "80%",
      "EGFR": "wild-type",
      "ALK": "not rearranged",
      "ROS1": "not rearranged",
      "Histology": "Non-squamous"
    },
    "ecog_ps": 1,
    "disease_stage": "Stage IV (M1a)",
    "baseline_target_lesions": [
      {"site": "Right upper lobe", "diameter_mm": 35.0, "type": "target"},
      {"site": "Mediastinal lymph node", "diameter_mm": 22.0, "type": "target"},
      {"site": "Liver segment VI", "diameter_mm": 18.0, "type": "target"}
    ],
    "baseline_sum_of_diameters_mm": 75.0,
    "baseline_non_target_lesions": [
      {"site": "Pleural effusion, right", "type": "non-target"}
    ],
    "assessments": [
      {
        "visit": "Week 9",
        "study_day": 63,
        "target_sum_diameters_mm": 48.0,
        "percent_change_from_baseline": -36.0,
        "target_response": "PR",
        "non_target_response": "Non-CR/Non-PD",
        "new_lesions": false,
        "overall_response": "PR"
      },
      {
        "visit": "Week 18",
        "study_day": 126,
        "target_sum_diameters_mm": 30.0,
        "percent_change_from_baseline": -60.0,
        "target_response": "PR",
        "non_target_response": "CR",
        "new_lesions": false,
        "overall_response": "PR"
      },
      {
        "visit": "Week 36",
        "study_day": 252,
        "target_sum_diameters_mm": 55.0,
        "percent_change_from_baseline": -26.7,
        "target_response": "PR",
        "non_target_response": "Non-CR/Non-PD",
        "new_lesions": false,
        "overall_response": "PR"
      }
    ],
    "best_overall_response": "PR",
    "confirmed_responder": true,
    "duration_of_response_days": 252,
    "progression_event": null,
    "death_event": null
  },
  "efficacy_summary": {
    "pembrolizumab_arm": {
      "n": 300,
      "ORR_percent": 44.0,
      "ORR_95CI": ["38.4", "49.8"],
      "DCR_percent": 71.0,
      "median_PFS_months": 12.4,
      "PFS_HR_vs_chemo": 0.65,
      "median_OS_months": 26.8,
      "OS_HR_vs_chemo": 0.72,
      "DoR_median_months": 18.6
    },
    "docetaxel_arm": {
      "n": 300,
      "ORR_percent": 26.0,
      "ORR_95CI": ["21.1", "31.3"],
      "DCR_percent": 58.0,
      "median_PFS_months": 6.8,
      "median_OS_months": 17.2,
      "DoR_median_months": 7.2
    }
  }
}
```

### Example 2: Safety Summary by Drug Class

**Request:** "Generate oncology safety data comparing checkpoint inhibitor vs chemotherapy AE profiles"

**Output:**

```json
{
  "therapeutic_area": "oncology",
  "comparison": "checkpoint-inhibitor vs chemotherapy",
  "safety_population": {
    "IO_arm": {"n": 298, "treated": 296, "TEAE_any_grade": 280, "TEAE_grade3plus": 52},
    "Chemo_arm": {"n": 295, "treated": 293, "TEAE_any_grade": 291, "TEAE_grade3plus": 118}
  },
  "most_common_teae_IO_arm": [
    {"ae_term": "Fatigue", "all_grade_pct": 35.8, "grade3plus_pct": 2.7},
    {"ae_term": "Rash", "all_grade_pct": 22.3, "grade3plus_pct": 1.7},
    {"ae_term": "Hypothyroidism", "all_grade_pct": 18.6, "grade3plus_pct": 0.7},
    {"ae_term": "Pruritus", "all_grade_pct": 17.2, "grade3plus_pct": 0.3},
    {"ae_term": "Diarrhea", "all_grade_pct": 15.9, "grade3plus_pct": 3.4},
    {"ae_term": "Arthralgia", "all_grade_pct": 12.5, "grade3plus_pct": 1.0},
    {"ae_term": "Pneumonitis", "all_grade_pct": 4.7, "grade3plus_pct": 2.0},
    {"ae_term": "Colitis", "all_grade_pct": 3.4, "grade3plus_pct": 1.4}
  ],
  "most_common_teae_chemo_arm": [
    {"ae_term": "Nausea", "all_grade_pct": 68.2, "grade3plus_pct": 5.1},
    {"ae_term": "Fatigue", "all_grade_pct": 55.6, "grade3plus_pct": 6.8},
    {"ae_term": "Neutropenia", "all_grade_pct": 48.5, "grade3plus_pct": 28.7},
    {"ae_term": "Anemia", "all_grade_pct": 35.2, "grade3plus_pct": 8.5},
    {"ae_term": "Alopecia", "all_grade_pct": 42.0, "grade3plus_pct": 0.0},
    {"ae_term": "Peripheral neuropathy", "all_grade_pct": 28.0, "grade3plus_pct": 4.4},
    {"ae_term": "Mucositis", "all_grade_pct": 25.6, "grade3plus_pct": 4.1}
  ],
  "irae_summary": [
    {"category": "Immune-related AEs (any grade)", "IO_arm_pct": 48.0, "chemo_arm_pct": 12.0},
    {"category": "irAEs requiring steroids", "IO_arm_pct": 22.0, "chemo_arm_pct": 3.0},
    {"category": "irAEs leading to discontinuation", "IO_arm_pct": 8.5, "chemo_arm_pct": 0.5},
    {"category": "All-cause discontinuation", "IO_arm_pct": 12.5, "chemo_arm_pct": 18.0}
  ]
}
```

---

## Validation Rules

| Rule | Requirement | Example |
|------|-------------|---------|
| RECIST Baseline | Sum of diameters must be recorded before first dose | BL SoD = 75.0 mm |
| RECIST CR Confirmatory | CR must be confirmed at least 4 weeks later | Scan at Wk 12 CR, Wk 16 CR = confirmed |
| RECIST PR Confirmatory | PR must be confirmed at least 4 weeks later | Scan at Wk 8 PR, Wk 12 PR = confirmed |
| ECOG at Screening | Must be 0-2 (or 0-1 per protocol) | ECOG 1 at screen |
| Biomarker Consistency | Biomarker status must be documented pre-randomization | PD-L1 result from local lab |
| PD-L1 Required | IO trials must specify assay and scoring method | "22C3 pharmDx, TPS ≥1%" |
| PFS Censoring Rules | Must define censoring rules for non-PD non-death | "Censored at last adequate tumor assessment" |
| OS Maturity | Data cutoff must declare deaths/surviving for OS | "68% maturity (204/300 events)" |
| New Lesion == PD | Any new lesion must be recorded as PD | "New hepatic lesion = PD" |
| Unequivocal Progression | Non-target PD requires global worsening | "Marked pleural effusion increase" |

### Business Rules

- **Target Lesion Selection**: Select up to 5 target lesions (max 2 per organ), measure longest diameter (short axis for lymph nodes; pathologically enlarged if ≥15mm short axis)
- **Baseline Window**: Baseline tumor assessment within 28 days prior to randomization
- **Assessment Schedule**: Typically q6w, q8w, or q9w during first year, then q12w
- **Confirmation of Response**: CR and PR must be confirmed by repeat assessment ≥4 weeks later in Phase III (not always required in Phase II)
- **Pseudo-progression Management**: For IO agents, initial PD with subsequent response (pseudo-progression) requires iRECIST confirmation; PD must be confirmed 4-8 weeks later
- **Stratification Integrity**: Subjects randomized within a stratum must be analyzed within that stratum
- **BICR vs Investigator**: Blinded independent central review should be primary for registrational trials when ORR or PFS is the primary endpoint

---

## Related Skills

### TrialSim Domains
- [adverse-events-ae.md](../domains/adverse-events-ae.md) - AE domain with oncology-specific MedDRA coding
- [demographics-dm.md](../domains/demographics-dm.md) - Subject demographics (age, sex, race for oncology)
- [exposure-ex.md](../domains/exposure-ex.md) - Dose modifications for AEs and response
- [laboratory-lb.md](../domains/laboratory-lb.md) - Hematology and chemistry for oncology monitoring

### TrialSim Core
- [../clinical-trials-domain.md](../clinical-trials-domain.md) - Core trial design concepts
- [../phase3-pivotal.md](../phase3-pivotal.md) - Phase III pivotal cohort designs

### Therapeutic Areas
- [cardiovascular.md](cardiovascular.md) - Cardio-oncology (LVEF monitoring)
- [cgt.md](cgt.md) - CAR-T and cell therapy in hematologic malignancies

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-06 | Initial oncology therapeutic area skill with RECIST v1.1, biomarker stratification, drug-class AE patterns, ECOG PS, and survival endpoints |
