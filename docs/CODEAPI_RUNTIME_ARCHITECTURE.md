# CODEAPI Runtime Architecture — Controlling Document

**This is the controlling architecture artifact for CODEAPI.** This architecture is now
the **running system**: `package.json.main` = `./extension-adapter/extension.js` (the
adapter is wired in), the packaged Python runtime, and the public `/term/*` routes are all
live and validated. It is no longer a plan to implement phase by phase. Where a detail lives in a narrower doc, this
document links to it and states the final rule. Precedence: on any conflict, the
**wire rules (§4)** and **invariants (§8)** here win, then the per-surface contracts
([WIRE_CONTRACT.md](WIRE_CONTRACT.md), [ADAPTER_BRIDGE_CONTRACT.md](ADAPTER_BRIDGE_CONTRACT.md),
[TERM_CONTRACT.md](TERM_CONTRACT.md)). For the operational view — file entry points,
execution flow, endpoint→logic map, install/build, and a worked example — see
[FILE_FLOW.md](FILE_FLOW.md), [INSTALL_AND_BUILD.md](INSTALL_AND_BUILD.md),
[OPERATIONAL_GUIDE.md](OPERATIONAL_GUIDE.md), and [DEBUGGING.md](DEBUGGING.md).

---

## 1. Purpose

CODEAPI is being migrated **from a sourceless JavaScript runtime bundle** (a minified
extension with no `src/`, no `sourcesContent`) **to a Python-owned runtime service
that a minimal VS Code JavaScript adapter launches and supervises.**

Non-negotiable framing:

- **VS Code still requires a JavaScript extension entrypoint.** `package.json.main`
  must resolve to a JS module exporting `activate`/`deactivate`.
- **Python does NOT become the VS Code extension.** There is no Python extension-host
  contract. Saying "VS Code runs Python" is wrong.
- **The architecture is: minimal JS adapter + Python runtime.** The adapter is a thin,
  hand-written shim; the Python runtime is a child process it manages.
- **The JS adapter owns the irreducible extension-host operations** — everything that
  can only run inside the Node/Electron host with the `vscode` object injected.
- **The Python runtime owns the public CODEAPI service and wire-compatible response
  shaping** — the byte-for-byte contract external clients depend on.

Rationale for replacement is **maintainability** (a sourceless minified bundle), not a
functional failure of the current bridge (the retired runtime-replacement decision).

---

## 2. Layer model

### Layer 0 — VS Code extension host
**Owns:** extension `activate`/`deactivate`; the `vscode.*` API object; `node-pty`;
visible terminal integration; Python child-process launch + supervision.
**Forbidden:** public CODEAPI route shaping; long-term business/control-plane logic.

### Layer 1 — JS adapter bridge (`extension-adapter/extension.js`)
**Owns:** the internal `/vscode/*` bridge routes; mandatory `X-CODEAPI-BRIDGE-TOKEN`
enforcement; executing `vscode.*`; executing JS `eval` for `/vscode/exec`; executing
`node-pty` terminal operations; **adapter-side terminal liveness state**.
**Forbidden:** pretending to be the public runtime; binding in 3637–3646 as a CODEAPI
window; shaping public compatibility envelopes — the bridge speaks **only** the bridge
envelope `{ok:true,value?}` / `{error}`.

### Layer 2 — Python runtime service (`python/codeapi_vscode/`, `runtime.py` + `server/`)
**Owns:** the public CODEAPI HTTP routes — `/health`, `/session`, dynamic endpoints,
mesh, `/exec`, `/command/:id`, `/workspace/folders`, `/editor/active`,
`/window/showMessage`, `/apis`, and future `/term/*`; compact JSON serialization;
public wire compatibility; 503/500/404 public error shaping.
**Forbidden:** direct `vscode.*`; direct `node-pty`; binding in 3637–3646 before Phase
14; inferring terminal liveness from `wrote`/`read`/`list`.

### Layer 3 — Python client / control plane (`client.py`, `terminal_client.py`, CLI)
**Owns:** `python -m codeapi_vscode`; `CodeAPIClient`; `TermClient`; parity with `the seed CLI`
until Phase 15.
**Forbidden:** intentional CLI/help divergence before Phase 15; whitelisting parity
differences.

### Layer 4 — cutover and retirement
**Owns:** Phase 14 `package.json`/`main` cutover; live end-to-end wiring; `/apis` fixed
descriptor reconciliation; Phase 15 seed retirement and parity decommissioning.

### Request flow
```
external client
   │  public route (127.0.0.1:CODEAPI_PY_PORT, 47337+)
   ▼
Layer 2  Python runtime ──(pure route)──► answered in Python ──► client
   │
   │  adapter-backed / terminal route
   ▼
   vscode/adapter.py ──POST /vscode/* + X-CODEAPI-BRIDGE-TOKEN──►
Layer 1  JS adapter bridge (ephemeral loopback) ── runs vscode.* / eval / node-pty ──►
   └───────────────── {ok:true,value?} / {error} ─────────────► Layer 2 shapes ► client
```

