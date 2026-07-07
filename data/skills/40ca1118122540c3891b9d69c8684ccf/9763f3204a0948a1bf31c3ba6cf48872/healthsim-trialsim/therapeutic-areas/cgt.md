---
name: cgt
description: |
  Generate Cell & Gene Therapy (CGT) clinical trial data including CAR-T cell
  therapy (lymphodepletion, apheresis, manufacturing, cryopreservation, infusion,
  CRS grading per Lee 2019 / ASTCT, ICANS, neurotoxicity), AAV-based gene therapy
  (vector copy number, transgene expression, neutralizing antibodies, liver
  toxicity), CRISPR gene editing, 15-year long-term follow-up per FDA guidance,
  RCL monitoring, integration site analysis, and CMC characterization. Triggers:
  "CAR-T", "gene therapy", "cell therapy", "CRISPR", "AAV", "CRS", "ICANS",
  "lymphodepletion", "vector", "RCL", "long-term follow-up", "ATMP", "CGT".
---

# Cell & Gene Therapy (CGT) Therapeutic Area

The Cell & Gene Therapy therapeutic area covers autologous and allogeneic CAR-T cell therapies, AAV-based gene therapies, CRISPR gene editing, and related advanced therapeutic medicinal products (ATMPs). Core assessment frameworks include CRS/ICANS grading, vector characterization, manufacturing process control, and mandated long-term safety follow-up.

---

## For Claude

This is a **therapeutic area skill** for generating Cell & Gene Therapy trial data. CGT is characterized by **unique manufacturing processes, acute toxicity syndromes, and regulatory-mandated 15-year long-term follow-up**.

**Always apply this skill when you see:**
- CAR-T cell therapy trials (CD19, BCMA, CD22, CD7, dual-targeting, armored CARs)
- Gene therapy trials using AAV, lentiviral, or non-viral vectors
- CRISPR/Cas9 gene editing or base editing therapeutic trials
- Cytokine Release Syndrome (CRS) monitoring and grading
- ICANS (Immune Effector Cell-Associated Neurotoxicity Syndrome) assessment
- Lymphodepletion regimens (fludarabine/cyclophosphamide)
- Apheresis, manufacturing, and cryopreservation process (vein-to-vein time)
- Vector copy number (VCN) or transgene expression monitoring
- AAV neutralizing antibody screening and capsid immunity
- FDA 15-year long-term follow-up requirements for gene therapy
- RCL (Replication Competent Lentivirus) monitoring for lentiviral products
- Integration site analysis (ISA) for integrating vectors
- Liver toxicity, complement activation, or TMA (thrombotic microangiopathy) in AAV therapies

**Key responsibilities:**
- Generate CRS grading (Lee 2019 / ASTCT consensus) with appropriate timing and management
- Model lymphodepletion pharmacokinetics and CAR-T expansion kinetics
- Apply AAV vector copy number quantitation and transgene expression levels
- Structure 15-year LTFU visit schedules and data collection per FDA guidance
- Model CMC release specifications (viability, transduction efficiency, potency)

---

## CAR-T Cell Therapy

### Treatment Journey

```
Screening/Consent
    →
Leukapheresis / Apheresis
    →
Cryopreservation & Shipment to Manufacturing
    →
CAR-T Manufacturing (7–14 days)
    →
Product Release Testing (Sterility, Viability, Potency, Identity)
    →
Cryopreserved Final Product Shipment to Site
    →
Lymphodepleting Chemotherapy (Day -5 to Day -3)
    →
CAR-T Cell Infusion (Day 0)
    →
Acute Toxicity Monitoring (CRS/ICANS, Day 0 to Day 28)
    →
Engraftment & Early Response Assessment (Day 28–Day 90)
    →
Long-Term Follow-Up (through Day 365 and then annually to Year 15)
```

### Lymphodepletion Regimen

