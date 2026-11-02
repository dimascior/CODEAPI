// extension-adapter/bridge_core.js — vscode-FREE bridge core (Phase 14 prerequisite).
//
// Holds the two pieces of bridge logic that must be identical to the legacy runtime
// AND must be unit-testable without the VS Code extension host:
//
//   1. /exec global injection — the exact set of globals a legacy `/exec` body sees
//      (vscode, Buffer, os, process, cp, nodePty, codeApiTerm). runExecBody() builds the
//      Function with that parameter list so a body that references any of them works.
//   2. /apis descriptor shaping — the 13 fixed non-command descriptors plus the
//      command-registry rows in the legacy shape.
//
// This module does NOT require('vscode') or require('node-pty'), so it loads in plain
// Node (tests, tools) as well as inside the extension host. The vscode/node-pty
// objects are passed IN by extension.js. See docs/PHASE_14_CUTOVER_PREPARATION.md
// (the /exec-globals and /apis blockers this closes) and docs/WIRE_CONTRACT.md (/apis).

'use strict';

const os = require('os');
const cp = require('child_process');

// The 13 fixed API descriptors captured verbatim from the LEGACY runtime in
// docs/PHASE_14_CUTOVER_PREPARATION.md (gate phase14-probe-065): 3 workspace, 5 window,
// 2 editor, 1 commands, 1 languages, 1 extensions. Fixed rows carry {category, method,
// parameters} only — `description` is a command-row-only field (WIRE_CONTRACT §/apis).
const FIXED_API_DESCRIPTORS = [
  { category: 'workspace', method: 'openTextDocument', parameters: ['uri'] },
  { category: 'workspace', method: 'saveAll', parameters: [] },
  { category: 'workspace', method: 'findFiles', parameters: ['include', 'exclude?', 'maxResults?'] },
  { category: 'window', method: 'showInformationMessage', parameters: ['message', '...items'] },
  { category: 'window', method: 'showErrorMessage', parameters: ['message', '...items'] },
  { category: 'window', method: 'showWarningMessage', parameters: ['message', '...items'] },
  { category: 'window', method: 'showInputBox', parameters: ['options?'] },
  { category: 'window', method: 'showQuickPick', parameters: ['items', 'options?'] },
  { category: 'editor', method: 'edit', parameters: ['callback'] },
  { category: 'editor', method: 'insertSnippet', parameters: ['snippet', 'location?', 'options?'] },
  { category: 'commands', method: 'executeCommand', parameters: ['command', '...args'] },
  { category: 'languages', method: 'getDiagnostics', parameters: ['resource?'] },
  { category: 'extensions', method: 'getExtension', parameters: ['extensionId'] },
];

// Build the full /apis descriptor array: the 13 fixed rows PREPENDED, then one row per
// registered command in the legacy shape
// {category:'command', method:id, parameters:['...args'], description:'Execute VSCode command: '+id}.
// Window-dependent command list — callers assert structure, not count (WIRE_CONTRACT §/apis).
function apisFromCommands(commandIds) {
  const fixed = FIXED_API_DESCRIPTORS.map((d) => ({
    category: d.category, method: d.method, parameters: d.parameters.slice(),
  }));
  const cmds = (commandIds || []).map((id) => ({
    category: 'command',
    method: id,
    parameters: ['...args'],
    description: 'Execute VSCode command: ' + id,
  }));
  return fixed.concat(cmds);
}

// Run an /exec body with the legacy global set in scope. Returns whatever the body
// returns (a value or a Promise) — the caller (extension.js runValue) awaits it, which
// is what preserves legacy async /exec behavior. Buffer/os/process/cp are resolved here;
// vscode/nodePty/codeApiTerm are provided by the host via `ctx`.
function runExecBody(code, ctx) {
  ctx = ctx || {};
  // eslint-disable-next-line no-new-func
  const fn = new Function(
    'vscode', 'Buffer', 'os', 'process', 'cp', 'nodePty', 'codeApiTerm', code);
  return fn(ctx.vscode, Buffer, os, process, cp, ctx.nodePty, ctx.codeApiTerm);
}

