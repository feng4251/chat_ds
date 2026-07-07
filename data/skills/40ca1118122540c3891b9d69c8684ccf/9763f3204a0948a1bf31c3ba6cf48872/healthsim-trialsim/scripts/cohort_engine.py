#!/usr/bin/env python3
"""
Cohort Engine — Realistic clinical trial data simulator.

Layered architecture inspired by Goldenholz et al. (2025) "A simulated randomized
controlled trial for epilepsy" (PMID: 39892341):

  Layer 1: Correlated patient baselines (multivariate Cholesky)
  Layer 2: Disease natural history + temporal correlation (mixed-effects)
  Layer 3: Treatment effect modulation (heterogeneous, time-to-onset)
  Layer 4: Observation noise (measurement error, reporting noise)
  Layer 5: Arm-specific adverse events

The PICO framework drives all parameterization:
  P — Population: correlated demographics, comorbidity patterns, disease severity
  I — Intervention: dose-response, onset timing, heterogeneous effects
  C — Control: placebo response modeling (regression to mean)
  O — Outcome: continuous change, binary responder, time-to-event

Usage as library:
  from cohort_engine import CohortSimulator, load_population_params

Usage as CLI:
  python cohort_engine.py --ta t2dm --subjects 500 --output sdtm_json/
"""

import json, os, sys, math, random
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

# ── Reproducibility ──────────────────────────────────────────────────────────

def set_seed(seed: int = 42):
    random.seed(seed)
    np.random.seed(seed)

# ── Utility ──────────────────────────────────────────────────────────────────

def _ensure_positive_semidefinite(matrix: np.ndarray) -> np.ndarray:
    """Repair a near-PSD covariance matrix by zeroing negative eigenvalues."""
    eigvals, eigvecs = np.linalg.eigh(matrix)
    eigvals[eigvals < 0] = 0
    return eigvecs @ np.diag(eigvals) @ eigvecs.T


def _days_since(reference_date: datetime, date_str: str) -> int:
    """Parse a date string and return days since reference."""
    fmt_candidates = ["%Y-%m-%d", "%Y-%m", "%Y"]
    for fmt in fmt_candidates:
        try:
            dt = datetime.strptime(date_str[:10], fmt)
            return (dt - reference_date).days
        except (ValueError, IndexError):
            continue
    return 0


# ── Parameter Structures ─────────────────────────────────────────────────────

@dataclass
class VariableDef:
    name: str
    mean: float
    std: float
    lo: Optional[float] = None   # clinical lower bound
    hi: Optional[float] = None   # clinical upper bound
    integer: bool = False
    unit: str = ""

@dataclass
class CorrelationDef:
    var1: str
    var2: str
    rho: float   # Pearson correlation [-1, 1]

@dataclass
class LongitudinalDef:
    variable: str
    time_slope: float          # population-mean change per day (on placebo)
    between_subject_sd: float   # SD of patient-specific intercept (random intercept)
    random_slope_sd: float      # SD of patient-specific slope (random slope)
    within_subject_sd: float    # residual / measurement noise
    visits: List[int]           # day offsets from baseline

@dataclass
class TreatmentEffectDef:
    variable: str
    drug_effect: float          # additive effect on slope (e.g., HbA1c slope -0.003/day)
    onset_halflife_days: float  # time to 50% max effect
    max_effect_days: float      # days to plateau
    responder_rate: float       # fraction achieving clinically meaningful change
    responder_threshold: float  # threshold for responder definition (e.g., SYSBP -5.0 mmHg)
    placebo_effect: float       # initial placebo "improvement" (regression to mean)
    primary_endpoint: bool = False       # designate this as the primary efficacy endpoint
    effect_size_cohens_d: Optional[float] = None  # target detectable effect size for power calc

@dataclass
class AETemplate:
    term: str
    soc: str
    placebo_rate: float
    drug_rate: float
    drug_rate_multiplier: float = 1.0
    severity_dist: Dict[str, float] = field(default_factory=lambda: {
        "MILD": 0.60, "MODERATE": 0.30, "SEVERE": 0.10
    })
    serious_rate: float = 0.02

@dataclass
class PopulationConfig:
    """PICO-structured trial population configuration."""
    therapeutic_area: str
    description: str
    variables: List[VariableDef] = field(default_factory=list)
    correlations: List[CorrelationDef] = field(default_factory=list)
    longitudinal: List[LongitudinalDef] = field(default_factory=list)
    treatment_effects: List[TreatmentEffectDef] = field(default_factory=list)
    ae_templates: List[AETemplate] = field(default_factory=list)

    # Statistical design parameters (for simulation + power analysis)
    alpha: float = 0.05              # Type I error rate
    power_target: float = 0.80       # Target statistical power

    # Study design
    arms: List[Tuple[str, str]] = field(default_factory=lambda: [
        ("ACT", "Active"), ("PBO", "Placebo")
    ])
    ratio: List[float] = field(default_factory=lambda: [1.0, 1.0])
    baseline_days: int = 14
    followup_days: int = 182
    n_sites: int = 50
    countries: List[str] = field(default_factory=lambda: ["USA", "CAN", "DEU", "GBR", "FRA", "JPN"])

    def get_variable(self, name: str) -> Optional[VariableDef]:
        for v in self.variables:
            if v.name == name:
                return v
        return None

    def get_correlation_matrix(self) -> Tuple[np.ndarray, List[str]]:
        """Build the Pearson correlation matrix from pairwise definitions."""
        names = [v.name for v in self.variables]
        n = len(names)
        corr = np.eye(n)
        name_to_idx = {n: i for i, n in enumerate(names)}
        for c in self.correlations:
            if c.var1 in name_to_idx and c.var2 in name_to_idx:
                i, j = name_to_idx[c.var1], name_to_idx[c.var2]
                corr[i, j] = c.rho
                corr[j, i] = c.rho
        return corr, names


# ── Layer 1: Correlated Baseline Generator ───────────────────────────────────

class BaselineGenerator:
    """Generate correlated patient baseline characteristics using multivariate normal."""

    def __init__(self, config: PopulationConfig, seed: int = 42):
        self.config = config
        self.rng = np.random.RandomState(seed)
        self._build_distribution()

    def _build_distribution(self):
        """Construct the multivariate normal from variable means, stds, and correlations."""
        vars_ = self.config.variables
        n = len(vars_)
        self.means = np.array([v.mean for v in vars_])
        self.stds = np.array([v.std for v in vars_])
        self.var_names = [v.name for v in vars_]

        corr_matrix, _ = self.config.get_correlation_matrix()
        # Convert correlation → covariance
        cov = np.outer(self.stds, self.stds) * corr_matrix
        self.cov = _ensure_positive_semidefinite(cov)

    def generate(self, n_subjects: int) -> Dict[str, np.ndarray]:
        """Return dict mapping variable_name → numpy array of length n_subjects."""
        raw = self.rng.multivariate_normal(self.means, self.cov, size=n_subjects)

        # Clip to clinical bounds
        result = {}
        for i, var in enumerate(self.config.variables):
            vals = raw[:, i]
            if var.lo is not None:
                vals = np.maximum(vals, var.lo)
            if var.hi is not None:
                vals = np.minimum(vals, var.hi)
            if var.integer:
                vals = np.round(vals).astype(int)
            else:
                vals = np.round(vals, 1)
            result[var.name] = vals
        return result