| Regimen | Drug & Dose | Schedule |
|---------|------------|----------|
| Standard LD (CD19 CAR-T) | Fludarabine 30 mg/m²/day + Cyclophosphamide 500 mg/m²/day | Day -5 to Day -3 (3 days) |
| Reduced Intensity | Fludarabine 25 mg/m²/day + Cyclophosphamide 250 mg/m²/day | Day -5 to Day -3 |
| Bendamustine-based (alternative) | Bendamustine 90 mg/m²/day | Day -4 to Day -3 (2 days) |

### Product Release Specifications (Example)

| Attribute | Specification | Method |
|-----------|---------------|--------|
| Viability | ≥70% viable cells | Trypan blue / automated cell counter |
| CAR Expression (Transduction Efficiency) | ≥15% (target ≥25%) | Flow cytometry |
| Total Viable Cell Dose | Per weight-based dosing table (flat or weight-based) | Cell count x viability |
| Potency | Interferon-gamma or cytotoxicity assay | ELISA / co-culture killing assay |
| Sterility | Negative (no growth at 14 days) | USP <71>; Gram stain pre-release |
| Endotoxin | ≤0.5 EU/mL | LAL assay |
| Mycoplasma | Negative | PCR or culture |
| Vector Copy Number (VCN) | Per specification range (typically <4-5 copies/cell) | qPCR or digital PCR |
| RCL | Negative (lentiviral products) | P24 ELISA or PCR-based assay |
| Identity | CD3+ and CAR+ per specification | Flow cytometry |

---

## CRS Grading and Management

### ASTCT / Lee 2019 Consensus Grading

| Grade | Fever | Hypotension | Hypoxia | Management |
|-------|-------|-------------|---------|------------|
| Grade 1 | ≥38°C | None | None | Supportive care, antipyretics, IV fluids; rule out infection |
| Grade 2 | ≥38°C | Not requiring vasopressors | Low-flow nasal cannula (≤6L/min) | Tocilizumab 8 mg/kg IV (up to 800 mg); repeat q8h if no response (max 4 doses); IV fluids |
| Grade 3 | ≥38°C | Requiring 1 vasopressor ± vasopressin | High-flow O2 (>6L/min), CPAP, or BiPAP | Tocilizumab + Dexamethasone 10 mg IV q6h; vasopressor support; ICU admission |
| Grade 4 | ≥38°C | Requiring multiple vasopressors (excluding vasopressin) | Positive pressure ventilation (intubated) | Tocilizumab + Methylprednisolone 1g IV q24h; multi-pressor support; mechanical ventilation |

### Key CRS Rules

- Fever must be present for CRS diagnosis (except in rare delayed cases)
- CRS timing: Median onset 2-7 days post-infusion (range 1-14 days for most products)
- Duration: Typically 5-10 days; longer for Grade 3-4
- **Tocilizumab** should not be given prophylactically (risk of masking fever and worsening ICANS)
- CRS-related organ toxicity includes: hepatic (transaminitis), renal (AKI), cardiac (troponin leak, reduced EF), coagulopathy

---

## ICANS (Immune Effector Cell-Associated Neurotoxicity Syndrome)

### ASTCT ICANS Grading (ICE = Immune Effector Cell Encephalopathy Score, max 10)

| Grade | ICE Score | Level of Consciousness | Seizure | Motor Findings | Cerebral Edema |
|-------|-----------|----------------------|---------|----------------|----------------|
| Grade 1 | 7-9 | Awakens spontaneously | None | None | None |
| Grade 2 | 3-6 | Awakens to voice | None | None | None |
| Grade 3 | 0-2 | Awakens only to tactile stimulus | Any clinical or EEG seizure resolving spontaneously or with rescue rx | None | Focal/local on imaging |
| Grade 4 | 0 (unarousable) | Stuporous or comatose | Life-threatening prolonged seizure or status epilepticus | Deep focal motor weakness (hemiparesis, paraparesis) | Diffuse cerebral edema on imaging; decerebrate/decorticate posturing |

