#!/usr/bin/env python3
"""
SDTM to CSV Converter (domain_parser-powered)
=============================================
Converts SDTM JSON records to CSV with CDISC-compliant headers.
Column order, variable labels, and data dictionary metadata are all
read from domain skill markdown files via domain_parser.py.

Usage:
  python sdtm_to_csv.py --input sdtm_json/ --output csv_output/
  python sdtm_to_csv.py --input dm.json --output dm.csv --domain DM
"""

import csv, json, sys, os
from typing import List, Dict

# ── Import shared parser (single source of truth) ──────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from domain_parser import DomainParser


def load_json(path: str) -> List[Dict]:
    with open(path, "r") as f:
        data = json.load(f)
    if isinstance(data, dict) and "records" in data:
        return data["records"]
    return data if isinstance(data, list) else []


def records_to_csv(domain: str, records: List[Dict], output_path: str, var_order: List[str], delimiter: str = ",") -> int:
    if not records:
        print(f"  {domain}: No records, skipping")
        return 0
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delimiter, quotechar='"', quoting=csv.QUOTE_MINIMAL)
        w.writerow(var_order)
        for rec in records:
            row = []
            for var in var_order:
                val = rec.get(var, "")
                if val is None: val = ""
                elif isinstance(val, (dict, list)): val = json.dumps(val)
                else: val = str(val)
                row.append(val)
            w.writerow(row)
    return len(records)


def generate_data_dict(domain: str, parser: DomainParser, output_path: str):
    dd = parser.get_domain(domain)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Variable", "Label", "Type", "Length", "Required", "Codelist", "Origin"])
        for var in dd.variables:
            w.writerow([var.name, var.label, var.type, var.length, "Y" if var.required else "", var.codelist or "", var.origin])


def convert_all(input_dir: str, output_dir: str, parser: DomainParser, data_dict: bool = False, delimiter: str = ",") -> Dict[str, int]:
    os.makedirs(output_dir, exist_ok=True)
    counts = {}
    for domain_name in parser.get_all_domains():
        json_path = os.path.join(input_dir, f"{domain_name.lower()}.json")
        if not os.path.exists(json_path):
            print(f"  {domain_name}: {json_path} not found, skipping")
            continue
        records = load_json(json_path)
        dd = parser.get_domain(domain_name)
        col_order = dd.column_order
        csv_path = os.path.join(output_dir, f"{domain_name.lower()}.csv")
        counts[domain_name] = records_to_csv(domain_name, records, csv_path, col_order, delimiter)
        print(f"  {domain_name}: {counts[domain_name]} records → {os.path.basename(csv_path)}")
        if data_dict:
            dd_path = os.path.join(output_dir, f"{domain_name.lower()}_datadict.csv")
            generate_data_dict(domain_name, parser, dd_path)
            print(f"    Data dict → {os.path.basename(dd_path)}")
    return counts


def main():
    import argparse
    ap = argparse.ArgumentParser(description="SDTM→CSV (domain_parser-powered)")
    ap.add_argument("--project-root", default=".", help="TrialSim project root")
    ap.add_argument("--input", "-i", required=True, help="SDTM JSON file or directory")
    ap.add_argument("--output", "-o", required=True, help="Output CSV file or directory")
    ap.add_argument("--delimiter", default=",", choices=[",", "tab", "|"])
    ap.add_argument("--data-dict", action="store_true", help="Generate data dictionary CSVs")
    ap.add_argument("--domain", help="Single domain (DM, AE, ...)")
    args = ap.parse_args()

    delim = "\t" if args.delimiter == "tab" else args.delimiter
    parser = DomainParser(os.path.abspath(args.project_root))

    if os.path.isfile(args.input):
        records = load_json(args.input)
        domain = args.domain or os.path.basename(args.input).replace(".json", "").upper()
        dd = parser.get_domain(domain)
        count = records_to_csv(domain, records, args.output, dd.column_order, delim)
        print(f"  {domain}: {count} 条记录 → {args.output}")
    else:
        counts = convert_all(args.input, args.output, parser, data_dict=args.data_dict, delimiter=delim)
        total = sum(counts.values())
        print(f"\n  合计: {total} 条记录，{len(counts)} 个域")


if __name__ == "__main__":
    main()
