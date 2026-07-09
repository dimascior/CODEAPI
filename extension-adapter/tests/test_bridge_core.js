// extension-adapter/tests/test_bridge_core.js — Phase 14 focused bridge coverage.
//
// Exercises the REAL production code in ../bridge_core.js (the module extension.js
// uses for /exec global injection and /apis shaping), with NO VS Code and NO node-pty:
// vscode/nodePty/codeApiTerm are passed as stubs; cp/os/Buffer/process are Node built-ins.
//
// Covers the Phase 14 blockers this change closes:
//   - /exec exposes typeof nodePty/codeApiTerm/cp === "object" (+ vscode/Buffer/os/process)
//   - async /exec remains awaited (mirrors extension.js runValue: await Promise.resolve)
//   - /apis includes the 13 fixed descriptors, prepended verbatim
//   - command rows use parameters:["...args"] and "Execute VSCode command: <id>"
// Plus (Phase 14B) terminal-create default resolution matched to the legacy bundle:
//   - shell = COMSPEC||powershell.exe (win) / SHELL||/bin/bash (posix)
//   - cols 120, rows 30, maxBytes 1e6 (1000000, not 1048576), args [], cwd precedence
//   - explicit caller overrides always win
// Plus (Phase 14C) codeApiTerm facade: Object.keys order == legacy
//   ["__pty","create","write","read","resize","list","kill"] + method semantics.
//
// Fidelity note: this tests bridge_core in isolation (the real shaping/injection code).
// It does NOT dev-load extension.js or load real node-pty — that literal end-to-end is
// the operator dev-load step (docs/PHASE_14_CUTOVER_PREPARATION.md), not this unit.
//
//     node extension-adapter/tests/test_bridge_core.js

'use strict';

const core = require('../bridge_core');

let pass = 0;
let fail = 0;
function check(label, cond) {
  if (cond) { pass++; console.log('OK   ' + label); }
  else { fail++; console.log('FAIL ' + label); }
}

