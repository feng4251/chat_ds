#!/usr/bin/env python3
"""
TrialSim Domain Parser — Single Source of Truth
================================================
Parses structured YAML frontmatter from domain skill markdown files,
providing variable definitions, controlled terminology, and business
rules to all downstream scripts (DDL generator, CSV converter, validator, etc.).

Architecture:
  domains/*.md  (YAML frontmatter + prose)  ← 单一事实来源
         │
         ▼
  domain_parser.py  (shared module)         ← 所有脚本的唯一数据入口
         │
    ┌────┼────┬────────┬──────────┐
    ▼    ▼     ▼        ▼          ▼
  DDL   CSV   Validator  ADaM     Readiness

Usage:
  from domain_parser import DomainParser
  dp = DomainParser("/path/to/trial-artifs-sim")
  dm_vars = dp.get_variables("DM")
  all_ct = dp.get_controlled_terminology()
"""

import os
import re
import yaml
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class VariableDef:
    """A single SDTM variable definition parsed from a domain skill file."""
    name: str
    label: str
    type: str          # text, integer, float, datetime
    length: int
    required: bool
    codelist: Optional[str] = None    # e.g., "SEX", "AESEV", "NY"
    origin: str = "CRF"              # CRF, DERIVED, ASSIGNED, PROTOCOL
    description: str = ""

    @property
    def sql_type(self) -> str:
        """Map SDTM data type to SQL type."""
        mapping = {"text": "VARCHAR", "integer": "INTEGER", "float": "DOUBLE", "datetime": "TIMESTAMP"}
        return mapping.get(self.type, "VARCHAR")

    @property
    def python_type(self) -> type:
        mapping = {"text": str, "integer": int, "float": float, "datetime": str}
        return mapping.get(self.type, str)


@dataclass
class DomainDef:
    """Complete domain definition parsed from a domain skill file."""
    name: str                         # e.g., "DM", "AE"
    description: str                  # Human-readable description
    observation_class: str            # SPECIAL PURPOSE, EVENTS, INTERVENTIONS, FINDINGS
    variables: List[VariableDef] = field(default_factory=list)
    controlled_terminology: Dict[str, List[str]] = field(default_factory=dict)
    business_rules: List[str] = field(default_factory=list)
    source_file: str = ""

    @property
    def required_variables(self) -> List[VariableDef]:
        return [v for v in self.variables if v.required]

    @property
    def expected_variables(self) -> List[VariableDef]:
        return [v for v in self.variables if not v.required]

    @property
    def column_order(self) -> List[str]:
        """Return variables in display order (required first, then expected)."""
        return [v.name for v in self.required_variables] + [v.name for v in self.expected_variables]

    @property
    def primary_key(self) -> Tuple[str, ...]:
        """Return the primary key columns for this domain."""
        if self.name == "DM":
            return ("USUBJID",)
        return ("USUBJID", f"{self.name}SEQ")


# ═══════════════════════════════════════════════════════════════════════════════
# Parser
# ═══════════════════════════════════════════════════════════════════════════════

