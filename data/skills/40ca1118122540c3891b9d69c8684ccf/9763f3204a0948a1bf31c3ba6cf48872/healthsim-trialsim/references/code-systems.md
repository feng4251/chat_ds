---
name: code-systems
description: |
  Comprehensive reference for clinical trial coding systems used across TrialSim:
  MedDRA v27.0, LOINC v2.78, ATC WHO 2025, CDISC Controlled Terminology (NCI EVS),
  and ISO standards. Referenced by all domain skills for terminology lookups.
---

# Code Systems Reference

This reference provides detailed lookup tables for all coding systems used in TrialSim clinical trial synthetic data generation. It is referenced by SKILL.md and all domain-level skills for standardized terminology.

---

## 1. MedDRA (Medical Dictionary for Regulatory Activities) v27.0

MedDRA is the ICH standard medical terminology for coding adverse events, medical history, and indications. It uses a 5-level hierarchy from broadest to most specific.

### 1.1 Hierarchy Structure

```
System Organ Class (SOC)         ← 27 categories
  └── High Level Group Term (HLGT)
      └── High Level Term (HLT)
          └── Preferred Term (PT) ← AEDECOD
              └── Lowest Level Term (LLT)
```

- **SOC** -- Highest level, represents anatomical or physiological system
- **HLGT** -- Groups HLTs by anatomy, pathology, or etiology
- **HLT** -- Groups PTs by anatomy, pathology, or etiology
- **PT** -- Preferred term; single concept for a medical condition
- **LLT** -- Lowest level; synonyms, lexical variants, abbreviations

### 1.2 Code Format

- SOC codes: 8 digits (e.g., `10017947`)
- PT codes: 8 digits (e.g., `10028813`)
- Codes are not hierarchical -- higher-level codes are independent 8-digit identifiers

### 1.3 All 27 System Organ Classes (SOCs)

| SOC Code | System Organ Class |
|----------|-------------------|
| 10005329 | Blood and lymphatic system disorders |
| 10007541 | Cardiac disorders |
| 10010331 | Congenital, familial and genetic disorders |
| 10014698 | Ear and labyrinth disorders |
| 10015919 | Endocrine disorders |
| 10017758 | Eye disorders |
| 10017947 | Gastrointestinal disorders |
| 10018065 | General disorders and administration site conditions |
| 10019805 | Hepatobiliary disorders |
| 10021428 | Infections and infestations |
| 10022022 | Injury, poisoning and procedural complications |
| 10022891 | Investigations |
| 10027433 | Metabolism and nutrition disorders |
| 10028395 | Musculoskeletal and connective tissue disorders |
| 10135991 | Neoplasms benign, malignant and unspecified (incl cysts and polyps) |
| 10029205 | Nervous system disorders |
| 10034259 | Pregnancy, puerperium and perinatal conditions |
| 10036523 | Product issues |
| 10037175 | Psychiatric disorders |
| 10038359 | Renal and urinary disorders |
| 10038594 | Reproductive system and breast disorders |
| 10038738 | Respiratory, thoracic and mediastinal disorders |
| 10040785 | Skin and subcutaneous tissue disorders |
| 10041244 | Social circumstances |
| 10042613 | Surgical and medical procedures |
| 10047065 | Vascular disorders |
| 10053636 | Immune system disorders |

### 1.4 Common Adverse Event Terms Mapped to PT and SOC

The following 100+ common clinical trial AEs are indexed by their MedDRA Preferred Term (PT) to the primary System Organ Class (SOC).

#### Blood and Lymphatic System Disorders (SOC: 10005329)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Anaemia | 10002034 |
| Febrile neutropenia | 10016288 |
| Neutropenia | 10029354 |
| Thrombocytopenia | 10043554 |
| Leukopenia | 10024384 |
| Lymphopenia | 10025446 |
| Pancytopenia | 10033626 |

#### Cardiac Disorders (SOC: 10007541)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Angina pectoris | 10002383 |
| Atrial fibrillation | 10003658 |
| Cardiac failure | 10007554 |
| Myocardial infarction | 10028596 |
| Palpitations | 10033557 |
| Tachycardia | 10043071 |
| Bradycardia | 10006053 |
| Pericarditis | 10034484 |
| Atrioventricular block | 10003586 |

#### Endocrine Disorders (SOC: 10015919)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Hyperthyroidism | 10020850 |
| Hypothyroidism | 10021114 |
| Adrenal insufficiency | 10001368 |
| Cushingoid | 10011650 |

#### Gastrointestinal Disorders (SOC: 10017947)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Abdominal pain | 10000081 |
| Abdominal pain upper | 10000087 |
| Constipation | 10010774 |
| Diarrhoea | 10012735 |
| Dyspepsia | 10013946 |
| Dysphagia | 10013950 |
| Flatulence | 10016897 |
| Gastritis | 10017867 |
| Gastroesophageal reflux disease | 10017885 |
| Nausea | 10028813 |
| Pancreatitis | 10033552 |
| Stomatitis | 10042128 |
| Vomiting | 10047700 |
| Dry mouth | 10013781 |
| Abdominal distension | 10000060 |

#### General Disorders and Administration Site Conditions (SOC: 10018065)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Asthenia | 10003598 |
| Chills | 10008531 |
| Death | 10011906 |
| Fatigue | 10016256 |
| Injection site reaction | 10022095 |
| Malaise | 10025482 |
| Oedema peripheral | 10030097 |
| Pain | 10033340 |
| Pyrexia | 10037660 |

#### Hepatobiliary Disorders (SOC: 10019805)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Cholecystitis | 10008612 |
| Drug-induced liver injury | 10058692 |
| Hepatic failure | 10019782 |
| Hepatitis | 10019797 |
| Hepatomegaly | 10019864 |
| Jaundice | 10023126 |
| Liver function test abnormal | 10037273 |

#### Infections and Infestations (SOC: 10021428)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Bronchitis | 10006451 |
| Cellulitis | 10007946 |
| Conjunctivitis | 10010741 |
| Cystitis | 10011776 |
| Influenza | 10022000 |
| Nasopharyngitis | 10028810 |
| Otitis media | 10033078 |
| Pharyngitis | 10034835 |
| Pneumonia | 10035664 |
| Rhinitis | 10039083 |
| Sepsis | 10040047 |
| Sinusitis | 10040746 |
| Upper respiratory tract infection | 10046301 |
| Urinary tract infection | 10046571 |
| COVID-19 | 10084394 |
| Gastroenteritis | 10017888 |

#### Investigations (SOC: 10022891)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Alanine aminotransferase increased | 10001551 |
| Aspartate aminotransferase increased | 10003481 |
| Blood alkaline phosphatase increased | 10005591 |
| Blood bilirubin increased | 10005682 |
| Blood creatinine increased | 10005557 |
| Blood glucose increased | 10005722 |
| Blood urea increased | 10005803 |
| Electrocardiogram QT prolonged | 10064027 |
| Haemoglobin decreased | 10018862 |
| Neutrophil count decreased | 10029413 |
| Platelet count decreased | 10035528 |
| Weight decreased | 10047888 |
| Weight increased | 10047895 |
| White blood cell count decreased | 10047942 |
| Gamma-glutamyltransferase increased | 10018065 |

#### Metabolism and Nutrition Disorders (SOC: 10027433)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Anorexia | 10002653 |
| Decreased appetite | 10011924 |
| Dehydration | 10012174 |
| Diabetes mellitus | 10012601 |
| Hypercalcaemia | 10020583 |
| Hyperglycaemia | 10020630 |
| Hyperkalemia | 10020647 |
| Hyperlipidaemia | 10020688 |
| Hyperuricaemia | 10020952 |
| Hypocalcaemia | 10020997 |
| Hypoglycaemia | 10020993 |
| Hypokalaemia | 10021018 |
| Hyponatraemia | 10021030 |
| Hypophosphataemia | 10021093 |
| Increased appetite | 10022050 |
| Obesity | 10029882 |

#### Musculoskeletal and Connective Tissue Disorders (SOC: 10028395)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Arthralgia | 10003239 |
| Back pain | 10003988 |
| Bone pain | 10005999 |
| Muscle spasms | 10028322 |
| Muscular weakness | 10028327 |
| Myalgia | 10028411 |
| Osteoarthritis | 10031161 |
| Osteoporosis | 10031285 |
| Pain in extremity | 10033425 |
| Rhabdomyolysis | 10039020 |
| Neck pain | 10028706 |

