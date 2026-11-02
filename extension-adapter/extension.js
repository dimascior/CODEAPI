// extension-adapter/extension.js — the LIVE VS Code adapter. This IS
// package.json.main: it activates on startup, launches + supervises the Python
// CODEAPI runtime, and exposes the token-gated bridge so the Python runtime can
// perform the IRREDUCIBLE vscode.* operations. Python NEVER calls vscode.*
// directly — it calls this bridge over loopback HTTP
// (docs/ADAPTER_BRIDGE_CONTRACT.md).
//
// Hand-written JavaScript on purpose (no TypeScript). CommonJS. Phase labels in
// comments below are provenance (which change introduced what), not status.

const vscode = require('vscode');
const http = require('http');
const crypto = require('crypto');
const { spawn } = require('child_process');
const path = require('path');
const bridgeCore = require('./bridge_core');   // vscode-free: /exec globals + /apis shaping

// The Python service binds ONLY in the reserved adapter band (47337-47400),
// never in the VS Code window band (3637-3646) — permanent policy so legacy
// runtimes and adapter runtimes stay distinguishable during discovery.
const PY_PORT_BASE = 47337;          // reserved adapter band 47337-47400
const HEALTH_TIMEOUT_MS = 15000;

let child = null;
let bridge = null;
let channel = null;
let bridgeToken = null;   // per-launch token; every bridge request must present it
let adapterPort = null;   // ephemeral bridge port; kept so restartRuntime reuses the bridge
let runtimeId = null;     // codeapiRuntimeId: fresh UUID per adapter activation (never persisted)

// --- lifecycle -------------------------------------------------------------

async function activate(context) {
  channel = vscode.window.createOutputChannel('CODEAPI Runtime');

  // 0. Mint the per-launch bridge token (ADAPTER_BRIDGE_CONTRACT.md §2.4). Loopback
  //    + ephemeral port is necessary but NOT sufficient; every bridge request must
  //    carry this token via X-CODEAPI-BRIDGE-TOKEN or it is rejected 403 before any
  //    vscode.* runs. Generated here, handed to Python as CODEAPI_BRIDGE_TOKEN.
  bridgeToken = crypto.randomBytes(32).toString('hex');

  // 0b. Mint codeapiRuntimeId: unique for THIS live adapter instance. This is the
  //     per-window disambiguator CODEAPI itself controls — unlike the workspace id
  //     below it is never persisted, so it cannot collide across windows or race
  //     on first activation; it simply rotates on reload (rediscover after reload).
  runtimeId = crypto.randomUUID ? crypto.randomUUID()
    : crypto.randomBytes(16).toString('hex');

  // 1. Start the adapter bridge (Python -> vscode.* callbacks). Ephemeral port.
  adapterPort = await startBridge();

  // 2. Register the contributed commands (package.json contributes.commands). These
  //    were declared but never registered before — invoking them errored. Both are
  //    real: showRuntimeLog reveals the output channel; restartRuntime restarts the
  //    Python child (reusing the already-running bridge/token). Auto-disposed on
  //    deactivate via context.subscriptions.
  context.subscriptions.push(
    vscode.commands.registerCommand('codeapi.showRuntimeLog', () => channel.show(true)),
    vscode.commands.registerCommand('codeapi.restartRuntime', () => startRuntime(context)),
  );

  // 3. Launch the Python runtime.
  await startRuntime(context);
}

// Durable WORKSPACE identity (codeapiWorkspaceId): a UUID created on first activation
// and persisted in workspaceState, reused on every later reload for this workspace.
// Semantically a WORKSPACE id, not a window id — workspaceState is scoped per
// workspace, so all windows on one workspace share it (grouping key, not a unique
// target). Also exported as CODEAPI_WINDOW_ID -> /session.codeapiWindowId for
// backward compatibility; that legacy name is documented as a workspace identity.
// For unique live-window targeting use codeapiRuntimeId / vscodeSessionId instead.
//
// THREAT MODEL — first-activation race: two windows activating simultaneously on a
// workspace with NO stored id can both observe "missing", mint different UUIDs, hand
// them to their runtimes, and race the persist; the stored value converges to the
// last write after reload. Awaiting update() (below) narrows the window but CANNOT
// make read-create-write atomic across extension hosts — which is exactly why unique
// targeting is delegated to the never-persisted codeapiRuntimeId.
async function getOrCreateWorkspaceId(context) {
  // Storage key kept as 'codeapi.windowId' so existing installs retain their id.
  let id = context.workspaceState.get('codeapi.windowId');
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID()
      : crypto.randomBytes(16).toString('hex');
    await context.workspaceState.update('codeapi.windowId', id);
  }
  return id;
}

// Spawn (or re-spawn) the Python runtime child against the existing bridge. Idempotent
// and restart-safe: stops any current child first, picks a fresh service port outside
// the VS Code window band, and waits for /health. Called by activate() and by the
// codeapi.restartRuntime command.
async function startRuntime(context) {
  stopChild();
  const codeapiWorkspaceId = await getOrCreateWorkspaceId(context);
  // Probe -> spawn -> verify, with the race CLOSED end to end:
  //  - the probe picks a candidate INSIDE the bounded band (47337-47400, the exact
  //    range discovery scans); exhaustion fails activation visibly;
  //  - the Python runtime binds EXACTLY the candidate or exits ALLOC_EXIT_CODE
  //    (it never falls forward to a different port);
  //  - readiness is IDENTITY-AWARE: /session must echo THIS adapter's
  //    codeapiRuntimeId/extensionHostPid on the expected serverPort. A 200 from
  //    some OTHER runtime that grabbed the port is detected and retried, never
  //    adopted as our child.
  let searchBase = PY_PORT_BASE;
  for (;;) {
    let pyPort;
    try {
      pyPort = await bridgeCore.pickFreePort(
        searchBase, bridgeCore.PORT_BAND_LIMIT, () => http.createServer());
    } catch (e) {
      channel.appendLine(`[runtime NOT started: ${errMsg(e)}]`);
      vscode.window.showErrorMessage(`CODEAPI: ${errMsg(e)}`);
      return;
    }
    const outcome = await launchAttempt(context, pyPort, codeapiWorkspaceId);
    if (outcome === 'ready') { return; }
    if (outcome === 'retry') { searchBase = pyPort + 1; continue; }
    return;   // 'fatal' — already logged/notified by launchAttempt
  }
}