// Terminal-create default resolution, matched to the LEGACY runtime bundle so the typed
// /term/* + codeApiTerm.create() paths migrate WITHOUT behavior change (TERM_CONTRACT §6).
// Authoritative source: the committed dist/extension.js:58 codeApiTerm.create —
//   shell(win32) = opts.shell || process.env.COMSPEC || "powershell.exe"
//   shell(posix) = opts.shell || process.env.SHELL   || "/bin/bash"
//   cols  = opts.cols     || 120
//   rows  = opts.rows     || 30
//   maxBytes = opts.maxBytes || 1e6   (1000000 — a decimal MB; NOT the binary 1048576)
//   args  = opts.args     || []
//   cwd   = opts.cwd || <workspace folder fsPath> || os.homedir()
// (corroborated by docs/REBUILD_PLAN.md's reverse-engineering: "120x30, COMSPEC,
// workspace cwd, 1 MB buffer".) This corrects the review approximation on two points:
// the Windows fallback is powershell.exe (not cmd.exe), and maxBytes is 1e6 (not 1048576).
//
// vscode-free: the workspace cwd + homedir are passed IN via ctx so this is unit-testable
// in plain Node. Windows env vars are case-insensitive, so reading `env.COMSPEC` also
// catches a `ComSpec`-cased variable on a real Windows host (matching the bundle literal).
function resolveTermDefaults(opts, ctx) {
  opts = opts || {};
  ctx = ctx || {};
  const platform = ctx.platform || process.platform;
  const env = ctx.env || process.env;
  const isWin = platform === 'win32';
  const shell = opts.shell
    || (isWin ? (env.COMSPEC || 'powershell.exe') : (env.SHELL || '/bin/bash'));
  const cwd = opts.cwd || ctx.workspaceCwd || ctx.homedir;
  return {
    shell,
    args: opts.args || [],
    cwd,
    cols: opts.cols || 120,
    rows: opts.rows || 30,
    maxBytes: opts.maxBytes || 1000000,
  };
}

// --- shell-integration bootstrap (P2) ------------------------------------
//
// Production terminals previously got NO shell integration: args defaulted to
// [] and env was the extension host's, so no VSCODE_NONCE and no dot-source.
// With readiness gating that is not a missing nicety — shellState would stay
// UNKNOWN forever and every `term run` would be refused. The bootstrap is
// therefore load-bearing.
//
// The mechanism is the one read out of the shipped shellIntegration.ps1 and
// then measured live: dot-source the script at startup with VSCODE_NONCE in
// the environment. The script reads the nonce into a global and unsets the
// variable itself.
//
// This attaches to the node-pty SHELL, not to a vscode.Terminal, so a
// headless surface:'process' terminal gets the same readiness signal as a
// visible one.
//
// The PowerShell test here answers a NARROWER question than Python's
// dialect_for_shell — "should integration be bootstrapped" rather than "is
// this dialect executable" — so the two are not the same decision. The
// basename set is exported so a test can assert they agree on PowerShell.
const POWERSHELL_BASENAMES = ['powershell.exe', 'powershell', 'pwsh.exe', 'pwsh'];

function isPowerShellShell(shell) {
  if (!shell) return false;
  const base = String(shell).replace(/\\/g, '/').split('/').pop().trim().toLowerCase();
  return POWERSHELL_BASENAMES.indexOf(base) !== -1;
}

const SI_INJECTED = 'injected';
const SI_NOT_POWERSHELL = 'not_powershell';
const SI_CUSTOM_ARGS = 'custom_args';
const SI_SCRIPT_UNAVAILABLE = 'script_unavailable';

// Returns { mode, args, env } — args/env are what the spawn should USE.
// `mode` is recorded on the terminal so a terminal without readiness can say
// WHY, rather than presenting an unexplained UNKNOWN.
function resolveShellIntegration(defs, ctx) {
  ctx = ctx || {};
  const base = { mode: SI_NOT_POWERSHELL, args: defs.args, env: {} };
  if (!isPowerShellShell(defs.shell)) return base;
  // A caller who supplied their own args gets exactly those. Appending to
  // them could change what their shell runs, and silently altering a
  // caller's process is worse than an unready terminal.
  if (defs.args && defs.args.length) {
    return { mode: SI_CUSTOM_ARGS, args: defs.args, env: {} };
  }
  if (!ctx.scriptPath || !ctx.scriptExists) {
    return { mode: SI_SCRIPT_UNAVAILABLE, args: defs.args, env: {} };
  }
  return {
    mode: SI_INJECTED,
    args: ['-NoExit', '-Command', '. "' + ctx.scriptPath + '"'],
    env: {
      VSCODE_NONCE: ctx.nonce,
      TERM_PROGRAM: 'vscode',
      VSCODE_STABLE: '1',
    },
  };
}

// Surface discriminator (docs/TERM_CONTRACT.md) — the HARD distinction between a PTY
// spawned as a bare background process and one surfaced as the VS Code integrated
// terminal `codeapi:<id>`. Every /term/create response and /term/list entry carries
// exactly one of these two labels, so a headless spawn can never be read (or reported
// by an agent) as a visible terminal window. The truthiness mirrors the create path's
// historical `if (opts.show)`, and extension.js termCreate drives the visible-terminal
// creation off THIS value — the label and the behavior are the same decision.
const TERM_SURFACE_TERMINAL = 'vscode-terminal';
const TERM_SURFACE_PROCESS = 'process';

