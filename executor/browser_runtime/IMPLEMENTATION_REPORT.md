# Unified session-sandbox implementation report

> Current production state as of 2026-07-30 is defined by `c62a4a69`,
> `304781c8`, and the Backend safety amendment `b4e8dc18`. The detailed body
> below records the earlier independent-browser
> implementation and remains useful as dependency/seccomp/Weston provenance,
> but its two-lane topology is historical and must not be used as current
> deployment guidance. `SESSION_HANDOFF.md` is the canonical operational state.

## Current unified scope

Production now runs four homogeneous `session-sandbox-v1` slots. Every slot
contains the immutable Bash/Python/Node, Playwright, Selenium, Chromium,
ChromeDriver and Weston closure described below. Harness reserves a healthy
slot from one content-bound UDS pool; it no longer asks the model or Skill to
choose between a base executor and a browser executor. A persistent lease keeps
slot affinity until close, while admission, abandon, quarantine, restart and
startup reap are runtime-owned.

Each slot has `network_mode:none`, a fixed UID/GID 65529 worker, a private
run/lease snapshot, HOME, TMP, workspace and process tree, and a 3 GiB cgroup
memory boundary. The fixed pool is not one container per chat. Runtime package
installation remains disabled; missing dependencies fail during capability
preflight.

The only networked Skill-code component is `skill-egress-proxy`. Public access
requires a frozen, execution-signed exact origin/method/path-prefix rule.
Private access additionally requires the current user URL authorization and
the deployment private-origin/CIDR allowlist. DNS answers remain pinned and
validated; loopback, metadata and off-policy addresses are rejected. Private
CA/key material exists only in the proxy-private volume and is never mounted
into an executor.

Backend and Harness share a separate host-local Docker volume only for
workspace mutation locks. `304781c8` moved those locks off the NFS workspace
after a real NFSv3 NLM hang proved that `flock(LOCK_NB)` does not bound lockd
RPC waits. Executors, proxy, browser, frontend and search cannot see this lock
volume. Missing/unsafe mounts fail closed.

`b4e8dc18` does not change the unified slot or egress topology. It hardens the
Backend reconciler around that lifecycle boundary: DB absence alone retains an
unfenced tree, pending journals retain/defer, and only an already present,
strictly validated durable deletion tombstone authorizes cleanup. Marker
metadata, bounded exact payload, inode/path stability and the destructive
boundary are revalidated fail closed. Production currently preserves the
single-Backend/no-overlapping-rollout invariant; full validation and rollback
evidence remain in `SESSION_HANDOFF.md`.

Current real-image acceptance covers four-slot Bash/Python/Node execution,
CommonJS/ESM and Python Playwright, Selenium, persistent object calls,
12,589,062-byte artifact transfer, signed public/private egress and direct,
loopback, private and metadata deny paths. The exact current test/image evidence
is recorded in `SESSION_HANDOFF.md`.

Current limitations are deliberate: the pool is single-host rather than
per-session containers; dependencies cannot be installed at runtime; private
egress is whitelist- and run-policy-scoped; CAPTCHA, stealth/anti-evasion and
unconfirmed important actions are unsupported; and the local lock volume is
not a multi-host active-active coordination service.

## Historical delivered scope (pre-c62)

This change adds an independent `browser-automation-v1` worker image and
integrates it with the executor process protocol and Compose topology. Harness
receives only the authenticated controller socket.

The profile provides an immutable dependency closure for:

- exact Node `.cjs`, `.js`, and `.mjs` Skill scripts;
- exact Python `.py` and Bash `.sh`/`.bash` Skill scripts;
- Node and Python Playwright;
- Selenium with a Debian Chromium/ChromeDriver pair;
- the complete shared base Skill Python dependency manifest;
- headed operation through a per-process Weston headless compositor.

Builder images are digest-pinned, npm uses a version/integrity lock, Python
uses a complete hash-checked lock, and Debian packages come from one immutable
snapshot. The build records the resolved Debian versions and verifies that the
browser and driver majors match.