### ICE Assessment (10 points)

| Domain | Task | Scoring |
|--------|------|---------|
| Orientation | Orientation to year, month, city, hospital (4 points) | 1 point each |
| Naming | Name 3 objects (3 points) | 1 point each (e.g., clock, pen, button) |
| Following Commands | Follow 1-step command (1 point) | 1 point (e.g., "Show me 2 fingers" or "Close your eyes") |
| Writing | Write a standard sentence (1 point) | 1 point (e.g., "Our national bird is ____") |
| Attention | Count backwards from 100 by 10 (1 point) | 1 point |

### Key ICANS Rules

- ICANS onset typically after CRS (median Day 5-8; range Day 2-21)
- CRS is the primary risk factor for ICANS; treating CRS early does not eliminate ICANS risk
- **Corticosteroids** are first-line for ICANS (dexamethasone or methylprednisolone)
- Prophylactic anti-epileptics (levetiracetam) often used in Grade ≥2
- Lumbar puncture and brain MRI recommended for Grade ≥3
- Papilledema screening (fundoscopy) before LP

---

## AAV Gene Therapy

### Vector Components

| Component | Function | Analytical Method |
|-----------|----------|-------------------|
| Capsid (AAV serotype) | Tissue tropism, immunogenicity, biodistribution | AAV serotype-specific ELISA, NGS capsid mapping |
| Promoter/Enhancer | Tissue-specific or ubiquitous transgene expression | Vector map / sequencing |
| Transgene | Therapeutic protein of interest | Sequencing, expression assay |
| PolyA signal | mRNA stability and nuclear export | Sequencing |
| ITRs (Inverted Terminal Repeats) | Vector genome replication and packaging | Restriction digest / sequencing |

### Key AAV Parameters

| Parameter | Description | Typical Specification |
|-----------|-------------|----------------------|
| Vector Genome Titer (vg/mL) | Quantitation of viral genomes | ≤1.0E14 vg/mL (concentrated) |
| Total Dose | vg/kg (weight-based) or total vg (fixed) | 1E12 to 3E14 vg/kg |
| Empty/Full Ratio | Proportion of capsids containing genome | Full ≥70% (by AUC/A260) |
| Potency | Transgene expression or functional activity in target cell | Cell-based assay |
| NAb Screening Titer | Anti-AAV neutralizing antibody titer | Exclusion if >1:5 or >1:50 (varies by serotype) |
| Biodistribution | Vector genome detection in tissues/fluids | qPCR of vector DNA |
| Transgene Expression | Protein/RNA level in target tissue | ELISA, IHC, or RNA-seq |

### VCN (Vector Copy Number) Measurement

| Tissue/Sample | Time Points | Expected Range |
|---------------|-------------|----------------|
| Blood (peripheral) | Days 1, 3, 7, 14, 28, then q3m | 0.1-100 copies/μg gDNA (peak ~Day 7-14) |
| Liver biopsy | Baseline, Day 28, Week 52 | 0.5-10 copies/diploid genome |
| Target tissue (biopsy) | Per protocol | Method-dependent |
| Semen | q3m until negative x3 | Below LLOQ (cleared to negative) |

---

## 15-Year Long-Term Follow-Up (LTFU)

### FDA Guidance (2020): Long Term Follow-Up After Administration of Human Gene Therapy Products

| Follow-Up Period | Visit Frequency | Key Assessments |
|-----------------|-----------------|-----------------|
| Year 1 | q3m (or per primary protocol) | VCN, transgene expression, AEs, immune response |
| Year 2-5 | q6m | VCN, immunogenicity (ADA), RCL, malignancy screening |
| Year 6-10 | Annually | Malignancy screening, survival status, late AEs, RCL (if integrating) |
| Year 11-15 | Annually | Malignancy screening, survival status, new diagnoses |

### LTFU Assessment Domains

