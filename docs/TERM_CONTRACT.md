# CODEAPI Terminal (`/term/*`) Contract

The typed terminal surface and its **liveness rule** — live and adapter-backed. The
public `/term/*` routes delegate to the JS adapter's `node-pty`; liveness comes from the
adapter's process state, never inferred by the Python service.

Companions: [ADAPTER_BRIDGE_CONTRACT.md](ADAPTER_BRIDGE_CONTRACT.md) (the bridge envelope
+ token; `/term/*` are adapter-backed exactly like the other public routes),
[WIRE_CONTRACT.md](WIRE_CONTRACT.md) (`/term/*` `200` shapes),
[OPERATIONAL_GUIDE.md](OPERATIONAL_GUIDE.md) (a worked terminal example),
[FILE_FLOW.md](FILE_FLOW.md).

---

## 1. The liveness rule (everything else follows)

A PTY's presence in the registry says nothing about whether its process is alive. In
practice PTYs exit on their own (e.g. Windows exit code `0xC0000142`) while the buffer,
`wrote` count, and list membership all still look "normal." Therefore:

> **write success ≠ process alive.** `write` returning `{wrote:N}` means N bytes were
> accepted into the PTY input — not that a live shell consumed them or produced output.
>
> **buffer output ≠ process alive.** A dead PTY still holds its last buffer; `read`
> returning bytes proves history, not a running process.
>
> **list presence ≠ process alive.** `list` enumerates every tracked PTY, dead included.
>
> **Liveness comes from adapter-side process state** — never inferred by the Python
> service from `wrote`, buffer content, or list membership.

`/term/list` therefore carries **`alive`** and **`exitCode`** per entry, sourced from the
adapter's tracked node-pty process state (§4). Every other shape is the plain PTY return.

---

## 2. Placement (two tiers, one boundary)

| Tier | Surface | Owns |
|---|---|---|
| Public | `POST/GET/DELETE 127.0.0.1:CODEAPI_PY_PORT/term/*` (47337+) | routing + wire shaping (`server/term_routes.py`) |
| Bridge | `…/vscode/term/*` on the adapter bridge (ephemeral loopback) | the actual `node-pty` calls (`extension-adapter/extension.js`) |

Same rules as [ADAPTER_BRIDGE_CONTRACT.md](ADAPTER_BRIDGE_CONTRACT.md): loopback-only,
mandatory `X-CODEAPI-BRIDGE-TOKEN`, uniform `{ok:true,value?}` / `{error}` envelope,
`AdapterUnavailable → 503`, `AdapterError → 500`. **`node-pty` lives in the JS adapter**;
the Python service never spawns a PTY.

---

## 3. Route contract

| Public route | Bridge route | Request | Response `200` |
|---|---|---|---|
| `GET /term/list` | `GET /vscode/term/list` | — | `[ { "id":"t…", "pid":N, "bytes":N, "alive":bool, "exitCode":N\|null } ]` |
| `POST /term/create` | `POST /vscode/term/create` | `{ id?, cwd?, shell?, args?, cols?, rows?, maxBytes?, show? }` | `{ "id":"t…", "shell":"…", "cwd":"…", "pid":N }` |
| `POST /term/:id/write` | `POST /vscode/term/:id/write` | `{ data, enter? }` | `{ "id":"t…", "wrote":N }` |
| `GET /term/:id/read` | `GET /vscode/term/:id/read` | `?tail=N&clear=bool` | `{ "id":"t…", "bytes":N, "buffer":"<raw ANSI>" }` |
| `POST /term/:id/resize` | `POST /vscode/term/:id/resize` | `{ cols, rows }` | `{ "id":"t…", "cols":N, "rows":N }` |
| `DELETE /term/:id` | `DELETE /vscode/term/:id` | — | `{ "id":"t…", "killed":bool }` |

Unknown `:id` → `404 {"error":"Terminal <id> not found"}`. No adapter configured/reachable
→ `503 {"error":"vscode adapter not available"}`.

---

## 4. Per-route semantics

