---
name: external-knowledge-harvester
description: |
  Automatically detect knowledge gaps in clinical trial design tasks and
  invoke external authoritative databases (ClinicalTrials.gov, PubMed) to
  fill missing domain knowledge. Triggers when TrialSim lacks therapeutic
  area expertise, drug pharmacology data, disease-specific I/E thresholds,
  or published trial design parameters. Integrates clinicaltrials-database
  and pubmed-database skills into the TrialSim workflow.
---

# External Knowledge Harvester

## 概述

当 TrialSim 执行临床方案设计任务时，若项目的 4 个治疗领域技能 (oncology/cardiovascular/CNS/CGT) 和 8 个 SDTM 域技能无法覆盖当前适应症，则自动激活此外部知识收割器。该技能调用 `clinicaltrials-database` 和 `pubmed-database` 两个外部数据库技能，从 ClinicalTrials.gov 和 PubMed 检索缺失的领域知识，生成结构化的知识补充报告，并应用于当前任务。

---

## For Claude

**Always apply this skill when you detect ANY of the following knowledge gaps:**

- 用户请求的**治疗领域不在项目 4 个 TA 技能范围内** (如肝病/肾病/自身免疫/罕见病)
- 项目技能文件中**缺少特定药物的药理学数据** (靶点、代谢途径、药物交互)
- 项目技能文件中**缺少特定疾病的诊断标准** (评分系统、分期标准、生物标志物阈值)
- 项目技能文件中**缺少真实试验的纳排阈值** (需要验证或补充 CT.gov 数据)
- **Phase 2/3 真实试验设计参数不明确** (样本量、终点选择、效应量)
- 项目中的**疾病自然史/流行病学数据不充分**

**Workflow (5 steps):**

1. **Detect Gaps**: 对照项目的 TA 技能和域技能，识别缺失的知识点
2. **Query ClinicalTrials.gov**: 搜索注册试验的纳排、终点、样本量、剂量组
3. **Query PubMed**: 搜索关键文献的效应量、诊断标准、药理机制
4. **Generate Supplement**: 以结构化格式总结检索结果，标注来源
5. **Apply to Task**: 将补充后的知识应用于当前方案设计

**Avoid using these external skills when:**
- 任务完全在项目现有的 4 个 TA 和 8 个域范围内
- 不需要具体的真实数据验证
- 仅进行格式转换或数据生成操作

---

## 知识缺口检测矩阵

当任何以下单元标记为 ❌ 时，自动触发外部检索：

```
                                │ 知识来源
设计参数                         │ 项目内   CT.gov   PubMed   补充数据库
────────────────────────────────┼─────────────────────────────────────
疾病诊断标准                     │ ❌       ❌       ✅
疾病分期/分级系统                 │ ❌       ❌       ✅
药物靶点与药理                   │ ❌       ❌       ✅       ✅ DrugBank, ChEMBL
药物代谢途径 (CYP, 转运体)        │ ❌       ❌       ✅       ✅ DrugBank
真实 Phase 2/3 纳排阈值           │ ❌       ✅       ✅
真实 Phase 2/3 样本量与终点       │ ❌       ✅       ✅
真实 Phase 2/3 效应量             │ ❌       ❌       ✅
治疗领域特定 AE 谱               │ ✅       ✅       ✅       ✅ FDA (标签)
CDISC 变量与受控术语              │ ✅       ❌       ❌
试验设计方法论 (MCP-Mod, 3+3等)    │ ✅       ❌       ❌
ICH/FDA/EMA 法规要求              │ ✅       ❌       ❌
疾病流行病学                     │ ❌       ❌       ✅
────────────────────────────────┼─────────────────────────────────────
── v2.2 新增行 ──                │
靶点蛋白结构与结构域               │ ❌       ❌       ❌       ✅ UniProt, PDB
蛋白互作网络与信号通路             │ ❌       ❌       ❌       ✅ STRING, KEGG, Reactome
靶点遗传验证 (GWAS/ClinVar)       │ ❌       ❌       ❌       ✅ GWAS Catalog, ClinVar
靶点可成药性与安全性负债           │ ❌       ❌       ❌       ✅ OpenTargets
已知靶向药物及生物活性 (IC50/Ki)   │ ❌       ❌       ❌       ✅ ChEMBL, DrugBank
临床前模型证据 (KO/转基因)         │ ❌       ❌       ✅       ✅ OpenAlex
scRNA-seq / 组织表达              │ ❌       ❌       ✅       ✅ OpenTargets
竞品药物识别与最高阶段             │ ❌       ✅       ✅       ✅ OpenTargets, DrugBank
竞品活性数据对比 (IC50/选择性)     │ ❌       ❌       ❌       ✅ ChEMBL
竞品安全性特征与FDA标签            │ ❌       ✅       ✅       ✅ FDA, DrugBank
治疗指南/临床实践标准              │ ❌       ❌       ✅
专利/IP趋势分析                   │ ❌       ❌       ❌       ✅ OpenAlex
引用图谱与文献计量                 │ ❌       ❌       ✅       ✅ OpenAlex
知识图谱 (疾病-基因-药物)          │ ❌       ❌       ❌       ✅ PrimeKG
研究趋势与新兴主题                 │ ❌       ❌       ❌       ✅ OpenAlex
```