// Distinct Python exit code for "requested port could not be bound" (see
// codeapi_vscode.runtime.ALLOCATION_EXIT_CODE). Anything else is a real failure.
const ALLOC_EXIT_CODE = 3;

// One spawn + identity-verified readiness attempt on an exact port.
// Returns 'ready' | 'retry' (port lost to a racer; try the next candidate) | 'fatal'.
async function launchAttempt(context, pyPort, codeapiWorkspaceId) {
  // Spawn the Python runtime with an ARGUMENT ARRAY (never a shell string / .bat shim —
  // cmd.exe re-parses argv and mangles quotes on Windows).
  const { exe, args, cwd, pythonPath } = resolveRuntimeCommand(context, pyPort, adapterPort);
  const wsUri = (vscode.workspace.workspaceFolders || [])[0]?.uri?.toString() || '';
  // Adapter-pinned identity (additive /session fields, docs/WIRE_CONTRACT.md):
  // codeapiWorkspaceId is durable across reloads but shared per workspace (grouping);
  // codeapiRuntimeId is unique to this live adapter activation (targeting);
  // vscodeSessionId is a VS Code-provided session correlation signal (secondary key —
  // not a CODEAPI-owned uniqueness guarantee); extensionHostPid is THIS extension-host
  // pid (differs from the Python runtime pid).
  // CODEAPI_WINDOW_ID carries the workspace id for backward compatibility.
  const env = {
    ...process.env,
    CODEAPI_PY_PORT: String(pyPort),
    CODEAPI_EXTENSION_PORT: String(adapterPort),
    CODEAPI_BRIDGE_TOKEN: bridgeToken,
    CODEAPI_WORKSPACE_URI: wsUri,
    CODEAPI_EXTENSION_PATH: context.extensionPath,
    CODEAPI_VSCODE_SESSION_ID: vscode.env.sessionId,
    CODEAPI_EXTENSION_HOST_PID: String(process.pid),
    CODEAPI_WINDOW_ID: codeapiWorkspaceId,
    CODEAPI_WORKSPACE_ID: codeapiWorkspaceId,
    CODEAPI_RUNTIME_ID: runtimeId,
  };
  // Dev mode: make codeapi_vscode importable (the package lives in <repo>/python).
  // Packaged mode leaves pythonPath null (the exe is self-contained).
  // PREPEND rather than replace: an inherited PYTHONPATH may carry something
  // the interpreter legitimately needs, and clobbering it would trade one
  // import failure for another. <repo>/python goes first so the working-tree
  // package always wins over anything else on the path.
  if (pythonPath) {
    const prior = process.env.PYTHONPATH;
    env.PYTHONPATH = prior ? (pythonPath + path.delimiter + prior) : pythonPath;
  }
  // Log exactly what is being launched so a failed dev-load is self-diagnosing in the
  // "CODEAPI Runtime" output channel (which python, which args, which cwd).
  channel.appendLine(`[starting runtime: ${exe} ${args.join(' ')} (cwd=${cwd})]`);
  child = spawn(exe, args, { env, cwd, windowsHide: true });
  let exitCode = null;
  // A missing / unresolvable python (spawn ENOENT) fires 'error', NOT 'exit'. Without
  // this handler the failure was invisible (only the readiness timeout showed, and the
  // unhandled 'error' could bubble). Log it explicitly with the resolved exe + a fix hint.
  child.on('error', (e) => {
    channel.appendLine(`[runtime spawn error: ${errMsg(e)} — exe='${exe}'. Set the `
      + `"codeapi.pythonPath" setting or CODEAPI_PYTHON to a real Python interpreter.]`);
    vscode.window.showErrorMessage(
      `CODEAPI runtime could not launch Python ('${exe}'): ${errMsg(e)}`);
  });
  if (child.stdout) { child.stdout.on('data', (d) => channel.append(d.toString())); }
  if (child.stderr) { child.stderr.on('data', (d) => channel.append(d.toString())); }
  child.on('exit', (code) => {
    exitCode = code;
    channel.appendLine(`[runtime exited: ${code}]`);
  });

  // Identity-aware readiness: /session must be OUR runtime on OUR port.
  const expected = {
    serverPort: pyPort,
    codeapiRuntimeId: runtimeId,
    extensionHostPid: process.pid,
  };
  const verdict = await waitForOwnedSession(pyPort, expected, HEALTH_TIMEOUT_MS,
                                            () => exitCode);
  if (verdict === 'ready') {
    channel.appendLine(`[runtime ready on 127.0.0.1:${pyPort} (identity verified)]`);
    return 'ready';
  }
  if (verdict === 'port-taken') {
    channel.appendLine(`[port ${pyPort} was taken before the runtime could bind — `
      + 'retrying the next candidate]');
    return 'retry';
  }
  if (verdict === 'foreign') {
    channel.appendLine(`[a DIFFERENT runtime is answering on ${pyPort} — not adopting `
      + 'it; retrying the next candidate]');
    stopChild();
    return 'retry';
  }
  if (verdict === 'died') {
    channel.appendLine(`[runtime exited (code ${exitCode}) before becoming ready]`);
    vscode.window.showErrorMessage('CODEAPI runtime failed to start (see output).');
    stopChild();
    return 'fatal';
  }
  channel.appendLine('[runtime did not become ready in time]');
  vscode.window.showErrorMessage('CODEAPI runtime failed to start (see output).');
  stopChild();
  return 'fatal';
}