#### Nervous System Disorders (SOC: 10029205)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Ageusia | 10001450 |
| Cerebral haemorrhage | 10007981 |
| Cerebrovascular accident | 10008147 |
| Cognitive disorder | 10009755 |
| Dizziness | 10013573 |
| Dysgeusia | 10013911 |
| Headache | 10019211 |
| Lethargy | 10024275 |
| Memory impairment | 10027159 |
| Migraine | 10027599 |
| Neuropathy peripheral | 10029331 |
| Paraesthesia | 10033775 |
| Presyncope | 10036636 |
| Sciatica | 10039668 |
| Seizure | 10039914 |
| Somnolence | 10041334 |
| Syncope | 10042772 |
| Tremor | 10044567 |
| Hypoaesthesia | 10020952 |

#### Psychiatric Disorders (SOC: 10037175)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Agitation | 10001497 |
| Anxiety | 10002855 |
| Confusional state | 10010424 |
| Delirium | 10012218 |
| Depression | 10012378 |
| Hallucination | 10019070 |
| Insomnia | 10022437 |
| Irritability | 10023056 |
| Suicidal ideation | 10042458 |
| Suicide attempt | 10042464 |

#### Renal and Urinary Disorders (SOC: 10038359)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Acute kidney injury | 10051176 |
| Chronic kidney disease | 10064848 |
| Dysuria | 10013987 |
| Haematuria | 10018862 |
| Proteinuria | 10037039 |
| Renal failure | 10038436 |
| Urinary incontinence | 10046543 |
| Urinary retention | 10046542 |

#### Respiratory, Thoracic and Mediastinal Disorders (SOC: 10038738)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Asthma | 10003553 |
| Cough | 10011224 |
| Dyspnoea | 10013968 |
| Epistaxis | 10015056 |
| Haemoptysis | 10018951 |
| Hypoxia | 10021174 |
| Nasal congestion | 10028733 |
| Oropharyngeal pain | 10031078 |
| Pleural effusion | 10035598 |
| Pneumonitis | 10035742 |
| Pulmonary embolism | 10037377 |
| Pulmonary hypertension | 10037400 |
| Respiratory failure | 10038678 |
| Wheezing | 10047985 |
| Interstitial lung disease | 10022610 |

#### Skin and Subcutaneous Tissue Disorders (SOC: 10040785)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Acne | 10000496 |
| Alopecia | 10001760 |
| Dermatitis | 10012435 |
| Dry skin | 10013787 |
| Erythema | 10015150 |
| Hyperhidrosis | 10020642 |
| Photosensitivity reaction | 10034960 |
| Pruritus | 10037087 |
| Psoriasis | 10037153 |
| Rash | 10037844 |
| Rash erythematous | 10037868 |
| Rash maculo-papular | 10037877 |
| Stevens-Johnson syndrome | 10042041 |
| Urticaria | 10046735 |

#### Vascular Disorders (SOC: 10047065)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Deep vein thrombosis | 10011913 |
| Flushing | 10016880 |
| Haematoma | 10018821 |
| Hot flush | 10020282 |
| Hypertension | 10020772 |
| Hypotension | 10021097 |
| Orthostatic hypotension | 10031281 |
| Phlebitis | 10034903 |
| Thrombosis | 10043993 |

#### Immune System Disorders (SOC: 10053636)

| Preferred Term (PT) | PT Code |
|--------------------|----------|
| Anaphylactic reaction | 10002198 |
| Anaphylactic shock | 10002218 |
| Drug hypersensitivity | 10013700 |
| Hypersensitivity | 10020751 |
| Infusion related reaction | 10022119 |
| Cytokine release syndrome | 10063378 |

### 1.5 MedDRA Hierarchy Example: Nausea

```
System Organ Class (SOC): Gastrointestinal disorders [10017947]
  └── High Level Group Term (HLGT): Gastrointestinal signs and symptoms [10017977]
      └── High Level Term (HLT): Nausea and vomiting symptoms [10028815]
          └── Preferred Term (PT): Nausea [10028813]
              └── Lowest Level Term (LLT): Feeling of nausea [10016124]
              └── Lowest Level Term (LLT): Nauseous [10028766]
```

### 1.6 Standardised MedDRA Queries (SMQs)

SMQs are groupings of MedDRA terms used to identify cases potentially representing a specific medical condition. They are essential for safety signal detection in clinical trials.

#### SMQ: Anaphylactic Reaction (SMQ Code: 20000004)

| Category | Term |
|----------|------|
| **Definition** | Acute, potentially life-threatening immediate-type hypersensitivity reaction |
| **Scope** | Narrow and broad terms capturing anaphylaxis and anaphylactoid reactions |
| **Key Narrow PTs** | Anaphylactic reaction, Anaphylactic shock, Anaphylactoid reaction, Circulatory collapse |
| **Key Broad PTs** | Chest discomfort, Dyspnoea, Hypotension, Laryngeal oedema, Swelling face, Urticaria, Wheezing |
| **Algorithm** | Narrow: 1+ narrow term; Broad: narrow OR (2+ broad terms from different categories) |

#### SMQ: Myocardial Infarction (SMQ Code: 20000046)

| Category | Term |
|----------|------|
| **Definition** | Necrosis of cardiac muscle due to prolonged ischaemia |
| **Scope** | Terms for myocardial infarction irrespective of mechanism |
| **Key Narrow PTs** | Myocardial infarction, Acute myocardial infarction, Myocardial reinfarction, STEMI, NSTEMI |
| **Key Broad PTs** | Troponin increased, Electrocardiogram ST segment elevation, Angina unstable |
| **Algorithm** | Narrow: 1+ narrow term; Broad: narrow OR relevant broad terms with clinical context |

#### SMQ: Hepatic Disorders (SMQ Code: 20000009)

| Category | Term |
|----------|------|
| **Definition** | Broad spectrum of drug-related hepatic conditions |
| **Scope** | Covers all manifestations: cytolytic, cholestatic, mixed, and clinical |
| **Key Narrow PTs** | Drug-induced liver injury, Hepatic failure, Hepatitis fulminant, Hepatic necrosis, Acute hepatic failure |
| **Key Broad PTs** | ALT increased, AST increased, ALP increased, Blood bilirubin increased, Jaundice, Hepatitis, Hepatic function abnormal |
| **Algorithm** | Narrow: 1+ narrow term; Broad: narrow OR relevant lab + clinical broad terms |
| **Sub-SMQs** | Drug related hepatic disorders -- severe (20000019), Hepatitis non-infectious (20000010), Hepatic failure, fibrosis and cirrhosis (20000011), Cholestasis and jaundice (20000017) |

#### SMQ: Acute Renal Failure (SMQ Code: 20000002)

| Category | Term |
|----------|------|
| **Definition** | Sudden deterioration in renal function |
| **Scope** | Terms for acute kidney injury and renal failure |
| **Key Narrow PTs** | Acute kidney injury, Renal failure acute, Anuria, Renal tubular necrosis, Oliguria |
| **Key Broad PTs** | Blood creatinine increased, Blood urea increased, Glomerular filtration rate decreased, Dysuria, Haematuria, Proteinuria |
| **Algorithm** | Narrow: 1+ narrow term; Broad: narrow OR (lab abnormality + clinical finding) |

#### SMQ: Severe Cutaneous Adverse Reactions (SMQ Code: 20000020)

| Category | Term |
|----------|------|
| **Definition** | Life-threatening drug-induced skin reactions |
| **Scope** | SCARs including SJS, TEN, DRESS, AGEP |
| **Key Narrow PTs** | Stevens-Johnson syndrome, Toxic epidermal necrolysis, Drug reaction with eosinophilia and systemic symptoms, Acute generalised exanthematous pustulosis |
| **Key Broad PTs** | Rash, Erythema multiforme, Skin exfoliation, Oral mucosal blistering, Skin necrosis, Mucosal ulceration |
| **Algorithm** | Narrow: 1+ narrow term; Broad: narrow OR temporally associated broad terms |

---

## 2. LOINC (Logical Observation Identifiers Names and Codes) v2.78

LOINC is the universal standard for identifying laboratory and clinical observations. Each LOINC code represents a unique combination of component, property, timing, system, scale, and method.

### 2.1 LOINC Parts Structure