function resolveTermSurface(opts) {
  return (opts && opts.show) ? TERM_SURFACE_TERMINAL : TERM_SURFACE_PROCESS;
}

// The ONE definition of what a terminal write submits and how large it is.
//
// This shaping existed TWICE in identical form — extension.js handleTermWrite
// (the HTTP /vscode/term/:id/write path the Python adapter reaches) and
// makeCodeApiTerm.write (the /exec facade). Two copies of the decision "does
// a CR get appended, and what does `wrote` then count" can drift, and the
// terminal-input regression this feeds (fixtures/regressions/
// pty-foreground-ownership-20260809.md) turned on exactly that decision. Both
// call sites now resolve here.
//
// `wrote` is Buffer.byteLength of the FINAL string, UTF-8, INCLUDING the
// appended CR. So it is a byte count, not a character count and not a
// keystroke count: "q" with enter is wrote=2 while a single 3-byte multibyte
// character with enter is wrote=4. It reports bytes ACCEPTED into the pty
// input stream — not command success, not admission, not proof of life.
//
// `enter` is read for TRUTHINESS here, deliberately unchanged. The /exec
// facade hands this straight to user-authored exec code where JS truthiness
// is the ambient expectation; the strict JSON-boolean contract is enforced
// upstream at the Python HTTP route and adapter, before a request ever
// reaches this process.
function termWritePayload(data, enter) {
  const text = data == null ? '' : String(data);
  const final = enter ? text + '\r' : text;
  return { data: final, wrote: Buffer.byteLength(final) };
}

// --- shell lifecycle from OSC 633 (R3/R4) --------------------------------
//
// Readiness is derived from sequences the SHELL emits, parsed out of our own
// node-pty stream. It lives here rather than on Terminal.shellIntegration so
// the identical state machine serves surface:'process' terminals, which have
// no VS Code Terminal object at all.
//
// MEASURED lifecycle (probe against the shipped shellIntegration.ps1):
//
//   startup   P;HasRichCommandDetection, P;ContinuationPrompt, P;IsWindows,
//             A, P;Cwd, B                     -> READY with no preceding D
//   command   E;<cmdline>[;nonce], C, D;<n>, A, P;Cwd, B
//   busy      C with NO D, for as long as a foreground child owns the
//             terminal — held across 8-10s for both `1..500 | more` and a
//             nested `powershell -NoProfile`, with D/A/B arriving within
//             ~100ms of the child releasing.
//
// D's code is NOT the native exit code: `cmd /c exit 3` reported D;1 and
// `throw` reported D;1, because the script maps $? to 0/1. So D marks that
// the interactive execution RETURNED; it never substitutes for the
// CODEAPI-END result, and the two are kept as separate evidence.
// FIVE states, deliberately. An earlier version also named PROMPTING (seen A,
// waiting for B) and COMMAND_RETURNED (seen D, prompt not yet back). Both were
// the same thing to every consumer — not ready — and neither was a state of
// the shell so much as a position on the prompt ARC that both startup and
// post-command traverse:
//
//     UNKNOWN   --A..B-->  READY  --C-->  BUSY  --D-->  RETURNING  --A..B--> READY
//
// The one piece of PROMPTING that was load-bearing is the rule that a B only
// grants readiness after an A, and that is a latch on the arc rather than a
// state: it is now the internal promptStartSeen flag.
const SHELL_UNKNOWN = 'UNKNOWN';
const SHELL_READY = 'READY';
const SHELL_BUSY = 'BUSY';
const SHELL_RETURNING = 'RETURNING';
const SHELL_EXITED = 'EXITED';

// A sequence can be split across onData callbacks. The first probe saw zero
// splits in 32 chunks, which is NOT evidence it cannot happen — only that a
// small sample missed it — so an unfinished prefix is carried rather than
// dropped. The tail is bounded so a stream containing a lone ESC ] 633 ; that
// never terminates cannot grow without limit.
const OSC_TAIL_LIMIT = 4096;
const OSC_INTRO = '\x1b]633;';
const _OSC633 = /\x1b\]633;([^\x07\x1b]*)(?:\x07|\x1b\\)/g;