---

## 工作流模式

### 模式 1: 新治疗领域方案设计 (如 Hepatology)

```
User: "设计 Resmetirom 治疗 MASH 的 Phase 2 试验"

Step 1 — 检测缺口:
  项目治疗领域: oncology, cardiovascular, CNS, CGT
  请求适应症: MASH (肝病) → 不在项目 TA 中 → 触发 External Knowledge Harvester

Step 2 — ClinicalTrials.gov 搜索:
  关键词: "Resmetirom", "MGL-3196", "MASH", "NASH"
  目标: 纳排标准、终点、样本量、剂量组
  API: clinicaltrials.gov/api/v2/studies/{NCT_ID}

Step 3 — PubMed 搜索:
  关键词: "Resmetirom[tiab] AND NASH[tiab]", "THR-beta[tiab] AND NASH[tiab]",
          "MRI-PDFF[tiab] AND biomarker[tiab]", "NASH CRN scoring[tiab]"
  目标: 效应量、药理机制、诊断标准验证
  API: eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi + efetch.fcgi

Step 4 — 生成知识补充:
  输出: examples/knowledge_gaps_filled.md
  格式: 缺失知识点 vs 数据库来源对照表 + 引用详情 + 方案修正建议

Step 5 — 应用补充知识:
  生成完整方案: protocols/phase2b_resmetirom_mash_final.md
  标注: 哪个参数来自项目文件, 哪个来自 CT.gov, 哪个来自 PubMed
```

### 模式 2: 纳排阈值验证

```
User: "生成一个糖尿病肾病的 Phase 3 纳排标准"

Step 1 — 检测缺口:
  项目 TA: 有 cardiovascular (CVOT 设计) 但无 nephrology
  项目 recruitment-enrollment.md: 有通用 I/E 模板但无疾病特异性阈值
  → 触发 External Knowledge Harvester

Step 2 — ClinicalTrials.gov 搜索:
  关键词: "diabetic kidney disease", "Phase 3", "SGLT2i"
  检索: eGFR 阈值、UACR 入选标准、排除标准

Step 3 — 应用:
  将 CT.gov 检索到的 eGFR ≥25、UACR ≥300 mg/g 等阈值
  填入 recruitment-enrollment.md 的 I/E 模板中
```

### 模式 3: 效应量估计

```
User: "计算 MASH Phase 2b MCP-Mod 的样本量"

Step 1 — 检测缺口:
  项目 phase2-proof-of-concept.md: 有 MCP-Mod 框架和公式
  但缺少 MASH 特异性效应量 (PDFF 变化均值/SD)
  → 触发 External Knowledge Harvester

Step 2 — PubMed 搜索:
  关键词: "Resmetirom", "MRI-PDFF", "relative reduction"
  检索: Phase 2 NCT02912260 的已发表效应量

Step 3 — 应用:
  效应量 δ = -26% (80mg), SD = 19%
  输入 phase2-proof-of-concept.md 的样本量公式
```

### 模式 4: 靶点生物学深度分析 (v2.2 新增)