```
LOINC = Component : Property : Timing : System : Scale : Method
           |           |         |         |       |       |
         What      Kind of      Time    Sample    Type     How
        measured   quantity    aspect    type    of scale measured
```

**Example: Glucose in serum/plasma -- `2345-7`**
- Component: Glucose
- Property: SCnc (Substance Concentration)
- Timing: Pt (Point in time)
- System: Ser/Plas (Serum or Plasma)
- Scale: Qn (Quantitative)
- Method: (none specified)

### 2.2 Comprehensive Metabolic Panel (CMP)

| LBTESTCD | Test Name | LOINC Code | Unit | Reference Range (Male) | Reference Range (Female) |
|----------|-----------|------------|------|------------------------|--------------------------|
| GLUC | Glucose | 2345-7 | mmol/L | 3.9--5.6 (fasting) | 3.9--5.6 (fasting) |
| BUN | Blood Urea Nitrogen | 3094-0 | mmol/L | 2.5--7.1 | 2.5--7.1 |
| CREAT | Creatinine | 2160-0 | umol/L | 62--106 | 44--80 |
| SODIUM | Sodium | 2951-2 | mmol/L | 136--145 | 136--145 |
| POTAS | Potassium | 2823-3 | mmol/L | 3.5--5.0 | 3.5--5.0 |
| CHLOR | Chloride | 2075-0 | mmol/L | 98--106 | 98--106 |
| CO2 | Carbon Dioxide | 2028-9 | mmol/L | 23--29 | 23--29 |
| CALC | Calcium | 17861-6 | mmol/L | 2.15--2.55 | 2.15--2.55 |
| TP | Total Protein | 2885-2 | g/L | 60--80 | 60--80 |
| ALB | Albumin | 1751-7 | g/L | 35--50 | 35--50 |
| ALT | Alanine Aminotransferase | 1742-6 | U/L | 7--56 | 7--56 |
| AST | Aspartate Aminotransferase | 1920-8 | U/L | 10--40 | 10--40 |
| ALP | Alkaline Phosphatase | 6768-6 | U/L | 44--147 | 44--147 |
| BILI | Total Bilirubin | 1975-2 | umol/L | 3--21 | 3--21 |
| BILD | Direct Bilirubin | 1968-7 | umol/L | 0--5 | 0--5 |
| GGT | Gamma GT | 2324-2 | U/L | 8--61 | 5--36 |
| ANION | Anion Gap | 18631-3 | mmol/L | 8--16 | 8--16 |
| URIC | Uric Acid | 3084-1 | umol/L | 230--480 | 150--360 |
| GLUF | Glucose Fasting | 1558-6 | mmol/L | 3.9--5.6 | 3.9--5.6 |

### 2.3 Complete Blood Count (CBC) with Differential

| LBTESTCD | Test Name | LOINC Code | Unit | Reference Range (Male) | Reference Range (Female) |
|----------|-----------|------------|------|------------------------|--------------------------|
| WBC | White Blood Cell Count | 6690-2 | 10^9/L | 4.5--11.0 | 4.5--11.0 |
| RBC | Red Blood Cell Count | 789-8 | 10^12/L | 4.5--5.5 | 4.0--5.0 |
| HGB | Hemoglobin | 718-7 | g/L | 135--175 | 120--160 |
| HCT | Hematocrit | 4544-3 | Fraction | 0.40--0.50 | 0.36--0.44 |
| MCV | Mean Corpuscular Volume | 787-2 | fL | 80--100 | 80--100 |
| MCH | Mean Corpuscular Hemoglobin | 785-6 | pg | 27--33 | 27--33 |
| MCHC | Mean Corpuscular Hgb Concentration | 786-4 | g/L | 320--360 | 320--360 |
| RDW | Red Cell Distribution Width | 788-0 | % | 11.5--14.5 | 11.5--14.5 |
| PLAT | Platelet Count | 777-3 | 10^9/L | 150--400 | 150--400 |
| MPV | Mean Platelet Volume | 32623-1 | fL | 7.5--11.5 | 7.5--11.5 |
| NEUTA | Neutrophils Absolute | 751-8 | 10^9/L | 2.0--7.5 | 2.0--7.5 |
| NEUTP | Neutrophils Percentage | 770-8 | % | 40--75 | 40--75 |
| LYMFA | Lymphocytes Absolute | 731-0 | 10^9/L | 1.0--4.0 | 1.0--4.0 |
| LYMFTP | Lymphocytes Percentage | 736-9 | % | 20--45 | 20--45 |
| MONOA | Monocytes Absolute | 742-7 | 10^9/L | 0.2--0.8 | 0.2--0.8 |
| MONOP | Monocytes Percentage | 5905-5 | % | 2--10 | 2--10 |
| EOAA | Eosinophils Absolute | 711-2 | 10^9/L | 0.0--0.4 | 0.0--0.4 |
| EOP | Eosinophils Percentage | 713-8 | % | 0--6 | 0--6 |
| BASOA | Basophils Absolute | 704-7 | 10^9/L | 0.0--0.2 | 0.0--0.2 |
| BASOP | Basophils Percentage | 706-2 | % | 0--2 | 0--2 |

### 2.4 Coagulation Panel

| LBTESTCD | Test Name | LOINC Code | Unit | Reference Range |
|----------|-----------|------------|------|-----------------|
| PT | Prothrombin Time | 5902-2 | s | 11.0--13.5 |
| INR | INR | 6301-6 | (ratio) | 0.9--1.1 (therapeutic: 2.0--3.0) |
| APTT | Activated Partial Thromboplastin Time | 3173-2 | s | 25--35 |
| FIB | Fibrinogen | 3255-7 | g/L | 2.0--4.0 |
| DDIM | D-Dimer | 48065-7 | mg/L FEU | < 0.50 |

### 2.5 Endocrinology & Other Specialty Tests

| LBTESTCD | Test Name | LOINC Code | Unit | Reference Range |
|----------|-----------|------------|------|-----------------|
| TSH | Thyroid Stimulating Hormone | 3016-3 | mIU/L | 0.4--4.0 |
| FT4 | Free T4 (Thyroxine) | 3024-7 | pmol/L | 9.0--24.0 |
| FT3 | Free T3 (Triiodothyronine) | 3051-0 | pmol/L | 3.5--6.5 |
| HBA1C | Hemoglobin A1c | 4548-4 | % | < 5.7 (normal) |
| FPG | Fasting Plasma Glucose | 1558-6 | mmol/L | 3.9--5.6 |
| EGFR | Estimated GFR | 62238-1 | mL/min/1.73m2 | >= 90 (normal) |
| CRPCRP | C-Reactive Protein | 1988-5 | mg/L | < 5.0 |
| HSCRP | High Sensitivity CRP | 30522-7 | mg/L | < 3.0 |
| CK | Creatine Kinase | 2157-6 | U/L | 30--200 (M), 20--170 (F) |
| CKM B | CK-MB | 32673-6 | ug/L | < 5.0 |
| TROP_I | Troponin I | 42757-7 | ng/L | < 35 |
| TROP_T | Troponin T | 6598-7 | ng/L | < 14 |
| BNP | B-Type Natriuretic Peptide | 33762-6 | ng/L | < 100 |
| NTBNP | NT-proBNP | 33763-4 | ng/L | < 125 (< 75 age) |
| FERR | Ferritin | 2276-4 | ug/L | 20--250 (M), 10--120 (F) |
| IRON | Iron | 2498-4 | umol/L | 11--28 |
| TIBC | Total Iron Binding Capacity | 2500-7 | umol/L | 44--80 |
| CHOL | Total Cholesterol | 2093-3 | mmol/L | < 5.2 |
| LDL | LDL Cholesterol | 13457-7 | mmol/L | < 2.6 (optimal) |
| HDL | HDL Cholesterol | 2085-9 | mmol/L | > 1.0 (M), > 1.3 (F) |
| TRIG | Triglycerides | 2571-8 | mmol/L | < 1.7 |
| AMYL | Amylase | 1798-8 | U/L | 30--110 |
| LIP | Lipase | 3040-3 | U/L | 10--60 |
| LDH | Lactate Dehydrogenase | 2532-0 | U/L | 100--250 |
| PHOS | Phosphate | 2777-1 | mmol/L | 0.80--1.45 |
| MG | Magnesium | 19123-9 | mmol/L | 0.70--1.00 |
| VB12 | Vitamin B12 | 2132-9 | pmol/L | 133--675 |
| FOL | Folate | 2284-8 | nmol/L | 7--45 |
| VD25 | Vitamin D 25-Hydroxy | 1989-3 | nmol/L | 50--150 |
| PSA | Prostate Specific Antigen | 2857-1 | ug/L | < 4.0 (age-adjusted) |
| CEAP | CEA | 2039-6 | ug/L | < 3.0 |
| CA125 | CA-125 | 10334-1 | kU/L | < 35 |
| AFP | Alpha-Fetoprotein | 1834-1 | ug/L | < 10 |

