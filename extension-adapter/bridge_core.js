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
      let s = data == null ? '' : String(data);
      if (opts && opts.enter) s += '\r';
      rec.pty.write(s);
      return { id, wrote: Buffer.byteLength(s) };
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

module.exports = {
  FIXED_API_DESCRIPTORS, apisFromCommands, runExecBody, resolveTermDefaults, makeCodeApiTerm,
};
