#!/usr/bin/env python3
"""
SDTM → ADaM Conversion Engine
==============================
Transforms SDTM domain records into ADaM analysis datasets.
Produces: ADSL, ADAE, ADLB, ADEFF, ADTTE

Derivation rules are based on ADaM IG 1.2 and CDISC standard algorithms.
SDTM domain names, variable names, and controlled terminology are sourced
from domain_parser.py (single source of truth). ADaM dataset names and
efficacy parameter mappings remain protocol-level parameters.

Usage:
  python sdtm_to_adam.py --input sdtm_json/ --output adam_json/
  python sdtm_to_adam.py --input sdtm_json/ --output adam_json/ --datasets ADSL,ADAE
"""

import json
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

# ── Import shared domain parser (single source of truth) ──────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from domain_parser import DomainParser


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════

# ── Lazy DomainParser reference (avoids re-parsing domain files on every call) ──
_dp_cache: Optional[DomainParser] = None

def _get_dp(project_root: str = None) -> DomainParser:
    """Return a cached DomainParser instance for this process."""
    global _dp_cache
    if _dp_cache is None:
        _dp_cache = DomainParser(project_root) if project_root else DomainParser()
    return _dp_cache


def _armcd_to_n(armcd: str) -> int:
    """Map arm code to numeric value for ADaM TRT01PN/TRT01AN.

    Placebo/control arms get 0; experimental arms get sequential numbers
    starting from 1. Previously hardcoded to only recognize "ACT" and "PBO",
    which broke trials using other arm codes (e.g., RES80 in MASH trials).
    """
    if not armcd:
        return 99
    cd = armcd.upper().strip()
    if cd in ("PBO", "PLACEBO", "PLC", "CONTROL", "CTL", "SOC", "OBS"):
        return 0
    return 1  # experimental — could be extended to multi-arm ordering with context

