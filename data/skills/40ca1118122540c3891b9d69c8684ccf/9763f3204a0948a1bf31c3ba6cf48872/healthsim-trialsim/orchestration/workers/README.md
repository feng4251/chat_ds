# Clinical Design Workers — Team Index

## Overview

The **TrialSim Clinical Design Worker Team (v2.2)** consists of 9 specialized Worker subagents (A through I) that collectively cover the full spectrum of clinical trial design assistance from Phase I to Phase IV, across all therapeutic areas and trial design types. Each Worker is designed with a built-in **Knowledge Gate** that proactively searches ClinicalTrials.gov, PubMed, ICH guidelines, FDA/EMA/NMPA databases, and v2.2新增的 10 个生物医学数据库 (DrugBank/OpenTargets/ChEMBL/UniProt/STRING/KEGG/Reactome/OpenAlex/GWAS/PrimeKG) before generating any output.

## Architecture

```
User Request
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  Orchestrator (orchestrator.yaml v2.2)                    │
│  • Intent classification (17 task types)                  │
│  • Knowledge Context bootstrap (7 pre-fetch sources)      │
│  • Parallel fan-out routing (up to 7 workers)             │
│  • Conflict resolution (6 strategies)                     │
│  • Final report synthesis (18 sections)                   │
└──┬──────┬──────┬──────┬──────┬──────┬──────┬──────┬──────┘
   │      │      │      │      │      │      │      │
   ▼      ▼      ▼      ▼      ▼      ▼      ▼      ▼      ▼
  W-A    W-B    W-C    W-D    W-E    W-F    W-G    W-H    W-I
```

## Worker Team

| ID | Worker | File | Core Function | Best For |
|----|--------|------|--------------|----------|
| **A** | Safety & Efficacy Extractor | `worker-safety-extraction.yaml` | Extract structured AE/SAE/ORR/PFS/OS data from PDF/HTML publications | Literature review, competitive benchmarking, safety profiling |
| **B** | PICO & Standards Auditor | `worker-pico-standards.yaml` | Extract PICO, audit against ICH/FDA/EMA/NMPA guidance | Regulatory gap analysis, protocol review, endpoint validation |
| **C** | Termination Analyst | `worker-termination-analysis.yaml` | Classify trial terminations into 6 categories with 29 subcategories | Learning from failed trials, risk assessment, "what to avoid" |
| **D** | I/E Criteria Designer | `worker-ie-criteria.yaml` | Draft I/E criteria with complexity scoring and risk flags | Protocol writing, enrollment optimization, I/E auditing |
| **E** | Biomarker Matcher | `worker-biomarker-matching.yaml` | Match patients to trials using 10 biomarker types | Precision medicine, patient screening, biomarker-stratified enrollment |
| **F** | AE Adjudicator | `worker-ae-adjudication.yaml` | Map free-text AE descriptions to CTCAE v5.0 + MedDRA v27.0 | Safety data cleaning, AE coding, pharmacovigilance |
| **G** | Target Biology Analyzer | `worker-target-biology.yaml` | Deep target biology: protein structure/domains, PPI networks, pathways, genetic validation, druggability, preclinical evidence matrix (11 DBs) | Target validation, MOA characterization, preclinical evidence review |
| **H** | Competitive Landscape Analyst | `worker-competitive-landscape.yaml` | Competitive intelligence: competitor ID, efficacy/safety benchmarking, differentiation strategy, treatment guidelines, IP trends (7 DBs) | Competitive analysis, differentiation, market intelligence, due diligence |
| **I** | Literature Synthesis Engine | `worker-literature-synthesis.yaml` | Structured evidence synthesis: citation index with evidence grading (Oxford CEBM), citation graphs, thematic analysis, knowledge gaps (5 DBs + PrimeKG fallback) | Systematic literature review, evidence mapping, research trend analysis |

**v2.2新增**: Workers G/H/I 集成了 10 个外部生物医学数据库，实现了模型内嵌知识报告中的靶点生物学深度、竞争格局分析和文献循证合成能力。

## Universality Guarantees

Each Worker is designed to operate across:

- **All Phases**: I, II, III, IV (including 0, 1/2, 2/3)
- **All Therapeutic Areas**: Oncology, Cardiovascular, CNS, Immunology, Rare Disease, Infectious, Metabolic, and more
- **All Design Types**: Superiority, Non-inferiority, Equivalence, Basket, Umbrella, Platform, Adaptive, Dose-escalation
- **All Sponsor Types**: Pharma, Biotech, Academic, CRO

When a Worker encounters a therapeutic area, drug class, or endpoint type NOT covered by the project's 4 built-in TA skills, it triggers the **Knowledge Gate** — actively searching ClinicalTrials.gov, PubMed, and ICH/FDA/EMA databases to acquire the necessary knowledge.

## Routing Decision Tree

### Single-Worker Scenarios
| If user says... | Dispatch |
|----------------|----------|
| "Extract safety data from this PDF/abstract" | **Worker A** |
| "Does this design meet FDA/EMA standards?" | **Worker B** |
| "Why were these trials terminated?" | **Worker C** |
| "Draft I/E criteria for [indication] Phase [N]" | **Worker D** |
| "Match this patient's variants to trials" | **Worker E** |
| "Grade this AE / adjudicate this safety event" | **Worker F** |

### Multi-Worker Scenarios (Fan-Out)
| If user says... | Parallel | Then Sequential |
|----------------|----------|-----------------|
| "Design a Phase III protocol for [drug] in [indication]" | A + B + C | D (after A/B/C) |
| "Comprehensive safety & efficacy review of [drug]" | A + C | — |
| "Full clinical development plan for [drug]" | A + B + C | D (after A/B/C) |
| "Match this patient cohort to trials" | E + D | — |

## Knowledge Gate Pattern

Every Worker follows this universal pattern before generating output:

```
┌──────────────────────────────────────┐
│  KNOWLEDGE GATE (per Worker)         │
│                                       │
│  1. INVENTORY: What do I need?       │
│     Disease, drug class, endpoints   │
│     Regulatory guidances             │
│     Comparable trial benchmarks      │
│                                       │
│  2. SELF-AUDIT: What do I know?      │
│     Project skill files              │
│     Prior context                    │
│                                       │
│  3. GAP SEARCH: What must I fetch?   │
│     → ClinicalTrials.gov             │
│     → PubMed                         │
│     → ICH Guidelines                 │
│     → FDA/EMA/NMPA                   │
│                                       │
│  4. PROCEED or FLAG gaps             │
└──────────────────────────────────────┘
```

## Dependency Graph

```
                    ┌─────────┐
                    │Worker B  │ (PICO & Standards)
                    │  (B can  │
                    │ run solo)│
                    └────┬─────┘
                         │ PICO output feeds into ↓
    ┌────────────────────┼────────────────────┐
    │                    │                    │
    ▼                    ▼                    ▼
┌─────────┐       ┌─────────┐        ┌─────────┐
│Worker A  │       │Worker D  │        │Worker E  │
│(Safety/  │       │(I/E      │        │(Biomarker│
│Efficacy) │       │Criteria) │        │Matching) │
└─────────┘       └─────────┘        └─────────┘
                         ▲                    ▲
                         │                    │
                    ┌─────────┐        ┌─────────┐
                    │Worker C  │        │Worker D  │
                    │(Terminat)│        │(I/E      │
                    │         │        │Criteria) │
                    └─────────┘        └─────────┘

    ┌─────────┐       ┌─────────┐
    │Worker F  │       │Worker E  │
    │(AE Adj)  │       │(Biomarker│
    │  Solo    │       │ Solo too)│
    └─────────┘       └─────────┘
```

## Shared References

All Workers share access to:

| Reference | Path | Used By |
|-----------|------|---------|
| Code Systems (MedDRA, LOINC, ATC) | `references/code-systems.md` | A, D, E, F |
| Data Models (JSON Schema) | `references/data-models.md` | All (output validation) |
| ICH Guidelines Index | `references/ich-guidelines-index.md` | B, C, D |
| Recruitment & Enrollment | `recruitment-enrollment.md` | D |
| Phase Skills | `phase1-dose-escalation.md`, `phase2-proof-of-concept.md`, `phase3-pivotal.md` | B, D |
| TA Skills | `therapeutic-areas/*.md` | B, D |
| External Knowledge Harvester | `skills/external-knowledge-harvester.md` | Orchestrator (pre-fetch) |
