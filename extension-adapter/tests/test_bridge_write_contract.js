// extension-adapter/tests/test_bridge_write_contract.js — D4/D6.
//
// The bridge is an INDEPENDENTLY REACHABLE API boundary, not an internal call
// from the Python runtime: extension.js serves it from its own
// http.createServer on 127.0.0.1 with an ephemeral port, gated only by
// X-CODEAPI-BRIDGE-TOKEN, and that token is handed to the Python child via the
// environment. Python is therefore not a mandatory hop, and the Python route's
// strict contract is NOT an end-to-end guarantee.
//
// So the same semantic theorem is proven here, at the transport boundary, on
// the REAL bridge_core.validateTermWriteRequest that extension.js
// handleTermWrite calls:
//
//     data    required, must be a JSON string
//     enter   required, must be a real JSON boolean
//
// No JavaScript truthiness as API validation. "false" must never mean Enter,
// and a number or object must never become terminal text.
//
// Also proves the D5 separation: the /exec codeApiTerm facade keeps its
// historical coercive behaviour, because one helper cannot serve two
// contracts without the strict one being weakened to fit the lenient one.
//
//     node extension-adapter/tests/test_bridge_write_contract.js

'use strict';

const core = require('../bridge_core');

const CR = String.fromCharCode(13);

let pass = 0;
let fail = 0;
function check(label, cond, detail) {
  if (cond) { pass++; console.log('OK   ' + label + (detail ? '   ' + detail : '')); }
  else { fail++; console.log('FAIL ' + label + (detail ? '   ' + detail : '')); }
}

// Rejected: ok false, an error string, and NO usable payload — the caller
// cannot proceed to a write by ignoring the verdict.
function reject(label, body, wantErr) {
  const v = core.validateTermWriteRequest(body);
  const ok = v.ok === false && typeof v.error === 'string'
    && v.data === undefined && v.enter === undefined
    && (!wantErr || v.error.indexOf(wantErr) !== -1);
  check(label, ok, JSON.stringify(v));
}

// Accepted: exact data and enter BY IDENTITY, then the payload the pty would
// receive and the byte count that describes it.
function accept(label, body, wantData, wantEnter, wantWrote) {
  const v = core.validateTermWriteRequest(body);
  let p = null;
  if (v.ok) p = core.termWritePayload(v.data, v.enter);
  const ok = v.ok === true && v.data === wantData && v.enter === wantEnter
    && p !== null && p.wrote === wantWrote
    && p.wrote === Buffer.byteLength(p.data)
    && p.data === (wantEnter ? wantData + CR : wantData);
  check(label, ok, JSON.stringify(v) + ' -> ' + JSON.stringify(p));
}

