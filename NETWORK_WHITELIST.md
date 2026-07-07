# healthsim-trialsim Skill 最小网络白名单

> 目标：在白名单访问制度下，让 `healthsim-trialsim` skill 完整跑通，不受网络限制。
> 范围：skill 自身调用的外部医学 API + skill 引用的 17 个子技能（`skill:*-database`）的实际 API endpoint + 系统 `web_search` 工具所需的 DDG 端点 + Python 包管理远程地址。
> 日期：2026-07-01

---

## 一、用户指定的核心医学数据源（5 个，必需）

| # | 用途 | 协议/端口 | 域名/IP | 路径 | 认证 | 必需性 |
|---|------|----------|---------|------|------|--------|
| 1 | openFDA / FAERS 药物 AE | HTTPS 443 | api.fda.gov | /drug/event.json | 无（公开） | 核心数据源 |
| 2 | DailyMed SPL / FDA label | HTTPS 443 | dailymed.nlm.nih.gov | /dailymed/services/v2/spls.json、/dailymed/services/v2/spls/{set_id}.xml | 无（公开） | 核心数据源 |
| 3 | ClinicalTrials.gov AE | HTTPS 443 | clinicaltrials.gov | /api/v2/studies | 无（公开） | 核心数据源 |
| 4 | RxNorm / RxNav 标准化 | HTTPS 443 | rxnav.nlm.nih.gov | /REST/rxcui.json、/REST/rxcui/{rxcui}/properties.json | 无（公开） | 核心数据源 |
| 5 | Open Targets 安全性补充 | HTTPS 443 | platform.opentargets.org | /downloads | 无（公开） | 核心数据源 |

---

## 二、skill 引用的 17 个子技能对应的实际 API endpoint（必需）

> healthsim-trialsim 的 orchestrator.yaml 和 worker-*.yaml 引用 17 个 `skill:*-database` 子技能，每个子技能对应一个实际的外部 API endpoint。以下逐一列出。

### 2.1 临床试验与文献数据库

| # | 子技能 | 实际 API endpoint | 认证 | 必需性 |
|---|--------|------------------|------|--------|
| 6 | `skill:clinicaltrials-database` | `https://clinicaltrials.gov/api/v2/studies` | 无（公开） | 必需 |
| 7 | `skill:pubmed-database` | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils` | 无（建议 Email） | 必需 |
| 8 | `skill:fda-database` | `https://api.fda.gov/drug/label.json` | 无（公开） | 必需 |
| 9 | `skill:openalex-database` | `https://api.openalex.org/` | 无（建议 Email） | 必需 |

### 2.2 药物与靶点数据库

| # | 子技能 | 实际 API endpoint | 认证 | 必需性 |
|---|--------|------------------|------|--------|
| 10 | `skill:drugbank-database` | `https://go.drugbank.com/api/v1/` | API Key | 必需 |
| 11 | `skill:chembl-database` | `https://www.ebi.ac.uk/chembl/api/data/` | 无（公开） | 必需 |
| 12 | `skill:uniprot-database` | `https://rest.uniprot.org/uniprotkb/` | 无（公开） | 必需 |
| 13 | `skill:opentargets-database` | `https://api.platform.opentargets.org/api/v4/graphql` | 无（公开） | 必需 |
| 14 | `skill:pubchem-database`（隐含） | `https://pubchem.ncbi.nlm.nih.gov/rest/pug/` | 无（公开） | 必需 |

### 2.3 通路与互作数据库

| # | 子技能 | 实际 API endpoint | 认证 | 必需性 |
|---|--------|------------------|------|--------|
| 15 | `skill:string-database` | `https://string-db.org/api/` | 无（公开） | 必需 |
| 16 | `skill:kegg-database` | `https://rest.kegg.jp/` | 无（公开） | 必需 |
| 17 | `skill:reactome-database` | `https://reactome.org/ContentService/` | 无（公开） | 必需 |

### 2.4 遗传与变异数据库

| # | 子技能 | 实际 API endpoint | 认证 | 必需性 |
|---|--------|------------------|------|--------|
| 18 | `skill:gene-database` | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils` (db=gene) | 无（建议 Email） | 必需 |
| 19 | `skill:clinvar-database` | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils` (db=clinvar) | 无（建议 Email） | 必需 |
| 20 | `skill:gwas-database` | `https://www.ebi.ac.uk/gwas/api/` | 无（公开） | 必需 |
| 21 | `skill:cosmic-database` | `https://cancer.sanger.ac.uk/cosmic-api/` | 需注册 | 必需 |
| 22 | `skill:pdb-database` | `https://data.rcsb.org/rest/v1/core/entry/` | 无（公开） | 必需 |

### 2.5 知识图谱数据库

| # | 子技能 | 实际 API endpoint | 认证 | 必需性 |
|---|--------|------------------|------|--------|
| 23 | `skill:primekg` | 本地 CSV 文件（下载源：`https://storage.googleapis.com/maayanlab/primekg/`） | 无 | 可选（下载时需要） |

