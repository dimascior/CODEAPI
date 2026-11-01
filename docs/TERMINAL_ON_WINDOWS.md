# CODEAPI on Windows 10 — Terminal Interaction, Entry→Endpoint Flow, and OS Implications

This document traces CODEAPI **from the file entry points to the OS endpoints** —
the real Windows processes, pseudo-consoles, ports, and privileges a request
actually touches — and then documents what those interactions *mean* on a Windows 10
host. It is the terminal-and-OS companion to [FILE_FLOW.md](FILE_FLOW.md) (the
layer/endpoint map), [TERM_CONTRACT.md](TERM_CONTRACT.md) (route shapes + liveness),
[DISCOVERY_AND_TERMINAL_ROUTING.md](DISCOVERY_AND_TERMINAL_ROUTING.md) (window
selection), and [SECURITY.md](SECURITY.md).

Reference host for every OS-specific claim below: **Windows 10 (10.0.19045)**,
PowerShell + Git-Bash available, Python via pyenv-win 3.8.10, `node-pty` 1.1.0
(N-API) under the VS Code Electron/Node host.

---

## 1. The one mental model: entry point → endpoint

- An **entry point** is where control enters a file: a route handler
  (`exec_handler`, `create_handler`), a bridge dispatch case (`handleTermCreate`),
  or the extension `activate()`.
- An **endpoint** is where the request stops being data and becomes an **OS effect**:
  a bound TCP socket, a spawned `codeapi-runtime.exe`, or — the one this document is
  about — a **real Windows shell process behind a ConPTY pseudo-console**.

Between them the request crosses a **process boundary** (Python runtime ↔ extension
host) and a **language boundary** (HTTP/JSON ↔ `vscode.*`/`node-pty`). Everything
tricky on Windows lives at those two seams.

---

## 2. The Windows process picture (who spawns whom, who owns the terminal)

This is the single most load-bearing fact and the one most people get wrong: **the
shell a terminal becomes is a child of the VS Code extension host — NOT a child of
the Python runtime.** `node-pty.spawn(...)` runs in `extension.js`
([termCreate, extension.js:349](../extension-adapter/extension.js)), which executes
inside `Code.exe`. The Python runtime never spawns a shell.

```
Code.exe  (VS Code extension host — Electron/Node; loaded package.json.main = adapter)
│
├── codeapi-runtime.exe        ← Layer 2 Python child (spawn, extension.js:109)
│      (or python.exe -m codeapi_vscode.runtime in dev mode)
│      • owns the PUBLIC HTTP port 47337+  • holds NO shells
│
├── conhost.exe + cmd.exe      ← a CODEAPI terminal (node-pty ConPTY), owned by the HOST
├── conhost.exe + powershell.exe   ← another terminal (shell chosen per /term/create)
└── …one (conhost + shell) pair per live PTY
```

Direct consequences on Windows:

- **Killing the Python runtime does not kill any terminal.** The shells outlive it;
  they are the host's children. Only `deactivate()`
  ([extension.js:134](../extension-adapter/extension.js), which loops `terms` and
  `pty.kill()`s each) or closing the VS Code window reaps them.
- **`codeapi.restartRuntime` moves the public port but keeps every terminal.**
  `startRuntime` stops only the Python child and re-spawns it against the **same**
  bridge port and the **same** in-memory `terms` registry. So after a restart the
  same PTYs are still alive and still reachable — on the **new** `47337+` port. A
  client pinned to the *old* port is stranded and must re-discover (this is exactly
  why identity + discovery exist).
- **Terminal privileges = the extension host's token.** node-pty passes
  `env: process.env` and inherits the host's user, PATH, and integrity level. If VS
  Code was launched elevated (e.g. an automated launch under the Windows `Admin`
  account), **every CODEAPI shell is elevated too**, with the Admin PATH — not the
  interactive `dimas` environment. This changes what `git`/`python`/etc. resolve to
  inside the PTY and is a real privilege-escalation surface (see §7).

---

## 3. Entry→Endpoint trace of one terminal command

A caller runs `git status` in a chosen window and reaps the terminal. Each hop names
the **entry** (the function reached) and the **endpoint/effect** it produces.

