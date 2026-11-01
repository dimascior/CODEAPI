# Release Hygiene — CODEAPI VSIX 0.1.6 (current release record)

The current shipped surface, the validation evidence behind it, and the boundaries
honored. Companion to [DISCOVERY_AND_TERMINAL_ROUTING.md](DISCOVERY_AND_TERMINAL_ROUTING.md),
[CODEAPI_RUNTIME_ARCHITECTURE.md](CODEAPI_RUNTIME_ARCHITECTURE.md), and
[INSTALL_AND_BUILD.md](INSTALL_AND_BUILD.md).

**Release state (2026-07-10):**

- Branch `release/terminal-runtime-cleanup`, consolidated history, **pushed** to
  `origin` (github.com/dimascior/CODEAPI).
- Manifest version **0.1.6** in BOTH `package.json` and `package-lock.json` (root and
  package entry — the lockfile had lagged at 0.1.3 through two releases; kept in
  lockstep since 0.1.5); artifact `codeapi-0.1.6.vsix`.
- 0.1.6 (delta over 0.1.5): **visible terminals track the pane's dimensions** — the
  pseudoterminal bridge now forwards `open(initialDimensions)`/`setDimensions()` to
  `pty.resize`, fixing cursor-addressed output rendering off-view/overpainted in the
  visible pane while `/term/read` carried every byte (TERM_CONTRACT §6.1 note).
- `LICENSE.md` (proprietary, `"license": "UNLICENSED"`) ships in the VSIX; no
  missing-license warning.
- 0.1.5 closes the four pre-release findings: the JS-probe/Python-bind race
  (exact-bind + identity-aware readiness, §5), stale version metadata, the stale
  shipped `extension-adapter/package.json` (now a minimal internal manifest — no
  skeleton/phase language, no duplicate contributions), and the ineffective
  `codeapi.pythonServicePortBase` setting (removed; the bounded band is
  architectural policy).
- Phase 15 is **complete**: the legacy source surfaces, the untracked seed CLI, and the
  byte-parity test are gone; the `capi` label is purged repo-wide
  (`capiTerm` → `codeApiTerm`, terminal label `codeapi:<id>`, CLI prog `codeapi`).
- Installed locally as `dimas.codeapi-0.1.6` (replaced 0.1.5). The VSIX is **not
  published** to any marketplace.

## 1. Packaging surface

The runtime ships as three things only: the VS Code adapter, the packaged Python
runtime exe, and the native PTY backend.

Included in the VSIX (from the `vsce package` tree for 0.1.5):

- `package.json`, `readme.md`, `LICENSE.md`
- `extension-adapter/**` — `extension.js` (`package.json.main`), `bridge_core.js`,
  its `package.json`, and `tests/test_bridge_core.js`
- `bin/codeapi-runtime/**` — PyInstaller onedir runtime (`codeapi-runtime.exe`
  1.19 MB + `_internal/` 56 files, 12.44 MB); `bin/` is git-ignored but ships, so it
  is NOT in `.vscodeignore`
- `node_modules/node-pty/**` — native PTY backend (the only `node_modules` entry kept;
  prebuilds are the bulk of the VSIX size)

Excluded via `.vscodeignore`: `.git/**`, `.github/**`, `.vscode/**`, `docs/**`,
`python/**`, `scripts/**`, `extension-bridge/**`, build/test/coverage/log trees,
`*.vsix`/`*.map`/`*.ts`/`*.log`/`__pycache__`, toolchain configs, and the non-runtime
root files `openapi.json` / `standalone-exec-with-action.js` — with
`node_modules/**` excluded except the single `!node_modules/node-pty/**` negation.

## 2. Installed-VSIX validation

Built with `npm run vsce:package` (prepublish rebuilds the PyInstaller runtime, so
the packaged exe carries the exact-bind allocation semantics), installed into the
real profile with `code --install-extension codeapi-0.1.5.vsix --force`. The running
window adopts 0.1.5 on the next reload; the post-reload probes that verify an
install (proven this way for 0.1.4, unchanged for 0.1.5):

- Extension identity: `vscode.extensions.all` reports `dimas.codeapi` at
  `…\.vscode\extensions\dimas.codeapi-<version>`, active.
- `/exec` globals: `typeof codeApiTerm === "object"` and `typeof capiTerm ===
  "undefined"`.
- `/session` now additionally carries `codeapiWorkspaceId` + `codeapiRuntimeId`.