| Domain | Assessments | Rationale |
|--------|-------------|-----------|
| Vector Persistence | VCN in blood/semen/urine | Assess vector clearance; germline transmission risk |
| Immunogenicity | Anti-capsid and anti-transgene antibodies | Waning immunity; re-dosing feasibility |
| RCL Monitoring | RCL PCR and/or culture-based assay | For lentiviral and gamma-retroviral vectors; infectious risk |
| Integration Site Analysis (ISA) | LAM-PCR, shear extension PCR, or targeted seq | Clonal dominance; insertional mutagenesis risk |
| Malignancy | Hematologic and solid tumor screening | Insertional oncogenesis; delayed malignancy |
| Survival | All-cause mortality | Long-term safety and efficacy durability |
| Autoimmunity | Autoantibody panel; clinical assessment | Immune dysregulation from gene-modified cells |
| Neurological | Long-term neurocognitive assessment | Delayed neurotoxicity from CNS-targeted therapies |
| Reproductive | Pregnancy outcomes; partner pregnancy tracking | Germline integration risk |

### LTFU Visit Data Collection

```json
{
  "ltfu_visit_schema": {
    "visit_id": "LTFU-Y05-001",
    "study_year": 5,
    "visit_date": "2029-04-15",
    "survival_status": "Alive",
    "assessments": {
      "vcn_blood_copies_per_ug_gDNA": 0.05,
      "vcn_semen_copies_per_ug_gDNA": null,
      "rcl_pcr": "Negative",
      "rcl_culture": "Pending",
      "anti_aav8_total_antibodies": "Positive (titer 1:400)",
      "anti_aav8_neutralizing_antibodies": "Positive (titer 1:50)",
      "anti_transgene_antibodies": "Negative",
      "hematology_panel": "Within normal limits",
      "chemistry_panel": "AST 42 (<40 ULN marginally elevated)",
      "malignancy_screening": {
        "physical_exam": "No palpable lymphadenopathy or masses",
        "imaging": "Chest X-ray - No evidence of malignancy",
        "hematologic": "CBC with differential - No blast cells; normal counts"
      },
      "autoimmune_assessment": "ANA negative; RF negative",
      "serious_adverse_events_since_last_visit": []
    }
  }
}
```

---

## CRISPR Gene Editing

### CRISPR-Specific Assessments

| Domain | Assessment | Method | Time Points |
|--------|------------|--------|-------------|
| On-Target Editing | % indels at target locus | NGS (targeted amplicon seq) | Engraftment, q3m Year 1 |
| Off-Target Editing | Indels at predicted/empiric off-target sites | GUIDE-seq, CIRCLE-seq, ONE-seq, or targeted NGS | Screen/qualification, engraftment, then annually |
| Translocation | Chromosomal rearrangements | UDiTaS, FISH, or long-read seq | Screen, engraftment |
| Karyotype | Gross chromosomal abnormalities | G-banding or spectral karyotyping | Screen, engraftment, then per LTFU |
| Guide RNA Persistence | gRNA levels | RT-qPCR | Per PK schedule |
| Cas9 Immunogenicity | Anti-Cas9 antibodies | ELISA; T-cell ELISpot | Baseline, q3m Year 1, annually thereafter |

---

## Examples

### Example 1: CAR-T Trial with CRS/ICANS

**Request:** "Generate CAR-T trial data for CD19-targeted CAR-T with CRS and ICANS monitoring"

**Output:**

