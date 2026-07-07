#!/usr/bin/env python3
"""
Generate synthetic SDTM test data for end-to-end testing of the TrialSim pipeline.

Powered by cohort_engine.py for realistic data generation with:
  - Multivariate correlated baselines (Cholesky decomposition)
  - Longitudinal mixed-effects trajectories
  - Treatment effect modulation (sigmoid onset, heterogeneous response)
  - Arm-specific adverse event patterns
  - Observation noise injection

Architecture inspired by Goldenholz et al. (2025) "A simulated randomized
controlled trial for epilepsy" — 5-layer simulation framework.

Usage:
  python generate_test_data.py --ta t2dm --subjects 500 --output sdtm_json/
  python generate_test_data.py --ta mash --subjects 200 --output sdtm_json/
  python generate_test_data.py --ta epilepsy --subjects 240 --seed 42
  python generate_test_data.py --list  # show available therapeutic areas
  python generate_test_data.py --subjects 100   # defaults to t2dm (backward compat)
"""

import json, sys, os, argparse
from cohort_engine import (
    CohortSimulator,
    load_population_params,
    list_population_params,
)


def main():
    parser = argparse.ArgumentParser(
        description="TrialSim SDTM Test Data Generator (cohort_engine-based)"
    )
    parser.add_argument("--ta", "--therapeutic-area", default="t2dm",
                        help="Therapeutic area template (t2dm, mash, hypertension, epilepsy)")
    parser.add_argument("--output", "-o", default="sdtm_json",
                        help="Output directory for SDTM JSON files")
    parser.add_argument("--subjects", "-n", type=int, default=500,
                        help="Number of subjects to generate")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility")
    parser.add_argument("--list", action="store_true",
                        help="List available therapeutic area templates and exit")
    parser.add_argument("--correlate", action="store_true", default=True,
                        help="Use multivariate correlated baselines (default: True)")
    parser.add_argument("--no-correlate", dest="correlate", action="store_false",
                        help="Disable correlation (independent variable generation)")
    parser.add_argument("--longitudinal", action="store_true", default=True,
                        help="Use longitudinal trajectory modeling (default: True)")
    parser.add_argument("--no-longitudinal", dest="longitudinal", action="store_false",
                        help="Disable longitudinal modeling (cross-sectional only)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print detailed simulation diagnostics")
    args = parser.parse_args()

    if args.list:
        print("可用的治疗领域模板：")
        print("=" * 60)
        for name in list_population_params():
            cfg = load_population_params(name)
            n_vars = len(cfg.variables)
            n_ae = len(cfg.ae_templates)
            n_corr = len(cfg.correlations)
            n_lg = len(cfg.longitudinal)
            n_tx = len(cfg.treatment_effects)
            arms_str = " 对比 ".join(f"{a[1]}" for a in cfg.arms)
            print(f"\n  {name} — {cfg.description}")
            print(f"    试验组:          {arms_str}")
            print(f"    变量数:          {n_vars}（{n_corr} 个相关性）")
            print(f"    纵向轨迹:        {n_lg} 条，其中 {n_tx} 条有治疗效应")
            print(f"    不良事件模板:    {n_ae}")
        return

    print(f"TrialSim 虚拟试验数据生成器 v2.0")
    print(f"  治疗领域:     {args.ta}")
    print(f"  受试者数量:   {args.subjects}")
    print(f"  随机种子:     {args.seed}")
    print(f"  相关基线:     {args.correlate}")
    print(f"  纵向建模:     {args.longitudinal}")
    print(f"  输出目录:     {args.output}/")
    print()

    # Load population configuration
    config = load_population_params(args.ta)
    print(f"试验配置: {config.description}")

    if not args.correlate:
        config.correlations = []
    if not args.longitudinal:
        config.longitudinal = []
        config.treatment_effects = []

    # Run simulation
    sim = CohortSimulator(config, seed=args.seed)
    study = sim.generate(n_subjects=args.subjects)

    # Print summary
    print()
    print("=" * 60)
    print(study.summary())
    print("=" * 60)

    # Compute treatment-vs-placebo statistics for key variables
    if args.verbose and config.treatment_effects:
        print("\n治疗效应诊断：")
        print("-" * 40)
        for tx in config.treatment_effects:
            active_vals = []
            placebo_vals = []
            for dm_rec in study.dm:
                pid = dm_rec["USUBJID"]
                arm = dm_rec["ARMCD"]
                # Find baseline and endpoint values from VS/LB
                matching = [r for r in study.vs if r["USUBJID"] == pid and r["VSTESTCD"] == tx.variable]
                if len(matching) >= 2:
                    base = matching[0]["VSSTRESN"]
                    end = matching[-1]["VSSTRESN"]
                    chg = end - base
                    if arm != "PBO":
                        active_vals.append(chg)
                    else:
                        placebo_vals.append(chg)

            if active_vals and placebo_vals:
                import statistics
                act_mean = statistics.mean(active_vals)
                pbo_mean = statistics.mean(placebo_vals)
                delta = act_mean - pbo_mean
                print(f"  {tx.variable:10s}: 对比安慰剂 Δ = {delta:+.2f} (试验组 {act_mean:+.2f}, 安慰剂组 {pbo_mean:+.2f}) | "
                      f"应答阈值={tx.responder_threshold:+.1f} | 起效半衰期={tx.onset_halflife_days}天")

        # AE rate comparison
        print("\n  各组不良事件发生率：")
        active_n = sum(1 for d in study.dm if d["ARMCD"] != "PBO")
        pbo_n = sum(1 for d in study.dm if d["ARMCD"] == "PBO")
        for tmpl in config.ae_templates:
            term = tmpl.term
            act_count = sum(1 for r in study.ae if r["AEDECOD"] == term and
                          any(d["ARMCD"] != "PBO" for d in study.dm if d["USUBJID"] == r["USUBJID"]))
            pbo_count = sum(1 for r in study.ae if r["AEDECOD"] == term and
                          any(d["ARMCD"] == "PBO" for d in study.dm if d["USUBJID"] == r["USUBJID"]))
            print(f"    {term:20s}: 试验组 {act_count/active_n:.2%}  安慰剂组 {pbo_count/pbo_n:.2%}")

    # Save
    study.save(args.output)
    print(f"\n已保存 8 个 SDTM 域至 {args.output}/")


if __name__ == "__main__":
    main()