### 2.6 Urinalysis

| LBTESTCD | Test Name | LOINC Code | Unit | Reference Range |
|----------|-----------|------------|------|-----------------|
| URPH | Urine pH | 2756-5 | (unitless) | 4.5--8.0 |
| URSG | Urine Specific Gravity | 5811-5 | (unitless) | 1.005--1.030 |
| URPRO | Urine Protein | 2888-6 | g/L | Negative |
| URGLU | Urine Glucose | 5792-7 | mmol/L | Negative |
| URKET | Urine Ketones | 5797-6 | mmol/L | Negative |
| URBIL | Urine Bilirubin | 5770-3 | (qual) | Negative |
| URBLD | Urine Blood | 5794-3 | (qual) | Negative |
| URNIT | Urine Nitrite | 5802-4 | (qual) | Negative |
| URLEU | Urine Leukocyte Esterase | 5799-2 | (qual) | Negative |
| UROBIL | Urobilinogen | 5818-0 | EU/dL | 0.1--1.0 |

### 2.7 Vital-Sign-Relevant LOINC Codes

| VSTESTCD | Vital Sign | LOINC Code | Unit | Normal Range |
|----------|-----------|------------|------|--------------|
| SYSBP | Systolic Blood Pressure | 8480-6 | mmHg | 90--139 |
| DIABP | Diastolic Blood Pressure | 8462-4 | mmHg | 60--89 |
| PULSE | Heart Rate | 8867-4 | beats/min | 60--100 |
| RESP | Respiratory Rate | 9279-1 | breaths/min | 12--20 |
| TEMP | Body Temperature | 8310-5 | C | 36.1--37.2 |
| HEIGHT | Body Height | 8302-2 | cm | N/A |
| WEIGHT | Body Weight | 3141-9 | kg | N/A |
| BMI | Body Mass Index | 39156-5 | kg/m2 | 18.5--24.9 |
| OXYSAT | Oxygen Saturation | 2708-6 | % | 95--100 |
| HEADCIRC | Head Circumference | 8287-5 | cm | N/A (pediatric) |

---

## 3. ATC (Anatomical Therapeutic Chemical) Classification WHO 2025

The ATC classification system categorizes drugs based on their therapeutic, pharmacological, and chemical properties. It is maintained by the WHO Collaborating Centre for Drug Statistics Methodology.

### 3.1 Five-Level Hierarchy

```
Level 1: Anatomical main group           (1 letter,  e.g., "A")
Level 2: Therapeutic subgroup            (2 digits,  e.g., "A10")
Level 3: Pharmacological subgroup        (1 letter,  e.g., "A10B")
Level 4: Chemical subgroup               (1 letter,  e.g., "A10BA")
Level 5: Chemical substance              (2 digits,  e.g., "A10BA02" = Metformin)
```

**Code format**: One letter + two digits + one letter + one letter + two digits = 7 characters

### 3.2 Level 1: Anatomical Main Groups

| ATC Code | Description |
|----------|-------------|
| A | Alimentary tract and metabolism |
| B | Blood and blood forming organs |
| C | Cardiovascular system |
| D | Dermatologicals |
| G | Genito-urinary system and sex hormones |
| H | Systemic hormonal preparations, excl. sex hormones and insulins |
| J | Antiinfectives for systemic use |
| L | Antineoplastic and immunomodulating agents |
| M | Musculo-skeletal system |
| N | Nervous system |
| P | Antiparasitic products, insecticides and repellents |
| R | Respiratory system |
| S | Sensory organs |
| V | Various |

### 3.3 Common Clinical Trial Concomitant Medications with Full ATC Codes

#### Diabetes Medications

| INN / Drug Name | ATC Code | ATC Description | Class |
|----------------|----------|-----------------|-------|
| Metformin | A10BA02 | Metformin | Biguanide |
| Insulin glargine | A10AE04 | Insulin glargine | Long-acting insulin analogue |
| Insulin lispro | A10AB04 | Insulin lispro | Rapid-acting insulin analogue |
| Insulin aspart | A10AB05 | Insulin aspart | Rapid-acting insulin analogue |
| Insulin detemir | A10AE05 | Insulin detemir | Long-acting insulin analogue |
| Insulin degludec | A10AE06 | Insulin degludec | Ultra-long-acting insulin |
| Glipizide | A10BB07 | Glipizide | Sulfonylurea |
| Gliclazide | A10BB09 | Gliclazide | Sulfonylurea |
| Empagliflozin | A10BK03 | Empagliflozin | SGLT2 inhibitor |
| Dapagliflozin | A10BK01 | Dapagliflozin | SGLT2 inhibitor |
| Sitagliptin | A10BH01 | Sitagliptin | DPP-4 inhibitor |
| Liraglutide | A10BJ02 | Liraglutide | GLP-1 receptor agonist |
| Semaglutide | A10BJ06 | Semaglutide | GLP-1 receptor agonist |
| Pioglitazone | A10BG03 | Pioglitazone | Thiazolidinedione |

#### Cardiovascular Medications

| INN / Drug Name | ATC Code | ATC Description | Class |
|----------------|----------|-----------------|-------|
| Atorvastatin | C10AA05 | Atorvastatin | Statin (HMG-CoA reductase inhibitor) |
| Rosuvastatin | C10AA07 | Rosuvastatin | Statin |
| Simvastatin | C10AA01 | Simvastatin | Statin |
| Ezetimibe | C10AX09 | Ezetimibe | Cholesterol absorption inhibitor |
| Lisinopril | C09AA03 | Lisinopril | ACE inhibitor |
| Enalapril | C09AA02 | Enalapril | ACE inhibitor |
| Ramipril | C09AA05 | Ramipril | ACE inhibitor |
| Losartan | C09CA01 | Losartan | Angiotensin II receptor blocker (ARB) |
| Valsartan | C09CA03 | Valsartan | ARB |
| Candesartan | C09CA06 | Candesartan | ARB |
| Amlodipine | C08CA01 | Amlodipine | Calcium channel blocker (DHP) |
| Nifedipine | C08CA05 | Nifedipine | Calcium channel blocker (DHP) |
| Diltiazem | C08DB01 | Diltiazem | Calcium channel blocker (non-DHP) |
| Metoprolol | C07AB02 | Metoprolol | Beta-blocker (selective) |
| Atenolol | C07AB03 | Atenolol | Beta-blocker (selective) |
| Bisoprolol | C07AB07 | Bisoprolol | Beta-blocker (selective) |
| Carvedilol | C07AG02 | Carvedilol | Alpha/beta-blocker |
| Hydrochlorothiazide | C03AA03 | Hydrochlorothiazide | Thiazide diuretic |
| Furosemide | C03CA01 | Furosemide | Loop diuretic |
| Spironolactone | C03DA01 | Spironolactone | Aldosterone antagonist |
| Warfarin | B01AA03 | Warfarin | Vitamin K antagonist |
| Apixaban | B01AF02 | Apixaban | Direct Factor Xa inhibitor |
| Rivaroxaban | B01AF01 | Rivaroxaban | Direct Factor Xa inhibitor |
| Clopidogrel | B01AC04 | Clopidogrel | P2Y12 platelet inhibitor |
| Aspirin (low dose) | B01AC06 | Acetylsalicylic acid | Platelet aggregation inhibitor |
| Digoxin | C01AA05 | Digoxin | Cardiac glycoside |
| Amiodarone | C01BD01 | Amiodarone | Class III antiarrhythmic |

#### Gastrointestinal Medications

