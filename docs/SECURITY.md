# Security

CODEAPI executes code inside VS Code and can spawn real shells.  The
hardening below reduces the attack surface to the minimum the terminal
bridge requires.

## Loopback binding

The HTTP server binds **only** to `127.0.0.1` (enforced by
`RuntimeConfigError` in `runtime.py`).  The Python-runtime ports
(`47337`+) must never be exposed on a LAN, public interface, or tunnel.
The optional public token alone is not sufficient for network exposure;
authorization, audit and policy controls would also be needed.

## Browser origin guard

Every request is checked before dispatch (`http_server.py:_handle`):

- **Origin header present → 403.**  Browsers add `Origin` on
  cross-origin requests AND on same-origin POST/PUT/DELETE.  Legitimate
  callers (CLI tools, `Invoke-RestMethod`, Node `http.request`) never
  set it.
- **Non-loopback Host header → 403.**  Blocks DNS-rebinding attacks
  where a page resolves a custom hostname to `127.0.0.1`.

## Opt-in public API authentication

The Python public API is not the token-protected internal `/vscode/*` bridge.
Public routes on loopback remain unauthenticated **by default** to preserve
existing trusted-local clients. Loopback prevents direct connections from other
machines, but does not isolate the service from other local processes.

Set `CODEAPI_PUBLIC_TOKEN` to a cryptographically random secret **before
launching VS Code** to require `X-CODEAPI-TOKEN: <secret>` for all public
routes except `GET /health` and `GET /session` (identity discovery).
Missing or incorrect credentials return HTTP 401 before dispatch. The bundled
Python client automatically sends the header when that variable is set.

PowerShell 5.1+ example, using a cryptographically secure random generator:

```powershell
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$env:CODEAPI_PUBLIC_TOKEN = [BitConverter]::ToString($bytes).Replace('-', '')
code .
```

Supply the same token to authorized clients through their environment; raw
HTTP callers must add the header. Do not store secrets in source control or
shared terminal transcripts. This does not replace OS process isolation.

## Security policy (restricted mode)

Controlled by the VS Code setting `codeapi.securityPolicy` (machine
scope, default `"restricted"`), propagated as
`CODEAPI_SECURITY_POLICY`.

| Route | Restricted | Open |
|---|---|---|
| `POST /exec` | **403** | Executes JS in the extension host |
| `POST /command/:id` | **403** | Runs any VS Code command |
| `POST /term/create` (custom `shell`) | Validated via `dialect_for_shell`; unsupported → **403** | Any shell |
| `POST /term/create` (`args`) | **403** | Passed through |
| All other routes | Normal | Normal |

## Write-gate verification

Controlled by the VS Code setting `codeapi.gateRoot` (machine scope,
default empty), propagated as `CODEAPI_GATE_ROOT`.  When set, terminal
write and submit operations are verified against the Helios gate system.

The guard (`gate_guard.py`) **fails closed**:

- **No inflight directory** → refuse.
- **Directory unreadable** → refuse.
- **No matching gate** → refuse.
- **Positive match** → allow once; the gate is consumed in memory and
  cannot authorise a second write.

Each gate must match:

| Write (`/term/:id/write`) | Submit (`/term/:id/submit`) |
|---|---|
| `data_sha256` (SHA-256 of `data`) | `submit_command_sha256` (SHA-256 of raw command) |
| `terminal_id` | `terminal_id` |
| `enter` (boolean) | — |
| `expires_utc` (not expired, parseable) | `expires_utc` |

Gate files are read as `utf-8-sig` to tolerate the BOM that PowerShell
5.1 `Set-Content -Encoding UTF8` produces.  CODEAPI **never** moves or
deletes gate files — that is the hook's and the lifecycle tooling's
responsibility.

## Recommended operating practice

- Use CODEAPI on trusted machines only.
- Keep the server loopback-only (enforced).
- Leave `securityPolicy` at `"restricted"` unless legacy tooling
  requires `/exec` or `/command`.
- Set `gateRoot` to the `.command-gate` directory when the Helios gate
  system is in use.
- Kill unused PTY sessions with `codeApiTerm.kill(id)`.
- Fully restart VS Code after installing a changed extension build.

## If remote access is needed

Prefer an SSH or Tailscale session to the machine, then call CODEAPI
locally from inside that session.  Avoid binding CODEAPI directly to a
remote network address.