The reviewed refresh uses Playwright 1.61.0 for both language bindings,
Selenium 4.46.0, and the 2026-07-20 Debian bookworm/bookworm-security
snapshot. The snapshot was the
newest available reviewed timestamp more than 48 hours old; Playwright 1.61.0
was the current stable release. The exact Chromium and ChromeDriver patch
versions are emitted into `installed-manifest.json` and recorded below after
the real image build.

The 2026-07-23 amd64 build resolved both Chromium and ChromeDriver to
`150.0.7871.124-1~deb12u1`. This pair came from the same reviewed Debian
security snapshot, has an exact major/patch match, and passed the real Node
CommonJS/ESM Playwright and Selenium smokes described below.

The standalone worker is UID/GID 65529. It is deliberately distinct from the
legacy browser's UID/GID 65532 because Linux accounts `RLIMIT_NPROC` by host
UID even across container PID namespaces. In the production executor target, a
root lifecycle controller owns the authenticated executor UDS and the fixed
proxy bridge. Each exact Skill process starts its own short-lived Weston
headless compositor after the UID/GID drop. Its Wayland socket, lock, and log
exist only in that process's lease-private `XDG_RUNTIME_DIR`, so lease code
never connects to a root-owned display server or leaves display state in a
shared writable root. The
controller uses root-owned `/usr/bin/prlimit` plus native
UID/GID credentials to run the exact Skill entrypoint as 65529:65529 with no
supplementary groups. npm, npx, pip, ensurepip, apt, and apt-get remain
unavailable in the final image.

## Historical egress baseline

The launcher requires the runtime-owned `SKILL_EGRESS_PROXY_URL`, exports it
through the standard proxy variables, and selects the controlled Chromium
wrapper. The wrapper:

- rejects Skill/model proxy and bypass overrides;
- rejects host-resolver overrides;
- rejects caller-selected TCP remote-debug listeners;
- rejects anti-detection Blink feature flags;
- disables QUIC and non-proxied WebRTC UDP;
- removes implicit Chromium loopback proxy bypass.

ChromeDriver receives one narrow compatibility exception: its own
`--remote-debugging-port=0` is accepted only when the baked-in ChromeDriver is
the wrapper's direct parent. That endpoint is ephemeral inside one worker and
must never be published or shared.

No shared CDP socket is part of this profile.

## Historical production topology (superseded)

Compose includes:

- networkless one-shot initializers for the controller and proxy UDS volumes;
- a read-only, capability-free UID 65531 policy proxy attached only to
  `browser_egress`;
- one read-only root controller with `network_mode: none`;
- a fixed controller-owned `127.0.0.1:18080` byte bridge to the proxy's
  read-only `/run/chatds-skill-egress/proxy.sock` mount;
- a proxy-owned setgid directory and dedicated GID 65530 which the Skill child
  loses before exec;
- a read-only Harness mount of only the authenticated controller UDS.

The worker has only loopback: no Docker DNS, Ethernet device, gateway, default
route, published port, application-network attachment, Docker socket, proxy
UDS authority, or CDP volume. The proxy is the only Skill-browser component
with an egress network.

The production Skill proxy is public-only: Compose does not project global
private-origin or private-CIDR exceptions into this lane. Such global settings
cannot express per-ToolContext, per-Skill, or per-lease authority. The legacy
browser retains its separate per-turn private-origin policy.

`EXECUTOR_ALLOWED_REQUEST_KINDS`, the browser profile, process lease counts,
resource bounds, `SKILL_EGRESS_PROXY_URL`, and HMAC authority are all
deployment-owned environment values. The fixed-pool limits are both one.
The base executor uses the same root-controller pattern with fixed worker
UID/GID 65528 while retaining its existing v1 one-shot request kinds. It does
not reuse the conventional host `nobody` UID: Linux applies `RLIMIT_NPROC`
across PID namespaces to the real host UID, so each executor lane needs a
dedicated identity for its configured process budget to be meaningful.