function createShellLifecycle() {
  return {
    shellIntegrationActive: false,
    shellState: SHELL_UNKNOWN,
    shellStateSequence: 0,
    lastShellEvent: null,
    lastShellEventAt: null,
    shellCommandActive: false,
    // Nesting depth, not a boolean: a nested integrated shell emits its own
    // C/D into the same stream, and only a D returning the depth to zero ends
    // the OUTER execution.
    commandDepth: 0,
    // The "a B only counts after an A" rule, kept as a latch rather than as a
    // state. Cleared by C and D as well as by the B that consumes it, so a
    // stale prompt start cannot satisfy a later B.
    promptStartSeen: false,
    lastCommandLine: null,
    lastShellReturnCode: null,   // $?-mapped 0/1, NOT a native exit code
    oscParserTail: '',
    // DIAGNOSTICS, not a public taxonomy. Every anomaly below already causes
    // its transition to be refused, so the protection is the refusal and the
    // tag only says which rule fired. A count plus the most recent tag answers
    // "is this terminal behaving oddly, and how" without an unbounded array
    // accumulating inside a record that can live for days.
    lifecycleAnomalyCount: 0,
    lastLifecycleAnomaly: null,
  };
}

function _anomaly(st, tag) {
  st.lifecycleAnomalyCount++;
  st.lastLifecycleAnomaly = tag;
}

// STRICT TRANSITION GRAMMAR.
//
// Reservation trusts shellState === READY, so this grammar is part of the
// authorization boundary, not merely bookkeeping. Two distinct problems:
//
// 1. NESTING, which is ordinary rather than hostile. A nested shell that also
//    has shell integration emits its OWN A/B/C/D into the same pty. Without
//    depth counting, the inner shell's D clears the outer BUSY and the outer
//    terminal looks free while the nested shell still owns it. (The probe's
//    nested `powershell -NoProfile` was silent only because it had no profile
//    and no dot-source; one with integration would not be.) C increments the
//    depth and D decrements it; only a D that returns the depth to zero ends
//    the execution.
//
// 2. ORDER. Only D leaves BUSY. An A or B arriving while a command is in
//    flight cannot make the shell ready, because a genuine prompt cycle
//    always follows a return. B additionally requires a preceding A, since
//    every measured B was immediately preceded by A (with optional P between).
//
// HONEST LIMIT: this does NOT make readiness unforgeable. Anything running in
// the shell can print these bytes, and the nonce cannot help — it is
// transmitted in plaintext in the E sequence and the shipped script states
// that any code able to run PowerShell can read it. What the grammar removes
// is accidental corruption and single stray sequences; a deliberate forger
// inside the shell remains out of scope, and readiness is therefore an
// evidence signal about a cooperating shell, not a security control.
function _transition(st, kind, arg, now) {
  st.lastShellEvent = kind;
  st.lastShellEventAt = now;
  let next = st.shellState;
  switch (kind) {
    case 'A':
      if (st.commandDepth > 0) {
        // A prompt cannot start while a command is still running.
        _anomaly(st, 'A_while_busy');
        return;
      }
      // A prompt is being DRAWN, so the shell is not accepting a submission
      // until its B arrives. Dropping out of READY here is exactly what the
      // removed PROMPTING state did; the cost of being wrong about a prompt
      // redraw is a spurious refusal, never a spurious admission.
      st.promptStartSeen = true;
      if (st.shellState === SHELL_READY) next = SHELL_UNKNOWN;
      break;
    case 'B':
      if (st.commandDepth > 0) {
        _anomaly(st, 'B_while_busy');
        return;
      }
      if (!st.promptStartSeen) {
        // Every measured B followed an A. A B on its own is not a prompt.
        _anomaly(st, 'B_without_A');
        return;
      }
      st.promptStartSeen = false;
      next = SHELL_READY;
      st.shellCommandActive = false;
      break;
    case 'C':
      st.commandDepth++;
      st.promptStartSeen = false;
      next = SHELL_BUSY;
      st.shellCommandActive = true;
      break;
    case 'D':
      if (st.commandDepth === 0) {
        // An execution ended that we never saw begin: joined mid-command, or
        // a stray sequence. Recorded; readiness is NOT claimed from it.
        _anomaly(st, 'D_without_C');
        return;
      }
      st.commandDepth--;
      st.lastShellReturnCode = (arg === undefined || arg === '') ? null : Number(arg);
      if (st.commandDepth > 0) {
        // A NESTED command returned; the outer one is still running.
        _anomaly(st, 'nested_D');
        return;
      }
      st.promptStartSeen = false;
      next = SHELL_RETURNING;
      st.shellCommandActive = false;
      break;
    case 'E':
      // Command-line metadata only. Deliberately NOT a readiness signal:
      // it says what is about to run, not that the shell is free.
      st.lastCommandLine = arg === undefined ? null : arg;
      return;
    case 'P': return;            // properties (Cwd, prompt type, …)
    default:
      _anomaly(st, 'unknown_kind:' + kind);
      return;
  }
  if (next !== st.shellState) {
    st.shellState = next;
    st.shellStateSequence++;
  }
}