# ── Layer 2+3: Longitudinal + Treatment Trajectory ───────────────────────────

class LongitudinalEngine:
    """
    Mixed-effects longitudinal trajectory model.

    Each patient has:
      - A random intercept (deviation from population baseline)
      - A random slope (deviation from population time-trend)
      - Within-subject residual noise per observation

    Treatment modifies the slope additively after onset delay.
    """

    def __init__(self, config: PopulationConfig, seed: int = 42):
        self.config = config
        self.rng = np.random.RandomState(seed)

    def simulate_patient_trajectories(
        self,
        patient_ids: List[str],
        arm_assignments: Dict[str, str],  # usubjid → armcd
        baselines: Dict[str, np.ndarray],  # varname → array
        baseline_date: datetime,
    ) -> Dict[str, List[Dict]]:
        """
        Returns dict: usubjid → [ {visit, visitnum, timepoint_day, VAR1: val, ...}, ...]
        """
        trajectories: Dict[str, List[Dict]] = {pid: [] for pid in patient_ids}

        for ldef in self.config.longitudinal:
            var = ldef.variable
            base_vals = baselines.get(var)
            if base_vals is None:
                continue

            # Find corresponding treatment effect definition
            tx_def = None
            for t in self.config.treatment_effects:
                if t.variable == var:
                    tx_def = t
                    break

            n_patients = len(patient_ids)
            # Random intercept and slope per patient
            rand_intercepts = self.rng.normal(0, ldef.between_subject_sd, n_patients)
            rand_slopes = self.rng.normal(0, ldef.random_slope_sd, n_patients)

            for p_idx, pid in enumerate(patient_ids):
                arm = arm_assignments.get(pid, "PBO")
                base = base_vals[p_idx]
                r_int = rand_intercepts[p_idx]
                r_slp = rand_slopes[p_idx]

                for v_idx, day in enumerate(ldef.visits):
                    # Natural disease course (placebo slope + random deviation)
                    pop_slope = ldef.time_slope
                    natural_change = day * (pop_slope + r_slp)

                    # Treatment effect modulation
                    tx_slope = 0.0
                    if tx_def and arm != "PBO":
                        # Sigmoid onset: effect scales from 0→1 over time
                        if day > 0 and tx_def.onset_halflife_days > 0:
                            onset_fraction = day / (day + tx_def.onset_halflife_days)
                        elif day > 0:
                            onset_fraction = 1.0
                        else:
                            onset_fraction = 0.0

                        # Cap at max effect days (plateau)
                        if tx_def.max_effect_days > 0 and day > tx_def.max_effect_days:
                            effective_day = tx_def.max_effect_days
                            onset_fraction = effective_day / (effective_day + tx_def.onset_halflife_days)

                        tx_slope = tx_def.drug_effect * onset_fraction
                    elif tx_def and arm == "PBO":
                        # Placebo: regression to mean → small initial change that decays
                        if day > 0 and tx_def.placebo_effect != 0:
                            decay = math.exp(-day / 30)  # ~1 month decay
                            tx_slope = tx_def.placebo_effect * decay

                    # Combined trajectory: baseline + random intercept + natural + treatment
                    trajectory_val = base + r_int + natural_change + tx_slope * day

                    # Within-subject observation noise
                    noise = self.rng.normal(0, ldef.within_subject_sd)
                    observed = trajectory_val + noise

                    # Apply clinical bounds
                    vdef = self.config.get_variable(var)
                    if vdef:
                        if vdef.lo is not None:
                            observed = max(observed, vdef.lo)
                        if vdef.hi is not None:
                            observed = min(observed, vdef.hi)
                        if vdef.integer:
                            observed = round(observed)
                        else:
                            observed = round(observed, 1)

                    # Find existing or create visit entry
                    visit_label = f"第{day}天" if day >= 0 else "筛选期"
                    visit_num = v_idx + 1

                    existing = next((e for e in trajectories[pid] if e["timepoint_day"] == day), None)
                    if existing:
                        existing[var] = observed
                    else:
                        trajectories[pid].append({
                            "visit": visit_label,
                            "visitnum": visit_num,
                            "timepoint_day": day,
                            var: observed,
                        })

        # Sort each patient's trajectories by timepoint
        for pid in patient_ids:
            trajectories[pid].sort(key=lambda x: x["timepoint_day"])

        return trajectories


# ── Layer 4+5: Adverse Event Engine ──────────────────────────────────────────

class AdverseEventEngine:
    """Generate treatment-arm-specific adverse events with realistic patterns."""

    def __init__(self, config: PopulationConfig, study_id: str = "", seed: int = 42):
        self.config = config
        self.study_id = study_id
        self.rng = np.random.RandomState(seed)

    def generate(
        self,
        patient_ids: List[str],
        arm_assignments: Dict[str, str],
        subject_dates: Dict[str, Dict],
    ) -> List[Dict]:
        """Return list of AE records (SDTM-compatible dicts).

        Args:
            subject_dates: {usubjid: {rfstdtc, rficdtc, rfendtc}}
        """
        records = []
        seq_counter: Dict[str, int] = {}

        sev_levels = ["MILD", "MODERATE", "SEVERE"]
        sev_weights_templates = {
            "MILD": [0.60, 0.30, 0.10],
            "MODERATE": [0.20, 0.55, 0.25],
            "SEVERE": [0.05, 0.25, 0.70],
        }
        outcomes = ["RECOVERED/RESOLVED"] * 70 + ["RECOVERING/RESOLVING"] * 20 + ["NOT RECOVERED/NOT RESOLVED"] * 10

        for pid in patient_ids:
            arm = arm_assignments.get(pid, "PBO")
            seq_counter[pid] = 0

            for ae_tmpl in self.config.ae_templates:
                # Each AE is a Bernoulli trial per patient per template
                rate = ae_tmpl.drug_rate if arm != "PBO" else ae_tmpl.placebo_rate
                if self.rng.random() > rate:
                    continue

                seq_counter[pid] += 1
                onset_day = self.rng.randint(1, self.config.followup_days)
                duration_days = max(1, int(self.rng.exponential(14)))
                subj_rfstdtc = subject_dates[pid]["rfstdtc"]
                onset_date = subj_rfstdtc + timedelta(days=onset_day)
                end_date = onset_date + timedelta(days=duration_days)

                # Severity with arm-specific distribution
                sev_dist = ae_tmpl.severity_dist
                sev = random.choices(
                    list(sev_dist.keys()),
                    weights=list(sev_dist.values())
                )[0]

                is_sae = self.rng.random() < ae_tmpl.serious_rate

                rec = {
                    "STUDYID": self.study_id,
                    "DOMAIN": "AE",
                    "USUBJID": pid,
                    "AESEQ": seq_counter[pid],
                    "AETERM": ae_tmpl.term,
                    "AEDECOD": ae_tmpl.term,
                    "AEBODSYS": ae_tmpl.soc,
                    "AESEV": sev,
                    "AESER": "Y" if is_sae else "N",
                    "AEACN": "DOSE NOT CHANGED",
                    "AEREL": self._assign_relationship(arm),
                    "AEOUT": random.choice(outcomes),
                    "AESTDTC": onset_date.strftime("%Y-%m-%d"),
                    "AEENDTC": end_date.strftime("%Y-%m-%d"),
                    "AESTDY": onset_day + 1,
                    "AEENDY": onset_day + duration_days + 1,
                }
                if is_sae:
                    rec.update({
                        "AESCONG": "N", "AESDISAB": "N", "AESDTH": "N",
                        "AESHOSP": "Y", "AESLIFE": "N", "AESMIE": "Y",
                    })
                records.append(rec)

        return records

    def _assign_relationship(self, arm: str) -> str:
        """分配因果关系评估；试验药物组具有更高的相关性概率。"""
        if arm == "PBO":
            weights = [0.05, 0.10, 0.65, 0.20]
        else:
            weights = [0.15, 0.25, 0.40, 0.20]
        return random.choices(
            ["RELATED", "POSSIBLY RELATED", "NOT RELATED", "PROBABLY RELATED"],
            weights=weights,
        )[0]