```json
{
  "therapeutic_area": "cgt",
  "modality": "CAR-T Cell Therapy",
  "indication": "Relapsed/Refractory Diffuse Large B-Cell Lymphoma (≥2 prior lines)",
  "design": {
    "phase": "Phase II (registrational)",
    "n_planned": 150,
    "treatment": "Single infusion of autologous CD19-directed CAR-T cells",
    "dose_levels": [
      {"level": 1, "dose_range": "1.0-2.0 × 10^6 CAR+ T cells/kg"}
    ],
    "primary_endpoint": "Complete Response (CR) rate per Lugano 2014",
    "key_secondary": ["ORR", "DoR", "OS", "PFS", "Manufacturing success rate"]
  },
  "crs_icns_findings": {
    "summary": {
      "n_infused": 148,
      "crs_any_grade_pct": 85.1,
      "crs_grade_1": 40.5,
      "crs_grade_2": 31.1,
      "crs_grade_3": 10.8,
      "crs_grade_4": 2.7,
      "crs_median_onset_days": 3,
      "crs_median_duration_days": 7,
      "tocilizumab_use_pct": 44.6,
      "corticosteroid_use_pct": 18.2,
      "icans_any_grade_pct": 35.8,
      "icans_grade_3plus_pct": 8.8,
      "icans_median_onset_days": 6,
      "cr_related_mortality": 0
    }
  },
  "sample_patient": {
    "USUBJID": "CAR-T-001-00028",
    "demographics": {"age": 62, "sex": "Male", "weight_kg": 78.5},
    "pre_treatment": {
      "prior_lines_therapy": 3,
      "bridging_therapy": "R-GemOx × 2 cycles (SD)",
      "ldh_baseline": "2.1 × ULN",
      "ecog_ps": 1,
      "tumor_burden": "SPD 4500 mm² (high)"
    },
    "lymphodepletion": {
      "regimen": "Fludarabine 30 mg/m² + Cyclophosphamide 500 mg/m² Days -5 to -3",
      "completed_per_protocol": true,
      "absolute_lymphocyte_count_day0": "0.1 × 10^9/L"
    },
    "product": {
      "dose_administered": "1.6 × 10^6 CAR+ T cells/kg",
      "viability_percent": 92.4,
      "transduction_efficiency_percent": 28.5,
      "vcn_copies_per_cell": 2.8,
      "ifn_gamma_potency": "18,500 pg/mL (meets specification)",
      "sterility": "Negative (pre-release Gram stain); negative at 14 days"
    },
    "crs_course": {
      "onset_day": 2,
      "peak_grade": 2,
      "resolution_day": 8,
      "max_temp_c": 39.6,
      "min_sbp": 98,
      "management": "Tocilizumab × 2 doses (Day 3, Day 4); IV fluids; no vasopressor",
      "icu_admission": false
    },
    "icans_course": {
      "onset_day": 6,
      "peak_grade": 1,
      "resolution_day": 9,
      "lowest_ice_score": 8,
      "management": "Dexamethasone 10 mg IV q12h × 4 doses",
      "eeg": "Not performed (Grade 1)",
      "mri_brain": "Not performed (Grade 1)"
    },
    "response": {
      "day_28_pet_ct": "Deauville 2",
      "day_28_response": "CR",
      "day_90_pet_ct": "Deauville 2",
      "day_90_response": "CR",
      "day_180_response": "CR",
      "day_365_response": "CR",
      "duration_of_response_months": "Ongoing at 18 months"
    }
  },
  "product_summary": {
    "manufacturing_success_rate_pct": 94.0,
    "median_vein_to_vein_days": 24,
    "out_of_specification_rate_pct": 6.0,
    "oos_reasons": {
      "low_viability": 2.7,
      "low_transduction_efficiency": 1.3,
      "sterility_positive": 1.3,
      "insufficient_cell_dose": 0.7
    }
  }
}
```

### Example 2: AAV Gene Therapy Long-Term Follow-Up

**Request:** "Generate AAV9 gene therapy LTFU data through Year 5"

**Output:**

