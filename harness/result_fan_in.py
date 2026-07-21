"""Provider-aware planning for persisted-result fan-in.

Complex workflows often persist each worker's result and later feed many of
those files to an aggregation worker.  Passing every file to one model request
does not scale: the inputs can exceed either the provider context window or the
runtime's bounded preload payload.  This module plans that fan-in without
reading only a prefix of an artifact or silently dropping a source.

The planner is deliberately independent from ``agent_loop`` and delegation.
It is a deterministic description of work, not an executor.  Callers must
materialize the provenance manifest and every planned reduction artifact,
verify the declared source checksums, and then use the final artifact as the
aggregation prerequisite.
"""

from __future__ import annotations

import codecs
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_PRELOAD_BYTE_ALLOWANCE = 512 * 1024
FAN_IN_PLANNER_VERSION = "fan-in-planner-v2"
FAN_IN_CONTRACT_VERSION = "chatds-bounded-fan-in-v1"
FAN_IN_OUTPUT_POLICY_VERSION = "semantic-reduction-policy-v2"
_BATCH_FIXED_TOKENS = 64
_BATCH_FIXED_BYTES = 256
_ITEM_FRAMING_TOKENS = 32
_ITEM_FRAMING_BYTES = 128
_DEFAULT_OUTPUT_RESERVE_TOKENS = 8_192
_DEFAULT_SYSTEM_TOOL_RESERVE_TOKENS = 4_096
_DEFAULT_MESSAGE_FRAMING_RESERVE_TOKENS = 512


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def estimate_mixed_text_tokens(value: str) -> int:
    """Conservatively estimate mixed ASCII/CJK input tokens.

    This matches the prerequisite accounting used by delegation: four ASCII
    characters per token, while every non-ASCII character counts as one.
    """

    text = str(value or "")
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(0, (ascii_chars + 3) // 4 + non_ascii_chars)


@dataclass(frozen=True)
class PersistedResult:
    """An exact persisted result and its immutable provenance metadata.

    ``content`` is optional because production callers normally keep large
    bodies on disk.  If a caller supplies content, the planner retains the
    exact object and never slices or rewrites it.
    """

    result_id: str
    ordinal: int
    path: str
    byte_size: int
    token_estimate: int
    checksum_sha256: str
    source_worker: str = ""
    consumes: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    content: str | bytes | None = field(default=None, repr=False, compare=False)

    def manifest_record(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "ordinal": self.ordinal,
            "path": self.path,
            "byte_size": self.byte_size,
            "token_estimate": self.token_estimate,
            "checksum_sha256": self.checksum_sha256,
            "source_worker": self.source_worker,
            "consumes": list(self.consumes),
            "provenance": dict(self.provenance),
        }

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        result = self.manifest_record()
        if include_content and self.content is not None:
            result["content"] = self.content
        return result


@dataclass(frozen=True)
class FanInBudget:
    """Effective persisted-result allowance for one model request."""

    provider: str
    context_length_tokens: int
    input_token_allowance: int
    input_byte_allowance: int
    base_prompt_tokens: int
    output_reserve_tokens: int
    system_tool_reserve_tokens: int
    framing_reserve_tokens: int
    safety_margin_tokens: int
    token_allowance_source: str
    byte_allowance_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "context_length_tokens": self.context_length_tokens,
            "input_token_allowance": self.input_token_allowance,
            "input_byte_allowance": self.input_byte_allowance,
            "base_prompt_tokens": self.base_prompt_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "system_tool_reserve_tokens": self.system_tool_reserve_tokens,
            "framing_reserve_tokens": self.framing_reserve_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "token_allowance_source": self.token_allowance_source,
            "byte_allowance_source": self.byte_allowance_source,
        }


@dataclass(frozen=True)
class ProvenanceManifest:
    """Canonical source manifest that every reduction output references."""

    path: str
    records: tuple[Mapping[str, Any], ...]
    checksum_sha256: str

    def render(self) -> str:
        return _canonical_json({"version": 1, "sources": list(self.records)})

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "checksum_sha256": self.checksum_sha256,
            "records": [dict(record) for record in self.records],
        }


@dataclass(frozen=True)
class ReductionArtifact:
    """A not-yet-materialized bounded reduction result."""

    artifact_id: str
    path: str
    estimated_tokens: int
    max_bytes: int
    source_start: int
    source_end: int
    provenance_manifest_path: str
    provenance_manifest_checksum_sha256: str
    immediate_input_ids: tuple[str, ...]
    checksum_sha256: None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "estimated_tokens": self.estimated_tokens,
            "max_bytes": self.max_bytes,
            "source_range": [self.source_start, self.source_end],
            "provenance_manifest_path": self.provenance_manifest_path,
            "provenance_manifest_checksum_sha256": (
                self.provenance_manifest_checksum_sha256
            ),
            "immediate_input_ids": list(self.immediate_input_ids),
            "checksum_sha256": None,
            "materialized": False,
        }


FanInItem = PersistedResult | ReductionArtifact


@dataclass(frozen=True)
class FanInBatch:
    """A complete, ordered set of inputs for one request."""

    batch_id: str
    stage: str
    ordinal: int
    items: tuple[FanInItem, ...]
    estimated_tokens: int
    estimated_bytes: int
    fits_budget: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "stage": self.stage,
            "ordinal": self.ordinal,
            "items": [
                item.to_dict()
                if isinstance(item, ReductionArtifact)
                else item.to_dict(include_content=False)
                for item in self.items
            ],
            "estimated_tokens": self.estimated_tokens,
            "estimated_bytes": self.estimated_bytes,
            "fits_budget": self.fits_budget,
        }