---

## 3. Route ownership table

Legend — Owner: **Py** = Python runtime (Layer 2), **Adapter** = JS bridge (Layer 1).

### Pure Python routes (no bridge)
| Public route | Bridge route | Owner | Phase status | Remaining work |
|---|---|---|---|---|
| `GET /health` | — | Py | done (11) | — |
| `GET /session` | — | Py | done (11) | — |
| `POST /add-endpoint` | — | Py | done (11) | — |
| `GET <registered dynamic path>` | — | Py | done (11) | — |
| `POST /remove-endpoint` | — | Py | done (11) | — |
| `GET /mesh/peers` | — | Py | done (11) | mesh discovery inert (peers always `[]`) |
| `POST /mesh/broadcast/:endpoint` | — | Py | done (11) | inert until real peers |

### Adapter-backed public routes
| Public route | Bridge route | Owner | Phase status | Remaining work |
|---|---|---|---|---|
| `POST /exec` | `POST /vscode/exec` | Py→Adapter | done (12C/E), diff 12F | — |
| `POST /command/:commandId` | `POST /vscode/command` | Py→Adapter | done (12C/E), diff 12F | — |
| `GET /workspace/folders` | `GET /vscode/workspace/folders` | Py→Adapter | done (12C/E), diff 12F | — |
| `GET /editor/active` | `GET /vscode/editor/active` | Py→Adapter | done (12C/E), diff 12F | — |
| `POST /window/showMessage` | `POST /vscode/window/message` | Py→Adapter | done (12C/E) | blocking; not live-diffed (UI) |
| `GET /apis` | `GET /vscode/apis` | Py→Adapter | routes done (12E) | **`/apis` descriptor divergence** — 12C emits command-registry only (Package C) |

### Terminal public routes
| Public route | Bridge route | Owner | Phase status | Remaining work |
|---|---|---|---|---|
| `GET /term/list` | `GET /vscode/term/list` | Py→Adapter | **done (13B/13C/13D)** | — (live) |
| `POST /term/create` | `POST /vscode/term/create` | Py→Adapter | done (13B/13C/13D) | — (live) |
| `POST /term/:id/write` | `POST /vscode/term/:id/write` | Py→Adapter | done (13B/13C/13D) | — (live) |
| `GET /term/:id/read` | `GET /vscode/term/:id/read` | Py→Adapter | done (13B/13C/13D) | — (live) |
| `POST /term/:id/resize` | `POST /vscode/term/:id/resize` | Py→Adapter | done (13B/13C/13D) | — (live) |
| `DELETE /term/:id` | `DELETE /vscode/term/:id` | Py→Adapter | done (13B/13C/13D) | — (live) |

### Bridge routes (Layer 1, internal only)
| Bridge route | Owner | Phase status | Remaining work |
|---|---|---|---|
| `POST /vscode/exec` | Adapter | done (12C) | — |
| `POST /vscode/command` | Adapter | done (12C) | — |
| `GET /vscode/workspace/folders` | Adapter | done (12C) | — |
| `GET /vscode/editor/active` | Adapter | done (12C) | — |
| `POST /vscode/window/message` | Adapter | done (12C) | — |
| `GET /vscode/apis` | Adapter | done (12C) | fixed descriptor rebuild (Package C) |
| `/vscode/term/*` (6 routes) | Adapter | done (13B) | live liveness proof (Package B / 13E) |

The public `/term/*` routes are **live (13D, committed and validated)**: they return
**503** when the adapter is unavailable (§4) and **`404 {"error":"Terminal <id> not found"}`**
for unknown ids.

---

## 4. Wire-shaping rules (single source of truth)

- `/exec` public response is **unwrapped**.
- `/exec` `return undefined` → **`200` with empty body** (Content-Length 0).
- `/exec` `return null` → **`200` with body `null`**.
- `/command/:id` is **wrapped** as `{"success":true,"result":<value>}`.
- `/command/:id` **omits `result`** when the command result is `undefined`.
- `/window/showMessage` **blocks** until dismissal and returns `{"success":true}`
  (selected item not surfaced).
- Adapter-backed public routes return **`503 {"error":"vscode adapter not available"}`**
  when no adapter is configured or reachable.
- Unknown public routes return **404**.
- `/term/*` routes return **503** when the adapter is unavailable — not 404.
- Unknown terminal ids return **`404 {"error":"Terminal <id> not found"}`**.
- `NO_BODY` is **distinct from `None`**: `NO_BODY` ⇒ empty body; `None` ⇒ JSON `null`.
- **Compact JSON is mandatory** (`separators=(",", ":")`); **do not sort keys**;
  preserve insertion order.

Provenance for these rules: [WIRE_CONTRACT.md](WIRE_CONTRACT.md),
the retired Phase 12F differential (7/7 byte-exact), gate
`phase12a1-exec-undef-011` (the `/exec` undefined bytes).

---

## 5. Terminal liveness architecture

Source: [TERM_CONTRACT.md](TERM_CONTRACT.md). Final architecture:

- **Terminal liveness is owned by the JS adapter (Layer 1).**
- **`node-pty` `onExit` is the source of truth** for `alive` and `exitCode`.
- **`process.kill(pid, 0)` is fallback reconciliation only** (flips a missed `alive`;
  never overrides a recorded `exitCode`).
- **Python passes `alive` and `exitCode` through verbatim.**
- **Python must NOT infer liveness** from `wrote`, `bytes`, `buffer`, or list membership.
- `write` success means **bytes accepted**, not process alive.
- `read` returns the **raw buffer**, not liveness.
- **Dead PTYs remain listed until cleanup** (observable as `alive:false`).
- **`DELETE` is cleanup/removal, not liveness observation** — a self-exited process is
  observed `alive:false`/`exitCode:<n>` on `/term/list` *before* any `DELETE`.

Evidence base: the PTY sweep (gates `phase12a-pty-drive-003` / `phase12a-pty-dive-004`)
found 7–8 dead PTYs (`exitCode 0xC0000142`) that `list` could not distinguish, writes
that no-op'd on dead PTYs, and a `read(clear)` that erased the exit marker.

---

## 6. Current commit state

Branch `phase-10-python-client` — **local, UNPUSHED**. Committed phase anchors:

| Commit | Phase |
|---|---|
| `c3b7c61` | Phase 10 — Python client/control-plane port |
| `300bc5a` | Python runtime bootstrap + wire contract |
| `a880c39` | Phase 11 — Python pure-route runtime |
| `7f6ca77` | Phase 12A — adapter-backed shape probes |
| `bf217f6` | Phase 12B — adapter bridge contract |
| `efab48e` | `/exec undefined` wire behavior |
| `4514d0b` | Phase 12C — JS adapter bridge handlers |
| `87e364a` | Phase 12D — Python adapter client |
| `e269cbe` | Phase 12E — adapter-backed public routes |
| `6c0936d` | Phase 12F — adapter-backed differential |
| `c316685` | Phase 13A — typed terminal contract and liveness |
| `ad3a620` | Phase 13B — JS bridge terminal handlers |
| `a6448d7` | Phase 13C — Python adapter terminal client |
| *(this doc)* | controlling runtime architecture |

Tests green as of 13C: term 16/16, adapter 21/21, pure 23/23, routes 17/17, cli 4/4.

---

## 7. Implementation packages (status)

### Package A — Phase 13D: public terminal routes — **DONE**
`python/codeapi_vscode/server/term_routes.py` implemented and registered in
`api_registry.register_all`; public `/term/*` returns **503 when no adapter exists** and
**404** for unknown ids; fake-adapter public-route tests cover byte-shaping +
`alive`/`exitCode` passthrough.

### Package B — Phase 13E: terminal differential + liveness proof — **DONE**
Typed `/term/*` proven against real node-pty: create → `alive:true` → process exits
**without `DELETE`** → `alive:false` + integer `exitCode` (observed while still listed) →
`DELETE` → unknown-id 404. Re-proven live end-to-end in Phase 14 and the release-hygiene run.

### Package C — Phase 14: cutover — **DONE (2026-07-07)**
`/apis` divergence characterized/reconciled; live end-to-end JS adapter + Python runtime
validated (real bridge, real `vscode`, real node-pty); packaged-runtime launch validated on
Windows; **`package.json.main` cut over to `./extension-adapter/extension.js`.** See
the retired Phase 14 cutover prep.

### Package D — Phase 15: legacy retirement — **IN PROGRESS**
Retire the tracked legacy source surfaces (retired TS/esbuild build scaffolding, the MCP
server, the Node/TS CLI, the old `extension-bridge`, dead standalone helpers, legacy debug
scripts). `the seed CLI` remains the canonical **untracked** CLI and is not deleted.

---

## 8. Architectural invariants (hard rules)

1. The Python runtime **never binds in 3637–3646** before the Phase-14 cutover.
2. The JS adapter **never owns public CODEAPI compatibility shaping** (only the bridge envelope).
3. Python **never calls `vscode.*` directly** — only via the adapter bridge.
4. The **bridge token is mandatory** on every `/vscode/*` request.
5. **Terminal liveness never comes from Python inference** — always adapter-provided.
6. Existing `the seed CLI` parity remains **exception-free until Phase 15** (no whitelists).
7. **`package.json.main` remains unchanged until Phase 14.** *(Phase 14 cutover APPLIED
   2026-07-07: `main` is now `./extension-adapter/extension.js`. The Python runtime still
   binds 47337+ outside the 3637–3646 band, so invariant #1 is unaffected.)*
8. **`/term/*` implementation must not begin until this architecture document is committed.**

---

## Acceptance of this document

Defines the full architecture top-down; includes the route-ownership table (§3), the
single-source wire rules (§4), and the remaining work packages (§7). Its purpose is to
**prevent future phases from inventing architecture ad hoc**. Packages A–C (Phases 13D,
13E, 14) are complete and live; the current work is **Package D (Phase 15 — legacy
retirement)**.
