# ChatDS agent continuation

Before making changes, read `SESSION_HANDOFF.md` completely. It is the canonical current state for this repository; older `_SESSION_*.md`, `_HARNESS_*.md`, and `_REMOTE_OPS.md` files are historical.

Key operating constraints:

- Preserve the dirty worktree and never use `git add -A`.
- Do not restore or stage the two user-owned tracked deletions documented in `SESSION_HANDOFF.md`.
- Never print or persist credentials; use the permission-restricted `.local_secrets` files.
- Keep Skill execution generic. V2.3 is a test case, not a target for disease-, filename-, or route-specific Harness logic.
- Do not automatically launch a model-heavy V2.3 E2E unless the user asks; the next expected test is user-driven.
- Git commits are local-only unless the user explicitly changes that instruction.
