# Security Notes

CODEAPI is powerful because it executes code inside VS Code and can spawn real
shells. Treat it as a local trusted automation interface.

## Default Boundary

The HTTP server is intended to bind only to:

```text
127.0.0.1
```

Do not expose the CODEAPI loopback ports (the Python runtime binds `47337`+) on a
LAN, public interface, or tunnel. The optional public token alone is not
sufficient for network exposure; authorization, audit and policy controls would also be needed.

## Opt-in public API authentication

The Python public API is not the token-protected internal `/vscode/*` bridge.
Public routes on loopback remain unauthenticated **by default** to preserve
existing trusted-local clients. Loopback prevents direct connections from other
machines, but does not isolate the service from other local processes or
automatically protect against browser-origin requests.

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

## Risky Capabilities

`POST /exec` can run arbitrary JavaScript in the extension host.

`POST /term/create` can spawn a real PTY shell.

Together, these are equivalent to local code execution as the current user.

## Recommended Operating Practice

- Use CODEAPI on trusted machines only.
- Keep the server loopback-only.
- Do not run unknown scripts through `/exec`.
- Treat captured terminal buffers as sensitive.
- Kill unused PTY sessions with `codeApiTerm.kill(id)`.
- Fully restart VS Code after installing a changed extension build.

## If Remote Access Is Needed

Prefer an SSH or Tailscale session to the machine, then call CODEAPI locally from
inside that session. Avoid binding CODEAPI directly to a remote network address.

If direct remote access is ever required, add at least:

- authentication token
- origin allowlist
- endpoint allowlist
- audit logging
- explicit enable/disable setting