function feedShellLifecycle(st, data, now) {
  if (!data) return st;
  let s = st.oscParserTail + data;
  st.oscParserTail = '';
  _OSC633.lastIndex = 0;
  let m;
  let last = 0;
  while ((m = _OSC633.exec(s)) !== null) {
    const payload = m[1];
    const semi = payload.indexOf(';');
    const kind = semi === -1 ? payload : payload.slice(0, semi);
    const arg = semi === -1 ? undefined : payload.slice(semi + 1);
    st.shellIntegrationActive = true;
    _transition(st, kind, arg, now);
    last = _OSC633.lastIndex;
  }
  const rest = s.slice(last);
  const open = rest.lastIndexOf(OSC_INTRO);
  if (open !== -1) {
    // A complete introducer with an unfinished payload.
    st.oscParserTail = rest.slice(open);
  } else {
    // The INTRODUCER ITSELF may be split. An earlier version carried only a
    // trailing ESC or ESC], so a partial introducer of length 3-6 — "\x1b]6",
    // "\x1b]63", … — matched neither branch and was silently DROPPED. Under
    // byte-at-a-time delivery that discarded every sequence and the state
    // machine never left UNKNOWN. The live probe had recorded zero splits in
    // 32 chunks, so only exhaustive split testing exposed it: a sample that
    // happens not to split proves nothing about a parser that must survive
    // splitting. Any proper prefix of the introducer is now carried.
    let carry = '';
    for (let n = Math.min(OSC_INTRO.length - 1, rest.length); n > 0; n--) {
      if (rest.slice(-n) === OSC_INTRO.slice(0, n)) { carry = rest.slice(-n); break; }
    }
    st.oscParserTail = carry;
  }
  if (st.oscParserTail.length > OSC_TAIL_LIMIT) {
    st.oscParserTail = '';
    _anomaly(st, 'osc_tail_overflow');
  }
  return st;
}

function markShellExited(st) {
  st.shellState = SHELL_EXITED;
  st.shellCommandActive = false;
  st.commandDepth = 0;
  st.promptStartSeen = false;
  st.shellStateSequence++;
  return st;
}

// True when the shell has genuinely returned to an idle prompt for this
// generation — the condition under which the extension may release a
// reservation WITHOUT the client asking. That matters because a client can
// die between submitting and releasing: the command still finishes, the shell
// still returns to READY, and the terminal must not stay reserved forever
// merely because nobody called /release.
function isShellIdle(st) {
  return !!st && st.shellState === SHELL_READY && st.commandDepth === 0;
}

// May this reservation be released automatically?
//
// Idleness ALONE is not sufficient, and assuming it was produced a real race.
// Between the atomic submit and the shell's OSC C there is a window in which
// the shell is still READY: the envelope has been written but not yet
// evaluated. The echo of that very envelope arrives as an onData event, and a
// release keyed only on idleness fires there — clearing the reservation
// before the command it belongs to has even started, so a second caller is
// admitted and two envelopes interleave in one shell.
//
// It presented as flakiness: whether a second run was refused depended on
// whether it arrived before or after the echo. Two passing runs are what
// misled me into retracting this diagnosis once already; a race that
// sometimes passes is still a race.
//
// So the execution must have been OBSERVED to start. sawBusy is set when the
// shell goes BUSY while this reservation is held, and only then does a return
// to idle mean the execution finished.
function shouldAutoRelease(rec) {
  if (!rec || !rec.activeExecutionId) return false;
  if (rec.alive !== true) return true;          // exited: nothing can finish
  if (!rec.executionSawBusy) return false;      // never started — not ours to clear
  return isShellIdle(rec.shell633);
}

// --- execution reservation (atomic terminal ownership) -------------------
//
// This lives in the EXTENSION, not in the Python client, and that placement
// is the whole point. Two `codeapi` invocations are separate OS processes, so
// a lock held in Python guards nothing across them: both would read READY,
// both would write, and their envelopes would interleave in one shell. The
// terms Map is the single owner of the record and the extension event loop is
// single-threaded, so verify-then-set here cannot be interleaved.
//
// The execution id is minted HERE, on success only, so an id exists if and
// only if the reservation that owns it succeeded. A caller-supplied id would
// let two callers claim the same terminal with different ids.
//
// Dialect is deliberately NOT checked here. Python owns dialect_for_shell
// with a tested mapping, and duplicating it in JS would recreate exactly the
// two-competing-copies defect removed from the write-shaping path. `shell` is
// immutable for a generation, so checking it before reserving cannot race.
const NOT_SUBMITTED = {
  TERMINAL_NOT_FOUND: 'TERMINAL_NOT_FOUND',
  TERMINAL_GENERATION_MISMATCH: 'TERMINAL_GENERATION_MISMATCH',
  TERMINAL_EXITED: 'TERMINAL_EXITED',
  SHELL_NOT_READY: 'SHELL_NOT_READY',
  EXECUTION_IN_PROGRESS: 'EXECUTION_IN_PROGRESS',
};

