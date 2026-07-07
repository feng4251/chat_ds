---
name: pathology
description: Analyze H&E pathology ROI or WSI images for morphological subtyping, lymph node metastasis detection, similar-case retrieval, and QC. Backed by an MCP server that uploads images to a remote GPU service. Research-assistance only — NOT a clinical diagnostic tool.
---

# Pathology Morphology & Lymph Node Spread Assist (MCP skill)

You have access to two MCP tools, exposed by the `pathology` MCP server, that
call a remote GPU service running open-source pathology foundation models
(Phikon-v2 by default).

## Tools

### `analyze_pathology_image`
Uploads a local image and runs one analysis task. Required args:

| arg | type | values / examples |
|---|---|---|
| `image_path` | string | local path on this host, e.g. `./lung_demo.png` |
| `task` | enum | `morphological_subtyping` · `lymph_node_metastasis_detection` · `lymph_node_metastasis_risk_prediction` · `similar_case_retrieval` · `quality_control` |
| `organ` | string | `lung`, `breast`, `colon`, `gastric`, `prostate`, `lymph_node` |
| `cancer_type` | string | `lung_adenocarcinoma`, `lung_squamous_cell_carcinoma`, `breast_carcinoma`, `colorectal_carcinoma`, `gastric_carcinoma`, `prostate_carcinoma` |
| `specimen_type` | enum | `roi` · `primary_tumor_wsi` · `lymph_node_wsi` · `metastatic_site_wsi` |
| `image_type` | enum | `roi` · `wsi` |

Optional: `stain` (default `H&E`), `candidate_labels` (list of strings; omit to use ontology defaults), `case_id`, `max_tiles` (default 3000), `tile_size` (default 224), `magnification` (default `20x`), `return_raw_json` (default false).

Returns a text summary: model status, QC, morphology / LN results, evidence tile count, warnings, and a report draft.

### `pathology_health`
No args. Returns active backend, GPU/CPU, FAISS index dimension, and per-task supervised-head availability.

## Hard rules — task gating (enforced server-side)

1. **`lymph_node_metastasis_detection` requires `specimen_type=lymph_node_wsi`.**
   Calling it with `roi` or `primary_tumor_wsi` returns HTTP 422.

2. **`lymph_node_metastasis_risk_prediction` requires `specimen_type=primary_tumor_wsi`** AND a trained supervised MIL head. The current deployment has no such head, so this task **always** returns `risk_group="unsupported_without_supervised_head"`. **Do not present any number as a pN+ risk score** even if asked — the service intentionally refuses to use zero-shot similarity as a risk proxy. Explain to the user that a supervised model is required.

3. **Morphology subtyping `confidence`** values come back as `low` / `moderate` / `uncertain` (never `high` without a supervised head). Relay them verbatim. `uncertain` means the top candidates are too close to separate.

4. **`unsupported_reason="embedding_dimension_mismatch"`** in the response means the server is misconfigured (backend embedding dim ≠ FAISS index dim). Tell the user; do not fabricate results.

## Required behavior

- **Always relay the service's `warnings` array verbatim** to the user. These include:
  - "This is not a final clinical diagnosis."
  - "This result is for research-use or physician-reviewed assistance only."
  - "Scores are similarity scores or model analysis scores, not calibrated clinical probabilities unless explicitly calibrated."
  - "Final diagnosis requires review by a qualified pathologist."
- Use the language **"candidate morphological pattern" / "similarity score" / "research-assistance result"**. Never say "diagnosis is", "diagnosed as", "confirmed", "final diagnosis", or "patient has X". This rule applies even when the model's score is high.
- If `qc_pass: false` in the response, do not present morphology results — explain QC failed and stop.
- For multi-step workflows, call `pathology_health` first to confirm the service is up and which tasks are supported.

## Example calls

### Lung adenocarcinoma morphology subtyping (ROI)
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

### Breast carcinoma morphology with explicit labels
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

### Lymph node WSI — metastasis detection
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
Note: returns `unsupported_without_supervised_head` unless a CAMELYON-trained
detector head or labeled LN reference tiles in the FAISS index are present.

### Similar-case retrieval
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

## Decision shortcuts

- User shows a tumor ROI and asks "what subtype?" → `morphological_subtyping`, relay top-3 with confidence, attach warnings.
- User says "this is a lymph node, is there metastasis?" → `lymph_node_metastasis_detection`. If response is `unsupported_without_supervised_head`, explain that detection requires a supervised head (CAMELYON-style) and do not guess.
- User asks "will this primary tumor spread to lymph nodes?" → `lymph_node_metastasis_risk_prediction`. Service will refuse. Tell the user honestly: pN+ risk prediction from primary tumor requires a trained supervised MIL head; morphological similarity is not a valid proxy.
- User shows a noisy / blurry image and the response has `qc_pass: false` → stop, explain QC failure.

## Server troubleshooting (for the user, not for you to fix)

- If `pathology_health` returns `embed_dim_mismatch_warning`: server should start with `PATHOLOGY_MODEL_BACKEND=phikon` (1024-dim matches bundled FAISS) or rebuild the FAISS index.
- If `pathology_health` errors out: the remote service may be down, or `PATHOLOGY_API_URL` env var on the MCP host points at the wrong address.