# ── Orchestrator ─────────────────────────────────────────────────────────────

class CohortSimulator:
    """
    PICO-driven cohort simulator producing realistic SDTM records.

    Usage:
        config = load_population_params("t2dm")
        sim = CohortSimulator(config, seed=42)
        study = sim.generate(n_subjects=500)
        # study.dm, study.ae, study.vs, study.lb, study.ex, study.ds, study.cm, study.mh
    """

    def __init__(self, config: PopulationConfig, seed: int = 42):
        self.config = config
        self.seed = seed
        set_seed(seed)
        # 中文试验编号前缀映射
        _ta_prefix_map = {
            "t2dm": "T2DM", "mash": "MASH", "hypertension": "HTN",
            "epilepsy": "EPI", "alzheimer": "AD", "cns": "CNS",
        }
        _prefix = _ta_prefix_map.get(config.therapeutic_area, config.therapeutic_area.upper())
        self.study_id = f"{_prefix}-III期-301"
        self.baseline_date = datetime(2025, 1, 15)
        self._init_engines()

    def _init_engines(self):
        self.baseline_gen = BaselineGenerator(self.config, self.seed)
        self.longitudinal_engine = LongitudinalEngine(self.config, self.seed + 1)
        self.ae_engine = AdverseEventEngine(self.config, self.study_id, self.seed + 2)

    def generate(self, n_subjects: int) -> "StudyData":
        """
        Run the full simulation pipeline:
          1. Assign arms
          2. Generate correlated baselines
          3. Simulate longitudinal trajectories
          4. Generate AEs, VS, LB from trajectories
          5. Build all 8 SDTM domains
        """
        # ── Arm assignment ─────────────────────────────────────────────
        arm_codes = [a[0] for a in self.config.arms]
        arm_names = [a[1] for a in self.config.arms]
        sites = [f"{i:03d}" for i in range(1, self.config.n_sites + 1)]

        arm_assignments: Dict[str, str] = {}
        arm_display: Dict[str, str] = {}
        site_assignments: Dict[str, str] = {}
        countries: Dict[str, str] = {}

        rng = np.random.RandomState(self.seed + 3)
        for i in range(n_subjects):
            arm_idx = rng.choice(len(arm_codes), p=np.array(self.config.ratio) / sum(self.config.ratio))
            site = sites[i % len(sites)]
            pid = f"{self.study_id}-{site}-{i+1:04d}"
            arm_assignments[pid] = arm_codes[arm_idx]
            arm_display[pid] = arm_names[arm_idx]
            site_assignments[pid] = site
            countries[pid] = self.config.countries[i % len(self.config.countries)]

        patient_ids = list(arm_assignments.keys())

        # ── Subject-specific dates (one set per subject) ───────────────
        date_rng = random.Random(self.seed + 9)
        subject_dates = {}
        for pid in patient_ids:
            rfstdtc = self.baseline_date + timedelta(days=date_rng.randint(0, 210))
            subject_dates[pid] = {
                "rfstdtc": rfstdtc,
                "rficdtc": rfstdtc - timedelta(days=7),
                "rfendtc": rfstdtc + timedelta(days=self.config.followup_days),
            }

        # ── Layer 1: Correlated baselines ──────────────────────────────
        baselines = self.baseline_gen.generate(len(patient_ids))

        # ── Layer 2+3: Longitudinal trajectories ───────────────────────
        trajectories = self.longitudinal_engine.simulate_patient_trajectories(
            patient_ids, arm_assignments, baselines, self.baseline_date
        )

        # ── Layer 4: Observation noise already applied in LongitudinalEngine
        # ── Layer 5: Arm-specific AEs (per-subject dates) ──────────────
        ae_records = self.ae_engine.generate(patient_ids, arm_assignments, subject_dates)

        # ── Build SDTM domains ─────────────────────────────────────────
        dm = self._build_dm(patient_ids, arm_assignments, arm_display, site_assignments, countries, baselines, subject_dates)
        vs = self._build_vs(patient_ids, trajectories, baselines)
        lb = self._build_lb(patient_ids, trajectories, baselines)
        ex = self._build_ex(patient_ids, arm_assignments, arm_display, subject_dates)
        ds = self._build_ds(patient_ids, subject_dates)
        cm = self._build_cm(patient_ids, baselines)
        mh = self._build_mh(patient_ids, baselines)

        return StudyData(
            dm=dm, ae=ae_records, vs=vs, lb=lb, ex=ex, ds=ds, cm=cm, mh=mh,
            config=self.config, study_id=self.study_id,
        )

    def _build_dm(self, patient_ids, arm_assignments, arm_display, site_assignments, countries, baselines, subject_dates):
        records = []
        races_pool = ["WHITE"] * 65 + ["BLACK OR AFRICAN AMERICAN"] * 15 + ["ASIAN"] * 12 + ["OTHER"] * 8
        ethnicities_pool = ["NOT HISPANIC OR LATINO"] * 78 + ["HISPANIC OR LATINO"] * 21 + ["NOT REPORTED"] * 1
        rng = random.Random(self.seed + 10)

        for i, pid in enumerate(patient_ids):
            site = site_assignments[pid]
            age_val = baselines.get("AGE", np.zeros(len(patient_ids)))[i]
            sex_val = "M" if rng.random() < 0.52 else "F"
            sd = subject_dates[pid]
            rfstdtc_date = sd["rfstdtc"]
            rfendtc_date = sd["rfendtc"]
            rficdtc_date = sd["rficdtc"]
            birth_date = rfstdtc_date - timedelta(days=int(age_val) * 365 + rng.randint(0, 364))

            rec = {
                "STUDYID": self.study_id, "DOMAIN": "DM",
                "USUBJID": pid,
                "SUBJID": f"{i+1:04d}", "SITEID": site,
                "RFSTDTC": rfstdtc_date.strftime("%Y-%m-%d"),
                "RFENDTC": rfendtc_date.strftime("%Y-%m-%d"),
                "BRTHDTC": birth_date.strftime("%Y-%m-%d"),
                "AGE": int(age_val), "AGEU": "YEARS", "SEX": sex_val,
                "RACE": rng.choice(races_pool),
                "ETHNIC": rng.choice(ethnicities_pool),
                "ARMCD": arm_assignments[pid], "ARM": arm_display[pid],
                "ACTARMCD": arm_assignments[pid], "ACTARM": arm_display[pid],
                "COUNTRY": countries[pid],
                "DTHFL": None, "DTHDTC": None,
                "RFICDTC": rficdtc_date.strftime("%Y-%m-%d"),
            }
            records.append(rec)
        return records

    def _build_vs(self, patient_ids, trajectories, baselines):
        """Convert longitudinal trajectories to VS domain records."""
        records = []
        vs_configs = [
            ("SYSBP", "收缩压", "mmHg"),
            ("DIABP", "舒张压", "mmHg"),
            ("PULSE", "脉搏率", "次/分"),
            ("WEIGHT", "体重", "kg"),
            ("BMI", "体重指数", "kg/m2"),
        ]
        seq = 0
        for i, pid in enumerate(patient_ids):
            traj = trajectories[pid]
            for visit in traj:
                day = visit["timepoint_day"]
                vnum = visit["visitnum"]
                vname = visit["visit"]
                visit_date = self.baseline_date + timedelta(days=day)

                for tcd, tname, tunit in vs_configs:
                    if tcd in visit:
                        seq += 1
                        val = visit[tcd]
                        records.append({
                            "STUDYID": self.study_id, "DOMAIN": "VS",
                            "USUBJID": pid, "VSSEQ": seq,
                            "VSTESTCD": tcd, "VSTEST": tname,
                            "VSORRES": str(val), "VSORRESU": tunit,
                            "VSSTRESC": str(val), "VSSTRESN": val,
                            "VSSTRESU": tunit,
                            "VISITNUM": vnum, "VISIT": vname,
                            "VSDTC": visit_date.strftime("%Y-%m-%d"),
                        })
        return records

    def _build_lb(self, patient_ids, trajectories, baselines):
        """Convert longitudinal trajectories to LB domain records."""
        lb_configs = [
            ("ALT", "丙氨酸氨基转移酶", "U/L", "1742-6", "CHEMISTRY"),
            ("AST", "天门冬氨酸氨基转移酶", "U/L", "1920-8", "CHEMISTRY"),
            ("CREAT", "肌酐", "mg/dL", "2160-0", "CHEMISTRY"),
            ("HGB", "血红蛋白", "g/dL", "718-7", "HEMATOLOGY"),
            ("PLAT", "血小板", "10^9/L", "777-3", "HEMATOLOGY"),
        ]
        lb_ref_ranges = {
            "ALT": (7, 40), "AST": (5, 35), "CREAT": (0.5, 1.3),
            "HGB": (12, 16), "PLAT": (150, 450),
        }
        records = []
        seq = 0
        for i, pid in enumerate(patient_ids):
            traj = trajectories[pid]
            for visit in traj:
                day = visit["timepoint_day"]
                vnum = visit["visitnum"]
                vname = visit["visit"]
                visit_date = self.baseline_date + timedelta(days=day)

                for tcd, tname, tunit, loinc, lbcat in lb_configs:
                    if tcd in visit:
                        seq += 1
                        val = visit[tcd]
                        lo, hi = lb_ref_ranges[tcd]
                        nrind = "LOW" if val < lo else "HIGH" if val > hi else "NORMAL"
                        records.append({
                            "STUDYID": self.study_id, "DOMAIN": "LB",
                            "USUBJID": pid, "LBSEQ": seq,
                            "LBTESTCD": tcd, "LBTEST": tname, "LBCAT": lbcat,
                            "LBORRES": str(val), "LBORRESU": tunit,
                            "LBSTRESC": str(val), "LBSTRESN": val,
                            "LBSTRESU": tunit,
                            "LBORNRLO": str(lo), "LBORNRHI": str(hi),
                            "LBNRIND": nrind, "LBLOINC": loinc,
                            "VISITNUM": vnum, "VISIT": vname,
                            "LBDTC": visit_date.strftime("%Y-%m-%d"),
                        })
        return records

    def _build_ex(self, patient_ids, arm_assignments, arm_display, subject_dates):
        records = []
        for pid in patient_ids:
            arm = arm_assignments[pid]
            sd = subject_dates[pid]
            ex_drug = arm_display[pid] if arm != "PBO" else "安慰剂"
            records.append({
                "STUDYID": self.study_id, "DOMAIN": "EX",
                "USUBJID": pid, "EXSEQ": 1,
                "EXTRT": ex_drug,
                "EXDOSE": 10 if arm != "PBO" else 0,
                "EXDOSU": "mg", "EXDOSFRQ": "QD",
                "EXROUTE": "ORAL", "EXDOSFRM": "TABLET",
                "EXSTDTC": sd["rfstdtc"].strftime("%Y-%m-%d"),
                "EXENDTC": sd["rfendtc"].strftime("%Y-%m-%d"),
                "EXDUR": self.config.followup_days,
            })
        return records

    def _build_ds(self, patient_ids, subject_dates):
        records = []
        outcomes = [
            ("COMPLETED", 0.85), ("ADVERSE EVENT", 0.06),
            ("WITHDRAWAL BY SUBJECT", 0.05), ("LOST TO FOLLOW-UP", 0.03),
            ("PHYSICIAN DECISION", 0.01),
        ]
        for pid in patient_ids:
            sd = subject_dates[pid]
            icf_date = sd["rficdtc"]
            rfstdtc_date = sd["rfstdtc"]
            end_date = sd["rfendtc"] + timedelta(days=1)
            records.append({
                "STUDYID": self.study_id, "DOMAIN": "DS",
                "USUBJID": pid, "DSSEQ": 1,
                "DSTERM": "已获得知情同意",
                "DSDECOD": "INFORMED CONSENT OBTAINED",
                "DSCAT": "PROTOCOL MILESTONE", "DSSCAT": "STUDY PARTICIPATION",
                "DSSTDTC": icf_date.strftime("%Y-%m-%d"),
                "EPOCH": "SCREENING", "VISITNUM": 1, "VISIT": "筛选期",
            })
            records.append({
                "STUDYID": self.study_id, "DOMAIN": "DS",
                "USUBJID": pid, "DSSEQ": 2,
                "DSTERM": "已随机化", "DSDECOD": "RANDOMIZED",
                "DSCAT": "PROTOCOL MILESTONE", "DSSCAT": "STUDY PARTICIPATION",
                "DSSTDTC": rfstdtc_date.strftime("%Y-%m-%d"),
                "EPOCH": "TREATMENT",
            })
            outcome = random.choices([o[0] for o in outcomes], weights=[o[1] for o in outcomes])[0]
            records.append({
                "STUDYID": self.study_id, "DOMAIN": "DS",
                "USUBJID": pid, "DSSEQ": 3,
                "DSTERM": f"完成研究" if outcome == "COMPLETED" else f"提前退出 - {outcome}",
                "DSDECOD": outcome,
                "DSCAT": "DISPOSITION EVENT", "DSSCAT": "STUDY PARTICIPATION",
                "DSSTDTC": sd["rfendtc"].strftime("%Y-%m-%d"),
                "EPOCH": "FOLLOW-UP",
            })
        return records

    def _build_cm(self, patient_ids, baselines):
        records = []
        meds = [
            ("二甲双胍", "A10BA02", "2型糖尿病"),
            ("阿托伐他汀", "C10AA05", "血脂异常"),
            ("赖诺普利", "C09AA03", "高血压"),
            ("氨氯地平", "C08CA01", "高血压"),
            ("阿司匹林", "B01AC06", "心血管预防"),
        ]
        seq = 0
        rng = random.Random(self.seed + 20)
        for pid in patient_ids:
            for med_name, atc, indication in meds:
                if rng.random() < 0.4:
                    continue
                seq += 1
                records.append({
                    "STUDYID": self.study_id, "DOMAIN": "CM",
                    "USUBJID": pid, "CMSEQ": seq,
                    "CMTRT": med_name, "CMDECOD": med_name,
                    "CMINDC": indication,
                    "CMROUTE": "ORAL", "CMDOSFRQ": "QD",
                    "CMSTDTC": "2020-01-15", "CMENDTC": None, "CMONGO": "Y",
                    "CMATC1CD": atc[0], "CMATC2CD": atc[:3],
                    "CMATC3CD": atc[:4], "CMATC4CD": atc,
                })
        return records

    def _build_mh(self, patient_ids, baselines):
        records = []
        conditions = [
            ("高血压", "血管疾病"),
            ("血脂异常", "代谢及营养类疾病"),
            ("肥胖", "代谢及营养类疾病"),
        ]
        seq = 0
        rng = random.Random(self.seed + 30)
        for pid in patient_ids:
            for cond, soc in conditions:
                if rng.random() < 0.5:
                    continue
                seq += 1
                records.append({
                    "STUDYID": self.study_id, "DOMAIN": "MH",
                    "USUBJID": pid, "MHSEQ": seq,
                    "MHTERM": f"{cond}病史",
                    "MHDECOD": cond, "MHBODSYS": soc,
                    "MHCAT": "既往病史",
                    "MHSTDTC": "2015-01", "MHCONTR": "Y",
                })
        return records