function evaluateReservation(rec, expectedGeneration) {
  if (!rec) {
    return { ok: false, reason: NOT_SUBMITTED.TERMINAL_NOT_FOUND, preSubmitState: null };
  }
  const state = (rec.shell633 && rec.shell633.shellState) || SHELL_UNKNOWN;
  // Generation FIRST. If the caller means a different incarnation of this id,
  // nothing else about this record answers their question — its liveness and
  // readiness describe a terminal they did not ask about.
  if (expectedGeneration != null && rec.generation !== expectedGeneration) {
    return { ok: false, reason: NOT_SUBMITTED.TERMINAL_GENERATION_MISMATCH,
             preSubmitState: state, generation: rec.generation };
  }
  if (rec.alive !== true) {
    return { ok: false, reason: NOT_SUBMITTED.TERMINAL_EXITED, preSubmitState: state };
  }
  // Readiness, not liveness. A terminal can be alive for days with a pager
  // owning it — that is the incident this exists to prevent.
  if (state !== SHELL_READY) {
    return { ok: false, reason: NOT_SUBMITTED.SHELL_NOT_READY, preSubmitState: state };
  }
  if (rec.activeExecutionId) {
    return { ok: false, reason: NOT_SUBMITTED.EXECUTION_IN_PROGRESS,
             preSubmitState: state, activeExecutionId: rec.activeExecutionId };
  }
  return { ok: true, reason: null, preSubmitState: state };
}

// OPERATOR RECOVERY ONLY — deliberately not part of the normal flow.
//
// Ordinary executions never release explicitly. The extension clears its own
// reservation when the shell returns to a prompt (shouldAutoRelease), which is
// what lets the lifecycle survive a client that dies mid-command: the command
// still finishes, the shell still comes back, and the terminal frees itself.
// No Python surface calls this route, and the live gate proves the normal path
// completes without it.
//
// What remains is the case automation cannot resolve: a reservation whose
// execution will never be observed to finish, because shell integration
// stopped reporting or the command was abandoned. That is an operator
// judgement, so it is spelled as one — it demands the generation AND the
// execution id AND a stated reason, and the result is always marked forced
// rather than presented as a clean release.
//
// The generation is checked FIRST and for the same reason submit checks it:
// naming an execution is not proof of owning it, and a recovery aimed at a
// previous incarnation of this terminal id must not free the current one.
function evaluateForceRelease(rec, expectedGeneration, expectedExecutionId, reason) {
  if (!rec) return { released: false, reason: NOT_SUBMITTED.TERMINAL_NOT_FOUND };
  if (expectedGeneration != null && rec.generation !== expectedGeneration) {
    return { released: false, reason: NOT_SUBMITTED.TERMINAL_GENERATION_MISMATCH,
             generation: rec.generation };
  }
  if (!rec.activeExecutionId) return { released: false, reason: 'NOT_RESERVED' };
  if (rec.activeExecutionId !== expectedExecutionId) {
    return { released: false, reason: 'NOT_OWNER',
             activeExecutionId: rec.activeExecutionId };
  }
  return { released: true, forced: true,
           shellState: (rec.shell633 && rec.shell633.shellState) || SHELL_UNKNOWN,
           releasedExecutionId: rec.activeExecutionId,
           releaseReason: reason };
}

// Strict validation for the force-release request, at the same transport
// boundary and for the same reason as submit: the bridge is independently
// reachable, so a coerced field here would be a coerced field in the only
// operation that can take a terminal away from a running execution.
function validateTermForceReleaseRequest(body) {
  let parsed;
  try { parsed = JSON.parse(body); }
  catch (e) { return { ok: false, error: 'request body is not valid JSON: ' + e.message }; }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false,
             error: 'request body must be a JSON object; got ' + jsonTypeName(parsed) };
  }
  for (const name of ['executionId', 'expectedGeneration', 'reason']) {
    if (!Object.prototype.hasOwnProperty.call(parsed, name)) {
      return { ok: false, error: "'" + name + "' is required" };
    }
    if (typeof parsed[name] !== 'string') {
      return { ok: false, error: "'" + name + "' must be a JSON string; got "
                                 + jsonTypeName(parsed[name]) };
    }
    if (!parsed[name]) {
      return { ok: false, error: "'" + name + "' must not be empty" };
    }
  }
  return { ok: true, executionId: parsed.executionId,
           expectedGeneration: parsed.expectedGeneration, reason: parsed.reason };
}

// The JSON type name, for error messages that name what actually arrived.
// typeof alone reports 'object' for null and for arrays, which is exactly the
// distinction a caller debugging a rejected body needs.
function jsonTypeName(v) {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  return typeof v;
}