async function main() {
  const { runExecBody, apisFromCommands, FIXED_API_DESCRIPTORS, resolveTermDefaults,
          makeCodeApiTerm } = core;

  // --- /exec global injection (the Phase 14 globals blocker) ----------------
  const ctx = { vscode: {}, nodePty: {}, codeApiTerm: {} };
  check('exec: typeof vscode === object', runExecBody('return typeof vscode', ctx) === 'object');
  check('exec: typeof Buffer === function', runExecBody('return typeof Buffer', ctx) === 'function');
  check('exec: typeof os === object', runExecBody('return typeof os', ctx) === 'object');
  check('exec: typeof process === object', runExecBody('return typeof process', ctx) === 'object');
  check('exec: typeof cp === object', runExecBody('return typeof cp', ctx) === 'object');
  check('exec: typeof nodePty === object', runExecBody('return typeof nodePty', ctx) === 'object');
  check('exec: typeof codeApiTerm === object', runExecBody('return typeof codeApiTerm', ctx) === 'object');

  // cp is the real child_process module; nodePty/codeApiTerm are the injected objects.
  check('exec: cp is real child_process (cp.spawn is a function)',
        typeof runExecBody('return cp.spawn', ctx) === 'function');
  check('exec: nodePty identity is the injected object',
        runExecBody('return nodePty.__sentinel', { nodePty: { __sentinel: 'pty' } }) === 'pty');
  check('exec: codeApiTerm methods callable through the global',
        runExecBody('return codeApiTerm.list()', { codeApiTerm: { list() { return 'L'; } } }) === 'L');

  // --- async /exec remains awaited ------------------------------------------
  // extension.js handleExec runs `runValue(res, () => runExecBody(...))`, and runValue
  // does `await Promise.resolve(thunk())`. Mirror exactly that awaiting here.
  const awaited = async (code) => await Promise.resolve(runExecBody(code, ctx));
  check('exec: sync arithmetic -> 2', (await awaited('return 1+1')) === 2);
  check('exec: Promise.resolve awaited -> 42', (await awaited('return Promise.resolve(42)')) === 42);
  check('exec: async IIFE awaited -> 7', (await awaited('return (async function(){ return 7 })()')) === 7);
  check('exec: delayed Promise awaited -> 99',
        (await awaited('return new Promise(function(r){ setTimeout(function(){ r(99) }, 20) })')) === 99);

  // --- /apis reconciliation -------------------------------------------------
  check('apis: exactly 13 fixed descriptors', FIXED_API_DESCRIPTORS.length === 13);

  const apis = apisFromCommands(['workbench.action.files.save', 'editor.action.format']);

  const expectedFixed = [
    ['workspace', 'openTextDocument', ['uri']],
    ['workspace', 'saveAll', []],
    ['workspace', 'findFiles', ['include', 'exclude?', 'maxResults?']],
    ['window', 'showInformationMessage', ['message', '...items']],
    ['window', 'showErrorMessage', ['message', '...items']],
    ['window', 'showWarningMessage', ['message', '...items']],
    ['window', 'showInputBox', ['options?']],
    ['window', 'showQuickPick', ['items', 'options?']],
    ['editor', 'edit', ['callback']],
    ['editor', 'insertSnippet', ['snippet', 'location?', 'options?']],
    ['commands', 'executeCommand', ['command', '...args']],
    ['languages', 'getDiagnostics', ['resource?']],
    ['extensions', 'getExtension', ['extensionId']],
  ];
  let fixedOk = true;
  for (let i = 0; i < expectedFixed.length; i++) {
    const row = apis[i];
    const [cat, method, params] = expectedFixed[i];
    if (!row || row.category !== cat || row.method !== method ||
        JSON.stringify(row.parameters) !== JSON.stringify(params) ||
        Object.prototype.hasOwnProperty.call(row, 'description')) {
      fixedOk = false;
      break;
    }
  }
  check('apis: 13 fixed descriptors prepended verbatim (no description key)', fixedOk);

  const cats = {};
  for (const r of apis) cats[r.category] = (cats[r.category] || 0) + 1;
  check('apis: category counts ws3/win5/ed2/cmds1/lang1/ext1 + command2',
        cats.workspace === 3 && cats.window === 5 && cats.editor === 2 &&
        cats.commands === 1 && cats.languages === 1 && cats.extensions === 1 &&
        cats.command === 2);

  const cmdRow = apis[13];
  check('apis: command row parameters === ["...args"]',
        JSON.stringify(cmdRow.parameters) === JSON.stringify(['...args']));
  check('apis: command row legacy shape + description',
        cmdRow.category === 'command' &&
        cmdRow.method === 'workbench.action.files.save' &&
        cmdRow.description === 'Execute VSCode command: workbench.action.files.save');

  check('apis: empty command list -> 13 fixed rows only', apisFromCommands([]).length === 13);
  check('apis: undefined command list tolerated -> 13', apisFromCommands().length === 13);

  // --- terminal create default resolution (Phase 14B, legacy-aligned) --------
  // Authoritative defaults from the committed bundle dist/extension.js:58 codeApiTerm.create:
  //   shell(win)=process.env.COMSPEC||"powershell.exe", shell(posix)=SHELL||"/bin/bash",
  //   cols:120, rows:30, maxBytes:1e6 (1000000), cwd:opts.cwd||workspaceCwd||homedir.
  check('term defaults: win32 shell resolves via COMSPEC first',
        resolveTermDefaults({}, { platform: 'win32',
          env: { COMSPEC: 'C:\\Windows\\System32\\cmd.exe' }, homedir: 'H' }).shell
          === 'C:\\Windows\\System32\\cmd.exe');
  check('term defaults: win32 shell fallback === powershell.exe (COMSPEC unset)',
        resolveTermDefaults({}, { platform: 'win32', env: {}, homedir: 'H' }).shell
          === 'powershell.exe');
  check('term defaults: posix shell via SHELL',
        resolveTermDefaults({}, { platform: 'linux', env: { SHELL: '/bin/zsh' }, homedir: 'H' }).shell
          === '/bin/zsh');
  check('term defaults: posix shell fallback === /bin/bash (SHELL unset)',
        resolveTermDefaults({}, { platform: 'linux', env: {}, homedir: 'H' }).shell === '/bin/bash');

  const dwin = resolveTermDefaults({}, { platform: 'win32', env: { COMSPEC: 'c' }, homedir: 'H' });
  check('term defaults: cols === 120', dwin.cols === 120);
  check('term defaults: rows === 30', dwin.rows === 30);
  check('term defaults: maxBytes === 1000000 (1e6, legacy) not 1048576',
        dwin.maxBytes === 1000000 && dwin.maxBytes !== 1048576);
  check('term defaults: args === []', Array.isArray(dwin.args) && dwin.args.length === 0);

  check('term defaults: cwd uses opts.cwd first',
        resolveTermDefaults({ cwd: 'O' }, { workspaceCwd: 'W', homedir: 'H' }).cwd === 'O');
  check('term defaults: cwd falls to workspaceCwd',
        resolveTermDefaults({}, { workspaceCwd: 'W', homedir: 'H' }).cwd === 'W');
  check('term defaults: cwd falls to homedir when no workspace',
        resolveTermDefaults({}, { homedir: 'H' }).cwd === 'H');

  const ov = resolveTermDefaults(
    { shell: 'myshell', cols: 10, rows: 20, maxBytes: 5, args: ['-x'], cwd: 'C' },
    { platform: 'win32', env: { COMSPEC: 'c' }, homedir: 'H' });
  check('term defaults: explicit overrides win across the board',
        ov.shell === 'myshell' && ov.cols === 10 && ov.rows === 20 &&
        ov.maxBytes === 5 && ov.cwd === 'C' &&
        JSON.stringify(ov.args) === JSON.stringify(['-x']));

  // codeApiTerm.create() and typed /term/create both call extension.js termCreate(opts),
  // which delegates to this one resolveTermDefaults — so they share the default path.
  // (Structural single-source; a live spawn needs node-pty + the host, i.e. dev-load.)
  check('term defaults: deterministic for a given (opts,ctx)',
        JSON.stringify(resolveTermDefaults({}, { platform: 'win32', env: { COMSPEC: 'c' }, homedir: 'H' }))
          === JSON.stringify(resolveTermDefaults({}, { platform: 'win32', env: { COMSPEC: 'c' }, homedir: 'H' })));

  // --- codeApiTerm facade: legacy Object.keys order + method semantics (Phase 14C) ---
  // vscode/node-pty-free: a fake `terms` registry + fake termCreate exercise the facade.
  const ctTerms = new Map();
  const ctCreate = (opts) => {
    const id = (opts && opts.id) || 't1';
    ctTerms.set(id, {
      id, pid: 111, buffer: 'hello-world', bytes: 11, alive: true, exitCode: null,
      vscodeTerm: null,
      pty: { _in: [], write(s) { this._in.push(s); },
             _killed: false, kill() { this._killed = true; },
             _resized: null, resize(c, r) { this._resized = [c, r]; } },
    });
    return { id, shell: 's', cwd: 'c', pid: 111 };
  };
  const ct = makeCodeApiTerm(ctTerms, ctCreate);

  check('codeApiTerm: Object.keys order EXACTLY matches legacy',
        JSON.stringify(Object.keys(ct))
          === JSON.stringify(['__pty', 'create', 'write', 'read', 'resize', 'list', 'kill']));
  check('codeApiTerm: __pty is the injected registry Map', ct.__pty === ctTerms);

  const ctc = ct.create({ id: 't1' });
  check('codeApiTerm.create -> {id,shell,cwd,pid} via termCreate',
        ctc.id === 't1' && ctc.pid === 111);
  const ctw = ct.write('t1', 'echo hi', { enter: true });
  check('codeApiTerm.write -> {id,wrote}=bytes accepted incl CR (not liveness)',
        ctw.id === 't1' && ctw.wrote === Buffer.byteLength('echo hi\r')
          && ctTerms.get('t1').pty._in.join('') === 'echo hi\r');
  const ctr = ct.read('t1', { tail: 5 });
  check('codeApiTerm.read -> raw buffer tail {id,bytes,buffer}',
        ctr.id === 't1' && ctr.bytes === 11 && ctr.buffer === 'world');
  const ctz = ct.resize('t1', 100, 40);
  check('codeApiTerm.resize -> {id,cols,rows}',
        ctz.cols === 100 && ctz.rows === 40
          && JSON.stringify(ctTerms.get('t1').pty._resized) === JSON.stringify([100, 40]));
  const ctl = ct.list();
  check('codeApiTerm.list -> legacy {id,pid,bytes} (no alive/exitCode)',
        ctl.length === 1
          && JSON.stringify(Object.keys(ctl[0]).sort()) === JSON.stringify(['bytes', 'id', 'pid']));
  const ctk = ct.kill('t1');
  check('codeApiTerm.kill -> {id,killed} and removes from registry',
        ctk.id === 't1' && ctk.killed === true && !ctTerms.has('t1'));
  let ctThrew = false;
  try { ct.write('nope', 'x'); } catch (e) { ctThrew = /Terminal nope not found/.test(String(e.message)); }
  check('codeApiTerm unknown id throws "Terminal <id> not found"', ctThrew);

  console.log('\n' + '='.repeat(44));
  console.log(`Phase 14 bridge_core: ${pass}/${pass + fail} passed`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => { console.error(e); process.exit(1); });
