---
name: phase1-dose-escalation
author: "Yinan Chen (陈翼男)"
description: |
  Generate Phase 1 dose escalation trial data including first-in-human (FIH) studies, 
  MTD determination, DLT assessment, and PK/PD sampling. Supports 3+3, BOIN, CRM, 
  mTPI designs. Triggers: "phase 1", "dose escalation", "first-in-human", "FIH", 
  "MTD", "DLT", "SAD", "MAD", "3+3", "BOIN", "CRM".
---

# Phase 1 Dose Escalation Trials

Generate realistic Phase 1 clinical trial data for dose escalation studies, including first-in-human (FIH) trials, maximum tolerated dose (MTD) determination, and pharmacokinetic characterization.

---

## For Claude

This is a **trial phase skill** for generating Phase 1 dose escalation data. Apply when users request first-in-human studies, dose-finding trials, or early clinical development data.

**Always apply this skill when you see:**
- Phase 1, Phase I, or first-in-human (FIH) trial requests
- Dose escalation, MTD, or DLT references
- SAD (single ascending dose) or MAD (multiple ascending dose)
- Design names: 3+3, BOIN, CRM, mTPI, Keyboard
- PK sampling or pharmacokinetic study requests
- Starting dose or dose-toxicity discussions

**Combine with:**
- Therapeutic area skills (oncology.md, cns.md) for disease-specific patterns
- SDTM domain skills for regulatory-compliant data structures

---

## Phase 1 Trial Characteristics

| Aspect | Typical Values |
|--------|----------------|
| **Sample Size** | 20-80 subjects (escalation), up to 100+ with expansion |
| **Duration** | 6-18 months |
| **Sites** | 1-5 specialized centers |
| **Population** | Healthy volunteers or patients (indication-dependent) |
| **Primary Objective** | Safety, tolerability, MTD/RP2D determination |
| **Secondary Objectives** | PK characterization, preliminary PD/efficacy |

### Population Selection

