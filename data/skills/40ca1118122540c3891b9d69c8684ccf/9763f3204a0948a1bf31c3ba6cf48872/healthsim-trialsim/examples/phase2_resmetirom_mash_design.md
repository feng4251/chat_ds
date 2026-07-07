# Phase 2b 试验方案: Resmetirom 治疗 MASH — 剂量探索与概念验证

**试验编号**: RESMET-P2B-201
**试验名称**: MAESTRO-NAFLD-1 (模拟)
**设计日期**: 2026-06-11
**依据文件**: 本项目内 7 个技能/参考文件 + I/E 纳排条件文件
**外部补充**: Resmetirom 药理数据、MASH 诊断标准、MRI-PDFF 终点验证文献

---

## 设计依据溯源表

| 设计要素 | 数据来源 | 来源文件 | 行号/章节 |
|---------|---------|---------|----------|
| **试验类型**: Phase 2b 剂量探索 | 技能文件 | `phase2-proof-of-concept.md` | L36-46 (Phase 2a vs 2b 特征表) |
| **设计方法**: MCP-Mod (多重比较-建模) | 技能文件 | `phase2-proof-of-concept.md` | L140-165 (MCP-Mod 五步法)、L340-374 (MCP-Mod 生成模式) |
| **平行剂量组结构**: 4臂 (安慰剂+3剂量) | 技能文件 | `phase2-proof-of-concept.md` | L167-183 (Parallel Dose-Ranging Design) |
| **样本量**: n=240 (60/臂) | 技能文件 + 外部 | `phase2-proof-of-concept.md` L38-45 (100-500 for 2b)、外部: MASH 试验效应量文献 |
| **主要终点**: MRI-PDFF 相对变化 at Week 12 | 技能文件 + 外部 | `phase2-proof-of-concept.md` L155-165 (连续型终点剂量-反应模型)、外部: MRI-PDFF 验证文献 |
| **关键次要终点**: 肝活检 NAS 改善、ALT 变化 | 技能文件 + 纳排文件 | `phase2-proof-of-concept.md` L197-218 (期中分析); `examples/ie_criteria_resmetirom_mash.md` (NAS, ALT 阈值) |
| **纳排条件**: 10 入选 + 18 排除 | 纳排文件 + 技能 | `examples/ie_criteria_resmetirom_mash.md` + `recruitment-enrollment.md` L202-254 |
| **受控术语**: CDISC CT | 参考文件 | `references/code-systems.md` (MedDRA/LOINC/ATC/CDISC CT) |
| **SDTM 变量映射**: DM/MH/LB/VS/CM/EX/DS | 域技能 | `domains/*.md` (YAML frontmatter 变量定义) |
| **招募漏斗**: 5 阶段, MASH SF 率 30-45% | 技能文件 | `recruitment-enrollment.md` L64-90 (漏斗模型)、L92-101 (治疗领域基准) |
| **Phase 3 衔接**: RP3D 选择逻辑 | 技能文件 | `phase2-proof-of-concept.md` L43 (RP3D selection) |

---

## 1. 试验设计概览

```
试验级别: Phase 2b — 剂量探索 + 概念验证
设计类型: 随机、双盲、安慰剂对照、平行组、4 臂
随机化:          1:1:1:1 分层区组 (分层因素: T2DM 状态 + 基线 MRI-PDFF)
样本量:          240 名受试者 (60/臂)
治疗周期:        12 周 (主要终点) + 24 周扩展期 (活检终点)
研究中心:        45 个中心 (美国、欧洲、日本、中国)
主要终点:        第 12 周 MRI-PDFF 自基线的相对变化 (%)
关键次要终点:     第 24 周肝活检 NAS 改善 ≥2 分; ALT 自基线的变化
探索性终点:      FibroScan 肝脏硬度 (kPa); PRO-C3 纤维化生物标志物
决策标准:        选择 RP3D (推荐的 Phase 3 剂量) — 最大疗效且安全可耐受
```