@dataclass
class StudyData:
    """Container for all generated SDTM domain records."""
    dm: List[Dict]
    ae: List[Dict]
    vs: List[Dict]
    lb: List[Dict]
    ex: List[Dict]
    ds: List[Dict]
    cm: List[Dict]
    mh: List[Dict]
    config: PopulationConfig
    study_id: str

    def save(self, output_dir: str):
        """Save all domains to JSON files."""
        os.makedirs(output_dir, exist_ok=True)
        for domain_name, data in [
            ("dm", self.dm), ("ae", self.ae), ("vs", self.vs),
            ("lb", self.lb), ("ex", self.ex), ("ds", self.ds),
            ("cm", self.cm), ("mh", self.mh),
        ]:
            filepath = os.path.join(output_dir, f"{domain_name}.json")
            with open(filepath, "w") as f:
                json.dump({
                    "domain": domain_name.upper(),
                    "records": data,
                    "count": len(data),
                }, f, indent=2)

    def summary(self) -> str:
        lines = [f"试验: {self.study_id} ({self.config.therapeutic_area})"]
        for domain_name, data in [
            ("DM", self.dm), ("AE", self.ae), ("VS", self.vs),
            ("LB", self.lb), ("EX", self.ex), ("DS", self.ds),
            ("CM", self.cm), ("MH", self.mh),
        ]:
            lines.append(f"  {domain_name}: {len(data)} 条记录")
        total = sum(len(d) for d in [self.dm, self.ae, self.vs, self.lb, self.ex, self.ds, self.cm, self.mh])
        lines.append(f"  总计: {total} 条记录")
        return "\n".join(lines)