@dataclass(frozen=True)
class ReductionStep:
    """One exact-preload, streaming-source, or balanced merge operation."""

    step_id: str
    wave: int
    ordinal: int
    strategy: str
    input_batch: FanInBatch
    output: ReductionArtifact
    requirements: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "wave": self.wave,
            "ordinal": self.ordinal,
            "strategy": self.strategy,
            "input_batch": self.input_batch.to_dict(),
            "output": self.output.to_dict(),
            "requirements": list(self.requirements),
        }


@dataclass(frozen=True)
class FanInOutputPolicy:
    """Effective, versioned output bounds used by a deterministic plan."""

    version: str
    strategy: str
    merge_topology: str
    max_tokens: int
    max_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "strategy": self.strategy,
            "merge_topology": self.merge_topology,
            "max_tokens": self.max_tokens,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True)
class FanInPlan:
    """Deterministic direct-preload or balanced-reduction plan.

    ``rolling_reduction`` remains the serialized mode name for compatibility;
    planner v2 executes its merge DAG as balanced ordered waves.
    """

    plan_id: str
    mode: str
    planner_version: str
    contract_version: str
    output_policy: FanInOutputPolicy
    budget: FanInBudget
    reduction_budget: FanInBudget
    source_results: tuple[PersistedResult, ...]
    source_manifest: ProvenanceManifest
    source_batches: tuple[FanInBatch, ...]
    reduction_steps: tuple[ReductionStep, ...]
    final_artifact: ReductionArtifact | None
    oversize_result_ids: tuple[str, ...]
    dependency_edges: tuple[tuple[str, str], ...]
    target_worker: str
    target_consumes: tuple[str, ...]
    unresolved_consumes: tuple[str, ...]
    warnings: tuple[str, ...]
    execution_namespace_sha256: str = ""

    @property
    def requires_reduction(self) -> bool:
        return self.mode != "direct"

    @property
    def final_budget(self) -> FanInBudget:
        """The final child request budget (``budget`` is the legacy name)."""

        return self.budget

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "mode": self.mode,
            "requires_reduction": self.requires_reduction,
            "planner_version": self.planner_version,
            "contract_version": self.contract_version,
            "output_policy": self.output_policy.to_dict(),
            "budget": self.budget.to_dict(),
            "final_budget": self.final_budget.to_dict(),
            "reduction_budget": self.reduction_budget.to_dict(),
            "source_results": [item.to_dict() for item in self.source_results],
            "source_manifest": self.source_manifest.to_dict(),
            "source_batches": [batch.to_dict() for batch in self.source_batches],
            "reduction_steps": [step.to_dict() for step in self.reduction_steps],
            "final_artifact": (
                self.final_artifact.to_dict() if self.final_artifact else None
            ),
            "oversize_result_ids": list(self.oversize_result_ids),
            "dependency_edges": [list(edge) for edge in self.dependency_edges],
            "target_worker": self.target_worker,
            "target_consumes": list(self.target_consumes),
            "unresolved_consumes": list(self.unresolved_consumes),
            "warnings": list(self.warnings),
            "execution_namespace_sha256": self.execution_namespace_sha256,
        }


def derive_fan_in_budget(
    provider_config: Mapping[str, Any] | None = None,
    *,
    token_allowance: int | None = None,
    byte_allowance: int | None = None,
    base_prompt_tokens: int = 0,
    output_reserve_tokens: int = _DEFAULT_OUTPUT_RESERVE_TOKENS,
    system_tool_reserve_tokens: int = _DEFAULT_SYSTEM_TOOL_RESERVE_TOKENS,
    framing_reserve_tokens: int = _DEFAULT_MESSAGE_FRAMING_RESERVE_TOKENS,
) -> FanInBudget:
    """Derive a safe provider-aware per-request fan-in budget.

    An explicit ``token_allowance`` is interpreted as an allowance for
    persisted-result inputs.  When a provider context length is also known, the
    smaller of the explicit allowance and the context-derived allowance wins.
    A token allowance or a valid provider context is mandatory; there is no
    provider-agnostic token ceiling that is safe for every model.
    """

    config = dict(provider_config or {})
    context_length = _first_positive_int(
        config,
        "context_length",
        "max_model_len",
        "max_context_tokens",
        "context_window",
    )
    provider = str(
        config.get("provider")
        or config.get("name")
        or config.get("model")
        or "unspecified"
    )
    base_prompt_tokens = _nonnegative_int(base_prompt_tokens, "base_prompt_tokens")
    output_reserve_tokens = _nonnegative_int(
        output_reserve_tokens, "output_reserve_tokens"
    )
    system_tool_reserve_tokens = _nonnegative_int(
        system_tool_reserve_tokens, "system_tool_reserve_tokens"
    )
    framing_reserve_tokens = _nonnegative_int(
        framing_reserve_tokens, "framing_reserve_tokens"
    )

    context_allowance: int | None = None
    safety_margin = 0
    if context_length > 0:
        safety_margin = min(16_384, max(1_024, int(context_length * 0.10)))
        context_allowance = context_length - (
            base_prompt_tokens
            + output_reserve_tokens
            + system_tool_reserve_tokens
            + framing_reserve_tokens
            + safety_margin
        )

    explicit_tokens = _optional_positive_int(token_allowance, "token_allowance")
    if explicit_tokens is not None and context_allowance is not None:
        effective_tokens = min(explicit_tokens, context_allowance)
        token_source = "min(explicit,provider_context)"
    elif explicit_tokens is not None:
        effective_tokens = explicit_tokens
        token_source = "explicit"
    elif context_allowance is not None:
        effective_tokens = context_allowance
        token_source = "provider_context"
    else:
        raise ValueError(
            "fan-in planning requires token_allowance or a positive provider "
            "context length"
        )
    if effective_tokens <= 0:
        raise ValueError(
            "provider context leaves no persisted-result input allowance after "
            "the configured prompt, output, tool, framing, and safety reserves"
        )

    explicit_bytes = _optional_positive_int(byte_allowance, "byte_allowance")
    if explicit_bytes is not None:
        effective_bytes = explicit_bytes
        byte_source = "explicit"
    else:
        preload_bytes = _first_positive_int(
            config,
            "max_preload_bytes",
            "preload_byte_allowance",
            "max_prerequisite_bytes",
        )
        if preload_bytes > 0:
            effective_bytes = preload_bytes
            byte_source = "provider_config"
        else:
            effective_bytes = DEFAULT_PRELOAD_BYTE_ALLOWANCE
            byte_source = "runtime_default_512kib"

    return FanInBudget(
        provider=provider,
        context_length_tokens=context_length,
        input_token_allowance=effective_tokens,
        input_byte_allowance=effective_bytes,
        base_prompt_tokens=base_prompt_tokens,
        output_reserve_tokens=output_reserve_tokens,
        system_tool_reserve_tokens=system_tool_reserve_tokens,
        framing_reserve_tokens=framing_reserve_tokens,
        safety_margin_tokens=safety_margin,
        token_allowance_source=token_source,
        byte_allowance_source=byte_source,
    )