// Poll GET /session until it is verifiably OURS (bridgeCore.sessionMatches), the
// child exits, or the deadline passes.
// Resolves 'ready' | 'port-taken' (child exited ALLOC_EXIT_CODE) | 'died'
// (child exited any other way) | 'foreign' (a session answered that is NOT ours)
// | 'timeout'.
function waitForOwnedSession(port, expected, timeoutMs, getExitCode) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const tick = () => {
      const ec = getExitCode();
      if (ec !== null && ec !== undefined && ec !== 0) {
        resolve(ec === ALLOC_EXIT_CODE ? 'port-taken' : 'died');
        return;
      }
      const req = http.get({ host: '127.0.0.1', port, path: '/session', timeout: 1000 },
        (res) => {
          let raw = '';
          res.on('data', (d) => { raw += d; });
          res.on('end', () => {
            let sess = null;
            try { sess = JSON.parse(raw); } catch (_) { /* not JSON yet */ }
            if (sess && bridgeCore.sessionMatches(sess, expected)) { resolve('ready'); return; }
            if (sess && typeof sess === 'object' && 'serverPort' in sess) { resolve('foreign'); return; }
            next();
          });
        });
      req.on('error', next);
      function next() {
        if (Date.now() < deadline) { setTimeout(tick, 300); } else { resolve('timeout'); }
      }
    };
    tick();
  });
}

function deactivate() {
  // Shutdown rule: prefer graceful /shutdown, then kill.
  stopChild();
  for (const rec of terms.values()) { try { rec.pty.kill(); } catch (_) {} }
  terms.clear();
  if (bridge) { try { bridge.close(); } catch (_) {} bridge = null; }
  bridgeToken = null;
}

// --- Python -> vscode.* bridge (Phase 12C) ----------------------------------
//
// The ONLY place vscode.* executes. One uniform envelope:
//   success 200    -> { ok: true, value }   value OMITTED when the result is
//                                            undefined; present-as-null for null
//   error   4xx/5xx -> { error: "..." }
// Every request MUST carry X-CODEAPI-BRIDGE-TOKEN === bridgeToken, else 403
// { error: "forbidden" } BEFORE any body read or vscode.* call. Loopback only.
// Public wrap/unwrap shaping is Python's job (ADAPTER_BRIDGE_CONTRACT.md §4).
// /vscode/term/* IS served here as of Phase 13B (typed terminal handlers below).

function startBridge() {
  return new Promise((resolve) => {
    bridge = http.createServer((req, res) => {
      const token = req.headers['x-codeapi-bridge-token'];
      if (!bridgeToken || token !== bridgeToken) {
        return sendJson(res, 403, { error: 'forbidden' });
      }
      readBody(req)
        .then((body) => dispatch(req, res, body))
        .catch((e) => sendJson(res, 500, { error: errMsg(e) }));
    });
    bridge.listen(0, '127.0.0.1', () => resolve(bridge.address().port));
  });
}

function dispatch(req, res, body) {
  const method = req.method;
  const url = (req.url || '').split('?')[0];
  if (method === 'POST' && url === '/vscode/exec') return handleExec(res, body);
  if (method === 'POST' && url === '/vscode/command') return handleCommand(res, body);
  if (method === 'GET' && url === '/vscode/workspace/folders') return handleWorkspaceFolders(res);
  if (method === 'GET' && url === '/vscode/editor/active') return handleEditorActive(res);
  if (method === 'POST' && url === '/vscode/window/message') return handleMessage(res, body);
  if (method === 'GET' && url === '/vscode/apis') return handleApis(res);
  // --- terminal routes (Phase 13B) ---
  if (method === 'GET' && url === '/vscode/term/list') return handleTermList(res);
  if (method === 'POST' && url === '/vscode/term/create') return handleTermCreate(res, body);
  const tm = url.match(
    /^\/vscode\/term\/([^/]+)(\/write|\/read|\/resize|\/submit|\/release)?$/);
  if (tm) {
    const id = decodeURIComponent(tm[1]);
    const sub = tm[2];
    if (method === 'POST' && sub === '/submit') return handleTermSubmit(res, id, body);
    if (method === 'POST' && sub === '/release') return handleTermForceRelease(res, id, body);
    if (method === 'POST' && sub === '/write') return handleTermWrite(res, id, body);
    if (method === 'GET' && sub === '/read') return handleTermRead(res, id, parseQuery(req.url));
    if (method === 'POST' && sub === '/resize') return handleTermResize(res, id, body);
    if (method === 'DELETE' && !sub) return handleTermDelete(res, id);
  }
  return sendJson(res, 404, { error: `Cannot ${method} ${url}` });
}

// --- handlers (each resolves to { ok:true, value } or { error }) ------------