# ── Population Parameter Templates ───────────────────────────────────────────

def _build_t2dm_config() -> PopulationConfig:
    """Type 2 Diabetes Mellitus — SGLT2i Phase 3 trial."""
    return PopulationConfig(
        therapeutic_area="t2dm",
        description="SGLT2抑制剂对比安慰剂治疗2型糖尿病，26周III期临床试验",
        variables=[
            VariableDef("AGE",  54.2, 11.5, lo=18, hi=75, integer=True, unit="YEARS"),
            VariableDef("SYSBP", 132.0, 14.0, lo=90, hi=180, unit="mmHg"),
            VariableDef("DIABP", 79.0, 9.0, lo=50, hi=100, unit="mmHg"),
            VariableDef("PULSE", 74.0, 10.0, lo=45, hi=110, unit="beats/min"),
            VariableDef("WEIGHT", 89.0, 18.0, lo=45, hi=160, unit="kg"),
            VariableDef("BMI", 32.0, 6.0, lo=18, hi=55, unit="kg/m2"),
            VariableDef("ALT", 25.0, 10.0, lo=3, hi=120, unit="U/L"),
            VariableDef("AST", 22.0, 8.0, lo=3, hi=100, unit="U/L"),
            VariableDef("CREAT", 0.9, 0.2, lo=0.3, hi=2.5, unit="mg/dL"),
            VariableDef("HGB", 14.0, 1.3, lo=9, hi=18, unit="g/dL"),
            VariableDef("PLAT", 250, 60, lo=100, hi=600, integer=True, unit="10^9/L"),
        ],
        correlations=[
            CorrelationDef("AGE", "SYSBP", 0.45),
            CorrelationDef("AGE", "CREAT", 0.30),
            CorrelationDef("WEIGHT", "SYSBP", 0.35),
            CorrelationDef("WEIGHT", "BMI", 0.85),
            CorrelationDef("BMI", "ALT", 0.30),
            CorrelationDef("ALT", "AST", 0.70),
            CorrelationDef("SYSBP", "DIABP", 0.55),
            CorrelationDef("WEIGHT", "DIABP", 0.25),
            CorrelationDef("AGE", "HGB", -0.15),
        ],
        longitudinal=[
            LongitudinalDef("SYSBP", time_slope=-0.005, between_subject_sd=5.0,
                             random_slope_sd=0.01, within_subject_sd=3.5,
                             visits=[-7, 1, 29, 57, 85, 113, 141, 183, 211]),
            LongitudinalDef("DIABP", time_slope=-0.002, between_subject_sd=3.5,
                             random_slope_sd=0.008, within_subject_sd=2.5,
                             visits=[-7, 1, 29, 57, 85, 113, 141, 183, 211]),
            LongitudinalDef("PULSE", time_slope=-0.001, between_subject_sd=4.0,
                             random_slope_sd=0.005, within_subject_sd=3.0,
                             visits=[-7, 1, 29, 57, 85, 113, 141, 183, 211]),
            LongitudinalDef("WEIGHT", time_slope=0.002, between_subject_sd=6.0,
                             random_slope_sd=0.015, within_subject_sd=1.5,
                             visits=[-7, 1, 29, 57, 85, 113, 141, 183, 211]),
            LongitudinalDef("BMI", time_slope=0.001, between_subject_sd=2.5,
                             random_slope_sd=0.005, within_subject_sd=0.5,
                             visits=[-7, 1, 29, 57, 85, 113, 141, 183, 211]),
            LongitudinalDef("ALT", time_slope=0.000, between_subject_sd=4.0,
                             random_slope_sd=0.003, within_subject_sd=2.0,
                             visits=[-7, 1, 57, 183]),
            LongitudinalDef("AST", time_slope=0.000, between_subject_sd=3.5,
                             random_slope_sd=0.002, within_subject_sd=1.8,
                             visits=[-7, 1, 57, 183]),
            LongitudinalDef("CREAT", time_slope=0.0001, between_subject_sd=0.08,
                             random_slope_sd=0.0002, within_subject_sd=0.04,
                             visits=[-7, 1, 57, 183]),
            LongitudinalDef("HGB", time_slope=-0.0005, between_subject_sd=0.5,
                             random_slope_sd=0.001, within_subject_sd=0.3,
                             visits=[-7, 1, 57, 183]),
            LongitudinalDef("PLAT", time_slope=-0.005, between_subject_sd=20,
                             random_slope_sd=0.01, within_subject_sd=10,
                             visits=[-7, 1, 57, 183]),
        ],
        treatment_effects=[
            TreatmentEffectDef("SYSBP", drug_effect=-0.02, onset_halflife_days=14,
                               max_effect_days=56, responder_rate=0.35,
                               responder_threshold=-5.0, placebo_effect=-0.003),
            TreatmentEffectDef("DIABP", drug_effect=-0.01, onset_halflife_days=14,
                               max_effect_days=56, responder_rate=0.30,
                               responder_threshold=-3.0, placebo_effect=-0.001),
            TreatmentEffectDef("WEIGHT", drug_effect=-0.015, onset_halflife_days=21,
                               max_effect_days=84, responder_rate=0.40,
                               responder_threshold=-3.0, placebo_effect=-0.003),
            TreatmentEffectDef("BMI", drug_effect=-0.005, onset_halflife_days=21,
                               max_effect_days=84, responder_rate=0.35,
                               responder_threshold=-1.0, placebo_effect=-0.001),
            TreatmentEffectDef("ALT", drug_effect=-0.003, onset_halflife_days=14,
                               max_effect_days=56, responder_rate=0.25,
                               responder_threshold=-5.0, placebo_effect=-0.001),
            TreatmentEffectDef("HGB", drug_effect=0.001, onset_halflife_days=21,
                               max_effect_days=56, responder_rate=0.10,
                               responder_threshold=0.5, placebo_effect=0.000),
        ],
        ae_templates=[
            AETemplate("尿路感染", "感染及侵染类疾病",
                       placebo_rate=0.05, drug_rate=0.084),
            AETemplate("生殖器真菌感染", "生殖系统及乳腺疾病",
                       placebo_rate=0.01, drug_rate=0.068),
            AETemplate("鼻咽炎", "感染及侵染类疾病",
                       placebo_rate=0.06, drug_rate=0.076),
            AETemplate("头痛", "神经系统疾病",
                       placebo_rate=0.04, drug_rate=0.052),
            AETemplate("恶心", "胃肠系统疾病",
                       placebo_rate=0.03, drug_rate=0.040),
            AETemplate("头晕", "神经系统疾病",
                       placebo_rate=0.02, drug_rate=0.036),
            AETemplate("多尿", "肾脏及泌尿系统疾病",
                       placebo_rate=0.01, drug_rate=0.048),
            AETemplate("口渴", "代谢及营养类疾病",
                       placebo_rate=0.01, drug_rate=0.032),
            AETemplate("腹泻", "胃肠系统疾病",
                       placebo_rate=0.04, drug_rate=0.068),
            AETemplate("背痛", "肌肉骨骼及结缔组织疾病",
                       placebo_rate=0.03, drug_rate=0.035),
        ],
        arms=[("ACT", "XYZ-889 10mg 每日一次"), ("PBO", "安慰剂")],
        ratio=[1.0, 1.0],
    )