```
User: "分析 Galectin-3 作为阿尔茨海默病靶点的生物学基础"

Step 1 — 检测缺口:
  项目 therapeutic-areas/cns.md: 有 CNS 治疗领域知识
  但缺少: 靶点蛋白结构/域架构、PPI 网络、信号通路、遗传验证、
  已知活性化合物、可成药性评估、临床前模型证据矩阵
  → 触发 External Knowledge Harvester (靶点生物学模式)

Step 2 — 多数据库并行查询:
  ① UniProt: 蛋白结构/域/PTM/异构体
  ② STRING: PPI 网络 (confidence ≥ 0.700) + GO/KEGG 富集
  ③ KEGG + Reactome: 信号通路图谱和反应通路
  ④ ChEMBL: 靶点所有 IC50/Ki/EC50 (assay_confidence ≥ 6)
  ⑤ DrugBank: 已知靶向药物及作用机制
  ⑥ OpenTargets: 靶点-疾病关联评分 + tractability + safety_liabilities
  ⑦ GWAS + ClinVar: 遗传变异-疾病关联
  ⑧ PubMed + OpenAlex: 临床前模型文献 (KO/转基因/scRNA-seq)

Step 3 — 构建证据矩阵:
  为每个提出的作用机制交叉验证 6 个证据维度:
  遗传证据 | 蛋白证据 | 通路证据 | 药理学证据 | 临床前模型证据 | 人类疾病关联
  每个维度按 0-3 评分 (0=无证据, 1=弱, 2=中, 3=强)
  附带证据来源 (PMID / 数据库 ID)

Step 4 — 靶点风险评估:
  靶点新颖性 (First-in-class / Best-in-class / Fast-follower)
  遗传验证强度 (Strong / Moderate / Weak)
  可成药性信心 (High / Medium / Low)
  安全性关注列表

Step 5 — 输出:
  Worker G (worker-target-biology) 的完整结构化输出
```

### 模式 5: 竞争格局分析 (v2.2 新增)

```
User: "分析阿尔茨海默病治疗领域的竞争格局"

Step 1 — 检测缺口:
  项目 therapeutic-areas/cns.md: 有 CNS 终点和 AE 谱
  但缺少: 适应症领域所有已批准/在研药物列表、竞品疗效/安全性基准、
  差异化策略、治疗指南对齐、IP 趋势
  → 触发 External Knowledge Harvester (竞争格局模式)

Step 2 — 多数据库查询:
  ① OpenTargets + DrugBank + CT.gov: 竞品全面识别 (三重交叉验证)
  ② DrugBank + ChEMBL: 竞品机制/靶点/活性数据 (IC50/Ki)
  ③ CT.gov + PubMed + FDA: 竞品疗效基准 (主要终点 Δ vs 对照)
  ④ FDA + DrugBank + PubMed: 竞品安全性特征 (AE 发生率 + 黑框警告)
  ⑤ PubMed: 治疗指南 (NCCN/ESMO/ACC/AHA/AAN/AASLD)
  ⑥ OpenAlex: 专利/发表趋势/机构/作者网络

Step 3 — 竞争矩阵合成:
  药物 | 申办方 | 机制 | 靶点 | 阶段 | 疗效 (Δ) | 安全性 | 差异化

Step 4 — 差异化策略:
  主差异化维度 (3-5 个): 机制/疗效/安全性/便利性/成本
  临床定位: 线数/亚群/联合用药/伴随诊断
  开发先例: 加速批准先例/同类失败教训/II→III 转换率

Step 5 — 输出:
  Worker H (worker-competitive-landscape) 的完整结构化输出
```

### 模式 6: 文献循证综合 (v2.2 新增)

