# Resmetirom (THR-β 激动剂) 治疗 MASH 临床试验 — 纳排标准

**药物**: Resmetirom (瑞司美替罗, MGL-3196)
**靶点**: 甲状腺激素受体-β (THR-β) — 肝脏特异性
**适应症**: 代谢相关脂肪性肝炎 (MASH / 原 NASH) 伴 F2-F3 期肝纤维化
**试验阶段**: Phase 3 确证性试验
**参考方案**: MAESTRO-NASH (NCT03900429)
**生成日期**: 2026-06-11
**生成方式**: 依据项目 `recruitment-enrollment.md` 的 I/E 模板 + `clinical-trials-domain.md` 的 MASH 知识

---

## 入选标准 (Inclusion Criteria — 必须满足全部)

| # | 标准 | 评估方法 | 量化阈值 | 对应 SDTM 变量 | 失败代码 |
|---|------|---------|---------|---------------|---------|
| **I1** | 年龄 18-80 岁 | 人口统计学 | `AGE` ∈ [18, 80] | DM.AGE | IE01 |
| **I2** | 肝活检证实 MASH 诊断 (NAS ≥4, 各项 ≥1) | 中心病理学阅片 | NAS ≥4, 脂肪变≥1, 小叶炎症≥1, 气球样变≥1 | MH.MHDECOD = "Non-alcoholic steatohepatitis" | IE02 |
| **I3** | 肝纤维化 F2 或 F3 期 (NASH CRN 分级) | 中心病理学阅片 | F2 (门静脉周围+窦周纤维化) 或 F3 (桥接纤维化) | MH.MHTERM 含 "fibrosis stage" | IE03 |
| **I4** | 肝活检在筛选前 6 个月内完成 | 病理报告日期 | 活检日期距筛查 ≤180 天 | — | IE04 |
| **I5** | 筛选期 MRI-PDFF ≥8% 肝脏脂肪含量 | MRI-PDFF | ≥8% | LB 自定义域 | IE05 |
| **I6** | 体重稳定 (筛选前 3 个月内变化 <5%) | 病史 + 生命体征 | ΔWeight <5% in 90 days | VS.WEIGHT | IE10 |
| **I7** | 若为 2 型糖尿病患者: HbA1c ≤9.5%, 稳定降糖方案 ≥3个月 | 实验室 + 病史 | HbA1c ≤9.5% | LB (HbA1c), CM (降糖药) | IE11 |
| **I8** | 筛选期 ALT ≤5× ULN | 实验室 | ALT ≤ 5× 40 = 200 U/L | LB.ALT, LB.LBORNRHI | IE12 |
| **I9** | 有生育能力女性: 血清妊娠试验阴性 + 高效避孕 | 实验室 + 知情同意 | β-hCG 阴性 | LB, DM.SEX | IE30 |
| **I10** | 签署知情同意书 | ICF | 日期 ≤ 任何筛选程序 | DM.RFICDTC | IE31 |

---

## 排除标准 (Exclusion Criteria — 必须全部不满足)

### E类: 肝脏相关排除

| # | 标准 | 评估方法 | 量化阈值 | 失败代码 |
|---|------|---------|---------|---------|
| **E1** | 肝硬化 (F4 期) 或临床失代偿征象 | 活检 + 临床评估 | 活检 F4 或 腹水/静脉曲张/肝性脑病 | IE02 |
| **E2** | 其他慢性肝病: HBV/HCV、自身免疫性肝炎、原发性胆汁性胆管炎、血色素沉着症、α1-抗胰蛋白酶缺乏 | 血清学 + 病史 | HBsAg-, anti-HCV-, ANA<1:160, 铁蛋白正常 | IE20 |
| **E3** | 显著酒精摄入 (男 >21 单位/周, 女 >14 单位/周) | 病史, AUDIT 问卷 | AUDIT ≥8 | IE21 |
| **E4** | 既往 NASH 药物治疗史: 维生素E>400IU/天、吡格列酮、GLP-1 RA、SGLT2i, 除非洗脱≥3个月 | 病史 + CM | 任何 NASH 相关的研究中药物或已上市药物治疗史 | IE04 |
| **E5** | 既往减肥手术 或 计划在研究期间进行 | 病史 | 任何类型的减肥手术 | IE22 |
| **E6** | 肝细胞癌 (HCC) 病史或筛查时 AFP >50 ng/mL | 病史 + 实验室 | AFP >50 且影像学未排除HCC | IE20 |

### F类: 代谢/心血管相关排除