| INN / Drug Name | ATC Code | ATC Description | Class |
|----------------|----------|-----------------|-------|
| Omeprazole | A02BC01 | Omeprazole | Proton pump inhibitor (PPI) |
| Pantoprazole | A02BC02 | Pantoprazole | PPI |
| Esomeprazole | A02BC05 | Esomeprazole | PPI |
| Ranitidine | A02BA02 | Ranitidine | H2-receptor antagonist |
| Famotidine | A02BA03 | Famotidine | H2-receptor antagonist |
| Ondansetron | A04AA01 | Ondansetron | 5-HT3 receptor antagonist |
| Metoclopramide | A03FA01 | Metoclopramide | Prokinetic |
| Loperamide | A07DA03 | Loperamide | Antidiarrheal |
| Lactulose | A06AD11 | Lactulose | Osmotic laxative |
| Mesalazine | A07EC02 | Mesalazine | Aminosalicylic acid (IBD) |

#### Analgesics

| INN / Drug Name | ATC Code | ATC Description | Class |
|----------------|----------|-----------------|-------|
| Paracetamol (Acetaminophen) | N02BE01 | Paracetamol | Anilide analgesic |
| Ibuprofen | M01AE01 | Ibuprofen | NSAID (propionic acid derivative) |
| Naproxen | M01AE02 | Naproxen | NSAID |
| Celecoxib | M01AH01 | Celecoxib | COX-2 selective NSAID |
| Morphine | N02AA01 | Morphine | Opioid (strong) |
| Oxycodone | N02AA05 | Oxycodone | Opioid (strong) |
| Tramadol | N02AX02 | Tramadol | Opioid (weak) |
| Codeine | R05DA04 | Codeine | Opioid (weak/antitussive) |
| Pregabalin | N03AX16 | Pregabalin | Gabapentinoid |
| Gabapentin | N03AX12 | Gabapentin | Gabapentinoid |
| Lidocaine | N01BB02 | Lidocaine | Local anaesthetic |

#### Anti-infectives

| INN / Drug Name | ATC Code | ATC Description | Class |
|----------------|----------|-----------------|-------|
| Amoxicillin | J01CA04 | Amoxicillin | Penicillin (extended spectrum) |
| Amoxicillin/Clavulanic acid | J01CR02 | Amoxicillin and beta-lactamase inhibitor | Penicillin combination |
| Ceftriaxone | J01DD04 | Ceftriaxone | 3rd gen cephalosporin |
| Cefuroxime | J01DC02 | Cefuroxime | 2nd gen cephalosporin |
| Ciprofloxacin | J01MA02 | Ciprofloxacin | Fluoroquinolone |
| Levofloxacin | J01MA12 | Levofloxacin | Fluoroquinolone |
| Azithromycin | J01FA10 | Azithromycin | Macrolide |
| Clarithromycin | J01FA09 | Clarithromycin | Macrolide |
| Doxycycline | J01AA02 | Doxycycline | Tetracycline |
| Vancomycin | J01XA01 | Vancomycin | Glycopeptide |
| Metronidazole | J01XD01 | Metronidazole | Nitroimidazole |
| Fluconazole | J02AC01 | Fluconazole | Triazole antifungal |
| Acyclovir | J05AB01 | Acyclovir | Nucleoside analogue antiviral |
| Oseltamivir | J05AH02 | Oseltamivir | Neuraminidase inhibitor |

#### Central Nervous System

| INN / Drug Name | ATC Code | ATC Description | Class |
|----------------|----------|-----------------|-------|
| Sertraline | N06AB06 | Sertraline | SSRI antidepressant |
| Fluoxetine | N06AB03 | Fluoxetine | SSRI antidepressant |
| Escitalopram | N06AB10 | Escitalopram | SSRI antidepressant |
| Venlafaxine | N06AX16 | Venlafaxine | SNRI antidepressant |
| Lorazepam | N05BA06 | Lorazepam | Benzodiazepine (anxiolytic) |
| Diazepam | N05BA01 | Diazepam | Benzodiazepine (anxiolytic) |
| Zolpidem | N05CF02 | Zolpidem | Non-BZD hypnotic |
| Haloperidol | N05AD01 | Haloperidol | Typical antipsychotic |
| Olanzapine | N05AH03 | Olanzapine | Atypical antipsychotic |
| Quetiapine | N05AH04 | Quetiapine | Atypical antipsychotic |
| Methylphenidate | N06BA04 | Methylphenidate | CNS stimulant |

#### Respiratory

| INN / Drug Name | ATC Code | ATC Description | Class |
|----------------|----------|-----------------|-------|
| Salbutamol (Albuterol) | R03AC02 | Salbutamol | SABA (short-acting beta agonist) |
| Salmeterol | R03AC12 | Salmeterol | LABA (long-acting beta agonist) |
| Ipratropium | R03BB01 | Ipratropium bromide | SAMA (short-acting muscarinic antagonist) |
| Tiotropium | R03BB04 | Tiotropium bromide | LAMA (long-acting muscarinic antagonist) |
| Fluticasone | R03BA05 | Fluticasone | Inhaled corticosteroid |
| Budesonide | R03BA02 | Budesonide | Inhaled corticosteroid |
| Montelukast | R03DC03 | Montelukast | Leukotriene receptor antagonist |

#### Other Common Medications

| INN / Drug Name | ATC Code | ATC Description | Class |
|----------------|----------|-----------------|-------|
| Levothyroxine | H03AA01 | Levothyroxine sodium | Thyroid hormone |
| Allopurinol | M04AA01 | Allopurinol | Xanthine oxidase inhibitor |
| Methotrexate | L04AX03 | Methotrexate | DMARD / antimetabolite |
| Prednisolone | H02AB06 | Prednisolone | Systemic corticosteroid |
| Dexamethasone | H02AB02 | Dexamethasone | Systemic corticosteroid |
| Cyclosporine | L04AD01 | Ciclosporin | Calcineurin inhibitor |
| Methotrexate (oncology) | L01BA01 | Methotrexate | Antimetabolite |
| Diphenhydramine | R06AA02 | Diphenhydramine | 1st gen antihistamine |
| Cetirizine | R06AE07 | Cetirizine | 2nd gen antihistamine |
| Loratadine | R06AX13 | Loratadine | 2nd gen antihistamine |
| Calcium carbonate | A12AA04 | Calcium carbonate | Calcium supplement |
| Colecalciferol (Vit D3) | A11CC05 | Colecalciferol | Vitamin D |
| Iron sulfate | B03AA07 | Ferrous sulfate | Oral iron |
| Folic acid | B03BB01 | Folic acid | Vitamin B9 |
| Cyanocobalamin (Vit B12) | B03BA01 | Cyanocobalamin | Vitamin B12 |
| Sildenafil | G04BE03 | Sildenafil | PDE5 inhibitor (erectile dysfunction) |
| Tamsulosin | G04CA02 | Tamsulosin | Alpha-1 blocker (BPH) |

### 3.4 Commonly Used Multi-Drug Combinations

| Brand Example | Components | ATC Codes |
|--------------|-----------|-----------|
| Co-amoxiclav | Amoxicillin + Clavulanic acid | J01CR02 |
| Co-trimoxazole | Sulfamethoxazole + Trimethoprim | J01EE01 |
| Caduet | Amlodipine + Atorvastatin | C10BX08 |
| Exforge | Amlodipine + Valsartan | C09DX01 |

---

## 4. CDISC Controlled Terminology (NCI EVS)

CDISC Controlled Terminology is maintained by the NCI Enterprise Vocabulary Services (EVS) and provides standardized value sets for SDTM, ADaM, and SEND datasets.

### 4.1 Demographics Terminology

#### SEX (C66731)

| Codelist Code | Term | Decode | Usage |
|--------------|------|--------|-------|
| C66731 | M | MALE | DM domain SEX variable |
| C66731 | F | FEMALE | DM domain SEX variable |
| C66731 | U | UNKNOWN | When sex cannot be determined |
| C66731 | UNDIFFERENTIATED | UNDIFFERENTIATED | Intersex / ambiguous |

#### RACE (C74457)

| Term | Usage Notes |
|------|-------------|
| AMERICAN INDIAN OR ALASKA NATIVE | Indigenous peoples of the Americas |
| ASIAN | East, South, Southeast Asian ancestry |
| BLACK OR AFRICAN AMERICAN | African ancestry |
| NATIVE HAWAIIAN OR OTHER PACIFIC ISLANDER | Indigenous Pacific peoples |
| WHITE | European, Middle Eastern, North African ancestry |
| MULTIPLE | Two or more races |
| OTHER | Race not listed above |
| UNKNOWN | Cannot be determined |
| NOT REPORTED | Subject declined to answer |

#### ETHNIC (C66790)

