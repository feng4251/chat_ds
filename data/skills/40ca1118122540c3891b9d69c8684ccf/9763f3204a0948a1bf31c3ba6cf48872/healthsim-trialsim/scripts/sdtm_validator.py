#!/usr/bin/env python3
"""
SDTM IG 3.4 Validator — Rule Engine (domain_parser-powered)
=============================================================
Validates SDTM datasets against CDISC SDTM IG 3.4 rules.
All domain knowledge (variables, controlled terminology, business rules)
is read from domain skill markdown files via domain_parser.py.

Usage:
  python sdtm_validator.py --input sdtm_json/ --output validation_report.json
  python sdtm_validator.py --domain DM --input dm.json
"""

import json, sys, os, re
from datetime import datetime
from typing import Dict, List
from collections import defaultdict
from dataclasses import dataclass, field, asdict

# ── Import shared parser (single source of truth) ──────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from domain_parser import DomainParser


@dataclass
class ValidationIssue:
    rule_id: str; domain: str; severity: str; description: str
    affected_records: List[str] = field(default_factory=list); suggestion: str = ""

@dataclass
class DomainReport:
    domain: str; record_count: int = 0
    issues: List[ValidationIssue] = field(default_factory=list); passed: bool = True
    error_count: int = 0; warning_count: int = 0; info_count: int = 0