```json
{
  "therapeutic_area": "cgt",
  "modality": "AAV Gene Therapy",
  "indication": "Spinal Muscular Atrophy (SMA) Type 1",
  "product": "AAV9-hSMN1 (Onasemnogene Abeparvovec)",
  "design": {
    "phase": "Phase III / Long-Term Follow-Up",
    "n_enrolled": 22,
    "n_in_ltfu": 21,
    "median_follow_up_years": 4.2,
    "dose": "1.1 × 10^14 vg/kg (single IV infusion)"
  },
  "ltfu_summary": {
    "deaths": {
      "n": 1,
      "cause": "Progressive respiratory failure unrelated to gene therapy (natural history)"
    },
    "sae_summary": {
      "hepatotoxicity": {
        "n": 5,
        "description": "Transient AST/ALT elevation; managed with prednisolone per protocol; all resolved"
      },
      "tma": {
        "n": 1,
        "description": "Mild thrombotic microangiopathy at Week 1; resolved with supportive care"
      },
      "thrombocytopenia": {
        "n": 8,
        "description": "Grade 1-2; nadir at Day 7-10; resolved by Day 28 without intervention"
      }
    },
    "vcn_blood": {
      "year_1_mean_copies_per_ug_gDNA": 12.5,
      "year_3_mean_copies_per_ug_gDNA": 4.2,
      "year_5_mean_copies_per_ug_gDNA": 1.8,
      "undetectable_by_year_5": 2
    },
    "vcn_semen": {
      "any_time_detectable": 0,
      "cleared_by_month_6": "All negative x3 by Month 6 in male subjects"
    },
    "immunogenicity": {
      "anti_aav9_nab_baseline_positive_pct": 0,
      "anti_aav9_nab_at_year_1": "100% (titer >1:100 in all patients)",
      "cross_reactive_to_other_serotypes": "Not assessed"
    },
    "malignancy_screening": {
      "hematologic_malignancy": 0,
      "solid_tumor": 0,
      "integration_site_analysis": "Performed annually; no clonal expansion or dominant integration sites detected"
    },
    "motor_milestones": {
      "sitting_without_support_at_year_5": "18/21 (85.7%)",
      "walking_independently_at_year_5": "4/21 (19.0%)",
      "chop_intend_mean_change_from_baseline_year_5": "+28.4"
    }
  },
  "sample_ltfu_visit": {
    "visit_year": 3,
    "subject_id": "SMA-001-0008",
    "age_at_visit": "3.8 years",
    "vital_status": "Alive",
    "vcn_blood": 3.8,
    "vcn_urine": "Not detected",
    "rcl_panel": "Negative",
    "rcl_monitoring_rationale": "AAV is non-integrating; RCL testing not required for AAV products (required for retroviral/lentiviral vectors only)",
    "aat_liver_panel": "AST 38, ALT 42, Total bilirubin 0.6, Albumin 4.2 (all WNL)",
    "platelet_count": "248 × 10^9/L (WNL)",
    "immunogenicity": "Anti-AAV9 NAb titer 1:3200 (not clinically significant per protocol)",
    "chop_intend_score": 52,
    "adverse_events_since_last_visit": [
      {"ae": "Upper respiratory infection", "grade": 1, "serious": false, "resolved": true}
    ]
  }
}
```

---

## Validation Rules