| Term | Usage Notes |
|------|-------------|
| HISPANIC OR LATINO | Of Cuban, Mexican, Puerto Rican, South/Central American or other Spanish culture |
| NOT HISPANIC OR LATINO | Not of Hispanic/Latino origin |
| NOT REPORTED | Subject declined to answer |
| UNKNOWN | Cannot be determined |

### 4.2 Adverse Events Terminology

#### AESEV -- Severity/Intensity (C82513)

| Term | Definition | CTCAE Relationship |
|------|------------|-------------------|
| MILD | Asymptomatic/mild symptoms; no intervention needed | Grade 1 |
| MODERATE | Minimal, local, or noninvasive intervention; limiting age-appropriate instrumental ADL | Grade 2 |
| SEVERE | Medically significant but not immediately life-threatening; hospitalization or prolongation of hospitalization indicated; disabling; limiting self-care ADL | Grade 3--4 |

#### AEREL -- Causality (C66768)

| Term | Definition |
|------|------------|
| NOT RELATED | No reasonable possibility that the drug caused the event |
| UNLIKELY RELATED | Temporal relationship improbable; other causes more likely |
| POSSIBLY RELATED | Reasonable temporal relationship; could also be explained by other factors |
| PROBABLY RELATED | Reasonable temporal relationship; unlikely attributable to other factors |
| DEFINITELY RELATED | Strong temporal relationship; event improves on dechallenge and reappears on rechallenge |

#### AEOUT -- Outcome (C66769)

| Term | Definition |
|------|------------|
| RECOVERED/RESOLVED | Event has fully resolved |
| RECOVERING/RESOLVING | Event is improving |
| NOT RECOVERED/NOT RESOLVED | Event is ongoing at last assessment |
| RECOVERED/RESOLVED WITH SEQUELAE | Event resolved but with lasting effects |
| FATAL | Event resulted in death |
| UNKNOWN | Outcome cannot be determined |

#### AEACN -- Action Taken (C66767)

| Term | Definition |
|------|------------|
| DOSE NOT CHANGED | Study treatment continued without change |
| DOSE REDUCED | Study treatment dose was decreased |
| DRUG INTERRUPTED | Study treatment temporarily stopped |
| DRUG WITHDRAWN | Study treatment permanently discontinued |
| NOT APPLICABLE | Action not applicable (e.g., non-drug trial) |
| UNKNOWN | Action taken is unknown |

### 4.3 Disposition Terminology

#### DSCAT -- Disposition Category

| Term | Description | Use |
|------|-------------|-----|
| DISPOSITION EVENT | Change in subject's status in study/treatment | Completion, withdrawal, death |
| PROTOCOL MILESTONE | Key protocol-defined events | Informed consent, randomization |
| OTHER EVENT | Additional disposition events | Transfer of care, lost to follow-up |

#### EPOCH -- Study Epoch

| Term | Definition |
|------|------------|
| SCREENING | Period between informed consent and first dose |
| TREATMENT | Period from first dose through last dose + washout |
| FOLLOW-UP | Period after treatment completion for safety monitoring |

### 4.4 Vital Signs Test Codes (VSTESTCD)

| VSTESTCD | VSTEST | Unit Codelist | Unit | Normal Range |
|----------|--------|---------------|------|--------------|
| SYSBP | Systolic Blood Pressure | C49671 (mmHg) | mmHg | 90--139 |
| DIABP | Diastolic Blood Pressure | C49671 (mmHg) | mmHg | 60--89 |
| PULSE | Pulse Rate | C49670 (beats/min) | beats/min | 60--100 |
| RESP | Respiratory Rate | C49670 (breaths/min) | breaths/min | 12--20 |
| TEMP | Temperature | C49672 (C) | C | 36.1--37.2 |
| HEIGHT | Height | C49668 (cm) | cm | N/A |
| WEIGHT | Weight | C28253 (kg) | kg | N/A |
| BMI | Body Mass Index | C49657 (kg/m2) | kg/m2 | 18.5--24.9 |
| OXYSAT | Oxygen Saturation | C49669 (%) | % | 95--100 |

### 4.5 Laboratory Test Code Codelists (LBTESTCD)

| LBTESTCD | LBTEST | LBCAT | Unit Codelist |
|----------|--------|-------|---------------|
| GLUC | Glucose | CHEMISTRY | C28253 (mmol/L) |
| BUN | Blood Urea Nitrogen | CHEMISTRY | C28253 (mmol/L) |
| CREAT | Creatinine | CHEMISTRY | C67107 (umol/L) |
| SODIUM | Sodium | CHEMISTRY | C28253 (mmol/L) |
| POTAS | Potassium | CHEMISTRY | C28253 (mmol/L) |
| CHLOR | Chloride | CHEMISTRY | C28253 (mmol/L) |
| CO2 | Carbon Dioxide | CHEMISTRY | C28253 (mmol/L) |
| CALC | Calcium | CHEMISTRY | C28253 (mmol/L) |
| TP | Total Protein | CHEMISTRY | C28253 (g/L) |
| ALB | Albumin | CHEMISTRY | C28253 (g/L) |
| ALT | Alanine Aminotransferase | CHEMISTRY | C28253 (U/L) |
| AST | Aspartate Aminotransferase | CHEMISTRY | C28253 (U/L) |
| ALP | Alkaline Phosphatase | CHEMISTRY | C28253 (U/L) |
| BILI | Bilirubin | CHEMISTRY | C67107 (umol/L) |
| BILD | Bilirubin Direct | CHEMISTRY | C67107 (umol/L) |
| GGT | Gamma-Glutamyl Transferase | CHEMISTRY | C28253 (U/L) |
| WBC | Leukocytes | HEMATOLOGY | C28253 (10^9/L) |
| RBC | Erythrocytes | HEMATOLOGY | C28253 (10^12/L) |
| HGB | Hemoglobin | HEMATOLOGY | C28253 (g/L) |
| HCT | Hematocrit | HEMATOLOGY | C28253 (ratio) |
| PLAT | Platelets | HEMATOLOGY | C28253 (10^9/L) |
| NEUT | Neutrophils | HEMATOLOGY | C28253 (10^9/L) |
| LYMPH | Lymphocytes | HEMATOLOGY | C28253 (10^9/L) |
| MONO | Monocytes | HEMATOLOGY | C28253 (10^9/L) |
| EO | Eosinophils | HEMATOLOGY | C28253 (10^9/L) |
| BASO | Basophils | HEMATOLOGY | C28253 (10^9/L) |
| HBA1C | Hemoglobin A1c | CHEMISTRY | C49669 (%) |
| TSH | Thyrotropin | CHEMISTRY | C28253 (mIU/L) |
| CK | Creatine Kinase | CHEMISTRY | C28253 (U/L) |
| CRP | C-Reactive Protein | CHEMISTRY | C28253 (mg/L) |
| EGFR | Estimated GFR | CHEMISTRY | C49657 (mL/min/1.73m2) |
| CHOL | Cholesterol | CHEMISTRY | C28253 (mmol/L) |
| TRIG | Triglycerides | CHEMISTRY | C28253 (mmol/L) |
| LDH | Lactate Dehydrogenase | CHEMISTRY | C28253 (U/L) |
| PT | Prothrombin Time | COAGULATION | C49665 (s) |
| INR | INR | COAGULATION | C49672 (ratio) |
| APTT | Activated PTT | COAGULATION | C49665 (s) |

### 4.6 Specimen Type (LBSPEC)

| Term | Description |
|------|-------------|
| SERUM | Blood serum (clotted) |
| PLASMA | Blood plasma (anticoagulated) |
| URINE | Urine specimen |
| WHOLE BLOOD | Unseparated blood |
| CSF | Cerebrospinal fluid |
| SYNOVIAL FLUID | Joint fluid |
| PLEURAL FLUID | Pleural cavity fluid |

### 4.7 Unit Codelists (C71620 -- UNIT)

Common units used across SDTM Findings domains:

| Unit Code | Description | Common Use |
|-----------|-------------|------------|
| g/L | Grams per litre | Hemoglobin, protein |
| kg | Kilogramme | Weight |
| kg/m2 | Kilogramme per square metre | BMI |
| mmol/L | Millimoles per litre | Electrolytes, glucose |
| umol/L | Micromoles per litre | Creatinine, bilirubin |
| U/L | Units per litre | Enzyme activity |
| 10^9/L | Billions per litre | Blood cell counts |
| 10^12/L | Trillions per litre | Red blood cells |
| fL | Femtolitre | MCV |
| mg/L | Milligramme per litre | CRP |
| mL/min/1.73m2 | Millilitre per minute per 1.73m2 | eGFR |
| mmHg | Millimetres of mercury | Blood pressure |
| beats/min | Beats per minute | Heart rate |
| breaths/min | Breaths per minute | Respiratory rate |
| C | Degrees Celsius | Temperature |
| cm | Centimetre | Height |
| % | Percent | HbA1c, SpO2 |
| s | Seconds | Coagulation times |
| ratio | Ratio | INR, HCT |

### 4.8 Route Terminology (C66729 -- ROUTE)

| Code | Description | Common Abbreviations |
|------|-------------|---------------------|
| ORAL | By mouth | PO |
| INTRAVENOUS | Into vein | IV |
| SUBCUTANEOUS | Under skin | SC, SQ, SubQ |
| INTRAMUSCULAR | Into muscle | IM |
| TOPICAL | On skin | TOP, EXT |
| INHALATION | Into lungs | INH |
| OPHTHALMIC | Into eye | OPTH |
| TRANSDERMAL | Through skin | TD, Patch |
| SUBLINGUAL | Under tongue | SL |
| BUCCAL | Inside cheek | BUC |
| RECTAL | Into rectum | PR |
| VAGINAL | Into vagina | PV |
| INTRATHECAL | Into spinal canal | IT |
| INTRADERMAL | Into skin (dermis) | ID |
| INTRALESIONAL | Into lesion | IL |
| INTRAPERITONEAL | Into peritoneal cavity | IP |

---

## 5. Cross-Reference Mappings

### 5.1 MedDRA PT to CTCAE SOC Cross-Reference (Common Oncology AEs)

| MedDRA PT | MedDRA SOC | CTCAE v5.0 SOC |
|-----------|-----------|----------------|
| Anaemia | Blood and lymphatic system disorders | Blood and lymphatic system disorders |
| Febrile neutropenia | Blood and lymphatic system disorders | Blood and lymphatic system disorders |
| Neutropenia | Blood and lymphatic system disorders | Blood and lymphatic system disorders |
| Thrombocytopenia | Blood and lymphatic system disorders | Blood and lymphatic system disorders |
| Atrial fibrillation | Cardiac disorders | Cardiac disorders |
| Myocardial infarction | Cardiac disorders | Cardiac disorders |
| Diarrhoea | Gastrointestinal disorders | Gastrointestinal disorders |
| Nausea | Gastrointestinal disorders | Gastrointestinal disorders |
| Vomiting | Gastrointestinal disorders | Gastrointestinal disorders |
| Constipation | Gastrointestinal disorders | Gastrointestinal disorders |
| Mucositis oral | Gastrointestinal disorders | Gastrointestinal disorders |
| Pancreatitis | Gastrointestinal disorders | Gastrointestinal disorders |
| Fatigue | General disorders | General disorders and administration site conditions |
| Pyrexia | General disorders | General disorders and administration site conditions |
| ALT increased | Investigations | Investigations |
| AST increased | Investigations | Investigations |
| Blood bilirubin increased | Investigations | Investigations |
| Creatinine increased | Investigations | Investigations |
| Anorexia | Metabolism and nutrition disorders | Metabolism and nutrition disorders |
| Hyperglycaemia | Metabolism and nutrition disorders | Metabolism and nutrition disorders |
| Hypokalaemia | Metabolism and nutrition disorders | Metabolism and nutrition disorders |
| Hyponatraemia | Metabolism and nutrition disorders | Metabolism and nutrition disorders |
| Myalgia | Musculoskeletal disorders | Musculoskeletal and connective tissue disorders |
| Arthralgia | Musculoskeletal disorders | Musculoskeletal and connective tissue disorders |
| Headache | Nervous system disorders | Nervous system disorders |
| Neuropathy peripheral | Nervous system disorders | Nervous system disorders |
| Dizziness | Nervous system disorders | Nervous system disorders |
| Dysgeusia | Nervous system disorders | Nervous system disorders |
| Acute kidney injury | Renal and urinary disorders | Renal and urinary disorders |
| Dyspnoea | Respiratory disorders | Respiratory, thoracic and mediastinal disorders |
| Pneumonitis | Respiratory disorders | Respiratory, thoracic and mediastinal disorders |
| Cough | Respiratory disorders | Respiratory, thoracic and mediastinal disorders |
| Rash | Skin disorders | Skin and subcutaneous tissue disorders |
| Alopecia | Skin disorders | Skin and subcutaneous tissue disorders |
| Pruritus | Skin disorders | Skin and subcutaneous tissue disorders |
| Hand-foot skin reaction | Skin disorders | Skin and subcutaneous tissue disorders |
| Hypertension | Vascular disorders | Vascular disorders |
| Hypotension | Vascular disorders | Vascular disorders |
| Infusion related reaction | Immune system disorders | Immune system disorders |
| Anaphylaxis | Immune system disorders | Immune system disorders |
| Cytokine release syndrome | Immune system disorders | Immune system disorders |

### 5.2 LOINC Code to CDISC LBTESTCD Mapping

| LOINC Code | LOINC Component | LBTESTCD | LBTEST |
|-----------|----------------|----------|--------|
| 1742-6 | Alanine aminotransferase | ALT | Alanine Aminotransferase |
| 1920-8 | Aspartate aminotransferase | AST | Aspartate Aminotransferase |
| 6768-6 | Alkaline phosphatase | ALP | Alkaline Phosphatase |
| 1975-2 | Bilirubin.total | BILI | Bilirubin |
| 1968-7 | Bilirubin.direct | BILD | Bilirubin Direct |
| 2324-2 | Gamma glutamyl transferase | GGT | Gamma-Glutamyl Transferase |
| 1751-7 | Albumin | ALB | Albumin |
| 2885-2 | Protein | TP | Total Protein |
| 2345-7 | Glucose | GLUC | Glucose |
| 1558-6 | Glucose^fasting | GLUF | Glucose Fasting |
| 3094-0 | Urea nitrogen | BUN | Blood Urea Nitrogen |
| 2160-0 | Creatinine | CREAT | Creatinine |
| 2951-2 | Sodium | SODIUM | Sodium |
| 2823-3 | Potassium | POTAS | Potassium |
| 2075-0 | Chloride | CHLOR | Chloride |
| 2028-9 | Carbon dioxide | CO2 | Carbon Dioxide |
| 17861-6 | Calcium | CALC | Calcium |
| 6690-2 | Leukocytes | WBC | Leukocytes |
| 789-8 | Erythrocytes | RBC | Erythrocytes |
| 718-7 | Hemoglobin | HGB | Hemoglobin |
| 4544-3 | Hematocrit | HCT | Hematocrit |
| 777-3 | Platelets | PLAT | Platelets |
| 751-8 | Neutrophils | NEUT | Neutrophils |
| 731-0 | Lymphocytes | LYMPH | Lymphocytes |
| 742-7 | Monocytes | MONO | Monocytes |
| 711-2 | Eosinophils | EO | Eosinophils |
| 704-7 | Basophils | BASO | Basophils |
| 787-2 | Erythrocyte mean corpuscular volume | MCV | Mean Corpuscular Volume |
| 785-6 | Erythrocyte mean corpuscular hemoglobin | MCH | Mean Corpuscular Hemoglobin |
| 4548-4 | Hemoglobin A1c | HBA1C | Hemoglobin A1c |
| 3016-3 | Thyrotropin | TSH | Thyrotropin |
| 62238-1 | Glomerular filtration rate | EGFR | Estimated GFR |
| 2157-6 | Creatine kinase | CK | Creatine Kinase |
| 1988-5 | C reactive protein | CRP | C-Reactive Protein |
| 2093-3 | Cholesterol | CHOL | Cholesterol |
| 13457-7 | Cholesterol in LDL | LDL | LDL Cholesterol |
| 2085-9 | Cholesterol in HDL | HDL | HDL Cholesterol |
| 2571-8 | Triglyceride | TRIG | Triglycerides |
| 2532-0 | Lactate dehydrogenase | LDH | Lactate Dehydrogenase |
| 5902-2 | Prothrombin time | PT | Prothrombin Time |
| 6301-6 | INR | INR | INR |
| 3173-2 | Activated partial thromboplastin time | APTT | Activated PTT |
| 3255-7 | Fibrinogen | FIB | Fibrinogen |
| 1798-8 | Amylase | AMYL | Amylase |
| 3040-3 | Lipase | LIP | Lipase |
| 3084-1 | Urate | URIC | Uric Acid |
| 2777-1 | Phosphate | PHOS | Phosphate |
| 19123-9 | Magnesium | MG | Magnesium |
| 32623-1 | Platelet mean volume | MPV | Mean Platelet Volume |
| 788-0 | Erythrocyte distribution width | RDW | Red Cell Distribution Width |