class DomainParser:
    """
    Parses TrialSim domain skill markdown files and provides structured
    access to variable definitions, controlled terminology, and business rules.
    """

    # Observation class mapping (derived from SDTM IG 3.4)
    OBSERVATION_CLASSES = {
        "DM": "SPECIAL PURPOSE",
        "AE": "EVENTS", "DS": "EVENTS", "MH": "EVENTS",
        "CM": "INTERVENTIONS", "EX": "INTERVENTIONS",
        "VS": "FINDINGS", "LB": "FINDINGS",
    }

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.domains_dir = os.path.join(project_root, "domains")
        self._domains: Dict[str, DomainDef] = {}
        self._ct_cache: Optional[Dict[str, Dict[str, List[str]]]] = None

    # ── Domain Loading ──────────────────────────────────────────────────────

    def load_all(self) -> Dict[str, DomainDef]:
        """Load all 8 SDTM domains."""
        for domain in ["DM", "AE", "CM", "VS", "LB", "EX", "DS", "MH"]:
            self.get_domain(domain)
        return self._domains

    def get_domain(self, name: str) -> DomainDef:
        """Get a domain definition by name, loading from markdown if needed."""
        name = name.upper()
        if name not in self._domains:
            self._domains[name] = self._load_domain(name)
        return self._domains[name]

    def _load_domain(self, name: str) -> DomainDef:
        """Parse a single domain markdown file."""
        filename = {
            "DM": "demographics-dm.md", "AE": "adverse-events-ae.md",
            "CM": "concomitant-meds-cm.md", "VS": "vital-signs-vs.md",
            "LB": "laboratory-lb.md", "EX": "exposure-ex.md",
            "DS": "disposition-ds.md", "MH": "medical-history-mh.md",
        }.get(name, f"{name.lower()}.md")

        filepath = os.path.join(self.domains_dir, filename)
        if not os.path.exists(filepath):
            return self._fallback_domain(name)

        raw = self._read_frontmatter(filepath)
        return self._parse_domain(name, raw, filepath)

    def _read_frontmatter(self, filepath: str) -> Dict:
        """Extract YAML frontmatter from a markdown file."""
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        # Extract YAML between --- markers
        match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        if not match:
            return {}
        try:
            return yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return {}

    def _is_valid_variable(self, name: str, domain: str) -> bool:
        """Check if a variable name is a valid SDTM variable for the domain."""
        # Core variables present in all domains
        if name in ("STUDYID", "DOMAIN", "USUBJID"):
            return True
        # Domain-specific variables: must start with the domain prefix
        if name.startswith(domain) and len(name) > len(domain):
            # Must be alphanumeric after prefix (e.g., AESEQ, not "AE Term")
            suffix = name[len(domain):]
            if suffix and suffix[0].isalpha() and suffix.isalnum():
                return True
        # DM-specific: SUBJID, SITEID, AGE, AGEU, SEX, RACE, ETHNIC, ARMCD, ARM,
        # COUNTRY, BRTHDTC, RFSTDTC, RFENDTC, RFICDTC, RFPENDTC, DTHFL, DTHDTC,
        # ACTARMCD, ACTARM don't all start with "DM"
        if domain == "DM":
            return name in ("STUDYID", "DOMAIN", "USUBJID", "SUBJID", "SITEID",
                "AGE", "AGEU", "SEX", "RACE", "ETHNIC", "ARMCD", "ARM", "COUNTRY",
                "BRTHDTC", "RFSTDTC", "RFENDTC", "RFICDTC", "RFPENDTC",
                "DTHFL", "DTHDTC", "ACTARMCD", "ACTARM")
        return False

    def _parse_domain(self, name: str, raw: Dict, filepath: str) -> DomainDef:
        """Parse structured YAML frontmatter into a DomainDef."""
        dd = DomainDef(
            name=name,
            description=raw.get("description", ""),
            observation_class=self.OBSERVATION_CLASSES.get(name, "FINDINGS"),
            source_file=filepath,
        )

        # Parse variables from frontmatter
        var_list = raw.get("variables", [])
        for var_data in var_list:
            if isinstance(var_data, dict):
                var_name = var_data.get("name", "")
                # Skip spurious entries from prose table parsing
                if not self._is_valid_variable(var_name, name):
                    continue
                dd.variables.append(VariableDef(
                    name=var_name,
                    label=var_data.get("label", ""),
                    type=var_data.get("type", "text"),
                    length=int(var_data.get("length", 8)),
                    required=var_data.get("required", False),
                    codelist=var_data.get("codelist"),
                    origin=var_data.get("origin", "CRF"),
                    description=var_data.get("description", ""),
                ))

        # Parse controlled terminology from frontmatter
        ct_raw = raw.get("controlled_terminology", {})
        if isinstance(ct_raw, dict):
            dd.controlled_terminology = {
                k: v if isinstance(v, list) else [v] for k, v in ct_raw.items()
            }

        # Parse business rules
        rules_raw = raw.get("business_rules", [])
        if isinstance(rules_raw, list):
            dd.business_rules = [str(r) for r in rules_raw]

        return dd

    # ── Fallback ────────────────────────────────────────────────────────────

    def _fallback_domain(self, name: str) -> DomainDef:
        """Minimal built-in definition for when a domain file is not yet enhanced."""
        fallbacks = {
            "DM": [
                ("STUDYID", "Study Identifier", "text", 20, True),
                ("DOMAIN", "Domain Abbreviation", "text", 2, True),
                ("USUBJID", "Unique Subject Identifier", "text", 40, True),
                ("SUBJID", "Subject Identifier for Study", "text", 20, True),
                ("RFSTDTC", "Reference Start Date/Time", "datetime", 19, True),
                ("RFENDTC", "Reference End Date/Time", "datetime", 19, True),
                ("SITEID", "Study Site Identifier", "text", 10, True),
                ("AGE", "Age", "integer", 8, True),
                ("AGEU", "Age Units", "text", 6, True, "AGEU"),
                ("SEX", "Sex", "text", 1, True, "SEX"),
                ("RACE", "Race", "text", 60, True, "RACE"),
                ("ETHNIC", "Ethnicity", "text", 40, True, "ETHNIC"),
                ("ARMCD", "Planned Arm Code", "text", 20, True),
                ("ARM", "Description of Planned Arm", "text", 200, True),
                ("COUNTRY", "Country", "text", 3, True, "ISO3166"),
                ("BRTHDTC", "Date/Time of Birth", "datetime", 19, False),
                ("RFICDTC", "Date/Time of Informed Consent", "datetime", 19, False),
                ("DTHFL", "Subject Death Flag", "text", 1, False, "NY"),
                ("DTHDTC", "Date/Time of Death", "datetime", 19, False),
                ("ACTARMCD", "Actual Arm Code", "text", 20, False),
                ("ACTARM", "Description of Actual Arm", "text", 200, False),
            ],
        }
        dd = DomainDef(name=name, observation_class=self.OBSERVATION_CLASSES.get(name, "FINDINGS"))
        for vdata in fallbacks.get(name, []):
            req = vdata[4] if len(vdata) > 4 else False
            ct = vdata[5] if len(vdata) > 5 else None
            dd.variables.append(VariableDef(name=vdata[0], label=vdata[1], type=vdata[2], length=vdata[3], required=req, codelist=ct))
        return dd

    # ── Lookup Methods ──────────────────────────────────────────────────────

    def get_variables(self, domain: str) -> List[VariableDef]:
        """Get all variables for a domain."""
        return self.get_domain(domain).variables

    def get_required_variables(self, domain: str) -> List[VariableDef]:
        """Get required variables for a domain."""
        return self.get_domain(domain).required_variables

    def get_column_order(self, domain: str) -> List[str]:
        """Get variable display order for CSV export."""
        return self.get_domain(domain).column_order

    def get_variable_labels(self, domain: str) -> Dict[str, str]:
        """Get variable name → label mapping for data dictionaries."""
        return {v.name: v.label for v in self.get_domain(domain).variables}

    def get_primary_key(self, domain: str) -> Tuple[str, ...]:
        """Get primary key columns for DDL generation."""
        return self.get_domain(domain).primary_key

    def get_all_domains(self) -> List[str]:
        return ["DM", "MH", "AE", "CM", "EX", "DS", "VS", "LB"]

    # ── Controlled Terminology ─────────────────────────────────────────────

    def get_controlled_terminology(self) -> Dict[str, List[str]]:
        """Aggregate all controlled terminology from all domain files."""
        if self._ct_cache is not None:
            return self._ct_cache

        # Built-in CT (from CDISC NCI EVS and code-systems.md)
        ct = {
            "SEX": ["M", "F", "U", "UNDIFFERENTIATED"],
            "RACE": ["WHITE", "BLACK OR AFRICAN AMERICAN", "ASIAN",
                     "AMERICAN INDIAN OR ALASKA NATIVE",
                     "NATIVE HAWAIIAN OR OTHER PACIFIC ISLANDER",
                     "MULTIPLE", "OTHER", "UNKNOWN", "NOT REPORTED"],
            "ETHNIC": ["HISPANIC OR LATINO", "NOT HISPANIC OR LATINO", "NOT REPORTED", "UNKNOWN"],
            "AESEV": ["MILD", "MODERATE", "SEVERE", "LIFE THREATENING", "DEATH"],
            "AEREL": ["NOT RELATED", "UNLIKELY RELATED", "POSSIBLY RELATED", "PROBABLY RELATED", "DEFINITELY RELATED", "RELATED"],
            "AEOUT": ["RECOVERED/RESOLVED", "RECOVERING/RESOLVING", "NOT RECOVERED/NOT RESOLVED", "RECOVERED/RESOLVED WITH SEQUELAE", "FATAL", "UNKNOWN"],
            "AEACN": ["DRUG WITHDRAWN", "DOSE REDUCED", "DOSE NOT CHANGED", "DRUG INTERRUPTED", "NOT APPLICABLE", "UNKNOWN"],
            "DSCAT": ["DISPOSITION EVENT", "PROTOCOL MILESTONE", "OTHER EVENT"],
            "DSSCAT": ["STUDY PARTICIPATION", "TREATMENT", "STUDY COMPLETION"],
            "DSDECOD": ["INFORMED CONSENT OBTAINED", "RANDOMIZED", "COMPLETED",
                        "ADVERSE EVENT", "DEATH", "LOST TO FOLLOW-UP",
                        "WITHDRAWAL BY SUBJECT", "PROTOCOL VIOLATION",
                        "LACK OF EFFICACY", "PHYSICIAN DECISION", "OTHER"],
            "EPOCH": ["SCREENING", "TREATMENT", "FOLLOW-UP"],
            "AGEU": ["YEARS", "MONTHS", "WEEKS", "DAYS"],
            "NY": ["Y", "N"],
            "VSTESTCD": ["SYSBP", "DIABP", "PULSE", "RESP", "TEMP", "HEIGHT", "WEIGHT", "BMI", "OXYSAT"],
            "EXDOSFRQ": ["QD", "BID", "TID", "QID", "Q12H", "Q8H", "Q6H", "QW", "Q2W", "Q3W", "Q4W", "PRN", "ONCE"],
            "EXROUTE": ["ORAL", "INTRAVENOUS", "SUBCUTANEOUS", "INTRAMUSCULAR", "TOPICAL", "INHALED", "SUBLINGUAL", "TRANSDERMAL"],
            "LBCAT": ["CHEMISTRY", "HEMATOLOGY", "URINALYSIS", "COAGULATION", "IMMUNOLOGY"],
            "LBNRIND": ["LOW", "NORMAL", "HIGH", "ABNORMAL"],
        }

        # Override / merge with domain-file-specific CT
        for domain_name in ["DM", "AE", "CM", "VS", "LB", "EX", "DS", "MH"]:
            try:
                dd = self.get_domain(domain_name)
                for key, values in dd.controlled_terminology.items():
                    if key in ct:
                        ct[key] = list(dict.fromkeys(ct[key] + values))  # preserve order while deduplicating
                    else:
                        ct[key] = values
            except Exception:
                pass

        self._ct_cache = ct
        return ct

    def get_ct_rank(self, codelist_name: str, value: str) -> int:
        """Return the 1-based rank/order position of a value in a controlled
        terminology codelist. Returns -1 if the value is not found.

        Useful for CTCAE grade-like ordered terminologies where list position
        corresponds to numeric severity (e.g., AESEV: MILD=1, MODERATE=2, ...).
        """
        ct = self.get_controlled_terminology()
        if codelist_name not in ct:
            return -1
        try:
            return ct[codelist_name].index(value) + 1
        except ValueError:
            return -1

    def validate_ct(self, domain: str, variable: str, value: str) -> Tuple[bool, str]:
        """Validate a value against CDISC controlled terminology."""
        ct = self.get_controlled_terminology()
        dd = self.get_domain(domain)
        # Find the variable's codelist
        for var in dd.variables:
            if var.name == variable and var.codelist:
                codelist_name = var.codelist
                allowed = ct.get(codelist_name, [])
                if allowed:
                    upper_allowed = {v.upper() for v in allowed}
                    if str(value).strip().upper() not in upper_allowed:
                        return False, f"'{value}' not in {codelist_name}"
                break
        return True, ""

    # ── Cross-Domain Queries ────────────────────────────────────────────────

    def get_common_variables(self) -> List[str]:
        """Variables that appear in every domain (core SDTM variables)."""
        return ["STUDYID", "DOMAIN", "USUBJID"]

    def get_date_variables(self, domain: str) -> List[str]:
        """All date/datetime variables in a domain."""
        return [v.name for v in self.get_variables(domain)
                if v.name.endswith(("DTC", "DTM")) or v.type == "datetime"]

    def get_numeric_variables(self, domain: str) -> List[str]:
        """All numeric variables in a domain."""
        return [v.name for v in self.get_variables(domain)
                if v.type in ("integer", "float")]


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience Singleton
# ═══════════════════════════════════════════════════════════════════════════════