| Rule | Requirement | Example |
|------|-------------|---------|
| CRS Fever | Grade ≥2 CRS requires documented fever ≥38°C | T = 37.5°C → cannot be Grade ≥2 |
| CRS Timing | CRS onset typically Day 2-7; onset after Day 14 unusual (query) | CRS onset Day 30 → flag for medical review |
| ICANS Timing | ICANS occurs after or concurrent with CRS; isolated ICANS rare | ICANS Day 2 without CRS → possible but requires documentation |
| ICE Score Calculation | ICE must be scored 0-10; Grade 3+ requiring ICE 0-2 | ICE = 6 → max ICANS Grade 2 (unless other Grade 3 criteria met) |
| Viability Threshold | Infused product must meet viability specification (≥70%) | Viability = 65% → out of specification; requires documented deviation |
| Lymphodepletion Window | LD must be administered Days -7 to -2 prior to infusion | LD Day -8 or Day -1 → protocol deviation |
| Tocilizumab Administration | Tocilizumab only for CRS (not ICANS); corticosteroids for ICANS | Tocilizumab for ICANS alone → protocol deviation (off-label) |
| AAV NAb Screening | NAb titer must be assessed before AAV gene therapy dosing | NAb assessment >7 days before infusion → repeat at baseline |
| RCL Monitoring Schedule | Lentiviral/retroviral products: q3m Year 1, q6m Year 2-5, annually through Year 15 | Year 4 with no RCL assessment → data gap |
| VCN in Semen | Male subjects receiving integrating vectors: q3m until 3 consecutive negatives, then annually | Single negative → not sufficient for clearance |
| Insertion Site Analysis | Required for integrating vectors (LV, RV); not required for AAV | ISA not performed for lentiviral CAR-T → regulatory gap |
| 15-Year LTFU Consent | All gene therapy subjects must consent to LTFU before dosing | LTFU consent dated after infusion → protocol violation |

### Business Rules

- **CRS Management Algorithm**: Tocilizumab must be available on-site before CAR-T infusion; first dose of tocilizumab should be administered within 2 hours of meeting Grade 2 criteria
- **ICANS Management**: Corticosteroids are first-line for ICANS; avoid tocilizumab monotherapy for ICANS (tocilizumab may worsen ICANS by increasing IL-6 levels in CNS)
- **Product Chain of Identity**: Dual-identifier verification (subject ID + unique product ID) at apheresis, manufacturing receipt, product release, and infusion to prevent administration errors
- **Bridging Therapy**: Bridging between apheresis and CAR-T infusion is allowed but must be documented; lymphodepletion washout period of ≥48 hours from last bridging dose
- **Out-of-Specification (OOS)**: Any OOS product must be evaluated by a cross-functional team (CMC, clinical, regulatory); infusion of OOS product requires documented justification and regulatory notification
- **Vector Shedding**: Serial assessment of vector in blood, urine, saliva, and semen (integrating vectors); semen must clear to negative x3 before contraception can be discontinued
- **SUD (Severe Unexpected Disease)**: Any new malignancy, new neurologic disorder, new autoimmune condition, or new hematologic disorder during LTFU must be reported as a SUSAR within 15 days
- **AAV Hepatotoxicity Management**: Prophylactic prednisolone starting Day -1; AST/ALT monitoring Day 1, 3, 7, 14, 21, 28; taper guided by LFT trajectory

---

## Related Skills

### TrialSim Domains
- [adverse-events-ae.md](../domains/adverse-events-ae.md) - AE domain with CGT-specific MedDRA coding (CRS, ICANS, neurotoxicity, hepatotoxicity, TMA)
- [demographics-dm.md](../domains/demographics-dm.md) - Demographics including pediatric populations (common in CGT)
- [exposure-ex.md](../domains/exposure-ex.md) - Single-dose exposure model (most CGT products)
- [concomitant-meds-cm.md](../domains/concomitant-meds-cm.md) - Tocilizumab, corticosteroids, antiepileptics
- [laboratory-lb.md](../domains/laboratory-lb.md) - VCN, RCL, immunogenicity, cytokine panels

### TrialSim Core
- [../clinical-trials-domain.md](../clinical-trials-domain.md) - Core trial design concepts
- [../phase3-pivotal.md](../phase3-pivotal.md) - Phase III pivotal cohort designs

### Therapeutic Areas
- [oncology.md](oncology.md) - CAR-T in hematologic malignancies
- [cns.md](cns.md) - Neurotoxicity monitoring overlap

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-06 | Initial CGT therapeutic area skill with CAR-T (lymphodepletion, CRS grading per ASTCT/Lee 2019, ICANS, manufacturing), AAV gene therapy (VCN, NAb, biodistribution), CRISPR assessments, 15-year long-term follow-up per FDA guidance, and RCL monitoring |
