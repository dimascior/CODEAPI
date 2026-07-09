// extension-adapter/extension.js — minimal JS bootstrap/adapter (DESIGN SKELETON).
//
// >>> NOT wired into package.json.main yet. Pointing main here is a Phase-14
// >>> cutover item (see docs/PYTHON_RUNTIME_BOOTSTRAP.md). This file changes no
// >>> runtime behavior until that switch is made.
//
// Role: launch + supervise the Python CODEAPI runtime, and expose an adapter
// bridge so the Python runtime can perform the IRREDUCIBLE vscode.* operations.
// Python NEVER calls vscode.* directly — it calls this bridge over loopback HTTP.
//
// Hand-written JavaScript on purpose (no TypeScript). CommonJS, like
// extension-bridge/src/extension.js (the seed), but HTTP transport, not AF_UNIX.

const vscode = require('vscode');
const http = require('http');
const crypto = require('crypto');
const { spawn } = require('child_process');
const path = require('path');
const bridgeCore = require('./bridge_core');   // vscode-free: /exec globals + /apis shaping

// The Python service MUST bind outside the VS Code window band (3637-3646) until
// the Phase-14 cutover, to avoid poisoning discovery with phantom windows.
const PY_PORT_BASE = 47337;          // reserved Python-service range 47337-47346
const HEALTH_TIMEOUT_MS = 15000;

let child = null;
let bridge = null;
let channel = null;
let bridgeToken = null;   // per-launch token; every bridge request must present it
let adapterPort = null;   // ephemeral bridge port; kept so restartRuntime reuses the bridge

// --- lifecycle -------------------------------------------------------------

async function activate(context) {
  channel = vscode.window.createOutputChannel('CODEAPI Runtime');

  // 0. Mint the per-launch bridge token (ADAPTER_BRIDGE_CONTRACT.md §2.4). Loopback
  //    + ephemeral port is necessary but NOT sufficient; every bridge request must
  //    carry this token via X-CODEAPI-BRIDGE-TOKEN or it is rejected 403 before any
  //    vscode.* runs. Generated here, handed to Python as CODEAPI_BRIDGE_TOKEN.
  bridgeToken = crypto.randomBytes(32).toString('hex');

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

// Durable per-workspace CODEAPI window identity: a UUID created on first activation and
// persisted in workspaceState, reused on every later reload for this workspace state.
// Passed to the runtime as CODEAPI_WINDOW_ID -> /session.codeapiWindowId (the stable
// target-selection key). Unrelated to the legacy /session.windowId placeholder.
function getOrCreateWindowId(context) {
  let id = context.workspaceState.get('codeapi.windowId');
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID()
      : crypto.randomBytes(16).toString('hex');
    context.workspaceState.update('codeapi.windowId', id);
  }
  return id;
}

