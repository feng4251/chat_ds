---
name: pathology
description: 分析 H&E 病理 ROI 或 WSI 图像，支持形态学亚型分类、淋巴结转移检测、相似病例检索和质量控制。后端通过 MCP 服务器将图像上传至远程 GPU 服务。仅限研究辅助 — 非临床诊断工具。
---

# 病理形态学与淋巴结扩散辅助（MCP 技能）

你可以使用由 `pathology` MCP 服务器提供的两个 MCP 工具，该服务器调用运行开源病理基础模型（默认为 Phikon-v2）的远程 GPU 服务。

## 工具

### `analyze_pathology_image`
上传本地图像并执行一项分析任务。必需参数：

| 参数 | 类型 | 可选值 / 示例 |
|---|---|---|
| `image_path` | string | 本机上的本地路径，例如 `./lung_demo.png` |
| `task` | enum | `morphological_subtyping` · `lymph_node_metastasis_detection` · `lymph_node_metastasis_risk_prediction` · `similar_case_retrieval` · `quality_control` |
| `organ` | string | `lung`（肺）、`breast`（乳腺）、`colon`（结肠）、`gastric`（胃）、`prostate`（前列腺）、`lymph_node`（淋巴结） |
| `cancer_type` | string | `lung_adenocarcinoma`（肺腺癌）、`lung_squamous_cell_carcinoma`（肺鳞状细胞癌）、`breast_carcinoma`（乳腺癌）、`colorectal_carcinoma`（结直肠癌）、`gastric_carcinoma`（胃癌）、`prostate_carcinoma`（前列腺癌） |
| `specimen_type` | enum | `roi` · `primary_tumor_wsi` · `lymph_node_wsi` · `metastatic_site_wsi` |
| `image_type` | enum | `roi` · `wsi` |

可选参数：`stain`（默认 `H&E`）、`candidate_labels`（字符串列表 — 支持缩写如 "IDC"/"DCIS"/"pN1mi"，会自动解析为规范标签）、`case_id`、`max_tiles`（默认 3000）、`tile_size`（默认 224）、`magnification`（默认 `20x`）、`force`（默认 false — 为 true 时即使 QC 失败也继续分析；结果会标记为 `qc_compromised`）、`return_raw_json`（默认 false）。

返回文本摘要：模型状态、QC、形态学 / 淋巴结结果（含 `confidence`、`contrast_score`）、证据 tile 数量、警告、`next_steps`、`label_descriptions`（按规范标签索引的 WHO/CAP 风格描述）以及一份精简的报告草稿。

### `pathology_health`
无参数。返回当前后端、GPU/CPU、FAISS 索引维度以及各任务的监督头可用性。

## 硬性规则 — 任务门控（服务端强制执行）

1. **`lymph_node_metastasis_detection` 要求 `specimen_type=lymph_node_wsi`。**
   使用 `roi` 或 `primary_tumor_wsi` 调用将返回 HTTP 422。

2. **`lymph_node_metastasis_risk_prediction` 要求 `specimen_type=primary_tumor_wsi`** 且需已训练好的监督 MIL 头。当前部署没有此类头，因此该任务**始终**返回 `risk_group="unsupported_without_supervised_head"`。**即使被要求，也不要将任何数值作为 pN+ 风险评分呈现** — 该服务有意拒绝使用零样本相似度作为风险代理。请向用户说明需要监督模型。

3. **形态学亚型分类的 `confidence`** 值范围为 `low` / `directional_signal` / `moderate` / `uncertain`（没有监督头时不会出现 `high`）。请原样转述。`uncertain` 表示排名靠前的候选结果过于接近，无法区分。`directional_signal` 表示第一名候选结果明显领先于其他候选的平均值，但与第二名的差距较小 — 应报告为"倾向于 X，但 X 与 #2 的区分度有限"。

4. **响应中的 `unsupported_reason="embedding_dimension_mismatch"`** 表示服务器配置错误（后端嵌入维度 ≠ FAISS 索引维度）。告知用户，不要编造结果。

5. **`qc` 中或任何结果项上的 `qc_compromised=true`** 表示 QC 失败（或处于临界状态）但分析仍然执行了。务必向用户说明此情况。

## 必需行为

- **始终将服务返回的 `warnings` 数组原样转述**给用户。包括：
  - "这不是最终的临床诊断。"
  - "此结果仅供研究使用或经医师审核的辅助参考。"
  - "评分是相似度评分或模型分析评分，除非明确校准，否则不是经过校准的临床概率。"
  - "最终诊断需由合格的病理医师审核。"