### 5.3 ATC Codes with WHO Defined Daily Doses (DDD)

The DDD is the assumed average maintenance dose per day for a drug used for its main indication in adults. Source: WHO ATC/DDD Index 2025.

| INN | ATC Code | DDD | Unit | Administration Route |
|-----|----------|-----|------|---------------------|
| Metformin | A10BA02 | 2.0 | g | Oral |
| Insulin glargine | A10AE04 | 40 | U | Subcutaneous |
| Empagliflozin | A10BK03 | 10 | mg | Oral |
| Sitagliptin | A10BH01 | 100 | mg | Oral |
| Atorvastatin | C10AA05 | 20 | mg | Oral |
| Rosuvastatin | C10AA07 | 10 | mg | Oral |
| Lisinopril | C09AA03 | 10 | mg | Oral |
| Amlodipine | C08CA01 | 5 | mg | Oral |
| Metoprolol | C07AB02 | 0.15 | g | Oral |
| Bisoprolol | C07AB07 | 10 | mg | Oral |
| Hydrochlorothiazide | C03AA03 | 25 | mg | Oral |
| Furosemide | C03CA01 | 40 | mg | Oral/IV |
| Warfarin | B01AA03 | 5 | mg | Oral |
| Apixaban | B01AF02 | 10 | mg | Oral |
| Clopidogrel | B01AC04 | 75 | mg | Oral |
| Omeprazole | A02BC01 | 20 | mg | Oral |
| Ondansetron | A04AA01 | 16 | mg | Oral/IV |
| Paracetamol | N02BE01 | 3 | g | Oral/Rectal |
| Ibuprofen | M01AE01 | 1.2 | g | Oral |
| Morphine | N02AA01 | 0.1 | g | Oral |
| Ceftriaxone | J01DD04 | 2 | g | Parenteral |
| Amoxicillin | J01CA04 | 1 | g | Oral |
| Vancomycin | J01XA01 | 2 | g | Parenteral |
| Ciprofloxacin | J01MA02 | 1 | g | Oral |
| Sertraline | N06AB06 | 50 | mg | Oral |
| Salbutamol | R03AC02 | 0.8 | mg | Inhalation |
| Levothyroxine | H03AA01 | 0.1 | mg | Oral |
| Allopurinol | M04AA01 | 0.4 | g | Oral |
| Prednisolone | H02AB06 | 10 | mg | Oral |
| Dexamethasone | H02AB02 | 1.5 | mg | Oral |

---

## 6. ISO Standards

### 6.1 ISO 3166-1 Alpha-3 Country Codes (Common Trial Countries)

| Alpha-3 | Country | Region | Typical Trial Volume |
|---------|---------|--------|---------------------|
| USA | United States of America | North America | High |
| CAN | Canada | North America | Medium |
| GBR | United Kingdom | Europe | High |
| DEU | Germany | Europe | High |
| FRA | France | Europe | High |
| ESP | Spain | Europe | High |
| ITA | Italy | Europe | High |
| POL | Poland | Europe | Medium-High |
| CZE | Czechia | Europe | Medium |
| HUN | Hungary | Europe | Medium |
| NLD | Netherlands | Europe | Medium |
| BEL | Belgium | Europe | Medium |
| SWE | Sweden | Europe | Medium |
| DNK | Denmark | Europe | Medium |
| AUT | Austria | Europe | Medium |
| CHE | Switzerland | Europe | Medium |
| JPN | Japan | Asia-Pacific | High |
| CHN | China | Asia-Pacific | High |
| KOR | South Korea | Asia-Pacific | Medium-High |
| AUS | Australia | Asia-Pacific | Medium-High |
| IND | India | Asia-Pacific | Medium |
| TWN | Taiwan | Asia-Pacific | Medium |
| RUS | Russian Federation | Europe/Asia | Medium |
| BRA | Brazil | Latin America | Medium |
| MEX | Mexico | Latin America | Medium |
| ARG | Argentina | Latin America | Medium |
| ZAF | South Africa | Africa | Medium |
| NZL | New Zealand | Oceania | Low-Medium |
| ISR | Israel | Middle East | Low-Medium |
| SGP | Singapore | Asia-Pacific | Low-Medium |
| UKR | Ukraine | Europe | Medium |

### 6.2 ISO 8601 Date/Time Format Specification

All date and time values in TrialSim datasets use ISO 8601 format per CDISC SDTM Implementation Guide requirements.

| Format | Pattern | Example | Use Case |
|--------|---------|---------|----------|
| Date (basic) | `YYYYMMDD` | `20240315` | Stored format (optional) |
| Date (extended) | `YYYY-MM-DD` | `2024-03-15` | Display/transfer (preferred) |
| Date-Time (basic) | `YYYYMMDDThhmmss` | `20240315T143000` | Timestamp (optional) |
| Date-Time (extended) | `YYYY-MM-DDThh:mm:ss` | `2024-03-15T14:30:00` | Timestamp (preferred) |
| Date-Time with timezone | `YYYY-MM-DDThh:mm:ss+hh:mm` | `2024-03-15T14:30:00-05:00` | Multi-region studies |
| Duration | `PnYnMnDTnHnMnS` | `P3M` (3 months) | Exposure duration |
| Partial date (month) | `YYYY-MM` | `2024-03` | Approximate dates |
| Partial date (year) | `YYYY` | `2024` | Birth year only |
| Interval | `YYYY-MM-DD/YYYY-MM-DD` | `2024-03-15/2024-06-15` | Study periods |

#### SDTM Variable Application

| SDTM Variable(s) | Format | Examples |
|-----------------|--------|----------|
| RFSTDTC, RFENDTC | Date or Date-Time (extended preferred) | `2024-03-15` |
| BRTHDTC (birth date) | Date | `1968-07-22` |
| AESTDTC, AEENDTC | Date or Date-Time | `2024-05-10` or `2024-05-10T09:30:00` |
| LBDTC (lab collection) | Date or Date-Time | `2024-04-01` or `2024-04-01T08:15:00` |
| VSDTC (vital signs) | Date or Date-Time | `2024-03-15` |
| DTHDTC (death) | Date or Date-Time | `2024-11-22` |
| EXSTDTC, EXENDTC | Date-Time (preferred for PK) | `2024-03-15T08:00:00` |
| RFXSTDTC, RFXENDTC | Date-Time | `2024-03-15T08:00:00` |

#### ISO 8601 Duration Pattern Notes

- `P` = Period designator (precedes all durations)
- `Y`, `M`, `D` = Year, Month, Day
- `T` = Time designator (separates date and time parts)
- `H`, `M`, `S` = Hour, Minute, Second
- Examples: `P1Y` (1 year), `P6M` (6 months), `P14D` (14 days), `PT8H` (8 hours), `P1Y6M14DT8H30M` (1yr 6mo 14d 8h 30m)

---

## References

- **MedDRA**: [meddra.org](https://www.meddra.org/) -- MedDRA v27.0 (March 2024), ICH M1
- **LOINC**: [loinc.org](https://loinc.org/) -- LOINC v2.78 (February 2024), Regenstrief Institute
- **ATC/DDD**: [whocc.no/atc_ddd_index](https://www.whocc.no/atc_ddd_index/) -- WHO ATC/DDD Index 2025
- **CDISC CT**: [cdis c.org](https://www.cdisc.org/standards/terminology) -- NCI EVS CDISC Controlled Terminology, quarterly releases
- **ISO 3166**: [iso.org](https://www.iso.org/iso-3166-country-codes.html) -- ISO 3166-1:2020
- **ISO 8601**: [iso.org](https://www.iso.org/standard/70908.html) -- ISO 8601-1:2019, ISO 8601-2:2019

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12 | Initial code systems reference: MedDRA, LOINC, ATC, CDISC CT, ISO |