class SDTMValidator:
    """Validates SDTM datasets using domain definitions from markdown skill files."""

    def __init__(self, project_root: str = "."):
        project_root = os.path.abspath(project_root)
        self.parser = DomainParser(project_root)
        self.ct = self.parser.get_controlled_terminology()
        self.reports: Dict[str, DomainReport] = {}

    def validate_all(self, sdtm_data: Dict[str, List[Dict]]) -> Dict[str, DomainReport]:
        for domain in self.parser.get_all_domains():
            if domain in sdtm_data:
                self.reports[domain] = self.validate_domain(domain, sdtm_data[domain])
        # Cross-domain consistency
        if "DM" in sdtm_data:
            cross = self._check_cross_domain(sdtm_data)
            for domain, issues in cross.items():
                if domain in self.reports:
                    self.reports[domain].issues.extend(issues)
                    self.reports[domain].error_count = sum(1 for i in self.reports[domain].issues if i.severity == "ERROR")
                    self.reports[domain].warning_count = sum(1 for i in self.reports[domain].issues if i.severity == "WARNING")
                    if any(i.severity == "ERROR" for i in self.reports[domain].issues):
                        self.reports[domain].passed = False
        return self.reports

    def validate_domain(self, domain: str, records: List[Dict]) -> DomainReport:
        r = DomainReport(domain=domain, record_count=len(records))
        if not records:
            r.issues.append(ValidationIssue("EMPTY", domain, "WARNING", f"No records"))
            return r

        dd = self.parser.get_domain(domain)
        r.issues.extend(self._check_required(records, dd))
        r.issues.extend(self._check_types_ranges(records, dd))
        r.issues.extend(self._check_ct(records, dd))
        r.issues.extend(self._check_seq(records, dd))
        r.issues.extend(self._check_dates(records, dd))
        r.issues.extend(self._check_business_rules(records, dd))

        r.error_count = sum(1 for i in r.issues if i.severity == "ERROR")
        r.warning_count = sum(1 for i in r.issues if i.severity == "WARNING")
        r.passed = r.error_count == 0
        return r

    # ── Checks ─────────────────────────────────────────────────────────────

    def _check_required(self, records, dd: DomainDef):
        issues = []
        for i, rec in enumerate(records):
            for var in dd.required_variables:
                if rec.get(var.name) in (None, ""):
                    issues.append(ValidationIssue("MISSING_REQUIRED", dd.name, "ERROR", f"Record {i}: {var.name} missing"))
        return issues

    def _check_types_ranges(self, records, dd: DomainDef):
        issues = []
        for i, rec in enumerate(records):
            uid = rec.get("USUBJID", f"rec_{i}")
            if "AGE" in rec and rec["AGE"] is not None:
                try:
                    a = float(rec["AGE"])
                    if a < 0 or a > 130:
                        issues.append(ValidationIssue("AGE_RANGE", dd.name, "ERROR", f"{uid}: AGE={a}"))
                except (ValueError, TypeError): pass
            if dd.name == "AE":
                ad = rec.get("AESTDTC", ""); ae = rec.get("AEENDTC", "")
                if ad and ae and ad > ae:
                    issues.append(ValidationIssue("DATE_ORDER", "AE", "ERROR", f"{uid}: AESTDTC > AEENDTC"))
            if dd.name == "EX":
                try:
                    d = float(rec.get("EXDOSE", 0))
                    if d < 0:
                        issues.append(ValidationIssue("DOSE_NEG", "EX", "ERROR", f"{uid}: EXDOSE={d}"))
                except (ValueError, TypeError): pass
                ed1 = rec.get("EXSTDTC", ""); ed2 = rec.get("EXENDTC", "")
                if ed1 and ed2 and ed1 > ed2:
                    issues.append(ValidationIssue("DATE_ORDER", "EX", "ERROR", f"{uid}: EXSTDTC > EXENDTC"))
        return issues

    def _check_ct(self, records, dd: DomainDef):
        issues = []
        for i, rec in enumerate(records):
            uid = rec.get("USUBJID", f"rec_{i}")
            for var in dd.variables:
                if var.codelist and var.name in rec and rec[var.name] not in (None, ""):
                    ok, msg = self.parser.validate_ct(dd.name, var.name, str(rec[var.name]))
                    if not ok:
                        issues.append(ValidationIssue("CT_VIOLATION", dd.name, "ERROR", f"{uid}: {var.name}='{rec[var.name]}' {msg}"))
        return issues

    def _check_seq(self, records, dd: DomainDef):
        issues = []
        seq_var = f"{dd.name}SEQ"
        if not records or seq_var not in records[0]: return issues
        seen = defaultdict(set)
        for rec in records:
            uid = rec.get("USUBJID", "")
            sv = rec.get(seq_var)
            if sv in seen[uid]:
                issues.append(ValidationIssue("DUP_SEQ", dd.name, "ERROR", f"{uid}: {seq_var}={sv} duplicated"))
            seen[uid].add(sv)
        return issues

    def _check_dates(self, records, dd: DomainDef):
        issues = []
        iso_pat = re.compile(r'^\d{4}(-\d{2}(-\d{2})?)?')
        date_vars = self.parser.get_date_variables(dd.name)
        for rec in records:
            uid = rec.get("USUBJID", "")
            for dv in date_vars:
                val = str(rec.get(dv, ""))
                if val and val not in ("None", "null", "NULL", "NA") and not iso_pat.match(val):
                    issues.append(ValidationIssue("BAD_DATE", dd.name, "ERROR", f"{uid}: {dv}='{val}' not ISO 8601"))
        return issues

    def _check_business_rules(self, records, dd: DomainDef):
        issues = []
        for i, rec in enumerate(records):
            uid = rec.get("USUBJID", f"rec_{i}")
            if dd.name == "DM" and rec.get("DTHFL") == "Y" and not rec.get("DTHDTC"):
                issues.append(ValidationIssue("DEATH_NO_DATE", "DM", "ERROR", f"{uid}: DTHFL=Y but no DTHDTC"))
            if dd.name == "AE" and rec.get("AESER") == "Y":
                sae = ["AESCONG", "AESDISAB", "AESDTH", "AESHOSP", "AESLIFE", "AESMIE"]
                if not any(rec.get(f) == "Y" for f in sae):
                    issues.append(ValidationIssue("SAE_NO_CRITERIA", "AE", "ERROR", f"{uid}: AESER=Y but no SAE criteria"))
            if dd.name == "CM" and rec.get("CMONGO") == "Y" and rec.get("CMENDTC"):
                issues.append(ValidationIssue("ONGO_WITH_END", "CM", "WARNING", f"{uid}: CMONGO=Y but CMENDTC exists"))
        # DS per-subject checks
        if dd.name == "DS":
            ds_subj = defaultdict(list)
            for rec in records: ds_subj[rec.get("USUBJID", "")].append(rec)
            for uid, recs in ds_subj.items():
                decodes = [r.get("DSDECOD", "") for r in recs]
                if "INFORMED CONSENT OBTAINED" not in decodes:
                    issues.append(ValidationIssue("NO_ICF", "DS", "ERROR", f"{uid}: missing informed consent"))
        return issues

    # ── Cross-Domain ────────────────────────────────────────────────────────

    def _check_cross_domain(self, data):
        issues = defaultdict(list)
        dm_ids = {r["USUBJID"] for r in data.get("DM", []) if "USUBJID" in r}
        dm_rfstdtc = {r["USUBJID"]: r.get("RFSTDTC", "") for r in data.get("DM", [])}

        for dom in ["AE", "CM", "VS", "LB", "EX", "DS", "MH"]:
            if dom not in data: continue
            for rec in data[dom]:
                u = rec.get("USUBJID", "")
                if u and u not in dm_ids:
                    issues[dom].append(ValidationIssue("ORPHAN", dom, "ERROR", f"{u}: not in DM"))

        if "AE" in data:
            for rec in data["AE"]:
                u = rec.get("USUBJID", ""); ae_d = rec.get("AESTDTC", "")
                rf = dm_rfstdtc.get(u, "")
                if ae_d and rf and ae_d < rf:
                    issues["AE"].append(ValidationIssue("AE_PRE_DOSE", "AE", "WARNING", f"{u}: AESTDTC < RFSTDTC"))
        return issues


