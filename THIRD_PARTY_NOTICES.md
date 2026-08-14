# Third-party material and reference repositories

The root `LICENSE` applies only to original ChatDS contributions for which the
ChatDS contributors can grant a license. It does not relicense third-party
software, documentation, generated material, datasets, model outputs, uploaded
Skill packages, or reference repositories. Those materials remain subject to
their own notices and applicable rights.

Known repository-local boundaries include:

- `deepseek-harness-clean/` is the pinned
  `https://github.com/deepseek-ai/deepseek-harness.git` Git submodule and a
  production build dependency of the optional DeepSeek Harness engine. It
  remains licensed under its own MIT `LICENSE`; ChatDS adapter code lives in
  `deepseek_runner/` and does not modify the pinned upstream source tree.
- `openclaw/` is third-party software and retains the MIT license in
  `openclaw/LICENSE`. It is historical reference material and is not a current
  Harness design reference.
- `claude-code-analysis-main/` is a third-party static-analysis/reference tree.
  No license grant is inferred from the absence of a repository-local license.
  It is not a current Harness design reference.
- `searxng-master/` contains third-party SearXNG material; its upstream license
  remains controlling. ChatDS-specific integration files are licensed only to
  the extent their ChatDS authors have rights to those contributions.
- `claude-code/` and `hermes-agent/` are independent, untracked nested Git
  repositories and are not distributed as part of the ChatDS Git tree. Their
  contents are not covered by the root `LICENSE`.
- User/session runtime data under `data/`, `workspace/`, `skills_and_refs/`,
  and related generated directories is not relicensed by the root `LICENSE`.

For future Harness work, the local nested repository `claude-code/` at its
recorded commit is the sole mature-Harness implementation reference requested
by the project owner. Reference use does not make it a build dependency and
does not change its own license status.
