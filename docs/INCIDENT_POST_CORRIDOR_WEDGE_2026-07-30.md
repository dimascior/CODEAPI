# CODEAPI incident record — POST-body corridor wedge after an interrupted in-flight request

Untracked workstation artifact, produced under the Control-Plane Recovery
charter (gate chain `pr5j-*`). Evidence labels: `local-proven` /
`source-proven` / `live-proven` / `inference` / `unresolved`.

## 1. Timeline (2026-07-30, workstation local)

| time | event | evidence |
|---|---|---|
| ~12:57 | `codeapi term run tms3hojz7b <probe>` blocks; killed at the 5-min tool timeout | gate `pr5g-probe-001` |
| ~13:13 | Buffer read shows the probe **executed** (PROBE_OK) — delivery without return | `pr5g-read-001` |
| ~13:24 | `term write` setup line acks (`wrote: 130`); 4,041-char chunk write blocks, killed at 3 min; buffer later shows the chunk **delivered and executed** | `pr5g-ship-002`, `pr5g-read-002` |
| 13:32→ | Every subsequent write blocks silently: 5×4,041-char (killed at 40 s), 84-char (90 s), 15-char (30 s ×2) | `pr5g-ship-003/004`, `pr5g-probe-002`, `pr5i-probe-001/002` |
| ~13:57 | `codeapi exec` blocks (45 s); `term create` blocks (40 s). GETs (`sessions`, `term list`, `term read`) serve throughout | `pr5g-exec-001`, `pr5h-create-001` |
| 14:22–15:34 | Wedge persists ≥ 2 h across spaced retries; buffers byte-static | `pr5h-*`, `pr5i-*` |
| ~20:0x | **+33 bytes appear on the fable buffer**: one bare re-printed prompt, no command echo — residue of a wedged write draining | `pr5j-inv-001/004` |
| 20:16 | Direct probes: POST corridor answers in **milliseconds** — wedge gone | `pr5j-probe-001` |
| 20:2x–20:59 | Full POST matrix passes; PR 5 proof lifecycle completes through the corridor | `pr5j-probe-004..006`, `pr5j-xfer-*`, `pr5j-exec-*`, `pr5j-clean-*` |

## 2. Request sizes and client timeouts

| request | size (chars) | client bound | outcome |
|---|---|---|---|
| term run probe | ~140 | `--run-timeout 60`, killed at 300 s | delivered, no return |
| chunk write | 4,041 (+CR) | killed at 180 s | delivered, no return |
| chunk writes ×5, ×6 | 4,041 each | `timeout 40` / `timeout 25` | not delivered |
| sizecheck write | 84 | `timeout 90` | not delivered |
| echo probes | 15 | `timeout 30` | not delivered |
| exec / create | ~30 / n-a | `timeout 45` / `timeout 40` | not delivered |

CLI-internal read timeouts (30 s default at both the CLI→runtime and
runtime→bridge legs) never surfaced an error to the caller — hangs were only
ever ended by external kill. `unresolved` which leg swallowed them.

## 3. Process identities — `live-proven`

```
component        pid    parent  port         identity
Python runtime   25876  41116   127.0.0.1:47337
                 c:\Users\dimas\.vscode\extensions\dimas.codeapi-0.1.7\
                 bin\codeapi-runtime\codeapi-runtime.exe
                 --host 127.0.0.1 --port 47337 --adapter-port 53337
extension host   41116  6608    127.0.0.1:53337 (bridge), 40418, 53307
                 Code.exe --utility-sub-type=node.mojom.NodeService
window main      6608   —       —
```

Neither pid changed across the incident: no reload, no respawn, PTYs preserved.

## 4. GET/POST matrix

During the wedge (`live-proven`):

```
GET  sessions / term list / term read      served throughout (list slow ~170 s via CLI)
POST term write / exec / term create       blocked, no acknowledgement, no delivery
onData events                              still processed (buffers grew from
                                           the last delivered chunk's echo)
```

After self-drain (`pr5j-probe-001/004..006`, `live-proven`):