- **`GET /term/list`** — enumerate every adapter-owned PTY. `alive`/`exitCode` come from
  the mechanism below, not from `bytes` or buffer. Dead PTYs remain listed
  (`alive:false`, `exitCode:<n>`) until an explicit `DELETE` (no implicit reaping).
- **`POST /term/create`** — create a **visible, adapter-owned** PTY (a `node-pty`
  surfaced as the VS Code terminal `codeapi:<id>` when `show` is truthy). Adapter-owned is
  the point: VS Code's own terminals are not buffer-readable; these are. `maxBytes` caps
  the retained buffer ring. Returns `{id,shell,cwd,pid}`.
- **`POST /term/:id/write`** — write `data` (optionally append Enter). Returns `{wrote:N}`
  = **bytes accepted, not command success and not proof of life** (§1).
- **`GET /term/:id/read`** — return the retained **raw ANSI/VT** buffer; the client
  strips. `tail=N` returns the last N bytes; `clear=true` empties the captured buffer
  after reading. **Caveat:** `clear` also erases any exit marker, which is exactly why
  liveness is structural (§1), never buffer-derived.
- **`POST /term/:id/resize`** — resize only (`cols`,`rows`); no write, no read.
- **`DELETE /term/:id`** — **cleanup/removal.** It may kill a live PTY, but after `DELETE`
  the terminal is no longer listed. `{killed:true}` when a live process was terminated;
  `{killed:false}` if it was already dead (idempotent). **`DELETE` is NOT the
  liveness-observation mechanism** — a process that exits on its own is observed as
  `alive:false`/`exitCode:<n>` on `/term/list` **while still listed**, before any `DELETE`.

---

## 5. Liveness mechanism (adapter-side, normative)

The adapter is the single source of truth for `alive`/`exitCode`:

1. **Primary — `node-pty` exit event.** At create, the adapter registers
   `pty.onExit(({exitCode}) => { record exitCode; alive=false })`. Until it fires,
   `alive=true`, `exitCode=null`.
2. **Fallback probe — `process.kill(pid, 0)`.** A poll that sends signal 0; an `ESRCH`
   throw means the process is gone (covers a missed/racy exit event). Used to reconcile,
   never to override a recorded `exitCode`. It can flip `alive:false` a beat before
   `exitCode` is set, so consumers poll `/term/list` until **both** `alive:false` **and**
   an integer `exitCode` are present.
3. **Python passes through.** The runtime copies `alive`/`exitCode` from the bridge
   verbatim; it never computes liveness from `wrote`, `bytes`, or buffer text.

---

## 6. `create` defaults

Defaults are resolved by the vscode-free helper `resolveTermDefaults(opts, ctx)` in
`extension-adapter/bridge_core.js` (unit-tested in
`extension-adapter/tests/test_bridge_core.js`), shared by both the typed
`/vscode/term/create` handler and the `codeApiTerm.create()` facade. Explicit caller options
always override:

| option | default |
|---|---|
| `shell` (win32) | `process.env.COMSPEC \|\| "powershell.exe"` |
| `shell` (posix) | `process.env.SHELL \|\| "/bin/bash"` |
| `cols` | `120` |
| `rows` | `30` |
| `maxBytes` | `1e6` = **1000000** (a decimal MB, **not** the binary `1048576`) |
| `args` | `[]` |
| `cwd` | `opts.cwd \|\| <workspace folder fsPath> \|\| os.homedir()` |

The PTY is spawned with `env: process.env` — it inherits the extension host's environment
(so a terminal in a user-launched window carries that user's environment).

---

## 7. Liveness acceptance test

The exit must be observable on `/term/list` **before** cleanup. Verified in the suite and
in the operational runs:

1. `POST /term/create` a PTY → `/term/list` shows `alive:true`, `exitCode:null`.
2. Cause it to exit **without** deleting the registry entry (e.g. `write` an `exit`).
3. `/term/list` shows `alive:false` and `exitCode:<n>` (both present).
4. `DELETE /term/:id` → `{killed:false}` (already exited); the terminal is removed and a
   read-after-delete returns `404`.

`write` to a dead PTY (between 3 and 4) returns `{wrote:N}` and must not masquerade as
success or proof of life.
