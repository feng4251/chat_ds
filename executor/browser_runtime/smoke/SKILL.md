---
name: browser-runtime-smoke
description: Immutable acceptance fixtures for the generic browser executor profile.
---

# Browser runtime smoke

These scripts validate the prebuilt browser profile. They use only local
in-memory pages and never install dependencies or access a remote origin.

Use the exact package-relative entrypoints below for the corresponding
acceptance checks:

- `base_identity_probe.py` validates the networkless base worker identity.
- `bash_direct_helper.sh` directly executes the declared
  `bash_direct_helper_child.sh` through `$SKILL_DIR` to validate
  immutable executable helpers.
- `node_playwright.cjs` and `node_playwright.mjs` validate headed Node
  Playwright through CommonJS and ESM.
- `python_browsers.py` validates headed Python Playwright and Selenium.
- `persistent_browser.py` exposes the public `BrowserProbe` class and
  `open_browser_probe` factory for persistent method-call validation.
- `ipc_denied.py` validates the denied cross-process IPC syscall families.
- `network_identity_probe.py` validates browser identity and policy-proxy
  boundaries.
- `escape_descendant.py` validates detached-descendant cleanup.
- `large_visual_artifact.py` validates bounded large-artifact synchronization.
- `long_running.py` is the deliberate abandoned-lease cleanup fixture.