def _derive_reduction_budget(
    final_budget: FanInBudget,
    *,
    provider_config: Mapping[str, Any] | None,
    reduction_provider_config: Mapping[str, Any] | None,
    reduction_token_allowance: int | None,
    reduction_byte_allowance: int | None,
    base_prompt_tokens: int,
    output_reserve_tokens: int,
    system_tool_reserve_tokens: int,
    framing_reserve_tokens: int,
) -> FanInBudget:
    """Return an opt-in reducer budget while preserving legacy defaults."""

    independent = any(
        value is not None
        for value in (
            reduction_provider_config,
            reduction_token_allowance,
            reduction_byte_allowance,
        )
    )
    if not independent:
        return final_budget

    if reduction_provider_config is not None:
        config = dict(reduction_provider_config)
        has_reduction_context = _first_positive_int(
            config,
            "context_length",
            "max_model_len",
            "max_context_tokens",
            "context_window",
        ) > 0
        reducer_tokens = (
            reduction_token_allowance
            if reduction_token_allowance is not None or has_reduction_context
            else final_budget.input_token_allowance
        )
        has_reduction_bytes = _first_positive_int(
            config,
            "max_preload_bytes",
            "preload_byte_allowance",
            "max_prerequisite_bytes",
        ) > 0
        reducer_bytes = (
            reduction_byte_allowance
            if reduction_byte_allowance is not None or has_reduction_bytes
            else final_budget.input_byte_allowance
        )
    else:
        config = dict(provider_config or {})
        reducer_tokens = (
            reduction_token_allowance
            if reduction_token_allowance is not None
            else final_budget.input_token_allowance
        )
        reducer_bytes = (
            reduction_byte_allowance
            if reduction_byte_allowance is not None
            else final_budget.input_byte_allowance
        )

    return derive_fan_in_budget(
        config,
        token_allowance=reducer_tokens,
        byte_allowance=reducer_bytes,
        base_prompt_tokens=base_prompt_tokens,
        output_reserve_tokens=output_reserve_tokens,
        system_tool_reserve_tokens=system_tool_reserve_tokens,
        framing_reserve_tokens=framing_reserve_tokens,
    )