function handleExec(res, body) {
  let code;
  try { code = JSON.parse(body || '{}').code; }
  catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); }
  if (typeof code !== 'string') return sendJson(res, 400, { error: 'missing "code"' });
  // Legacy /exec globals (docs/PHASE_14_CUTOVER_PREPARATION.md, gate phase14-probe-065):
  // vscode, Buffer, os, process, cp, nodePty, codeApiTerm. bridgeCore.runExecBody injects
  // them all; the body may `return` a value or a Promise and runValue awaits it,
  // preserving legacy async /exec behavior. nodePty is the real node-pty module and
  // codeApiTerm is the registry-backed facade below; if node-pty cannot load, the exec
  // fails, exactly as it would on the legacy runtime whose nodePty was always present.
  runValue(res, () => bridgeCore.runExecBody(code, {
    vscode, nodePty: loadPty(), codeApiTerm,
  }));
}

function handleCommand(res, body) {
  let parsed;
  try { parsed = JSON.parse(body || '{}'); }
  catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); }
  const id = parsed.id;
  const args = Array.isArray(parsed.args) ? parsed.args : [];
  if (typeof id !== 'string') return sendJson(res, 400, { error: 'missing "id"' });
  runValue(res, () => vscode.commands.executeCommand(id, ...args));
}

function handleWorkspaceFolders(res) {
  runValue(res, () => (vscode.workspace.workspaceFolders || []).map((f) => ({
    uri: f.uri.toString(), name: f.name, index: f.index,
  })));
}

function handleEditorActive(res) {
  runValue(res, () => {
    const ed = vscode.window.activeTextEditor;
    if (!ed) return null;                 // explicit null (present), not undefined
    const d = ed.document;
    const sel = ed.selection;
    return {
      document: {
        uri: d.uri.toString(), fileName: d.fileName,
        languageId: d.languageId, lineCount: d.lineCount,
      },
      selection: {
        start: { line: sel.start.line, character: sel.start.character },
        end: { line: sel.end.line, character: sel.end.character },
      },
    };
  });
}

function handleMessage(res, body) {
  let parsed;
  try { parsed = JSON.parse(body || '{}'); }
  catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); }
  const message = parsed.message == null ? '' : String(parsed.message);
  const type = parsed.type || 'info';
  const show = type === 'error' ? vscode.window.showErrorMessage
    : type === 'warning' ? vscode.window.showWarningMessage
      : vscode.window.showInformationMessage;
  // BLOCKING: resolves only when the user dismisses/acts on the toast. Do NOT add a
  // timeout here — the Python client owns the (long) timeout. The resolved value
  // (selected item | undefined) is returned; the PUBLIC /window/showMessage route
  // discards it and returns {success:true} (ADAPTER_BRIDGE_CONTRACT.md §4/§5).
  runValue(res, () => show(message));
}

function handleApis(res) {
  // Phase 14 /apis reconciliation: prepend the 13 fixed non-command descriptors and
  // emit command rows in the legacy shape (parameters:['...args'], description:
  // 'Execute VSCode command: '+id). Both live in bridgeCore.apisFromCommands so they
  // are unit-tested without the host. The command list is real and window-dependent
  // (vscode.commands.getCommands) — callers assert structure, not count.
  runValue(res, async () => {
    const commands = await vscode.commands.getCommands(true);
    return bridgeCore.apisFromCommands(commands);
  });
}

// --- terminal registry + handlers (Phase 13B) ------------------------------
//
// Adapter-owned node-pty terminals. The adapter OWNS the pty (so its buffer is
// readable — VS Code's own terminals are not) and, when `show` is set, surfaces it
// as the VS Code terminal `codeapi:<id>`. Liveness comes from node-pty's onExit
// (authoritative exitCode + alive=false); process.kill(pid,0) is a fallback only.
// See docs/TERM_CONTRACT.md. The public Python /term/* routes (server/term_routes.py)
// front these bridge handlers.

const terms = new Map();   // id -> record
let termSeq = 0;

function loadPty() {
  // node-pty lives in the extension host (external dep, shipped at package time).
  return require('node-pty');
}

function newTermId() {
  return 't' + Date.now().toString(36) + (termSeq++).toString(36);
}

function parseQuery(rawUrl) {
  const q = {};
  const i = (rawUrl || '').indexOf('?');
  if (i < 0) return q;
  for (const pair of rawUrl.slice(i + 1).split('&')) {
    if (!pair) continue;
    const eq = pair.indexOf('=');
    const k = eq < 0 ? pair : pair.slice(0, eq);
    const v = eq < 0 ? '' : pair.slice(eq + 1);
    q[decodeURIComponent(k)] = decodeURIComponent(v);
  }
  return q;
}

function reconcileAlive(rec) {
  // onExit is the source of truth (already set rec.alive/exitCode). Fallback only:
  // if still marked alive, probe existence; ESRCH => the process is gone.
  if (!rec.alive) return;
  try { process.kill(rec.pid, 0); } catch (e) {
    if (e && e.code === 'ESRCH') { rec.alive = false; }
  }
}

