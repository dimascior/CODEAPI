# Regression fixture: PTY foreground ownership and wrapper/wrapped-command evidence

## Context

Captured 2026-08-09 from a live HeliosEvidenceCatalog promotion boundary.
Not synthetic. One trace exercises foreground-process ownership, pager
interception, an ambiguous write receipt, missing command-admission
evidence, and PTY desynchronisation.

Two architectural results:

    wrapper execution          !=  wrapped-command admission
    transport acknowledgement  !=  terminal-state acknowledgement

The terminal `tmbrwfix1` and its gate evidence are retained as the
historical reproduction and must not be used for recovery.

## Trigger

A PowerShell script driving git with bare `git diff` / `git log` /
`git show`. Git pages when stdout is a TTY, and the corridor terminal is
a real PTY, so `less` took the foreground at the first diff. Every
subsequent `term run` was delivered into `less` as pager keystrokes.

## Minimal reproduction

    codeapi -p <port> term run <term> 'powershell -File script-calling-bare-git-diff.ps1'
    codeapi -p <port> term run <term> 'powershell -File anything-else.ps1'

The second wrapped command is never admitted by the target shell.

## Observed sequence

    term run  publish-hygiene.ps1
      -> review output printed correctly
      -> full `git diff` reached
      -> less takes foreground
      -> completion token absent at 120s
      -> the commit/push steps AFTER the diff never ran

    term run  verify-hygiene-publish.ps1
      -> text consumed by less as pager keystrokes
      -> transcript fills with SUMMARY OF LESS COMMANDS
      -> terminal now in the less HELP screen

    term write --b64 cQ==      (payload decodes to one character, "q")
      -> reported: wrote: 2
      -> `q` exits the HELP screen, returns to the PAGER
      -> the appended Enter answers "Press RETURN for more"
      -> foreground still owned by less

    term run  state-check.ps1  (GIT_PAGER=cat, --no-pager throughout)
      -> payload submitted to PTY; target-shell admission not proven

## Finding 1 - wrapper and wrapped-command are separate evidence planes

The central result. A terminal wrapper operation has two independent
execution planes, and success in the outer plane establishes nothing
about the inner one.

    WRAPPER PLANE                        WRAPPED-COMMAND PLANE
    authorization matched     PROVEN     wrapped command identified  PROVEN
    wrapper submitted         PROVEN     payload submitted to PTY    PROVEN
    wrapper process started   PROVEN     target-shell admission      NOT PROVEN
    wrapper process completed PROVEN     wrapped command started     NOT PROVEN
                                         wrapped command completed   NOT PROVEN

Stated precisely:

    wrapper success currently cannot establish wrapped-command execution.

For this incident: the outer `codeapi ... term run` executed - it
connected, submitted the payload to the PTY and exited cleanly. The
wrapped PowerShell execution was not proven, because target-shell
admission was not proven.

An earlier draft used the shorthand "gate consumed without execution".
That wording is retired: it is imprecise and points the fix at the wrong
layer. It appears here only as superseded historical wording.

Identity consequence - a wrapper operation must retain both identities
independently, connected to the existing Helios wrapper/wrapped-command
model rather than a parallel namespace:

    wrapper_command / wrapper_command_sha256
    wrapped_command / wrapped_command_sha256
    terminal_id
    terminal_generation (session identity)
    authorization / submission / admission / completion correlations

A successful wrapper result is never promoted into a successful
wrapped-command result without wrapped-command evidence.

## Finding 2 - transport receipt is not terminal-state evidence

`wrote: N` means input was accepted into the PTY input stream. It does
not mean the foreground process consumed it as intended, changed state,
or returned control to the shell.

The Python layers already say so:

    terminal_client.write  "Returns {id,wrote}: bytes ACCEPTED, not command success."
    adapter.term_write     "`wrote` = BYTES ACCEPTED, not command success and
                            not proof of life."

The contract is documented in the layer nobody reads and dropped at the
CLI boundary, which prints a bare `wrote: 2`. The caveat must survive to
the surface operators and agents actually use.

Target result contract:

    payload_kind            line_input | exact_text | raw_bytes
    text_encoding           utf-8 | none
    append_enter            true | false
    requested_units         with unit named
    accepted_units          with unit named
    terminal_state_before   observed, or explicitly unknown
    terminal_state_after    observed, or explicitly unknown
    shell_ready             observed, or explicitly unknown

Terminal-state fields may report `unknown` until foreground observation
exists; they may not be omitted, because omission reads as success.