## 3. Identity-selected visible terminal proof (proven on the 0.1.4 install;
   terminal surface unchanged in 0.1.5)

- `POST /term/create` (`--show`, `powershell.exe`, cwd `CODEAPI-clone`) →
  `{id: tmrdu8wc30, pid: 24356}`; `vscode.window.terminals` lists
  **`codeapi:tmrdu8wc30`** — the renamed visible-terminal label, in the intended
  window.
- Real work was routed through that PTY (git stage/commit/push typed via
  `term write --b64`, output grounded via `/term/read`), proving write → execute →
  readback end to end on the installed build.

## 4. Identity model — durability and uniqueness split

No supported VS Code storage is both durable across reloads and per-window unique, so
identity no longer asks one field for both:

- **`codeapiWorkspaceId`** (legacy wire name `codeapiWindowId`, same value) — persisted
  in `workspaceState`; durable across reloads, **shared by windows on one workspace**
  (grouping key). Sharers are flagged `duplicate-codeapi-window-id`, and its
  first-activation persist race (two windows minting ids simultaneously) is documented
  in the threat model; the persist is awaited but cannot be made atomic across hosts.
- **`codeapiRuntimeId`** — fresh UUID per adapter activation, never persisted; **the
  unique live-window target** (rotates on reload — rediscover after).
- **`vscodeSessionId`** — a VS Code-provided session correlation signal (secondary
  key; per-session uniqueness is VS Code's guarantee, not CODEAPI's); the compound
  display key `<codeapiWorkspaceId>@<vscodeSessionId>` combines grouping + session.

**Every selector is strict**: `select_by_identity` (at every precedence tier) and
`select_session` raise on multiple final candidates, naming the ports — no silent
first-match. Covered by 10 adversarial cases in `python/tests/test_discovery.py`
(collision flagging, raise-on-ambiguity at each tier, runtime-id targeting through a
workspace-id collision, first-activation race scenario).

## 5. Port policy — allocation range equals discovery range

Both allocators (JS `bridge_core.pickFreePort`, Python `PortAllocator`) are bounded to
the **same band discovery scans: 47337–47400** (64 simultaneous adapter runtimes).
Exhaustion fails activation **visibly** (error notification + runtime log; the Python
allocator raises), and an out-of-band preferred port is refused — an unbounded
allocator could otherwise start a healthy runtime on 47401+ that discovery never
finds. Proven by exhaustion tests on both sides (fake-server injection in
`test_bridge_core.js`; monkeypatched allocator in `test_runtime_pure.py`).

**The probe→bind race is closed by an exact-bind handshake.** The JS probe cannot
hold the port it selects, so a racer can grab it before the Python bind. The runtime
therefore binds an adapter-selected port **exactly or exits with the distinct
allocation code 3** — it never falls forward to a different port (that would desync
the adapter's polling, or worse, let the adapter adopt a stranger). The adapter keys
on exit 3 to retry its next candidate, and **readiness is identity-aware**: it polls
`/session` and requires `serverPort`, `codeapiRuntimeId`, and `extensionHostPid` to
match its own launch (`bridge_core.sessionMatches`) — an HTTP 200 from a foreign
runtime on the polled port is rejected and retried, never adopted. Proven by the
occupied-port adversarial case in `test_runtime_cli.py` (exit 3, no fall-forward) and
the foreign-session rejection cases in `test_bridge_core.js`.

## 6. Regressions (2026-07-10, at the 0.1.5 build)

`node --check` clean (`extension.js`, `bridge_core.js`); `bridge_core` **51/51**
(incl. bounded-allocation/exhaustion and identity-aware-readiness cases). Python
(self-executing scripts, `PYTHONPATH=python`): discovery **46/46**, smoke **4/4**,
runtime_pure **30/30**, runtime_cli **6/6** (incl. the occupied-port adversarial
race), adapter_client **21/21**, adapter_term **16/16**, term_routes **12/12**,
vscode_routes **14/14** — **149/149 total**.

## 7. Boundaries honored

- The runtime binds only the reserved adapter band (47337–47400) — allocation and
  discovery share the band, exhaustion is visible; the VS Code window band 3637–3646
  stays reserved for legacy runtimes (permanent policy, enforced by `_guard_port` and
  both allocators).
- Public wire shapes unchanged (WIRE_CONTRACT / TERM_CONTRACT byte compatibility).
- Terminal liveness observed from `/term/list`, never inferred from `DELETE`.
- VSIX installed locally only; no marketplace publication.