```
P1 GET  runtime /session            200 in 0.002 s
P2 POST runtime /term/<bogus>/write 404 in 0.004 s   (full corridor round trip)
P3 GET  bridge  /vscode/apis        403 in 0.002 s   (tokenless, pre-body)
P4 POST bridge  /vscode/exec        403 in 0.002 s   (tokenless, pre-body)
P7 POST runtime /exec               200 in 0.039 s   {"recovery":"ok",
                                                      "typeofCodeApiTerm":"object"}
create/write/read/delete probe PTY  200 in 0.128/0.003/0.027/0.034 s
                                    marker counted twice (echo + executed)
```

## 5. Recovery results

* **Runtime-only recovery: NOT NEEDED.** The charter's kill/respawn was
  prepared (process verified, respawn path read from source) but the
  least-disruptive attempt — direct probing — found the corridor already
  healthy. No process was terminated.
* **Extension-host recovery: NOT NEEDED.** No reload. `Developer: Reload
  Window` authorization unused.
* **PTY replacement mapping: identity.** `tmrx1qs3v8` (fable) and `tms3hojz7b`
  (admin) survived with full scrollback. One terminal was **added**:
  `tmlocalxfer` (cmd.exe, pid 56044, vscode-terminal surface) as the dedicated
  local transfer PTY. Disposable probe `tmprobe5j` created and deleted.

## 6. Fault-domain analysis — source-backed

The failing operations are exactly the POSTs that traverse the extension
bridge's `readBody(req).then(dispatch)` ([extension.js:277]); GETs dispatch
without a body read, and the token check runs pre-body ([extension.js:273]).
`term write` itself is synchronous and trivial ([bridge_core.js:134]). The
Python service is a `ThreadingHTTPServer` ([http_server.py:82]) forwarding via
30 s-bounded urllib calls ([vscode/adapter.py:123]).

Root cause, stated to the evidence and no further:

```
Historical observation:
POST operations remained pending after an interrupted in-flight request.
Recovery observation:
runtime and bridge GET/POST paths responded normally during direct probes.
Source-backed hardening location:
readBody lifecycle handling.
Exact historical stuck object:
unresolved.
```

The wedge began at the moment a client was killed while a large POST was
in flight and ended, hours later, with a visible drain (one CR reaching the
fable shell). Everything between blocked at the body stage: accepted, never
dispatched, never answered. By the time direct runtime-versus-bridge probing
was possible the corridor had drained, so layer attribution between
runtime-forwarding and bridge-body-read remains `unresolved`; any account of
the precise stuck object is `inference`. What is `live-proven`: the extension
host's event loop stayed alive throughout (onData appended, GETs served), so
the fault was request-scoped state, not a dead process.

Secondary finding (`live-proven`): the `codeapi` CLI layer is independently
slow and failure-masking — `term list` ~170 s, a `term create` that died
silently at 60 s where the direct route took 69 ms, and no surfaced timeout
errors. Two CLI-side mangling classes were also isolated: an inline JSON body
reduced to empty (`wrote:0`) through the bash quoting layers, and a literal
newline inside a typed command producing a shell continuation line. Both are
eliminated by `curl --data-binary @file` bodies.

## 7. Recommended hardening (readBody is the leading location)

1. Bounded body size and a body-read deadline in `readBody()`; reply 408/413
   and destroy the socket on breach.
2. Handle `req.on('aborted')` and premature `close` — settle the promise so the
   request cannot pend forever.
3. Response guards: never write to a socket whose request was aborted.
4. Regression tests: client disconnects mid-body; a successful POST immediately
   following an interrupted POST; large-body POST followed by small.
5. File streaming for large payloads (the `xfer.cmd` + ssh-stdin pattern);
   never multi-KB terminal lines.
6. CLI: surface timeouts as errors, bound session discovery, and log which leg
   a hang occurred on.

## 8. Final evidence classification

```
POSTs pending after an interrupted in-flight
  request (historical observation)            live-proven
corridor healthy end to end (post-drain)      live-proven
readBody lifecycle as hardening location      source-proven
exact historical stuck object                 unresolved (drained before probing)
event loop alive during wedge                 live-proven
CLI layer slow/failure-masking                live-proven
PTY preservation through recovery             live-proven
```
