#!/usr/bin/env python3
"""
One-time script: Enrich domain markdown files with structured YAML frontmatter.
Parses existing variable tables in each domains/*.md and injects machine-readable
variable definitions into the YAML frontmatter.

Run once: python scripts/enrich_domain_frontmatter.py
"""

import re, yaml, os, sys
from typing import Dict, List

DOMAIN_FILES = {
    "DM": "domains/demographics-dm.md",
    "AE": "domains/adverse-events-ae.md",
    "CM": "domains/concomitant-meds-cm.md",
    "VS": "domains/vital-signs-vs.md",
    "LB": "domains/laboratory-lb.md",
    "EX": "domains/exposure-ex.md",
    "DS": "domains/disposition-ds.md",
    "MH": "domains/medical-history-mh.md",
}

# CDISC CT codelist mappings from the domain prose
KNOWN_CODELISTS = {
    "SEX": "SEX", "RACE": "RACE", "ETHNIC": "ETHNIC", "AGEU": "AGEU", "COUNTRY": "ISO3166",
    "AESEV": "AESEV", "AESER": "NY", "AEREL": "AEREL", "AEOUT": "AEOUT", "AEACN": "AEACN",
    "AESCONG": "NY", "AESDISAB": "NY", "AESDTH": "NY", "AESHOSP": "NY", "AESLIFE": "NY", "AESMIE": "NY",
    "DSCAT": "DSCAT", "DSSCAT": "DSSCAT", "DSDECOD": None, "EPOCH": "EPOCH",
    "VSTESTCD": "VSTESTCD", "VSBLFL": "NY", "VSORRESU": None, "VSPOS": "POSITION",
    "LBTESTCD": None, "LBCAT": "LBCAT", "LBNRIND": "LBNRIND", "LBBLFL": "NY",
    "EXDOSFRQ": "EXDOSFRQ", "EXROUTE": "EXROUTE", "EXDOSU": None, "EXDOSFRM": None,
    "CMROUTE": "EXROUTE", "CMDOSFRQ": "EXDOSFRQ", "CMONGO": "NY",
    "MHCONTR": "NY", "MHENRF": None, "MHOCCUR": "NY", "DTHFL": "NY",
    "RFSTDTC": None, "RFENDTC": None, "BRTHDTC": None, "RFICDTC": None, "RFPENDTC": None, "DTHDTC": None,
    "ACTARMCD": None, "ACTARM": None, "ARMCD": None, "ARM": None,
}

def infer_codelist(var_name: str) -> str:
    """Infer controlled terminology codelist from variable name."""
    return KNOWN_CODELISTS.get(var_name)

def infer_type(type_str: str) -> str:
    """Map SDTM type description to canonical type."""
    t = type_str.strip().upper()
    if t in ("CHAR", "TEXT"): return "text"
    if t in ("NUM", "INTEGER", "INT"): return "integer"
    if t in ("FLOAT", "DOUBLE", "REAL"): return "float"
    if t in ("DATETIME", "DATE"): return "datetime"
    return "text"

def infer_origin(var_name: str) -> str:
    """Infer variable origin type."""
    if var_name in ("STUDYID", "DOMAIN", "USUBJID", "SITEID", "ARMCD", "ARM"):
        return "ASSIGNED"
    if var_name.endswith(("SEQ", "STRESC", "STRESN", "LOINC", "ATC1CD", "ATC2CD", "ATC3CD", "ATC4CD", "DECOD", "BODSYS", "DY", "TOXGR")):
        return "DERIVED"
    if var_name.endswith(("TERM", "ORRES", "TRT")) or var_name in ("CMTRT", "EXTRT", "MHTERM", "DSTERM"):
        return "CRF"
    return "PROTOCOL"

def parse_variable_table(content: str) -> List[Dict]:
    """Parse SDTM variable markdown tables ONLY from the '## SDTM Variables' section."""
    variables = []
    in_sdtm_section = False
    in_table = False

    for line in content.split('\n'):
        line_stripped = line.strip()
        line_lower = line_stripped.lower()

        # Enter/exit the ## SDTM Variables section
        if line_stripped.startswith('## ') and 'sdtm variables' in line_lower:
            in_sdtm_section = True
            continue
        if in_sdtm_section and line_stripped.startswith('## ') and 'sdtm variables' not in line_lower:
            in_sdtm_section = False
            in_table = False
            continue

        if not in_sdtm_section:
            continue

        if re.match(r'\| (Variable|\*\*Variable\*\*) \|', line_stripped):
            in_table = True
            continue
        if in_table and re.match(r'\|[-–—|\s]+\|', line_stripped):
            continue
        # Stop table parsing on any subsection header
        if in_table and line_stripped.startswith('### '):
            in_table = False
            continue
        if in_table and line_stripped.startswith('## '):
            in_table = False
            continue

        if in_table and line_stripped.startswith('| '):
            parts = [p.strip() for p in line_stripped.split('|')[1:-1]]
            if len(parts) >= 4:
                var_name = parts[0].replace('**', '').strip()
                label = parts[1].strip()
                type_str = parts[2].strip()
                length_str = parts[3].strip()
                length = int(re.sub(r'[^0-9]', '', length_str)) if re.sub(r'[^0-9]', '', length_str) else 8
                if var_name and var_name[0].isalpha() and not var_name[0].islower():
                    variables.append({
                        "name": var_name, "label": label,
                        "type": infer_type(type_str), "length": length,
                        "required": False,
                    })

    return variables

