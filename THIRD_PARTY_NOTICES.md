# Third-party software and excluded material

The root `LICENSE` applies only to original ChatDS contributions for which the
ChatDS contributors can grant a license. It does not relicense third-party
software, documentation, generated material, datasets, model outputs, uploaded
Skill packages, or reference repositories. Those materials remain subject to
their own notices and applicable rights.

The public ChatDS repository does not distribute the historical reference
repositories, production/runtime data, uploaded Skills, or generated business
artifacts that may have existed in private development workspaces.

Important runtime/build boundaries include:

- The optional Claude Turn image downloads a pinned
  `@anthropic-ai/claude-code` package and its platform package during the image
  build. Those packages remain subject to Anthropic's terms and are not
  relicensed by ChatDS.
- The `local-search` profile runs the pinned official SearXNG and Valkey
  container images. Their upstream licenses and notices remain controlling.
- Python, npm, browser, base-image, and system dependencies listed in component
  manifests or Dockerfiles retain their own licenses.
- Model weights and external model services are not distributed by this
  repository and remain subject to their respective licenses and provider
  terms.
- User/session runtime data under `data/` and `workspace/`, uploaded Skill
  archives, MCP servers, reference datasets, and generated artifacts are not
  covered by the root license merely because ChatDS processes them.

Independent local reference repositories used during private development are
not ChatDS build dependencies and are not part of this Git tree. Reference use
does not change their own license status.