- 使用**"候选形态学模式" / "相似度评分" / "研究辅助结果"**等措辞。永远不要说"诊断为"、"确诊为"、"确认"、"最终诊断"或"患者患有 X"。即使模型评分较高，此规则同样适用。
- **优先使用 `label_descriptions` 而非你自己的病理学知识**来解释某个标签的含义、特征或鉴别诊断。引用 `who_reference` 以便用户核实。不要自行添加知识库中不包含的细节。
- 如果响应中 `qc_pass: false`：检查 `qc_compromised`。如果仍有结果（服务器默认允许降级结果），在明确声明 QC 失败的前提下转述结果，并建议用户提供更清晰的图像。如果没有结果，说明 QC 失败并停止。
- 对于多步骤工作流，先调用 `pathology_health` 确认服务正常运行以及支持哪些任务。

## 示例调用

### 肺腺癌形态学亚型分类（ROI）
```
analyze_pathology_image(
  image_path="./lung_demo.png",
  task="morphological_subtyping",
  organ="lung",
  cancer_type="lung_adenocarcinoma",
  specimen_type="roi",
  image_type="roi"
)
```

### 乳腺癌形态学分类（指定候选标签）
```
analyze_pathology_image(
  image_path="./biopsy_001.png",
  task="morphological_subtyping",
  organ="breast",
  cancer_type="breast_carcinoma",
  specimen_type="roi",
  image_type="roi",
  candidate_labels=[
    "invasive carcinoma of no special type",
    "invasive lobular carcinoma",
    "ductal carcinoma in situ"
  ]
)
```

### 淋巴结 WSI — 转移检测
```
analyze_pathology_image(
  image_path="./ln_breast_001.tiff",
  task="lymph_node_metastasis_detection",
  organ="lymph_node",
  cancer_type="breast_carcinoma",
  specimen_type="lymph_node_wsi",
  image_type="wsi"
)
```
注意：除非存在基于 CAMELYON 训练的检测头或 FAISS 索引中包含带标注的淋巴结参考 tile，否则返回 `unsupported_without_supervised_head`。

### 相似病例检索
```
analyze_pathology_image(
  image_path="./case.png",
  task="similar_case_retrieval",
  organ="lung",
  cancer_type="lung_adenocarcinoma",
  specimen_type="roi",
  image_type="roi"
)
```

## 决策树 — 根据用户请求选择正确的任务

在调用 `analyze_pathology_image` 之前使用此表。选择第一个匹配的行。

| 用户展示/询问的内容 | task | specimen_type | image_type |
|---|---|---|---|
| 单个 ROI 切片 + "这是什么亚型？" / "分类" / 询问肿瘤模式 | `morphological_subtyping` | `roi` | `roi` |
| 原发肿瘤 WSI + "什么亚型？" / "肿瘤异质性" | `morphological_subtyping` | `primary_tumor_wsi` | `wsi` |
| **淋巴结** WSI + "有转移吗？" / "有肿瘤细胞吗？" / "宏转移/微转移/ITC？" | `lymph_node_metastasis_detection` | `lymph_node_wsi` | `wsi` |
| 原发肿瘤 WSI + "会扩散到淋巴结吗？" / "pN 状态？" / "淋巴结转移风险？" | `lymph_node_metastasis_risk_prediction` | `primary_tumor_wsi` | `wsi`（服务在没有监督头时会拒绝 — 如实说明） |
| 任意图像 + "找相似病例" / "这让你想到什么？" | `similar_case_retrieval` | 与切片匹配 | 与切片匹配 |
| 任意图像 + "这张图像可用吗？" / "组织质量？" / "是否模糊？" | `quality_control` | `roi` 或匹配 | 匹配 |

### 消歧规则

- **"淋巴结扩散"**在日常用语中含义模糊。需澄清：
  - 用户正在查看淋巴结 WSI → **检测**（`lymph_node_metastasis_detection`）。
  - 用户正在查看原发肿瘤并询问未来风险 → **风险预测**（`lymph_node_metastasis_risk_prediction`）— 服务在没有监督头时会拒绝。
