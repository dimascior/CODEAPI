# Release Hygiene — terminal-first CODEAPI VSIX

How the shipped VSIX was trimmed to the terminal-first runtime essentials, and the
live proof that an identity-selected, adapter-owned **visible** terminal executes
commands in the intended VS Code window. Companion to
[DISCOVERY_AND_TERMINAL_ROUTING.md](DISCOVERY_AND_TERMINAL_ROUTING.md) and
[CODEAPI_RUNTIME_ARCHITECTURE.md](CODEAPI_RUNTIME_ARCHITECTURE.md).

Branch: **`release/terminal-runtime-cleanup`** (cut from `b880701` — adapter-pinned
window identity; later advanced through **Phase 15** legacy retirement and this
finalization pass). The VSIX is still **not published**; `the seed CLI` remains **untracked**
and untouched. For the current operational picture see [FILE_FLOW.md](FILE_FLOW.md),
[INSTALL_AND_BUILD.md](INSTALL_AND_BUILD.md), [OPERATIONAL_GUIDE.md](OPERATIONAL_GUIDE.md),
and [DEBUGGING.md](DEBUGGING.md); developer config is tracked under `.vscode/`.

## 1. Packaging surface

The runtime ships as three things only: the VS Code adapter, the packaged Python
runtime exe, and the native PTY backend.

Included in the VSIX:

- `package.json`
- `readme.md` (vsce always includes the README)
- `extension-adapter/**` — `extension.js` (`package.json.main`), `bridge_core.js`
  (plus its `package.json` and `tests/test_bridge_core.js`)
- `bin/codeapi-runtime/**` — the PyInstaller onedir runtime (`codeapi-runtime.exe`
  + `_internal/`); `bin/` is git-ignored but **must** ship, so it is NOT in
  `.vscodeignore`
- `node_modules/node-pty/**` — native PTY backend (the only `node_modules` entry kept)

Excluded via `.vscodeignore` (`node_modules/**` with a single `!node_modules/node-pty/**`
negation, plus): `.command-gate/**`, `.git/**`, `.github/**`, `.vscode/**`,
`.vscode-test/**`, `docs/**`, `python/**` (incl. `python/build/**`, `python/tests/**`,
`python/.pytest_cache/**`), `scripts/**`, `legacy-seed/**`, `extension-bridge/**`,
`dist/**`, `src/**`, `out/**`, `cli/**`, `test/**`, `tests/**`, `build/**`,
`coverage/**`, `logs/**`, `*.vsix`, `*.map`, `*.ts`, `*.log`, `**/__pycache__/**`,
`*.pyc`, esbuild/eslint/tsconfig toolchain configs, and the three non-runtime root
files `AGENTS.md`, `openapi.json`, `standalone-exec-with-action.js`.

### VSIX contents before / after

| build | files | size | ships bloat? |
|---|---|---|---|
| before (`33b1bc4`/`7b38f36`) | 546 | 25.67 MB | `.command-gate/`, `legacy-seed/` (incl. `the seed CLI`), `dist/`, `python/`, `docs/` (incl. `REBUILD_PLAN.md`), `scripts/`, `extension-bridge/` |
| after (this branch) | **353** | **22.02 MB** | none — only `package.json`, `readme.md`, `bin/codeapi-runtime/**`, `extension-adapter/**`, `node_modules/node-pty/**` |

`vsce package` still emits a non-fatal `LICENSE … not found` **warning**: the project is
intentionally `"private": true` / `"license": "UNLICENSED"`, so per the mission no
open-source license was invented. The warning is expected and left in place.

## 2. Isolated installed-VSIX validation

Install + launch into isolated dirs (no `--extensionDevelopmentPath`):

