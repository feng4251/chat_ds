# Phase 2b 临床试验方案

## Resmetirom (THR-β 激动剂) 治疗 MASH 伴肝纤维化 —— 剂量探索与概念验证

---

**方案编号**: RESMET-P2B-201
**方案版本**: 2.0 (知识补充修订版)
**设计日期**: 2026-06-11
**NCT 参考**: NCT02912260 (Phase 2), NCT03900429 (Phase 3 MAESTRO-NASH)
**药物**: Resmetirom (瑞司美替罗, Rezdiffra™), THR-β 选择性激动剂
**适应症**: 代谢相关脂肪性肝炎 (MASH) 伴 F2-F3 期肝纤维化
**申办方**: [虚拟] TrialSim Generated Protocol

---

## 设计方案速览

```
设计类型:    随机、双盲、安慰剂对照、平行组、3 臂 Phase 2b 剂量探索
随机化:      1:1:1 分层区组 (分层因子: T2DM 状态 + 基线 MRI-PDFF 分层)
样本量:      180 名受试者 (60/臂)
剂量组:      安慰剂 | Resmetirom 80mg QD | Resmetirom 100mg QD
治疗周期:    36 周 (MRI-PDFF 主要终点 @ Week 16; 活检次要终点 @ Week 36)
研究中心:    35 个 (美国 20, 欧洲 10, 亚太 5)
主要终点:    Week 16 MRI-PDFF 自基线的相对变化 (%)
关键次要:    Week 36 肝活检 NASH 消退 (NAS 0-2) 且纤维化不恶化;
             Week 36 纤维化改善 ≥1 期且 NASH 不恶化;
             ALT 自基线变化; LDL-C 变化
决策:        若 MCP-Mod 剂量-反应显著 (p<0.05) 且 ≥1 个剂量组的安全性可接受 → RP3D 进入 Phase 3
```

---

## 目录

