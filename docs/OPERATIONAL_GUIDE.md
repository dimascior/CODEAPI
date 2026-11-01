# CODEAPI Operational Guide

One complete, current example: from a clean clone to driving a visible terminal in a
selected VS Code window and reaping it. This is the canonical end-to-end flow.

Prereqs and boundaries: [INSTALL_AND_BUILD.md](INSTALL_AND_BUILD.md). How it works:
[FILE_FLOW.md](FILE_FLOW.md). Troubleshooting: [DEBUGGING.md](DEBUGGING.md).

---

## The one flow

### 1–2. Install / build / package

```bash
git clone git@github.com:dimascior/CODEAPI.git CODEAPI && cd CODEAPI
npm install
npm run vscode:prepublish     # builds bin/codeapi-runtime/codeapi-runtime.exe
npm run vsce:package          # -> codeapi-<version>.vsix
```

### 3. Launch the extension

Install the VSIX into a profile and open a workspace, **or** press F5
(**Run Extension**). On startup the adapter spawns the packaged runtime and it binds a
port at/after 47337. Confirm:

```bash
curl -s http://127.0.0.1:47337/health     # {"status":"ok"}
```

### 4. Call discovery

```python
# set PYTHONPATH=python
from codeapi_vscode.discovery import discover_sessions, select_by_identity, TerminalRouter

sessions = discover_sessions()            # scans 3637-3646 + 47337-47400
for s in sessions:
    # print the FULL identity, not just the port — port is not identity
    print(s.codeapi_window_id, s.vscode_session_id, s.extension_host_pid,
          s.server_port, s.workspace_uri, "|", s.runtime_type, s.terminal_routes, s.flags)
```

Discovery reads `/health` + `/session` on every responder, probes `GET /term/list`, and
classifies each as `adapter` / `legacy` / `unknown`. Only an `adapter` runtime with
`terminal_routes == "live"` can own a terminal. **If two rows share a `workspace_uri`
(both flagged `duplicate-workspace`), you must disambiguate by `codeapiWindowId` in the
next step — never by port or workspace.**

### 5. Select the session by `codeapiWindowId`

```python
target = select_by_identity(sessions, "<codeapiWindowId>", require_terminals=True)
base = target.base_url                    # e.g. http://127.0.0.1:47337
```

`codeapiWindowId` is the **durable** identity (a UUID the adapter persists in
`context.workspaceState`; survives runtime restarts). Precedence:
`codeapiWindowId` > `vscodeSessionId` > `extensionHostPid` > `workspaceUri` > `serverPort`.

### 6. Create a visible terminal

```bash
curl -sX POST $BASE/term/create -H "Content-Type: application/json" \
     -d '{"shell":"powershell.exe","show":true}'
# -> {"id":"tmr…","shell":"powershell.exe","cwd":"…","pid":NNNN}
```

`show:true` surfaces the adapter-owned pty as the VS Code terminal `codeapi:<id>` in the
selected window — a physical, human-visible confirmation you targeted the right window.

### 7. Send a command

```bash
curl -sX POST $BASE/term/tmr…/write -H "Content-Type: application/json" \
     -d '{"data":"git status --short --branch","enter":true}'
# -> {"id":"tmr…","wrote":24}      (bytes accepted — NOT success)
```

### 8. Read output

```bash
curl -s $BASE/term/tmr…/read
# -> {"id":"tmr…","bytes":N,"buffer":"…raw ANSI…"}
```

The buffer is the raw screen scrollback (ANSI included). Parse the region after your
command's echo.

### 9. Observe exit state

```bash
curl -sX POST $BASE/term/tmr…/write -H "Content-Type: application/json" \
     -d '{"data":"exit","enter":true}'
# poll:
curl -s $BASE/term/list
# wait until the record shows  "alive":false  AND  "exitCode":<int>
```

Liveness comes from `/term/list` (`alive:false` + integer `exitCode`), **never** from
`DELETE`.

### 10. Delete the terminal

```bash
curl -sX DELETE $BASE/term/tmr…
# -> {"id":"tmr…","killed":false}   (already exited; killed:false is EXPECTED here)
curl -s $BASE/term/tmr…/read        # -> 404 after delete
```

### 11. Interpret success / failure

| Observation | Meaning |
|---|---|
| `create` returns `{id,pid}` and `codeapi:<id>` appears in the window | terminal is real + targeted the right window |
| `write` returns `{wrote:N}` | N bytes typed — says nothing about the command's result |
| `read` buffer shows the expected output | the command actually ran (this is the success signal) |
| `list` shows `alive:false` + integer `exitCode` | the process exited; `exitCode 0` = clean |
| `DELETE` returns `killed:false` **after** an observed exit | expected — the process was already gone |
| `read` after delete → `404` | cleanup complete |
| adapter-backed route → `503` | no live bridge (standalone runtime, or wrong window) |
| `/term/*` → `404` on 3637–3646 | you hit the legacy bundle, not the Python runtime |

---

## Non-terminal calls (quick reference)

```bash
# run JS in the extension host (raw code as the body; value returned unwrapped)
curl -sX POST $BASE/exec -H "Content-Type: text/plain" \
     --data-binary 'return vscode.workspace.workspaceFolders.map(f => f.name)'

# run a VS Code command (wrapped result)
curl -sX POST $BASE/command/workbench.action.files.save -H "Content-Type: application/json" -d '{}'
# -> {"success":true}

# active editor (object or null)
curl -s $BASE/editor/active
```

The endpoint→code map, wire shapes, and terminal contract are in
[FILE_FLOW.md](FILE_FLOW.md), [WIRE_CONTRACT.md](WIRE_CONTRACT.md), and
[TERM_CONTRACT.md](TERM_CONTRACT.md).
