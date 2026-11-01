# CODEAPI Install & Build

The exact clean-clone → build → package → verify sequence, and the boundary between
what the **repo** contains and what the **VSIX** ships.

See also: [FILE_FLOW.md](FILE_FLOW.md), [OPERATIONAL_GUIDE.md](OPERATIONAL_GUIDE.md),
[DEBUGGING.md](DEBUGGING.md).

---

## Prerequisites

| Tool | Purpose | Notes |
|---|---|---|
| Node.js + npm | JS adapter + `node-pty` + `@vscode/vsce` | Node 20+ (validated on v22.x) |
| Python 3.8 (+ PyInstaller) | build the packaged runtime | PyInstaller must be installed for the interpreter that `python` resolves to |
| VS Code | run/debug the extension, install the VSIX | 1.102.0+ |

The build calls `python` (from `PATH`) and needs **PyInstaller** installed for that
interpreter. On this project the runtime is built with a pyenv Python 3.8.10 that has
PyInstaller 6.x. If `python` on `PATH` is a Windows Store alias (exit 9009) or lacks
PyInstaller, point the build at a real interpreter (activate the right venv/pyenv shim
so `python` resolves correctly).

---

## Clean-clone sequence

```bash
# 1. Clone the branch
git clone --branch release/terminal-runtime-cleanup --single-branch \
    git@github.com:dimascior/CODEAPI.git CODEAPI
cd CODEAPI

# 2. Confirm toolchain
node --version        # v20+/v22+
npm --version
python --version      # 3.8.x with PyInstaller

# 3. Install JS deps (node-pty + @vscode/vsce)
npm install

# 4. Build the packaged Python runtime (PyInstaller)
npm run vscode:prepublish
#    -> bin/codeapi-runtime/codeapi-runtime.exe  + bin/codeapi-runtime/_internal/

# 5. Confirm the runtime built
#    Windows: dir bin\codeapi-runtime\codeapi-runtime.exe
#    bash:    test -f bin/codeapi-runtime/codeapi-runtime.exe && echo OK

# 6. Package the VSIX
npm run vsce:package
#    -> codeapi-<version>.vsix   (warning-free; LICENSE.md present)
```

`node_modules/`, `bin/`, and `*.vsix` are git-ignored — they are **generated**, not
committed. A fresh clone therefore has none of them until you run the steps above.
`.vscode/` developer config **is** tracked (see [DEBUGGING.md](DEBUGGING.md)).

You can also drive these from VS Code: **Tasks: Run Task** → `install`,
`build runtime`, `package VSIX` (see `.vscode/tasks.json`).

---

## Install into an isolated VS Code profile (optional)

To validate the packaged extension without touching your normal profile:

```bash
code --user-data-dir  <temp>/codeapi-ud --extensions-dir <temp>/codeapi-ext \
     --install-extension codeapi-<version>.vsix --force

code --new-window --user-data-dir <temp>/codeapi-ud --extensions-dir <temp>/codeapi-ext <repo>
```

On startup the adapter activates and launches the packaged runtime from the installed
extension dir (packaged-mode precedence — see [DEBUGGING.md](DEBUGGING.md)). Then verify:

```bash
curl -s http://127.0.0.1:47337/health          # {"status":"ok"}
curl -s http://127.0.0.1:47337/session          # serverPort + codeapiWindowId + …
```

For the full end-to-end (discover → select → visible terminal → run → read → reap),
see [OPERATIONAL_GUIDE.md](OPERATIONAL_GUIDE.md).

---

## Repo vs VSIX boundary

The repo carries source, tests, docs, and developer tooling. The **VSIX** ships only the
runtime surface. `.vscodeignore` enforces the boundary.

### VSIX includes (runtime surface only)

- `package.json`
- `readme.md` (`README.md`)
- `LICENSE.md`
- `extension-adapter/**` (`extension.js`, `bridge_core.js`)
- `bin/codeapi-runtime/**` (the packaged runtime exe + `_internal/`)
- `node_modules/node-pty/**` (native PTY backend — the only `node_modules` kept)

### VSIX excludes (repo-only)

- `python/**` (raw Python source, tests, `python/build/**`, `python/.pytest_cache/**`)
- `docs/**`
- `.vscode/**`
- `.command-gate/**`
- `.claude/**`
- `.git/**`, `.github/**`, `.vscode-test/**`
- `dist/**`, `src/**`, `out/**`, `cli/**`, `test/**`, `tests/**`, `build/**`, `coverage/**`, `logs/**`
- `legacy-seed/**`, `extension-bridge/**`, `scripts/**` (retired in Phase 15 — not present)
- `*.vsix`, `*.map`, `*.ts`, `*.log`, `*.pyc`, `**/__pycache__/**`
- `node_modules/**` except `!node_modules/node-pty/**`

### Verify the package contents

`npm run vsce:package` prints the included-files tree. Confirm it contains only the
six inclusion groups above and none of the exclusions. The current package is
**~354 files / ~22 MB** (the bulk is `node_modules/node-pty` prebuilds and the
PyInstaller `_internal/`).