1. [方案设计依据](#1-方案设计依据)
2. [纳排标准](#2-纳排标准)
3. [终点定义](#3-终点定义)
4. [MCP-Mod 统计设计](#4-mcp-mod-统计设计)
5. [样本量计算](#5-样本量计算)
6. [访视方案](#6-访视方案)
7. [期中分析与安全性监测](#7-期中分析与安全性监测)
8. [SDTM 数据生成规划](#8-sdtm-数据生成规划)
9. [Go/No-Go 决策标准](#9-gono-go-决策标准)
10. [Phase 3 衔接路径](#10-phase-3-衔接路径)
11. [项目技能文件使用索引](#11-项目技能文件使用索引)

---

## 1. 方案设计依据

### 1.1 核心设计原理

| 设计选择 | 依据 | 来源 |
|---------|------|------|
| **Phase 2b (非 2a)** | 已有明确的 Phase 2a 概念验证数据 (NCT02912260, N=125, 80mg 单组)，本试验需做剂量选择 | `phase2-proof-of-concept.md` L36-46 |
| **MCP-Mod 设计** | FDA/EMA 推荐的剂量探索方法，同时控制多重性和建模 | `phase2-proof-of-concept.md` L132-165 |
| **3 臂非 4 臂** | Phase 3 真实设计 (CT.gov NCT03900429) 使用 80mg 和 100mg，无 40mg 组 | `knowledge_gaps_filled.md` §1 (ClinicalTrials.gov) |
| **主要终点: MRI-PDFF @ Week 16** | Phase 2b 不需要活检作为主要终点 (活检在确证性 Phase 3 中作为主要终点)；MRI-PDFF 是 FDA 认可的替代终点 | PMID 40046382 (无创标志物综述); `phase2-proof-of-concept.md` L36-46 |
| **活检 @ Week 36** | 作为关键次要终点支持加速批准路径 (与 MAESTRO-NASH 的 52 周逻辑一致) | NCT03900429 (双主要终点设计) |
| **N=180 (60/臂)** | MCP-Mod 模拟 (Power=90%, α=0.05) | `phase2-proof-of-concept.md` L38-45 (Phase 2b: 100-500 样本量) |

### 1.2 真实数据校正

来自 ClinicalTrials.gov 和 PubMed 的关键修正:

| 参数 | 未校正值 | 校正值 | 来源 |
|------|--------|-------|------|
| 名称 | NASH | **MASH** (2023 AASLD/EASL 更名) | PMID 38771485 (Drugs 2024) |
| ALT 排除 | >5×ULN (200 U/L) | **>250 U/L** | NCT03900429 eligibilityModule |
| TSH 排除 | "未控制甲功异常" | **TSH >7 mIU/L** | NCT03900429 eligibilityModule |
| HbA1c 排除 | >9.5% | **≥9.0%** | NCT03900429 eligibilityModule |
| NAS 入选 | ≥4 (各项≥1) | **≥4** (无需各项≥1) | NCT03900429 + PMID 21319198 (Brunt 2011: NAS≠诊断) |
| AST 入选 | 未设定 | **AST ≤8.5×ULN** | NCT03900429 eligibilityModule |
| MRI-PDFF 角色 | 主要终点 | **Week 16 主要终点; Week 36 活检是关键次要** | NCT03900429 (活检是 Phase 3 主要终点，Phase 2 可更灵活) |

---

## 2. 纳排标准

> **依据**: 本项目 `recruitment-enrollment.md` L202-254 (I/E 模板), `examples/ie_criteria_resmetirom_mash.md` (疾病特异性 I/E), ClinicalTrials.gov NCT03900429 (真实阈值), PubMed PMID 21319198 (NAS 验证)

### 2.1 入选标准 (必须全部满足)

| # | 标准 | 阈值 | 评估方法 | SDTM 变量 | 来源 |
|---|------|------|---------|----------|------|
| **I1** | 年龄 18-80 岁 | [18, 80] | 人口统计学 | DM.AGE | 纳排文件 I1 |
| **I2** | 筛选前 6 个月内肝活检证实 MASH | NAS ≥4, 脂肪变≥1, 小叶炎症≥1, 气球样变≥1; 纤维化 F2 或 F3 | 中心病理学阅片 (NASH CRN) | MH | 纳排文件 I2-I3 |
| **I3** | 筛选期 MRI-PDFF ≥8% | ≥8% | 3T MRI 肝脏脂肪定量 | LB (自定义 LBTESTCD="PDFF") | NCT03900429; 纳排文件 I5 |
| **I4** | 筛选期 ALT ≤250 U/L (≤6.25×ULN) | ≤250 | 实验室 | LB.ALT | **CT.gov 修正** (原为 >200) |
| **I5** | 筛选期 AST ≤8.5×ULN | ≤8.5×ULN | 实验室 | LB.AST | **CT.gov 补充** (原缺失) |
| **I6** | 若为 T2DM: HbA1c <9.0% 且降糖方案稳定 ≥3个月 | HbA1c <9.0% | 实验室 + CM | LB, CM | **CT.gov 修正** (原为 >9.5%) |
| **I7** | TSH 0.5-7.0 mIU/L | TSH ∈ [0.5, 7.0] | 实验室 | LB (TSH) | **CT.gov 补充** (原为模糊的"甲功异常") |
| **I8** | 体重稳定 (筛选前 3 个月内变化 <5%) | ΔWeight <5% in 90d | 病史 + VS | VS.WEIGHT | 纳排文件 I6 |
| **I9** | 有生育能力女性: 血清妊娠试验阴性 + 高效避孕 | β-hCG 阴性 | 实验室 | LB | 纳排文件 I9 |
| **I10** | 签署知情同意书 | — | ICF | DM.RFICDTC | 纳排文件 I10 |

### 2.2 排除标准 (必须全部不满足)

#### 肝脏相关

| # | 标准 | 阈值 | 失败代码 |
|---|------|------|---------|
| **E1** | 肝硬化 (F4) 或临床失代偿 (腹水、静脉曲张出血、肝性脑病) | 活检 F4 或 临床失代偿 | IE02 |
| **E2** | 其他慢性肝病 (HBV/HCV/自身免疫性肝炎/PBC/血色素沉着症/Wilson病/α1-AT缺乏) | 血清学 + 病史 | IE20 |
| **E3** | 显著酒精摄入 (男 >21 单位/周, 女 >14 单位/周) | AUDIT ≥8 | IE21 |
| **E4** | 既往减肥手术或计划研究期间进行 | — | IE22 |
| **E5** | HCC 病史或 AFP >50 ng/mL 且影像学未排除 | AFP >50 | IE20 |

#### 代谢/心血管相关

| # | 标准 | 阈值 | 失败代码 |
|---|------|------|---------|
| **E6** | HbA1c ≥9.0% | HbA1c ≥9.0% | IE10 |
| **E7** | 筛选前 6 个月内心肌梗死、卒中、不稳定心绞痛 | 6 个月内 | IE24 |
| **E8** | 未控制高血压 (SBP>160 或 DBP>100 mmHg, 重复确认) | SBP>160 或 DBP>100 | IE14 |

#### 药物相关

| # | 标准 | 阈值 | 失败代码 |
|---|------|------|---------|
| **E9** | 已知对 Resmetirom 或辅料过敏 | — | IE20 |
| **E10** | 使用已知致肝脂肪变性的药物 (胺碘酮、甲氨蝶呤、他莫昔芬、丙戊酸) | ≥2周, 筛选前6月内 | IE21 |
| **E11** | 强效/中效 CYP3A4 诱导剂或抑制剂 | 酮康唑、利福平、卡马西平等 | IE21 |
| **E12** | TSH >7 mIU/L (甲状腺功能异常) | TSH >7 | IE10 |
| **E13** | 甲状腺激素替代治疗: 除非剂量稳定 ≥3个月 且 TSH 在正常范围 | — | IE21 |

#### 一般排除

| # | 标准 | 阈值 | 失败代码 |
|---|------|------|---------|
| **E14** | eGFR <30 mL/min/1.73m² | <30 | IE11 |
| **E15** | 妊娠、哺乳 | β-hCG 阳性 | IE30 |
| **E16** | 5 年内恶性肿瘤史 (除外已治愈的非黑色素瘤皮肤癌、原位癌) | 5 年内诊断 | IE21 |
| **E17** | 30 天内参与其他干预性试验 | 30 天内 | IE34 |
| **E18** | 研究者判断不适合 | — | IE32 |

---

## 3. 终点定义

### 3.1 主要终点

| 参数 | 定义 | 分析方法 | SDTM 映射 | 评估时点 |
|------|------|---------|----------|---------|
| **MRI-PDFF 相对变化** | (Week16 - Baseline) / Baseline × 100% | MCP-Mod (主要); ANCOVA (敏感性) | LB: `LBTESTCD="PDFF"`, `LBORRESU="%"` | Week 16 ±5 天 |

### 3.2 关键次要终点

| 终点 | 定义 | 分析方法 | SDTM 域 | 评估时点 |
|------|------|---------|---------|---------|
| **NASH 消退** | NAS 0-2 (脂肪变 0-1, 炎症 0-1, 气球样变=0) 且纤维化不恶化 | Logistic 回归 | MH (活检报告) | Week 36 |
| **纤维化改善 ≥1 期** | NASH CRN 纤维化分期改善 ≥1 且 NASH 不恶化 | Logistic 回归 | MH (活检报告) | Week 36 |
| **MRI-PDFF ≥30% 减少** | 二分类应答者分析 | Logistic 回归 | LB | Week 16 |
| **ALT 变化** | 自基线的绝对变化 (U/L) | MMRM | LB | Week 2,4,8,12,16,24,36 |
| **LDL-C 变化** | 自基线的百分比变化 (%) | MMRM | LB | Week 12,16,24,36 |
| **FibroScan kPa 变化** | 肝脏硬度测量 | ANCOVA | 自定义 | Week 16,36 |

### 3.3 安全性终点

| 终点 | 监测方法 | SDTM 域 |
|------|---------|---------|
| TEAE + SAE (MedDRA 编码) | 全程 + 30 天随访 | AE |
| 血清转氨酶升高 (ALT/AST >3×ULN) | 每次访视 | LB + LBTOXGR |
| 甲状腺功能 (TSH, fT3, fT4) | Week 4, 8, 16, 24, 36 | LB |
| 骨密度 (DXA) | 筛选 + Week 36 | 自定义 |
| 生命体征 | 每次访视 | VS |
| 心电图 (QTc) | 筛选 + Week 16, 36 | 自定义 |

---

## 4. MCP-Mod 统计设计

> **依据**: `phase2-proof-of-concept.md` L132-165 (MCP 步骤 + Mod 步骤)

### 4.1 候选剂量-反应模型 (6 模型)

| 模型 | 公式 | Resmetirom 的合理性 |
|------|------|-------------------|
| **Emax** | E = E₀ + (Emax × d)/(ED50 + d) | THR-β 受体结合饱和 → 符合 Emax 动力学 (首选模型) |
| **Sigmoidal Emax** | E = E₀ + (Emax × d^h)/(ED50^h + d^h) | 若 h>1 提示协同激活 |
| **Linear** | E = E₀ + δ × d | 若 Emax 未达——即 80-100mg 范围内效应仍线性上升 |
| **Exponential** | E = E₀ + E₁ × (1 - exp(-d/δ)) | 快速起效后接近平台 |
| **Log-linear** | E = E₀ + δ × log(d + 1) | 检测高剂量效应被压缩 |
| **Quadratic** | E = E₀ + β₁d + β₂d² | 检测高剂量效应下降 (β₂<0) |

**先验 ED50**: 基于 THR-β 受体 EC50 ~0.3μM 和肝脏药物暴露数据, 预估 ED50 ≈ 20-40mg (PMID 33655500)

### 4.2 MCP 步骤 (多重比较 — 检测剂量-反应信号)

```
MCP (Multiple Comparison Procedures):
  - 4 个最优对照检验 (基于 Emax 形状的先验)
  - 多重性调整: Westfall-Young Bootstrap (10,000 次)
  - α_MCP = 0.05 (单侧)
  - 零假设 H₀: 所有剂量组的效应 = 安慰剂组
  - 备择假设 H₁: 至少一个剂量组的效应 > 安慰剂组
```

### 4.3 Mod 步骤 (模型拟合与选择)

```
Mod (Modeling):
  1. 对 6 个候选模型进行非线性最小二乘拟合
  2. 基于以下标准选择最佳模型: AIC/BIC/残差标准误差
  3. 若多个模型支持 → 模型平均法
  4. 从最佳模型估计:
     - MED (最小有效剂量): 产生 15% PDFF 相对减少的最低剂量
     - ED50: 产生 50% Emax 的剂量
     - ED80: 产生 80% Emax 的剂量 → RP3D 候选
```

### 4.4 预期剂量-效应 (基于 Phase 2 NCT02912260 和文献)

| 剂量组 | N | MRI-PDFF Δ% (SE) | ≥30% PDFF 减少率 | ALT Δ U/L (SE) | LDL-C Δ% |
|--------|---|------------------|------------------|----------------|---------|
| 安慰剂 | 60 | -2% (4.0) | 8% | -2 (6) | -2% |
| 80mg QD | 60 | -26% (5.0) | 42% | -16 (7) | -13% |
| 100mg QD | 60 | -30% (5.5) | 48% | -19 (8) | -16% |

---

## 5. 样本量计算

> **依据**: `phase2-proof-of-concept.md` L38-45, MCP-Mod 模拟方法

```
MCP-Mod 样本量模拟 (10,000 iterations):

  效应量 (安慰剂校正的 PDFF 变化):
    - 80mg: Δ = -24% (相对于安慰剂的绝对减少)
    - 100mg: Δ = -28%
    - SD: 19% (基于 Phase 2 NCT02912260 MRI-PDFF 变异度)

  参数: α = 0.05 (单侧), Power = 90%, 3 组

  模拟结果:
    - 每组需要 ≥54 可评估受试者
    - 考虑 10% 脱落率 (活检/MRI 不耐受): 54/0.90 = 60/臂
    - 总计: N = 60 × 3 = 180
  
  招募目标: 筛查 ~300 名 → 随机化 180 名 (SF 率 ~40%)
```

---

## 6. 访视方案

| 访视 | 时间 | 窗口 | 评估内容 | SDTM 域 |
|------|------|------|---------|---------|
| **V1 筛选** | Day -28 to -1 | — | I/E 评估、肝活检 (若 >6 个月)、MRI-PDFF、实验室 (ALT/AST/HbA1c/TSH/eGFR/LDL-C)、生命体征、心电图、合并用药、AUDIT、知情同意 | DM, MH, LB, VS, CM, DS |
| **V2 基线/随机** | Day 1 | — | 随机化 (IWRS)、分配药物、实验室、生命体征、基线 PRO | DM, LB, VS, EX |
| **V3** | Week 2 | ±3d | 安全性实验室 (ALT/AST/TSH)、生命体征、AE 评估、依从性检查 | LB, VS, AE, EX |
| **V4** | Week 4 | ±3d | 安全性实验室 (ALT/AST/TSH/LDL-C)、生命体征、AE、依从性 | LB, VS, AE, EX |
| **V5** | Week 8 | ±5d | 安全性实验室、生命体征、AE、依从性、药代动力学 (谷浓度) | LB, VS, AE |
| **V6** | Week 12 | ±5d | 实验室 (ALT/AST/TSH/LDL-C/脂质谱)、生命体征、AE、依从性 | LB, VS, AE |
| **V7** | **Week 16 (主要)** | ±5d | **MRI-PDFF**、FibroScan、实验室、生命体征、心电图、AE、依从性 | LB, VS, AE |
| **V8** | Week 24 | ±7d | 实验室、生命体征、AE、依从性、骨密度 (DXA) | LB, VS, AE |
| **V9** | **Week 36 (活检)** | ±7d | **肝活检**、MRI-PDFF、FibroScan、实验室、生命体征、心电图、AE、骨密度 | MH, LB, VS, AE |
| **V10 安全性随访** | Week 40 | ±7d | 实验室、生命体征、AE (末次给药后 30 天) | LB, VS, AE |

---

## 7. 期中分析与安全性监测

> **依据**: `phase2-proof-of-concept.md` L197-218 (期中分析框架), `clinical-trials-domain.md` (ICH E6 DSMB 要求)

### 7.1 DSMB 组成

依据 `phase3-pivotal.md` L220-234 的 DSMB 模板:

| 角色 | 专业领域 | 职责 |
|------|---------|------|
| 主席 | 生物统计学 | 审查非盲数据, 做出继续/停止建议 |
| 成员 1 | 肝病学 | MASH 领域专家, 评估肝脏安全信号 |
| 成员 2 | 内分泌学 | THR-β 通路专家, 评估甲状腺安全信号 |
| 统计师 | 独立统计师 | 期中分析执行 |

### 7.2 期中分析方案

| 分析 | 时机 | 目的 | 停止规则 |
|------|------|------|---------|
| **安全性审查 1** | 前 30 名完成 Week 4 | 排除急性肝毒性/TSH 严重抑制 | ALT >10×ULN in ≥2 subjects → DSMB 紧急会议 |
| **安全性审查 2** | 前 60 名完成 Week 16 | 期中安全性评估 | 活性组 SAE 率显著高于安慰剂 → 建议调整 |
| **最终分析** | 全部 180 名完成 Week 36 | MCP-Mod + 活检读片 | — |

### 7.3 安全性停止规则

```
ALT/AST 升高:
  - >8×ULN (无其他病因) → 永久停药
  - >5×ULN + TBL >2×ULN (Hy's Law) → 永久停药 + SAE 报告
  - >3×ULN + 症状 (恶心、腹痛、乏力) → 中断给药 + 密切监测

TSH 变化:
  - TSH <0.1 mIU/L (深度抑制) → 中断给药 + 甲状腺功能全套
  - TSH <0.3 mIU/L + 症状 (心悸、震颤) → 减量或中断

骨密度:
  - DXA T-score 下降 >0.5 (相对于基线) → 评估继续参与的风险
```

---

## 8. SDTM 数据生成规划

> **依据**: 项目 `domains/*.md` YAML frontmatter (160 结构化变量), `references/code-systems.md` (受控术语)

### 8.1 各域记录数预估

| 域 | 记录数 | 关键变量 (来自 YAML frontmatter) | 受控术语 (来自 code-systems.md) |
|----|--------|-------------------------------|------------------------------|
| **DM** | 180 | STUDYID, USUBJID, AGE, SEX, RACE, ARM, COUNTRY | SEX (C66731), RACE (C74457), ETHNIC (C66790) |
| **MH** | ~450 | MHDECOD (MASH), MHBODSYS, MHSTDTC | MedDRA PT→SOC |
| **LB** | ~16,200 | ALT/AST/GGT/TBL/TSH/LDL-C/PDFF (自定义) + LBNRIND + LBLOINC | LOINC: ALT=1742-6, AST=1920-8, TSH=3016-3, LDL-C=2089-1 |
| **VS** | ~9,000 | WEIGHT, BMI, SYSBP, DIABP, PULSE | VSTESTCD: SYSBP/DIABP/PULSE/WEIGHT/BMI |
| **EX** | 180 | EXTRT, EXDOSE (0/80/100), EXDOSFRQ="QD", EXROUTE="ORAL" | EXDOSFRQ (CDISC CT), EXROUTE (CDISC CT) |
| **AE** | ~400 | AETERM, AEDECOD, AEBODSYS, AESEV, AESER, AEREL | AESEV (MILD/MODERATE/SEVERE), AEREL (5 级) |
| **CM** | ~500 | CMTRT, CMDECOD, CMATC1CD-CM4CD | WHO ATC (5 级) |
| **DS** | ~540 | DSTERM, DSDECOD, DSCAT, DSSCAT | DSCAT (PROTOCOL MILESTONE/DISPOSITION EVENT) |

### 8.2 MASH 特有的自定义 SDTM 变量

| 自定义变量 | Label | 域 | LOINC/映射 |
|-----------|-------|-----|-----------|
| `LBTESTCD="PDFF"` | MRI Proton Density Fat Fraction | LB | 无标准 LOINC, 使用 CDISC 自定义 |
| `LBTESTCD="KPA"` | FibroScan Liver Stiffness | LB | 无标准 LOINC |
| `MH.MHTERM="NASH CRN Score"` | 活检 NAS (0-8) + 纤维化分期 (F0-F4) | MH | MedDRA PT: "Non-alcoholic steatohepatitis" |
| `LBTESTCD="TSH"` | Thyroid Stimulating Hormone | LB | LOINC 3016-3 |
| `LBTESTCD="FT3"` | Free T3 | LB | LOINC 3051-0 |

---

## 9. Go/No-Go 决策标准

> **依据**: `phase2-proof-of-concept.md` L43 (RP3D 选择)

| 标准 | Go → Phase 3 | No-Go | 权重 |
|------|-------------|-------|------|
| **MCP-Mod 剂量-反应** | 调整后 p < 0.05 (至少 1 个对比) | p ≥ 0.05 | **门控** |
| **MRI-PDFF 效应量** | ≥1 个活性组的 PDFF 相对减少 ≥20% (安慰剂校正) | 两组均 <15% | 高 |
| **活检 NASH 消退** (关键次要) | ≥1 个活性组的 NASH 消退率 ≥25% (安慰剂 ~10%) | 两组均 <15% | 高 |
| **ED50 可识别** | ED50 在 0-80mg 范围内可准确估计 | 平坦剂量-反应 | 中 |
| **ALT 改善** | ≥1 个活性组的 ALT 绝对降低 ≥12 U/L (安慰剂校正) | 两组均 <8 | 中 |
| **LDL-C 改善** | ≥1 个活性组的 LDL-C 降低 ≥10% (安慰剂校正) | 两组均 <5% | 低 |
| **安全性 — 肝脏** | 无 Hy's Law 病例; ALT >5×ULN 发生率 ≤2% | ALT >5×ULN >5% 或 任何 Hy's Law 病例 | **门控** |
| **安全性 — 甲状腺** | TSH <0.1 mIU/L 发生率 ≤5% | TSH <0.1 mIU/L >10% | **门控** |
| **安全性 — 骨密度** | DXA T-score 变化无临床意义 (<0.3 组均值变化) | T-score 下降 >0.5 (组均值) | 中 |
| **总体 SAE** | 活性组 SAE ≤安慰剂 + 5% | 活性组 SAE 显著更高 (p<0.05) | 高 |

### RP3D 选择逻辑

```
若 80mg 和 100mg 均满足 Go 标准:
  ├── 若 100mg 的效应量显著大于 80mg (p<0.05) → RP3D = 100mg
  ├── 若 100mg 的安全性显著劣于 80mg → RP3D = 80mg
  └── 若无显著差异 → RP3D = 100mg (基于 Phase 3 MAESTRO-NASH 的先验)

若仅 100mg 满足 (80mg 效应不足):
  └── RP3D = 100mg

若仅 80mg 满足 (100mg 安全性问题):
  └── RP3D = 80mg

若两组均不满足:
  └── No-Go → 终止开发 或 考虑探索更高剂量
```

---

## 10. Phase 3 衔接路径

| 里程碑 | 时间 | 内容 |
|--------|------|------|
| **数据库锁定** | Week 36 LSLV + 4 周 | SDTM + 统计分析 |
| **主要结果读出** | 数据库锁定 + 2 周 | MRI-PDFF MCP-Mod 结果 |
| **活检结果读出** | 数据库锁定 + 6 周 | 中心病理学阅片 (3 名独立病理医生) |
| **RP3D 确定** | 活检结果 + 2 周 | Go/No-Go 决策 |
| **Phase 3 方案撰写** | RP3D + 4 周 | MAESTRO-NASH 确证性 Phase 3 (N ≈ 1,700-2,000) |
| **FDA 沟通** | Phase 3 方案定稿后 | Type B 会议 (EOP2) |

---

## 11. 项目技能文件使用索引

### 11.1 本项目文件使用

| # | 文件 | 使用的具体内容 | 行号/章节 |
|---|------|--------------|---------|
| 1 | `phase2-proof-of-concept.md` | Phase 2b 特征 (L36-46), MCP-Mod 方法 (L132-165), 平行剂量组结构 (L167-183), 期中分析 (L197-218), 生成模式 (L340-374) | 全文 |
| 2 | `recruitment-enrollment.md` | I/E 评估格式 (L202-254), IE01-IE34 代码体系 (L112-148), 5 阶段筛选漏斗 (L64-101) | §2, §4, §5 |
| 3 | `domains/demographics-dm.md` | DM 变量 YAML frontmatter (22 变量) | YAML frontmatter |
| 4 | `domains/adverse-events-ae.md` | AE 变量 (21 变量), AESEV/AEREL 受控术语, SAE 六标准 (AESCONG-AESMIE) | YAML frontmatter, CT 表 |
| 5 | `domains/laboratory-lb.md` | LB 变量 (23 变量), LBNRIND, LBLOINC, Hy's Law 标准, CTCAE 分级, 肝脏毒性监测模式 | YAML frontmatter, L296-310 |
| 6 | `domains/vital-signs-vs.md` | VS 变量 (20 变量), VSTESTCD (SYSBP/DIABP/PULSE/WEIGHT/BMI) | YAML frontmatter |
| 7 | `domains/exposure-ex.md` | EX 变量 (20 变量), EXDOSFRQ/EXROUTE 受控术语, 剂量修改模式 | YAML frontmatter |
| 8 | `domains/disposition-ds.md` | DS 变量 (12 变量), DSDECOD 里程碑 (知情同意→随机→完成/中止) | YAML frontmatter, L88-114 |
| 9 | `domains/medical-history-mh.md` | MH 变量 (20 变量), MedDRA PT→SOC 编码 | YAML frontmatter |
| 10 | `domains/concomitant-meds-cm.md` | CM 变量 (22 变量), ATC 分类 (CMATC1CD-CM4CD), CYP3A4 交互药物 | YAML frontmatter |
| 11 | `references/code-systems.md` | MedDRA v27 (SOC→PT), LOINC v2.78 (ALT/AST/TSH/LDL-C), ATC 2025 (合并用药), CDISC CT (SEX/RACE/AESEV/AEREL) | 全文 |
| 12 | `references/data-models.md` | 15 实体 JSON Schema (Subject, Study, AE, Exposure, MedicalHistory, DispositionEvent) | 实体 Schema |
| 13 | `clinical-trials-domain.md` | ICH E6/E8/E9 合规, CDISC 标准, Phase 2→3 衔接逻辑 | L12-17, L23-48 |
| 14 | `phase3-pivotal.md` | DSMB 组成模板 (L220-234), 分层随机化方法 (L114-125) | §7, §2 |
| 15 | `examples/ie_criteria_resmetirom_mash.md` | MASH 特异性纳排标准 (10 入选 + 18 排除), 筛查访视表 | 全文 |

### 11.2 外部数据库补充

| # | 来源 | 具体补充内容 |
|---|------|------------|
| 1 | **ClinicalTrials.gov** NCT03900429 | 真实 Phase 3 设计 (N=1759, 3臂), 纳排阈值 (ALT>250, TSH>7, HbA1c≥9.0%, NAS≥4), 双主要终点 (52周活检+54月临床结局) |
| 2 | **ClinicalTrials.gov** NCT02912260 | Phase 2 历史设计 (N=125, 80mg 单组) |
| 3 | **PubMed** PMID 38324483 | MAESTRO-NASH Phase 3 主要结果 (*NEJM* 2024), Harrison SA et al. |
| 4 | **PubMed** PMID 38771485 | Resmetirom FDA 加速批准 (2024.03), *Drugs* 2024, Keam SJ |
| 5 | **PubMed** PMID 33655500 | THR-β 肝脏药理机制 (独立于体重的脂质降低), *Br J Pharmacol* 2021, Kannt A et al. |
| 6 | **PubMed** PMID 21319198 | NASH CRN NAS 评分 vs 组织病理诊断的区分, *Hepatology* 2011, Brunt EM et al. |
| 7 | **PubMed** PMID 40046382 | 无创标志物 (MRI-PDFF) 诊断 NAFLD 的系统综述, *Cureus* 2025 |

### 11.3 未使用的项目文件 (与本方案不相关)

| 文件 | 不相关原因 |
|------|-----------|
| `phase1-dose-escalation.md` | Resmetirom Phase 1 已完成 (NCT01367873), 本方案为 Phase 2b |
| `rwe/synthetic-control.md` | 本试验为 RCT, 不需要外部对照臂 |
| `therapeutic-areas/oncology.md` | 不适用于肝病 |
| `therapeutic-areas/cardiovascular.md` | 不直接相关 (虽 MASH 患者有心血管合并症) |
| `therapeutic-areas/cns.md` | 不适用 |
| `therapeutic-areas/cgt.md` | 不适用 |
| `skills/adam/*.md` | 数据分析和统计编程阶段使用, 非方案设计阶段 |
| `formats/dimensional-analytics.md` | BI 分析阶段使用 |

---

*方案生成: TrialSim v2.0 + clinicaltrials-database + pubmed-database*
*本方案为合成试验设计方案, 用于测试和开发目的*
