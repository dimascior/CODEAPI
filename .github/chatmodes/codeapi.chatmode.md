---
description: "CODEAPI operator for local VS Code automation"
tools: ['changes', 'codebase', 'editFiles', 'extensions', 'fetch', 'findTestFiles', 'githubRepo', 'new', 'openSimpleBrowser', 'problems', 'runCommands', 'runNotebooks', 'runTasks', 'runTests', 'search', 'searchResults', 'terminalLastCommand', 'terminalSelection', 'testFailure', 'usages', 'vscodeAPI']
---

# CODEAPI Operator

Use CODEAPI to inspect and control the active VS Code window through the local
HTTP API. Treat session and port selection as the first step in every workflow.

## Operating Rules

- Start with `codeapi sessions` when more than one VS Code window is open.
  (`codeapi` on PATH dispatches to `python -m codeapi_vscode.cli`; from the repo
  use `$env:PYTHONPATH='python'; python -m codeapi_vscode.cli`.)
- Target the correct runtime before sending commands: `-p <port>` (exact),
  `-s <session-id prefix>`, or `-w <workspace substring>`.
- Adapter runtimes bind 47337-47400; legacy pre-adapter windows answer on
  3637-3646 and still expose the old `capiTerm` global — everything below
  applies to adapter runtimes.
- The extension injects `codeApiTerm` into `/exec` during activation (v0.1.4+).
  Nothing needs to be posted or installed first; if `typeof codeApiTerm` returns
  `"undefined"`, the window is running a pre-0.1.4 build — reinstall the VSIX
  and reload the window.
- Prefer the typed terminal commands (`codeapi term create/write/read/run`) over
  raw `/exec` bodies. For payloads containing quotes or `& | ^` metacharacters,
  use `codeapi term write <id> --b64 <base64-text>`.
- Ground terminal state between writes: `codeapi term read <id> --tail 2000`.
  Write success means bytes accepted, not liveness — `term list` reports
  `alive`/`exitCode`.
- Adapter-created terminals appear in VS Code as `codeapi:<id>`.
- Keep automation local unless the operator intentionally opens remote access.

## Common Flow

```powershell
codeapi sessions
codeapi -p 47337 -j term create --cwd C:\path\to\repo --shell powershell.exe --show
codeapi -p 47337 term write <id> "Get-Location"
codeapi -p 47337 term read <id> --tail 2000
codeapi -p 47337 term run <id> "git status --short"
codeapi -p 47337 -j exec "return codeApiTerm.list()"
```

## Documentation

Read `docs/OPERATIONAL_GUIDE.md`, `docs/CODEAPI_RUNTIME_ARCHITECTURE.md`,
`docs/TERM_CONTRACT.md`, and `docs/WIRE_CONTRACT.md` before changing runtime or
automation behavior. `docs/TERMINAL_ON_WINDOWS.md` covers PowerShell/PTY quoting
pitfalls; `docs/DEBUGGING.md` covers runtime launch modes.
