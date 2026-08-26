# Third-party material and reference repositories

The root `LICENSE` applies only to original ChatDS contributions for which the
ChatDS contributors can grant a license. It does not relicense third-party
software, documentation, generated material, datasets, model outputs, uploaded
Skill packages, or reference repositories. Those materials remain subject to
their own notices and applicable rights.

Known repository-local boundaries include:

- `deepseek-harness/` and `deepseek-harness-clean/` are read-only vendored
  snapshots of `https://github.com/deepseek-ai/deepseek-harness.git` at commit
  `47f943859bef60e4160492346772ded9b24f765a` and tree
  `f904efab9ef435201d6ba4da88a34d6366568272`. The `-clean` copy is a production
  build dependency of the optional DeepSeek Harness engine. Both remain
  licensed under their own MIT `LICENSE`; ChatDS adapter code lives in
  `deepseek_runner/` and does not modify either pinned upstream snapshot.
- `openclaw/` is third-party software and retains the MIT license in
  `openclaw/LICENSE`. It is historical reference material and is not a current
  Harness design reference.
- `claude-code-analysis-main/` is a third-party static-analysis/reference tree.
  No license grant is inferred from the absence of a repository-local license.
  It is not a current Harness design reference.
- `searxng-master/` contains third-party SearXNG material; its upstream license
  remains controlling. ChatDS-specific integration files are licensed only to
  the extent their ChatDS authors have rights to those contributions.
- `claude-code/` is a read-only vendored reference snapshot imported from
  commit `6f6f12b37f529488b10e53928dd5508bb93535c7` and tree
  `ef7589945b3767ead85fc52f68d013f88094bd47`. No root-level license grant is
  inferred for that material, and it is not a production build dependency.
- `hermes-agent/` is a read-only vendored reference snapshot imported from
  commit `6c73e8ffaa7b8df1e7b2f9d5792b4ee027e41637` and tree
  `29759e962655e2ba1d8bd5b70ac1d356a22070c0`. It remains licensed under its
  own MIT `LICENSE` and is not the current Harness design reference.
- User/session runtime data under `data/`, `workspace/`, `skills_and_refs/`,
  and related generated directories is not relicensed by the root `LICENSE`.

For future Harness work, the vendored `claude-code/` snapshot at the recorded
commit and tree is the sole mature-Harness implementation reference requested
by the project owner. Reference use does not make it a build dependency and
does not change its own license status.