function makeVisibleTerminal(id, rec) {
  // Bridge the node-pty to a VS Code terminal so it is visible AND readable.
  //
  // DIMENSION SYNC IS MANDATORY: the pty spawns at the legacy default 120x30, but
  // the visible pane has its own geometry. Without open(initialDimensions) +
  // setDimensions() forwarding, the shell keeps addressing a 30-row screen the
  // pane does not have — PSReadLine's absolute cursor writes (ESC[30;42H) land
  // off-view or overpaint other rows, so the pane "does not display everything"
  // even though /term/read sees every byte. For a VISIBLE terminal the pane is
  // the authoritative display, so its size wins (an API /term/:id/resize on a
  // visible terminal will be re-overridden on the next pane resize; headless
  // show:false terminals keep their API-controlled size).
  const writeEmitter = new vscode.EventEmitter();
  rec.pty.onData((d) => writeEmitter.fire(d));
  const syncDims = (dims) => {
    if (!dims || !dims.columns || !dims.rows) { return; }
    try { rec.pty.resize(dims.columns, dims.rows); } catch (_) {}
  };
  const pseudo = {
    onDidWrite: writeEmitter.event,
    open: (initialDimensions) => syncDims(initialDimensions),
    setDimensions: (dimensions) => syncDims(dimensions),
    close: () => { try { rec.pty.kill(); } catch (_) {} },
    handleInput: (data) => { try { rec.pty.write(data); } catch (_) {} },
  };
  const term = vscode.window.createTerminal({ name: 'codeapi:' + id, pty: pseudo });
  term.show(false);
  return term;
}

function termCreate(opts) {
  // Shared create path for BOTH the typed /vscode/term/create handler and the
  // codeApiTerm.create() facade. Spawns an adapter-owned node-pty, wires onData (ring
  // buffer) + onExit (authoritative alive/exitCode), optionally surfaces a visible
  // VS Code terminal, registers it, and returns {id,shell,cwd,pid,surface}: the legacy
  // shape plus the HARD surface discriminator — 'vscode-terminal' (surfaced as the
  // visible integrated terminal codeapi:<id>) vs 'process' (headless PTY with NO
  // presence in the VS Code terminal UI). The visible-terminal branch below is driven
  // off the same resolved value, so the label can never diverge from the behavior.
  const nodePty = loadPty();
  const os = require('os');
  const id = opts.id ? String(opts.id) : newTermId();
  // Legacy-aligned defaults via the vscode-free helper (unit-tested in bridge_core):
  // shell = COMSPEC||powershell.exe (win) / SHELL||/bin/bash (posix); 120x30; 1e6 buffer;
  // cwd = opts.cwd || workspace folder || os.homedir(). Matches dist/extension.js:58.
  const defs = bridgeCore.resolveTermDefaults(opts, {
    platform: process.platform,
    env: process.env,
    workspaceCwd: (vscode.workspace.workspaceFolders || [])[0]?.uri?.fsPath,
    homedir: os.homedir(),
  });
  const surface = bridgeCore.resolveTermSurface(opts);

  // Shell-integration bootstrap. The script ships INSIDE the running VS Code
  // installation, so its path is derived from vscode.env.appRoot rather than
  // hardcoded — a hardcoded path would break on every update, and the probe's
  // archive path is not where production reads from.
  const siNonce = crypto.randomUUID ? crypto.randomUUID()
    : crypto.randomBytes(16).toString('hex');
  const siPath = path.join(vscode.env.appRoot, 'out', 'vs', 'workbench',
    'contrib', 'terminal', 'common', 'scripts', 'shellIntegration.ps1');
  let siExists = false;
  try { siExists = require('fs').existsSync(siPath); } catch (_) { siExists = false; }
  const si = bridgeCore.resolveShellIntegration(defs, {
    scriptPath: siPath, scriptExists: siExists, nonce: siNonce,
  });

  const p = nodePty.spawn(defs.shell, si.args,
    { name: 'xterm-color', cols: defs.cols, rows: defs.rows, cwd: defs.cwd,
      env: Object.assign({}, process.env, si.env) });
  // generation: a fresh identity for THIS incarnation of the terminal id.
  // Ids are caller-chosen and reusable — kill 'tX' and create 'tX' again and
  // the name is identical while the process, buffer and shell are not. Without
  // a generation, evidence captured against the first incarnation could be
  // attributed to the second, which is the same class of mistake as reading a
  // stale marker from an earlier execution. Minted once, never recomputed.
  const generation = 'g-' + (crypto.randomUUID ? crypto.randomUUID()
    : crypto.randomBytes(16).toString('hex'));
  const rec = {
    id, pty: p, shell: defs.shell, cwd: defs.cwd, pid: p.pid, buffer: '', bytes: 0,
    maxBytes: defs.maxBytes, alive: true, exitCode: null, vscodeTerm: null, surface,
    generation, createdAt: Date.now(),
    // Shell lifecycle state lives on the RECORD, so it is scoped to this
    // generation by construction — a kill/recreate of the same id builds a
    // new record and therefore starts from UNKNOWN, with no readiness
    // carried across. rec.buffer stays terminal history; structured state
    // does not live there, because the ring truncates.
    shell633: bridgeCore.createShellLifecycle(),
    // Exactly one execution may own the command channel at a time. Null means
    // the terminal is free; a value is proof some execution holds it.
    activeExecutionId: null,
    executionReservedAt: null,
    // WHY a terminal has (or lacks) readiness. An unready terminal that
    // cannot say why is indistinguishable from a broken one.
    shellIntegrationBootstrap: si.mode,
  };
  p.onData((d) => {
    rec.buffer += d;
    rec.bytes += Buffer.byteLength(d);
    // Parsed incrementally as bytes arrive, with a carry tail for sequences
    // split across callbacks — rec.buffer cannot be the parser's input
    // because it is truncated from the front once it exceeds maxBytes.
    bridgeCore.feedShellLifecycle(rec.shell633, d, Date.now());
    rec.shell633.lastShellEventOffset = rec.bytes;
    // Record that THIS reservation's execution actually started. Without it,
    // the release below fires on the echo of the envelope — before the shell
    // has evaluated it — and a second caller is let in.
    if (rec.activeExecutionId && rec.shell633
        && rec.shell633.commandDepth > 0) {
      rec.executionSawBusy = true;
    }
    // AUTOMATIC release. The extension observes the shell returning to an
    // idle prompt and clears its own reservation, so the normal lifecycle
    // needs no cooperation from the client. That matters because a client can
    // die between submitting and releasing: the command still finishes, the
    // shell still returns to READY, and the terminal must not stay reserved
    // forever merely because nobody called /release.
    if (bridgeCore.shouldAutoRelease(rec)) {
      rec.lastReleasedExecutionId = rec.activeExecutionId;
      rec.activeExecutionId = null;
      rec.executionReservedAt = null;
      rec.lastReleaseReason = 'SHELL_READY';
    }
    if (rec.buffer.length > rec.maxBytes) {
      rec.buffer = rec.buffer.slice(rec.buffer.length - rec.maxBytes);
    }
  });
  p.onExit(({ exitCode }) => {
    rec.alive = false;
    rec.exitCode = exitCode;
    bridgeCore.markShellExited(rec.shell633);
    // A terminal that has exited can never return to READY, so an automatic
    // release on exit is the only thing that prevents a permanent hold.
    if (rec.activeExecutionId) {
      rec.lastReleasedExecutionId = rec.activeExecutionId;
      rec.activeExecutionId = null;
      rec.executionReservedAt = null;
      rec.lastReleaseReason = 'TERMINAL_EXITED';
    }
  });
  if (surface === bridgeCore.TERM_SURFACE_TERMINAL) {
    rec.vscodeTerm = makeVisibleTerminal(id, rec);
  }
  terms.set(id, rec);
  return { id, shell: defs.shell, cwd: defs.cwd, pid: p.pid, surface, generation };
}