```
User: "对 Galectin-3 在阿尔茨海默病中的研究进行文献综述"

Step 1 — 检测缺口:
  项目内无文献引用管理能力
  需要: 全面文献检索、结构化引用索引、证据分级、引用图谱
  → 触发 External Knowledge Harvester (文献循证模式)

Step 2 — 多数据库查询:
  ① PubMed: 5 个策略性查询 (药物+靶点 / 临床试验 / 机制 / 综述 / 指南)
  ② OpenAlex: 扩展检索 (非 PubMed 索引著作)
  ③ PrimeKG: 知识图谱路径 (疾病↔基因↔药物) — 如 CSV 不可用回退到 OpenTargets+DrugBank+STRING
  ④ OpenAlex: 引用关系图谱 (citing/cited by)、研究趋势
  ⑤ PubMed: 系统综述/荟萃分析/指南检索
  ⑥ FDA: 审评文件作为最高质量循证来源

Step 3 — 结构化引用索引:
  20-40 篇纳入文献的完整元数据
  每篇文献: Oxford CEBM 证据等级 (1A-5)
  按主张组织证据 (Evidence-by-Claim)

Step 4 — 引用图谱与趋势:
  论文-论文引用边列表 + 节点中心性
  主题聚类 (靶点生物学/临床疗效/竞争格局/安全性/生物标志物)
  研究趋势 (按年份/机构/作者/资金来源)
  新兴和衰退主题识别

Step 5 — 综合叙述:
  200-300 字中文执行摘要
  3-7 个综合结论 (含置信度和证据基础)
  知识空白识别 (含严重程度分级和弥补建议)
  格式化的 Vancouver 引用列表

Step 6 — 输出:
  Worker I (worker-literature-synthesis) 的完整结构化输出
```

---

## 数据库技能调用接口

### ClinicalTrials.gov

```python
# 直接 API 调用模式 (在 clinicaltrials-database 技能中)
import requests

# 搜索特定药物
url = "https://clinicaltrials.gov/api/v2/studies"
params = {
    "query.intr": "DRUG_NAME",
    "filter.overallStatus": "COMPLETED",
    "pageSize": 10
}

# 获取特定试验详情
url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
study = response.json()
eligibility = study['protocolSection']['eligibilityModule']
outcomes = study['protocolSection']['outcomesModule']
```

### PubMed

```python
# E-utilities API 调用模式 (在 pubmed-database 技能中)
import requests

# 搜索
base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
params = {"db": "pubmed", "term": "QUERY", "retmax": 10, "retmode": "json"}
resp = requests.get(f"{base}/esearch.fcgi", params=params)

# 获取摘要
params = {"db": "pubmed", "id": "PMID1,PMID2", "rettype": "xml", "retmode": "xml"}
resp = requests.get(f"{base}/efetch.fcgi", params=params)
```

### DrugBank, OpenTargets, ChEMBL, UniProt, STRING, KEGG, OpenAlex, GWAS (v2.2 新增)

这些数据库技能在本系统的 `~/.claude/skills/` 路径下均有独立的 Skill 目录，包含 SKILL.md 和功能完备的 Python 脚本。调用时使用 `Skill(skill="skill-name")` 方式通过 Skill 系统自动加载。

| 数据库 | Skill 名称 | 核心能力 | 脚本文件 |
|--------|-----------|---------|---------|
| **DrugBank** | `drugbank-database` | 9,591 种药物信息、靶点、机制、DDI、药理学 | `scripts/drugbank_helper.py` (DrugBankHelper 类) |
| **OpenTargets** | `opentargets-database` | 靶点-疾病关联评分、tractability、safety_liabilities、已知药物 | `scripts/query_opentargets.py` (GraphQL API) |
| **ChEMBL** | `chembl-database` | 200万+化合物的 IC50/Ki/EC50 生物活性数据 | `scripts/example_queries.py` (REST API) |
| **UniProt** | `uniprot-database` | 蛋白结构/域/PTM/异构体、FASTA、跨数据库 ID 映射 | `scripts/uniprot_client.py` (REST API) |
| **STRING** | `string-database` | 59M+蛋白的 PPI 网络、GO/KEGG 富集 | `scripts/string_api.py` (REST API) |
| **KEGG** | `kegg-database` | 通路图谱、药物-通路链接、代谢网络 | `scripts/kegg_api.py` (REST API) |
| **Reactome** | `reactome-database` | 反应通路富集、与 KEGG 互补的通路覆盖 | REST API |
| **OpenAlex** | `openalex-database` | 2.4亿+学术著作、引用图谱、研究趋势 | `scripts/openalex_client.py` + `query_helpers.py` |
| **GWAS Catalog** | `gwas-database` | SNP-性状关联 (p < 5×10⁻⁸)、遗传效应量 | GWAS Catalog REST API |
| **PrimeKG** | `primekg` | 129K 节点知识图谱 (疾病-基因-药物路径) | `scripts/query_primekg.py` (需要本地 CSV) |