The browser lane omits a finite `RLIMIT_AS` and uses a 3 GiB cgroup memory
limit as its resident-memory boundary. Modern Chromium/V8 uses very large,
architecture- and page-dependent sparse mappings; a virtual-address limit is
therefore not a sound RSS control. Real lease A/B tests stalled at Playwright
`newPage()` with 256 GiB, 512 GiB, and 1 TiB, while a renderer's observed RSS
remained tens of MiB. The trusted executor accepts the exact `unlimited` value
only for persistent `browser-automation-v1` processes. The base lane keeps its
existing 2 GiB address-space limit.

Before exposing the executor protocol, the entrypoint drops to UID/GID 65529
with no supplementary groups and runs the real Node Playwright, Python
Playwright, and Selenium smoke. The ordinary protocol health check is therefore
a readiness gate for a container instance that has already passed browser,
Chromium sandbox, and non-root Weston startup.

The production target makes fixed `/workspace`, `/tmp`, and `/dev/shm`
non-writable to UID 65529. Global `/tmp` remains `noexec`. Exact execution
trees instead live on the unshared, controller-owned
`/run/chatds-executor-work` tmpfs. That mount is executable so a standard Skill
can directly invoke a declared shebang helper, but only immutable supported
script suffixes receive mode `0550`; Skill data remains `0440`. The fixed root
is `0755`, so the worker can write only inside the controller-created
lease-private workspace/runtime descendants. The same root is included in
startup, admission, health-boundary, and teardown residue checks. The launcher
validates private worker ownership/mode for `HOME` and `TMPDIR`, uses those
paths for browser temporary state, forces Chromium away from `/dev/shm`, and
keeps the Wayland socket and compositor state inside the private runtime tree.
Both `$CHATDS_SKILL_DIR` and the conventional `$SKILL_DIR` resolve to the same
immutable exact Skill root; the browser launcher forwards only these
runtime-owned values.

Chromium uses Playwright v1.61's pinned Docker seccomp baseline. It retains
`clone`, `setns`, and `unshare` for the non-root user-namespace sandbox while
removing unused SysV IPC and POSIX message-queue syscall families. Both browser
services receive only the additional `SYS_CHROOT` capability required by
Chromium's namespace sandbox. Neither uses `seccomp:unconfined` or `SYS_ADMIN`.

## Historical verification performed

The complete amd64 browser-executor image was built. The pinned dependency
build produced image `a7a5671b694f`; a source-only validation layer containing
the final executor health/resource changes, proxy-relay cancellation handling,
and smoke fixtures produced image `eba3fa299201`. The latter was used for every
low-level browser-matrix result below. A final source refresh containing the
executable private-temp-root and `SKILL_DIR` fixes produced browser image
`76acea01fdf8` and base image `a7afa67c6c2f`; those images passed the
application-level Harness matrix. A Docker Hub auth EOF prevented a second
no-cache frontend resolution, so source-only layers are recorded explicitly
rather than being misrepresented as independent dependency builds.

Static and protocol verification included:

- the joint runtime/profile/topology/proxy/resource suite;
- Python lock resolution with `pip --dry-run --require-hashes`;
- Node lock resolution with `npm ci --dry-run --ignore-scripts`;
- Python source compilation, `git diff --check`, Dockerfile build checks, and
  `docker compose config -q`;
- default-deny browser and base seccomp profiles, with direct syscall probes
  confirming SysV IPC and POSIX message-queue operations return `EPERM`; and
- a real base executor image (`42c27b419f09`) using UID/GID 65528, zero worker
  capabilities/groups, no route, no direct public/private/metadata reachability,
  clean shared roots, and the same IPC-denial result.

The real browser process-lease matrix passed:

- headed Node Playwright through CommonJS and ESM;
- headed Python Playwright and Selenium;
- persistent public class and factory invocation with Playwright and Selenium;
- idempotent stdin close, output offsets, method calls, close acknowledgements,
  and artifact sync;
