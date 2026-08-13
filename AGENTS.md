# ChatDS agent continuation

Before making changes, read `SESSION_HANDOFF.md` completely. It is the canonical current state for this repository; older `_SESSION_*.md`, `_HARNESS_*.md`, and `_REMOTE_OPS.md` files are historical.

Key operating constraints:

- Preserve the dirty worktree and never use `git add -A`.
- Do not restore or stage the two user-owned tracked deletions documented in `SESSION_HANDOFF.md`.
- Never print or persist credentials; use the permission-restricted `.local_secrets` files.
- Keep Skill execution generic. V2.3 is a test case, not a target for disease-, filename-, or route-specific Harness logic.
- Treat V2.3 only as a complex stress test and business-level acceptance oracle. Any defect it exposes must first be restated as a domain-independent compiler, workflow, capability, sandbox, evidence, artifact, recovery, or lifecycle invariant before production code is changed.
- A V2.3-discovered production fix must not branch on Skill/package/session IDs, domain terms, route/worker/KG IDs, fixed worker/file counts, report names, or other fixture literals. Values declared by the Skill may be compiled and enforced as data; they must not be embedded as Harness policy.
- Every such fix must add a synthetic generic regression and, where applicable, at least one non-V2.3 cross-domain holdout or mutation/rename test. V2.3 E2E is an acceptance test, never the sole regression proving generality.
- Do not automatically launch a model-heavy V2.3 E2E unless the user asks; the next expected test is user-driven.
- Git commits are local-only unless the user explicitly changes that instruction.
- Treat `ClaudeCodeEngine` as a thin adapter around the unmodified, pinned
  Claude Code CLI, not as another ChatDS-owned Harness. Never patch, rebuild,
  fork, or replace the Claude Code binary/core. The adapter may own only the
  Web/user/Session boundary, one-Workspace mount and isolation, provider/model
  protocol binding, attachment lowering, Skill/plugin/MCP projection, SSE
  projection, persistence, cancellation/cleanup, and deployment-owned security
  policy. Planning, tool loops, sub-agents, compaction, provider retries, and
  native session behavior remain Claude Code-owned. Do not add a parallel
  agent loop, retry/compaction state machine, control prompt, or model/Skill/
  Session-specific workaround to ClaudeCodeEngine. When a third-party model or
  compatibility facade disagrees with native Claude Code, fix only the protocol
  adapter if possible; otherwise report the compatibility boundary explicitly.
- Every session diagnosis must correlate all three evidence sources before
  proposing a fix: the session debug/AgentRun logs, the persisted conversation
  context, and the exact Skill package/instructions/resources enabled for that
  session. Compare them explicitly, classify systemic causes across Harness,
  Skill, provider/model, network/policy, and upstream availability, then fix
  only generic root causes. A frontend error string or one evidence source is
  never sufficient by itself.
- For an explicitly requested iterative E2E campaign, treat each round as an
  independent run and close the same evidence loop before counting it: freeze
  the exact conversation and Skill snapshot, reconstruct the AgentRun/tool/
  provider/artifact timeline, compare the declared workflow and deliverable
  contract with the durable result, and record the terminal reason. Replaying
  or reinterpreting one run does not count as another round.
- After every E2E terminal, automatically apply the user's established repair
  question chain without waiting for the user to repeat it: determine current
  status and exact failure point; correlate conversation, immutable Skill and
  debug/AgentRun/tool evidence; explain every delegated attempt and artifact
  stage; define root cause and observable invariants; create deterministic
  reproductions; compare mature harness patterns; then implement, test, commit
  and deploy only a generic cross-Skill correction.
- Before changing production code for an E2E defect, restate it as a cross-domain
  invariant, create a deterministic failure-injection or scripted-provider
  reproduction, and add a non-V2.3 holdout or mutation/rename case when
  applicable. Machine-owned receipts and durable state are authoritative for
  control-plane facts; model prose is content, not workflow state.
- An E2E defect iteration counts as a repair iteration only when the resulting
  production change improves execution for arbitrary conforming Skills through
  a generic compiler, workflow, capability, sandbox, evidence, artifact,
  recovery, or lifecycle invariant. A passing acceptance round may be recorded
  without manufacturing a code change; a fixture-specific workaround never
  counts as an iteration.
- Every E2E repair iteration must include a current comparison with mature
  session-wise Harness/workflow implementation. The sole current implementation
  reference is the local, independent nested Git repository
  `/nfs/yangbb/codes/chat_ds/claude-code/`; freeze and record its exact commit
  for each comparison. Treat that source as the primary design evidence. Web
  search is allowed only when a relevant local code path is stubbed, broken,
  or genuinely ambiguous; record the exact uncertainty and distinguish local
  source evidence from the minimal Web corroboration. Do not resume routine
  OpenClaw/Hermes/framework surveys or use another Harness as a substitute for
  the local source. Map the observed mechanism to
  concrete code paths and patterns such as durable checkpoints/pending writes,
  typed state and structured output, idempotent activity retries, subgraph
  failure isolation, sandbox/workspace boundaries, and trace/terminal
  semantics. State whether to adopt, adapt behind the existing
  authority/receipt contracts, or reject each relevant pattern. A stub is an
  unknown boundary, not evidence of an implementation; do not infer or invent
  the missing private behavior. Listing names without a problem-to-code-path-
  to-decision mapping is not sufficient.
- The preferred phase order is monotonic: compile/bind -> decide conditional
  authority -> satisfy mandatory receipts -> optional retrieval -> synthesize ->
  fan-in -> validate artifacts -> persist exactly one authoritative terminal.
  Bounded recovery must preserve the current mandatory frontier and must not
  silently advance to a later phase.