---

## 输出文件规范

### 知识补充报告 (knowledge_gaps_filled.md)

必须包含:
1. **对照表**: 缺失知识点 | 来源数据库 | 引用文章/试验 | 补充内容
2. **方案修正表**: 原方案值 vs 数据库真实值 vs 修正建议
3. **未找到的知识点**: 声明哪些问题在数据库中无法解答

### 完整方案 (protocols/*.md)

必须包含:
1. **项目技能文件使用索引**: 列出每个方案参数对应哪个项目文件
2. **外部数据库补充索引**: 列出哪些参数来自 CT.gov/PubMed
3. **未使用文件声明**: 说明哪些项目文件与当前方案不相关

---

## 目前已完成的示例

| 任务 | 知识缺口 | 搜索来源 | 输出文件 |
|------|---------|---------|---------|
| Resmetirom MASH Phase 2b 方案 | 肝病 TA, 药理, 诊断标准, 纳排阈值 | CT.gov NCT03900429 + 7 PubMed PMIDs | `protocols/phase2b_resmetirom_mash_final.md` |
| I/E 纳排标准生成 | 疾病特异性阈值 | CT.gov eligibilityModule | `examples/ie_criteria_resmetirom_mash.md` |
| 知识补充报告 | 10 个知识点 | CT.gov (3) + PubMed (7) | `examples/knowledge_gaps_filled.md` |

---

## 与项目文件的集成关系

```
skills/external-knowledge-harvester.md (本文件)
  │
  ├──→ 调用 clinicaltrials-database 技能
  │     └──→ CT.gov API v2 → 纳排、终点、样本量
  │
  ├──→ 调用 pubmed-database 技能
  │     └──→ PubMed E-utilities → 文献、机制、效应量
  │
  ├──→ 集成到 TrialSim 方案生成流程
  │     ├── recruitment-enrollment.md → I/E 模板填充阈值
  │     ├── phase2-proof-of-concept.md → 效应量输入
  │     ├── therapeutic-areas/ → 生成新的 TA 知识
  │     └── references/code-systems.md → 补充新的 LOINC/MedDRA 术语
  │
  └──→ 输出
        ├── examples/knowledge_gaps_filled.md → 知识补充报告
        └── protocols/*.md → 完整的试验方案
```

---

## 触发短语

当用户请求中出现以下模式时，自动考虑调用外部知识收割器:

- "设计一个 [不常见的适应症] 的临床试验"
- "为 [新药] 生成纳排标准"
- "这个疾病的诊断标准是什么"
- "Phase 2 的效应量从哪里来"
- "验证这个参数的阈值"
- "真实试验中这个值是怎样的"
- "搜索 ClinicalTrials.gov"
- "查一下 PubMed"
- "补充缺失的知识"

---

## 相关技能

- [clinicaltrials-database](../clinicaltrials-database) — ClinicalTrials.gov API v2 查询技能
- [pubmed-database](../pubmed-database) — PubMed E-utilities 文献检索技能
- [SKILL.md](../../SKILL.md) — TrialSim 主编排技能
- [clinical-trials-domain.md](../../clinical-trials-domain.md) — 临床试验领域知识
- [recruitment-enrollment.md](../../recruitment-enrollment.md) — 纳排标准模板
- [phase2-proof-of-concept.md](../../phase2-proof-of-concept.md) — Phase 2 试验设计框架

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2026-06-11 | 初始版本 — 从 Resmetirom MASH 试验设计中提炼的工作流 |
| 2.0 | 2026-06-30 | v2.2 集成更新 — 新增 10 个外部数据库技能 (DrugBank/OpenTargets/ChEMBL/UniProt/STRING/KEGG/Reactome/OpenAlex/GWAS/PrimeKG)；新增 3 个工作流模式 (靶点生物学深度分析/竞争格局分析/文献循证综合)；扩展知识缺口检测矩阵至 25 个参数行；新增 3 个 Worker (G: 靶点生物学, H: 竞争格局, I: 文献合成) |