## Finding 3 - CodeAPI term write input semantics

Established from source, not inferred.

    # python/codeapi_vscode/cli.py  cmd_term_write
    enter = not args.no_enter                     # DEFAULT: True
    decoded = base64.b64decode(b64).decode("utf-8")
    client.term.write(tid, decoded, enter=enter, ...)

`--b64` is therefore NOT a raw-byte interface. The payload is
base64-decoded and then UTF-8 decoded to a Python string before it
reaches the terminal client. Accurate classification of the existing
forms:

    term write TEXT                     text input, Enter appended
    term write TEXT --no-enter          exact text input, no terminator
    term write --b64 B64                base64-encoded UTF-8 TEXT, Enter appended
    term write --b64 B64 --no-enter     base64-encoded UTF-8 TEXT, no terminator

So `--b64 cQ==` sent the character `q` plus an Enter terminator appended
by design. That is the second unit in `wrote: 2`.

The distinction to carry through the API:

    encoded text   - passes through UTF-8 decoding; not byte-transparent
    raw bytes      - no decoding, no terminator, byte-for-byte

There is currently NO raw-bytes primitive. Arbitrary byte sequences that
are not valid UTF-8 cannot be expressed by any existing form.

## Finding 4 - begin/end admission evidence

Every `term run` needs a unique execution envelope:

    BEGIN <execution-id>
    <wrapped command>
    END <execution-id> <exit-code>

    no BEGIN        -> wrapped-command admission NOT PROVEN
    BEGIN, no END   -> started, completion unknown
    BEGIN + END     -> completed with captured exit