function handleTermCreate(res, body) {
  let opts;
  try { opts = JSON.parse(body || '{}'); }
  catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); }
  runValue(res, () => termCreate(opts));
}

function handleTermList(res) {
  runValue(res, () => {
    const out = [];
    for (const rec of terms.values()) {
      reconcileAlive(rec);
      // surface travels with the record for its whole lifetime; a record that
      // somehow predates the field defaults to 'process' — never over-claim a
      // visible terminal.
      //
      // shell is the SPAWN-TIME truth, recorded at creation and never
      // recomputed. It is immutable metadata for this terminal generation:
      // re-deriving it later from COMSPEC or the current environment would
      // describe the ENVIRONMENT rather than the process actually running,
      // and a caller choosing an execution dialect from that would submit
      // one shell's syntax to another. Callers previously had no way to ask
      // — create returned `shell` but list did not — so `term run` against a
      // pre-existing terminal could only guess.
      const sh = rec.shell633 || null;
      out.push({ id: rec.id, pid: rec.pid, bytes: rec.bytes,
                 shell: rec.shell || null,
                 generation: rec.generation || null,
                 // Shell READINESS, distinct from `alive`. A terminal can be
                 // alive with a pager owning it — tmbrwfix1 proved exactly
                 // that — so liveness must never be read as eligibility.
                 shellIntegrationActive: sh ? sh.shellIntegrationActive : false,
                 shellIntegrationBootstrap: rec.shellIntegrationBootstrap || null,
                 shellState: sh ? sh.shellState : bridgeCore.SHELL_UNKNOWN,
                 shellStateSequence: sh ? sh.shellStateSequence : 0,
                 shellCommandActive: sh ? sh.shellCommandActive : false,
                 // Diagnostics, not contract. Two scalars rather than the
                 // former unbounded array: enough to see that a terminal is
                 // reporting oddly and which rule caught it, without turning
                 // every internal grammar tag into something callers depend on.
                 lifecycleAnomalyCount: sh ? sh.lifecycleAnomalyCount : 0,
                 lastLifecycleAnomaly: sh ? sh.lastLifecycleAnomaly : null,
                 activeExecutionId: rec.activeExecutionId || null,
                 alive: rec.alive, exitCode: rec.exitCode,
                 surface: rec.surface || bridgeCore.TERM_SURFACE_PROCESS });
    }
    return out;
  });
}

// Reserve AND submit in ONE event-loop turn.
//
// A separate reserve, returning to the client, followed by a write, still
// leaves a window: ownership proves the caller held the lock, not that its
// bytes were the next thing written. Nothing runs between the verdict, the
// assignment and the pty.write here, so "eligible" and "this exact envelope
// was submitted" are the same instant.
//
// The client mints the execution id and builds the envelope, because Python
// owns dialect selection and the tested envelope construction and neither
// should be duplicated in JS. The id is CORRELATION, not authority: proposing
// one grants nothing. Authority is rec.activeExecutionId === executionId,
// decided here.
function handleTermSubmit(res, id, body) {
  let opts;
  try { opts = JSON.parse(body || '{}'); }
  catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); }
  const v0 = bridgeCore.validateTermSubmitRequest(body);
  if (!v0.ok) return sendJson(res, 400, { error: v0.error });
  runValue(res, () => {
    const rec = terms.get(id);
    if (rec) { reconcileAlive(rec); }
    const v = bridgeCore.evaluateReservation(rec, v0.expectedGeneration);
    if (!v.ok) {
      // Refused BEFORE any byte is written — that is the whole point.
      return { id, submitted: false, submissionAccepted: false,
               executionId: null, reason: v.reason,
               preSubmitState: v.preSubmitState,
               generation: rec ? rec.generation : null,
               activeExecutionId: v.activeExecutionId || null,
               bytes: rec ? rec.bytes : null };
    }
    rec.activeExecutionId = v0.executionId;
    rec.executionReservedAt = Date.now();
    // This execution has not been seen to start yet, so the auto-release must
    // not fire on the echo of the envelope about to be written.
    rec.executionSawBusy = false;
    const bytesBefore = rec.bytes;
    const p = bridgeCore.termWritePayload(v0.data, true);
    try {
      rec.pty.write(p.data);
    } catch (e) {
      // The write failed synchronously, so nothing was submitted. Roll the
      // reservation back rather than leaving the terminal owned by an
      // execution that never happened.
      rec.activeExecutionId = null;
      rec.executionReservedAt = null;
      return { id, submitted: false, submissionAccepted: false,
               executionId: null, reason: 'WRITE_FAILED',
               error: errMsg(e), preSubmitState: v.preSubmitState,
               generation: rec.generation, bytes: bytesBefore };
    }
    return { id, submitted: true, submissionAccepted: true,
             executionId: v0.executionId, generation: rec.generation,
             preSubmitState: v.preSubmitState, wrote: p.wrote,
             observationStartOffset: bytesBefore,
             shellStateSequence: rec.shell633 ? rec.shell633.shellStateSequence : 0 };
  });
}