# ── Report ──────────────────────────────────────────────────────────────────

def generate_readiness(reports: Dict[str, DomainReport]) -> Dict:
    weights = {"DM": 15, "AE": 15, "CM": 10, "VS": 10, "LB": 10, "EX": 10, "DS": 10, "MH": 10, "__cross": 10}
    score = 0
    for domain, r in reports.items():
        w = weights.get(domain, 5)
        penalty = r.error_count * 3 + r.warning_count * 1
        score += max(0, w - min(penalty, w))
    return {"total_score": min(100, round(score)), "max_score": 100, "grade": "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D", "recommendation": "READY_FOR_SUBMISSION" if score >= 90 else "MINOR_FIXES" if score >= 75 else "SIGNIFICANT_FIXES"}

def print_report(reports, readiness):
    print("\n" + "=" * 80)
    print("  SDTM IG 3.4 验证 (数据来源: domains/*.md 经由 domain_parser)")
    print("=" * 80)
    t_e = sum(r.error_count for r in reports.values())
    t_w = sum(r.warning_count for r in reports.values())
    print(f"  状态: {'✅ 通过' if t_e == 0 else '❌ 未通过'} | 错误: {t_e} | 警告: {t_w}")
    print(f"  就绪度: {readiness['total_score']}/100 ({readiness['grade']}) — {readiness['recommendation']}\n")
    for dom, r in reports.items():
        icon = "✅" if r.passed else "❌"
        print(f"  {icon} {dom}: {r.record_count} 条记录, {r.error_count}错/{r.warning_count}警")
    print("=" * 80)


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="SDTM Validator (domain_parser-powered)")
    ap.add_argument("--project-root", default=".", help="TrialSim project root")
    ap.add_argument("--input", "-i", help="SDTM JSON directory or single file")
    ap.add_argument("--output", "-o", help="Output JSON report")
    ap.add_argument("--quiet", "-q", action="store_true")
    args = ap.parse_args()

    if not args.input:
        print("Usage: sdtm_validator.py --input sdtm_json/ [--output report.json]"); sys.exit(1)

    v = SDTMValidator(args.project_root)
    data = {}

    if os.path.isdir(args.input):
        for dom in v.parser.get_all_domains():
            path = os.path.join(args.input, f"{dom.lower()}.json")
            if os.path.exists(path):
                with open(path) as f: j = json.load(f)
                data[dom] = j.get("records", j) if isinstance(j, dict) else j
    else:
        with open(args.input) as f: j = json.load(f)
        dn = os.path.basename(args.input).replace(".json", "").upper()
        data[dn] = j.get("records", j) if isinstance(j, dict) else j

    reports = v.validate_all(data)
    readiness = generate_readiness(reports)

    if not args.quiet:
        print_report(reports, readiness)

    if args.output:
        out = {"validator": "SDTM IG 3.4 (domain_parser)", "timestamp": datetime.now().isoformat(), "readiness": readiness, "domains": {d: asdict(r) for d, r in reports.items()}}
        with open(args.output, "w") as f: json.dump(out, f, indent=2, default=str)

    sys.exit(1 if any(not r.passed for r in reports.values()) else 0)


if __name__ == "__main__":
    main()