def plan_persisted_result_fan_in(
    results: Sequence[PersistedResult | Mapping[str, Any] | str | Path],
    *,
    provider_config: Mapping[str, Any] | None = None,
    token_allowance: int | None = None,
    byte_allowance: int | None = None,
    reduction_provider_config: Mapping[str, Any] | None = None,
    reduction_token_allowance: int | None = None,
    reduction_byte_allowance: int | None = None,
    base_prompt_tokens: int = 0,
    output_reserve_tokens: int = _DEFAULT_OUTPUT_RESERVE_TOKENS,
    system_tool_reserve_tokens: int = _DEFAULT_SYSTEM_TOOL_RESERVE_TOKENS,
    framing_reserve_tokens: int = _DEFAULT_MESSAGE_FRAMING_RESERVE_TOKENS,
    workspace_root: str | Path | None = None,
    source_worker_hints: Mapping[str, str] | None = None,
    consumes_hints: Mapping[str, Sequence[str]] | None = None,
    target_worker: str = "",
    target_consumes: Sequence[str] | None = None,
    execution_namespace: str | None = None,
    reduction_output_tokens: int | None = None,
    reduction_output_bytes: int | None = None,
) -> FanInPlan:
    """Plan ordered persisted-result fan-in without truncation or omission.

    Inputs remain in their supplied order.  ``source_worker``/``consumes``
    metadata is retained and compiled into dependency edges, but it never
    causes a result to be silently filtered or reordered.  Direct delivery is
    decided only against the final child budget (the legacy ``budget`` field).
    Callers may explicitly give the internal reducer a separate, usually larger
    input budget with the ``reduction_*`` arguments.  If the final child cannot
    accept the exact sources, at least one reduction remains mandatory even
    when one reducer request can accept them all.

    Multiple reduction outputs are combined as a stable adjacent balanced tree
    in deterministic waves.  This preserves source order while avoiding the
    linear latency and context churn of a rolling accumulator.

    A source that cannot fit by itself is not reported as a preload failure.
    It receives a ``stream_exact_source`` step: the executor must process the
    complete file in bounded pages, verify its checksum, and persist a bounded
    reduction before continuing.
    """

    budget = derive_fan_in_budget(
        provider_config,
        token_allowance=token_allowance,
        byte_allowance=byte_allowance,
        base_prompt_tokens=base_prompt_tokens,
        output_reserve_tokens=output_reserve_tokens,
        system_tool_reserve_tokens=system_tool_reserve_tokens,
        framing_reserve_tokens=framing_reserve_tokens,
    )
    reduction_budget = _derive_reduction_budget(
        budget,
        provider_config=provider_config,
        reduction_provider_config=reduction_provider_config,
        reduction_token_allowance=reduction_token_allowance,
        reduction_byte_allowance=reduction_byte_allowance,
        base_prompt_tokens=base_prompt_tokens,
        output_reserve_tokens=output_reserve_tokens,
        system_tool_reserve_tokens=system_tool_reserve_tokens,
        framing_reserve_tokens=framing_reserve_tokens,
    )
    normalized = _normalize_results(
        results,
        workspace_root=workspace_root,
        source_worker_hints=source_worker_hints,
        consumes_hints=consumes_hints,
    )
    if not normalized:
        raise ValueError("fan-in planning requires at least one persisted result")

    if execution_namespace is None:
        execution_namespace_value = ""
    elif not isinstance(execution_namespace, str):
        raise TypeError("execution_namespace must be a string or None")
    else:
        execution_namespace_value = execution_namespace.strip()
        if len(execution_namespace_value.encode("utf-8")) > 1_024:
            raise ValueError("execution_namespace exceeds the 1024-byte limit")
    execution_namespace_sha256 = (
        hashlib.sha256(execution_namespace_value.encode("utf-8")).hexdigest()
        if execution_namespace_value
        else ""
    )

    dependencies, unresolved = _dependency_hints(
        normalized,
        target_worker=str(target_worker or ""),
        target_consumes=tuple(_string_list(target_consumes)),
    )
    records = tuple(item.manifest_record() for item in normalized)

    # Batch identifiers are not part of item accounting, so fixed probe IDs can
    # determine topology before the content-addressed plan ID exists.
    direct_probe = _make_batch(
        normalized,
        budget,
        batch_id="direct-probe",
        stage="direct",
        ordinal=0,
    )
    requires_reduction = not direct_probe.fits_budget
    probe_batches: tuple[FanInBatch, ...] = ()
    probe_oversize: tuple[str, ...] = ()
    if requires_reduction:
        probe_batches = _partition_items(
            normalized,
            reduction_budget,
            stage="source",
            id_prefix="reduction-probe",
        )
        probe_oversize = tuple(
            batch.items[0].result_id
            for batch in probe_batches
            if not batch.fits_budget
            and isinstance(batch.items[0], PersistedResult)
        )

        placeholder_plan_id = "0" * 20
        placeholder_path = (
            f"results/.chatds/fan_in/{placeholder_plan_id}/source_manifest.json"
        )
        placeholder_body = _canonical_json(
            {"version": 1, "sources": list(records)}
        )
        placeholder_manifest = ProvenanceManifest(
            path=placeholder_path,
            records=records,
            checksum_sha256=hashlib.sha256(
                placeholder_body.encode("utf-8")
            ).hexdigest(),
        )
        output_tokens, output_bytes = _safe_reduction_output_allowance(
            reduction_budget,
            final_budget=budget,
            plan_id=placeholder_plan_id,
            manifest=placeholder_manifest,
            requested_tokens=reduction_output_tokens,
            requested_bytes=reduction_output_bytes,
            requires_pairwise_merge=(
                len(probe_batches) > 1 or bool(probe_oversize)
            ),
        )
        output_policy = FanInOutputPolicy(
            version=FAN_IN_OUTPUT_POLICY_VERSION,
            strategy="bounded_semantic_reduction",
            merge_topology="stable_ordered_balanced_binary_waves",
            max_tokens=output_tokens,
            max_bytes=output_bytes,
        )
    else:
        output_tokens = 0
        output_bytes = 0
        output_policy = FanInOutputPolicy(
            version=FAN_IN_OUTPUT_POLICY_VERSION,
            strategy="direct",
            merge_topology="none",
            max_tokens=0,
            max_bytes=0,
        )

    seed = _canonical_json(
        {
            "planner_version": FAN_IN_PLANNER_VERSION,
            "contract_version": FAN_IN_CONTRACT_VERSION,
            "mode": "rolling_reduction" if requires_reduction else "direct",
            "sources": records,
            "final_budget": budget.to_dict(),
            "reduction_budget": reduction_budget.to_dict(),
            "output_policy": output_policy.to_dict(),
            "source_partition": [
                [_item_id(item) for item in batch.items]
                for batch in probe_batches
            ],
            "oversize_result_ids": list(probe_oversize),
            "target_worker": str(target_worker or ""),
            "target_consumes": _string_list(target_consumes),
            # Bind physical artifacts to one execution without putting a raw
            # caller/run identifier into any filesystem path or manifest.
            "execution_namespace_sha256": execution_namespace_sha256,
        }
    )
    plan_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    manifest_path = f"results/.chatds/fan_in/{plan_id}/source_manifest.json"
    manifest_body = _canonical_json({"version": 1, "sources": list(records)})
    manifest = ProvenanceManifest(
        path=manifest_path,
        records=records,
        checksum_sha256=hashlib.sha256(manifest_body.encode("utf-8")).hexdigest(),
    )

    warnings: list[str] = []
    if unresolved:
        warnings.append(
            "Unresolved consumes hints were retained for audit and did not "
            "silently remove or reorder any source: " + ", ".join(unresolved)
        )

    if not requires_reduction:
        source_batches = (
            _make_batch(
                normalized,
                budget,
                batch_id=f"{plan_id}-direct-0001",
                stage="direct",
                ordinal=0,
            ),
        )
        return FanInPlan(
            plan_id=plan_id,
            mode="direct",
            planner_version=FAN_IN_PLANNER_VERSION,
            contract_version=FAN_IN_CONTRACT_VERSION,
            output_policy=output_policy,
            budget=budget,
            reduction_budget=reduction_budget,
            source_results=normalized,
            source_manifest=manifest,
            source_batches=source_batches,
            reduction_steps=(),
            final_artifact=None,
            oversize_result_ids=(),
            dependency_edges=dependencies,
            target_worker=str(target_worker or ""),
            target_consumes=tuple(_string_list(target_consumes)),
            unresolved_consumes=unresolved,
            warnings=tuple(warnings),
            execution_namespace_sha256=execution_namespace_sha256,
        )

    source_batches = _partition_items(
        normalized,
        reduction_budget,
        stage="source",
        id_prefix=f"{plan_id}-source",
    )
    oversize = tuple(
        batch.items[0].result_id
        for batch in source_batches
        if not batch.fits_budget and isinstance(batch.items[0], PersistedResult)
    )
    # Re-evaluate against the actual manifest. Plan IDs and checksums are fixed
    # width, so a mismatch indicates a future accounting-contract change and
    # must fail closed rather than silently invalidating the ID binding.
    verified_output = _safe_reduction_output_allowance(
        reduction_budget,
        final_budget=budget,
        plan_id=plan_id,
        manifest=manifest,
        requested_tokens=reduction_output_tokens,
        requested_bytes=reduction_output_bytes,
        requires_pairwise_merge=(len(source_batches) > 1 or bool(oversize)),
    )
    if verified_output != (output_tokens, output_bytes):
        raise RuntimeError(
            "internal fan-in planning error: effective output policy changed "
            "after plan ID derivation"
        )
    warnings.append(
        "Direct delivery was evaluated against the final child budget and did "
        "not fit; reduction is mandatory even if the independent reducer "
        "budget can preload every source in one request."
    )
    if oversize:
        warnings.append(
            "One or more complete source artifacts exceed a single preload "
            "budget; use stream_exact_source reduction with complete coverage "
            "and checksum verification instead of truncating or failing preload: "
            + ", ".join(oversize)
        )
    warnings.append(
        "Reduction outputs are semantic reductions, not sliced source bodies; "
        "every step must preserve the material semantics, identifiers, "
        "relationships, ordering, and uncertainty actually present in its "
        "inputs, plus the provenance-manifest reference."
    )

    steps: list[ReductionStep] = []
    leaf_outputs: list[ReductionArtifact] = []
    common_requirements = (
        "Verify every materialized input checksum before use.",
        "Process every input completely; never treat a prefix or truncated page as complete.",
        "Preserve all material input semantics and explicit identifiers; retain contradictions, uncertainty, or unavailable markers when they actually occur.",
        "Persist the output at the declared path and record its actual checksum.",
        "Carry the canonical provenance-manifest path and checksum forward unchanged.",
    )
    for index, batch in enumerate(source_batches, start=1):
        start, end = _source_range(batch.items)
        strategy = "complete_preload" if batch.fits_budget else "stream_exact_source"
        output = ReductionArtifact(
            artifact_id=f"{plan_id}-leaf-{index:04d}",
            path=(
                f"results/.chatds/fan_in/{plan_id}/"
                f"leaf_{index:04d}.md"
            ),
            estimated_tokens=output_tokens,
            max_bytes=output_bytes,
            source_start=start,
            source_end=end,
            provenance_manifest_path=manifest.path,
            provenance_manifest_checksum_sha256=manifest.checksum_sha256,
            immediate_input_ids=tuple(_item_id(item) for item in batch.items),
        )
        step = ReductionStep(
            step_id=f"{plan_id}-step-{len(steps) + 1:04d}",
            wave=1,
            ordinal=len(steps),
            strategy=strategy,
            input_batch=batch,
            output=output,
            requirements=common_requirements,
        )
        steps.append(step)
        leaf_outputs.append(output)

    current_wave = list(leaf_outputs)
    wave = 2
    while len(current_wave) > 1:
        next_wave: list[ReductionArtifact] = []
        pair_ordinal = 0
        for cursor in range(0, len(current_wave), 2):
            left = current_wave[cursor]
            if cursor + 1 >= len(current_wave):
                # Carrying an odd final node preserves its exact source range;
                # it is combined in the next deterministic wave.
                next_wave.append(left)
                continue
            right = current_wave[cursor + 1]
            pair_ordinal += 1
            merge_batch = _make_batch(
                (left, right),
                reduction_budget,
                batch_id=(
                    f"{plan_id}-merge-w{wave:04d}-{pair_ordinal:04d}"
                ),
                stage="rolling",
                ordinal=pair_ordinal - 1,
            )
            if not merge_batch.fits_budget:
                raise RuntimeError(
                    "internal fan-in planning error: bounded reduction "
                    "artifacts do not fit the balanced merge request budget"
                )
            merged = ReductionArtifact(
                artifact_id=(
                    f"{plan_id}-merge-w{wave:04d}-{pair_ordinal:04d}"
                ),
                path=(
                    f"results/.chatds/fan_in/{plan_id}/"
                    f"merge_w{wave:04d}_{pair_ordinal:04d}.md"
                ),
                estimated_tokens=output_tokens,
                max_bytes=output_bytes,
                source_start=left.source_start,
                source_end=right.source_end,
                provenance_manifest_path=manifest.path,
                provenance_manifest_checksum_sha256=manifest.checksum_sha256,
                immediate_input_ids=(left.artifact_id, right.artifact_id),
            )
            steps.append(
                ReductionStep(
                    step_id=f"{plan_id}-step-{len(steps) + 1:04d}",
                    wave=wave,
                    ordinal=len(steps),
                    strategy="rolling_reduce",
                    input_batch=merge_batch,
                    output=merged,
                    requirements=common_requirements,
                )
            )
            next_wave.append(merged)
        current_wave = next_wave
        wave += 1

    accumulator = current_wave[0]

    final_batch = _make_batch(
        (accumulator,),
        budget,
        batch_id=f"{plan_id}-final-probe",
        stage="final",
        ordinal=0,
    )
    if not final_batch.fits_budget:
        raise RuntimeError(
            "internal fan-in planning error: final reduction artifact does not "
            "fit the final child budget"
        )

    return FanInPlan(
        plan_id=plan_id,
        mode="rolling_reduction",
        planner_version=FAN_IN_PLANNER_VERSION,
        contract_version=FAN_IN_CONTRACT_VERSION,
        output_policy=output_policy,
        budget=budget,
        reduction_budget=reduction_budget,
        source_results=normalized,
        source_manifest=manifest,
        source_batches=source_batches,
        reduction_steps=tuple(steps),
        final_artifact=accumulator,
        oversize_result_ids=oversize,
        dependency_edges=dependencies,
        target_worker=str(target_worker or ""),
        target_consumes=tuple(_string_list(target_consumes)),
        unresolved_consumes=unresolved,
        warnings=tuple(warnings),
        execution_namespace_sha256=execution_namespace_sha256,
    )