- a deterministic 12,589,062-byte PNG, proving the greater-than-8-MiB artifact
  path;
- UID/GID 65529, no supplementary groups/capabilities, loopback only, no route,
  no direct public/private/metadata connection, no controller/proxy UDS
  authority, private/metadata proxy rejection, and successful public HTTPS
  proxying; and
- a detached `setsid`/double-fork descendant followed by lease close, leaving
  zero host tasks for UID 65529 and empty `/tmp`, `/dev/shm`, and `/workspace`.

The historical `8e486.../visual-browser-operator` package was exercised as an
acceptance fixture, not as a routing special case. Its exact
`ChromeVisualSession` class completed constructor, local observe, public
`open`, artifact sync, method close, and lease close. The public URL from that
Skill's own reference was reached through the policy proxy and produced
document responses `202 Accepted` then `400 Bad Request`; the resulting empty
page warning is therefore classified as upstream site behavior, not a missing
Selenium/Playwright/Bash capability.

Docker health remained `healthy` with zero failing streak through 60 checks
while ordinary and persistent leases were active. A deliberately abandoned
live lease had five UID-65529 tasks and a private runtime tree before Docker
restart; the restart boundary left neither behind, the new controller reran
the headed readiness smoke, returned healthy, and accepted a fresh Playwright
lease. Independently, the Harness startup reaper observed and reaped one live
abandoned lease without restarting the container; the following Playwright
lease passed and the worker again had zero UID-65529 tasks and empty shared
roots. A separate real setsid/double-fork/SIGTERM-refork containment test also
passed before a second lease. The legacy UID-65532 CDP browser started healthy
under the same custom browser seccomp profile.

Application-level Harness routing also passed against the two real executor
UDSes. The repeatable `harness_process_acceptance.py` runner entered only
through `run_skill_process` and used one exact, digest-authorized Skill package.
It selected `base-v1` for the ordinary identity probe and
an exact Bash entrypoint, and `browser-automation-v1` for the headed Node
Playwright entrypoint and a persistent Python `BrowserProbe` instance. The
base probe returned `base-identity-ok`. Bash directly executed its declared
`${SKILL_DIR}/bash_direct_helper_child.sh` and returned
`bash-direct-helper-ok`, proving the shebang/immutable-execute path rather than
only interpreter-mediated Bash. Node returned `node-playwright-ok`; the
persistent method call returned `harness-persistent-browser-ok`. For all four,
the manager's
authorized package/entrypoint digests and the executor lease attestations
matched the same immutable snapshot. Explicit close left no live manager
records or retained cleanup retries. This separate high-level acceptance keeps
a low-level client test from being mistaken for the full Harness routing path.

## Historical security limitations

The proxy environment and Chromium wrapper are application-layer defense in
depth. Arbitrary untrusted code may create its own socket, but the
`network_mode: none` boundary leaves it no direct destination other than the
fixed controller bridge on loopback. Therefore this image must never be
attached directly to the application network, default bridge, host network, or
an unrestricted Internet network.

The runtime does not authorize CAPTCHA solving, anti-bot evasion,
access-control bypass, or unconfirmed important actions. Chromium sandbox
support must be provided by the host/container security profile; do not add
`--no-sandbox` as a compatibility workaround.

The fixed pool limits blast radius with HMAC-authenticated v2 requests,
dumpability hardening, a distinct worker UID, process-only request policy, one
active lease, controller-owned dependencies, and worker-UID sweeping. It is
still a fixed-size container pool, not dynamic per-lease containers.

The private execution tmpfs is intentionally executable because an authorized
Skill process may directly invoke a declared helper. It does not grant a new
entrypoint or package capability—the Harness still starts only a
snapshot/digest-authorized script—but code already running inside that lease
can create native executables in its own writable runtime tree. Seccomp,
capability, network-namespace, cgroup, fixed-UID, and teardown boundaries must
therefore remain mandatory.
