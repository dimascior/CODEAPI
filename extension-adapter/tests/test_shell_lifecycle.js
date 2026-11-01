// extension-adapter/tests/test_shell_lifecycle.js — R3/R4.
//
// The state machine is built from a LIVE measurement against the shipped
// shellIntegration.ps1, not from the hypothesis. Recorded orderings:
//
//   startup   P;HasRichCommandDetection · P;ContinuationPrompt · P;IsWindows
//             A · P;Cwd · B                        (READY, no preceding D)
//   command   E;<cmdline>;<nonce> · C · D;<n> · A · P;Cwd · B
//   busy      C with NO D while a foreground child owns the terminal —
//             held across 8-10s for `1..500 | more` AND a nested
//             `powershell -NoProfile`; D/A/B arrived within ~100ms of release
//
// And the correction that matters: D does NOT carry the native exit code.
// `cmd /c exit 3` produced D;1 and `throw` produced D;1, because the script
// maps $? to 0/1. D means the interactive execution RETURNED.
//
//     node extension-adapter/tests/test_shell_lifecycle.js

'use strict';

const core = require('../bridge_core');

const ESC = String.fromCharCode(27);
const BEL = String.fromCharCode(7);
const osc = (payload) => ESC + ']633;' + payload + BEL;

let pass = 0;
let fail = 0;
function check(label, cond, detail) {
  if (cond) { pass++; console.log('OK   ' + label + (detail ? '   ' + detail : '')); }
  else { fail++; console.log('FAIL ' + label + (detail ? '   ' + detail : '')); }
}

function fresh() { return core.createShellLifecycle(); }
function feed(st, s) { return core.feedShellLifecycle(st, s, Date.now()); }