```
code --user-data-dir  C:\Users\dimas\AppData\Local\Temp\codeapi-clean-vsix-userdata \
     --extensions-dir C:\Users\dimas\AppData\Local\Temp\codeapi-clean-vsix-extensions \
     --install-extension C:\Users\dimas\Desktop\CODEAPI\codeapi-0.1.3.vsix --force

code --new-window \
     --user-data-dir  C:\Users\dimas\AppData\Local\Temp\codeapi-clean-vsix-userdata \
     --extensions-dir C:\Users\dimas\AppData\Local\Temp\codeapi-clean-vsix-extensions \
     C:\Users\dimas\Desktop\CODEAPI
```

Installed extension dir: `…\codeapi-clean-vsix-extensions\dimas.codeapi-0.1.3`.

**Packaged mode from the installed dir proven** (Get-CimInstance Win32_Process):

```
pid=31184  …\codeapi-clean-vsix-extensions\dimas.codeapi-0.1.3\bin\codeapi-runtime\codeapi-runtime.exe
           --host 127.0.0.1 --port 47337 --adapter-port 52308
```

The exe path is INSIDE the installed extension dir — not the source repo, not
`python.exe`. (The pre-existing bloated-VSIX runtime `pid=38636` on 47339 stayed alive
under the older `codeapi-vsix-extensions` dir and is a useful contrast below.)

## 3. Identity-selected visible terminal proof

Identity-aware discovery over both bands (`discover_sessions`) saw exactly the two
adapter runtimes and told them apart by durable identity:

| port | type | terms | pyPid | extPid | codeapiWindowId | flags |
|---|---|---|---|---|---|---|
| 47337 | adapter | live | 31184 | 39500 | `61ae336c-0516-43f4-8dd7-9cea7b31de9a` | no-window-id, duplicate-workspace |
| 47339 | adapter | live | 38636 | — | — | no-window-id, **no-codeapi-window-id**, duplicate-workspace |

- **Selected** the clean runtime by identity:
  `select_by_identity("61ae336c-0516-43f4-8dd7-9cea7b31de9a", require_terminals=True)`
  → port 47337. The stale pre-identity runtime (47339) is correctly flagged
  `no-codeapi-window-id` and is not the target.
- **Created** a visible terminal (`show:true`) → `codeapi:tmrasdq3r0` (pid 35420,
  `powershell.exe`, cwd `c:\Users\dimas\Desktop\CODEAPI`), confirmed a real VS Code
  terminal: `vscode.window.terminals` → `['codeapi:tmrasdq3r0']`.
- **Ran a human-visible command:**
  `Write-Output "CODEAPI_VISIBLE_TARGET_d5bff6be"; Get-Location; git status --short --branch`
- **Read back** through `/term/read` (861 bytes): the marker
  `CODEAPI_VISIBLE_TARGET_d5bff6be`, the cwd `C:\Users\dimas\Desktop\CODEAPI`, and
  `## release/terminal-runtime-cleanup` (branch line from `git status --branch`).
- **Liveness** observed from `/term/list`: `alive:false` + `exitCode:0` — **before**
  DELETE. DELETE then returned `{killed:false}` (already exited, proving liveness did
  NOT come from DELETE); read-after-delete → `404`. Test PTY cleaned up.

`VISIBLE TERMINAL PROOF: PASS` (marker `CODEAPI_VISIBLE_TARGET_d5bff6be`,
terminal `codeapi:tmrasdq3r0`, exitCode 0, port 47337).

## 4. Regressions

`node --check` clean (bridge_core.js + extension.js); `bridge_core` **43/43**.
Python (run directly, not pytest): discovery **33/33**, term_routes **12/12**,
adapter_term **16/16**, adapter_client **21/21**, runtime_pure **27/27**,
vscode_routes **14/14**, runtime_cli **4/4** (**127** total). `test_runtime_cli` bound
47338 because 47337 was held by the live clean-VSIX runtime.

## 5. Boundaries honored

- No Phase 15.
- `cli.py` unchanged (discovery/identity stays a separate utility — no `--help`/parity impact).
- `the seed CLI` unchanged.
- `package.json.main` unchanged (already `./extension-adapter/extension.js`).
- VSIX **not published**; installed only into an isolated env.
- No push.
