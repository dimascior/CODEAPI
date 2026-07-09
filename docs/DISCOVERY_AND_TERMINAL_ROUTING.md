# Window/Session Discovery & Terminal Routing

How a Python caller finds the running CODEAPI VS Code windows, picks the right one,
and drives an adapter-owned terminal in it. Implemented in
[`python/codeapi_vscode/discovery.py`](../python/codeapi_vscode/discovery.py); live
demo in [`python/tools/discover_and_route.py`](../python/tools/discover_and_route.py).
Governed by [CODEAPI_RUNTIME_ARCHITECTURE.md](CODEAPI_RUNTIME_ARCHITECTURE.md) (§4 wire
rules, §5 terminal liveness) and [TERM_CONTRACT.md](TERM_CONTRACT.md). For the full
worked example see [OPERATIONAL_GUIDE.md](OPERATIONAL_GUIDE.md); for the endpoint→logic
map and layer model see [FILE_FLOW.md](FILE_FLOW.md).

## Why `/health` is not identity

After the Phase-14 cutover several CODEAPI runtimes can be alive at once:

- the **legacy** sourceless bundle in the VS Code window band **3637–3646**;
- one or more **adapter/Python** runtimes answering inside the **discovery scan window
  47337–47400** (root dev host, installed VSIX, isolated test runtimes). The runtime does
  **not** hard-bind that whole range — it binds the *next free* port at/after 47337 (the
  allocator's reserved band 47337–47346), so ports drift. **47337–47400 is the window
  discovery scans**, not a fixed runtime bind range.

A stale or duplicate runtime still answers `GET /health 200`. Selecting a window
therefore requires `GET /session` identity — never health alone.

## Window identity: what to target

`/session` carries several identity signals with different durability. Prefer the most
durable that is present (selection precedence):

1. **`codeapiWindowId`** — a UUID the adapter persists in `context.workspaceState`
   (`codeapi.windowId`), created on first activation and reused on every later reload for
   that workspace state. **The durable target identity** — it survives runtime restarts and
   extension reloads, so pin to it when you can.
2. **`vscodeSessionId`** — `vscode.env.sessionId`; **session-scoped** — stable within one VS
   Code session, rotates on reload/restart as VS Code defines it.
3. **`extensionHostPid`** — the JS adapter's `process.pid`; **process-correlation evidence**
   (tie a runtime to a specific extension-host process). It is **not** the Python `pid` —
   `/session.pid` is the runtime process, `extensionHostPid` is its parent extension host, and
   the two differ.
4. **`workspaceUri`** — **human-facing but ambiguous**: two VS Code windows open on the same
   folder share it, so it cannot disambiguate windows on its own.
5. **`serverPort`** — last resort; drifts as runtimes bind the next free port.

The legacy `windowId` placeholder stays `0` in the runtime and is **not** used for identity.
`show:true` on `/term/create` surfaces the adapter-owned pty as the VS Code terminal
`codeapi:<id>` in the selected window — a **physical, human-visible confirmation** that you
targeted the right window (the terminal literally appears there).

## Discovery

```python
from codeapi_vscode.discovery import discover_sessions, select_session, TerminalRouter

sessions = discover_sessions()          # scans 3637-3646 + 47337-47400
```

`discover_sessions(ranges=DEFAULT_RANGES, timeout=1.0, probe_terminals=True)` returns a
list of `DiscoveredSession`. For each responder it reads `/health` + `/session` and probes
`GET /term/list`, then fills:

| field | meaning |
|---|---|
| `port` | the port it answered on |
| `server_port` | `/session.serverPort` (should equal `port`; otherwise `port-mismatch`) |
| `workspace_uri`, `pid`, `window_id`, `session_id` | `/session` identity |
| `codeapi_window_id`, `vscode_session_id`, `extension_host_pid` | adapter-pinned identity (additive; see "Window identity") |
| `terminal_routes` | `live` (`/term/list` 200) · `unavailable` (503) · `absent` (404/none) |
| `runtime_type` | `adapter` · `legacy` · `unknown` (see below) |
| `flags` | observable problems (see below) |
| `healthy` | `/health` 200 + `status:"ok"` |

### Classification (`classify_runtime`, pure/testable)

- **adapter** — `/term/*` present (`live`/`unavailable`), OR port in `47337–47400`.
  The public `/term/*` routes are a Python-runtime feature the legacy bundle never had,
  so their presence is the strongest signal.
- **legacy** — port in `3637–3646` and `/term/*` `absent`.
- **unknown** — anything else.

### Flags (`compute_flags` + `mark_duplicates`, pure/testable)

- `unhealthy` — `/health` not `200 ok`.
- `no-session` — `/session` not a dict.
- `port-mismatch` — `serverPort` ≠ answering port (phantom/misconfigured — do not target).
- `no-workspace` — no `workspaceUri`.
- `no-window-id` — legacy `windowId` 0/absent (kept for continuity; not used for identity).
- `no-codeapi-window-id` — an **adapter** runtime that did not report `codeapiWindowId`
  (pre-identity build, or launched without the new env) — the durable-identity gap to act on.
- `no-terminals` — adapter route exists but the bridge is unreachable (`503`) — a
  leftover standalone runtime that cannot own a terminal.
- `duplicate-workspace` — more than one responder shares a `workspaceUri`.

`DiscoveredSession.stale` (unhealthy / port-mismatch / no-session) and
`.terminals_usable` (`terminal_routes == "live"`) are convenience booleans.

## Selecting a target window

**Prefer identity, not a port or workspace.** A port identifies a *server*, not a
*window*, and `workspaceUri` is shared when one folder is open in several windows.
Resolve a single opaque token by precedence
`codeapiWindowId > vscodeSessionId > extensionHostPid > workspaceUri > serverPort`:

```python
target = select_by_identity(sessions, "<codeapiWindowId>", require_terminals=True)
```

`select_by_identity` tries the token as each identity in that order; the durable
`codeapiWindowId` wins over an ambiguous `workspaceUri` collision. For explicit, ANDed
criteria use `select_session(...)` — it filters healthy sessions by any of `port`
(matches answering **or** server port), `session_id` (prefix), `workspace` (substring),
`runtime_type`, the three identity fields (`codeapi_window_id` / `vscode_session_id` /
`extension_host_pid`), and `require_terminals`. Both raise `ValueError` if nothing matches.

**Workspace/port are a fallback only, marked unsafe when ambiguous.** If two responders
share a `workspaceUri` (both flagged `duplicate-workspace`), selecting by workspace
returns the **first** match — which may be the wrong window; pin `codeapiWindowId`
instead. **Only an `adapter` runtime with `terminal_routes == "live"` can own a
terminal** — legacy runtimes and `no-terminals` adapters are not routable via `/term/*`.

## Routing a terminal to the selected window

`TerminalRouter(base_url)` drives the **public `/term/*` routes** (Phase 13D), not the
legacy `codeApiTerm`-over-`/exec` path:

```python
router = TerminalRouter(target.base_url)
result = router.run_and_reap(shell="powershell.exe")   # full create->…->reap proof
```

`run_and_reap` performs: `POST /term/create` → `POST /term/:id/write` `echo <marker>` →
`GET /term/:id/read` (marker back) → write `exit` → **`wait_exit`** → `DELETE /term/:id` →
`GET /term/:id/read` expecting `404`. It returns `{marker, terminal, steps{…}, exit_code,
dead_record, ok}`.

**Liveness rule (§5):** `wait_exit` polls `GET /term/list` until the record shows
`alive:false` **and** an integer `exitCode`. Liveness is **never** inferred from `DELETE`
(which reports `killed:false` when the process already exited). node-pty's `onExit` is the
source of truth; the adapter's process-existence fallback can flip `alive:false` a beat
before `exitCode` is set, so we wait for both.

## CLI

```
set PYTHONPATH=python
python python/tools/discover_and_route.py                 # table + auto-route (adapter w/ terminals)
python python/tools/discover_and_route.py --no-route      # discovery table only
python python/tools/discover_and_route.py --port 47339
python python/tools/discover_and_route.py --workspace CODEAPI --runtime-type adapter
```

## Tests

`python python/tests/test_discovery.py` — network-free unit tests for
`classify_runtime`, `compute_flags`, `mark_duplicates`, and `select_session` (21/21).

## Live evidence

**Discovery (2026-07-07)** — `discover_and_route.py` found **5 responders** across both
ranges and classified them correctly (legacy 404s `/term/list`; adapter serves it):

| port | serverPort | type | terms | pid | windowId | session | flags |
|---|---|---|---|---|---|---|---|
| 3637 | 3637 | legacy | absent | 18772 | 311398 | 361ff651 | duplicate-workspace |
| 3638 | 3638 | legacy | absent | 29300 | 869097 | 4ba1a18d | duplicate-workspace |
| 47337 | 47337 | adapter | live | 26192 | 0 | 995a45f8 | no-window-id, duplicate-workspace |
| 47338 | 47338 | adapter | live | 38396 | 0 | 7e2ba965 | no-window-id, duplicate-workspace |
| 47339 | 47339 | adapter | live | 38636 | 0 | d7e53a83 | no-window-id, duplicate-workspace |

All five share the CODEAPI workspace (→ `duplicate-workspace`); the three adapter runtimes
are, respectively, the stale 14C dev-host runtime (47337), the root-cutover dev host (47338),
and the installed-VSIX runtime (47339).

**Terminal routing against two distinct live sessions (2026-07-07), both `6/6 PASS`:**

- **Auto-selected** first adapter-with-terminals → port **47337** (session `995a45f8`): marker
  `CODEAPI_ROUTE_20a762e3`, terminal `tmran7am81` pid 40292 → dead record
  `{alive:false, exitCode:0}` observed via `/term/list` **before** DELETE → read-after-delete 404.
- **Explicitly selected** `--port 47339` (session `d7e53a83`, installed VSIX): marker
  `CODEAPI_ROUTE_63fb9cf8`, terminal `tmrancrfx1` pid 39192 → dead record
  `{alive:false, exitCode:0}` before DELETE → read-after-delete 404.

Both proved liveness via `alive:false` + integer `exitCode` from `/term/list`, **not** via
DELETE (which returned `killed:false` because the process had already exited). Test PTYs were
cleaned up. Unit tests: `test_discovery.py` **33/33**.

**Adapter-pinned identity (2026-07-07)** — after adding the additive `/session` fields and
rebuilding the packaged runtime, a fresh root dev host on **47338** reported:
`codeapiWindowId=632d86a5-…`, `vscodeSessionId=58fda6fa-…`, `extensionHostPid=3344`,
`pyPid=30540` — i.e. **pyPid ≠ extensionHostPid** (distinct processes). Discovery flagged the
older adapter runtimes launched *before* the feature (47337, 47339) with `no-codeapi-window-id`
and left the new one unflagged. Terminal routing to 47338 passed **6/6** (visible `show:true`
PTY). **Reload durability:** triggering `codeapi.restartRuntime` moved the runtime to a new port
(47340) with a new `pyPid` (5908) but the **same `codeapiWindowId` `632d86a5-…`** — proving the
durable identity survives runtime reload (persisted in `workspaceState`) while the pid rotates.

**Release-hygiene validation (2026-07-07, cleaned installed VSIX)** — on branch
`release/terminal-runtime-cleanup`, the trimmed `codeapi-0.1.3.vsix` (353 files) was
installed into an isolated env and launched with no `--extensionDevelopmentPath`. Its
packaged runtime came up on **47337** (`pyPid=31184`, `extensionHostPid=39500`,
`codeapiWindowId=61ae336c-…`) from the *installed* dir exe. Discovery told it apart from
a stale pre-identity runtime on 47339 (flagged `no-codeapi-window-id`);
`select_by_identity("61ae336c-…", require_terminals=True)` resolved to 47337. A visible
`codeapi:tmrasdq3r0` terminal (confirmed in `vscode.window.terminals`) ran
`Write-Output "CODEAPI_VISIBLE_TARGET_d5bff6be"; Get-Location; git status --short --branch`;
`/term/read` returned the marker, the CODEAPI cwd, and `## release/terminal-runtime-cleanup`;
liveness `alive:false`/`exitCode:0` came from `/term/list` before DELETE (which reported
`killed:false`), read-after-delete `404`. Full write-up in
[RELEASE_HYGIENE.md](RELEASE_HYGIENE.md).
