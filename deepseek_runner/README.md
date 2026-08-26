# DeepSeek Harness engine adapter

This directory adapts the pinned upstream DeepSeek Harness runtime into the
same ChatDS `AgentEngine` boundary used by the other engines. It does not fork
or patch the upstream agent loop.

## Runtime boundary

- `deepseek-harness-clean/` is a read-only vendored snapshot pinned to upstream
  commit `47f943859bef60e4160492346772ded9b24f765a` (`0.1.0-rc.5`) with tree
  `f904efab9ef435201d6ba4da88a34d6366568272`. A normal clone contains it;
  no submodule initialization is required.
- `Dockerfile.runner` follows the upstream source-build sequence (`pnpm
  install`, `pnpm run build`) and runs the built `apps/cli/lib/bin.js` entry.
- The trusted supervisor owns Docker lifecycle and durable terminal state. A
  Turn container has `network_mode:none` and receives exactly one
  user/Session workspace at `/workspace`; no repository root, another user,
  another Session, or Docker socket is mounted.
- The model worker cannot write the controller event ledger. It writes native
  Session events to a bounded tmpfs spool; PID 1 validates and forwards those
  events into the root-owned durable ledger.
- Each Turn receives an immutable Skill view. Standard Skill-declared MCP
  servers are compiled into the upstream MCP client plugin as data. ChatDS
  controller-owned capabilities are not impersonated as ordinary MCP servers.
- Workspace permission presets are `read_only`, `workspace_write`, and
  `session_full`. Even full Session permission remains inside the container's
  exact mounts and signed egress boundary.

## Search and network

DeepSeek Harness's native Web capability is bound to the
`chatds-searxng-search` provider. It calls the exact deployment-owned
`http://searxng:8080/search` JSON endpoint using GET, `safesearch=1`, and no
redirects. The Turn remains networkless: Node uses a loopback bridge to the
shared signed egress proxy, whose per-Turn policy grants only the selected
OpenAI `/chat/completions` endpoint, the SearXNG `/search` endpoint when Web is
enabled, Skill/MCP-declared endpoints, and the configured bounded public-read
profile.

The `deepseek-harness` Compose profile also activates SearXNG and Valkey, and
the supervisor waits for SearXNG health before accepting work.

## Deployment

Required deployment values belong in the permission-restricted `.env`; never
commit credentials. The important non-secret switches are:

```text
DEEPSEEK_HARNESS_ENGINE_ENABLED=true
DEEPSEEK_HARNESS_RUNNER_MAX_RUN_SECONDS=14400
DEEPSEEK_HARNESS_PUBLIC_READ_EGRESS_ENABLED=true
```

Build and start the optional engine with:

```sh
BUILDX_BUILDER=default docker compose --profile deepseek-harness build \
  deepseek-harness-runner-image deepseek-runner-supervisor
docker compose --profile deepseek-harness up -d \
  deepseek-runner-supervisor backend frontend
```

The Backend exposes available engines through `/api/chat/engines`. A
Conversation persists one `engine_id`; after it has messages, changing engines
requires a normal Session fork. Model choices are filtered by explicit
deployment-owned compatibility bindings.

## Verification

The generic contract tests live in
`backend/tests/test_deepseek_harness_contract.py`. They cover root/child event
authority, exact Session binding, canonical tool grants, untrusted event-spool
validation, and cross-domain/renamed MCP compilation. Frontend engine/model
selection mutations are covered by `frontend/src/utils/engineSelection.test.js`.
The container build verifies the pinned upstream version and strips set-id
files and file capabilities from the final runtime image.