def split_content(content: str) -> tuple:
    """Split markdown into frontmatter YAML and body."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
    if match:
        return match.group(1), match.group(2)
    return "", content

def classify_sections(content: str) -> Dict[str, List[str]]:
    """Identify which variables are in Required vs Expected sections.
    Only parses the ## SDTM Variables section, ignoring all other prose tables."""
    sections = {"required": [], "expected": []}
    in_sdtm_section = False
    current_section = None
    in_table = False

    for line in content.split('\n'):
        line_lower = line.strip().lower()
        line_stripped = line.strip()

        # Enter/exit the SDTM Variables section
        if line_stripped.startswith('## ') and 'sdtm variables' in line_lower:
            in_sdtm_section = True
            continue
        if in_sdtm_section and line_stripped.startswith('## ') and 'sdtm variables' not in line_lower:
            in_sdtm_section = False
            current_section = None
            in_table = False
            continue
        # Sub-sections within SDTM Variables
        if in_sdtm_section and '### required variables' in line_lower:
            current_section = "required"
            in_table = False
            continue
        if in_sdtm_section and '### expected variables' in line_lower:
            current_section = "expected"
            in_table = False
            continue
        if in_sdtm_section and '### permissible variables' in line_lower:
            current_section = "expected"
            in_table = False
            continue
        # Reset on any other ### header
        if in_sdtm_section and current_section and line_stripped.startswith('### ') and 'variable' not in line_lower:
            current_section = None
            in_table = False
            continue

        if in_sdtm_section and current_section and in_table and line.startswith('| '):
            parts = [p.strip() for p in line.split('|')[1:-1]]
            if len(parts) >= 1:
                var_name = parts[0].replace('**', '').strip()
                if var_name and var_name[0].isalpha():
                    sections[current_section].append(var_name)

        if in_sdtm_section and current_section and re.match(r'\| (Variable|\*\*Variable\*\*) \|', line):
            in_table = True
            continue
        if in_table and re.match(r'\|[-–—|\s]+\|', line):
            continue

    return sections

def enrich_file(filepath: str, domain: str):
    """Enrich a single domain markdown file with structured YAML frontmatter."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    old_yaml, body = split_content(content)

    # Parse existing YAML frontmatter
    try:
        existing = yaml.safe_load(old_yaml) or {}
    except yaml.YAMLError:
        existing = {}

    # Parse variable tables
    variables = parse_variable_table(content)

    # Classify required vs expected
    sections = classify_sections(content)
    required_names = set(sections.get("required", []))

    for var in variables:
        var["required"] = var["name"] in required_names
        cl = infer_codelist(var["name"])
        if cl:
            var["codelist"] = cl
        var["origin"] = infer_origin(var["name"])

    # Build new YAML frontmatter
    new_frontmatter = {
        "name": existing.get("name", f"{domain.lower()}-domain"),
        "description": existing.get("description", ""),
        "domain": domain,
        "variables": variables,
    }

    # Add business rules from the prose
    rules = extract_business_rules(content)
    if rules:
        new_frontmatter["business_rules"] = rules

    # Serialize
    new_yaml = yaml.dump(new_frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120)

    # Write back
    new_content = f"---\n{new_yaml}---\n{body}"
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)

    print(f"  ✅ {domain}: {len(variables)} variables ({sum(1 for v in variables if v['required'])} required, {sum(1 for v in variables if not v['required'])} expected) — {filepath}")

def extract_business_rules(content: str) -> List[str]:
    """Extract business rules from domain prose."""
    rules = []
    in_rules = False
    for line in content.split('\n'):
        if '### Business Rules' in line:
            in_rules = True
            continue
        if in_rules and line.startswith('## '):
            break
        if in_rules and line.strip().startswith('- '):
            rule = line.strip()[2:].strip()
            # Remove markdown formatting
            rule = re.sub(r'\*\*|\*|`', '', rule)
            if len(rule) > 10:
                rules.append(rule)
    return rules

def main():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print("Enriching domain markdown files with structured YAML frontmatter...\n")

    for domain, relpath in DOMAIN_FILES.items():
        filepath = os.path.join(project_root, relpath)
        if not os.path.exists(filepath):
            print(f"  ❌ {domain}: file not found at {relpath}")
            continue
        enrich_file(filepath, domain)

    print(f"\n✅ All {len(DOMAIN_FILES)} domain files enriched.")
    print("Run 'python scripts/domain_parser.py .' to verify.")

if __name__ == "__main__":
    main()