def _build_mash_config() -> PopulationConfig:
    """MASH/NASH — Resmetirom Phase 2b trial (THR-beta agonist)."""
    return PopulationConfig(
        therapeutic_area="mash",
        description="Resmetirom（THR-β激动剂）对比安慰剂治疗MASH，52周IIb期临床试验",
        variables=[
            VariableDef("AGE",  52.0, 11.0, lo=18, hi=75, integer=True, unit="YEARS"),
            VariableDef("SYSBP", 128.0, 13.0, lo=90, hi=170, unit="mmHg"),
            VariableDef("DIABP", 78.0, 8.5, lo=50, hi=100, unit="mmHg"),
            VariableDef("PULSE", 72.0, 9.0, lo=45, hi=105, unit="beats/min"),
            VariableDef("WEIGHT", 92.0, 17.0, lo=50, hi=160, unit="kg"),
            VariableDef("BMI", 33.5, 5.5, lo=25, hi=50, unit="kg/m2"),
            VariableDef("ALT", 52.0, 22.0, lo=10, hi=250, unit="U/L"),
            VariableDef("AST", 40.0, 18.0, lo=8, hi=200, unit="U/L"),
            VariableDef("CREAT", 0.85, 0.18, lo=0.3, hi=2.0, unit="mg/dL"),
            VariableDef("HGB", 14.2, 1.2, lo=10, hi=18, unit="g/dL"),
            VariableDef("PLAT", 220, 55, lo=100, hi=500, integer=True, unit="10^9/L"),
        ],
        correlations=[
            CorrelationDef("AGE", "SYSBP", 0.40),
            CorrelationDef("WEIGHT", "BMI", 0.88),
            CorrelationDef("BMI", "ALT", 0.50),
            CorrelationDef("ALT", "AST", 0.75),
            CorrelationDef("SYSBP", "DIABP", 0.50),
            CorrelationDef("WEIGHT", "ALT", 0.38),
            CorrelationDef("AGE", "CREAT", 0.25),
            CorrelationDef("BMI", "SYSBP", 0.30),
        ],
        longitudinal=[
            LongitudinalDef("ALT", time_slope=0.000, between_subject_sd=10.0,
                             random_slope_sd=0.01, within_subject_sd=5.0,
                             visits=[-14, 1, 28, 56, 84, 168, 252, 364, 392]),
            LongitudinalDef("AST", time_slope=0.000, between_subject_sd=8.0,
                             random_slope_sd=0.008, within_subject_sd=4.0,
                             visits=[-14, 1, 28, 56, 84, 168, 252, 364, 392]),
            LongitudinalDef("WEIGHT", time_slope=0.001, between_subject_sd=5.0,
                             random_slope_sd=0.01, within_subject_sd=1.5,
                             visits=[-14, 1, 28, 56, 84, 168, 252, 364, 392]),
            LongitudinalDef("SYSBP", time_slope=-0.002, between_subject_sd=4.5,
                             random_slope_sd=0.008, within_subject_sd=3.0,
                             visits=[-14, 1, 28, 56, 84, 168, 252, 364, 392]),
            LongitudinalDef("DIABP", time_slope=-0.001, between_subject_sd=3.0,
                             random_slope_sd=0.005, within_subject_sd=2.5,
                             visits=[-14, 1, 28, 56, 84, 168, 252, 364, 392]),
            LongitudinalDef("PULSE", time_slope=-0.001, between_subject_sd=3.5,
                             random_slope_sd=0.004, within_subject_sd=2.5,
                             visits=[-14, 1, 28, 56, 84, 168, 252, 364, 392]),
            LongitudinalDef("BMI", time_slope=0.0005, between_subject_sd=2.0,
                             random_slope_sd=0.003, within_subject_sd=0.5,
                             visits=[-14, 1, 28, 56, 84, 168, 252, 364, 392]),
            LongitudinalDef("CREAT", time_slope=0.0001, between_subject_sd=0.07,
                             random_slope_sd=0.0001, within_subject_sd=0.03,
                             visits=[-14, 1, 84, 252, 364]),
            LongitudinalDef("HGB", time_slope=-0.0003, between_subject_sd=0.45,
                             random_slope_sd=0.0008, within_subject_sd=0.25,
                             visits=[-14, 1, 84, 252, 364]),
            LongitudinalDef("PLAT", time_slope=-0.003, between_subject_sd=18,
                             random_slope_sd=0.008, within_subject_sd=8,
                             visits=[-14, 1, 84, 252, 364]),
        ],
        treatment_effects=[
            TreatmentEffectDef("ALT", drug_effect=-0.06, onset_halflife_days=28,
                               max_effect_days=168, responder_rate=0.55,
                               responder_threshold=-17.0, placebo_effect=-0.005),
            TreatmentEffectDef("AST", drug_effect=-0.04, onset_halflife_days=28,
                               max_effect_days=168, responder_rate=0.50,
                               responder_threshold=-12.0, placebo_effect=-0.003),
            TreatmentEffectDef("WEIGHT", drug_effect=-0.01, onset_halflife_days=35,
                               max_effect_days=168, responder_rate=0.30,
                               responder_threshold=-2.0, placebo_effect=-0.003),
        ],
        ae_templates=[
            AETemplate("恶心", "胃肠系统疾病",
                       placebo_rate=0.06, drug_rate=0.12,
                       severity_dist={"MILD": 0.55, "MODERATE": 0.35, "SEVERE": 0.10}),
            AETemplate("腹泻", "胃肠系统疾病",
                       placebo_rate=0.05, drug_rate=0.10,
                       severity_dist={"MILD": 0.60, "MODERATE": 0.30, "SEVERE": 0.10}),
            AETemplate("瘙痒", "皮肤及皮下组织疾病",
                       placebo_rate=0.02, drug_rate=0.06),
            AETemplate("头痛", "神经系统疾病",
                       placebo_rate=0.05, drug_rate=0.08),
            AETemplate("疲劳", "全身性疾病及给药部位各种反应",
                       placebo_rate=0.05, drug_rate=0.07),
            AETemplate("上呼吸道感染", "感染及侵染类疾病",
                       placebo_rate=0.07, drug_rate=0.09),
            AETemplate("鼻咽炎", "感染及侵染类疾病",
                       placebo_rate=0.06, drug_rate=0.08),
            AETemplate("ALT升高", "各类检查",
                       placebo_rate=0.03, drug_rate=0.02,
                       severity_dist={"MILD": 0.50, "MODERATE": 0.35, "SEVERE": 0.15},
                       serious_rate=0.01),
        ],
        arms=[("RES80", "Resmetirom 80mg 每日一次"), ("PBO", "安慰剂")],
        ratio=[1.0, 1.0],
    )


