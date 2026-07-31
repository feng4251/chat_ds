import tempfile
import textwrap
import unittest
from pathlib import Path

from skills.loader import load_skill_content


class ExecutionContractCompilerTests(unittest.TestCase):
    def _write(self, root: Path, relative_path: str, content: str) -> None:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def _build_valid_skill(self, root: Path) -> Path:
        self._write(
            root,
            "SKILL.md",
            """
            ---
            name: generic-research-workflow
            version: "3.0"
            description: Runs a generic multi-worker research workflow.
            ---
            # Generic workflow
            Follow the package's declarative orchestration contract.
            """,
        )
        self._write(
            root,
            "orchestration/orchestrator.yaml",
            """
            orchestrator_id: research-orchestrator
            name: Research Orchestrator
            version: "3.0"
            description: Generic orchestration contract v3.0.
            intent_classification:
              description: Select only from package-declared dimensions.
              dimensions:
                task_kind:
                  description: Requested workflow family.
                  values: [lookup, comprehensive]
                  workers_map:
                    lookup: [worker-alpha]
                    comprehensive: [worker-alpha, worker-beta, worker-synthesis]
                domain:
                  values: [general, specialist]
                  knowledge_source_map:
                    general: [references/general.md, "skill:catalog-database"]
                    specialist: [references/specialist.md, WebSearch]
                run_mode:
                  values: [quick, deep]
                  phase_skill_map:
                    quick: resources/quick.md
                    deep: [resources/quick.md, resources/deep.md]
                policy_family:
                  values: [baseline, strict]
                  regulatory_rules:
                    strict:
                      agencies: [agency-a, agency-b]
            knowledge_bootstrap:
              description: Fetch shared evidence once.
              pre_fetch_sources:
                - name: catalog
                  skill: catalog-database
                  query_strategy: search by entity
                  extract_fields: [id, title]
                  retrieval_completeness_policy: exhaustive
              shared_context_template: "Evidence: {evidence}"
            routing_rules:
              direct_lookup:
                patterns: ["lookup.*entity"]
                worker: worker-alpha
                priority: 1
                spawn_mode: direct
              comprehensive:
                patterns: ["build.*comprehensive", "full.*analysis"]
                workers: [worker-alpha, worker-beta]
                sequential_workers: [worker-synthesis]
                priority: 10
                spawn_mode: parallel
                default: true
                requires_full_output: true
                output_mode: full_report
                output_profile:
                  requires_full_output: true
                  name: decision-package
                deliverable:
                  type: markdown
                  canonical: FULL_REPORT.md
                required_files: [references/general.md]
                format_files: [formats/report-output.md]
                supporting_files: [resources/deep.md]
            aggregation:
              description: Reconcile worker outputs.
              steps:
                - step: DEDUPLICATE
                  method: retain one canonical record
                - step: CONSISTENCY
                  checks:
                    - Every claim has provenance
                    - id: totals_match
                      description: Totals agree across artifacts
            conflict_resolution:
              strategies:
                - name: SOURCE_PRIORITY
                  priority: 1
                  applies_to: [all]
                  description: Prefer the authoritative source.
            final_report_template:
              sections:
                - section: Summary
                  order: 1
                  source_worker: worker-synthesis
                - section: Evidence
                  order: 2
                  source_workers: [worker-alpha, worker-beta]
              auto_merge:
                mandatory: true
                command_template: "cat 01_*.md 02_*.md 03_*.md > {NAME}_FULL_REPORT.md"
                output_artifact: "{NAME}_FULL_REPORT.md"
                expected_size_range: "10KB-20KB"
                post_merge_verification:
                  - "Line count > 100"
              narrative_quality_instructions: |
                1. Explain each decision.
                2. Put a transition between modules.
            """,
        )
        self._write(root, "references/general.md", "# General\n")
        self._write(root, "references/specialist.md", "# Specialist\n")
        self._write(root, "resources/quick.md", "# Quick\n")
        self._write(root, "resources/deep.md", "# Deep\n")
        for worker_id, name, dependency in (
            ("worker-alpha", "Evidence Collector", None),
            ("worker-beta", "Independent Reviewer", None),
            ("worker-synthesis", "Synthesis Lead", "worker-alpha"),
        ):
            dependency_yaml = f"depends_on: [{dependency}]" if dependency else "depends_on: []"
            self._write(
                root,
                f"orchestration/workers/{worker_id}.yaml",
                (
                    f"worker_id: {worker_id}\n"
                    f"name: {name}\n"
                    'version: "1.0"\n'
                    f"{dependency_yaml}\n"
                    "tools:\n"
                    "  - name: source_search\n"
                    "    source: built-in\n"
                    "knowledge_gate:\n"
                    "  checks:\n"
                    "    - Evidence is available\n"
                ),
            )
        self._write(
            root,
            "formats/report-output.md",
            """
            # Output format

            3 content files + `README.md` (index) + `_checklist.md` + `FULL_REPORT.md` = 6 total

            The package contains exactly 6 files declared above; no additional files are allowed.

            | File | Sections |
            |---|---|
            | `01_summary.md` | Summary |
            | `02_evidence.md` | Evidence |
            | `03_recommendations.md` | Recommendations |

            Max lines per file | 250

            ## Module File Template

            ```markdown
            # Title
            **Owner**: [worker id]
            **Decision**: [summary]

            **→ Next**: [next module]
            ```

            Every file must include the declared owner and decision markers.

            ## Checklist Template

            ```markdown
            **Checklist Status**: complete
            **Root cause**: only shown during troubleshooting
            ```
            """,
        )
        self._write(
            root,
            "formats/reference-notes.md",
            """
            # Reference Notes

            A troubleshooting example mentions `99_example.md`, but this
            document does not declare an output package.

            ```markdown
            **Reference-only marker**: do not make this a report requirement
            ```
            """,
        )
        return root / "SKILL.md"

    def test_compiles_generic_routes_workers_and_waves(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_md = self._build_valid_skill(Path(temp_dir))
            loaded = load_skill_content(skill_md, skill_dir=str(skill_md.parent), session_id="s1")

        execution = loaded.get("execution_contract") or {}
        workflow = loaded.get("workflow_contract") or {}
        self.assertEqual(execution, workflow.get("execution_contract"))
        self.assertEqual(execution.get("worker_ids"), [
            "worker-alpha",
            "worker-beta",
            "worker-synthesis",
        ])
        workers = {worker["id"]: worker for worker in execution.get("workers") or []}
        self.assertEqual(workers["worker-alpha"]["file"], "orchestration/workers/worker-alpha.yaml")
        self.assertEqual(workers["worker-alpha"]["role_hint"], "Evidence Collector")

        routes = {route["id"]: route for route in execution.get("routes") or []}
        comprehensive = routes["comprehensive"]
        self.assertEqual(comprehensive["priority"], 10)
        self.assertEqual(comprehensive["spawn_mode"], "parallel")
        self.assertEqual(
            comprehensive["parallel_workers"],
            ["worker-alpha", "worker-beta"],
        )
        self.assertEqual(comprehensive["sequential_workers"], ["worker-synthesis"])
        self.assertEqual(
            comprehensive["waves"],
            [
                {
                    "id": "parallel",
                    "mode": "parallel",
                    "workers": ["worker-alpha", "worker-beta"],
                    "dependencies": [],
                },
                {
                    "id": "sequential",
                    "mode": "sequential",
                    "workers": ["worker-synthesis"],
                    "dependencies": ["parallel"],
                },
            ],
        )
        self.assertTrue(comprehensive["default"])
        self.assertTrue(comprehensive["requires_full_output"])
        self.assertEqual(comprehensive["output_mode"], "full_report")
        self.assertEqual(
            comprehensive["output_profile"],
            {"requires_full_output": True, "name": "decision-package"},
        )
        self.assertEqual(
            comprehensive["deliverable"],
            {"type": "markdown", "canonical": "FULL_REPORT.md"},
        )
        self.assertEqual(comprehensive["required_files"], ["references/general.md"])
        self.assertEqual(comprehensive["format_files"], ["formats/report-output.md"])
        self.assertEqual(comprehensive["supporting_files"], ["resources/deep.md"])
        self.assertNotIn("default", routes["direct_lookup"])
        self.assertNotIn("requires_full_output", routes["direct_lookup"])

    def test_compiles_declared_intent_dimensions_without_classification_logic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_md = self._build_valid_skill(Path(temp_dir))
            execution = load_skill_content(
                skill_md,
                skill_dir=str(skill_md.parent),
                session_id="s1",
            )["execution_contract"]

        intent = execution["intent_classification"]
        self.assertEqual(
            intent["descriptions"],
            ["Select only from package-declared dimensions."],
        )
        dimensions = {
            dimension["id"]: dimension
            for dimension in intent["dimensions"]
        }
        self.assertEqual(
            dimensions["task_kind"]["values"],
            ["lookup", "comprehensive"],
        )
        self.assertEqual(
            dimensions["task_kind"]["workers_map"]["comprehensive"],
            ["worker-alpha", "worker-beta", "worker-synthesis"],
        )
        self.assertEqual(
            dimensions["domain"]["knowledge_source_map"]["general"],
            ["references/general.md", "skill:catalog-database"],
        )
        self.assertEqual(
            dimensions["run_mode"]["phase_skill_map"]["deep"],
            ["resources/quick.md", "resources/deep.md"],
        )
        self.assertEqual(
            dimensions["policy_family"]["regulatory_rules"]["strict"]["agencies"],
            ["agency-a", "agency-b"],
        )
        for dimension in dimensions.values():
            self.assertEqual(
                dimension["source_file"],
                "orchestration/orchestrator.yaml",
            )

    def test_compiles_bootstrap_aggregation_conflicts_and_quality(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_md = self._build_valid_skill(Path(temp_dir))
            execution = load_skill_content(
                skill_md,
                skill_dir=str(skill_md.parent),
                session_id="s1",
            )["execution_contract"]

        self.assertEqual(
            execution["knowledge_bootstrap"]["sources"][0]["skill"],
            "catalog-database",
        )
        self.assertEqual(
            execution["knowledge_bootstrap"]["sources"][0][
                "retrieval_completeness_policy"
            ],
            "exhaustive",
        )
        self.assertEqual(len(execution["aggregation"]["steps"]), 2)
        self.assertEqual(
            ["DEDUPLICATE"],
            execution["aggregation"]["steps"][1]["depends_on"],
        )
        self.assertEqual(len(execution["aggregation"]["checks"]), 2)
        self.assertEqual(
            execution["conflict_resolution"]["strategies"][0]["name"],
            "SOURCE_PRIORITY",
        )
        output = execution["output_contract"]
        self.assertEqual(output["declared_section_count"], 2)
        self.assertEqual(output["declared_file_count"], 6)
        self.assertEqual(len(output["declared_modular_files"]), 3)
        self.assertNotIn("99_example.md", output["declared_modular_files"])
        self.assertEqual(
            output["output_format_files"],
            ["formats/report-output.md"],
        )
        self.assertEqual(
            output["declared_ancillary_files"],
            ["README.md", "_checklist.md"],
        )
        self.assertEqual(output["artifact_index"], "README.md")
        self.assertEqual(output["artifact_set_policy"]["mode"], "exact")
        self.assertEqual(len(output["artifact_set_policy"]["artifacts"]), 6)
        self.assertEqual(output["declared_final_artifact"], "{NAME}_FULL_REPORT.md")
        self.assertIn("cat", output["merge_command"])
        self.assertEqual(output["expected_min_lines"], 100)
        quality = execution["quality_contract"]
        self.assertEqual(len(quality["narrative_rules"]), 2)
        self.assertEqual(quality["constraints"]["max_lines_per_file"], 250)
        self.assertIn("**Owner**", quality["template_markers"])
        self.assertNotIn("**Reference-only marker**", quality["template_markers"])
        self.assertEqual(
            quality["required_module_markers"],
            ["**Owner**", "**Decision**", "**→ Next**"],
        )
        self.assertNotIn("**Checklist Status**", quality["required_module_markers"])
        self.assertNotIn("**Root cause**", quality["required_module_markers"])
        mapping = {
            item["file"]: item
            for item in quality["section_file_mapping"]
        }
        self.assertEqual(mapping["01_summary.md"]["section_ids"], ["Summary"])
        self.assertEqual(mapping["02_evidence.md"]["section_ids"], ["Evidence"])
        self.assertEqual(mapping["03_recommendations.md"]["section_ids"], [])
        self.assertEqual(
            mapping["03_recommendations.md"]["unresolved_sections"],
            ["Recommendations"],
        )
        self.assertTrue(
            mapping["03_recommendations.md"]["enforce_heading_count"]
        )
        self.assertEqual(
            mapping["03_recommendations.md"]["required_heading_groups"],
            1,
        )
        self.assertTrue(execution["diagnostics"]["valid"])

    def test_compiles_museum_resource_closure_for_workers_bootstrap_and_aggregation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: museum-digital-catalog
                description: Compile a museum catalog resource closure.
                version: "1.0"
                ---
                # Museum digital catalog
                Reconcile and render collection records from declared resources.
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: museum-digital-catalog
                workers:
                  catalog-reader:
                    file: orchestration/workers/catalog-reader.yaml
                knowledge_bootstrap:
                  sources:
                    - id: local-catalog-schema
                      source: package
                      path: references/catalog-schema.json
                      tools:
                        - name: card-template
                          source: local
                          file: templates/catalog-card.html
                      capabilities:
                        - name: palette
                          resources:
                            primary: assets/palette.bin
                      skills:
                        - source: skill:remote-museum-index
                          path: scripts/not-in-parent.py
                    - id: remote-vocabulary
                      source: https://example.test/vocabulary.json
                      path: https://example.test/vocabulary.json
                    - id: uploaded-inventory
                      path: workspace/inventory.csv
                routing_rules:
                  reconcile:
                    patterns: ["reconcile.*museum.*catalog"]
                    worker: catalog-reader
                    default: true
                aggregation:
                  steps:
                    - id: render-catalog-index
                      tools:
                        - name: index-renderer
                          source: project
                          path: scripts/render-index.mjs
                      capabilities:
                        - files: [templates/catalog-card.html]
                      skills:
                        - source: package
                          resource: references/catalog-schema.json
                """,
            )
            self._write(
                root,
                "orchestration/workers/catalog-reader.yaml",
                """
                worker_id: catalog-reader
                name: Catalog Reader
                tools:
                  - name: schema-reader
                    source: project
                    path: references/catalog-schema.json
                  - name: remote-normalizer
                    source: skill:remote-normalizer
                    path: scripts/not-in-parent.py
                capabilities:
                  - name: label-template
                    source: package
                    file: templates/catalog-card.html
                skills:
                  - name: local-normalizer
                    source: local
                    resources: [scripts/normalize-record.sh]
                """,
            )
            self._write(root, "references/catalog-schema.json", '{"type": "object"}\n')
            self._write(root, "templates/catalog-card.html", "<article></article>\n")
            self._write(root, "scripts/normalize-record.sh", "#!/bin/bash\n")
            self._write(root, "scripts/render-index.mjs", "export default {};\n")
            palette = root / "assets/palette.bin"
            palette.parent.mkdir(parents=True, exist_ok=True)
            palette.write_bytes(b"museum-palette")

            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="museum-resource-closure",
            )["execution_contract"]

        self.assertTrue(execution["diagnostics"]["valid"])
        worker = next(item for item in execution["workers"] if item["id"] == "catalog-reader")
        self.assertIn("tools", worker)
        self.assertIn("capabilities", worker)
        self.assertIn("skills", worker)
        self.assertEqual(
            [
                "references/catalog-schema.json",
                "templates/catalog-card.html",
                "scripts/normalize-record.sh",
            ],
            worker["local_resources"],
        )

        bootstrap = execution["knowledge_bootstrap"]["sources"]
        local_source = next(item for item in bootstrap if item["id"] == "local-catalog-schema")
        self.assertIn("tools", local_source)
        self.assertIn("capabilities", local_source)
        self.assertIn("skills", local_source)
        self.assertEqual(
            [
                "references/catalog-schema.json",
                "templates/catalog-card.html",
                "assets/palette.bin",
            ],
            local_source["local_resources"],
        )
        self.assertNotIn(
            "local_resources",
            next(item for item in bootstrap if item["id"] == "remote-vocabulary"),
        )
        self.assertNotIn(
            "local_resources",
            next(item for item in bootstrap if item["id"] == "uploaded-inventory"),
        )

        aggregation = execution["aggregation"]["steps"][0]
        self.assertIn("tools", aggregation)
        self.assertIn("capabilities", aggregation)
        self.assertIn("skills", aggregation)
        self.assertEqual(
            [
                "scripts/render-index.mjs",
                "templates/catalog-card.html",
                "references/catalog-schema.json",
            ],
            aggregation["local_resources"],
        )

    def test_declared_parent_package_resources_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            root = base / "skill"
            root.mkdir()
            outside = base / "outside.json"
            outside.write_text("{}\n", encoding="utf-8")
            self._write(
                root,
                "SKILL.md",
                "---\nname: archive-resource-safety\ndescription: Validate parent package resource safety.\n---\n# Archive resource safety\n",
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: archive-resource-safety
                routing_rules:
                  inspect:
                    patterns: ["inspect.*archive"]
                    worker: archive-reader
                    default: true
                """,
            )
            self._write(
                root,
                "orchestration/workers/archive-reader.yaml",
                """
                worker_id: archive-reader
                tools:
                  - source: project
                    path: references/missing.json
                  - source: local
                    file: ../outside.json
                  - source: package
                    resource: references/linked.json
                  - path: https://example.test/external.json
                  - path: workspace/upload.json
                  - path: skill:remote-archive/references/index.json
                  - source: skill:remote-archive
                    path: scripts/remote-only.py
                """,
            )
            references = root / "references"
            references.mkdir(parents=True, exist_ok=True)
            (references / "linked.json").symlink_to(outside)

            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="archive-resource-safety",
            )["execution_contract"]

        errors = execution["diagnostics"]["errors"]
        codes = {item["code"] for item in errors}
        self.assertFalse(execution["diagnostics"]["valid"])
        self.assertIn("missing_declared_local_resource", codes)
        self.assertIn("unsafe_declared_local_resource", codes)
        self.assertIn("symlink_declared_local_resource", codes)
        self.assertEqual(3, len([
            item for item in errors
            if item["code"] in {
                "missing_declared_local_resource",
                "unsafe_declared_local_resource",
                "symlink_declared_local_resource",
            }
        ]))

    def test_worker_discovery_requires_registration_or_explicit_worker_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: library-digitization\ndescription: Compile registered library digitization workers.\n---\n# Library digitization\n",
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: library-digitization
                workers:
                  registered-reader:
                    file: workers/registered.yaml
                routing_rules:
                  digitize:
                    patterns: ["digitize.*library"]
                    workers: [self-declared-reader, registered-reader]
                    spawn_mode: parallel
                    default: true
                """,
            )
            self._write(
                root,
                "workers/self-declared.yaml",
                """
                worker_id: self-declared-reader
                name: Self-declared Reader
                """,
            )
            self._write(
                root,
                "workers/registered.yaml",
                """
                name: Registered Reader
                role: Read the registered collection manifest.
                """,
            )
            self._write(
                root,
                "workers/worker-template.yaml",
                """
                id: must-not-become-a-worker
                name: Worker Configuration Template
                fields: [name, role, tools]
                """,
            )
            self._write(
                root,
                "workers/example.md",
                """
                ---
                name: Example only
                ---
                # Worker example
                This is documentation, not executable work.
                """,
            )

            loaded = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="worker-discovery",
            )
            execution = loaded["execution_contract"]

        self.assertTrue(execution["diagnostics"]["valid"])
        self.assertEqual(
            ["registered-reader", "self-declared-reader"],
            execution["worker_ids"],
        )
        workers = {worker["id"]: worker for worker in execution["workers"]}
        self.assertEqual("Registered Reader", workers["registered-reader"]["name"])
        self.assertEqual(
            "Read the registered collection manifest.",
            workers["registered-reader"]["role_hint"],
        )
        self.assertNotIn("must-not-become-a-worker", workers)
        self.assertEqual(
            {"registered-reader", "self-declared-reader"},
            {worker["id"] for worker in loaded["workflow_contract"]["workers"]},
        )
        self.assertEqual(
            ["workers/self-declared.yaml", "workers/registered.yaml"],
            loaded["workflow_contract"]["worker_files"],
        )

    def test_sequential_declared_stage_is_a_worker_chain_and_stages_are_ordered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: observatory-image-pipeline\ndescription: Compile a sequential observatory image pipeline.\n---\n# Observatory image pipeline\n",
            )
            for worker_id in (
                "calibrate-a",
                "calibrate-b",
                "classify",
                "review",
                "publish-a",
                "publish-b",
            ):
                self._write(
                    root,
                    f"workers/{worker_id}.yaml",
                    f"worker_id: {worker_id}\nname: {worker_id}\n",
                )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: observatory-image-pipeline
                routing_rules:
                  process-nightly-images:
                    patterns: ["process.*observatory.*images"]
                    default: true
                    stages:
                      - id: calibrate
                        mode: parallel
                        workers: [calibrate-a, calibrate-b]
                      - id: classify-and-review
                        mode: sequential
                        workers: [classify, review]
                      - id: publish
                        mode: parallel
                        workers: [publish-a, publish-b]
                """,
            )

            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="observatory-stage-chain",
            )["execution_contract"]

        self.assertTrue(execution["diagnostics"]["valid"])
        route = execution["routes"][0]
        self.assertEqual(
            [
                {
                    "id": "calibrate",
                    "mode": "parallel",
                    "workers": ["calibrate-a", "calibrate-b"],
                    "dependencies": [],
                },
                {
                    "id": "classify-and-review-1",
                    "mode": "sequential",
                    "workers": ["classify"],
                    "dependencies": ["calibrate"],
                },
                {
                    "id": "classify-and-review",
                    "mode": "sequential",
                    "workers": ["review"],
                    "dependencies": ["classify-and-review-1"],
                },
                {
                    "id": "publish",
                    "mode": "parallel",
                    "workers": ["publish-a", "publish-b"],
                    "dependencies": ["classify-and-review"],
                },
            ],
            route["waves"],
        )

    def test_workers_and_output_contract_alone_form_a_compiled_orchestrator(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: map-tile-export\ndescription: Compile workers and outputs without an orchestrator ID.\n---\n"
                "# Map tile export\nFollow `workflows/export.yaml` exactly.\n",
            )
            self._write(
                root,
                "workflows/export.yaml",
                """
                workers:
                  tile-exporter:
                    name: Tile Exporter
                    role: Export the declared tile manifest.
                output_contract:
                  declared_artifacts: [tiles.json]
                  declared_file_count: 1
                  artifact_formats:
                    tiles.json: json
                """,
            )

            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="workers-output-only",
            )["execution_contract"]

        self.assertTrue(execution["diagnostics"]["valid"])
        self.assertEqual(["tile-exporter"], execution["worker_ids"])
        self.assertEqual(["tiles.json"], execution["output_contract"]["declared_artifacts"])

    def test_unsupported_execution_graph_invalidates_contract_without_dropping_workers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: unsupported-graph\ndescription: Reject an unsupported execution graph.\n---\n"
                "# Unsupported graph\nFollow `workflows/graph.yaml` exactly.\n",
            )
            self._write(
                root,
                "workflows/graph.yaml",
                """
                workers:
                  graph-worker:
                    name: Graph Worker
                execution_graph:
                  nodes: [graph-worker]
                  edges: []
                output_contract:
                  declared_artifacts: [graph.json]
                  declared_file_count: 1
                """,
            )

            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="unsupported-graph",
            )["execution_contract"]

        self.assertFalse(execution["diagnostics"]["valid"])
        self.assertIn("graph-worker", execution["worker_ids"])
        self.assertEqual(["graph.json"], execution["output_contract"]["declared_artifacts"])
        self.assertIn(
            "unsupported_execution_field",
            {item["code"] for item in execution["diagnostics"]["errors"]},
        )

    def test_non_report_steps_retry_and_timeout_fail_closed(self):
        """Common workflow-engine controls must never be silently discarded."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: cache-maintenance\ndescription: Reject unsupported retry and timeout controls.\n---\n# Cache maintenance\n",
            )
            self._write(
                root,
                "workflows/maintenance.yaml",
                """
                orchestrator_id: cache-maintenance
                workers:
                  cache-cleaner:
                    name: Cache Cleaner
                    role: Remove expired cache entries.
                steps:
                  - id: purge-expired
                    worker: cache-cleaner
                  - id: rebuild-index
                    worker: cache-cleaner
                    depends_on: [purge-expired]
                retry:
                  max_attempts: 3
                  backoff_seconds: 2
                timeout: 90
                """,
            )

            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="cache-maintenance",
            )["execution_contract"]

        diagnostics = execution["diagnostics"]
        self.assertFalse(diagnostics["valid"])
        self.assertEqual(["cache-cleaner"], execution["worker_ids"])
        unsupported = {
            item["field"]: item
            for item in diagnostics["errors"]
            if item.get("code") == "unsupported_execution_field"
        }
        self.assertEqual({"steps", "retry", "timeout"}, set(unsupported))
        for field, item in unsupported.items():
            self.assertEqual(field, item["yaml_path"])
            self.assertEqual("workflows/maintenance.yaml", item["source_file"])
            self.assertEqual("fail_closed", item["disposition"])
            self.assertIn("rather than silently ignoring", item["message"])

    def test_unlinked_execution_only_workflow_document_remains_advisory(self):
        """Generic steps are not a private DSL without an authority signal."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: generic-file-rotation\ndescription: Recognize execution-only workflow declarations.\n---\nRotate files safely.\n",
            )
            self._write(
                root,
                "workflows/rotation.yaml",
                """
                steps:
                  - run: rotate
                max_retries: 2
                timeout_seconds: 30
                """,
            )

            loaded = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="generic-file-rotation",
            )

        self.assertIsNone(loaded.get("execution_contract"))
        workflow = loaded.get("workflow_contract") or {}
        self.assertNotIn(
            "workflows/rotation.yaml",
            workflow.get("workflow_files") or [],
        )
        self.assertIn(
            "workflows/rotation.yaml",
            (loaded.get("linked_files") or {}).get("workflows") or [],
        )

    def test_supported_nested_aggregation_steps_remain_valid(self):
        """The fail-closed rule is scoped; supported nested steps still compile."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: record-deduplicator\ndescription: Compile supported nested aggregation steps.\n---\nDeduplicate records.\n",
            )
            self._write(
                root,
                "workflows/deduplicate.yaml",
                """
                orchestrator_id: record-deduplicator
                aggregation:
                  steps:
                    - step: DEDUPLICATE
                      method: Keep the newest record for each stable key.
                """,
            )

            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="record-deduplicator",
            )["execution_contract"]

        self.assertTrue(execution["diagnostics"]["valid"])
        self.assertEqual("DEDUPLICATE", execution["aggregation"]["steps"][0]["id"])

    def test_reports_invalid_regex_missing_workers_and_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: broken-workflow
                description: Report malformed routes and worker references.
                version: "3.0"
                ---
                # Broken workflow
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: broken
                version: "2.0"
                description: Broken orchestrator v2.0.
                workers:
                  declared-inline:
                    file: orchestration/workers/missing.yaml
                routing_rules:
                  invalid:
                    patterns: ["(unterminated"]
                    worker: missing-route-worker
                    spawn_mode: direct
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="s1",
            )["execution_contract"]

        diagnostics = execution["diagnostics"]
        error_codes = {item["code"] for item in diagnostics["errors"]}
        warning_codes = {item["code"] for item in diagnostics["warnings"]}
        self.assertFalse(diagnostics["valid"])
        self.assertIn("invalid_route_pattern", error_codes)
        self.assertIn("missing_worker_reference", error_codes)
        self.assertIn("missing_worker_file", error_codes)
        self.assertIn("version_mismatch", warning_codes)

    def test_route_regex_contract_rejects_redos_length_and_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: bounded-route-patterns
                description: Bound route regular expression declarations.
                ---
                # Bounded routes
                """,
            )
            self._write(
                root,
                "orchestration/workers/worker.yaml",
                "worker_id: worker\nname: Worker\n",
            )
            patterns = ["(a+)+$", "x" * 513]
            patterns.extend(f"safe-pattern-{index}" for index in range(63))
            pattern_yaml = "\n".join(
                f"      - {pattern!r}" for pattern in patterns
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                (
                    "orchestrator_id: bounded\n"
                    "workers:\n"
                    "  worker:\n"
                    "    file: orchestration/workers/worker.yaml\n"
                    "routing_rules:\n"
                    "  bounded:\n"
                    "    patterns:\n"
                    f"{pattern_yaml}\n"
                    "    worker: worker\n"
                ),
            )
            execution = load_skill_content(
                root / "SKILL.md", skill_dir=str(root), session_id="s1"
            )["execution_contract"]

        errors = execution["diagnostics"]["errors"]
        error_codes = {item["code"] for item in errors}
        self.assertFalse(execution["diagnostics"]["valid"])
        self.assertIn("unsafe_route_pattern", error_codes)
        self.assertIn("route_pattern_too_long", error_codes)
        self.assertIn("too_many_route_patterns", error_codes)
        self.assertLessEqual(len(execution["routes"][0]["patterns"]), 64)

    def test_compiles_explicit_route_overlap_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: ordered-routes\ndescription: Compile an explicit route overlap policy.\n---\n# Ordered routes\n",
            )
            for worker_id in ("small", "large"):
                self._write(
                    root,
                    f"orchestration/workers/{worker_id}.yaml",
                    f"worker_id: {worker_id}\nname: {worker_id}\n",
                )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: ordered
                routing_policy:
                  tie_breaker: explicit_order
                  route_order: [small-route, large-route]
                workers:
                  small:
                    file: orchestration/workers/small.yaml
                  large:
                    file: orchestration/workers/large.yaml
                routing_rules:
                  small-route:
                    patterns: ["overlap"]
                    worker: small
                  large-route:
                    patterns: ["overlap"]
                    worker: large
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md", skill_dir=str(root), session_id="s1"
            )["execution_contract"]

        self.assertTrue(execution["diagnostics"]["valid"])
        self.assertEqual(
            execution["route_selection_policy"],
            {
                "tie_break": "explicit_order",
                "route_order": ["small-route", "large-route"],
            },
        )

    def test_package_route_pattern_total_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: package-pattern-limit\ndescription: Bound aggregate route pattern declarations.\n---\n# Pattern limit\n",
            )
            self._write(
                root,
                "orchestration/workers/worker.yaml",
                "worker_id: worker\nname: Worker\n",
            )
            route_blocks: list[str] = []
            for route_index in range(9):
                patterns = "\n".join(
                    f"      - route-{route_index}-pattern-{pattern_index}"
                    for pattern_index in range(64)
                )
                route_blocks.append(
                    f"  route-{route_index}:\n"
                    "    patterns:\n"
                    f"{patterns}\n"
                    "    worker: worker\n"
                )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                (
                    "orchestrator_id: package-limit\n"
                    "workers:\n"
                    "  worker:\n"
                    "    file: orchestration/workers/worker.yaml\n"
                    "routing_rules:\n"
                    + "".join(route_blocks)
                ),
            )
            execution = load_skill_content(
                root / "SKILL.md", skill_dir=str(root), session_id="s1"
            )["execution_contract"]

        error_codes = {
            item["code"] for item in execution["diagnostics"]["errors"]
        }
        self.assertIn("too_many_route_patterns_total", error_codes)
        self.assertLessEqual(
            sum(len(route.get("patterns") or []) for route in execution["routes"]),
            512,
        )

    def test_lints_intent_worker_and_local_resource_references(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: invalid-intent-contract
                description: Reject invalid intent and resource references.
                ---
                # Invalid intent contract
                """,
            )
            self._write(
                root,
                "orchestration/workers/available.yaml",
                """
                worker_id: available-worker
                name: Available
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: invalid-intent
                intent_classification:
                  dimensions:
                    task:
                      values: [known]
                      workers_map:
                        known: [available-worker, missing-worker]
                        undeclared-value: [available-worker]
                    resource_profile:
                      values: [default]
                      knowledge_source_map:
                        default:
                          - references/missing.md
                          - skill:external-database
                          - WebSearch
                    duplicate-values:
                      values: [one, one]
                routing_rules:
                  direct:
                    patterns: ["run"]
                    worker: available-worker
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="s1",
            )["execution_contract"]

        diagnostics = execution["diagnostics"]
        error_codes = {item["code"] for item in diagnostics["errors"]}
        warning_codes = {item["code"] for item in diagnostics["warnings"]}
        self.assertIn("missing_intent_worker_reference", error_codes)
        self.assertIn("missing_intent_resource_reference", error_codes)
        self.assertIn("intent_mapping_unknown_value", warning_codes)
        self.assertIn("duplicate_intent_dimension_values", warning_codes)

    def test_lints_unsafe_and_missing_route_resource_references(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: invalid-route-resources
                description: Reject unsafe route resource references.
                ---
                # Invalid route resources
                """,
            )
            self._write(
                root,
                "orchestration/workers/available.yaml",
                """
                worker_id: available-worker
                name: Available
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: invalid-route-resources
                routing_rules:
                  invalid-resources:
                    patterns: ["run"]
                    worker: available-worker
                    default: true
                    requires_full_output: true
                    output_mode: full_report
                    output_profile:
                      name: strict-package
                    deliverable: report.md
                    required_files:
                      - ../outside.md
                      - references/missing.md
                    format_files:
                      - /absolute/outside.md
                    supporting_files:
                      - resources/*.missing.md
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="s1",
            )["execution_contract"]

        route = execution["routes"][0]
        self.assertTrue(route["default"])
        self.assertTrue(route["requires_full_output"])
        self.assertEqual(route["output_mode"], "full_report")
        self.assertEqual(route["output_profile"], {"name": "strict-package"})
        self.assertEqual(route["deliverable"], "report.md")
        self.assertEqual(
            route["required_files"],
            ["../outside.md", "references/missing.md"],
        )
        self.assertEqual(route["format_files"], ["/absolute/outside.md"])
        self.assertEqual(route["supporting_files"], ["resources/*.missing.md"])

        diagnostics = execution["diagnostics"]
        self.assertFalse(diagnostics["valid"])
        unsafe = {
            (item.get("field"), item.get("resource"))
            for item in diagnostics["errors"]
            if item["code"] == "unsafe_route_resource_reference"
        }
        missing = {
            (item.get("field"), item.get("resource"))
            for item in diagnostics["errors"]
            if item["code"] == "missing_route_resource_reference"
        }
        self.assertEqual(
            unsafe,
            {
                ("required_files", "../outside.md"),
                ("format_files", "/absolute/outside.md"),
            },
        )
        self.assertEqual(
            missing,
            {
                ("required_files", "references/missing.md"),
                ("supporting_files", "resources/*.missing.md"),
            },
        )

    def test_intent_defaults_and_required_empty_enums_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: invalid-intent-enums
                description: Reject required empty intent enumerations.
                ---
                # Invalid intent enums
                """,
            )
            self._write(
                root,
                "orchestration/workers/available.yaml",
                """
                worker_id: available-worker
                name: Available
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: invalid-intent-enums
                intent_classification:
                  dimensions:
                    mode:
                      values: [quick, deep]
                      default: unsupported
                    required-empty:
                      required: true
                      values: []
                    optional-open:
                      required: false
                routing_rules:
                  direct:
                    patterns: ["run"]
                    worker: available-worker
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="s1",
            )["execution_contract"]

        dimensions = {
            dimension["id"]: dimension
            for dimension in execution["intent_classification"]["dimensions"]
        }
        self.assertEqual(dimensions["mode"]["values"], ["quick", "deep"])
        self.assertEqual(dimensions["mode"]["default"], "unsupported")
        self.assertTrue(dimensions["required-empty"]["required"])
        self.assertNotIn("values", dimensions["required-empty"])
        self.assertFalse(dimensions["optional-open"]["required"])

        diagnostics = execution["diagnostics"]
        self.assertFalse(diagnostics["valid"])
        errors_by_code = {
            item["code"]: item
            for item in diagnostics["errors"]
        }
        self.assertEqual(
            errors_by_code["intent_default_unknown_value"]["dimension_id"],
            "mode",
        )
        self.assertEqual(
            errors_by_code["required_intent_dimension_without_values"]["dimension_id"],
            "required-empty",
        )

    def test_route_worker_dependencies_require_unique_strict_ancestor_waves(self):
        def compile_case(stages: str, *, dependency: bool = True) -> set[str]:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                self._write(
                    root,
                    "SKILL.md",
                    """
                    ---
                    name: route-worker-dag
                    version: "1.0"
                    description: Validate route and worker DAG composition.
                    ---
                    # Route worker DAG
                    """,
                )
                self._write(
                    root,
                    "workers/a.yaml",
                    "worker_id: a\ndepends_on: []\n",
                )
                self._write(
                    root,
                    "workers/b.yaml",
                    (
                        "worker_id: b\ndepends_on: [a]\n"
                        if dependency
                        else "worker_id: b\ndepends_on: []\n"
                    ),
                )
                stage_yaml = textwrap.indent(
                    textwrap.dedent(stages).strip() + "\n",
                    "      ",
                )
                self._write(
                    root,
                    "orchestration/orchestrator.yaml",
                    (
                        "orchestrator_id: route-worker-dag\n"
                        "routing_rules:\n"
                        "  full:\n"
                        "    default: true\n"
                        "    stages:\n"
                        + stage_yaml
                    ),
                )
                execution = load_skill_content(
                    root / "SKILL.md",
                    skill_dir=str(root),
                    session_id="s1",
                )["execution_contract"]
                return {
                    item["code"] for item in execution["diagnostics"]["errors"]
                }

        cases = {
            "same-wave": (
                """
                - id: both
                  mode: parallel
                  workers: [a, b]
                """,
                True,
                "worker_dependency_same_wave",
            ),
            "later-wave": (
                """
                - id: consume
                  mode: sequential
                  workers: [b]
                - id: produce
                  mode: sequential
                  workers: [a]
                  depends_on: [consume]
                """,
                True,
                "worker_dependency_later_wave",
            ),
            "missing-from-route": (
                """
                - id: consume
                  mode: sequential
                  workers: [b]
                """,
                True,
                "worker_dependency_missing_from_route",
            ),
            "duplicate-wave-assignment": (
                """
                - id: first
                  mode: sequential
                  workers: [a]
                - id: second
                  mode: sequential
                  workers: [a]
                  depends_on: [first]
                """,
                False,
                "duplicate_worker_wave_assignment",
            ),
        }
        for label, (stages, dependency, expected_code) in cases.items():
            with self.subTest(label=label):
                self.assertIn(
                    expected_code,
                    compile_case(stages, dependency=dependency),
                )

        valid_codes = compile_case(
            """
            - id: produce
              mode: sequential
              workers: [a]
            - id: consume
              mode: sequential
              workers: [b]
              depends_on: [produce]
            """,
            dependency=True,
        )
        self.assertEqual(set(), valid_codes)

    def test_aggregation_dependencies_are_linted_as_a_dag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: aggregation-dag
                version: "1.0"
                description: Validate aggregation dependencies.
                ---
                # Aggregation DAG
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: aggregation-dag
                aggregation:
                  steps:
                    - id: duplicate
                    - id: duplicate
                    - id: missing
                      depends_on: [not-declared]
                    - id: cycle-a
                      depends_on: [cycle-b]
                    - id: cycle-b
                      depends_on: [cycle-a]
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="s1",
            )["execution_contract"]

        error_codes = {
            item["code"] for item in execution["diagnostics"]["errors"]
        }
        self.assertFalse(execution["diagnostics"]["valid"])
        self.assertIn("duplicate_aggregation_step_id", error_codes)
        self.assertIn("missing_aggregation_dependency", error_codes)
        self.assertIn("aggregation_dependency_cycle", error_codes)

    def test_aggregation_dependency_order_need_not_match_declaration_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: aggregation-topology
                version: "1.0"
                description: Compile a topological aggregation graph.
                ---
                # Aggregation topology
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: aggregation-topology
                aggregation:
                  steps:
                    - id: final
                      depends_on: [review]
                    - id: review
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="s1",
            )["execution_contract"]

        self.assertTrue(execution["diagnostics"]["valid"])
        self.assertEqual(
            ["review"],
            execution["aggregation"]["steps"][0]["depends_on"],
        )

    def test_lints_dependency_graph_duplicate_ids_stages_and_file_arithmetic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: invalid-graph-workflow
                description: Lint dependency graph and stage declarations.
                version: "1.0"
                ---
                # Invalid graph workflow

                Apply [the declared output format](formats/broken-output.md).
                """,
            )
            self._write(
                root,
                "orchestration/workers/alpha.yaml",
                """
                worker_id: worker-alpha
                name: Alpha
                depends_on: [worker-beta]
                """,
            )
            self._write(
                root,
                "orchestration/workers/beta.yaml",
                """
                worker_id: worker-beta
                name: Beta
                depends_on: [worker-alpha]
                """,
            )
            self._write(
                root,
                "orchestration/workers/gamma.yaml",
                """
                worker_id: worker-gamma
                name: Gamma
                depends_on: [worker-missing]
                """,
            )
            self._write(
                root,
                "orchestration/workers/duplicate-alpha.yaml",
                """
                worker_id: worker-alpha
                name: Duplicate Alpha
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: invalid-graph
                version: "1.0"
                routing_rules:
                  repeated-route:
                    patterns: ["first"]
                    worker: worker-alpha
                    spawn_mode: direct
                  repeated-route:
                    patterns: ["second"]
                    worker: worker-beta
                    spawn_mode: direct
                  cyclic-stages:
                    patterns: ["staged"]
                    stages:
                      - id: collect
                        mode: parallel
                        workers: [worker-alpha]
                        depends_on: [finalize]
                      - id: finalize
                        mode: sequential
                        workers: [worker-beta]
                        depends_on: [collect]
                      - id: finalize
                        mode: sequential
                        workers: [worker-gamma]
                        depends_on: [missing-wave]
                """,
            )
            self._write(
                root,
                "formats/broken-output.md",
                """
                # Output Package

                2 content files + `README.md` + `FULL_REPORT.md` = 9 total

                | File | Purpose |
                |---|---|
                | `01_summary.md` | Summary |
                | `02_details.md` | Details |
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="s1",
            )["execution_contract"]

        diagnostics = execution["diagnostics"]
        error_codes = {item["code"] for item in diagnostics["errors"]}
        self.assertFalse(diagnostics["valid"])
        self.assertIn("duplicate_worker_id", error_codes)
        self.assertIn("missing_worker_dependency", error_codes)
        self.assertIn("worker_dependency_cycle", error_codes)
        self.assertIn("duplicate_route_id", error_codes)
        self.assertIn("duplicate_stage_id", error_codes)
        self.assertIn("missing_stage_dependency", error_codes)
        self.assertIn("stage_dependency_cycle", error_codes)
        self.assertIn("file_count_arithmetic_mismatch", error_codes)
        self.assertNotIn("artifact_index", execution["output_contract"])
        self.assertNotIn("artifact_set_policy", execution["output_contract"])

    def test_preserves_data_only_artifact_validators_in_execution_ir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: generic-json-export
                description: Produces a validated JSON export.
                ---
                # Generic JSON export
                Follow the declarative output contract.
                """,
            )
            self._write(
                root,
                "orchestration/orchestrator.yaml",
                """
                orchestrator_id: generic-json-export
                output_contract:
                  declared_artifacts: [records.json]
                  templates: [templates/records.schema.json]
                  artifact_validators:
                    records.json:
                      format: json
                      json_root_type: object
                      required_json_keys: [records]
                      min_bytes: 2
                """,
            )
            self._write(
                root,
                "templates/records.schema.json",
                '{"type":"object","required":["records"]}',
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="s1",
            )["execution_contract"]

        self.assertTrue(execution["diagnostics"]["valid"], execution["diagnostics"])
        self.assertEqual(
            {
                "records.json": {
                    "format": "json",
                    "json_root_type": "object",
                    "required_json_keys": ["records"],
                    "min_bytes": 2,
                }
            },
            execution["output_contract"]["artifact_validators"],
        )
        self.assertEqual(
            ["templates/records.schema.json"],
            execution["output_contract"]["local_resources"],
        )

    def test_worker_compiles_every_named_instruction_output_block(self):
        """Renamed sibling blocks are obligations, not domain-specific prose."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                """
                ---
                name: orbital-assurance
                description: Runs a multi-capability orbital assurance worker.
                ---
                Execute the declared worker contract.
                """,
            )
            self._write(
                root,
                "orchestration/main.yaml",
                """
                orchestrator_id: orbital-assurance
                workers:
                  analyst:
                    file: orchestration/workers/analyst.yaml
                routing_rules:
                  full:
                    patterns: ["full.*orbital"]
                    worker: analyst
                    default: true
                """,
            )
            self._write(
                root,
                "orchestration/workers/analyst.yaml",
                """
                worker_id: analyst
                name: Orbital Assurance Analyst
                instructions: |
                  Extract the declared mission assumptions.
                output_format:
                  mission_assumptions:
                    orbit: string
                thermal_instructions: |
                  Execute the thermal-margin analysis.
                thermal_output_format:
                  thermal_margin:
                    status: string
                radiation_instructions: |
                  Execute the radiation-tolerance analysis.
                radiation_output_format:
                  radiation_tolerance:
                    status: string
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="multi-block-worker",
            )["execution_contract"]

        self.assertTrue(
            execution["diagnostics"]["valid"],
            execution["diagnostics"],
        )
        worker = execution["workers"][0]
        self.assertEqual(
            [
                "mission_assumptions",
                "thermal_margin",
                "radiation_tolerance",
            ],
            list(worker["output_schema"]),
        )
        self.assertEqual(
            ["primary", "thermal", "radiation"],
            [block["id"] for block in worker["instruction_blocks"]],
        )
        self.assertTrue(all(
            block["required"] is True
            and len(block["instruction_sha256"]) == 64
            and len(block["output_sha256"]) == 64
            for block in worker["instruction_blocks"]
        ))
        self.assertEqual(
            [["mission_assumptions"], ["thermal_margin"], ["radiation_tolerance"]],
            [block["required_result_fields"] for block in worker["instruction_blocks"]],
        )

    def test_conflicting_worker_output_blocks_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write(
                root,
                "SKILL.md",
                "---\nname: conflicting-worker\ndescription: Reject ambiguous blocks.\n---\nRun it.\n",
            )
            self._write(
                root,
                "orchestration/main.yaml",
                """
                orchestrator_id: conflicting-worker
                workers:
                  analyst:
                    file: orchestration/workers/analyst.yaml
                """,
            )
            self._write(
                root,
                "orchestration/workers/analyst.yaml",
                """
                worker_id: analyst
                instructions: "Perform the primary analysis."
                output_format:
                  status: string
                audit_instructions: "Perform the independent audit."
                audit_output_format:
                  status:
                    type: object
                """,
            )
            execution = load_skill_content(
                root / "SKILL.md",
                skill_dir=str(root),
                session_id="conflicting-worker-blocks",
            )["execution_contract"]

        self.assertFalse(execution["diagnostics"]["valid"])
        self.assertIn(
            "conflicting_worker_output_field",
            {
                error["code"]
                for error in execution["diagnostics"]["errors"]
            },
        )


if __name__ == "__main__":
    unittest.main()