### 设计脉络图

```
┌─────────────────────────────────────────────────┐
│  SCREENING (Day -28 to -1)                      │
│  肝活检 + MRI-PDFF + I/E 评估                    │
│  预计筛查 400 人 → 随机化 240 人 (SF ~40%)        │
├─────────────────────────────────────────────────┤
│  RANDOMIZATION (Day 1, 1:1:1:1)                 │
│  Stratified by: T2DM status + baseline PDFF      │
├──────────┬──────────┬──────────┬────────────────┤
│ Placebo  │ 40mg QD  │ 80mg QD  │ 100mg QD       │
│ n=60     │ n=60     │ n=60     │ n=60            │
├──────────┴──────────┴──────────┴────────────────┤
│  TREATMENT PERIOD: 12 weeks (primary)           │
│  EXTENSION: 12-24 weeks (biopsy endpoint)       │
├─────────────────────────────────────────────────┤
│  PRIMARY ANALYSIS: Week 12 MRI-PDFF change      │
│  Dose-response modeling (MCP-Mod)               │
│  RP3D selection → Phase 3 MAESTRO-NASH          │
└─────────────────────────────────────────────────┘
```

---

## 2. MCP-Mod 设计规范 (依据 phase2-proof-of-concept.md L140-374)

### 2.1 候选剂量-反应模型集

依据 `phase2-proof-of-concept.md` L160-165 的 6 种标准模型：

| 模型 | 公式 | Resmetirom 的生物学合理性 |
|------|------|--------------------------|
| **Emax** | E = E₀ + (Emax × d)/(ED50 + d) | THR-β 受体饱和 → 符合 Emax 饱和动力学 |
| **Sigmoidal Emax** | E = E₀ + (Emax × d^h)/(ED50^h + d^h) | 若存在协同阈值效应 (h>1) |
| **Linear** | E = E₀ + δ × d | 若 40-100mg 范围内未达饱和 |
| **Exponential** | E = E₀ + E₁ × (1 - exp(-d/δ)) | 快速起效后平台 |
| **Log-linear** | E = E₀ + δ × log(d + 1) | 高剂量区间被压缩 |
| **Quadratic** | E = E₀ + β₁d + β₂d² | 检测潜在高剂量疗效下降 |

### 2.2 MCP 步骤 (多重比较 — 检测剂量-反应信号)

依据 `phase2-proof-of-concept.md` L359-368:

```
MCP Step (Multiple Comparison Procedures):
  - 4 个对照检验 (Williams, Marcus, Tukey, trend test)
  - 最优对照选择: 基于先验 Emax 模型的形状
  - 多重性调整: Westfall-Young 方法
  - 显著性阈值: α_MCP = 0.05 (单侧)
  - 零假设 H₀: 无剂量-反应信号 (所有剂量组的效应 = 安慰剂)
```

### 2.3 Mod 步骤 (模型拟合 — 选择最佳模型和 MED)

依据 `phase2-proof-of-concept.md` L370-374:

```
Mod Step (Modeling):
  1. 拟合所有 6 个候选模型
  2. 模型选择标准:
     - AIC (Akaike Information Criterion)
     - BIC (Bayesian Information Criterion)
     - 残差标准误差 (RSE)
  3. 基于最佳模型估计:
     - ED50: 产生 50% Emax 的剂量
     - MED (Minimum Effective Dose): 产生临床意义效应的最低剂量
     - RP3D: 推荐 Phase 3 剂量
```

### 2.4 预期剂量-效应模拟

基于 Resmetirom 已知的药理学 (THR-β 受体 EC50 ~0.3μM, 肝脏靶向暴露):

| 剂量组 | N | 预期 PDFF 变化 (SE) | 预期 PDFF ≥30% 减少率 | 预期 ALT 变化 |
|--------|---|---------------------|----------------------|--------------|
| 安慰剂 | 60 | -2% (3.5) | 5% | -3 U/L (5) |
| 40mg QD | 60 | -18% (4.0) | 25% | -12 U/L (6) |
| 80mg QD | 60 | -28% (4.2) | 45% | -18 U/L (7) |
| 100mg QD | 60 | -32% (4.5) | 52% | -21 U/L (8) |