function main() {
  check('bridge_core exports validateTermWriteRequest',
        typeof core.validateTermWriteRequest === 'function');

  // --- root body ----------------------------------------------------------
  console.log('\n-- root body --');
  const NOT_JSON = 'not valid JSON';
  const NOT_OBJ = 'must be a JSON object';
  reject('malformed JSON -> rejected', '{"data":"q"', NOT_JSON);
  reject('empty body -> rejected (was {} before)', '', NOT_JSON);
  reject('undefined body -> rejected', undefined, NOT_JSON);
  reject('root null -> rejected', 'null', NOT_OBJ);
  reject('root array -> rejected', '[]', NOT_OBJ);
  reject('root string -> rejected', '"x"', NOT_OBJ);
  reject('root number -> rejected', '0', NOT_OBJ);
  reject('root boolean -> rejected', 'true', NOT_OBJ);

  // --- data ---------------------------------------------------------------
  console.log('\n-- data --');
  const REQ = "'data' is required";
  const STR = "'data' must be a JSON string";
  reject('data omitted -> rejected', '{"enter":true}', REQ);
  reject('data omitted, enter omitted too -> rejected', '{}', REQ);
  reject('data null -> rejected', '{"data":null,"enter":true}', STR);
  reject('data number 0 -> rejected', '{"data":0,"enter":true}', STR);
  reject('data number 123 -> rejected', '{"data":123,"enter":true}', STR);
  reject('data bool -> rejected', '{"data":false,"enter":true}', STR);
  reject('data object -> rejected', '{"data":{"x":1},"enter":true}', STR);
  reject('data array -> rejected', '{"data":[],"enter":true}', STR);

  // --- enter --------------------------------------------------------------
  console.log('\n-- enter --');
  const EREQ = "'enter' is required";
  const EBOOL = "'enter' must be a JSON boolean";
  reject('enter omitted -> rejected (required at transport)',
         '{"data":"q"}', EREQ);
  reject('enter null -> rejected', '{"data":"q","enter":null}', EBOOL);
  reject('enter "false" -> rejected, NOT read as truthy',
         '{"data":"q","enter":"false"}', EBOOL);
  reject('enter "true" -> rejected', '{"data":"q","enter":"true"}', EBOOL);
  reject('enter 0 -> rejected', '{"data":"q","enter":0}', EBOOL);
  reject('enter 1 -> rejected', '{"data":"q","enter":1}', EBOOL);
  reject('enter [] -> rejected', '{"data":"q","enter":[]}', EBOOL);
  reject('enter {} -> rejected', '{"data":"q","enter":{}}', EBOOL);

  // --- accepted, with the byte theorem on every one -----------------------
  console.log('\n-- accepted --');
  accept('data "q" + enter true -> one CR, wrote 2',
         '{"data":"q","enter":true}', 'q', true, 2);
  accept('data "q" + enter false -> exact text, wrote 1',
         '{"data":"q","enter":false}', 'q', false, 1);
  accept('empty data + enter true -> a deliberate bare CR, wrote 1',
         '{"data":"","enter":true}', '', true, 1);
  accept('empty data + enter false -> nothing at all, wrote 0',
         '{"data":"","enter":false}', '', false, 0);
  accept('multibyte + enter true -> wrote 4 for ONE character',
         '{"data":"\\u4e2d","enter":true}', String.fromCodePoint(0x4e2d), true, 4);
  accept('multibyte + enter false -> wrote 3',
         '{"data":"\\u4e2d","enter":false}', String.fromCodePoint(0x4e2d), false, 3);
  accept('astral + enter true -> wrote 5',
         '{"data":"\\ud83d\\ude00","enter":true}',
         String.fromCodePoint(0x1f600), true, 5);

  // The two dangerous shapes from before this validation existed. Both were
  // ACCEPTED by the bridge and both produced a terminal action.
  console.log('\n-- the pre-fix bypasses, now closed --');
  reject('{"data":123} no longer becomes the text "123"',
         '{"data":123}', STR);
  reject('{"data":"","enter":"false"} no longer submits a bare CR',
         '{"data":"","enter":"false"}', EBOOL);

  // --- D5: the /exec facade keeps its coercive behaviour ------------------
  //
  // Deliberate. The facade hands `opts` to user-authored exec code where JS
  // truthiness is the ambient expectation. Strictness lives at the transport
  // boundary; forcing one helper to serve both would mean weakening this one
  // or breaking that one.
  console.log('\n-- D5 facade compatibility (coercion preserved on purpose) --');
  const written = [];
  const terms = new Map([['t1', {
    id: 't1', bytes: 0, buffer: '', pty: { write(s) { written.push(s); } },
  }]]);
  const api = core.makeCodeApiTerm(terms, () => ({ id: 't1' }));

  const f1 = api.write('t1', 123, { enter: false });
  check('facade still coerces a number to text (unchanged)',
        written[0] === '123' && f1.wrote === 3, 'written=' + JSON.stringify(written[0]));

  const f2 = api.write('t1', 'q', { enter: 'false' });
  check('facade still treats "false" as truthy (unchanged, and why the '
        + 'transport boundary must not)',
        written[1] === 'q' + CR && f2.wrote === 2,
        'written=' + JSON.stringify(written[1]));

  const f3 = api.write('t1', null, {});
  check('facade still maps null data to empty (unchanged)',
        written[2] === '' && f3.wrote === 0);

  check('termWritePayload itself is still the coercive shaper',
        core.termWritePayload(123, 1).data === '123' + CR);

  console.log('\n' + '='.repeat(54));
  console.log('bridge write contract: ' + pass + '/' + (pass + fail) + ' passed');
  process.exit(fail === 0 ? 0 : 1);
}

main();