// Strict validation for the TRANSPORT boundary write request.
//
// This exists because the bridge is an INDEPENDENTLY REACHABLE API, not an
// internal call from the Python runtime. extension.js serves it from its own
// http.createServer on loopback, gated by X-CODEAPI-BRIDGE-TOKEN - and that
// token is handed to the Python child through the environment
// (CODEAPI_BRIDGE_TOKEN), so it is not a Python-only secret and Python is not
// a mandatory hop. Any local caller holding the token reaches handleTermWrite
// directly. The Python route's validation is therefore NOT an end-to-end
// guarantee, and before this function existed a direct caller could send:
//
//     {"data":123}                  -> String(123) -> "123" typed into a shell
//     {"data":"","enter":"false"}   -> "false" is truthy -> a bare CR
//     {}                            -> data '', enter falsy -> nothing
//
// The last one also shows the bridge's own omission default was FALSY, the
// opposite of the route's TRUE - so the same omitted field meant different
// things depending on which door the request came through.
//
// `enter` is REQUIRED here rather than defaulted. Defaults belong at the
// semantic surface where a caller expressed an intent; this is a transport
// edge, and the only supported caller (VsCodeAdapter.term_write) always sends
// it explicitly. Types are checked, never coerced: the string "false" is
// truthy in JS, so coercion turns an explicit request for no Enter into an
// Enter.
//
// Deliberately SEPARATE from termWritePayload, which stays coercive for the
// /exec codeApiTerm facade. One helper serving both contracts would mean
// weakening the strict one to keep the compatibility one working.
// Strict validation for the atomic submit request. Same discipline as the
// write route: types are checked, never coerced, and the request is refused
// before the terminal registry is consulted.
function validateTermSubmitRequest(body) {
  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch (e) {
    return { ok: false, error: 'request body is not valid JSON: ' + e.message };
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false,
             error: 'request body must be a JSON object; got ' + jsonTypeName(parsed) };
  }
  for (const name of ['executionId', 'data']) {
    if (!Object.prototype.hasOwnProperty.call(parsed, name)) {
      return { ok: false, error: "'" + name + "' is required" };
    }
    if (typeof parsed[name] !== 'string') {
      return { ok: false, error: "'" + name + "' must be a JSON string; got "
                                 + jsonTypeName(parsed[name]) };
    }
  }
  if (!parsed.executionId) {
    return { ok: false, error: "'executionId' must not be empty" };
  }
  // expectedGeneration is REQUIRED and must be a string. Omitting it would
  // let a caller run against whatever incarnation happens to hold the id now,
  // which is exactly what the generation exists to prevent.
  if (typeof parsed.expectedGeneration !== 'string' || !parsed.expectedGeneration) {
    return { ok: false,
             error: "'expectedGeneration' is required and must be a non-empty "
                    + 'JSON string' };
  }
  return { ok: true, executionId: parsed.executionId, data: parsed.data,
           expectedGeneration: parsed.expectedGeneration };
}

function validateTermWriteRequest(body) {
  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch (e) {
    return { ok: false, error: 'request body is not valid JSON: ' + e.message };
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false,
             error: 'request body must be a JSON object; got ' + jsonTypeName(parsed) };
  }
  if (!Object.prototype.hasOwnProperty.call(parsed, 'data')) {
    return { ok: false, error: "'data' is required" };
  }
  if (typeof parsed.data !== 'string') {
    return { ok: false,
             error: "'data' must be a JSON string; got " + jsonTypeName(parsed.data) };
  }
  if (!Object.prototype.hasOwnProperty.call(parsed, 'enter')) {
    return { ok: false, error: "'enter' is required" };
  }
  if (typeof parsed.enter !== 'boolean') {
    return { ok: false,
             error: "'enter' must be a JSON boolean (true/false); got "
                    + jsonTypeName(parsed.enter) };
  }
  return { ok: true, data: parsed.data, enter: parsed.enter };
}