def load_domain(sdtm_dir: str, domain: str) -> List[Dict]:
    """Load SDTM domain records."""
    path = os.path.join(sdtm_dir, f"{domain.lower()}.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict) and "records" in data:
        return data["records"]
    if isinstance(data, list):
        return data
    return []

def parse_date(date_str: str) -> Optional[datetime]:
    """Parse ISO 8601 date string."""
    if not date_str:
        return None
    try:
        # Handle various ISO 8601 precisions
        date_str = str(date_str)[:10]  # Take YYYY-MM-DD portion
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None

def days_between(d1: Optional[str], d2: Optional[str]) -> Optional[int]:
    """Calculate days between two ISO 8601 dates."""
    pd1, pd2 = parse_date(d1), parse_date(d2)
    if pd1 and pd2:
        return (pd2 - pd1).days
    return None

def safe_float(val: Any) -> Optional[float]:
    """Safely convert to float."""
    try:
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ADSL — Subject-Level Analysis Dataset
# ═══════════════════════════════════════════════════════════════════════════════

def derive_adsl(dm_records: List[Dict], ds_records: List[Dict], ex_records: List[Dict], vs_records: List[Dict], lb_records: List[Dict]) -> List[Dict]:
    """Derive ADSL from DM + DS + EX + VS + LB."""
    adsl_records = []
    ds_by_subject = defaultdict(list)
    for r in ds_records:
        ds_by_subject[r.get("USUBJID", "")].append(r)
    ex_by_subject = defaultdict(list)
    for r in ex_records:
        ex_by_subject[r.get("USUBJID", "")].append(r)

    for dm in dm_records:
        usubjid = dm.get("USUBJID", "")
        adsl = {
            "STUDYID": dm.get("STUDYID"),
            "USUBJID": usubjid,
            "SUBJID": dm.get("SUBJID"),
            "SITEID": dm.get("SITEID"),
            "COUNTRY": dm.get("COUNTRY"),
            # Demographics
            "AGE": dm.get("AGE"),
            "AGEU": dm.get("AGEU"),
            "SEX": dm.get("SEX"),
            "RACE": dm.get("RACE"),
            "ETHNIC": dm.get("ETHNIC"),
            # Age groups
            "AGEGR1": classify_age_group(dm.get("AGE")),
            "AGEGR1N": classify_age_group_n(dm.get("AGE")),
            # Treatment
            "TRT01P": dm.get("ARM"),
            "TRT01PN": _armcd_to_n(dm.get("ARMCD")),
            "TRT01A": dm.get("ACTARM", dm.get("ARM")),
            "TRT01AN": _armcd_to_n(dm.get("ACTARMCD", dm.get("ARMCD"))),
            # Dates
            "RFSTDTC": dm.get("RFSTDTC"),
            "RFENDTC": dm.get("RFENDTC"),
            "RFICDTC": dm.get("RFICDTC"),
            "RFPENDTC": dm.get("RFPENDTC"),
            "DTHFL": dm.get("DTHFL"),
            "DTHDTC": dm.get("DTHDTC"),
            # Treatment duration
            "TRTDURD": days_between(dm.get("RFSTDTC"), dm.get("RFENDTC")),
            # Populations
            "SAFFL": "Y" if usubjid in ex_by_subject and len(ex_by_subject[usubjid]) > 0 else "N",
            "ITTFL": "Y",  # All randomized
            "COMPLFL": "Y" if is_completer(usubjid, ds_by_subject) else "N",
            # Disposition
            "DCSREAS": get_discontinuation_reason(usubjid, ds_by_subject),
            "EOSSTT": get_eos_status(usubjid, ds_by_subject),
        }
        adsl_records.append(adsl)
    return adsl_records

def classify_age_group(age) -> str:
    """Classify age into group."""
    if age is None: return "Missing"
    age = int(age)
    if age < 45: return "<45"
    elif age < 65: return "45-64"
    elif age < 75: return "65-74"
    else: return ">=75"

def classify_age_group_n(age) -> int:
    if age is None: return -1
    age = int(age)
    if age < 45: return 1
    elif age < 65: return 2
    elif age < 75: return 3
    else: return 4

def is_completer(usubjid: str, ds_by_subject: Dict) -> bool:
    for rec in ds_by_subject.get(usubjid, []):
        if rec.get("DSDECOD") == "COMPLETED" and rec.get("DSSCAT") == "STUDY PARTICIPATION":
            return True
    return False

def get_discontinuation_reason(usubjid: str, ds_by_subject: Dict) -> str:
    for rec in ds_by_subject.get(usubjid, []):
        if rec.get("DSDECOD") not in ("COMPLETED", "INFORMED CONSENT OBTAINED", "RANDOMIZED"):
            return rec.get("DSDECOD", "")
    return "COMPLETED"

def get_eos_status(usubjid: str, ds_by_subject: Dict) -> str:
    for rec in ds_by_subject.get(usubjid, []):
        ds_scat = rec.get("DSSCAT", "")
        ds_decode = rec.get("DSDECOD", "")
        if ds_scat == "STUDY PARTICIPATION" and ds_decode != "INFORMED CONSENT OBTAINED":
            return ds_decode
    return "ONGOING"


# ═══════════════════════════════════════════════════════════════════════════════
# ADAE — Adverse Event Analysis Dataset (BDS)
# ═══════════════════════════════════════════════════════════════════════════════

def derive_adae(ae_records: List[Dict], dm_lookup: Dict[str, Dict]) -> List[Dict]:
    """Derive ADAE from AE + DM."""
    adae_records = []
    for ae in ae_records:
        usubjid = ae.get("USUBJID", "")
        dm = dm_lookup.get(usubjid, {})
        rfstdtc = dm.get("RFSTDTC", "")
        ast_days = days_between(rfstdtc, ae.get("AESTDTC"))
        aen_days = days_between(rfstdtc, ae.get("AEENDTC"))
        duration = days_between(ae.get("AESTDTC"), ae.get("AEENDTC"))

        adae = {
            "STUDYID": ae.get("STUDYID"),
            "USUBJID": usubjid,
            "AESEQ": ae.get("AESEQ"),
            # Analysis dates
            "ASTDT": ae.get("AESTDTC", "")[:10] if ae.get("AESTDTC") else "",
            "ASTDTF": "",  # No imputation
            "AENDT": ae.get("AEENDTC", "")[:10] if ae.get("AEENDTC") else "",
            "AENDTF": "",
            # Duration
            "ADURN": duration if duration is not None else "",
            "ADURU": "DAYS" if duration is not None else "",
            # Study day
            "ASTDY": ast_days if ast_days is not None else ae.get("AESTDY"),
            "AENDY": aen_days if aen_days is not None else ae.get("AEENDY"),
            # Flags
            "AOCCFL": "Y",  # First occurrence (assume one record per event per subject)
            "TRTEMFL": "Y" if ast_days is not None and ast_days >= 1 else "N",
            # Severity numeric
            "ASEV": map_severity(ae.get("AESEV")),
            "ASEVN": map_severity_n(ae.get("AESEV")),
            # Causality numeric
            "AREL": map_causality(ae.get("AEREL")),
            "ARELN": map_causality_n(ae.get("AEREL")),
            # Serious
            "SAFFN": 1 if ae.get("AESER") == "Y" else 0,
            # Action
            "AACN": ae.get("AEACN"),
            # Hierarchy
            "AEDECOD": ae.get("AEDECOD"),
            "AEBODSYS": ae.get("AEBODSYS"),
            "AEOUT": ae.get("AEOUT"),
            # Traceability
            "SRCDOM": "AE",
            "SRCVAR": "AESEQ",
            "SRCSEQ": ae.get("AESEQ"),
        }
        adae_records.append(adae)
    return adae_records

def map_severity(sev: str) -> str:
    """Pass-through AESEV — validate against domain_parser CT if available."""
    if not sev:
        return ""
    dp = _get_dp()
    ct = dp.get_controlled_terminology()
    valid_values = ct.get("AESEV", [])
    if valid_values and sev not in valid_values:
        return sev  # still return original if not in CT (tolerate unknown values)
    return sev

def map_severity_n(sev: str) -> int:
    """Map AESEV to numeric rank using domain_parser controlled terminology order."""
    if not sev:
        return -1
    return _get_dp().get_ct_rank("AESEV", sev)

def map_causality(rel: str) -> str:
    """Pass-through AEREL — validate against domain_parser CT if available."""
    if not rel:
        return ""
    return rel

def map_causality_n(rel: str) -> int:
    """Map AEREL to numeric rank using domain_parser controlled terminology order."""
    if not rel:
        return -1
    rank = _get_dp().get_ct_rank("AEREL", rel)
    if rank == -1:
        return -1
    # "NOT RELATED" → 0 (1-based → 0-based offset for this codelist)
    return rank - 1


# ═══════════════════════════════════════════════════════════════════════════════
# ADLB — Laboratory Analysis Dataset (BDS)
# ═══════════════════════════════════════════════════════════════════════════════

def derive_adlb(lb_records: List[Dict], dm_lookup: Dict[str, Dict]) -> List[Dict]:
    """Derive ADLB from LB + DM."""
    adlb_records = []
    # Group LB by subject+test to compute baselines
    baseline_by_subject_test = compute_baselines(lb_records, dm_lookup)

    for lb in lb_records:
        usubjid = lb.get("USUBJID", "")
        testcd = lb.get("LBTESTCD", "")
        visitnum = lb.get("VISITNUM")
        aval = safe_float(lb.get("LBSTRESN"))
        base_val = baseline_by_subject_test.get((usubjid, testcd))

        chg = (aval - base_val) if aval is not None and base_val is not None else None
        pchg = ((aval - base_val) / base_val * 100) if aval is not None and base_val is not None and base_val != 0 else None

        adlb = {
            "STUDYID": lb.get("STUDYID"),
            "USUBJID": usubjid,
            "PARAM": lb.get("LBTEST"),
            "PARAMCD": testcd,
            "AVAL": aval,
            "AVALC": lb.get("LBSTRESC", ""),
            "BASE": base_val,
            "BASEC": str(base_val) if base_val is not None else "",
            "CHG": round(chg, 2) if chg is not None else None,
            "PCHG": round(pchg, 2) if pchg is not None else None,
            "SHIFT1": compute_shift(base_val, aval, testcd, lb.get("LBORNRLO"), lb.get("LBORNRHI")),
            "TOXGR": lb.get("LBTOXGR", ""),
            "TOXGRN": safe_float(lb.get("LBTOXGR", "0")) or 0,
            "ANL01FL": "Y" if visitnum == 10 else "",  # Week 26 = primary visit
            "ADY": lb.get("LBDY", days_between(dm_lookup.get(usubjid, {}).get("RFSTDTC", ""), lb.get("LBDTC", ""))),
            "ADT": (lb.get("LBDTC", "")[:10] if lb.get("LBDTC") else ""),
            "AVISITN": visitnum,
            "AVISIT": lb.get("VISIT", ""),
            "LBNRIND": lb.get("LBNRIND", ""),
            "LBLOINC": lb.get("LBLOINC", ""),
            "SRCDOM": "LB",
            "SRCVAR": "LBSEQ",
            "SRCSEQ": lb.get("LBSEQ"),
        }
        adlb_records.append(adlb)
    return adlb_records

def compute_baselines(lb_records: List[Dict], dm_lookup: Dict[str, Dict]) -> Dict[Tuple, Optional[float]]:
    """Compute baseline values: last non-missing pre-dose value per subject per test."""
    # Sort by subject, test, date
    subject_test_vals = defaultdict(list)
    for lb in lb_records:
        usubjid = lb.get("USUBJID", "")
        testcd = lb.get("LBTESTCD", "")
        dt = lb.get("LBDTC", "")
        rfstdtc = dm_lookup.get(usubjid, {}).get("RFSTDTC", "")
        aval = safe_float(lb.get("LBSTRESN"))
        if aval is not None and dt and rfstdtc and dt <= rfstdtc:
            subject_test_vals[(usubjid, testcd)].append((dt, aval))

    baselines = {}
    for (usubjid, testcd), vals in subject_test_vals.items():
        vals.sort(key=lambda x: x[0], reverse=True)  # Most recent first
        baselines[(usubjid, testcd)] = vals[0][1]  # Last pre-dose value
    return baselines

def compute_shift(base_val: Optional[float], aval: Optional[float], testcd: str, lorlo, orhi) -> str:
    """Compute toxicity grade shift."""
    if base_val is None or aval is None:
        return "Missing→Missing"
    base_tox = classify_toxicity(base_val, testcd, lorlo, orhi)
    anal_tox = classify_toxicity(aval, testcd, lorlo, orhi)
    return f"{base_tox}→{anal_tox}"

def classify_toxicity(val: float, testcd: str, lorlo, orhi) -> str:
    """Simple toxicity grade classification."""
    rlo = safe_float(lorlo)
    rhi = safe_float(orhi)
    if rlo is None and rhi is None:
        return "0"
    if rlo is not None and val < rlo:
        return "1↓"
    if rhi is not None and val > rhi:
        if testcd in ("ALT", "AST", "BILI"):
            if rhi and val > 3 * rhi: return "2+"
            return "1+"
        return "1+"
    return "0"


# ═══════════════════════════════════════════════════════════════════════════════
# ADEFF — Efficacy Analysis Dataset (BDS)
# ═══════════════════════════════════════════════════════════════════════════════

def derive_adeff(lb_records: List[Dict], vs_records: List[Dict], dm_lookup: Dict[str, Dict]) -> List[Dict]:
    """Derive ADEFF from LB (HbA1c, FPG) and VS (weight, BP)."""
    adeff_records = []
    # HbA1c from LB
    for lb in lb_records:
        if lb.get("LBTESTCD") in ("HBA1C", "GLUC"):
            paramcd = "HBA1C" if lb.get("LBTESTCD") == "HBA1C" else "FPG"
            param = "HbA1c (%)" if paramcd == "HBA1C" else "Fasting Plasma Glucose (mg/dL)"
            aval = safe_float(lb.get("LBSTRESN"))
            base_val = compute_subject_baseline(adeff_records, lb.get("USUBJID", ""), paramcd, lb.get("LBDTC", ""))
            chg = (aval - base_val) if aval is not None and base_val is not None else None
            respfl = "Y" if paramcd == "HBA1C" and aval is not None and aval < 7.0 else "N" if paramcd == "HBA1C" else ""
            adeff_records.append({
                "STUDYID": lb.get("STUDYID"),
                "USUBJID": lb.get("USUBJID"),
                "PARAM": param,
                "PARAMCD": paramcd,
                "AVAL": aval,
                "BASE": base_val,
                "CHG": round(chg, 2) if chg is not None else None,
                "RESPFL": respfl,
                "ANL01FL": "Y" if lb.get("VISITNUM") == 10 else "",
                "ADY": lb.get("LBDY"),
                "ADT": (lb.get("LBDTC", "")[:10] if lb.get("LBDTC") else ""),
                "AVISITN": lb.get("VISITNUM"),
                "AVISIT": lb.get("VISIT"),
                "SRCDOM": "LB",
                "SRCVAR": "LBSEQ",
                "SRCSEQ": lb.get("LBSEQ"),
            })
    # Weight from VS
    for vs in vs_records:
        if vs.get("VSTESTCD") in ("WEIGHT", "SYSBP", "DIABP"):
            paramcd_map = {"WEIGHT": "WEIGHT", "SYSBP": "SBP", "DIABP": "DBP"}
            paramcd = paramcd_map.get(vs.get("VSTESTCD", ""), vs.get("VSTESTCD"))
            aval = safe_float(vs.get("VSSTRESN"))
            adeff_records.append({
                "STUDYID": vs.get("STUDYID"),
                "USUBJID": vs.get("USUBJID"),
                "PARAM": vs.get("VSTEST", ""),
                "PARAMCD": paramcd,
                "AVAL": aval,
                "BASE": None,  # Requires baseline computation
                "CHG": None,
                "RESPFL": "",
                "ANL01FL": "Y" if vs.get("VISITNUM") == 10 else "",
                "ADY": vs.get("VSDY"),
                "ADT": (vs.get("VSDTC", "")[:10] if vs.get("VSDTC") else ""),
                "AVISITN": vs.get("VISITNUM"),
                "AVISIT": vs.get("VISIT"),
                "SRCDOM": "VS",
                "SRCVAR": "VSSEQ",
                "SRCSEQ": vs.get("VSSEQ"),
            })
    return adeff_records

def compute_subject_baseline(adeff_records, usubjid, paramcd, current_dt):
    """Simple baseline: first value for this subject+param."""
    for r in adeff_records:
        if r["USUBJID"] == usubjid and r["PARAMCD"] == paramcd and r.get("ANL01FL") != "Y":
            return r.get("AVAL")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# ADTTE — Time-to-Event Analysis Dataset
# ═══════════════════════════════════════════════════════════════════════════════

def derive_adtte(dm_records: List[Dict], ds_records: List[Dict], ae_records: List[Dict]) -> List[Dict]:
    """Derive ADTTE from DM + DS + AE."""
    adtte_records = []
    ds_by_subject = defaultdict(list)
    for r in ds_records:
        ds_by_subject[r.get("USUBJID", "")].append(r)
    ae_by_subject = defaultdict(list)
    for r in ae_records:
        ae_by_subject[r.get("USUBJID", "")].append(r)

    for dm in dm_records:
        usubjid = dm.get("USUBJID", "")
        rfstdtc = dm.get("RFSTDTC", "")

        # OS
        os_record = derive_os(usubjid, dm, ds_by_subject, rfstdtc)
        if os_record:
            adtte_records.append(os_record)

        # TTD
        ttd_record = derive_ttd(usubjid, dm, ds_by_subject, rfstdtc)
        if ttd_record:
            adtte_records.append(ttd_record)

    return adtte_records

def derive_os(usubjid, dm, ds_by_subject, rfstdtc):
    """Derive Overall Survival record."""
    # Check if subject died
    death_date = dm.get("DTHDTC", "")
    if dm.get("DTHFL") == "Y" and death_date:
        aval = days_between(rfstdtc, death_date)
        return {
            "STUDYID": dm.get("STUDYID"),
            "USUBJID": usubjid,
            "PARAM": "Overall Survival (Days)",
            "PARAMCD": "OS",
            "AVAL": aval if aval else "",
            "STARTDT": rfstdtc,
            "ADT": death_date,
            "CNSR": 0,
            "EVNTDESC": "Death",
            "CNSDTDSC": "",
            "SRCDOM": "DM",
            "SRCVAR": "",
            "SRCSEQ": None,
        }
    else:
        # Censored at last study date
        last_date = dm.get("RFPENDTC", dm.get("RFENDTC", ""))
        aval = days_between(rfstdtc, last_date) if last_date else None
        return {
            "STUDYID": dm.get("STUDYID"),
            "USUBJID": usubjid,
            "PARAM": "Overall Survival (Days)",
            "PARAMCD": "OS",
            "AVAL": aval if aval else "",
            "STARTDT": rfstdtc,
            "ADT": last_date,
            "CNSR": 1,
            "EVNTDESC": "",
            "CNSDTDSC": "Alive at end of study",
            "SRCDOM": "DM",
            "SRCVAR": "",
            "SRCSEQ": None,
        }

def derive_ttd(usubjid, dm, ds_by_subject, rfstdtc):
    """Derive Time to Treatment Discontinuation."""
    # Find treatment discontinuation in DS
    disc_date = None
    disc_reason = ""
    for rec in ds_by_subject.get(usubjid, []):
        if rec.get("DSSCAT") == "TREATMENT" and rec.get("DSDECOD") not in ("COMPLETED", "INFORMED CONSENT OBTAINED", "RANDOMIZED"):
            disc_date = rec.get("DSSTDTC", "")
            disc_reason = rec.get("DSDECOD", "")
            break

    if disc_date:
        aval = days_between(rfstdtc, disc_date)
        return {
            "STUDYID": dm.get("STUDYID"),
            "USUBJID": usubjid,
            "PARAM": "Time to Treatment Discontinuation (Days)",
            "PARAMCD": "TTD",
            "AVAL": aval if aval else "",
            "STARTDT": rfstdtc,
            "ADT": disc_date,
            "CNSR": 0,
            "EVNTDESC": disc_reason,
            "CNSDTDSC": "",
            "SRCDOM": "DS",
            "SRCVAR": "DSSEQ",
            "SRCSEQ": rec.get("DSSEQ"),
        }
    else:
        # Censored: still on treatment
        last_date = dm.get("RFENDTC", "")
        aval = days_between(rfstdtc, last_date) if last_date else None
        return {
            "STUDYID": dm.get("STUDYID"),
            "USUBJID": usubjid,
            "PARAM": "Time to Treatment Discontinuation (Days)",
            "PARAMCD": "TTD",
            "AVAL": aval if aval else "",
            "STARTDT": rfstdtc,
            "ADT": last_date,
            "CNSR": 1,
            "EVNTDESC": "",
            "CNSDTDSC": "Still on treatment at end of study",
            "SRCDOM": "DS",
            "SRCVAR": "",
            "SRCSEQ": None,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Main Converter
# ═══════════════════════════════════════════════════════════════════════════════

def convert_all(sdtm_dir: str, output_dir: str, datasets: List[str] = None, project_root: str = None):
    """Convert SDTM JSON to all ADaM datasets.

    SDTM domain names are sourced from domain_parser (single source of truth).
    ADaM dataset names remain as protocol-level parameters.
    """
    os.makedirs(output_dir, exist_ok=True)
    all_datasets = ["ADSL", "ADAE", "ADLB", "ADEFF", "ADTTE"]
    if datasets is None:
        datasets = all_datasets

    # Load SDTM domains from parser-derived domain names (not hardcoded)
    dp = _get_dp(project_root)
    sdmt_domain_names = [d.lower() for d in dp.get_all_domains()]
    domain_data = {}
    for domain in sdmt_domain_names:
        records = load_domain(sdtm_dir, domain)
        if records:
            domain_data[domain] = records

    dm = domain_data.get("dm", [])
    ae = domain_data.get("ae", [])
    ds = domain_data.get("ds", [])
    ex = domain_data.get("ex", [])
    vs = domain_data.get("vs", [])
    lb = domain_data.get("lb", [])

    dm_lookup = {r.get("USUBJID", ""): r for r in dm}
    results = {}

    if "ADSL" in datasets and dm:
        adsl = derive_adsl(dm, ds, ex, vs, lb)
        save_dataset(adsl, "adsl", output_dir)
        results["ADSL"] = len(adsl)

    if "ADAE" in datasets and ae and dm_lookup:
        adae = derive_adae(ae, dm_lookup)
        save_dataset(adae, "adae", output_dir)
        results["ADAE"] = len(adae)

    if "ADLB" in datasets and lb and dm_lookup:
        adlb = derive_adlb(lb, dm_lookup)
        save_dataset(adlb, "adlb", output_dir)
        results["ADLB"] = len(adlb)

    if "ADEFF" in datasets and (lb or vs) and dm_lookup:
        adeff = derive_adeff(lb, vs, dm_lookup)
        save_dataset(adeff, "adeff", output_dir)
        results["ADEFF"] = len(adeff)

    if "ADTTE" in datasets and dm and ds:
        adtte = derive_adtte(dm, ds, ae)
        save_dataset(adtte, "adtte", output_dir)
        results["ADTTE"] = len(adtte)

    return results

def save_dataset(records: List[Dict], name: str, output_dir: str):
    """Save ADaM dataset as JSON."""
    path = os.path.join(output_dir, f"{name.lower()}.json")
    with open(path, "w") as f:
        json.dump({"dataset": name, "records": records, "count": len(records)}, f, indent=2)

# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="SDTM → ADaM Conversion Engine")
    parser.add_argument("--input", "-i", required=True, help="SDTM JSON directory")
    parser.add_argument("--output", "-o", required=True, help="ADaM JSON output directory")
    parser.add_argument("--project-root", help="Path to TrialSim project root (for domain_parser). Auto-detected if omitted.")
    parser.add_argument("--datasets", default="ADSL,ADAE,ADLB,ADEFF,ADTTE", help="Comma-separated dataset names")
    args = parser.parse_args()

    datasets = [d.strip().upper() for d in args.datasets.split(",")]
    print(f"正在转换 SDTM → ADaM...")
    print(f"  输入:  {args.input}")
    print(f"  输出: {args.output}")
    print(f"  数据集: {', '.join(datasets)}")

    results = convert_all(args.input, args.output, datasets, args.project_root)
    for name, count in results.items():
        print(f"  {name}: {count} 条记录")
    print(f"  完成。数据集总数: {len(results)}")

if __name__ == "__main__":
    main()