def _normalize_results(
    values: Sequence[PersistedResult | Mapping[str, Any] | str | Path],
    *,
    workspace_root: str | Path | None,
    source_worker_hints: Mapping[str, str] | None,
    consumes_hints: Mapping[str, Sequence[str]] | None,
) -> tuple[PersistedResult, ...]:
    root = Path(workspace_root).resolve() if workspace_root is not None else None
    worker_hints = {str(key): str(value) for key, value in (source_worker_hints or {}).items()}
    dependency_hints = {
        str(key): tuple(_string_list(value))
        for key, value in (consumes_hints or {}).items()
    }
    normalized: list[PersistedResult] = []
    seen_ids: set[str] = set()
    for ordinal, value in enumerate(values):
        if isinstance(value, PersistedResult):
            item = value
            if item.ordinal != ordinal:
                item = PersistedResult(
                    result_id=item.result_id,
                    ordinal=ordinal,
                    path=item.path,
                    byte_size=item.byte_size,
                    token_estimate=item.token_estimate,
                    checksum_sha256=item.checksum_sha256,
                    source_worker=item.source_worker,
                    consumes=item.consumes,
                    provenance=dict(item.provenance),
                    content=item.content,
                )
        else:
            mapping = dict(value) if isinstance(value, Mapping) else {"path": str(value)}
            raw_path = str(mapping.get("result_path") or mapping.get("path") or "").strip()
            result_id = str(mapping.get("result_id") or mapping.get("id") or f"result-{ordinal + 1:04d}")
            if not result_id:
                raise ValueError(f"result at ordinal {ordinal} has an empty result_id")
            content = mapping.get("content")
            declared_size = _mapping_optional_int(mapping, "byte_size", "bytes", "size")
            declared_tokens = _mapping_optional_int(
                mapping, "token_estimate", "estimated_tokens", "content_tokens"
            )
            declared_checksum = _mapping_string(
                mapping, "checksum_sha256", "sha256", "checksum"
            )
            if declared_checksum.startswith("sha256:"):
                declared_checksum = declared_checksum[7:]

            if content is not None:
                if not isinstance(content, (str, bytes)):
                    raise ValueError(
                        f"result at ordinal {ordinal} content must be str or bytes"
                    )
                body = content.encode("utf-8") if isinstance(content, str) else content
                byte_size = len(body)
                checksum = hashlib.sha256(body).hexdigest()
                token_estimate = _token_estimate_for_bytes(body)
                _verify_declared_metadata(
                    ordinal,
                    declared_size,
                    declared_tokens,
                    declared_checksum,
                    byte_size,
                    token_estimate,
                    checksum,
                )
            elif raw_path:
                resolved = _resolve_result_path(raw_path, root)
                if resolved.is_file():
                    byte_size, token_estimate, checksum = _inspect_text_file(resolved)
                    _verify_declared_metadata(
                        ordinal,
                        declared_size,
                        declared_tokens,
                        declared_checksum,
                        byte_size,
                        token_estimate,
                        checksum,
                    )
                elif (
                    declared_size is not None
                    and declared_tokens is not None
                    and declared_checksum
                ):
                    byte_size = declared_size
                    token_estimate = declared_tokens
                    checksum = declared_checksum.casefold()
                else:
                    raise ValueError(
                        f"persisted result path is not readable and complete metadata "
                        f"was not supplied: {raw_path}"
                    )
            else:
                raise ValueError(
                    f"result at ordinal {ordinal} requires path/result_path or content"
                )

            _validate_checksum(checksum, ordinal)
            hint_keys = (result_id, raw_path, Path(raw_path).name if raw_path else "")
            source_worker = str(mapping.get("source_worker") or "").strip()
            if not source_worker:
                source_worker = next(
                    (worker_hints[key] for key in hint_keys if key and key in worker_hints),
                    "",
                )
            consumes = tuple(_string_list(mapping.get("consumes")))
            if not consumes:
                consumes = next(
                    (dependency_hints[key] for key in hint_keys if key and key in dependency_hints),
                    (),
                )
            provenance = mapping.get("provenance")
            if not isinstance(provenance, Mapping):
                provenance = {}
            item = PersistedResult(
                result_id=result_id,
                ordinal=ordinal,
                path=raw_path or f"inline://{result_id}",
                byte_size=byte_size,
                token_estimate=token_estimate,
                checksum_sha256=checksum,
                source_worker=source_worker,
                consumes=consumes,
                provenance=dict(provenance),
                content=content,
            )

        if item.result_id in seen_ids:
            raise ValueError(f"duplicate persisted result_id: {item.result_id}")
        seen_ids.add(item.result_id)
        if item.byte_size < 0 or item.token_estimate < 0:
            raise ValueError(f"negative persisted result size: {item.result_id}")
        _validate_checksum(item.checksum_sha256, ordinal)
        normalized.append(item)
    return tuple(normalized)


