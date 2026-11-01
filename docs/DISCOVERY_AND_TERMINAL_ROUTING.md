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

Identity deliberately splits two properties no single supported field provides
together — durability across reloads and uniqueness between simultaneous windows:

1. **`codeapiRuntimeId`** — a fresh UUID the adapter mints on every activation, never
   persisted. **The unique live-window target**: no shared storage, so no collision
   and no race. Rotates on window reload — rediscover after reloads.
2. **`vscodeSessionId`** — `vscode.env.sessionId`; a VS Code-provided session
   correlation signal, useful as a secondary target key. VS Code guarantees only
   that it identifies the current session and changes when the editor restarts —
   it is NOT the CODEAPI-owned uniqueness guarantee (that is `codeapiRuntimeId`).
3. **`codeapiWorkspaceId`** (legacy name **`codeapiWindowId`** — same value) — a UUID the
   adapter persists in `context.workspaceState` (`codeapi.windowId`). **Durable across
   reloads but per-WORKSPACE**: all windows on one workspace share it, so it groups
   windows; it does not uniquely target one. Discovery flags sharers
   (`duplicate-codeapi-window-id`) and selection by a colliding id raises.
   **Threat model — first-activation race:** two windows activating simultaneously on
   an id-less workspace can each observe "missing", mint different UUIDs, and race the
   persist; the stored value converges to the last write after reload (ids can differ
   now and converge later). The adapter awaits the persist, which narrows but cannot
   close this race — which is exactly why unique targeting belongs to
   `codeapiRuntimeId`, not this field.
4. **`extensionHostPid`** — the JS adapter's `process.pid`; **process-correlation
   evidence** (tie a runtime to a specific extension-host process). It is **not** the
   Python `pid` — `/session.pid` is the runtime process, `extensionHostPid` its parent.
5. **`workspaceUri`** — **human-facing but ambiguous**: two VS Code windows open on the
   same folder share it, so it cannot disambiguate windows on its own.
6. **`serverPort`** — transport handle; drifts as runtimes bind the next free port.

A useful compound display key is `<codeapiWorkspaceId>@<vscodeSessionId>` — unique for
the current session while keeping the durable workspace grouping visible.

The legacy `windowId` placeholder stays `0` in the runtime and is **not** used for identity.
`show:true` on `/term/create` surfaces the adapter-owned pty as the VS Code terminal
`codeapi:<id>` in the selected window — a **physical, human-visible confirmation** that you
targeted the right window (the terminal literally appears there).

## Discovery

```python
from codeapi_vscode.discovery import discover_sessions, select_session, TerminalRouter

sessions = discover_sessions()          # scans 3637-3646 + 47337-47400
```

**Allocation range == discovery range (one coherent policy).** Every allocator in the
stack — the JS adapter (`bridge_core.pickFreePort`) and the Python
`PortAllocator` — is bounded to the same `47337–47400` band discovery scans (64
simultaneous adapter runtimes). Band exhaustion **fails activation visibly** (error
notification + runtime log) instead of binding an out-of-band port that discovery
would never find, and the Python allocator refuses an out-of-band preferred port for
the same reason.

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
- `duplicate-codeapi-window-id` — more than one responder shares a `codeapiWindowId`
  (two windows on the same workspace share the per-workspace memento). The id is not a
  window identity while this flag is present — select by `vscodeSessionId` or port.

`DiscoveredSession.stale` (unhealthy / port-mismatch / no-session) and
`.terminals_usable` (`terminal_routes == "live"`) are convenience booleans.

## Selecting a target window

**Prefer identity, not a port or workspace.** A port identifies a *server*, not a
*window*, and `workspaceUri` is shared when one folder is open in several windows.
Resolve a single opaque token by precedence
`codeapiRuntimeId > vscodeSessionId > codeapiWindowId/codeapiWorkspaceId >
extensionHostPid > workspaceUri > serverPort`:

```python
target = select_by_identity(sessions, "<codeapiRuntimeId>", require_terminals=True)
```

Targeting rules in practice:

```
Current live window:            codeapiRuntimeId or vscodeSessionId
All windows of one workspace:   codeapiWorkspaceId (grouping; ambiguous as a target)
Transport:                      serverPort
Process correlation:            extensionHostPid
```

**Every selector is strict.** `select_by_identity` tries the token as each identity in
precedence order, and at whichever tier hits, **multiple matches raise `ValueError`**
(naming the candidate ports) — never a silent first-match. The same rule applies to
`select_session(...)`, which filters healthy sessions by ANDed criteria (`port`,
`session_id` prefix, `workspace` substring, `runtime_type`, `codeapi_window_id` /
`codeapi_workspace_id` / `codeapi_runtime_id` / `vscode_session_id` /
`extension_host_pid`, `require_terminals`) and raises if the result is not exactly one
session. Narrow ambiguous criteria with `codeapi_runtime_id`, `vscode_session_id`, or
`port`.

**Only an `adapter` runtime with `terminal_routes == "live"` can own a
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