| Therapeutic Area | Population | Rationale |
|-----------------|------------|-----------|
| Oncology | Patients with advanced/refractory disease | Ethical (can't expose healthy to cytotoxic) |
| CNS | Healthy volunteers → patients | Start safe, then disease population |
| Cardiovascular | Healthy volunteers | Safe initial assessment |
| Infectious Disease | Healthy volunteers | Vaccine/antimicrobial safety |
| Rare Disease | Patients | Limited population availability |

---

## Study Design Components

### Single Ascending Dose (SAD)

Sequential cohorts receive single doses of increasing strength.

```
Cohort Structure:
┌─────────┬────────────┬─────────────┬─────────────────┐
│ Cohort  │ Dose Level │ N (Active)  │ N (Placebo)     │
├─────────┼────────────┼─────────────┼─────────────────┤
│ 1       │ Starting   │ 6           │ 2               │
│ 2       │ Level 2    │ 6           │ 2               │
│ 3       │ Level 3    │ 6           │ 2               │
│ ...     │ ...        │ ...         │ ...             │
│ N       │ MTD        │ 6           │ 2               │
└─────────┴────────────┴─────────────┴─────────────────┘
```

**Key Features:**
- Sentinel dosing: First 1-2 subjects dosed, safety review before rest of cohort
- Intensive PK sampling over 24-72 hours
- Safety follow-up period (typically 7-14 days) before next cohort
- Placebo subjects for blinding (typically 6:2 or 3:1 ratio)

### ⚠️ Critical Design Principle: Blinding in Phase I

**Phase I FIH 试验（非肿瘤适应症）必须采用随机、双盲、安慰剂对照设计。** 这不是可选项，而是 ICH E8(R1) 和 EMA FIH Guideline (2017) 的明确要求。

**为什么 Phase I 需要双盲？**

```
Phase I 的首要终点 = 安全性/耐受性（非疗效）

安全性评估包含大量主观判断：
  ├── 头痛、恶心、疲劳、头晕 → 受试者主观报告
  ├── DLT 判定（是否为药物相关）→ 研究者主观判断
  ├── AE 归因（RELATED vs NOT RELATED）→ 双方主观判断
  └── 输液反应分级 → 研究者主观评分

如果开放标签的后果：
  ├── 受试者知道自己用活性药 → 过度报告轻度 AE
  ├── 研究者知道分组 → 过度将 AE 归因于研究药物
  └── 结果：DLT 率虚高 → MTD 错误下移 → 错过有效剂量
```

**已上市 AD 药物 Phase I 全为双盲设计：**

| 药物 | Phase I | NCT |
|------|---------|-----|
| Lecanemab | 随机、双盲、安慰剂对照 SAD+MAD | NCT01267370 |
| Donanemab | 随机、双盲、安慰剂对照 SAD+MAD | NCT01837641 |
| Aducanumab | 随机、双盲、安慰剂对照 (PRIME) | NCT01677572 |
| Gantenerumab | 随机、双盲、安慰剂对照 | — |
| Solanezumab | 随机、双盲、安慰剂对照 | — |

**盲态管理的关键机制：SMC 非盲态 + 研究者/受试者盲态**

```
┌─────────────────────────────────────────────────────┐
│  SMC（安全性监查委员会）── 唯一非盲态角色              │
│  ├── 审查每个队列的非盲态安全性数据                    │
│  ├── 做出剂量递增/暂停/终止决策                        │
│  └── 不与研究者/受试者沟通分组信息                     │
├─────────────────────────────────────────────────────┤
│  研究者 + 受试者 ── 全程盲态                          │
│  ├── 评估 AE 严重程度和归因时不带预设                   │
│  ├── 报告主观症状不受分组信息干扰                      │
│  └── DLT 判定基于客观标准（CTCAE v5.0）                │
└─────────────────────────────────────────────────────┘
```

**例外：何时 Phase I 不使用双盲？**

| 情景 | 设计 | 原因 |
|------|------|------|
| **肿瘤 I 期**（细胞毒性药物） | 开放标签 | 伦理（不能让晚期癌症患者用安慰剂）；毒性客观可测 |
| **基因治疗 / CAR-T** | 开放标签 | 侵入性操作、无法做安慰剂对照 |
| **罕见病患者** | 开放标签 | 患者数量极有限，无法做有意义的随机化 |
| **CNS / 心血管 / 代谢 FIH** | **随机双盲安慰剂对照** ✅ | 健康志愿者、主观安全性终点、足够样本量 |

### Multiple Ascending Dose (MAD)

Sequential cohorts receive multiple doses over days/weeks.

```
Cohort Structure:
┌─────────┬────────────┬─────────────┬─────────────────┬─────────────┐
│ Cohort  │ Dose Level │ Frequency   │ Duration        │ N (Act:Pbo) │
├─────────┼────────────┼─────────────┼─────────────────┼─────────────┤
│ 1       │ Level 1    │ QD          │ 7 days          │ 8:2         │
│ 2       │ Level 2    │ QD          │ 7 days          │ 8:2         │
│ 3       │ Level 3    │ QD          │ 14 days         │ 8:2         │
│ 4       │ Level 4    │ BID         │ 14 days         │ 8:2         │
└─────────┴────────────┴─────────────┴─────────────────┴─────────────┘
```

**Key Features:**
- PK sampling on Day 1 and at steady state
- Safety labs at baseline, mid-treatment, end of treatment
- Trough samples for accumulation assessment
- **For monoclonal antibodies (mAb)**: Always include at least one Q2W and one Q4W cohort for frequency comparison — this is critical for Phase II/III dose schedule selection

### MAD Frequency Comparison (v2.3 新增 — mAb 必需)

**对于治疗性单克隆抗体 (mAb)，MAD 设计必须包含给药频率的对比。**

```
MAD 频率对比设计:
┌─────────┬────────────┬─────────────┬─────────────────┬─────────────┐
│ Cohort  │ Dose Level │ Frequency   │ Duration        │ 目的         │
├─────────┼────────────┼─────────────┼─────────────────┼─────────────┤
│ M1      │ 低剂量      │ Q2W         │ 8 周 (5 次给药) │ 较频繁给药   │
│ M2      │ 中剂量      │ Q2W         │ 8 周 (5 次给药) │ 剂量递增     │
│ M3      │ 高剂量      │ Q2W         │ 8 周 (5 次给药) │ 高剂量       │
│ M4      │ 高剂量      │ Q4W         │ 12 周 (4 次给药)│ 频率对比 ⭐   │
└─────────┴────────────┴─────────────┴─────────────────┴─────────────┘

M4 队列的核心价值:
  - 回答关键问题: "Q4W 给药是否与 Q2W 有相当的靶点结合和 PD 效应？"
  - 如果 M4 的稳态谷浓度 (Ctrough) ≥ 10× IC50 且 CSF 靶点占位 ≥ 90% 
    → 支持 Phase II/III 使用更方便的 Q4W 方案
  - 参考先例: Lecanemab (Q2W), Donanemab (Q4W) — 两者均通过 MAD 频率对比确定
```

### Phase I → Phase II 剂量单位 PK 桥接 (v2.3 新增)

**当 Phase I 使用 mg/kg (按体重) 给药而 Phase II/III 计划使用固定剂量 (flat dosing) 时，必须提供 PK 桥接依据。**

```
PK 桥接决策流程:
  
  Phase I SAD/MAD 完成 (mg/kg 给药)
       │
       ▼
  群体 PK (PopPK) 建模
       │
       ├── 检查: 体重对 CL 的影响程度
       │    ├── θ_WT < 0.5 (弱相关) → 固定剂量可行 ✅
       │    └── θ_WT > 0.5 (中/强相关) → 继续 mg/kg 或按体重分层固定剂量
       │
       ├── 检查: 个体间 PK 变异性 (CV%)
       │    ├── CV% < 30% → 固定剂量可行 ✅
       │    └── CV% > 50% → 需要考虑治疗药物监测 (TDM) 或按体重给药
       │
       └── 最终建议:
            ├── 如果两项均通过 → 切换到固定剂量 (mg Q4W)
            │   示例: 3 mg/kg × 70 kg ≈ 210 mg → 取整为 200 mg 或 180 mg
            └── 如果任一项不通过 → 保留 mg/kg 或分层固定剂量
```

**参考先例**: 所有已批准 AD 单抗 (Lecanemab, Donanemab, Aducanumab) 均在 Phase I 中使用 mg/kg，在 Phase II/III 中切换到固定剂量。每次切换背后都有群体 PK 数据支撑。

---

## Dose Escalation Designs

### ⭐ 推荐首选: BOIN 设计 (Bayesian Optimal Interval) — v2.3

**对于非肿瘤适应症的单克隆抗体 FIH 试验，BOIN 设计是推荐的默认剂量递增方法。** 改良 3+3 仅作为备选方案（适用于统计资源有限的场景）。

| 特性 | BOIN | 3+3 |
|------|------|-----|
| **统计效率** | 高——正确 MTD 选择概率高 ~10-15% | 低 |
| **决策透明性** | 预定义边界 (λe, λd)，决策规则明确 | 固定规则 |
| **超剂量风险** | 低 (~8-12%) | 较高 |
| **MTD 组受试者数** | 平均 ~12-18 人 | 通常仅 6 人 |
| **监管接受度** | FDA/EMA 明确接受 | 传统设计 |
| **实施复杂度** | 需要统计师支持 | 简单 |
| **适用场景** | **所有非肿瘤 FIH (首选)** | 统计资源有限 |

```
BOIN 参数推荐 (mAb, CNS 适应症):
  - 目标 DLT 率 φ = 0.25 (非肿瘤 CNS 药物标准)
  - 剂量递增边界: λe = 0.659 (递增), λd = 0.903 (递减) — Yuan et al. 2016, JCO
  - 每队列最小 6 名活性受试者
  - 决策规则:
      DLT ≤ λe × N → 递增
      DLT ≥ λd × N → 递减
      其他 → 维持/扩大至 12 人
  - 最大每剂量组样本量 = 12
  - 推荐剂量水平数: 5-7 个
```

**文献支持**: Yuan Y et al. BOIN: A Novel Bayesian Optimal Interval Design for Phase I Clinical Trials. *J Clin Oncol*. 2016;34(3):259-267.

### 备选: 3+3 Design (Rule-Based)

The traditional algorithm-based design using fixed rules.

**Decision Rules:**
```
At current dose level:
├── 0/3 DLTs → Escalate to next dose
├── 1/3 DLTs → Enroll 3 more subjects
│   ├── 1/6 DLTs → Escalate to next dose
│   └── ≥2/6 DLTs → MTD exceeded, de-escalate
├── ≥2/3 DLTs → MTD exceeded, de-escalate
└── MTD = Highest dose with <33% DLT rate
```

**Advantages:** Simple, transparent, no statistical expertise required
**Limitations:** Conservative, many patients at subtherapeutic doses, imprecise MTD

### BOIN Design (Model-Assisted)

Bayesian Optimal Interval design using pre-calculated boundaries.

**Parameters:**
- Target DLT rate (φ): typically 0.25-0.33
- Escalation boundary (λe): typically 0.6 × φ
- De-escalation boundary (λd): typically 1.4 × φ

**Decision Rules (for φ = 0.25):**
| Observed DLT Rate | Decision |
|-------------------|----------|
| ≤ 0.157 (λe) | Escalate |
| 0.157 < rate < 0.359 | Stay |
| ≥ 0.359 (λd) | De-escalate |

**Pre-tabulated Decisions (φ = 0.25, cohort = 3):**
| DLTs/N | 0/3 | 1/3 | 2/3 | 3/3 |
|--------|-----|-----|-----|-----|
| Decision | E | S | D | D |

E = Escalate, S = Stay, D = De-escalate

**Advantages:** Easy to implement, good performance, transparent
**Limitations:** Requires pre-specification of boundaries

### Continual Reassessment Method (CRM, Model-Based)

Bayesian model-based design that updates dose-toxicity curve after each cohort.

**Key Components:**
- Dose-toxicity model: P(DLT|d) = d^exp(β)
- Prior distribution on β
- Target DLT probability (typically 0.25-0.33)
- After each cohort: Update posterior, select dose closest to target

**Advantages:** Most accurate MTD identification, uses all accumulated data
**Limitations:** Requires statistical expertise, potential for irrational decisions

### mTPI-2 / Keyboard Design (Model-Assisted)

Uses toxicity probability intervals to guide decisions.

**Intervals:**
- Underdosing: [0, φ - ε1]
- Target: [φ - ε1, φ + ε2]  
- Overdosing: [φ + ε2, 1]

**Decision:** Based on which interval has highest posterior probability

---

## Starting Dose Determination

### NOAEL-Based Approach (FDA Guidance)

```
Step 1: Identify NOAEL from most appropriate animal species
Step 2: Convert to Human Equivalent Dose (HED)
        HED = Animal Dose × (Animal Km / Human Km)
        
        Km values by species:
        ┌──────────┬────────┐
        │ Species  │ Km     │
        ├──────────┼────────┤
        │ Mouse    │ 3      │
        │ Rat      │ 6      │
        │ Rabbit   │ 12     │
        │ Monkey   │ 12     │
        │ Dog      │ 20     │
        │ Human    │ 37     │
        └──────────┴────────┘

Step 3: Apply safety factor (typically 10x)
        MRSD = HED / 10
```

### MABEL-Based Approach (EMA Guidance)

For high-risk biologics and immunomodulatory agents:

```
MABEL = Minimum Anticipated Biological Effect Level

Based on:
- Receptor occupancy (typically 10-20% occupancy)
- EC10 from in vitro potency assays
- Lowest pharmacologically active dose in animals
- PK/PD modeling predictions
```

---

## Dose-Limiting Toxicity (DLT) Definitions

### Standard DLT Criteria

| Category | DLT Definition |
|----------|----------------|
| **Hematologic** | Grade 4 neutropenia >7 days, febrile neutropenia, Grade 4 thrombocytopenia, Grade 3 thrombocytopenia with bleeding |
| **Non-Hematologic** | Grade 3-4 toxicity (except nausea/vomiting controlled with antiemetics, alopecia, fatigue <7 days) |
| **Hepatic** | Grade 3 AST/ALT elevation, Grade 2 bilirubin with transaminases |
| **Cardiac** | QTc prolongation >500ms or >60ms increase from baseline |
| **Dose Modifications** | Inability to receive ≥75% of planned doses due to toxicity |

### DLT Evaluation Window

| Study Type | DLT Window | Rationale |
|------------|------------|-----------|
| Cytotoxic chemotherapy | Cycle 1 (21-28 days) | Acute toxicity assessment |
| Targeted therapy | 28 days | May have delayed onset |
| Immunotherapy | 6-8 weeks | Immune-related AEs delayed |
| Cell therapy | 28 days (CRS), 8 weeks (neurotox) | Different toxicity kinetics |

---

## Pharmacokinetic Sampling

### Intensive PK Schedule (SAD)

| Time Point | Sample Type | Purpose |
|------------|-------------|---------|
| Pre-dose | Baseline | Confirm no drug present |
| 0.25, 0.5, 1h | Absorption | Tmax determination |
| 2, 4, 6, 8h | Distribution | Cmax, early elimination |
| 12, 24h | Elimination | Terminal phase |
| 48, 72h | Extended | Long half-life drugs |
| 96, 168h | Optional | Very long half-life |

### Sparse PK Schedule (MAD)

| Day | Time Points | Purpose |
|-----|-------------|---------|
| Day 1 | Pre, 1, 2, 4, 8, 12h | First dose characterization |
| Days 2-6 | Pre-dose (trough) | Accumulation assessment |
| Day 7 | Full profile | Steady-state characterization |
| Day 14 | Pre-dose | Extended steady-state |

### PK Parameters Generated

| Parameter | Description | Units |
|-----------|-------------|-------|
| Cmax | Maximum concentration | ng/mL |
| Tmax | Time to maximum | hours |
| AUC0-t | Area under curve (0 to last) | ng·h/mL |
| AUC0-inf | Area under curve (0 to infinity) | ng·h/mL |
| t½ | Terminal half-life | hours |
| CL/F | Apparent clearance | L/h |
| Vd/F | Apparent volume of distribution | L |
| Rac | Accumulation ratio | dimensionless |

---

## Expansion Cohorts

After MTD/RP2D determination, expansion cohorts provide additional data.

### Expansion Cohort Objectives

| Objective | Cohort Size | Design |
|-----------|-------------|--------|
| Additional safety at RP2D | 10-20 | Single-arm |
| Preliminary efficacy signal | 20-40 | Simon's two-stage |
| Biomarker development | 15-30 | Enriched population |
| Alternative schedule | 10-20 | Different dosing regimen |
| Combination therapy | 15-30 | With standard of care |
| Special populations | 10-20 | Renal/hepatic impairment |

---

## Generation Patterns

### Pattern 1: Standard 3+3 Escalation

```json
{
  "study_type": "phase1_dose_escalation",
  "design": "3+3",
  "dose_levels": [
    {"level": 1, "dose": 10, "unit": "mg", "n_enrolled": 3, "n_dlt": 0, "decision": "escalate"},
    {"level": 2, "dose": 25, "unit": "mg", "n_enrolled": 3, "n_dlt": 0, "decision": "escalate"},
    {"level": 3, "dose": 50, "unit": "mg", "n_enrolled": 3, "n_dlt": 1, "decision": "expand"},
    {"level": 3, "dose": 50, "unit": "mg", "n_enrolled": 6, "n_dlt": 1, "decision": "escalate"},
    {"level": 4, "dose": 100, "unit": "mg", "n_enrolled": 3, "n_dlt": 2, "decision": "mtd_exceeded"},
    {"level": 3, "dose": 50, "unit": "mg", "mtd": true}
  ],
  "total_enrolled": 18,
  "mtd_dose": 50,
  "mtd_unit": "mg"
}
```

### Pattern 2: BOIN Design with PK

```json
{
  "study_type": "phase1_fih",
  "design": "BOIN",
  "target_dlt_rate": 0.25,
  "boundaries": {
    "escalation": 0.157,
    "de_escalation": 0.359
  },
  "dose_levels": [
    {
      "level": 1,
      "dose": 0.1,
      "unit": "mg/kg",
      "n_enrolled": 3,
      "n_dlt": 0,
      "observed_dlt_rate": 0.0,
      "decision": "escalate",
      "pk_summary": {
        "cmax_mean": 45.2,
        "cmax_cv": 32,
        "auc_mean": 312,
        "auc_cv": 28,
        "half_life_mean": 8.5
      }
    }
  ]
}
```

### Pattern 3: Oncology FIH with Expansion

```json
{
  "study_type": "phase1_oncology",
  "design": "accelerated_titration_3+3",
  "population": "advanced_solid_tumors",
  "parts": {
    "dose_escalation": {
      "n_subjects": 24,
      "n_dose_levels": 8,
      "mtd_dose": "400mg",
      "rp2d": "400mg QD"
    },
    "dose_expansion": {
      "cohorts": [
        {"name": "NSCLC", "n": 20, "orr": 0.15},
        {"name": "Melanoma", "n": 20, "orr": 0.25},
        {"name": "CRC", "n": 20, "orr": 0.10}
      ]
    }
  }
}
```

---

## Examples

### Example 1: First-in-Human SAD/MAD Study

**Request:** "Generate a first-in-human Phase 1 study for a novel oral small molecule"

```json
{
  "study": {
    "study_id": "ABC-101",
    "title": "First-in-Human Single and Multiple Ascending Dose Study of ABC-001",
    "phase": "1",
    "design": "randomized_placebo_controlled",
    "population": "healthy_volunteers"
  },
  "part_a_sad": {
    "design": "3+3_modified",
    "cohorts": [
      {
        "cohort_id": "SAD-1",
        "dose_mg": 5,
        "subjects": [
          {"usubjid": "ABC101-001", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-002", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-003", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-004", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-005", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-006", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-007", "arm": "placebo", "dlt": false},
          {"usubjid": "ABC101-008", "arm": "placebo", "dlt": false}
        ],
        "pk_parameters": {
          "cmax_ng_ml": {"mean": 12.3, "sd": 3.2},
          "tmax_h": {"median": 1.5, "range": [1.0, 3.0]},
          "auc_0_inf_ng_h_ml": {"mean": 98.5, "sd": 22.1},
          "t_half_h": {"mean": 6.2, "sd": 1.1}
        },
        "decision": "escalate"
      },
      {
        "cohort_id": "SAD-2",
        "dose_mg": 15,
        "subjects": [
          {"usubjid": "ABC101-009", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-010", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-011", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-012", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-013", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-014", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-015", "arm": "placebo", "dlt": false},
          {"usubjid": "ABC101-016", "arm": "placebo", "dlt": false}
        ],
        "pk_parameters": {
          "cmax_ng_ml": {"mean": 35.8, "sd": 8.9},
          "tmax_h": {"median": 2.0, "range": [1.0, 4.0]},
          "auc_0_inf_ng_h_ml": {"mean": 295.2, "sd": 65.3},
          "t_half_h": {"mean": 6.5, "sd": 1.3}
        },
        "decision": "escalate"
      },
      {
        "cohort_id": "SAD-3",
        "dose_mg": 50,
        "subjects": [
          {"usubjid": "ABC101-017", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-018", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-019", "arm": "active", "dlt": true, "dlt_term": "Grade 3 headache"},
          {"usubjid": "ABC101-020", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-021", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-022", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-023", "arm": "placebo", "dlt": false},
          {"usubjid": "ABC101-024", "arm": "placebo", "dlt": false}
        ],
        "pk_parameters": {
          "cmax_ng_ml": {"mean": 118.5, "sd": 28.2},
          "tmax_h": {"median": 2.0, "range": [1.5, 3.5]},
          "auc_0_inf_ng_h_ml": {"mean": 985.3, "sd": 198.6},
          "t_half_h": {"mean": 6.8, "sd": 1.5}
        },
        "decision": "escalate"
      },
      {
        "cohort_id": "SAD-4",
        "dose_mg": 100,
        "subjects": [
          {"usubjid": "ABC101-025", "arm": "active", "dlt": false},
          {"usubjid": "ABC101-026", "arm": "active", "dlt": true, "dlt_term": "Grade 3 nausea"},
          {"usubjid": "ABC101-027", "arm": "active", "dlt": true, "dlt_term": "Grade 3 vomiting"},
          {"usubjid": "ABC101-028", "arm": "placebo", "dlt": false}
        ],
        "pk_parameters": {
          "cmax_ng_ml": {"mean": 245.2, "sd": 52.1},
          "tmax_h": {"median": 2.5, "range": [2.0, 4.0]},
          "auc_0_inf_ng_h_ml": {"mean": 2012.5, "sd": 385.2}
        },
        "decision": "mtd_exceeded"
      }
    ],
    "mtd": {
      "dose_mg": 50,
      "rationale": "100mg exceeded MTD with 2/3 DLTs; 50mg had 1/6 DLTs (16.7%)"
    }
  },
  "part_b_mad": {
    "design": "sequential_cohort",
    "dosing_duration_days": 14,
    "cohorts": [
      {
        "cohort_id": "MAD-1",
        "dose_mg": 15,
        "frequency": "QD",
        "n_active": 8,
        "n_placebo": 2,
        "n_completers": 10,
        "dlts": 0,
        "steady_state_pk": {
          "cmax_ss_ng_ml": {"mean": 42.1, "sd": 10.5},
          "cmin_ss_ng_ml": {"mean": 8.2, "sd": 2.1},
          "accumulation_ratio": 1.35
        }
      },
      {
        "cohort_id": "MAD-2",
        "dose_mg": 30,
        "frequency": "QD",
        "n_active": 8,
        "n_placebo": 2,
        "n_completers": 9,
        "dlts": 1,
        "steady_state_pk": {
          "cmax_ss_ng_ml": {"mean": 85.3, "sd": 19.2},
          "cmin_ss_ng_ml": {"mean": 16.8, "sd": 4.2},
          "accumulation_ratio": 1.42
        }
      }
    ],
    "rp2d": {
      "dose": "30mg QD",
      "rationale": "Acceptable safety profile, PK supports once daily dosing"
    }
  },
  "safety_summary": {
    "total_enrolled": 42,
    "total_aes": 68,
    "treatment_related_aes": 45,
    "serious_aes": 0,
    "discontinuations_due_to_ae": 2,
    "most_common_aes": [
      {"term": "Headache", "n": 18, "percent": 42.9},
      {"term": "Nausea", "n": 12, "percent": 28.6},
      {"term": "Fatigue", "n": 8, "percent": 19.0}
    ]
  }
}
```



### Example 2: BOIN Oncology Dose Escalation

**Request:** "Generate Phase 1 oncology trial using BOIN design for a novel kinase inhibitor"

```json
{
  "study": {
    "study_id": "KI-001",
    "title": "Phase 1 Dose Escalation Study of KI-001 in Advanced Solid Tumors",
    "phase": "1",
    "design": "BOIN",
    "population": "advanced_solid_tumors_refractory"
  },
  "design_parameters": {
    "target_dlt_rate": 0.30,
    "escalation_boundary": 0.197,
    "de_escalation_boundary": 0.419,
    "cohort_size": 3,
    "max_sample_size": 36
  },
  "dose_escalation": {
    "dose_levels": [
      {
        "level": 1,
        "dose": "50mg BID",
        "cohort": 1,
        "n_treated": 3,
        "n_dlt": 0,
        "observed_rate": 0.00,
        "decision": "escalate",
        "dlt_details": []
      },
      {
        "level": 2,
        "dose": "100mg BID",
        "cohort": 2,
        "n_treated": 3,
        "n_dlt": 0,
        "observed_rate": 0.00,
        "decision": "escalate",
        "dlt_details": []
      },
      {
        "level": 3,
        "dose": "200mg BID",
        "cohort": 3,
        "n_treated": 3,
        "n_dlt": 1,
        "observed_rate": 0.33,
        "decision": "stay",
        "dlt_details": [
          {
            "usubjid": "KI001-007",
            "dlt_term": "Grade 3 diarrhea",
            "dlt_onset_day": 18,
            "resolved": true,
            "resolution_day": 25
          }
        ]
      },
      {
        "level": 3,
        "dose": "200mg BID",
        "cohort": 4,
        "n_treated": 6,
        "n_dlt": 1,
        "observed_rate": 0.167,
        "decision": "escalate",
        "cumulative_at_dose": {"n": 6, "dlt": 1, "rate": 0.167}
      },
      {
        "level": 4,
        "dose": "300mg BID",
        "cohort": 5,
        "n_treated": 3,
        "n_dlt": 2,
        "observed_rate": 0.67,
        "decision": "de_escalate",
        "dlt_details": [
          {
            "usubjid": "KI001-013",
            "dlt_term": "Grade 3 fatigue",
            "dlt_onset_day": 14
          },
          {
            "usubjid": "KI001-015",
            "dlt_term": "Grade 3 hypertension",
            "dlt_onset_day": 21
          }
        ]
      },
      {
        "level": 3,
        "dose": "200mg BID",
        "cohort": 6,
        "n_treated": 9,
        "n_dlt": 2,
        "observed_rate": 0.222,
        "decision": "mtd_declared",
        "isotonic_estimate": 0.22
      }
    ],
    "mtd_determination": {
      "mtd_dose": "200mg BID",
      "total_at_mtd": 9,
      "dlts_at_mtd": 2,
      "dlt_rate": 0.222,
      "target_rate": 0.30
    }
  },
  "pk_summary": {
    "dose_proportionality": "approximately_linear",
    "accumulation_ratio": 1.8,
    "half_life_h": 12.5,
    "steady_state_day": 5
  },
  "preliminary_efficacy": {
    "evaluable_for_response": 18,
    "best_responses": {
      "CR": 0,
      "PR": 2,
      "SD": 8,
      "PD": 8
    },
    "orr": 0.111,
    "dcr": 0.556
  }
}
```

### Example 3: Cell Therapy Phase 1 with Extended DLT Window

**Request:** "Generate Phase 1 CAR-T cell therapy dose escalation data"

```json
{
  "study": {
    "study_id": "CART-101",
    "title": "Phase 1 Study of CART-101 in Relapsed/Refractory B-cell Lymphoma",
    "phase": "1",
    "design": "3+3_modified",
    "population": "r_r_dlbcl"
  },
  "study_design": {
    "conditioning": "fludarabine_cyclophosphamide",
    "dlt_window": {
      "crs_window_days": 28,
      "neurotoxicity_window_days": 56
    },
    "dose_levels": [
      {"level": 1, "cells_per_kg": "1e6", "description": "1 × 10^6 CAR-T cells/kg"},
      {"level": 2, "cells_per_kg": "3e6", "description": "3 × 10^6 CAR-T cells/kg"},
      {"level": 3, "cells_per_kg": "1e7", "description": "1 × 10^7 CAR-T cells/kg"}
    ]
  },
  "dose_escalation": [
    {
      "level": 1,
      "dose": "1e6 cells/kg",
      "subjects": [
        {
          "usubjid": "CART101-001",
          "crs_grade": 1,
          "crs_onset_day": 3,
          "icans_grade": 0,
          "dlt": false,
          "response_d28": "CR"
        },
        {
          "usubjid": "CART101-002",
          "crs_grade": 2,
          "crs_onset_day": 5,
          "icans_grade": 1,
          "dlt": false,
          "response_d28": "PR"
        },
        {
          "usubjid": "CART101-003",
          "crs_grade": 1,
          "crs_onset_day": 4,
          "icans_grade": 0,
          "dlt": false,
          "response_d28": "CR"
        }
      ],
      "decision": "escalate"
    },
    {
      "level": 2,
      "dose": "3e6 cells/kg",
      "subjects": [
        {
          "usubjid": "CART101-004",
          "crs_grade": 2,
          "crs_onset_day": 2,
          "icans_grade": 2,
          "dlt": false,
          "response_d28": "CR"
        },
        {
          "usubjid": "CART101-005",
          "crs_grade": 3,
          "crs_onset_day": 3,
          "tocilizumab_doses": 2,
          "icans_grade": 1,
          "dlt": false,
          "response_d28": "CR"
        },
        {
          "usubjid": "CART101-006",
          "crs_grade": 2,
          "crs_onset_day": 4,
          "icans_grade": 3,
          "icans_onset_day": 8,
          "dlt": true,
          "dlt_term": "Grade 3 ICANS",
          "response_d28": "PR"
        }
      ],
      "decision": "expand"
    },
    {
      "level": 2,
      "dose": "3e6 cells/kg",
      "expansion": true,
      "total_n": 6,
      "total_dlt": 1,
      "dlt_rate": 0.167,
      "decision": "escalate"
    },
    {
      "level": 3,
      "dose": "1e7 cells/kg",
      "subjects": [
        {
          "usubjid": "CART101-010",
          "crs_grade": 3,
          "icans_grade": 3,
          "dlt": true,
          "dlt_term": "Grade 3 ICANS requiring ICU"
        },
        {
          "usubjid": "CART101-011",
          "crs_grade": 4,
          "icans_grade": 2,
          "dlt": true,
          "dlt_term": "Grade 4 CRS"
        },
        {
          "usubjid": "CART101-012",
          "crs_grade": 3,
          "icans_grade": 4,
          "dlt": true,
          "dlt_term": "Grade 4 ICANS"
        }
      ],
      "decision": "mtd_exceeded"
    }
  ],
  "mtd_determination": {
    "mtd_dose": "3e6 cells/kg",
    "rp2d": "3e6 cells/kg",
    "rationale": "Level 3 exceeded MTD (3/3 DLTs); Level 2 demonstrated acceptable safety (1/6 DLTs)"
  },
  "efficacy_summary": {
    "evaluable": 9,
    "cr_rate": 0.667,
    "orr": 0.889,
    "median_time_to_response_days": 28
  }
}
```

---

## Validation Rules

| Field | Rule | Error Handling |
|-------|------|----------------|
| `dose_level` | Sequential integer starting at 1 | Auto-assign if missing |
| `n_dlt` | ≤ `n_treated` | Error if exceeded |
| `dlt_rate` | n_dlt / n_treated | Calculate if missing |
| `decision` | Must follow design algorithm | Validate against rules |
| `mtd` | Must be declared dose level | Verify DLT rate < target |
| `pk_parameters` | All positive values | Reject negative values |
| `dlt_onset_day` | Within DLT window | Flag if outside window |

### Design-Specific Validation

**3+3 Design:**
- Cohort size must be 3 (or 6 for expansion)
- Decision follows exact algorithm
- MTD is highest dose with <33% DLT rate

**BOIN Design:**
- Boundaries correctly calculated from target
- Decision matches boundary comparison
- Isotonic regression for final MTD

---

## Business Rules

### Dose Escalation Rules

1. **No dose skipping** - Cannot skip untested dose levels
2. **Minimum observation** - DLT window must complete before escalation decision
3. **Cohort completion** - All subjects in cohort must reach decision point
4. **Safety stopping** - If starting dose exceeds MTD, trial may stop

### PK Generation Rules

1. **Dose proportionality** - AUC and Cmax scale with dose (unless saturation)
2. **Variability** - CV typically 20-50% for PK parameters
3. **Half-life consistency** - Should be similar across dose levels
4. **Accumulation** - Predicted from half-life and dosing interval

### Safety Monitoring Rules

1. **Sentinel dosing** - First 1-2 subjects observed before cohort completion
2. **Cohort review** - Safety review committee (SRC) between cohorts
3. **Stopping rules** - Pre-defined criteria for early termination
4. **Blinding** - Maintain until database lock (placebo-controlled studies)

---

## Related Skills

| Skill | Integration |
|-------|-------------|
| [SDTM Demographics (DM)](domains/demographics-dm.md) | Subject identifiers, disposition |
| [SDTM Adverse Events (AE)](domains/adverse-events-ae.md) | DLT coding, safety data |
| [SDTM Exposure (EX)](domains/exposure-ex.md) | Dose administration records |
| [SDTM Disposition (DS)](domains/disposition-ds.md) | Screen failures, discontinuations |
| [Oncology Trials](therapeutic-areas/oncology.md) | Cancer-specific Phase 1 patterns |
| [CNS Trials](therapeutic-areas/cns.md) | Neurology Phase 1 considerations |
| [Cell & Gene Therapy](therapeutic-areas/cgt.md) | CAR-T, gene therapy dose escalation |
| [Phase 2 Proof-of-Concept](phase2-proof-of-concept.md) | Transition to Phase 2 |

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2024-12-20 | Initial comprehensive Phase 1 skill |