_default_parser: Optional[DomainParser] = None

def get_parser(project_root: str = None) -> DomainParser:
    """Get or create the default domain parser."""
    global _default_parser
    if _default_parser is None or project_root is not None:
        root = project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _default_parser = DomainParser(root)
    return _default_parser


# ── CLI (for testing) ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, json
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    dp = DomainParser(root)
    dp.load_all()

    for domain_name in dp.get_all_domains():
        dd = dp.get_domain(domain_name)
        print(f"\n{'='*60}")
        print(f"  {domain_name} — {dd.description[:80]}")
        print(f"  Observation Class: {dd.observation_class}")
        print(f"  Source: {os.path.basename(dd.source_file)}")
        print(f"  Variables: {len(dd.variables)} ({len(dd.required_variables)} required, {len(dd.expected_variables)} expected)")
        print(f"  PK: {dd.primary_key}")
        for v in dd.required_variables:
            print(f"    * {v.name:12s} {v.sql_type:10s} {'NOT NULL':8s} -- {v.label}")
        for v in dd.expected_variables[:5]:
            print(f"      {v.name:12s} {v.sql_type:10s} {'':8s} -- {v.label}")
        if len(dd.expected_variables) > 5:
            print(f"      ... ({len(dd.expected_variables) - 5} more)")

    ct = dp.get_controlled_terminology()
    print(f"\n  Controlled Terminology: {len(ct)} codelists, {sum(len(v) for v in ct.values())} values")