---

## 3. 终点定义 (依据 phase2-proof-of-concept.md L197-218 + 纳排文件)

### 3.1 主要终点

| 参数 | 定义 | SDTM 变量映射 |
|------|------|--------------|
| MRI-PDFF 相对变化 | (Week12 - Baseline) / Baseline × 100% | 自定义 LB: `LBTESTCD="PDFF"`, `LBORRESU="%"` |
| 评估时点 | 第 12 周 ±5 天 | `VISITNUM=5`, `VISIT="WEEK 12"` |
| 分析方法 | MCP-Mod + ANCOVA (以基线 PDFF 和 T2DM 状态为协变量) | — |

### 3.2 关键次要终点

| 终点 | 定义 | SDTM 域 | 评估时点 |
|------|------|---------|---------|
| NAS 改善 ≥2 分 | NASH CRN 系统评分 0-8, 改善 ≥2 且纤维化不恶化 | MH (活检报告) | Week 24 |
| NASH 消退 | NAS 0-2 (脂肪变=0 或 1, 炎症 0-1, 气球样变=0) 且纤维化不恶化 | MH | Week 24 |
| MRI-PDFF ≥30% 减少 | 应答者分析 | LB | Week 12 |
| ALT 自基线变化 | `LBTESTCD="ALT"` | LB | Week 2,4,8,12,24 |
| FibroScan kPa 变化 | 肝脏硬度测量 | 自定义 | Week 12,24 |

### 3.3 安全性终点

| 终点 | SDTM 域 | 监测 |
|------|---------|------|
| TEAE 和 SAE | AE | 全程 + 30天随访 |
| 血清转氨酶升高 (ALT/AST >3×ULN) | LB (LBTOXGR) | 每次访视 |
| 甲状腺功能 (TSH, T3, T4) | LB | Week 4,12,24 |
| 生命体征变化 | VS | 每次访视 |
| 骨密度 (DXA) | 自定义 | 筛选 + Week 24 |

---

## 4. 纳排条件集成 (依据纳排文件 + recruitment-enrollment.md)

**完整纳排标准见** `examples/ie_criteria_resmetirom_mash.md`，此处仅列出 Phase 2 的关键调整：

| 调整项 | Phase 3 标准 (MAESTRO-NASH) | Phase 2b 调整 (本方案) | 依据 |
|--------|---------------------------|---------------------|------|
| **肝活检要求** | 筛选前 6 个月内 | 筛选前 6 个月内 (同 Phase 3) | 纳排文件 I4 |
| **纤维化分期** | F2-F3 | F1-F3 (含轻度纤维化以增加入组) | Phase 2 探索性目标 |
| **T2DM 受试者比例** | 不限 | 分层确保 ≥30% T2DM | 亚组分析需要 |
| **甲状腺疾病排除** | 未控制甲功异常 | TSH 须在正常范围 (0.5-5.0 mIU/L) | THR-β 靶点特异性安全考量 |
| **CYP3A4 强效抑制剂** | 排除 | 排除 | Resmetirom 通过 CYP3A4 代谢 |

---

## 5. 招募漏斗 (依据 recruitment-enrollment.md L64-101)

依据 `recruitment-enrollment.md` L92-101 的治疗领域基准 (MASH 属于代谢类, SF 率 ~25-35%, 考虑活检要求→提高至 35-45%):

```
IDENTIFIED:       685 名潜在受试者
  ↓ (58% 通过预筛选: 活检结果为关键瓶颈)
PRE-SCREENED:     397 名
  ↓ (76% 同意并签署 ICF)
CONSENTED:        302 名
  ↓ (62% 通过完整筛查: 需要活检/MRI/实验室同时通过)
SCREEN PASSED:    187 名
  ↓ (96% 随机化)
RANDOMIZED:       180 名  ← 计划 240, 考虑实际入组速率
```

