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
- Every session diagnosis must correlate all three evidence sources before
  proposing a fix: the session debug/AgentRun logs, the persisted conversation
  context, and the exact Skill package/instructions/resources enabled for that
  session. Compare them explicitly, classify systemic causes across Harness,
  Skill, provider/model, network/policy, and upstream availability, then fix
  only generic root causes. A frontend error string or one evidence source is
  never sufficient by itself.