def _build_hypertension_config() -> PopulationConfig:
    """Hypertension — ACEi/ARB Phase 3 trial."""
    return PopulationConfig(
        therapeutic_area="hypertension",
        description="ACE抑制剂对比安慰剂治疗高血压，12周III期临床试验",
        variables=[
            VariableDef("AGE",  56.0, 12.0, lo=18, hi=80, integer=True, unit="YEARS"),
            VariableDef("SYSBP", 152.0, 12.0, lo=140, hi=180, unit="mmHg"),
            VariableDef("DIABP", 93.0, 7.0, lo=85, hi=110, unit="mmHg"),
            VariableDef("PULSE", 75.0, 10.0, lo=50, hi=110, unit="beats/min"),
            VariableDef("WEIGHT", 85.0, 16.0, lo=45, hi=150, unit="kg"),
            VariableDef("BMI", 30.0, 5.5, lo=18, hi=45, unit="kg/m2"),
            VariableDef("ALT", 24.0, 9.0, lo=3, hi=100, unit="U/L"),
            VariableDef("AST", 22.0, 8.0, lo=3, hi=90, unit="U/L"),
            VariableDef("CREAT", 0.95, 0.22, lo=0.4, hi=2.5, unit="mg/dL"),
            VariableDef("HGB", 14.0, 1.3, lo=10, hi=18, unit="g/dL"),
            VariableDef("PLAT", 245, 58, lo=120, hi=550, integer=True, unit="10^9/L"),
        ],
        correlations=[
            CorrelationDef("AGE", "SYSBP", 0.50),
            CorrelationDef("SYSBP", "DIABP", 0.50),
            CorrelationDef("WEIGHT", "SYSBP", 0.30),
            CorrelationDef("WEIGHT", "BMI", 0.85),
            CorrelationDef("AGE", "CREAT", 0.32),
            CorrelationDef("ALT", "AST", 0.70),
            CorrelationDef("AGE", "HGB", -0.12),
        ],
        longitudinal=[
            LongitudinalDef("SYSBP", time_slope=-0.002, between_subject_sd=4.0,
                             random_slope_sd=0.015, within_subject_sd=4.0,
                             visits=[-7, 1, 14, 28, 56, 84, 98]),
            LongitudinalDef("DIABP", time_slope=-0.001, between_subject_sd=2.5,
                             random_slope_sd=0.01, within_subject_sd=2.5,
                             visits=[-7, 1, 14, 28, 56, 84, 98]),
            LongitudinalDef("PULSE", time_slope=-0.0005, between_subject_sd=3.5,
                             random_slope_sd=0.005, within_subject_sd=3.0,
                             visits=[-7, 1, 14, 28, 56, 84, 98]),
            LongitudinalDef("WEIGHT", time_slope=0.001, between_subject_sd=5.0,
                             random_slope_sd=0.01, within_subject_sd=1.0,
                             visits=[-7, 1, 14, 28, 56, 84, 98]),
            LongitudinalDef("ALT", time_slope=0.000, between_subject_sd=3.5,
                             random_slope_sd=0.002, within_subject_sd=1.8,
                             visits=[-7, 1, 28, 84]),
            LongitudinalDef("AST", time_slope=0.000, between_subject_sd=3.0,
                             random_slope_sd=0.002, within_subject_sd=1.5,
                             visits=[-7, 1, 28, 84]),
            LongitudinalDef("CREAT", time_slope=0.0002, between_subject_sd=0.08,
                             random_slope_sd=0.0002, within_subject_sd=0.04,
                             visits=[-7, 1, 28, 84]),
            LongitudinalDef("HGB", time_slope=-0.0003, between_subject_sd=0.5,
                             random_slope_sd=0.001, within_subject_sd=0.3,
                             visits=[-7, 1, 28, 84]),
            LongitudinalDef("PLAT", time_slope=-0.003, between_subject_sd=18,
                             random_slope_sd=0.008, within_subject_sd=9,
                             visits=[-7, 1, 28, 84]),
        ],
        treatment_effects=[
            TreatmentEffectDef("SYSBP", drug_effect=-0.04, onset_halflife_days=7,
                               max_effect_days=28, responder_rate=0.55,
                               responder_threshold=-10.0, placebo_effect=-0.008),
            TreatmentEffectDef("DIABP", drug_effect=-0.025, onset_halflife_days=7,
                               max_effect_days=28, responder_rate=0.50,
                               responder_threshold=-6.0, placebo_effect=-0.005),
        ],
        ae_templates=[
            AETemplate("头晕", "神经系统疾病",
                       placebo_rate=0.02, drug_rate=0.06),
            AETemplate("咳嗽", "呼吸系统、胸及纵隔疾病",
                       placebo_rate=0.02, drug_rate=0.10),
            AETemplate("头痛", "神经系统疾病",
                       placebo_rate=0.04, drug_rate=0.06),
            AETemplate("低血压", "血管疾病",
                       placebo_rate=0.01, drug_rate=0.035),
            AETemplate("疲劳", "全身性疾病及给药部位各种反应",
                       placebo_rate=0.04, drug_rate=0.06),
        ],
        arms=[("ACT", "ACEi 10mg 每日一次"), ("PBO", "安慰剂")],
        ratio=[1.0, 1.0],
    )