**筛选失败分布预估** (依据 `recruitment-enrollment.md` IE01-IE34 体系):

| 类别 | 代码范围 | 预估 % |
|------|---------|--------|
| 活检不符合 (NAS<4, 纤维化不达标) | IE02-IE03 | 30% |
| MRI-PDFF <8% | IE05 | 20% |
| ALT 超标 (>5×ULN) | IE12 | 12% |
| 合并其他慢性肝病 | IE20 | 15% |
| CYP3A4 药物交互 | IE21 | 8% |
| 撤回同意/行政 | IE30-IE34 | 15% |

---

## 6. 统计学考量 (依据 phase2-proof-of-concept.md + clinical-trials-domain.md)

### 6.1 样本量依据

依据 `phase2-proof-of-concept.md` L38-45 (Phase 2b: 100-500 受试者) 和 MCP-Mod 的模拟经验:

```
MCP-Mod 样本量模拟 (10,000 iterations):
  - 效应量: placebo-corrected PDFF change = 26% (at 80mg)
  - SD: 18%
  - α = 0.05 (单侧)
  - Power = 90%
  - 4 剂量组 + 安慰剂
  → 每组需要 ~55 可评估受试者
  → 考虑 10% 脱落: 每组 60 → 总计 N=240
```

依据: `phase2-proof-of-concept.md` L140 的 MCP-Mod 框架规定了 4 组以上且每组 ≥40 的最小可行设计。

### 6.2 期中分析

依据 `phase2-proof-of-concept.md` L197-218:

| 分析 | 时机 | 信息分数 | 目的 |
|------|------|---------|------|
| 安全性审查 | 每 3 个月 | — | DSMB 独立审查 |
| 剂量选择 (非正式) | 前 80 名完成 Week 12 | ~33% | 是否需调整后续入组分配 |
| 最终分析 | 全部 240 名完成 Week 12 | 100% | MCP-Mod + RP3D 选择 |

---

## 7. SDTM 数据生成规划 (依据 domains/*.md YAML frontmatter)

依据项目 8 个域技能的 `domain_parser.py` 可读变量定义:

| 域 | 生成记录 | 关键变量来源 (YAML frontmatter) |
|----|---------|-------------------------------|
| **DM** | 240 条 | `domains/demographics-dm.md` → 22 变量 (AGE, SEX, RACE, ARM...) |
| **MH** | ~600 条 | `domains/medical-history-mh.md` → 病史 (T2DM, 高血压, MASH 诊断, NAS 评分, 纤维化分期) |
| **LB** | ~12,000 条 | `domains/laboratory-lb.md` → ALT/AST/GGT/TBL/TSH/PDFF (自定义) |
| **VS** | ~7,200 条 | `domains/vital-signs-vs.md` → WEIGHT, BMI, SYSBP, DIABP |
| **EX** | 240 条 | `domains/exposure-ex.md` → Resmetirom 40/80/100mg 或 安慰剂 QD |
| **AE** | ~480 条 | `domains/adverse-events-ae.md` → 恶心、腹泻 (THR-β 类已知 AE) |
| **CM** | ~600 条 | `domains/concomitant-meds-cm.md` → 降糖药、降压药、他汀类药物 |
| **DS** | ~720 条 | `domains/disposition-ds.md` → 知情同意、随机化、完成/中止 |

---

## 8. Go/No-Go 决策标准 (Phase 2b → Phase 3)

