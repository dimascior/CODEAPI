# Security Notes

CODEAPI is powerful because it executes code inside VS Code and can spawn real
shells. Treat it as a local trusted automation interface.

## Default Boundary

The HTTP server is intended to bind only to:

```text
127.0.0.1
```

Do not expose the CODEAPI loopback ports (the Python runtime binds `47337`+) on a
LAN, public interface, or tunnel without adding authentication and policy controls.

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