---

## 三、Web 搜索基础设施（必需）

> 系统 `harness/tools/web_search.py` 通过 `ddgs` 库（9.14.4）调用 DuckDuckGo。skill 的 Knowledge Gate 触发外部检索时依赖此通道。

| # | 用途 | 协议/端口 | 域名/IP | 路径 | 必需性 |
|---|------|----------|---------|------|--------|
| 24 | DuckDuckGo 主搜索 | HTTPS 443 | duckduckgo.com | / | 必需 |
| 25 | DuckDuckGo HTML 备用 | HTTPS 443 | html.duckduckgo.com | /html/ | 必需 |

---

## 四、Python 包管理（必需）

> harness 容器 `~/.config/pip/pip.conf` 已配置为清华源 `https://pypi.tuna.tsinghua.edu.cn/simple`。

| # | 用途 | 协议/端口 | 域名/IP | 路径 | 必需性 |
|---|------|----------|---------|------|--------|
| 26 | 清华 PyPI 镜像（已配置） | HTTPS 443 | pypi.tuna.tsinghua.edu.cn | /simple | 必需 |
| 27 | 官方 PyPI 索引（fallback） | HTTPS 443 | pypi.org | /simple | 可选 |
| 28 | PyPI 文件托管（fallback） | HTTPS 443 | files.pythonhosted.org | /packages/ | 可选 |

---

## 五、标准参考文档（可选，skill 文件中引用但未直接调用 API）

| # | 用途 | 协议/端口 | 域名/IP | 必需性 |
|---|------|----------|---------|--------|
| 29 | CDISC 标准（SDTM/ADaM） | HTTPS 443 | www.cdisc.org | 可选 |
| 30 | MedDRA 术语 | HTTPS 443 | www.meddra.org | 可选 |
| 31 | LOINC 实验室代码 | HTTPS 443 | loinc.org | 可选 |
| 32 | ISO 国家代码 | HTTPS 443 | www.iso.org | 可选 |
| 33 | WHO ATC/DDD 索引 | HTTPS 443 | www.whocc.no | 可选 |

---

## 六、最终最小必要白名单（推荐给网管）

以下 23 个域名是「跑通 healthsim-trialsim」所必需的最小集合：

```
# ── 用户指定的 5 个核心医学数据源 ──
api.fda.gov                            # openFDA AE/Label
dailymed.nlm.nih.gov                   # DailyMed SPL
clinicaltrials.gov                     # ClinicalTrials.gov API
rxnav.nlm.nih.gov                      # RxNorm/RxNav
platform.opentargets.org               # Open Targets 下载

# ── 17 个子技能对应的实际 API endpoint ──
eutils.ncbi.nlm.nih.gov                # PubMed, ClinVar, Gene (NCBI E-utilities)
pubchem.ncbi.nlm.nih.gov               # PubChem 化合物查询
go.drugbank.com                        # DrugBank 药物数据库
www.ebi.ac.uk                          # ChEMBL, GWAS Catalog (EBI API)
rest.uniprot.org                       # UniProt 蛋白质数据库
string-db.org                          # STRING 蛋白互作网络
rest.kegg.jp                           # KEGG 通路数据库
reactome.org                           # Reactome 通路数据库
api.openalex.org                       # OpenAlex 学术数据库
api.platform.opentargets.org           # OpenTargets GraphQL API
cancer.sanger.ac.uk                    # COSMIC 癌症基因组数据库
data.rcsb.org                          # PDB/RCSB 蛋白结构数据库

# ── Web 搜索基础设施 ──
duckduckgo.com                         # web_search (DDG)
html.duckduckgo.com                    # web_search (DDG HTML)

# ── Python 包管理 ──
pypi.tuna.tsinghua.edu.cn              # pip 清华源
```

### 可选 fallback（建议一并加入）

```
pypi.org                               # 官方 PyPI fallback
files.pythonhosted.org                 # PyPI 文件 fallback
storage.googleapis.com                 # PrimeKG 下载源（如需）
```

### 标准参考文档（可选）

```
www.cdisc.org                          # CDISC 标准
www.meddra.org                         # MedDRA 术语
loinc.org                              # LOINC 实验室代码
www.iso.org                            # ISO 国家代码
www.whocc.no                           # WHO ATC/DDD 索引
```

---

## 七、核对说明

### 7.1 用户提供的 API 总览 11 项，全部已纳入本清单

| # | 数据库 | Base URL | 在本清单的位置 |
|---|--------|----------|---------------|
| 1 | ClinicalTrials.gov | https://clinicaltrials.gov/api/v2/studies | 第一节 #3、第二节 #6 |
| 2 | PubMed (NCBI) | https://eutils.ncbi.nlm.nih.gov/entrez/eutils | 第二节 #7 |
| 3 | FDA (openFDA) | https://api.fda.gov/ | 第一节 #1、第二节 #8 |
| 4 | OpenTargets | https://api.platform.opentargets.org/api/v4/graphql | 第二节 #13 |
| 5 | DrugBank | https://go.drugbank.com/api/v1/ | 第二节 #10 |
| 6 | ChEMBL | https://www.ebi.ac.uk/chembl/api/data/ | 第二节 #11 |
| 7 | UniProt | https://rest.uniprot.org/uniprotkb/ | 第二节 #12 |
| 8 | STRING | https://string-db.org/api/ | 第二节 #15 |
| 9 | KEGG | https://rest.kegg.jp/ | 第二节 #16 |
| 10 | Reactome | https://reactome.org/ContentService/ | 第二节 #17 |
| 11 | OpenAlex | https://api.openalex.org/ | 第二节 #9 |