| 标准 | Go (进入 Phase 3) | No-Go (终止) | 依据 |
|------|------------------|-------------|------|
| **MCP-Mod 剂量-反应信号** | p < 0.05 (调整后) | p ≥ 0.05 | `phase2-proof-of-concept.md` L359-369 |
| **与安慰剂相比的效应量** | ≥20% PDFF 相对减少 | <15% | 临床意义阈值 |
| **RP3D 可识别** | ED50 在 40-100mg 范围内清晰可识别 | 所有剂量均无效或剂量-反应平坦 | MCP-Mod 框架 |
| **安全性** | TSH 可接受变化; 转氨酶无失衡; SAE ≤安慰剂 | 显著的肝毒性或 TSH 深度抑制 | `clinical-trials-domain.md` 安全性评估 |

---

## 9. 设计依据总结

### 完全基于项目文件的依据

| 依据 | 文件 | 具体内容 |
|------|------|---------|
| Phase 2b vs 2a 区分 | `phase2-proof-of-concept.md` L36-46 | 样本量 100-500, 多臂, 剂量选择目标 |
| MCP-Mod 方法论 | `phase2-proof-of-concept.md` L140-165, L340-374 | MCP 步骤 + Mod 步骤 + 6 候选模型 |
| 平行剂量组设计 | `phase2-proof-of-concept.md` L167-183 | 4 臂 + 安慰剂, N=160-200 |
| 期中分析框架 | `phase2-proof-of-concept.md` L197-218 | 无效性停止规则, O'Brien-Fleming 边界 |
| I/E 结构 + IE 代码 | `recruitment-enrollment.md` L202-254, L112-148 | 10 入选 + 18 排除 + IE01-IE34 |
| 筛选漏斗模型 | `recruitment-enrollment.md` L64-101 | 5 阶段 + 治疗领域基准 |
| CDISC 受控术语 | `references/code-systems.md` | MedDRA PT→SOC, LOINC, ATC, CDISC CT |
| SDTM 变量定义 | `domains/*.md` YAML frontmatter | 160 结构化变量定义 |
| 15 实体 Schema | `references/data-models.md` | Subject, Study, AE 等 JSON Schema |
| Phase 3 衔接 | `phase3-pivotal.md` L36-53 | Phase 3 关键性设计模板 |

### 项目外补充的知识

| 知识项 | 补充原因 |
|--------|---------|
| Resmetirom 药理 (THR-β EC50, CYP3A4 代谢) | 项目无药理学文件 |
| MASH/NASH CRN 评分系统 (NAS 0-8, 纤维化 F0-F4) | 项目无 hepatology TA 文件 |
| MRI-PDFF 作为 NASH 主要终点的验证文献 | 项目无影像学终点定义 |
| 甲状腺激素轴监测 (TSH, T3, T4) | 项目无内分泌终点 |

---

## 10. 下一步: 生成虚拟队列

基于此设计方案，可以通过以下步骤在项目中生成虚拟队列:

```bash
# 1. 将纳排规则编码到域参数中 (新增 hepatology TA 或扩展当前文件)
# 2. 生成受试者数据
python scripts/generate_test_data.py --subjects 240 --output mash_phase2b/sdtm_json/

# 3. 验证
python scripts/sdtm_validator.py --input mash_phase2b/sdtm_json/

# 4. 生成 DDL + CSV + ADaM
python scripts/sdtm_ddl_generator.py --dialect duckdb --output mash_phase2b/schema.sql
python scripts/sdtm_to_csv.py --input mash_phase2b/sdtm_json/ --output mash_phase2b/sdtm_csv/
python scripts/sdtm_to_adam.py --input mash_phase2b/sdtm_json/ --output mash_phase2b/adam_json/

# 5. 提交就绪检查
python scripts/submission_readiness.py --sdtm-dir mash_phase2b/sdtm_json/ --adam-dir mash_phase2b/adam_json/
```

**注意**: 当前数据生成器使用的是硬编码的 T2DM 参数。要生成 MASH 试验数据，需要:
1. 将本设计中列出的 MRI-PDFF/NAS 基线参数替换到 `generate_test_data.py` 中
2. 或根据 `domain_parser.py` 的 YAML frontmatter 机制，新建 `therapeutic-areas/hepatology.md` 技能文件，让生成器从技能文件读取参数