def _build_epilepsy_config() -> PopulationConfig:
    """Epilepsy — Anti-seizure medication Phase 3 (based on Goldenholz et al. 2025)."""
    return PopulationConfig(
        therapeutic_area="epilepsy",
        description="抗癫痫药物对比安慰剂治疗局灶性癫痫，12周维持期临床试验",
        variables=[
            VariableDef("AGE", 36.0, 12.0, lo=18, hi=70, integer=True, unit="YEARS"),
            VariableDef("SYSBP", 118.0, 12.0, lo=85, hi=160, unit="mmHg"),
            VariableDef("DIABP", 73.0, 8.0, lo=45, hi=95, unit="mmHg"),
            VariableDef("PULSE", 72.0, 10.0, lo=50, hi=105, unit="beats/min"),
            VariableDef("WEIGHT", 74.0, 15.0, lo=40, hi=130, unit="kg"),
            VariableDef("BMI", 25.5, 4.8, lo=16, hi=40, unit="kg/m2"),
            VariableDef("ALT", 22.0, 9.0, lo=3, hi=100, unit="U/L"),
            VariableDef("AST", 21.0, 8.0, lo=3, hi=90, unit="U/L"),
            VariableDef("CREAT", 0.8, 0.18, lo=0.3, hi=2.0, unit="mg/dL"),
            VariableDef("HGB", 13.8, 1.3, lo=10, hi=18, unit="g/dL"),
            VariableDef("PLAT", 240, 55, lo=130, hi=550, integer=True, unit="10^9/L"),
        ],
        correlations=[
            CorrelationDef("AGE", "SYSBP", 0.35),
            CorrelationDef("SYSBP", "DIABP", 0.50),
            CorrelationDef("WEIGHT", "BMI", 0.87),
            CorrelationDef("ALT", "AST", 0.72),
            CorrelationDef("WEIGHT", "SYSBP", 0.28),
        ],
        longitudinal=[
            LongitudinalDef("SYSBP", time_slope=-0.001, between_subject_sd=4.0,
                             random_slope_sd=0.005, within_subject_sd=3.0,
                             visits=[-60, -30, 1, 30, 60, 90, 120]),
            LongitudinalDef("DIABP", time_slope=-0.0005, between_subject_sd=2.5,
                             random_slope_sd=0.004, within_subject_sd=2.0,
                             visits=[-60, -30, 1, 30, 60, 90, 120]),
            LongitudinalDef("PULSE", time_slope=-0.001, between_subject_sd=3.5,
                             random_slope_sd=0.004, within_subject_sd=2.5,
                             visits=[-60, -30, 1, 30, 60, 90, 120]),
            LongitudinalDef("WEIGHT", time_slope=0.0005, between_subject_sd=5.0,
                             random_slope_sd=0.008, within_subject_sd=1.0,
                             visits=[-60, -30, 1, 30, 60, 90, 120]),
            LongitudinalDef("ALT", time_slope=0.000, between_subject_sd=3.0,
                             random_slope_sd=0.002, within_subject_sd=1.5,
                             visits=[-60, 1, 60, 120]),
            LongitudinalDef("AST", time_slope=0.000, between_subject_sd=2.8,
                             random_slope_sd=0.0015, within_subject_sd=1.3,
                             visits=[-60, 1, 60, 120]),
            LongitudinalDef("CREAT", time_slope=0.0001, between_subject_sd=0.07,
                             random_slope_sd=0.0001, within_subject_sd=0.03,
                             visits=[-60, 1, 60, 120]),
            LongitudinalDef("HGB", time_slope=-0.0003, between_subject_sd=0.45,
                             random_slope_sd=0.0008, within_subject_sd=0.25,
                             visits=[-60, 1, 60, 120]),
            LongitudinalDef("PLAT", time_slope=-0.002, between_subject_sd=17,
                             random_slope_sd=0.006, within_subject_sd=8,
                             visits=[-60, 1, 60, 120]),
        ],
        treatment_effects=[
            TreatmentEffectDef("SYSBP", drug_effect=-0.003, onset_halflife_days=14,
                               max_effect_days=42, responder_rate=0.10,
                               responder_threshold=-3.0, placebo_effect=-0.001),
        ],
        ae_templates=[
            AETemplate("嗜睡", "神经系统疾病",
                       placebo_rate=0.08, drug_rate=0.22),
            AETemplate("头晕", "神经系统疾病",
                       placebo_rate=0.06, drug_rate=0.18),
            AETemplate("疲劳", "全身性疾病及给药部位各种反应",
                       placebo_rate=0.06, drug_rate=0.15),
            AETemplate("头痛", "神经系统疾病",
                       placebo_rate=0.10, drug_rate=0.14),
            AETemplate("复视", "眼器官疾病",
                       placebo_rate=0.02, drug_rate=0.10),
            AETemplate("共济失调", "神经系统疾病",
                       placebo_rate=0.02, drug_rate=0.08),
            AETemplate("恶心", "胃肠系统疾病",
                       placebo_rate=0.05, drug_rate=0.10),
        ],
        arms=[("ACT", "Cenobamate 400mg 每日一次"), ("PBO", "安慰剂")],
        ratio=[1.0, 1.0],
        baseline_days=60,
        followup_days=90,
    )


# ── Registry ──────────────────────────────────────────────────────────────────

_POPULATION_REGISTRY: Dict[str, callable] = {
    "t2dm": _build_t2dm_config,
    "mash": _build_mash_config,
    "hypertension": _build_hypertension_config,
    "epilepsy": _build_epilepsy_config,
}


def load_population_params(therapeutic_area: str) -> PopulationConfig:
    """Load a built-in population configuration by therapeutic area.

    Supported areas: t2dm, mash, hypertension, epilepsy
    """
    ta = therapeutic_area.lower()
    if ta not in _POPULATION_REGISTRY:
        available = ", ".join(sorted(_POPULATION_REGISTRY.keys()))
        raise ValueError(
            f"Unknown therapeutic area '{ta}'. Available: {available}"
        )
    return _POPULATION_REGISTRY[ta]()


def list_population_params() -> List[str]:
    return sorted(_POPULATION_REGISTRY.keys())


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="队列引擎 — 符合CDISC标准的虚拟临床试验SDTM数据模拟器"
    )
    parser.add_argument("--ta", "--therapeutic-area", default="t2dm",
                        help="治疗领域 (t2dm, mash, hypertension, epilepsy)")
    parser.add_argument("--subjects", "-n", type=int, default=500)
    parser.add_argument("--output", "-o", default="sdtm_json")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--list", action="store_true", help="列出可用的治疗领域模板")
    args = parser.parse_args()

    if args.list:
        print("可用的治疗领域模板：")
        for name in list_population_params():
            cfg = load_population_params(name)
            print(f"  {name:20s} — {cfg.description}")
        return

    print(f"正在加载 '{args.ta}' 人群参数...")
    config = load_population_params(args.ta)
    print(f"  {config.description}")

    sim = CohortSimulator(config, seed=args.seed)
    print(f"正在生成 {args.subjects} 名受试者...")
    study = sim.generate(n_subjects=args.subjects)

    print(study.summary())
    study.save(args.output)
    print(f"已保存至 {args.output}/")


if __name__ == "__main__":
    main()