def _resolve_result_path(raw_path: str, root: Path | None) -> Path:
    path = Path(raw_path)
    unresolved = root / path if root is not None and not path.is_absolute() else path
    if unresolved.is_symlink():
        raise ValueError(f"persisted result symlink is not accepted: {raw_path}")
    candidate = unresolved.resolve()
    if root is not None and candidate != root and root not in candidate.parents:
        raise ValueError(f"persisted result path escapes workspace_root: {raw_path}")
    return candidate


def _inspect_text_file(path: Path) -> tuple[int, int, str]:
    hasher = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    byte_size = 0
    ascii_chars = 0
    non_ascii_chars = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            byte_size += len(chunk)
            hasher.update(chunk)
            text = decoder.decode(chunk, final=False)
            ascii_chunk = sum(1 for char in text if ord(char) < 128)
            ascii_chars += ascii_chunk
            non_ascii_chars += len(text) - ascii_chunk
    tail = decoder.decode(b"", final=True)
    ascii_tail = sum(1 for char in tail if ord(char) < 128)
    ascii_chars += ascii_tail
    non_ascii_chars += len(tail) - ascii_tail
    token_estimate = (ascii_chars + 3) // 4 + non_ascii_chars
    return byte_size, token_estimate, hasher.hexdigest()


def _token_estimate_for_bytes(body: bytes) -> int:
    return estimate_mixed_text_tokens(body.decode("utf-8", errors="replace"))