| # | Call | Entry (file · function) | Crosses | Endpoint / OS effect |
|---|---|---|---|---|
| 1 | `POST /term/create {shell:"powershell.exe",show:true}` | `term_routes.py · create_handler` | public HTTP → | delegates to the adapter |
| 2 | → bridge | `vscode/adapter.py · term_create` → `extension.js · handleTermCreate` → `termCreate` | Python→host process boundary; **token checked first** | `node-pty.spawn` → **ConPTY + powershell.exe** born as a `Code.exe` child; a visible `codeapi:<id>` terminal appears in the window |
| 3 | `GET /term/list` | `handleTermList` (+ `reconcileAlive`) | → | reads the in-host registry; reports `{pid, alive, exitCode}` |
| 4 | `POST /term/:id/write {data:"git status",enter:true}` | `handleTermWrite` | JSON→`String(data)`→`pty.write` | keystrokes injected into the ConPTY input; `\r` appended = Enter; shell parses them |
| 5 | `GET /term/:id/read` | `handleTermRead` | → | returns the raw VT/ANSI screen buffer (ring-capped at ~1 MB) |
| 6 | write `"exit"` + poll `GET /term/list` | `handleTermWrite` + `onExit` | → | shell exits; `onExit` records `alive:false` + integer `exitCode` |
| 7 | `DELETE /term/:id` | `handleTermDelete` | → | `pty.kill()` + `Pseudoterminal.dispose()`; removed from registry; `{killed:false}` if already exited |

The **only** place the OS is actually touched is step 2's `node-pty.spawn` and step
4's `pty.write` — everything else is bookkeeping over that one live handle. Note the
asymmetry the contract insists on: step 4's `{wrote:N}` is *bytes accepted by the
ConPTY input*, not proof the command ran; the success signal is the **buffer** in
step 5 and the **exitCode** in step 6.

---

## 4. The shell endpoint on Windows

### 4.1 Which shell you get (the COMSPEC default trap)

[`resolveTermDefaults` (bridge_core.js:88)](../extension-adapter/bridge_core.js) picks:

```
shell (win32) = opts.shell || process.env.COMSPEC || "powershell.exe"
```

On effectively every Windows 10 install `COMSPEC` is set to
`C:\Windows\system32\cmd.exe`, so **the default terminal is cmd.exe, not
PowerShell.** This matters because the two shells parse input completely differently
(`$env:X` vs `%X%`, quoting, `;` vs `&`, pipelines). Always pass `shell` explicitly
unless you truly want cmd:

```bash
curl -sX POST $BASE/term/create -H "Content-Type: application/json" \
     -d '{"shell":"powershell.exe","show":true}'
```

Other defaults (all overridable per call): `cols=120`, `rows=30`, `maxBytes=1000000`
(a **decimal** MB ring buffer, not 1 MiB), `cwd = opts.cwd || <workspace folder> ||
os.homedir()` — on Windows a `C:\…` path.

### 4.2 ConPTY, not a raw pipe

On Windows 10 ≥ 1809, node-pty drives a **ConPTY** (Windows pseudo-console); each PTY
gets its own `conhost.exe`. Implications for what you read back:

- **The buffer is a raw VT/ANSI *screen*, not a clean stdout stream.** ConPTY emits
  cursor moves, colour codes, and redraw sequences. `GET /term/:id/read` returns
  exactly that. Parse the region *after* your command's echo; strip ANSI (`\x1b[…m`,
  cursor ops) before matching. Do not expect line-buffered plaintext.
- **`clear=true` erases the buffer — including any exit marker.** This is precisely
  why liveness is structural (`/term/list`), never scraped from the buffer.

### 4.3 The write path is keystrokes, and metacharacters are the *shell's* job

`data` travels JSON body → Python → JSON bridge body → JS `String(data)` →
`pty.write(data)` → ConPTY → the shell's line editor. Because JSON carries **data,
not code**, the dangerous characters `& | ^ < > "` survive every intermediate layer
untouched and are interpreted **only by the final shell** — which is what you want.
This typed-endpoint path is strictly safer than the old "build a `codeApiTerm.write(…)`
JavaScript string and POST it to `/exec`" pattern, which layered a JS parse and a
`cmd.exe` re-parse on top.

Caveats that remain Windows-specific:

- **Enter = CR (`\r`)**, appended when `enter:true`. Both cmd and PowerShell accept a
  bare CR as submit inside a ConPTY.