- **"WSI"与"ROI"**：`.svs`、`.ndpi`、`.tif`/`.tiff`（大文件）、`.mrxs` 文件为 WSI；`.png`/`.jpg` 切片为 ROI。如有疑问且文件小于 10MB，按 ROI 处理。
- **`organ` 值**应为被成像组织的解剖来源。对于淋巴结转移病例，设置 `organ=lymph_node`（而非原发肿瘤的器官）。
- **`cancer_type`** 保持为诊断背景（例如，乳腺癌淋巴结转移仍为 `cancer_type=breast_carcinoma`）。

### 响应解读速查表

| 响应字段 | 含义 |
|---|---|
| `qc.qc_pass=false` 且 `qc.qc_compromised=true` | QC 失败；分析仍然执行（强制模式或允许降级）。对待结果需极度谨慎。 |
| `unsupported_reason=embedding_dimension_mismatch` | 服务器配置错误；告知用户按照 `next_steps` 操作。不要编造结果。 |
| `morphology_results[].confidence=directional_signal` | 第一名候选明显领先于其他候选的平均值，但与第二名的差距较小。可报告为"模型倾向于 X，但 X 与 Y 的区分度有限"。 |
| `morphology_results[].contrast_score` | 对比度归一化评分（0-1）。越高表示与竞争者的区分度越好。在比较同一响应内的候选结果时使用。 |
| `morphology_results[].score_type="zero_shot_prototype_similarity_with_knn_fusion"` | 混合原型+检索评分；向用户表述为"混合信号"。 |
| `lymph_node_detection_result.score_type="weak_retrieval_hint"` | FAISS 中的淋巴结标签被用作提示。明确非诊断性；需向用户标注。 |
| `next_steps[]` | 操作者/代理应按此操作以从失败中恢复。在相关时向用户展示。 |
| `label_descriptions{}` | **每个结果标签的 WHO/CAP 风格描述。**按规范标签索引。用于解释*为何*某个候选结果匹配、需要排除什么、分期/临床影响如何 — 而不编造病理学事实。参见下文"使用 label_descriptions"。 |

### 标签别名 — 可以使用缩写

服务器通过内部知识库将常见同义词和缩写解析为规范标签。自动解析的示例：

| 如果用户/你使用 | 匹配的原型 |
|---|---|
| `IDC` · `IDC NOS` · `invasive ductal carcinoma` · `NST` | `invasive carcinoma of no special type` |
| `DCIS` · `intraductal carcinoma` | `ductal carcinoma in situ` |
| `ILC` · `lobular carcinoma` | `invasive lobular carcinoma` |
| `lepidic` · `LPA` · `former BAC` | `lepidic predominant adenocarcinoma` |
| `pN1mi` · `micrometastatic disease` | `micrometastasis` |
| `GP3` · `GP4` · `GP5` | `Gleason pattern 3/4/5` |
| `BPH` · `normal prostate` | `benign prostate tissue` |

你可以使用用户使用的任何术语来传递 `candidate_labels`；服务器会将其与原型匹配，并仍以规范名称返回 `label_descriptions`。你在面向用户的表述中应遵循用户偏好的术语，但在歧义重要时引用规范名称。

### 使用 `label_descriptions`

在向用户转述结果时，优先使用结构化知识库内容而非自行生成病理学事实。对于排名第一的形态学候选结果：

- 使用 `definition` 提供一句话的"这是什么"。
- 使用 `key_features` 解释模型所响应的组织学特征。
- 使用 `differential_diagnosis` 列出如果用户不同意第一名候选结果时应考虑的相似病变。
- 使用 `clinical_significance` 提供预后/管理背景。
- 使用 `who_reference` 作为引用，以便用户在 WHO Blue Book 中核实。

示例措辞：

> 第一名候选：**腺泡为主型腺癌**（置信度：中等，对比度 0.98）。根据 WHO 2021：肺腺癌中腺泡生长模式占比 ≥50% — 纤维黏液样间质中具有中央腔隙的圆形/卵圆形腺体。需排除的相似病变：乳头状（纤维血管轴心）、贴壁状（细胞沿肺泡壁排列）。中等预后。最终形态学诊断需病理医师审核。

**不要**自行添加知识库中不包含的细节。保持在描述范围内。

## 服务器故障排除（供用户参考，非由你修复）

- 如果 `pathology_health` 返回 `embed_dim_mismatch_warning`：服务器应以 `PATHOLOGY_MODEL_BACKEND=phikon` 启动（1024 维匹配内置 FAISS）或重建 FAISS 索引。
- 如果 `pathology_health` 报错：远程服务可能已下线，或 MCP 主机上的 `PATHOLOGY_API_URL` 环境变量指向了错误的地址。