function main() {
  // --- initial state ----------------------------------------------------
  let st = fresh();
  check('a new lifecycle starts UNKNOWN and inactive',
        st.shellState === core.SHELL_UNKNOWN
        && st.shellIntegrationActive === false
        && st.shellCommandActive === false);

  // --- measured startup -------------------------------------------------
  feed(st, osc('P;HasRichCommandDetection=True') + osc('P;ContinuationPrompt=>> ')
    + osc('P;IsWindows=True') + osc('A') + osc('P;Cwd=C:\\x5cUsers') + osc('B'));
  check('startup A…B establishes READY with no preceding D',
        st.shellState === core.SHELL_READY && st.shellIntegrationActive === true,
        'state=' + st.shellState);

  // --- measured command cycle -------------------------------------------
  const nonce = 'c9029a00-17b4-4e85-ac62-b648ec4c950c';
  feed(st, osc('E;cmd /c exit 0;' + nonce));
  check('E records the command line and does NOT change state',
        st.lastCommandLine === 'cmd /c exit 0;' + nonce
        && st.shellState === core.SHELL_READY,
        'state=' + st.shellState);

  feed(st, osc('C'));
  check('C -> BUSY with a command active',
        st.shellState === core.SHELL_BUSY && st.shellCommandActive === true);

  feed(st, osc('D;0'));
  check('D -> RETURNING, command no longer active',
        st.shellState === core.SHELL_RETURNING
        && st.shellCommandActive === false && st.lastShellReturnCode === 0);

  feed(st, osc('A'));
  check('A after D latches the prompt start but is NOT yet ready',
        st.shellState !== core.SHELL_READY && st.promptStartSeen === true,
        'state=' + st.shellState);
  feed(st, osc('P;Cwd=C:\\x5cUsers') + osc('B'));
  check('B -> READY again', st.shellState === core.SHELL_READY);

  // The collapse of PROMPTING into a latch must not weaken the arc: a prompt
  // being REDRAWN while READY still has to leave READY until its B arrives.
  feed(st, osc('A'));
  check('A while READY leaves READY until the B completes the arc',
        st.shellState !== core.SHELL_READY, 'state=' + st.shellState);
  feed(st, osc('B'));
  check('and that B restores READY', st.shellState === core.SHELL_READY);

  // --- THE decisive property: C without D stays BUSY --------------------
  st = fresh();
  feed(st, osc('A') + osc('B'));
  feed(st, osc('E;1..500 | more;' + nonce) + osc('C'));
  check('foreground child: BUSY once C arrives',
        st.shellState === core.SHELL_BUSY);
  // Lots of intervening output — the pager painting the screen — but no D.
  for (let i = 0; i < 50; i++) feed(st, 'line ' + i + '\r\n');
  check('foreground child: still BUSY after 50 chunks with no D',
        st.shellState === core.SHELL_BUSY && st.shellCommandActive === true,
        'this is the state the original incident was in');
  feed(st, osc('D;0') + osc('A') + osc('P;Cwd=C:\\x5cx') + osc('B'));
  check('foreground child released -> READY again',
        st.shellState === core.SHELL_READY && st.shellCommandActive === false);
  check('READY -> BUSY -> READY is a complete transition',
        st.shellStateSequence >= 4, 'seq=' + st.shellStateSequence);

  // --- D is NOT the native exit code ------------------------------------
  st = fresh();
  feed(st, osc('A') + osc('B') + osc('C') + osc('D;1'));
  check('D;1 records a RETURN code, not a native exit code',
        st.lastShellReturnCode === 1,
        'cmd /c exit 3 measured as D;1 — D never substitutes for CODEAPI-END');

  // --- split sequences across chunks ------------------------------------
  //
  // The live probe recorded zero splits across 32 chunks, which is not
  // evidence that it cannot happen — only that a small sample missed it.
  // Every split point of a full cycle is exercised here instead.
  const full = osc('A') + osc('B') + osc('C') + osc('D;0') + osc('A') + osc('B');
  let worstOk = true;
  for (let cut = 1; cut < full.length; cut++) {
    const s = fresh();
    feed(s, full.slice(0, cut));
    feed(s, full.slice(cut));
    if (s.shellState !== core.SHELL_READY) { worstOk = false; break; }
  }
  check('every split point of a full cycle still ends READY',
        worstOk, full.length - 1 + ' split positions exercised');

  // Byte-at-a-time is the pathological case.
  st = fresh();
  for (const ch of full) feed(st, ch);
  check('byte-at-a-time delivery still ends READY',
        st.shellState === core.SHELL_READY, 'state=' + st.shellState);

  // A split INSIDE the payload, which is where a naive parser drops events.
  st = fresh();
  feed(st, ESC + ']633;E;some comm');
  check('a half-delivered sequence is carried, not dropped',
        st.oscParserTail.length > 0 && st.lastCommandLine === null);
  feed(st, 'and here' + BEL + osc('C'));
  check('the carried sequence completes on the next chunk',
        st.lastCommandLine === 'some command here'
        && st.shellState === core.SHELL_BUSY,
        'cmdline=' + JSON.stringify(st.lastCommandLine));

  // --- anomalies and bounds ---------------------------------------------
  //
  // Anomalies are diagnostics: the PROTECTION is that the transition is
  // refused, and each check below asserts the refusal first and the tag
  // second. A count plus the most recent tag replaced the former unbounded
  // array, so the assertions name the last one rather than search a list.
  st = fresh();
  feed(st, osc('A') + osc('B') + osc('D;0'));
  check('D with no preceding C is recorded as an anomaly',
        st.lastLifecycleAnomaly === 'D_without_C'
        && st.lifecycleAnomalyCount === 1
        && st.shellState === core.SHELL_READY,
        'last=' + st.lastLifecycleAnomaly);

  // --- strict grammar: readiness cannot be manufactured -----------------
  //
  // Reservation trusts shellState === READY, so these are authorization
  // boundary cases, not bookkeeping.
  console.log('\n-- strict transition grammar --');
  const busyState = () => {
    const s = fresh();
    feed(s, osc('A') + osc('B') + osc('E;more x;n') + osc('C'));
    return s;
  };

  st = busyState();
  feed(st, osc('B'));
  check('BUSY + stray B stays BUSY, never READY',
        st.shellState === core.SHELL_BUSY
        && st.lastLifecycleAnomaly === 'B_while_busy',
        'state=' + st.shellState);

  st = busyState();
  feed(st, osc('A'));
  check('BUSY + stray A stays BUSY',
        st.shellState === core.SHELL_BUSY
        && st.lastLifecycleAnomaly === 'A_while_busy');

  st = busyState();
  feed(st, osc('A') + osc('B'));
  check('BUSY + a full fake prompt cycle still stays BUSY',
        st.shellState === core.SHELL_BUSY,
        'a pager printing A/B cannot make the shell eligible');

  st = fresh();
  feed(st, osc('B'));
  check('B with no preceding A does not establish READY',
        st.shellState !== core.SHELL_READY
        && st.lastLifecycleAnomaly === 'B_without_A',
        'state=' + st.shellState);

  // The same rule after a command, where the latch is what carries it: a D
  // leaves RETURNING, and a B arriving without its A must not shortcut to
  // READY. This is the case the removed COMMAND_RETURNED state used to hold.
  st = fresh();
  feed(st, osc('A') + osc('B') + osc('C') + osc('D;0'));
  check('after D the shell is RETURNING with the prompt latch clear',
        st.shellState === core.SHELL_RETURNING && st.promptStartSeen === false);
  feed(st, osc('B'));
  check('RETURNING + a B with no A stays unready',
        st.shellState === core.SHELL_RETURNING
        && st.lastLifecycleAnomaly === 'B_without_A',
        'state=' + st.shellState);

  // Nesting: a nested integrated shell emits its OWN C/D into this stream.
  st = fresh();
  feed(st, osc('A') + osc('B') + osc('C'));       // outer command starts
  feed(st, osc('C'));                              // nested shell's command
  check('nested C keeps the shell BUSY',
        st.shellState === core.SHELL_BUSY && st.commandDepth === 2,
        'depth=' + st.commandDepth);
  feed(st, osc('D;0'));                            // nested command returned
  check('a nested D does NOT end the outer execution',
        st.shellState === core.SHELL_BUSY && st.commandDepth === 1
        && st.lastLifecycleAnomaly === 'nested_D',
        'without depth counting the outer terminal would look free');
  feed(st, osc('D;0'));
  check('the outer D at depth zero ends the execution',
        st.shellState === core.SHELL_RETURNING && st.commandDepth === 0);
  feed(st, osc('A') + osc('B'));
  check('and the prompt cycle then restores READY',
        st.shellState === core.SHELL_READY);

  check('isShellIdle is false while BUSY and true at an idle prompt',
        core.isShellIdle(busyState()) === false && core.isShellIdle(st) === true);

  st = fresh();
  feed(st, ESC + ']633;' + 'x'.repeat(9000));
  check('an unterminated sequence cannot grow the tail without bound',
        st.oscParserTail === ''
        && st.lastLifecycleAnomaly === 'osc_tail_overflow',
        'tail=' + st.oscParserTail.length);

  // The whole point of the count: a record that lives for days must not
  // accumulate one string per stray sequence.
  st = fresh();
  for (let i = 0; i < 200; i++) feed(st, osc('B'));
  check('repeated anomalies cost two scalars, not an unbounded array',
        st.lifecycleAnomalyCount === 200
        && st.lastLifecycleAnomaly === 'B_without_A'
        && st.anomalies === undefined,
        'count=' + st.lifecycleAnomalyCount);

  // --- exit --------------------------------------------------------------
  st = fresh();
  feed(st, osc('A') + osc('B'));
  core.markShellExited(st);
  check('terminal exit -> EXITED, never READY',
        st.shellState === core.SHELL_EXITED && st.shellCommandActive === false);

  // --- readiness is not liveness ----------------------------------------
  st = fresh();
  feed(st, osc('A') + osc('B') + osc('C'));
  check('a BUSY shell is not READY even though its terminal is alive',
        st.shellState !== core.SHELL_READY,
        'tmbrwfix1 was alive for days while a pager owned it');

  // --- execution reservation (atomic terminal ownership) ----------------
  //
  // A record shaped like the extension's, with only the fields the verdict
  // reads. The reservation lives in the extension because two `codeapi`
  // invocations are separate PROCESSES — a Python-side lock guards nothing
  // across them.
  const recFor = (over) => Object.assign({
    id: 'tm1', generation: 'g-1', alive: true,
    activeExecutionId: null,
    shell633: (() => { const s = fresh(); feed(s, osc('A') + osc('B')); return s; })(),
  }, over || {});

  console.log('\n-- reservation --');
  let v = core.evaluateReservation(recFor(), 'g-1');
  check('READY, right generation, unowned -> eligible',
        v.ok === true && v.preSubmitState === core.SHELL_READY);

  v = core.evaluateReservation(undefined, 'g-1');
  check('missing terminal -> TERMINAL_NOT_FOUND',
        v.ok === false && v.reason === core.NOT_SUBMITTED.TERMINAL_NOT_FOUND);

  v = core.evaluateReservation(recFor(), 'g-OLD');
  check('generation mismatch -> TERMINAL_GENERATION_MISMATCH',
        v.ok === false
        && v.reason === core.NOT_SUBMITTED.TERMINAL_GENERATION_MISMATCH,
        'ids are reusable; a generation names one incarnation');

  // Checked BEFORE liveness on purpose: if the caller means a different
  // incarnation, this record's aliveness answers a question they did not ask.
  v = core.evaluateReservation(recFor({ alive: false }), 'g-OLD');
  check('generation is checked before liveness',
        v.reason === core.NOT_SUBMITTED.TERMINAL_GENERATION_MISMATCH);

  v = core.evaluateReservation(recFor({ alive: false }), 'g-1');
  check('dead terminal -> TERMINAL_EXITED',
        v.ok === false && v.reason === core.NOT_SUBMITTED.TERMINAL_EXITED);

  const busy = fresh();
  feed(busy, osc('A') + osc('B') + osc('E;more x;n') + osc('C'));
  v = core.evaluateReservation(recFor({ shell633: busy }), 'g-1');
  check('pager holding the foreground -> SHELL_NOT_READY',
        v.ok === false && v.reason === core.NOT_SUBMITTED.SHELL_NOT_READY
        && v.preSubmitState === core.SHELL_BUSY,
        'this is the original incident, refused before any write');

  v = core.evaluateReservation(recFor({ activeExecutionId: 'codeapi-aaa' }), 'g-1');
  check('already reserved -> EXECUTION_IN_PROGRESS',
        v.ok === false && v.reason === core.NOT_SUBMITTED.EXECUTION_IN_PROGRESS
        && v.activeExecutionId === 'codeapi-aaa');

  // THE concurrency theorem. Two callers evaluate against the SAME record;
  // the first assignment must make the second ineligible.
  const shared = recFor();
  const a = core.evaluateReservation(shared, 'g-1');
  shared.activeExecutionId = 'codeapi-A';          // what the extension does
  const b = core.evaluateReservation(shared, 'g-1');
  check('two callers: A reserves, B is refused',
        a.ok === true && b.ok === false
        && b.reason === core.NOT_SUBMITTED.EXECUTION_IN_PROGRESS,
        'envelopes cannot interleave in one shell');

  // --- automatic release must not fire before the execution starts ------
  //
  // The race this closes: between the atomic submit and the shell's OSC C,
  // the shell is STILL READY — the envelope is written but not yet evaluated.
  // The echo of that envelope arrives as an onData event, and a release keyed
  // only on idleness fires there, clearing the reservation before its own
  // command has begun and letting a second caller in. It presented as
  // flakiness because it depended on whether the second run arrived before or
  // after the echo, and two passing runs once misled me into retracting this.
  console.log('\n-- automatic release --');
  const justSubmitted = recFor({ activeExecutionId: 'codeapi-A' });
  justSubmitted.executionSawBusy = false;          // C has not arrived yet
  check('auto-release does NOT fire on the echo, before OSC C',
        core.shouldAutoRelease(justSubmitted) === false,
        'shell is still READY here; releasing would admit a second caller');

  const running = recFor({ activeExecutionId: 'codeapi-A', shell633: busy });
  running.executionSawBusy = true;
  check('auto-release does not fire while the command is running',
        core.shouldAutoRelease(running) === false);

  const finished = recFor({ activeExecutionId: 'codeapi-A' });
  finished.executionSawBusy = true;                // C was seen, prompt is back
  check('auto-release fires once the execution started AND the prompt returned',
        core.shouldAutoRelease(finished) === true);

  const deadHeld = recFor({ activeExecutionId: 'codeapi-A', alive: false });
  deadHeld.executionSawBusy = false;
  check('a dead terminal releases even if its execution never started',
        core.shouldAutoRelease(deadHeld) === true,
        'nothing can finish on an exited terminal');

  check('an unreserved terminal is not auto-released',
        core.shouldAutoRelease(recFor()) === false);

  // --- force release: OPERATOR RECOVERY ONLY ----------------------------
  //
  // There is no ordinary release any more. The conditional variant this
  // replaced ("release only once the shell is READY again") was doing the
  // same job as the automatic release above, from a route no Python surface
  // ever called — two mechanisms for one outcome, and the one nobody used
  // could still be aimed at the wrong terminal. What is left is the case
  // automation cannot resolve: an execution that will never be observed to
  // finish. That is an operator judgement, so it demands the generation, the
  // execution id AND a reason, and always reports itself as forced.
  console.log('\n-- force release (operator recovery) --');
  const held = recFor({ activeExecutionId: 'codeapi-A' });
  let r = core.evaluateForceRelease(held, 'g-1', 'codeapi-B', 'stuck');
  check('a non-owner cannot force-release',
        r.released === false && r.reason === 'NOT_OWNER');

  r = core.evaluateForceRelease(recFor({ activeExecutionId: 'codeapi-A' }),
                                'g-OLD', 'codeapi-A', 'stuck');
  check('a stale generation cannot force-release the current terminal',
        r.released === false
        && r.reason === core.NOT_SUBMITTED.TERMINAL_GENERATION_MISMATCH,
        'recovery aimed at a previous incarnation must not free this one');

  r = core.evaluateForceRelease(recFor(), 'g-1', 'codeapi-A', 'stuck');
  check('an unreserved terminal reports NOT_RESERVED',
        r.released === false && r.reason === 'NOT_RESERVED');

  r = core.evaluateForceRelease(
    recFor({ activeExecutionId: 'codeapi-A', shell633: busy }),
    'g-1', 'codeapi-A', 'client died holding the terminal');
  check('force releases an abandoned reservation, mid-command, and says so',
        r.released === true && r.forced === true
        && r.releaseReason === 'client died holding the terminal',
        'a terminal must not stay reserved forever');

  const fr = (o) => core.validateTermForceReleaseRequest(
    typeof o === 'string' ? o : JSON.stringify(o));
  check('force release: all three fields required',
        fr({ executionId: 'a', expectedGeneration: 'g-1', reason: 'x' }).ok === true
        && fr({ executionId: 'a', expectedGeneration: 'g-1' }).ok === false
        && fr({ executionId: 'a', reason: 'x' }).ok === false
        && fr({ expectedGeneration: 'g-1', reason: 'x' }).ok === false);
  check('force release: no field may be empty or coerced',
        fr({ executionId: 'a', expectedGeneration: 'g-1', reason: '' }).ok === false
        && fr({ executionId: 'a', expectedGeneration: 'g-1', reason: 1 }).ok === false
        && fr({ executionId: 'a', expectedGeneration: '', reason: 'x' }).ok === false,
        'the one operation that can take a terminal from a running execution');
  check('force release: malformed and non-object bodies refused',
        fr('{"reason":').ok === false && fr('[]').ok === false
        && fr('null').ok === false);

  // --- atomic submit request validation ---------------------------------
  console.log('\n-- atomic submit request --');
  const sub = (o) => core.validateTermSubmitRequest(
    typeof o === 'string' ? o : JSON.stringify(o));

  check('submit: valid request accepted',
        sub({ executionId: 'codeapi-a', data: 'x', expectedGeneration: 'g-1' }).ok === true);
  check('submit: expectedGeneration is REQUIRED',
        sub({ executionId: 'codeapi-a', data: 'x' }).ok === false,
        'omitting it would run against whatever incarnation holds the id now');
  check('submit: executionId required and non-empty',
        sub({ data: 'x', expectedGeneration: 'g-1' }).ok === false
        && sub({ executionId: '', data: 'x', expectedGeneration: 'g-1' }).ok === false);
  check('submit: data must be a string, never coerced',
        sub({ executionId: 'a', data: 123, expectedGeneration: 'g-1' }).ok === false);
  check('submit: malformed body refused',
        sub('{"executionId":').ok === false);
  check('submit: non-object root refused',
        sub('[]').ok === false && sub('null').ok === false);

  // --- shell-integration bootstrap (P1/P2) ------------------------------
  //
  // P1 measured the production path: args defaulted to [] and env was the
  // extension host's, so no VSCODE_NONCE and no dot-source — production
  // PowerShell terminals had NO integration. With readiness gating that would
  // leave shellState UNKNOWN forever and refuse every run, so this bootstrap
  // is load-bearing rather than a nicety.
  console.log('\n-- shell integration bootstrap --');
  const ctx = { scriptPath: 'C:\\vsc\\shellIntegration.ps1', scriptExists: true,
                nonce: 'n-1' };

  let si = core.resolveShellIntegration({ shell: 'powershell.exe', args: [] }, ctx);
  check('powershell + default args -> injected',
        si.mode === core.SI_INJECTED
        && si.args[0] === '-NoExit' && si.args[1] === '-Command'
        && si.args[2].indexOf('shellIntegration.ps1') !== -1
        && si.env.VSCODE_NONCE === 'n-1' && si.env.TERM_PROGRAM === 'vscode',
        'args=' + JSON.stringify(si.args));

  si = core.resolveShellIntegration({ shell: 'pwsh', args: [] }, ctx);
  check('pwsh is also bootstrapped', si.mode === core.SI_INJECTED);

  si = core.resolveShellIntegration(
    { shell: 'C:\\WINDOWS\\system32\\cmd.exe', args: [] }, ctx);
  check('cmd is not bootstrapped and its args are untouched',
        si.mode === core.SI_NOT_POWERSHELL && si.args.length === 0
        && Object.keys(si.env).length === 0);

  si = core.resolveShellIntegration(
    { shell: 'powershell.exe', args: ['-File', 'x.ps1'] }, ctx);
  check('caller-supplied args are NEVER altered',
        si.mode === core.SI_CUSTOM_ARGS
        && si.args.join(' ') === '-File x.ps1'
        && Object.keys(si.env).length === 0,
        'silently changing what a caller runs is worse than an unready terminal');

  si = core.resolveShellIntegration({ shell: 'powershell.exe', args: [] },
                                    { scriptPath: 'x', scriptExists: false });
  check('a missing script degrades to no integration, and says so',
        si.mode === core.SI_SCRIPT_UNAVAILABLE && si.args.length === 0,
        'the terminal will be UNKNOWN, not silently assumed ready');

  check('the bootstrap basename set matches the execution dialect set',
        core.POWERSHELL_BASENAMES.slice().sort().join(',')
        === ['powershell.exe', 'powershell', 'pwsh.exe', 'pwsh'].sort().join(','),
        'mirrors python _POWERSHELL_BASENAMES; they answer different '
        + 'questions but must agree on what PowerShell is');

  console.log('\n' + '='.repeat(54));
  console.log('shell lifecycle: ' + pass + '/' + (pass + fail) + ' passed');
  process.exit(fail === 0 ? 0 : 1);
}

main();