None of these may be inferred from the CodeAPI client process exit. The
absence of a completion token is ambiguous across distinct failures:

    completion token absent
      |- command still executing
      |- interactive child owns foreground
      |- command never admitted by the shell
      |- shell restarted
      |- terminal desynchronised
      `- transport/output loss

"May be interactive" collapses all of these. This trace is the third case.

## Finding 5 - foreground ownership and terminal generation

A begin marker distinguishes "started but did not finish" from "never
admitted". It does NOT tell a caller how to recover, because the correct
recovery input depends on which application owns the foreground:

    less (pager view)   -> q
    less (HELP screen)  -> q returns to the PAGER, not to the shell
    vi                  -> ESC :q!
    python REPL         -> exit() or ^D
    ^C                  -> wrong or destructive in several of the above

This trace shows the second case exactly. One `q` was sent, the receipt
was correct, and the conclusion "pager cleared" was wrong - the foreground
state had been inferred rather than observed. Two `q`s were required from
the help screen.

Terminal state, formalised independently of transport success:

    READY
    COMMAND_SUBMITTED
    COMMAND_STARTED
    INTERACTIVE
    DESYNCHRONIZED

`term run` may submit only when there is positive shell-readiness evidence
for the expected terminal generation. Without it:

    term run
      -> report terminal state
      -> preserve the proposed command WITHOUT treating it as started

Expose, preferring observable evidence over application-name guessing:

    terminal_state
    state_confidence / evidence
    shell_ready
    active_process (when observable)
    generation
    last_execution_id

## Finding 6 - Helios authorization lifecycle with ADMISSION_UNKNOWN

A missing begin sentinel does NOT prove non-execution: the wrapped command
may have started while the sentinel or its output was lost. The
single-use authorization lifecycle and the command execution lifecycle are
linked but distinct, so a binary "burns only on admission" is wrong.

    PENDING
      -> RESERVED / INFLIGHT
      -> WRAPPER_EXECUTED
      -> wrapped-command admission classification:

           PROVEN_ADMITTED      -> normal execution/result lifecycle
           PROVEN_NOT_ADMITTED  -> retry policy may evaluate reuse
           ADMISSION_UNKNOWN    -> fail closed for automatic replay,
                                   preserve evidence, require
                                   reconciliation before another execution

This prevents an uncertain command from silently becoming reusable and
executing twice.

## Contributing caller defect - Git pager activation

Scripts driving git in a PTY must neutralise paging:

    $env:GIT_PAGER = 'cat'
    git --no-pager <verb>

Caller responsibility, and it does not remove the need for the above: any
interactive child produces the same class of failure.

## Preserved evidence

    evidence/20260809-codex-cat-prod-hygienepublish-747.gate.json
    evidence/20260809-codex-cat-prod-hygieneverify-748.gate.json
    evidence/20260809-codex-cat-prod-pagerquit-749.gate.json
    evidence/20260809-codex-cat-prod-statecheck-750.gate.json

749 and 750 are the pair that matter: 749 wrote into an unknown
foreground; 750's wrapper executed while its wrapped command was never
proven admitted.

## Duplicate-key non-finding

Duplicate JSON keys were suspected in gates 749 and 750 after in-place SHA
and exit_capture edits. Inspection of the archived gates shows single
canonical keys throughout - the edits were replacements, not appends. This
trace is NOT evidence that duplicate keys occurred.

A duplicate-key rejection rule remains independently justified as schema
hardening for `command_sha256`, `command`, `risk_tier`,
`working_directory`, `shell` and `exit_capture`, since last-wins parsing
would otherwise silently decide what was authorised.

## Required regression tests

Derived directly from this incident. Not yet implemented; listed as the
acceptance set.

    pager owns foreground        second term run is not shell-admitted
    less HELP + one q            returns to pager, not to shell
    transport acceptance         does not mark shell ready
    line input                   adds the proven Enter representation
    exact text input             adds no terminator
    base64 UTF-8 text            preserves defined text semantics
    invalid UTF-8 base64         demonstrates the encoded-text boundary
    multibyte UTF-8 payload      survives CLI -> adapter unchanged
    NUL payload                  behaviour recorded through the string path
    escape/control sequence      behaviour recorded through the string path
    raw-write primitive          requested bytes == transport bytes (once built)
    wrapper completion           does not imply wrapped-command completion
    missing BEGIN                does not imply safe replay
    enter default at every layer omission produces one canonical behaviour

## Resolved - complete write call chain, cited at every layer

    python/codeapi_vscode/cli.py            cmd_term_write
        enter = not args.no_enter                       -> default True
        decoded = base64.b64decode(b64).decode("utf-8") -> Python str
    python/codeapi_vscode/terminal_client.py  write(..., enter=True)
        POST /term/{id}/write  {"data": str, "enter": bool}
    python/codeapi_vscode/server/term_routes.py  write_handler:58
        b = _json_body(req)                             -> {} on parse failure
        adapter.term_write(tid, b.get("data"),
                           enter=bool(b.get("enter")))  -> TRUTHINESS, no schema
    python/codeapi_vscode/vscode/adapter.py   term_write:220 (enter=False default)
        POST /vscode/term/{id}/write  {"data", "enter"}
    extension-adapter/extension.js            termWrite:536
        let data = opts.data == null ? '' : String(opts.data);
        if (opts.enter) data += '\r';
        rec.pty.write(data);
        return { id, wrote: Buffer.byteLength(data) };

Answers, from source:

    VS Code API invoked   NONE for writes. The terminal is created as
                          vscode.window.createTerminal({pty: pseudo}) over an
                          adapter-owned node-pty; writes go straight to
                          rec.pty.write(). Terminal.sendText is NOT used, so
                          addNewLine semantics never apply.
    enter=true means      data += '\r'
    Enter representation  CR, U+000D, 0x0D. Not LF, not CRLF.
    text encoding         Python str -> JSON -> JS string -> node-pty writes
                          the string (UTF-8). A second encoding step exists.
    `wrote` unit          Buffer.byteLength(data) = UTF-8 BYTES of the final
                          string INCLUDING the appended CR. Not characters.

For the incident payload:

    "cQ==" -> b"q" -> "q" -> {"data":"q","enter":true}
           -> "q" + "\r" -> pty.write("q\r") -> wrote: 2

`wrote: 2` is therefore exactly correct and exactly uninformative: it counts
bytes handed to node-pty, including a terminator the caller did not supply.

## Resolved - Gate C1: no request validation on the HTTP write route

`_json_body` (term_routes.py:27) has NO schema and NO validator:

    try:    data = req.json()
    except: return {}            # invalid/empty body -> {} , fail-OPEN
    return data if isinstance(data, dict) else {}

So `bool(b.get("enter"))` applies Python truthiness to an arbitrary JSON
value. Observed contract:

    omitted   -> None   -> False  -> no Enter
    null      -> None   -> False  -> no Enter
    false     -> False  -> False  -> no Enter
    true      -> True   -> True   -> Enter
    0         -> 0      -> False  -> no Enter
    1         -> 1      -> True   -> Enter
    ""        -> ""     -> False  -> no Enter
    "true"    -> str    -> True   -> Enter
    "false"   -> str    -> TRUE   -> ENTER            <-- semantic inversion

The `"false"` case is the hazard. A loosely-typed client, a shell-built
JSON body, or any form-encoded bridge that stringifies booleans asks for
"no Enter" and gets Enter. In a PTY an unintended Enter SUBMITS whatever
is on the current input line - which is the same failure class this whole
fixture documents.

Compounding, `data` is equally unvalidated, and the extension maps a
missing payload to empty string:

    opts.data == null ? '' : String(opts.data)

so `{"enter": true}` with no `data` writes a bare CR - pressing Enter on
whatever the foreground application currently holds.

Target contract: validate `enter` as a real Boolean, reject all
non-Boolean values with a request error, and choose ONE documented default
for omission. Do not rely on truthiness conversion.

## Open implementation gaps

    1. No raw-bytes primitive. `--b64` is encoded text; non-UTF-8 byte
       sequences are inexpressible through any current form.
       STILL OPEN, and now MEASURED rather than inferred: the CLI decodes
       with .decode("utf-8") before the terminal client call, so valid
       Base64 carrying non-UTF-8 bytes raises UnicodeDecodeError with the
       terminal untouched (test_cli_term_write_b64.py).
    2. No request validation on /term/{id}/write: `enter` accepts any JSON
       value via truthiness, and "false" appends Enter.
       CLOSED - see "Resolved - C1.3/C2/C3 input contract".
    3. Invalid or non-object JSON bodies fail OPEN to {} rather than 400.
       CLOSED - same section. Note the first fix for this was itself
       defective; the detail is recorded there because it is the more
       useful finding.
    4. `data` unvalidated; null becomes '' and, with enter, sends a bare CR.
       CLOSED - same section.
    5. No begin sentinel, so admission is unobservable. STILL OPEN.
    6. No foreground-ownership or shell-readiness reporting. STILL OPEN.
    7. CLI write result does not carry the transport-only caveat that the
       Python layers document, and `wrote` is not labelled with its unit
       (UTF-8 bytes including any appended terminator). STILL OPEN at the
       CLI print site; the unit itself is now proven and documented at the
       shaping layer (bridge_core.termWritePayload).
    8. A caller reaching the VS Code bridge directly, bypassing the Python
       runtime, was NOT covered by the strict contract.
       CLOSED - see "Resolved - D3/D4/D5 transport boundary". The /exec
       facade still reads `enter` for truthiness, deliberately and now
       separately.

## Default inversion - live at the HTTP boundary, not merely latent

Complete inventory of `adapter.term_write` callers:

    server/term_routes.py:62   enter=bool(b.get("enter"))   EXPLICIT
    tests/test_adapter_term.py:126  enter=True               EXPLICIT
    tests/test_term_routes.py:45    stub, default False      n/a

Declared defaults:

    cli.py            enter = not args.no_enter     -> True
    terminal_client   def write(..., enter=True)    -> True
    adapter.py        def term_write(..., enter=False) -> False

Because the only production caller passes `enter` explicitly, the adapter
default never fires - so the inversion is latent THERE. But it is live one
layer out: `term_routes` computes `bool(b.get("enter"))`, and a JSON body
omitting `enter` yields `None` -> `False`. An HTTP caller that omits the
field gets NO Enter, while a CLI caller omitting `--no-enter` gets Enter.

    CLI omission   -> Enter appended
    HTTP omission  -> Enter NOT appended

That is the "direct callers receive opposite behaviour from CLI callers"
case, and it is reachable today by any non-CLI client. One canonical
default must be chosen and propagated through CLI, client, transport,
adapter and extension, with a regression test for omission at every
directly callable layer.

The inventory and defaults above describe the state AT THE INCIDENT. They
are preserved unchanged as the historical record; the current state is in
the section below.

## Resolved - C1.3/C2/C3 input contract

Chosen canonical default: `enter` TRUE. Two of the three declared defaults
already said True (CLI, terminal client); the HTTP route was the outlier.

Current contract, each line proven by a named test rather than by reading:

    root body   JSON object required, parsed strictly
    data        REQUIRED, must be a JSON string
    enter       optional, must be a JSON boolean, default TRUE
    adapter     term_write(term_id, data, *, enter) - required, keyword-only,
                must be a real bool
    extension   bridgeCore.termWritePayload - one CR appended when enter is
                truthy; `wrote` = Buffer.byteLength(final), UTF-8, incl. CR

    python/tests/test_term_write_contract.py     40  route + adapter
    python/tests/test_cli_term_write_b64.py      15  encoded-text boundary
    extension-adapter/tests/test_term_write_bytes.js  26  byte theorem

Every rejection case asserts HTTP 400 AND an adapter call count of zero, so
a rejection cannot be reported by a status code while the terminal was
nonetheless touched. Every acceptance case asserts the exact
(term_id, data, enter) triple, with `enter` compared by IDENTITY so no
truthy stand-in passes as True.

The three-way distinction the incident lacked is now structural:

    {}                          400, no terminal action
    {"data":""}                 200, a deliberate bare Enter
    {"data":"","enter":false}   200, nothing at all

### Finding 7 - the first fix for gap 3 was itself defective

Tightening `enter` to default TRUE made an unparseable body MORE dangerous
than it had been: a malformed request became `{}`, `data` became None, the
extension mapped null to '', and the write became a bare CR. A body-shape
guard was added in the same change, before it could ship.

### Finding 8 - the body-shape guard was dead code, and the matrix passed anyway

The guard called `req.json()`, which delegates to `util.json.loads`. That
helper is TOLERANT BY DESIGN - it returns `{}` for a malformed body and
never raises - so the try/except around it could not fire. Malformed
bodies were still rejected, but by the `data` guard, and the acceptance
matrix showed 30/30 green while the documented protection did not exist.
It surfaced only because the matrix printed the measured error string and
malformed input reported "'data' is required" instead of "not valid JSON".

The rejection was therefore ACCIDENTAL: had `data` ever acquired a default,
malformed input would have become `{}` and, under ENTER_DEFAULT=True, a
bare CR. `_object_body` now parses `req.text()` directly, and every
rejection case asserts WHICH guard fired.

    a passing matrix is not evidence that the guard under test is the one
    doing the work

## Resolved - D3/D4/D5 transport boundary

### Finding 10 - the strict contract stopped one layer above the CR

Python validation was never end-to-end, because the extension bridge is an
INDEPENDENTLY REACHABLE API rather than an internal call:

    transport    HTTP, extension.js own http.createServer
    binding      listen(0, '127.0.0.1') - loopback, ephemeral port
    auth         X-CODEAPI-BRIDGE-TOKEN must equal a per-launch 32-byte
                 random hex token, checked BEFORE the body is read and
                 before any vscode.* call; otherwise 403 {error:forbidden}
    token reach  minted in activate(), handed to the Python child through
                 the environment as CODEAPI_BRIDGE_TOKEN - so it is NOT a
                 Python-only secret and Python is NOT a mandatory hop
    body         handleTermWrite did JSON.parse(body || '{}') and no more

Any local process holding the token therefore reached the CR-append
directly. Measured bypasses, all accepted by the bridge before this change:

    {"data":123}                  String(123) -> "123" typed into a shell
    {"data":"","enter":"false"}   "false" is TRUTHY -> a bare CR submitted
    {}                            data '', enter falsy -> writes nothing

The last one also shows the bridge's own omission default was FALSY - the
opposite of the route's TRUE. The same omitted field meant different things
depending on which door the request came through.

`bridge_core.validateTermWriteRequest` now applies the same theorem at that
boundary: `data` a required string, `enter` a required real boolean, no
truthiness anywhere. `enter` is REQUIRED rather than defaulted because this
is a transport edge and the only supported caller
(VsCodeAdapter.term_write) always sends it explicitly.

    extension-adapter/tests/test_bridge_write_contract.js   38

### Finding 11 - one helper cannot serve two contracts

The strict validator is deliberately SEPARATE from `termWritePayload`,
which stays coercive for the `/exec` codeApiTerm facade - that facade hands
`opts` to user-authored exec code where JS truthiness is the ambient
expectation. Folding both into one helper would mean weakening the strict
contract to keep the compatibility one working. The facade's coercion is
asserted UNCHANGED in the same suite, so the separation is proven rather
than claimed:

    facade   write(id, 123, {enter:false})      -> "123"
    facade   write(id, "q", {enter:"false"})    -> "q" + CR
    strict   {"data":123}                       -> rejected
    strict   {"data":"q","enter":"false"}       -> rejected

### Finding 9 - the write shaping existed twice

`extension.js handleTermWrite` and `bridge_core makeCodeApiTerm.write` each
carried their own copy of the CR-append and the byte count - identical, and
free to drift. The decision this whole fixture turns on had two definitions
and no test on either. Both now call `bridge_core.termWritePayload`. The
extraction is behaviour-preserving, evidenced by the pre-existing 59-test
bridge_core suite passing unchanged.