### 7.2 之前清单中遗漏、本次补齐的 10 个 API/子技能

| 遗漏项 | 实际 API endpoint | 子技能 |
|--------|------------------|--------|
| DrugBank | go.drugbank.com | `skill:drugbank-database` |
| UniProt | rest.uniprot.org | `skill:uniprot-database` |
| STRING | string-db.org | `skill:string-database` |
| KEGG | rest.kegg.jp | `skill:kegg-database` |
| Reactome | reactome.org | `skill:reactome-database` |
| OpenAlex | api.openalex.org | `skill:openalex-database` |
| ClinVar | eutils.ncbi.nlm.nih.gov (db=clinvar) | `skill:clinvar-database` |
| COSMIC | cancer.sanger.ac.uk | `skill:cosmic-database` |
| PDB/RCSB | data.rcsb.org | `skill:pdb-database` |
| GWAS Catalog | www.ebi.ac.uk/gwas/api/ | `skill:gwas-database` |

### 7.3 子技能与 API endpoint 的完整映射

healthsim-trialsim 的 orchestrator.yaml 和 worker-*.yaml 引用 17 个 `skill:*-database` 子技能，每个子技能对应一个实际的外部 API endpoint：

| 子技能 | 实际 API endpoint | 域名 |
|--------|------------------|------|
| `skill:clinicaltrials-database` | `https://clinicaltrials.gov/api/v2/studies` | clinicaltrials.gov |
| `skill:pubmed-database` | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils` (db=pubmed) | eutils.ncbi.nlm.nih.gov |
| `skill:fda-database` | `https://api.fda.gov/drug/label.json` | api.fda.gov |
| `skill:opentargets-database` | `https://api.platform.opentargets.org/api/v4/graphql` | api.platform.opentargets.org |
| `skill:drugbank-database` | `https://go.drugbank.com/api/v1/` | go.drugbank.com |
| `skill:chembl-database` | `https://www.ebi.ac.uk/chembl/api/data/` | www.ebi.ac.uk |
| `skill:uniprot-database` | `https://rest.uniprot.org/uniprotkb/` | rest.uniprot.org |
| `skill:string-database` | `https://string-db.org/api/` | string-db.org |
| `skill:kegg-database` | `https://rest.kegg.jp/` | rest.kegg.jp |
| `skill:reactome-database` | `https://reactome.org/ContentService/` | reactome.org |
| `skill:openalex-database` | `https://api.openalex.org/` | api.openalex.org |
| `skill:gene-database` | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils` (db=gene) | eutils.ncbi.nlm.nih.gov |
| `skill:clinvar-database` | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils` (db=clinvar) | eutils.ncbi.nlm.nih.gov |
| `skill:gwas-database` | `https://www.ebi.ac.uk/gwas/api/` | www.ebi.ac.uk |
| `skill:cosmic-database` | `https://cancer.sanger.ac.uk/cosmic-api/` | cancer.sanger.ac.uk |
| `skill:pdb-database` | `https://data.rcsb.org/rest/v1/core/entry/` | data.rcsb.org |
| `skill:primekg` | 本地 CSV 文件（下载源：`https://storage.googleapis.com/maayanlab/primekg/`） | storage.googleapis.com |

---

## 八、给网管的最终清单（粘贴版）

```
# healthsim-trialsim Skill 最小网络白名单
# 所有条目均为 HTTPS 443

# ── 核心医学数据源（用户指定）──
api.fda.gov
dailymed.nlm.nih.gov
clinicaltrials.gov
rxnav.nlm.nih.gov
platform.opentargets.org

# ── skill 引用的 17 个子技能对应的实际 API endpoint ──
eutils.ncbi.nlm.nih.gov
pubchem.ncbi.nlm.nih.gov
go.drugbank.com
www.ebi.ac.uk
rest.uniprot.org
string-db.org
rest.kegg.jp
reactome.org
api.openalex.org
api.platform.opentargets.org
cancer.sanger.ac.uk
data.rcsb.org

# ── Web 搜索基础设施 ──
duckduckgo.com
html.duckduckgo.com

# ── Python 包管理 ──
pypi.tuna.tsinghua.edu.cn

# ── 可选 fallback ──
pypi.org
files.pythonhosted.org
storage.googleapis.com

# ── 标准参考文档（可选）──
www.cdisc.org
www.meddra.org
loinc.org
www.iso.org
www.whocc.no
```