def _verify_declared_metadata(
    ordinal: int,
    declared_size: int | None,
    declared_tokens: int | None,
    declared_checksum: str,
    actual_size: int,
    actual_tokens: int,
    actual_checksum: str,
) -> None:
    if declared_size is not None and declared_size != actual_size:
        raise ValueError(
            f"persisted result {ordinal} byte_size mismatch: declared "
            f"{declared_size}, actual {actual_size}"
        )
    if declared_tokens is not None and declared_tokens != actual_tokens:
        raise ValueError(
            f"persisted result {ordinal} token_estimate mismatch: declared "
            f"{declared_tokens}, actual {actual_tokens}"
        )
    if declared_checksum and declared_checksum.casefold() != actual_checksum:
        raise ValueError(f"persisted result {ordinal} checksum mismatch")


def _validate_checksum(value: str, ordinal: int) -> None:
    checksum = str(value or "")
    if len(checksum) != 64 or any(char not in "0123456789abcdefABCDEF" for char in checksum):
        raise ValueError(f"persisted result {ordinal} requires a valid SHA-256 checksum")


def _dependency_hints(
    items: tuple[PersistedResult, ...],
    *,
    target_worker: str,
    target_consumes: tuple[str, ...],
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    aliases: dict[str, list[str]] = {}
    for item in items:
        for alias in (
            item.result_id,
            item.path,
            Path(item.path).name,
            item.source_worker,
        ):
            if alias:
                aliases.setdefault(alias, []).append(item.result_id)

    edges: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for consumer in items:
        for dependency in consumer.consumes:
            matches = aliases.get(dependency, [])
            if len(matches) == 1:
                edge = (matches[0], consumer.result_id)
                if edge not in edges:
                    edges.append(edge)
            else:
                unresolved.append(f"{consumer.result_id}:{dependency}")
    target_id = target_worker or "__target__"
    for dependency in target_consumes:
        matches = aliases.get(dependency, [])
        if len(matches) == 1:
            edge = (matches[0], target_id)
            if edge not in edges:
                edges.append(edge)
        else:
            unresolved.append(f"{target_id}:{dependency}")
    return tuple(edges), tuple(dict.fromkeys(unresolved))


def _partition_items(
    items: Sequence[FanInItem],
    budget: FanInBudget,
    *,
    stage: str,
    id_prefix: str,
) -> tuple[FanInBatch, ...]:
    batches: list[FanInBatch] = []
    current: list[FanInItem] = []
    for item in items:
        singleton = _make_batch(
            (item,),
            budget,
            batch_id=f"{id_prefix}-{len(batches) + 1:04d}",
            stage=stage,
            ordinal=len(batches),
        )
        if not singleton.fits_budget:
            if current:
                batches.append(
                    _make_batch(
                        tuple(current),
                        budget,
                        batch_id=f"{id_prefix}-{len(batches) + 1:04d}",
                        stage=stage,
                        ordinal=len(batches),
                    )
                )
                current = []
            batches.append(
                _make_batch(
                    (item,),
                    budget,
                    batch_id=f"{id_prefix}-{len(batches) + 1:04d}",
                    stage=stage,
                    ordinal=len(batches),
                )
            )
            continue
        candidate = _make_batch(
            tuple(current + [item]),
            budget,
            batch_id=f"{id_prefix}-{len(batches) + 1:04d}",
            stage=stage,
            ordinal=len(batches),
        )
        if current and not candidate.fits_budget:
            batches.append(
                _make_batch(
                    tuple(current),
                    budget,
                    batch_id=f"{id_prefix}-{len(batches) + 1:04d}",
                    stage=stage,
                    ordinal=len(batches),
                )
            )
            current = [item]
        else:
            current.append(item)
    if current:
        batches.append(
            _make_batch(
                tuple(current),
                budget,
                batch_id=f"{id_prefix}-{len(batches) + 1:04d}",
                stage=stage,
                ordinal=len(batches),
            )
        )
    return tuple(batches)


def _make_batch(
    items: tuple[FanInItem, ...],
    budget: FanInBudget,
    *,
    batch_id: str,
    stage: str,
    ordinal: int,
) -> FanInBatch:
    tokens = _BATCH_FIXED_TOKENS
    byte_count = _BATCH_FIXED_BYTES
    for item in items:
        item_tokens, item_bytes = _item_cost(item)
        tokens += item_tokens
        byte_count += item_bytes
    return FanInBatch(
        batch_id=batch_id,
        stage=stage,
        ordinal=ordinal,
        items=items,
        estimated_tokens=tokens,
        estimated_bytes=byte_count,
        fits_budget=(
            tokens <= budget.input_token_allowance
            and byte_count <= budget.input_byte_allowance
        ),
    )


def _item_cost(item: FanInItem) -> tuple[int, int]:
    if isinstance(item, PersistedResult):
        metadata = item.manifest_record()
        body_tokens = item.token_estimate
        body_bytes = item.byte_size
    else:
        metadata = item.to_dict()
        body_tokens = item.estimated_tokens
        body_bytes = item.max_bytes
    serialized = _canonical_json(metadata)
    return (
        body_tokens + estimate_mixed_text_tokens(serialized) + _ITEM_FRAMING_TOKENS,
        body_bytes + len(serialized.encode("utf-8")) + _ITEM_FRAMING_BYTES,
    )


def _safe_reduction_output_allowance(
    reduction_budget: FanInBudget,
    *,
    final_budget: FanInBudget,
    plan_id: str,
    manifest: ProvenanceManifest,
    requested_tokens: int | None,
    requested_bytes: int | None,
    requires_pairwise_merge: bool,
) -> tuple[int, int]:
    dummy = ReductionArtifact(
        artifact_id=f"{plan_id}-reduction-0000",
        path=f"results/.chatds/fan_in/{plan_id}/rolling_0000.md",
        estimated_tokens=0,
        max_bytes=0,
        source_start=0,
        source_end=1,
        provenance_manifest_path=manifest.path,
        provenance_manifest_checksum_sha256=manifest.checksum_sha256,
        immediate_input_ids=(f"{plan_id}-input-0000", f"{plan_id}-input-0001"),
    )
    metadata_tokens, metadata_bytes = _item_cost(dummy)
    # The final child must be able to receive one complete reduction artifact.
    # This is a distinct constraint from the internal reducer's input budget.
    final_token_cap = (
        final_budget.input_token_allowance
        - _BATCH_FIXED_TOKENS
        - metadata_tokens
    )
    final_byte_cap = (
        final_budget.input_byte_allowance
        - _BATCH_FIXED_BYTES
        - metadata_bytes
    )
    token_cap = final_token_cap
    byte_cap = final_byte_cap

    if requires_pairwise_merge:
        # Two reduction artifacts must fit an internal merge. Divide the
        # remaining capacity by three rather than two to retain headroom for
        # longer materialized identifiers and concrete prompt metadata.
        merge_token_cap = (
            reduction_budget.input_token_allowance
            - _BATCH_FIXED_TOKENS
            - 2 * metadata_tokens
        ) // 3
        merge_byte_cap = (
            reduction_budget.input_byte_allowance
            - _BATCH_FIXED_BYTES
            - 2 * metadata_bytes
        ) // 3
        token_cap = min(token_cap, merge_token_cap)
        byte_cap = min(byte_cap, merge_byte_cap)
    if token_cap <= 0 or byte_cap <= 0:
        raise ValueError(
            "fan-in allowances are too small for a manifest-addressed final "
            "reduction artifact and the required internal merge topology"
        )
    explicit_tokens = _optional_positive_int(
        requested_tokens, "reduction_output_tokens"
    )
    explicit_bytes = _optional_positive_int(
        requested_bytes, "reduction_output_bytes"
    )
    output_tokens = min(explicit_tokens or token_cap, token_cap)
    output_bytes = min(explicit_bytes or output_tokens * 4, byte_cap)
    return output_tokens, output_bytes


def _source_range(items: Sequence[FanInItem]) -> tuple[int, int]:
    starts: list[int] = []
    ends: list[int] = []
    for item in items:
        if isinstance(item, PersistedResult):
            starts.append(item.ordinal)
            ends.append(item.ordinal + 1)
        else:
            starts.append(item.source_start)
            ends.append(item.source_end)
    return min(starts), max(ends)


def _item_id(item: FanInItem) -> str:
    return item.result_id if isinstance(item, PersistedResult) else item.artifact_id


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence):
        values = value
    else:
        values = [value]
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _mapping_optional_int(mapping: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if mapping.get(key) is not None:
            return _optional_nonnegative_int(mapping.get(key), key)
    return None


def _mapping_string(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        if mapping.get(key) is not None:
            return str(mapping.get(key) or "").strip()
    return ""


def _first_positive_int(mapping: Mapping[str, Any], *keys: str) -> int:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _optional_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _nonnegative_int(value: Any, name: str) -> int:
    return _optional_nonnegative_int(value, name)


__all__ = [
    "DEFAULT_PRELOAD_BYTE_ALLOWANCE",
    "FAN_IN_CONTRACT_VERSION",
    "FAN_IN_OUTPUT_POLICY_VERSION",
    "FAN_IN_PLANNER_VERSION",
    "FanInBatch",
    "FanInBudget",
    "FanInOutputPolicy",
    "FanInPlan",
    "PersistedResult",
    "ProvenanceManifest",
    "ReductionArtifact",
    "ReductionStep",
    "derive_fan_in_budget",
    "estimate_mixed_text_tokens",
    "plan_persisted_result_fan_in",
]