| # | 标准 | 评估方法 | 量化阈值 | 失败代码 |
|---|------|---------|---------|---------|
| **E7** | HbA1c >9.5% 的未控制糖尿病 | 实验室 | HbA1c >9.5% | IE10 |
| **E8** | 筛选前 6 个月内心肌梗死、卒中或不稳定型心绞痛 | 病史 | 6 个月内发生 | IE24 |
| **E9** | 未控制的高血压 (SBP >160 或 DBP >100 mmHg) | 生命体征 | SBP >160 或 DBP >100, 重复测量确认 | IE14 |

### G类: 药物相关排除

| # | 标准 | 评估方法 | 量化阈值 | 失败代码 |
|---|------|---------|---------|---------|
| **E10** | 已知对 Resmetirom 或其辅料过敏 | 病史 | 任何过敏史 | IE20 |
| **E11** | 使用已知引起肝脂肪变性的药物 (胺碘酮、甲氨蝶呤、他莫昔芬、丙戊酸) ≥2周, 筛选前6个月内 | CM 域 | 在 ATC 交互列表中匹配 | IE21 |
| **E12** | 使用强效或中效 CYP3A4 诱导剂或抑制剂 | CM 域 | 酮康唑、利福平、卡马西平、圣约翰草等 | IE21 |
| **E13** | 使用甲状腺激素替代治疗的受试者, 除非剂量稳定 ≥3 个月且 TSH 在正常范围 | CM + LB | TSH 不在正常范围 | IE10 |

### H类: 一般健康相关排除

| # | 标准 | 评估方法 | 量化阈值 | 失败代码 |
|---|------|---------|---------|---------|
| **E14** | eGFR <30 mL/min/1.73m² (CKD-EPI) | 实验室 | eGFR <30 | IE11 |
| **E15** | 妊娠、哺乳或计划在研究期间怀孕 | 病史 + 实验室 | β-hCG 阳性 | IE30 |
| **E16** | 过去5年内恶性肿瘤病史, 除外已治愈的非黑色素瘤皮肤癌、原位癌 | 病史 | 5年内诊断 | IE21 |
| **E17** | 筛选前30天内参与其他干预性临床试验或使用研究药物 | 病史 + DS | 30天内 | IE34 |
| **E18** | 研究者判断不适合参与的任何医学状况 | 研究者判断 | — | IE32 |

---

## 筛查评估访视 (Screening Visit)

| 评估项目 | 方法 | SDTM 域 | 窗口 |
|---------|------|---------|------|
| 知情同意 | ICF 签署 | DS | 第 -28 天 |
| 人口统计学与病史 | 病历查阅 + 受试者访谈 | DM, MH | 第 -28 至 -1 天 |
| 肝活检 | 经皮肝活检 + 中心阅片 | — (外部文件) | 第 -180 至 -1 天 |
| MRI-PDFF | 3T MRI 肝脏脂肪定量 | — (外部文件) | 第 -28 至 -1 天 |
| 实验室评估 | ALT, AST, ALP, GGT, TBL, Alb, HbA1c, FPG, eGFR, β-hCG, AFP, TSH | LB | 第 -28 至 -1 天 |
| 生命体征 | SBP, DBP, HR, Weight, BMI | VS | 第 -28 至 -1 天 |
| 合并用药 | 所有处方与非处方药 | CM | 第 -28 至 -1 天 |
| 饮酒评估 | AUDIT 问卷 | — | 第 -28 至 -1 天 |

---

## 典型受试者纳排评估示例 (格式遵循 recruitment-enrollment.md)

### 受试者 A — 符合条件 ✅

```json
{
  "eligibility_assessment": {
    "subject_id": "SCRN-RESMET-001-0042",
    "assessment_date": "2026-02-15",
    "assessed_by": "Dr. Chen",
    "overall_result": "ELIGIBLE",
    "inclusion_criteria": [
      {"criterion": "I1", "met": true, "value": "52 years", "notes": null},
      {"criterion": "I2", "met": true, "value": "NAS 6 (S3+I2+B1)", "notes": "Central path confirmed"},
      {"criterion": "I3", "met": true, "value": "F3 bridging fibrosis", "notes": "NASH CRN stage 3"},
      {"criterion": "I5", "met": true, "value": "MRI-PDFF 18.2%", "notes": "≥8% threshold met"},
      {"criterion": "I7", "met": true, "value": "HbA1c 7.8%, metformin stable 2yr", "notes": "≤9.5% and stable regimen ≥3mo"},
      {"criterion": "I8", "met": true, "value": "ALT 62 U/L", "notes": "≤5×ULN (200)"}
    ],
    "exclusion_criteria": [
      {"criterion": "E1", "met": false, "notes": "No cirrhosis on biopsy"},
      {"criterion": "E7", "met": false, "notes": "HbA1c 7.8% ≤9.5%"},
      {"criterion": "E14", "met": false, "notes": "eGFR 78 mL/min"}
    ],
    "failure_reasons": []
  }
}
```