// Spawn (or re-spawn) the Python runtime child against the existing bridge. Idempotent
// and restart-safe: stops any current child first, picks a fresh service port outside
// the VS Code window band, and waits for /health. Called by activate() and by the
// codeapi.restartRuntime command.
async function startRuntime(context) {
  stopChild();
  // Choose the Python service port (outside 3637-3646).
  const pyPort = await pickFreePort(PY_PORT_BASE);

  // Spawn the Python runtime with an ARGUMENT ARRAY (never a shell string / .bat shim —
  // cmd.exe re-parses argv and mangles quotes on Windows).
  const { exe, args, cwd, pythonPath } = resolveRuntimeCommand(context, pyPort, adapterPort);
  const wsUri = (vscode.workspace.workspaceFolders || [])[0]?.uri?.toString() || '';
  // Adapter-pinned window identity (additive /session fields, docs/WIRE_CONTRACT.md):
  // codeapiWindowId is durable across reloads (workspaceState); vscodeSessionId is
  // session-scoped; extensionHostPid is THIS extension-host pid (differs from the Python
  // runtime pid). Existing env vars are unchanged.
  const codeapiWindowId = getOrCreateWindowId(context);
  const env = {
    ...process.env,
    CODEAPI_PY_PORT: String(pyPort),
    CODEAPI_EXTENSION_PORT: String(adapterPort),
    CODEAPI_BRIDGE_TOKEN: bridgeToken,
    CODEAPI_WORKSPACE_URI: wsUri,
    CODEAPI_EXTENSION_PATH: context.extensionPath,
    CODEAPI_VSCODE_SESSION_ID: vscode.env.sessionId,
    CODEAPI_EXTENSION_HOST_PID: String(process.pid),
    CODEAPI_WINDOW_ID: codeapiWindowId,
  };
  // Dev mode: make codeapi_vscode importable (the package lives in <repo>/python).
  // Packaged mode leaves pythonPath null (the exe is self-contained).
  if (pythonPath) { env.PYTHONPATH = pythonPath; }
  // Log exactly what is being launched so a failed dev-load is self-diagnosing in the
  // "CODEAPI Runtime" output channel (which python, which args, which cwd).
  channel.appendLine(`[starting runtime: ${exe} ${args.join(' ')} (cwd=${cwd})]`);
  child = spawn(exe, args, { env, cwd, windowsHide: true });
  // A missing / unresolvable python (spawn ENOENT) fires 'error', NOT 'exit'. Without
  // this handler the failure was invisible (only the 15s health timeout showed, and the
  // unhandled 'error' could bubble). Log it explicitly with the resolved exe + a fix hint.
  child.on('error', (e) => {
    channel.appendLine(`[runtime spawn error: ${errMsg(e)} — exe='${exe}'. Set the `
      + `"codeapi.pythonPath" setting or CODEAPI_PYTHON to an explicit python.exe.]`);
    vscode.window.showErrorMessage(
      `CODEAPI runtime could not launch Python ('${exe}'): ${errMsg(e)}`);
  });
  if (child.stdout) { child.stdout.on('data', (d) => channel.append(d.toString())); }
  if (child.stderr) { child.stderr.on('data', (d) => channel.append(d.toString())); }
  child.on('exit', (code) => channel.appendLine(`[runtime exited: ${code}]`));

  // Wait for Python /health before reporting ready.
  const ok = await waitForHealth(pyPort, HEALTH_TIMEOUT_MS);
  if (!ok) {
    channel.appendLine('[runtime did not become healthy in time]');
    vscode.window.showErrorMessage('CODEAPI runtime failed to start (see output).');
    stopChild();
    return;
  }
  channel.appendLine(`[runtime healthy on 127.0.0.1:${pyPort}]`);
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
  const tm = url.match(/^\/vscode\/term\/([^/]+)(\/write|\/read|\/resize)?$/);
  if (tm) {
    const id = decodeURIComponent(tm[1]);
    const sub = tm[2];
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
// See docs/TERM_CONTRACT.md. No Python /term/* routes yet (Phase 13D).

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
  const writeEmitter = new vscode.EventEmitter();
  rec.pty.onData((d) => writeEmitter.fire(d));
  const pseudo = {
    onDidWrite: writeEmitter.event,
    open: () => {},
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
  // VS Code terminal, registers it, and returns the legacy {id,shell,cwd,pid} shape.
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
  const p = nodePty.spawn(defs.shell, defs.args,
    { name: 'xterm-color', cols: defs.cols, rows: defs.rows, cwd: defs.cwd, env: process.env });
  const rec = {
    id, pty: p, shell: defs.shell, cwd: defs.cwd, pid: p.pid, buffer: '', bytes: 0,
    maxBytes: defs.maxBytes, alive: true, exitCode: null, vscodeTerm: null,
  };
  p.onData((d) => {
    rec.buffer += d;
    rec.bytes += Buffer.byteLength(d);
    if (rec.buffer.length > rec.maxBytes) {
      rec.buffer = rec.buffer.slice(rec.buffer.length - rec.maxBytes);
    }
  });
  p.onExit(({ exitCode }) => { rec.alive = false; rec.exitCode = exitCode; });
  if (opts.show) { rec.vscodeTerm = makeVisibleTerminal(id, rec); }
  terms.set(id, rec);
  return { id, shell: defs.shell, cwd: defs.cwd, pid: p.pid };
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
      out.push({ id: rec.id, pid: rec.pid, bytes: rec.bytes,
                 alive: rec.alive, exitCode: rec.exitCode });
    }
    return out;
  });
}

function handleTermWrite(res, id, body) {
  let opts;
  try { opts = JSON.parse(body || '{}'); }
  catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); }
  const rec = terms.get(id);
  if (!rec) return sendJson(res, 404, { error: `Terminal ${id} not found` });
  runValue(res, () => {
    let data = opts.data == null ? '' : String(opts.data);
    if (opts.enter) data += '\r';
    rec.pty.write(data);
    // bytes accepted into the pty input — NOT command success, NOT proof of life.
    return { id, wrote: Buffer.byteLength(data) };
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
  // Packaged mode (Phase 14): self-contained exe; cwd = its folder, no PYTHONPATH.
  const packaged = path.join(context.extensionPath, 'bin', 'codeapi-runtime',
                             'codeapi-runtime.exe');
  if (fs.existsSync(packaged)) {
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
  const py = cfgPy || process.env.CODEAPI_PYTHON || 'python.exe';
  const repoRoot = fs.existsSync(path.join(context.extensionPath, 'python'))
    ? context.extensionPath
    : path.resolve(context.extensionPath, '..');
  const pythonPath = path.join(repoRoot, 'python');
  return { exe: py, args: ['-m', 'codeapi_vscode.runtime', ...commonArgs],
           cwd: repoRoot, pythonPath };
}

function pickFreePort(base) {
  return new Promise((resolve) => {
    const srv = http.createServer();
    srv.listen(base, '127.0.0.1', () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
    srv.on('error', () => resolve(pickFreePort(base + 1)));
  });
}

function waitForHealth(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const tick = () => {
      const req = http.get({ host: '127.0.0.1', port, path: '/health', timeout: 1000 },
        (res) => { res.resume(); resolve(res.statusCode === 200); });
      req.on('error', () => (Date.now() < deadline ? setTimeout(tick, 300) : resolve(false)));
      req.on('timeout', () => { req.destroy(); });
    };
    tick();
  });
}

function stopChild() {
  if (!child) return;
  try { child.kill(); } catch (_) {}
  child = null;
}

module.exports = { activate, deactivate };