// OPERATOR RECOVERY. Not reached by any normal execution — the auto-release in
// p.onData handles those — and no Python surface calls it. It exists so an
// abandoned reservation cannot own a terminal forever, and it is spelled as a
// deliberate act: generation, execution id and a stated reason, all required.
function handleTermForceRelease(res, id, body) {
  const v0 = bridgeCore.validateTermForceReleaseRequest(body);
  if (!v0.ok) return sendJson(res, 400, { error: v0.error });
  runValue(res, () => {
    const rec = terms.get(id);
    if (rec) { reconcileAlive(rec); }
    const v = bridgeCore.evaluateForceRelease(
      rec, v0.expectedGeneration, v0.executionId, v0.reason);
    if (v.released) {
      rec.lastReleasedExecutionId = rec.activeExecutionId;
      rec.activeExecutionId = null;
      rec.executionReservedAt = null;
      rec.executionSawBusy = false;
      // Recorded as forced so a later reader can tell an operator override
      // from the shell genuinely coming back.
      rec.lastReleaseReason = 'FORCED: ' + v0.reason;
    }
    return Object.assign({ id }, v,
      { shellState: rec && rec.shell633 ? rec.shell633.shellState : null });
  });
}

function handleTermWrite(res, id, body) {
  // Validated HERE, not only in the Python route. This bridge is an
  // independently reachable API: any local caller holding the bridge token
  // arrives at this function without passing through Python at all, so the
  // route's strictness is not an end-to-end guarantee. `data` must be a
  // string and `enter` a real boolean, or the request is refused before the
  // registry is touched (bridge_core.validateTermWriteRequest).
  const v = bridgeCore.validateTermWriteRequest(body);
  if (!v.ok) return sendJson(res, 400, { error: v.error });
  const rec = terms.get(id);
  if (!rec) return sendJson(res, 404, { error: `Terminal ${id} not found` });
  runValue(res, () => {
    // Shared with the /exec facade — bridgeCore.termWritePayload is the single
    // definition of the CR-append and the byte count, so the two paths cannot
    // drift apart on what a write actually submits.
    const p = bridgeCore.termWritePayload(v.data, v.enter);
    rec.pty.write(p.data);
    // bytes accepted into the pty input — NOT command success, NOT proof of life.
    return { id, wrote: p.wrote };
  });
}

function handleTermRead(res, id, query) {
  const rec = terms.get(id);
  if (!rec) return sendJson(res, 404, { error: `Terminal ${id} not found` });
  runValue(res, () => {
    let buffer = rec.buffer;
    const tail = query.tail ? parseInt(query.tail, 10) : 0;
    if (tail > 0 && buffer.length > tail) buffer = buffer.slice(buffer.length - tail);
    const out = { id, bytes: rec.bytes, buffer };
    if (query.clear === 'true' || query.clear === '1') { rec.buffer = ''; }
    return out;
  });
}

function handleTermResize(res, id, body) {
  let opts;
  try { opts = JSON.parse(body || '{}'); }
  catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); }
  const rec = terms.get(id);
  if (!rec) return sendJson(res, 404, { error: `Terminal ${id} not found` });
  runValue(res, () => {
    const cols = parseInt(opts.cols, 10);
    const rows = parseInt(opts.rows, 10);
    rec.pty.resize(cols, rows);
    return { id, cols, rows };
  });
}

function handleTermDelete(res, id) {
  const rec = terms.get(id);
  if (!rec) return sendJson(res, 404, { error: `Terminal ${id} not found` });
  runValue(res, () => {
    const wasAlive = rec.alive;
    try { rec.pty.kill(); } catch (_) {}
    if (rec.vscodeTerm) { try { rec.vscodeTerm.dispose(); } catch (_) {} }
    terms.delete(id);
    // cleanup/removal; NOT the liveness-observation mechanism (see TERM_CONTRACT.md).
    return { id, killed: wasAlive };
  });
}