### 受试者 B — 筛选失败 ❌

```json
{
  "eligibility_assessment": {
    "subject_id": "SCRN-RESMET-003-0112",
    "assessment_date": "2026-02-22",
    "assessed_by": "Dr. Chen",
    "overall_result": "SCREEN_FAILURE",
    "inclusion_criteria": [
      {"criterion": "I1", "met": true, "value": "47 years", "notes": null},
      {"criterion": "I2", "met": true, "value": "NAS 5 (S2+I2+B1)", "notes": "Central path confirmed"},
      {"criterion": "I3", "met": true, "value": "F2 perisinusoidal fibrosis", "notes": "NASH CRN stage 2"},
      {"criterion": "I5", "met": true, "value": "MRI-PDFF 14.5%", "notes": "≥8%"},
      {"criterion": "I8", "met": false, "value": "ALT 235 U/L", "notes": ">5×ULN (200)"}
    ],
    "exclusion_criteria": [
      {"criterion": "E2", "met": true, "value": "Anti-HCV positive", "notes": "Newly detected HCV Ab"},
      {"criterion": "E8", "met": true, "value": "MI 3 months ago", "notes": "Recent ACS event within 6 months"}
    ],
    "failure_reasons": [
      {"code": "IE12", "description": "ALT >5×ULN", "criterion": "I8", "value": "235 U/L"},
      {"code": "IE20", "description": "Concurrent chronic liver disease: HCV", "criterion": "E2"},
      {"code": "IE24", "description": "Recent MI within 6 months", "criterion": "E8"}
    ]
  }
}
```

---

## 预期筛选失败率分布 (对应 recruitment-enrollment.md 漏斗模型)

| 类别 | IE 代码 | 预期占比 | 典型原因 |
|------|---------|---------|---------|
| 肝脏疾病相关 | IE01-IE04 | **20%** | 活检不符合 (NAS<4, F1/F4), MRI-PDFF <8% |
| 实验室异常 | IE10-IE14 | **30%** | ALT >5×ULN, HbA1c >9.5%, eGFR <30 |
| 病史排除 | IE20-IE24 | **25%** | 合并慢性肝病, 近期CV事件, 恶性肿瘤史 |
| 药物/合并用药 | IE21 | **15%** | CYP3A4 强效抑制剂, NASH 竞争药物 |
| 行政/同意 | IE30-IE34 | **10%** | 撤回同意, 竞争试验, 活检不耐受 |

**总筛选失败率 (MASH 试验基准)**: 30-45%

---

## 如何与项目现有能力对接

### 知识层 (已具备)
| 能力 | 来源文件 | 在本次生成中的应用 |
|------|---------|------------------|
| I/E 评估 JSON 格式 | `recruitment-enrollment.md` L230-254 | ✅ 直接套用 |
| IE 代码体系 IE01-IE34 | `recruitment-enrollment.md` L112-148 | ✅ 分配至具体纳排项 |
| 筛选漏斗 5 阶段模型 | `recruitment-enrollment.md` L64-90 | ✅ 预估转化率 |
| CDISC 受控术语 | `domains/*.md` + `references/code-systems.md` | ✅ ALT/ HbA1c/ eGFR 等 LOINC |
| Phase 3 试验设计模板 | `phase3-pivotal.md` | ✅ 随机、双盲、安慰剂对照 |
| DM/MH/CM/LB/VS 变量定义 | `domains/*.md` YAML frontmatter | ✅ 纳排直接映射至 SDTM 变量 |

### 代码层 (需新建)
| 脚本 | 功能 | 状态 |
|------|------|------|
| `scripts/eligibility_engine.py` | 逐受试者自动评估纳排 | ❌ 未创建 |
| `generate_test_data.py` 中集成 | 生成 screen failure DS 记录 | ❌ 未实现 |

### 本文件可直接履行的作用
1. 作为 **LLM prompt 输入** — 让 LLM 按此纳排标准生成符合 MASH 试验要求的虚拟受试者
2. 作为 **手动审查清单** — 在数据生成后逐条核验受试者是否符合入组条件
3. 作为 **eligibility_engine.py 的需求文档** — 10 条入选 + 18 条排除 = 清晰的编码规格
