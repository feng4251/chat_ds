# Unified session-sandbox dependency profile

The immutable `browser-automation-v1` dependency profile is the superset used
by the physical `session-sandbox-v1` executor. Standard-format Skills can ship
exact `.cjs`, `.js`, `.mjs`, `.py`, `.sh`, or `.bash` entrypoints without
choosing a separate execution environment. It contains:

- Node 22.23.1 and Node Playwright 1.61.0;
- CPython 3.12.11, Python Playwright 1.61.0, and Selenium 4.46.0;
- the complete shared base Skill Python dependency manifest (scientific,
  data, HTTP, PDF, and Office/report generation libraries);
- snapshot-pinned `curl` for declared command-line HTTP requests;
- a Chromium/ChromeDriver pair from one immutable Debian snapshot;
- Weston headless compositor for scripts that request a visible browser;
- no runtime npm, npx, pip, or apt frontend.

The normal entrypoint is:

```text
/usr/local/bin/chatds-browser-runtime-exec path/to/exact-script.cjs [args...]
/usr/local/bin/chatds-browser-runtime-exec path/to/exact-script.py [args...]
/usr/local/bin/chatds-browser-runtime-exec path/to/exact-script.sh [args...]
```

The launcher selects a fixed interpreter from the extension, starts a private
Weston compositor and Wayland socket, loads dependencies from root-owned paths, and validates the
runtime-injected proxy endpoint. It does not evaluate a shell command and does not
install missing packages. Long-running stdin/stdout protocols remain alive for
the life of the worker process; process leasing, idempotent input delivery,
artifact synchronization, cancellation, and TTL cleanup belong to the executor
daemon protocol rather than this image.

Node dependencies are also exposed through the immutable root
`/node_modules -> /opt/chatds-browser-runtime/node_modules` symlink. This is
required because ESM bare imports do not use `NODE_PATH`; `.mjs` and
module-style `.js` Skills therefore resolve the same pinned Playwright package
as CommonJS scripts without runtime installation.

## Required deployment boundary

This image is not safe to attach to an ordinary application, bridge, or host
network. The production session sandbox runs with `network_mode: none`: its
namespace has loopback but no Ethernet interface, Docker DNS, gateway, or
default route. For each execution or persistent lease, the controller creates
one ephemeral loopback byte bridge to
`/run/chatds-skill-egress/proxy.sock`. The bridge signs the exact HTTP methods
and canonical URL prefixes compiled from the frozen ToolContext; derived
origins are only routing metadata and an empty rule set is deny-all. The proxy
socket is supplied read-only from a named volume,
and only the separately networked policy proxy can resolve and dial
destinations. Do not mount a Docker socket or expose raw CDP to the Skill
process.

Every request method and URL must match the selected Skill's frozen capability
closure. A prefix ending in `/` may cover descendants; any other path is exact.
Private destinations require three independent gates: an exact rule for the
current Skill, the deployment allowlist, and either an explicit URL in the
current user turn or an explicit continuation resolved to the nearest bounded
user-authored URL turn. Assistant, tool, and ambient-history URLs never grant
authority. A literal private IP is pinned to that exact address; an
allowlisted hostname must also resolve entirely inside the configured private
CIDRs. No model argument, request header, proxy environment override, or
sibling Skill can add a method, URL prefix, or private-network grant.

The lease launcher injects the runtime-owned environment key
`SKILL_EGRESS_PROXY_URL` with the ephemeral loopback endpoint. Proxy
credentials, fragments, non-canonical URLs, and caller-selected proxy
endpoints are forbidden; paths and query prefixes remain inside the signed
rule. Container readiness alone uses a short-lived fixed-port deny-all
bridge for the baked browser smoke, closes it, and only then starts the
executor server.

The launcher also exports this endpoint as the standard HTTP(S) proxy
environment. The Chromium wrapper rejects caller proxy overrides, disables QUIC and
non-proxied WebRTC UDP, and makes loopback browser requests use the proxy.
Playwright's expected browser executable and `/usr/bin/chromium` both resolve
to this wrapper. It also rejects caller host-resolver rules, TCP remote-debug
listeners, and anti-detection Blink flags. ChromeDriver is allowed only its
internally generated ephemeral `--remote-debugging-port=0` when it is the
wrapper's direct parent; no debug port is published or shared across workers.
These controls are defense in depth, not a substitute for the network
namespace: untrusted code can search for lower-level browser binaries or
implement its own socket client. Such a client still sees only loopback, and
the only loopback egress service relays to the fail-closed policy proxy.

## Identity and filesystem contract

- The lifecycle controller, proxy bridge, and executor UDS run as root.
- Each exact Skill process receives its own short-lived Weston headless
  compositor started as the worker UID. Its Wayland socket, lock, and log live
  only in the lease-private `XDG_RUNTIME_DIR`; no root-owned display server or
  cross-lease display socket is reachable by untrusted lease code.
- All untrusted session-sandbox workers are UID/GID 65529, deliberately
  distinct from the root controller and from the legacy
  browser's 65532 because `RLIMIT_NPROC` accounting is host-UID-global.
- The controller uses `/usr/bin/prlimit` and native process credentials to
  spawn the exact Skill entrypoint as UID/GID 65529 after clearing every
  supplementary group. Admission is serial and cleanup sweeps that dedicated
  UID before it can be reused.
- Global `/tmp` remains non-writable and `noexec` for the worker. Exact Skill
  snapshots and lease-private runtime state live under the separate,
  controller-owned `/run/chatds-executor-work` executable tmpfs. Its fixed root
  is not worker-writable; supported immutable script suffixes are `0550` and
  all other Skill resources remain `0440`. This permits declared shebang
  helpers without turning the shared temp root or Skill data into executables.
