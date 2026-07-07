"""
MCP (Model Context Protocol) server adapter for the Pathology Morphology & LN
Spread Assist service.

Runs on the *client* host (where the markdown-only agent harness lives) as a
stdio MCP server. It exposes two tools backed by HTTP calls to the GPU server:

  - analyze_pathology_image: upload + run a research-assistance analysis
  - pathology_health:        report active backend, GPU, FAISS, task support

Configuration (env vars):
    PATHOLOGY_API_URL      Remote service base URL.
                           Default: http://10.10.132.126:18018
    PATHOLOGY_API_TIMEOUT  HTTP timeout in seconds. Default: 600

Research-use only. NOT a clinical diagnostic system.

----------------------------------------------------------------------
Install on the client host (no torch / no CUDA needed):

    pip install "mcp[cli]>=1.0" requests

Register with your MCP-aware harness. Example for Claude Code / Desktop
(`~/.config/claude/claude_desktop_config.json` or equivalent):

    {
      "mcpServers": {
        "pathology": {
          "command": "python",
          "args": ["/abs/path/to/mcp_server/pathology_mcp.py"],
          "env": {
            "PATHOLOGY_API_URL": "http://<gpu-server>:18018"
          }
        }
      }
    }

The harness then auto-discovers analyze_pathology_image and pathology_health,
and the LLM can call them like any other tool.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional

import requests
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config & logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] pathology_mcp: %(message)s",
)
logger = logging.getLogger("pathology_mcp")

API_URL = os.environ.get("PATHOLOGY_API_URL", "http://10.10.132.126:18018").rstrip("/")
API_TIMEOUT = float(os.environ.get("PATHOLOGY_API_TIMEOUT", "600"))

_ANALYZE_UPLOAD = f"{API_URL}/v1/pathology/analyze-upload"
_HEALTH = f"{API_URL}/health"

_VALID_IMAGE_TYPES = {"roi", "wsi"}
_VALID_SPECIMEN_TYPES = {
    "primary_tumor_wsi",
    "lymph_node_wsi",
    "metastatic_site_wsi",
    "roi",
}
_VALID_TASKS = {
    "morphological_subtyping",
    "lymph_node_metastasis_detection",
    "lymph_node_metastasis_risk_prediction",
    "similar_case_retrieval",
    "quality_control",
}

mcp = FastMCP("pathology")


# ---------------------------------------------------------------------------
# Compact result rendering
# ---------------------------------------------------------------------------
def _summarize(resp: dict) -> str:
    lines: List[str] = []
    lines.append(f"case_id: {resp.get('case_id')}")
    lines.append(f"status: {resp.get('status')}  task: {resp.get('task')}")
    lines.append(
        f"specimen_type: {resp.get('specimen_type')}  organ: {resp.get('organ')}"
    )
    lines.append(f"validated_scope: {resp.get('validated_scope')}")

    ms = resp.get("model_status") or {}
    lines.append(
        f"model: backend={ms.get('active_backend')} dim={ms.get('embed_dim')} "
        f"device={ms.get('device')} degraded={ms.get('degraded')}"
    )

    qc = resp.get("qc") or {}
    compromised_str = ""
    if qc.get("qc_compromised"):
        compromised_str = " COMPROMISED"
    lines.append(
        f"QC: pass={qc.get('qc_pass')}{compromised_str} "
        f"tissue_ratio={qc.get('tissue_ratio')} blur={qc.get('blur_score')}"
    )

    if resp.get("unsupported_reason"):
        lines.append(f"unsupported_reason: {resp['unsupported_reason']}")

    morph = resp.get("morphology_results")
    if morph:
        lines.append("morphology_results (top 5):")
        for r in morph[:5]:
            contrast = r.get("contrast_score")
            contrast_str = f" contrast={contrast:.3f}" if isinstance(contrast, (int, float)) else ""
            lines.append(
                f"  - {r['label']}: score={r['score']:.4f}{contrast_str} "
                f"[{r.get('confidence', 'uncertain')}] ({r.get('score_type')})"
            )

    ld = resp.get("lymph_node_detection_result")
    if ld:
        lines.append(
            f"lymph_node_detection: status={ld.get('status')} "
            f"score={ld.get('score')} type={ld.get('score_type')} "
            f"confidence={ld.get('confidence')}"
        )
        if ld.get("reason"):
            lines.append(f"  reason: {ld['reason']}")

    lr = resp.get("lymph_node_risk_result")
    if lr:
        lines.append(
            f"lymph_node_risk: group={lr.get('risk_group')} "
            f"score={lr.get('risk_score')} type={lr.get('score_type')}"
        )
        if lr.get("reason"):
            lines.append(f"  reason: {lr['reason']}")

    rr = resp.get("retrieval_results") or []
    if rr:
        lines.append(f"retrieval_results (top 3 of {len(rr)}):")
        for r in rr[:3]:
            lines.append(
                f"  - {r.get('label')} (source={r.get('source')}, "
                f"similarity={r.get('similarity')})"
            )

    ev = resp.get("evidence_tiles") or []
    lines.append(f"evidence_tiles: {len(ev)}")

    # next_steps — actionable items for the agent / user.
    ns = resp.get("next_steps") or []
    if ns:
        lines.append("next_steps:")
        for s in ns:
            lines.append(f"  -> {s}")

    # label_descriptions — WHO/CAP-style descriptions for the top result(s).
    # Show full detail for the top-1 morphology label, condensed for the rest.
    label_desc = resp.get("label_descriptions") or {}
    if label_desc:
        # Resolve the top-1 morphology label's canonical (if any) so we can
        # expand its full description.
        top_canonical = None
        if morph:
            # The response top-1 label may itself be an alias; the description
            # entry's canonical_label is the key. Find by query match.
            top_label = morph[0]["label"]
            for canon, d in label_desc.items():
                if d.get("query", "").lower() == top_label.lower() or canon.lower() == top_label.lower():
                    top_canonical = canon
                    break
            if top_canonical is None:
                top_canonical = next(iter(label_desc.keys()), None)

        lines.append("label_descriptions:")
        for canon, d in label_desc.items():
            tag = "[TOP]" if canon == top_canonical else ""
            lines.append(f"  - {canon} {tag}".rstrip())
            if canon == top_canonical:
                # Full detail for the top result
                if d.get("definition"):
                    lines.append(f"      definition: {d['definition'].strip()[:280]}")
                if d.get("key_features"):
                    lines.append("      key_features:")
                    for kf in d["key_features"][:4]:
                        lines.append(f"        * {kf}")
                if d.get("differential_diagnosis"):
                    lines.append("      differential_diagnosis:")
                    for ddx in d["differential_diagnosis"][:3]:
                        lines.append(f"        * {ddx}")
                if d.get("clinical_significance"):
                    lines.append(f"      clinical_significance: {d['clinical_significance'].strip()[:200]}")
                if d.get("who_reference"):
                    lines.append(f"      who_reference: {d['who_reference']}")
            else:
                # Brief for the rest — just the definition opener
                defn = (d.get("definition") or "").strip().replace("\n", " ")
                if defn:
                    lines.append(f"      {defn[:140]}{'...' if len(defn) > 140 else ''}")

    warns = resp.get("warnings") or []
    if warns:
        lines.append("warnings:")
        for w in warns:
            lines.append(f"  * {w}")

    if resp.get("report_draft"):
        lines.append("")
        lines.append("report_draft:")
        lines.append(resp["report_draft"])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def analyze_pathology_image(
    image_path: str,
    task: str,
    organ: str,
    cancer_type: str,
    specimen_type: str = "roi",
    image_type: str = "roi",
    stain: str = "H&E",
    candidate_labels: Optional[List[str]] = None,
    case_id: Optional[str] = None,
    max_tiles: int = 3000,
    tile_size: int = 224,
    magnification: str = "20x",
    force: bool = False,
    return_raw_json: bool = False,
) -> str:
    """Analyze a pathology image (ROI or WSI) on the remote research service.

    Uploads the local image file at `image_path` to the remote pathology service
    and returns a research-assistance analysis. This is NOT a clinical diagnosis.

    Args:
        image_path: Local path on THIS host to the ROI/WSI image to upload.
        task: One of morphological_subtyping, lymph_node_metastasis_detection,
            lymph_node_metastasis_risk_prediction, similar_case_retrieval,
            quality_control.
        organ: Anatomical site, e.g. lung, breast, colon, gastric, prostate,
            lymph_node.
        cancer_type: e.g. lung_adenocarcinoma, breast_carcinoma,
            colorectal_carcinoma.
        specimen_type: primary_tumor_wsi, lymph_node_wsi, metastatic_site_wsi,
            or roi. NOTE: lymph_node_metastasis_detection REQUIRES
            lymph_node_wsi; lymph_node_metastasis_risk_prediction REQUIRES
            primary_tumor_wsi and refuses without a supervised MIL head.
        image_type: roi or wsi.
        stain: Stain type, default H&E.
        candidate_labels: Optional explicit label list; omit to use server's
            ontology defaults for the organ/cancer_type/task.
        case_id: Optional case identifier; auto-derived from filename if absent.
        max_tiles: Max tiles for WSI tiling (default 3000).
        tile_size: Tile size in px (default 224).
        magnification: Target magnification (default 20x).
        force: If True, run analysis even when QC fails. Results are marked
            qc_compromised=true (default False).
        return_raw_json: If True return the full JSON response instead of a
            compact text summary.
    """
    p = Path(image_path)
    if not p.exists():
        return f"ERROR: image file not found on this host: {image_path}"

    if image_type not in _VALID_IMAGE_TYPES:
        return f"ERROR: image_type must be one of {sorted(_VALID_IMAGE_TYPES)}"
    if specimen_type not in _VALID_SPECIMEN_TYPES:
        return f"ERROR: specimen_type must be one of {sorted(_VALID_SPECIMEN_TYPES)}"
    if task not in _VALID_TASKS:
        return f"ERROR: task must be one of {sorted(_VALID_TASKS)}"

    if not case_id:
        case_id = p.stem

    data = {
        "case_id": case_id,
        "image_type": image_type,
        "specimen_type": specimen_type,
        "organ": organ,
        "cancer_type": cancer_type,
        "stain": stain,
        "task": task,
        "mode": "research_assist",
        "max_tiles": str(max_tiles),
        "tile_size": str(tile_size),
        "magnification": magnification,
        "force": "true" if force else "false",
    }
    if candidate_labels:
        data["candidate_labels"] = json.dumps(candidate_labels)

    try:
        with open(p, "rb") as fh:
            files = {"file": (p.name, fh)}
            r = requests.post(
                _ANALYZE_UPLOAD, data=data, files=files, timeout=API_TIMEOUT
            )
    except requests.RequestException as exc:
        return f"ERROR: failed to reach pathology service at {API_URL}: {exc}"

    if r.status_code != 200:
        detail = r.text
        try:
            detail = r.json().get("detail", detail)
        except Exception:
            pass
        return f"ERROR: service returned HTTP {r.status_code}: {detail}"

    resp = r.json()
    if return_raw_json:
        return json.dumps(resp, ensure_ascii=False, indent=2)
    return _summarize(resp)


@mcp.tool()
def pathology_health() -> str:
    """Check the remote pathology service health and per-task support.

    Returns active model backend, embedding dimension, device, FAISS index
    availability, and which tasks have supervised heads installed.
    """
    try:
        r = requests.get(_HEALTH, timeout=30)
    except requests.RequestException as exc:
        return f"ERROR: failed to reach pathology service at {API_URL}: {exc}"
    if r.status_code != 200:
        return f"ERROR: health check HTTP {r.status_code}: {r.text}"
    return json.dumps(r.json(), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    logger.info(f"Pathology MCP server starting (remote service: {API_URL})")
    mcp.run()