// codeApiTerm facade factory — vscode-free so BOTH Object.keys order and method semantics
// are unit-testable in plain Node. Built over the injected Phase-13B `terms` Map + the
// `termCreate` fn (which owns the vscode/node-pty-dependent spawn). The key insertion
// order below MUST equal the legacy Object.keys(codeApiTerm)
//   ["__pty","create","write","read","resize","list","kill"]
// (docs/TERM_CONTRACT.md §6). Phase-13 rules preserved: `wrote` = bytes ACCEPTED (not
// command success, not proof of life); read returns the RAW buffer; list() returns the
// legacy {id,pid,bytes} shape and does NOT infer or expose alive/exitCode; unknown id
// throws `Terminal <id> not found`.
function makeCodeApiTerm(terms, termCreate) {
  return {
    __pty: terms,                 // legacy exposed the registry handle under __pty
    create(opts) { return termCreate(opts || {}); },
    write(id, data, opts) {
      const rec = terms.get(id);
      if (!rec) throw new Error(`Terminal ${id} not found`);
      const p = termWritePayload(data, opts && opts.enter);
      rec.pty.write(p.data);
      return { id, wrote: p.wrote };
    },
    read(id, opts) {
      const rec = terms.get(id);
      if (!rec) throw new Error(`Terminal ${id} not found`);
      opts = opts || {};
      let buffer = rec.buffer;
      const tail = opts.tail ? parseInt(opts.tail, 10) : 0;
      if (tail > 0 && buffer.length > tail) buffer = buffer.slice(buffer.length - tail);
      const out = { id, bytes: rec.bytes, buffer };
      if (opts.clear) { rec.buffer = ''; }
      return out;
    },
    resize(id, cols, rows) {
      const rec = terms.get(id);
      if (!rec) throw new Error(`Terminal ${id} not found`);
      const c = parseInt(cols, 10);
      const r = parseInt(rows, 10);
      rec.pty.resize(c, r);
      return { id, cols: c, rows: r };
    },
    list() {
      const out = [];
      for (const rec of terms.values()) {
        out.push({ id: rec.id, pid: rec.pid, bytes: rec.bytes });
      }
      return out;
    },
    kill(id) {
      const rec = terms.get(id);
      if (!rec) throw new Error(`Terminal ${id} not found`);
      const wasAlive = rec.alive;
      try { rec.pty.kill(); } catch (_) {}
      if (rec.vscodeTerm) { try { rec.vscodeTerm.dispose(); } catch (_) {} }
      terms.delete(id);
      return { id, killed: wasAlive };
    },
  };
}

// Host-native Python runtime lookup, pure so it can be tested on every OS.
function runtimeExecutableName(platform) {
  return platform === 'win32' ? 'codeapi-runtime.exe' : 'codeapi-runtime';
}

function defaultPythonName(platform) {
  return platform === 'win32' ? 'python.exe' : 'python3';
}

// --- port allocation (bounded) ----------------------------------------------
//
// The Python service is allocated ONLY inside the reserved adapter band
// 47337-47400 — the exact range discovery scans (discovery.py ADAPTER_RANGE).
// An unbounded allocator could bind 47401+ and start a runtime that normal
// discovery never finds; exhaustion must instead fail activation VISIBLY.
// `createServer` is injectable so exhaustion is unit-testable without binding
// 64 real sockets.
const PORT_BAND_BASE = 47337;
const PORT_BAND_LIMIT = 47400;   // inclusive; 64 ports total

function pickFreePort(base, limit, createServer) {
  return new Promise((resolve, reject) => {
    if (base > limit) {
      reject(new Error(
        `adapter band ${PORT_BAND_BASE}-${limit} exhausted: no free port for the `
        + 'Python runtime. Close unused CODEAPI windows/runtimes and retry.'));
      return;
    }
    const srv = createServer();
    srv.listen(base, '127.0.0.1', () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
    srv.on('error', () => resolve(pickFreePort(base + 1, limit, createServer)));
  });
}

// Identity-aware readiness: "ready" means THE RUNTIME THIS ADAPTER LAUNCHED is
// answering — not merely that something returned HTTP 200 on the polled port.
// Guards the probe->spawn race: if another runtime grabbed the port, its /session
// carries a different codeapiRuntimeId/extensionHostPid and MUST NOT be adopted.
function sessionMatches(session, expected) {
  return !!session
    && session.serverPort === expected.serverPort
    && session.codeapiRuntimeId === expected.codeapiRuntimeId
    && session.extensionHostPid === expected.extensionHostPid;
}

module.exports = {
  FIXED_API_DESCRIPTORS, apisFromCommands, runExecBody, resolveTermDefaults, makeCodeApiTerm,
  resolveTermSurface, TERM_SURFACE_TERMINAL, TERM_SURFACE_PROCESS, termWritePayload,
  validateTermWriteRequest, validateTermSubmitRequest, jsonTypeName,
  resolveShellIntegration, isPowerShellShell, POWERSHELL_BASENAMES,
  SI_INJECTED, SI_NOT_POWERSHELL, SI_CUSTOM_ARGS, SI_SCRIPT_UNAVAILABLE,
  createShellLifecycle, feedShellLifecycle, markShellExited, isShellIdle,
  shouldAutoRelease,
  evaluateReservation, evaluateForceRelease, validateTermForceReleaseRequest,
  NOT_SUBMITTED,
  SHELL_UNKNOWN, SHELL_READY, SHELL_BUSY, SHELL_RETURNING, SHELL_EXITED,
  pickFreePort, PORT_BAND_BASE, PORT_BAND_LIMIT, sessionMatches,
  runtimeExecutableName, defaultPythonName,
};