- **Encoding**: node-pty hands back UTF-8 strings; a program that emits legacy OEM
  code-page bytes (cp437/850) can still produce mojibake for non-ASCII. Prefer
  UTF-8-aware output (`chcp 65001`, or PowerShell's default UTF-8 rendering).
- **Long/awkward payloads**: if a command contains newlines or shell-hostile bytes,
  base64-encode it and decode in the shell (`powershell -EncodedCommand`, or
  `certutil`/`[Convert]::FromBase64String`) rather than fighting the line editor.

---

## 5. Liveness and Windows exit codes

`onExit` (node-pty) is the source of truth: it sets `alive:false` and the integer
`exitCode`. `reconcileAlive` is a **fallback only** — `process.kill(pid, 0)` (libuv
emulates it via `OpenProcess`) flips `alive:false` on `ESRCH` if an exit event was
missed, but never overwrites a recorded `exitCode`. So `alive:false` can appear one
poll *before* `exitCode` is populated — **wait for both**.

Windows exit codes you will actually see (they are NTSTATUS-shaped when abnormal):

| Code (dec) | Code (hex / meaning) | Cause |
|---|---|---|
| `0` | clean exit | success |
| `1` | generic failure | command returned nonzero |
| `9009` | `ERROR_...`/cmd "not recognized" | exe not found on PATH (classic: the **Windows Store `python` alias stub**) |
| `-1073741510` | `0xC000013A` STATUS_CONTROL_C_EXIT | the process was Ctrl-C / console-terminated |
| `-1073741502` | `0xC0000142` STATUS_DLL_INIT_FAILED | a DLL failed to initialize at startup |

The historical dead-PTY sweep observed large negative NTSTATUS exits like these on
terminals that `list()` still reported — the evidence that drove the additive
`alive`/`exitCode` fields. (Note: earlier notes mislabeled `-1073741510` as
`0xC0000142`; the correct mapping is above — `-1073741510` is Ctrl-C-exit.)

---

## 6. Ports and binding on Windows

`PortAllocator.resolve` ([port_allocator.py](../python/codeapi_vscode/session/port_allocator.py))
binds `127.0.0.1` and **refuses the legacy window band 3637–3646** outright. The JS
adapter first walks upward from 47337 (`pickFreePort`, unbounded) and hands the chosen
port to the runtime as `--port`; the Python allocator tries that preferred port, then
falls back across its reserved band `47337–47346`. Discovery scans `47337–47400`.
Windows specifics:

- **No `SO_REUSEADDR`.** The bind probe fails if the port is in `TIME_WAIT` or held —
  which is the intended "is it actually free" test, but means a just-crashed runtime's
  port may be briefly unbindable and the allocator will step up.
- **Windows excluded/reserved port ranges are invisible occupants.** Hyper-V, WSL2,
  Docker, and dynamic-port reservations can make a port in the band unbindable *with
  nothing listening on it*. Inspect with
  `netsh interface ipv4 show excludedportrange protocol=tcp`. The allocator just skips
  to the next free port — which is why the *effective* port drifts and why you must
  discover by identity, never assume 47337.
- **Loopback only.** Nothing here is reachable off-box; see §7.

Inspect what is actually listening:

```powershell
Get-NetTCPConnection -State Listen |
  Where-Object { $_.LocalPort -ge 47337 -and $_.LocalPort -le 47400 } |
  Select-Object LocalPort, OwningProcess
```

---

## 7. Privilege & security implications on Windows

- **`/exec` + `/term/create` = local code execution as the extension-host user.**
  `/exec` runs arbitrary JS in `Code.exe`; `/term/create` spawns a real shell. Treat
  the loopback port as a live local shell.
- **The terminal's integrity level is inherited from VS Code.** On this host an
  agent-launched VS Code runs under the elevated `Admin` account, so its CODEAPI
  shells are elevated and use the Admin PATH — *not* the interactive `dimas`
  environment. A user-launched VS Code yields `dimas`-level shells. Know which one you
  are driving; it changes both privilege and which `git`/`python` you get.
- **The bridge is the real trust boundary, and it is guarded.** Every `/vscode/*`
  call must carry `X-CODEAPI-BRIDGE-TOKEN` (a per-launch `crypto.randomBytes(32)`),
  checked *before* any body read or `vscode.*`/`node-pty` call; a mismatch is
  `403 forbidden`. Loopback is necessary but not sufficient — the token is what stops
  another local process from driving the host through the bridge.
- **Keep it loopback.** Do not tunnel or LAN-expose `47337+` without adding auth,
  origin/endpoint allowlists, and audit logging (see [SECURITY.md](SECURITY.md)).
- **Captured buffers are sensitive.** `GET /term/:id/read` returns whatever scrolled
  past — tokens, paths, secrets. Treat the buffer as confidential and reap PTYs you
  are done with.

---

## 8. Windows-specific failure modes & diagnostics

| Symptom | Root cause on Windows | Fix |
|---|---|---|
| runtime never healthy; log shows `[runtime exited: 9009]` | `python` resolved to the **Store app-execution-alias stub**, not a real interpreter (GUI host PATH ≠ shell PATH) | set `codeapi.pythonPath` / `CODEAPI_PYTHON` to the real `python.exe`, reload |
| PyInstaller build `WinError 5 (Access denied)` on `codeapi-runtime.exe` | a **running runtime holds the repo-bin exe open** (Windows locks the image of a running process) | stop the runtime using that exact `bin\codeapi-runtime\codeapi-runtime.exe`, then rebuild |
| dev-host launch opens an ordinary window, adapter never activates | **VS Code instance reuse** — `code --extensionDevelopmentPath` handed to a running instance | launch with `--new-window` + a distinct `--user-data-dir`; confirm the `[Extension Development Host]` title bar |
| garbage `UsersdimasApp…` dir created in the repo | launching `code` via the **Bash tool strips backslashes** | launch Windows GUIs via PowerShell, or use forward-slash/quoted paths |
| `/term/*` → 404 on 3637–3646 | you hit the **legacy** bundle, which has no `/term/*` | target a `47337+` adapter runtime |
| adapter-backed routes → 503 | runtime is up but **standalone** (no bridge) — e.g. `python -m codeapi_vscode.runtime` run bare | drive it inside a dev host / installed VSIX so the adapter bridge exists |
| non-ASCII output looks like mojibake | console **OEM code page** vs UTF-8 | `chcp 65001` / prefer UTF-8-emitting commands |

The self-diagnosing log for all of the above is the **CODEAPI Runtime** output
channel, mirrored to disk at
`<user-data-dir>\logs\<ts>\window1\exthost\output_logging_*\<n>-CODEAPI Runtime.log`
(readable without focusing the GUI). It records the exact spawn line
(`[starting runtime: <exe> <args> (cwd=…)]`), health, and any spawn error.

---

## 9. Lifecycle & cleanup summary (Windows)

| Action | Python runtime | Bridge (adapter) | node-pty terminals |
|---|---|---|---|
| `activate()` | spawned on a fresh `47337+` port | started on an ephemeral loopback port; token minted | none yet |
| `codeapi.restartRuntime` | **stopped + re-spawned on a new port** | **unchanged** (reused) | **survive** (same registry) — reachable via the new port |
| window reload / `deactivate()` | killed | closed | **all killed** (`pty.kill()` loop) |
| `DELETE /term/:id` | — | — | that one killed + disposed + removed |
| VS Code window closed | killed by host teardown | closed | all killed |

The asymmetry in row 2 is the operational crux on Windows: a runtime restart is
**non-destructive to terminals** but **rotates the public port**, so any client must
be prepared to re-discover by `codeapiWindowId` rather than hold a port.

---

## 10. Minimal Windows interaction recipes

`BASE` is hardcoded **only for brevity** — it assumes you already selected the target
window by identity. Port is not identity; never target "whichever port answers first"
(see [DISCOVERY_AND_TERMINAL_ROUTING.md](DISCOVERY_AND_TERMINAL_ROUTING.md)).

```bash
# PowerShell terminal, run one command, read past the VT noise, reap it
# BASE = base_url of a window you SELECTED by codeapiWindowId, not "first healthy port"
BASE=http://127.0.0.1:47337
ID=$(curl -sX POST $BASE/term/create -H 'Content-Type: application/json' \
        -d '{"shell":"powershell.exe","show":true}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -sX POST $BASE/term/$ID/write -H 'Content-Type: application/json' \
     -d '{"data":"git status --short --branch","enter":true}'
sleep 1
curl -s "$BASE/term/$ID/read?tail=4000"          # find your output after the echo
curl -sX POST $BASE/term/$ID/write -H 'Content-Type: application/json' \
     -d '{"data":"exit","enter":true}'
# poll until alive:false AND integer exitCode, THEN:
curl -sX DELETE $BASE/term/$ID
```

For window selection before you route (when several runtimes are alive), see
[DISCOVERY_AND_TERMINAL_ROUTING.md](DISCOVERY_AND_TERMINAL_ROUTING.md) and use
`select_by_identity(sessions, "<codeapiWindowId>", require_terminals=True)`.
