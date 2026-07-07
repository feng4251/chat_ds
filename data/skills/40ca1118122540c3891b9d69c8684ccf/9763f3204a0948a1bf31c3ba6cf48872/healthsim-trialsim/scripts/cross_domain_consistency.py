#!/usr/bin/env python3
"""
Cross-Domain Consistency Checker (domain_parser-powered)
=========================================================
All domain knowledge (variable names, date fields, business rules)
read from domain skill markdown files via domain_parser.py.

Usage:
  python cross_domain_consistency.py --input sdtm_json/ --output report.json
"""

import json, sys, os
from datetime import datetime
from typing import Dict, List
from collections import defaultdict
from dataclasses import dataclass, field, asdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from domain_parser import DomainParser


@dataclass
class Issue:
    rule: str; severity: str; domains: List[str]; description: str
    affected: List[str] = field(default_factory=list); count: int = 0

@dataclass
class Report:
    sdtm_dir: str; timestamp: str; total: int = 0
    critical: int = 0; error: int = 0; warning: int = 0; info: int = 0
    issues: List[Issue] = field(default_factory=list); passed: bool = True


class CrossDomainChecker:
    def __init__(self, sdtm_dir: str, project_root: str = "."):
        self.sdtm_dir = sdtm_dir
        self.parser = DomainParser(os.path.abspath(project_root))
        self.data: Dict[str, List[Dict]] = {}
        self.dm_lookup: Dict[str, Dict] = {}
        self.report = Report(sdtm_dir=sdtm_dir, timestamp=datetime.now().isoformat())

    def _add(self, rule, severity, domains, desc, affected=None):
        issue = Issue(rule=rule, severity=severity, domains=domains, description=desc, affected=affected or [], count=len(affected) if affected else 0)
        self.report.issues.append(issue); self.report.total += 1
        if severity == "CRITICAL": self.report.critical += 1
        elif severity == "ERROR": self.report.error += 1
        elif severity == "WARNING": self.report.warning += 1
        else: self.report.info += 1

    def load(self) -> bool:
        for dom in self.parser.get_all_domains():
            path = os.path.join(self.sdtm_dir, f"{dom.lower()}.json")
            if os.path.exists(path):
                with open(path) as f: j = json.load(f)
                self.data[dom] = j.get("records", j) if isinstance(j, dict) else j
        if "DM" not in self.data:
            self._add("NO_DM", "CRITICAL", ["DM"], "DM required"); return False
        for rec in self.data["DM"]: self.dm_lookup[rec.get("USUBJID", "")] = rec
        return True

    def run(self) -> Report:
        if not self.load(): return self.report
        self._check_orphans()
        self._check_temporal()
        self._check_death()
        self._check_exposure()
        self._check_ds_completeness()
        self.report.passed = self.report.critical == 0 and self.report.error == 0
        return self.report

    def _check_orphans(self):
        dm_ids = set(self.dm_lookup.keys())
        for dom in ["AE", "CM", "VS", "LB", "EX", "DS", "MH"]:
            if dom not in self.data: continue
            orphans = [r.get("USUBJID","") for r in self.data[dom] if r.get("USUBJID","") not in dm_ids]
            if orphans: self._add("ORPHAN", "CRITICAL", [dom, "DM"], f"{dom}: {len(set(orphans))} orphans", list(set(orphans))[:10])

    def _check_temporal(self):
        rfstdtc_map = {r["USUBJID"]: r.get("RFSTDTC","") for r in self.data.get("DM",[]) if "USUBJID" in r}
        if "AE" in self.data:
            ee = set()
            for r in self.data["AE"]:
                u = r.get("USUBJID",""); ad = r.get("AESTDTC","")
                rf = rfstdtc_map.get(u,"")
                if ad and rf and ad < rf: ee.add(u)
            if ee: self._add("AE_BEFORE_TRT", "WARNING", ["AE","DM"], f"{len(ee)} subjects with AE before first dose", list(ee)[:10])

    def _check_death(self):
        ds_deaths = {}
        if "DS" in self.data:
            for r in self.data["DS"]:
                if r.get("DSDECOD") == "DEATH": ds_deaths[r.get("USUBJID","")] = r.get("DSSTDTC","")
        for u, ds_date in ds_deaths.items():
            dm = self.dm_lookup.get(u, {})
            if dm.get("DTHFL") != "Y":
                self._add("DEATH_DM", "CRITICAL", ["DS","DM"], f"{u}: DS.DEATH but DM.DTHFL!=Y", [u])
            dm_dd = dm.get("DTHDTC", "")
            if ds_date and dm_dd and ds_date != dm_dd:
                self._add("DEATH_DATE", "ERROR", ["DS","DM"], f"{u}: date mismatch", [u])

    def _check_exposure(self):
        if "EX" not in self.data or "DS" not in self.data: return
        ds_end = {}
        for r in self.data["DS"]:
            if r.get("DSSCAT") in ("TREATMENT",) and r.get("DSDECOD") not in ("COMPLETED","INFORMED CONSENT OBTAINED","RANDOMIZED"):
                u = r.get("USUBJID",""); dt = r.get("DSSTDTC","")
                if u not in ds_end or (dt and (not ds_end[u] or dt < ds_end[u])): ds_end[u] = dt
        late = set()
        for r in self.data["EX"]:
            u = r.get("USUBJID",""); ee = r.get("EXENDTC","")
            if u in ds_end and ee and ds_end[u] and ee > ds_end[u]: late.add(u)
        if late: self._add("EX_AFTER_DISC", "WARNING", ["EX","DS"], f"{len(late)} subjects with exposure after DC", list(late)[:10])

    def _check_ds_completeness(self):
        if "DS" not in self.data: return
        ds_subj = defaultdict(list)
        for r in self.data["DS"]: ds_subj[r.get("USUBJID","")].append(r)
        missing = [u for u, recs in ds_subj.items() if "INFORMED CONSENT OBTAINED" not in [r.get("DSDECOD","") for r in recs]]
        if missing: self._add("NO_ICF", "WARNING", ["DS"], f"{len(missing)} subjects without ICF", missing[:10])


def print_report(r: Report):
    print("\n" + "=" * 80)
    print("  跨域数据一致性检查 (数据来源: domains/*.md 经由 domain_parser)")
    print("=" * 80)
    print(f"  状态: {'✅ 通过' if r.passed else '❌ 未通过'}")
    print(f"  问题数: {r.total} (严重{r.critical}, 错误{r.error}, 警告{r.warning}, 提示{r.info})\n")
    for sev in ["CRITICAL","ERROR","WARNING","INFO"]:
        for iss in [i for i in r.issues if i.severity == sev]:
            icon = {"CRITICAL":"🔴","ERROR":"❌","WARNING":"⚠️","INFO":"ℹ️"}[sev]
            print(f"  {icon} [{sev}] {iss.rule}: {iss.description} [{', '.join(iss.domains)}]")
    print("=" * 80)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Cross-Domain Consistency (domain_parser)")
    ap.add_argument("--project-root", default=".")
    ap.add_argument("--input", "-i", required=True)
    ap.add_argument("--output", "-o"); ap.add_argument("--quiet", "-q", action="store_true")
    args = ap.parse_args()
    c = CrossDomainChecker(args.input, args.project_root)
    r = c.run()
    if not args.quiet: print_report(r)
    if args.output:
        with open(args.output, "w") as f: json.dump(asdict(r), f, indent=2, default=str)
    sys.exit(0 if r.passed else 1)

if __name__ == "__main__":
    main()