- `$CHATDS_SKILL_DIR`, `$CHATDS_SKILL_ROOT`, and conventional `$SKILL_DIR`
  identify the same immutable exact package. Use one of those anchors for a
  direct local helper when the process working directory is `workspace`;
  alternatively request `cwd=skill`. Bare relative Skill paths are not
  resolved from the default workspace directory.
- The egress proxy is UID/GID 65531. Its setgid UDS directory assigns GID
  65530 only to the controller bridge. The worker never receives that group.
- Run the container with a read-only root filesystem, all capabilities dropped
  then add only `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `KILL`, `SETGID`, `SETUID`,
  and Chromium's sandbox-local `SYS_CHROOT` to the controller. Retain
  `no-new-privileges`, bounded
  PID/memory/CPU limits, and writable tmpfs mounts only for `/tmp` and
  `/dev/shm`.
- Do not apply a finite `RLIMIT_AS` to the unified dependency-superset lane.
  Chromium 150/V8 uses
  architecture- and page-dependent sparse virtual mappings while a renderer's
  observed RSS remains small; 256 GiB, 512 GiB, and 1 TiB limits all stalled a
  real Playwright `newPage()` lease. Resident memory is independently and
  strictly bounded by the 3 GiB container cgroup. The trusted executor accepts
  `unlimited` only for the fixed session-sandbox dependency profile.
- Materialize the exact Skill package read-only in the private execution tmpfs
  and keep its lease workspace as a separate writable descendant.
- Keep fixed `/workspace`, `/tmp`, and `/dev/shm` roots non-writable to the
  worker. The controller creates/chowns only a private per-lease runtime tree;
  `HOME`, `TMPDIR`, `XDG_RUNTIME_DIR`, the Wayland socket, and compositor state
  stay inside that tree and are removed during process/lease teardown.

The standalone dependency target defaults to worker UID 65529. The
`session-sandbox` target starts the single root controller.

## Health checks

The default lightweight health check validates the installed manifest, pinned
language/library versions, paired Chromium/ChromeDriver major, distinct UIDs,
absence of runtime installers, and Playwright's wrapper binding. It does not
open a browser.

The production controller runs the explicit browser smoke as UID/GID 65529,
with supplementary groups cleared, before it starts the executor protocol
server. Therefore the protocol health check cannot become ready unless Node
Playwright, Python Playwright, Selenium, Chromium's sandbox, and non-root Weston
have already passed once for that container instance.

After injecting `SKILL_EGRESS_PROXY_URL` and applying the production container
security profile, run the explicit integration smoke:

```text
/usr/local/bin/chatds-browser-runtime-health --browser-smoke
```

That opens local in-memory pages through Node Playwright, Python Playwright,
and Selenium in headed mode. It does not access the Internet.

`smoke/large_visual_artifact.py` is a separate process-lease integration
fixture. It deterministically writes an uncompressible PNG between 8 MiB and
24 MiB to `CHATDS_OUTPUT_DIR`; use it to verify end-to-end artifact sync limits
without making every container readiness check allocate a large file.

## Security and functional limits

- The runtime provides normal browser automation. It does not authorize
  CAPTCHA solving, anti-bot evasion, access-control bypass, or unconfirmed
  important actions.
- Debian snapshot pinning fixes the browser closure but also freezes security
  updates. Updating the profile requires a reviewed snapshot/version bump and
  rebuild, never an install inside a live worker.
- Chromium sandbox compatibility depends on the host kernel/container seccomp
  profile. The repository pins Playwright v1.61's Docker seccomp baseline,
  retains `clone`, `setns`, and `unshare` for Chromium's unprivileged
  user-namespace sandbox, and removes SysV IPC and POSIX message-queue
  syscalls that the browser runtime does not require. The wrapper removes
  Playwright's default `--no-sandbox` argument. Do not use
  `seccomp:unconfined`, add `SYS_ADMIN`, or re-enable `--no-sandbox`.
- Full safety still depends on executor leases, digest-bound script authority,
  operation idempotency, cleanup, and the external egress policy gateway.
- This profile intentionally does not provide X11. Standard Skills that
  declare X11 as mandatory must fail capability preflight clearly instead of
  receiving an emulated or shared X display.

## Maintenance and rebuild policy

Review this profile at least monthly and immediately after a relevant Chromium,
ChromeDriver, Playwright, Python, or Debian security advisory. A profile update
must:

1. choose a recent timestamped Debian snapshot whose `bookworm` and
   `bookworm-security` Release files are both available;
2. pin the same current stable Playwright release in Node and Python, regenerate
   both integrity/hash locks, and verify the checked-in seccomp JSON derives
   from that Playwright tag's `utils/docker/seccomp_profile.json` with only
   the documented cross-process IPC removals;
3. build the immutable dependency and `session-sandbox` targets, record the
   resolved Chromium and ChromeDriver patch versions from
   `installed-manifest.json`, and require their majors to match;
4. rerun static/profile tests, the non-root Node/Python Playwright and Selenium
   readiness smoke, the legacy-browser smoke, direct-egress negative probes,
   signed public/private/deny-all origin-policy probes, and Compose topology
   checks; and
5. deploy only a tested image digest and update the deployment digest record.

Never replace the timestamp, dependency versions, or deployment image with a
floating `latest` reference. The current profile uses the 2026-07-20 Debian
snapshot (the newest reviewed snapshot more than 48 hours old when selected)
and Playwright 1.61.0, released 2026-06-29.