// --- codeApiTerm compatibility facade (Phase 14) -------------------------------
//
// Injected into /exec so legacy bodies that call codeApiTerm.<m>(...) keep working on
// this bridge. It is a thin facade over the SAME Phase-13B `terms` registry — method
// signatures and return shapes mirror the observed legacy codeApiTerm
// (docs/PHASE_13E_TERMINAL_DIFFERENTIAL.md): create(opts), list(), write(id,data,opts),
// read(id,opts), resize(id,cols,rows), kill(id). Phase-13 rules hold: `wrote` = bytes
// ACCEPTED (not command success, not proof of life); read returns the RAW buffer;
// liveness stays an adapter-side, typed-/term/list concern, so codeApiTerm.list() returns
// the legacy {id,pid,bytes} shape and does NOT infer or expose alive/exitCode here.
// Unknown id throws `Terminal <id> not found` (the bridge maps thrown errors to 500;
// the typed /vscode/term/* routes remain the 404-on-unknown-id surface).

// Built by the vscode-free factory so Object.keys(codeApiTerm) order matches legacy
// (["__pty","create","write","read","resize","list","kill"]) and is unit-testable.
// terms = the 13B registry; termCreate = the vscode/node-pty-owning spawn path.
const codeApiTerm = bridgeCore.makeCodeApiTerm(terms, termCreate);

// --- envelope + IO helpers --------------------------------------------------

async function runValue(res, thunk) {
  try {
    const value = await Promise.resolve(thunk());
    // JSON.stringify drops undefined-valued keys, so an undefined result yields
    // {"ok":true} (value omitted); null yields {"ok":true,"value":null}. This
    // present-vs-absent bit is what lets Python reproduce /command's omitted-result
    // and /exec's empty-body-on-undefined shapes.
    sendJson(res, 200, { ok: true, value });
  } catch (e) {
    sendJson(res, 500, { error: errMsg(e) });
  }
}

function sendJson(res, status, payload) {
  const data = Buffer.from(JSON.stringify(payload), 'utf8');
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': data.length,
  });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; });
    req.on('end', () => resolve(data));
    req.on('error', reject);
  });
}

function errMsg(e) {
  return String((e && e.message) || e);
}

// --- helpers (real; the JS bootstrap logic is not stubbed) ------------------

function resolveRuntimeCommand(context, pyPort, adapterPort) {
  const commonArgs = ['--host', '127.0.0.1', '--port', String(pyPort),
                      '--adapter-port', String(adapterPort)];
  const fs = require('fs');
  // PyInstaller must be built on this host OS; POSIX executables have no .exe suffix.
  const runtimeName = process.platform === 'win32' ? 'codeapi-runtime.exe' : 'codeapi-runtime';
  const packaged = path.join(context.extensionPath, 'bin', 'codeapi-runtime', runtimeName);
  // CODEAPI_FORCE_DEV_RUNTIME forces source mode even when the packaged exe
  // exists. Without it, packaged mode wins unconditionally whenever bin/
  // holds a build — and bin/ is gitignored, so an isolated Extension
  // Development Host loading the current WORKING TREE still ran a FROZEN
  // Python runtime from an older source. Any Python-side change was
  // therefore untestable in an extension host: a new route simply 404s while
  // the extension half appears correct. Opt-in, so production behaviour is
  // unchanged for anyone not setting it.
  if (fs.existsSync(packaged) && !process.env.CODEAPI_FORCE_DEV_RUNTIME) {
    return { exe: packaged, args: commonArgs, cwd: path.dirname(packaged), pythonPath: null };
  }
  // Dev mode: explicit python.exe -m codeapi_vscode.runtime (arg array, NOT a
  // .bat shim). The package lives in <repo>/python, so PYTHONPATH points there and
  // cwd is the repo root. Repo-root detection is robust to BOTH layouts: after the
  // Phase-14 cutover the ROOT manifest is loaded, so context.extensionPath IS the
  // repo root (which directly contains python/); under a dev-load of the
  // extension-adapter/ subfolder it is one level below. Prefer extensionPath when it
  // already holds python/, else step up one level.
  const cfgPy = vscode.workspace.getConfiguration('codeapi').get('pythonPath');
  const py = cfgPy || process.env.CODEAPI_PYTHON || resolvePythonExe(fs);
  const repoRoot = fs.existsSync(path.join(context.extensionPath, 'python'))
    ? context.extensionPath
    : path.resolve(context.extensionPath, '..');
  const pythonPath = path.join(repoRoot, 'python');
  return { exe: py, args: ['-m', 'codeapi_vscode.runtime', ...commonArgs],
           cwd: repoRoot, pythonPath };
}

// Find a real interpreter; on Windows avoid Store aliases.
//
// Bare 'python.exe' resolved to the App Execution Alias stub under
// WindowsApps, which prints "Python was not found..." and exits 9009 without
// ever running the module — measured in an isolated host, where the symptom
// was simply that no runtime port ever answered. Those stub entries are
// zero-length reparse points, so a real interpreter is told apart by having
// a non-empty file size, and WindowsApps is excluded by path regardless.
function resolvePythonExe(fs) {
  if (process.platform !== 'win32') { return 'python3'; }
  const dirs = String(process.env.PATH || '').split(path.delimiter);
  const names = ['python.exe', 'python3.exe'];
  for (const dir of dirs) {
    if (!dir || /\\WindowsApps(\\|$)/i.test(dir)) { continue; }
    for (const name of names) {
      const candidate = path.join(dir, name);
      try {
        const st = fs.statSync(candidate);
        if (st.isFile() && st.size > 0) { return candidate; }
      } catch (_) { /* not here */ }
    }
  }
  // Nothing usable found. Return the bare name so the failure surfaces in the
  // runtime log exactly as before rather than being masked by a wrong guess.
  return 'python.exe';
}

function stopChild() {
  if (!child) return;
  try { child.kill(); } catch (_) {}
  child = null;
}

module.exports = { activate, deactivate };
