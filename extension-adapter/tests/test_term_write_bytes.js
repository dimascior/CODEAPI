// extension-adapter/tests/test_term_write_bytes.js — C3 terminal-write byte theorem.
//
// Exercises the REAL production shaping in ../bridge_core.js: termWritePayload,
// which extension.js handleTermWrite and the /exec codeApiTerm.write facade both
// call. Nothing here re-implements the CR-append or the byte count — a test that
// re-typed that logic would prove only that it can be re-typed.
//
// What is proven:
//   - enter appends exactly one CR (0x0D), never LF, never CRLF
//   - `wrote` is Buffer.byteLength of the FINAL string, UTF-8
//   - character count != byte count, and the CR contributes exactly one byte
//   - the pty receives the same string that `wrote` counted
//
// Background: fixtures/regressions/pty-foreground-ownership-20260809.md. The
// incident turned on `wrote: 2` for a one-character payload — the second byte
// being an appended CR, i.e. an Enter keypress into whatever held the terminal
// foreground.
//
// The control characters are built with String.fromCharCode rather than typed
// as literals, and their code points are asserted before use. A literal NUL or
// ESC is invisible in review, and if anything along the way normalised one to a
// space the byte count would still be 1 — the case would pass while testing
// nothing. Correct by construction, then verified.
//
//     node extension-adapter/tests/test_term_write_bytes.js

'use strict';

const core = require('../bridge_core');

const NUL = String.fromCharCode(0);
const ESC = String.fromCharCode(27);
const ETX = String.fromCharCode(3);      // Ctrl-C
const CR = String.fromCharCode(13);

let pass = 0;
let fail = 0;
function check(label, cond, detail) {
  if (cond) { pass++; console.log('OK   ' + label + (detail ? '   ' + detail : '')); }
  else { fail++; console.log('FAIL ' + label + (detail ? '   ' + detail : '')); }
}

// A pty that only records. Recording what was written is the whole point:
// `wrote` must describe the bytes that actually entered the stream.
function fakeTerm() {
  const written = [];
  const terms = new Map([['t1', {
    id: 't1', bytes: 0, buffer: '', written,
    pty: { write(s) { written.push(s); } },
  }]]);
  return { terms, written, api: core.makeCodeApiTerm(terms, () => ({ id: 't1' })) };
}

function main() {
  const { termWritePayload } = core;
  check('bridge_core exports termWritePayload',
        typeof termWritePayload === 'function');

  check('control constants really are U+0000 / U+001B / U+0003 / U+000D',
        NUL.charCodeAt(0) === 0 && ESC.charCodeAt(0) === 27
        && ETX.charCodeAt(0) === 3 && CR.charCodeAt(0) === 13);

  // --- the two cases from the incident ------------------------------------
  const noEnter = termWritePayload('q', false);
  check('data="q" enter=false -> final "q", wrote=1',
        noEnter.data === 'q' && noEnter.wrote === 1,
        JSON.stringify(noEnter));

  const withEnter = termWritePayload('q', true);
  check('data="q" enter=true -> final "q"+CR, wrote=2',
        withEnter.data === 'q' + CR && withEnter.wrote === 2,
        JSON.stringify(withEnter));

  // The terminator is CR, and ONLY CR. An LF or a CRLF pair would still be a
  // submitted line but a different byte count and a different wire fact.
  const tail = Buffer.from(withEnter.data, 'utf8');
  check('the appended terminator is exactly one 0x0D, no 0x0A',
        tail[tail.length - 1] === 0x0d && !tail.includes(0x0a)
        && tail.length === noEnter.wrote + 1,
        'bytes=' + JSON.stringify(Array.from(tail)));

  // --- character count != byte count --------------------------------------
  // Deliberately three widths: 2-byte, 3-byte, 4-byte (astral, and 2 UTF-16
  // code units in JS, so .length disagrees with both counts).
  for (const [label, ch, bytes] of [
    ['e-acute U+00E9 (2 bytes)', String.fromCodePoint(0xe9), 2],
    ['CJK U+4E2D (3 bytes)', String.fromCodePoint(0x4e2d), 3],
    ['emoji U+1F600 (4 bytes)', String.fromCodePoint(0x1f600), 4],
  ]) {
    const off = termWritePayload(ch, false);
    const on = termWritePayload(ch, true);
    check('multibyte ' + label + ': wrote counts BYTES not characters',
          off.wrote === bytes && off.wrote !== [...ch].length,
          'wrote=' + off.wrote + ' codepoints=' + [...ch].length
          + ' js_length=' + ch.length);
    check('multibyte ' + label + ': CR adds exactly one byte',
          on.wrote === bytes + 1 && on.data === ch + CR,
          'no_enter=' + off.wrote + ' enter=' + on.wrote);
  }

  // A string whose JS .length equals neither the codepoint count nor the byte
  // count, so any of the three being used by mistake is visible.
  const mixed = 'a' + String.fromCodePoint(0xe9) + String.fromCodePoint(0x4e2d)
    + String.fromCodePoint(0x1f600);
  const mixedOn = termWritePayload(mixed, true);
  check('mixed-width string: wrote == Buffer.byteLength(final)',
        mixedOn.wrote === Buffer.byteLength(mixed + CR)
        && mixedOn.wrote === 1 + 2 + 3 + 4 + 1
        && mixedOn.wrote !== mixed.length
        && mixedOn.wrote !== [...mixed].length,
        'wrote=' + mixedOn.wrote + ' js_length=' + mixed.length
        + ' codepoints=' + [...mixed].length);

  // --- control and boundary payloads --------------------------------------
  // These are carried, not interpreted: the shaping layer has no opinion about
  // what the bytes mean to the foreground application. Note the last case —
  // enter appends a CR even when the text already ends in one.
  for (const [label, s, enter, want] of [
    ['empty string, no enter', '', false, 0],
    ['empty string + enter (a bare CR)', '', true, 1],
    ['NUL U+0000', NUL, false, 1],
    ['ESC U+001B', ESC, false, 1],
    ['ETX U+0003 (Ctrl-C)', ETX, false, 1],
    ['ESC sequence ESC[A', ESC + '[A', false, 3],
    ['embedded CR', 'a' + CR + 'b', false, 3],
    ['embedded LF', 'a\nb', false, 3],
    ['text already ending in CR, + enter', 'a' + CR, true, 3],
  ]) {
    const p = termWritePayload(s, enter);
    check('boundary payload ' + label + ' -> wrote=' + want,
          p.wrote === want, JSON.stringify(p));
  }

  // The empty-payload pair is the JS-side half of the Python theorem: an
  // intentional empty payload is a real, expressible request, and with enter
  // it is a bare Enter — one byte, no text.
  const bare = termWritePayload('', true);
  check('empty data + enter is a BARE CR and nothing else',
        bare.data === CR && bare.wrote === 1, JSON.stringify(bare));

  // null/undefined map to '' at this edge (defensive, pre-existing). With the
  // Python route now REQUIRING `data`, no in-repo caller can reach it that way.
  check('null data still maps to empty, not the text "null"',
        termWritePayload(null, false).data === ''
        && termWritePayload(undefined, false).data === '');

  // --- the facade writes what it counted ----------------------------------
  const f1 = fakeTerm();
  const r1 = f1.api.write('t1', 'q', { enter: true });
  check('facade: pty receives the SAME string wrote counted',
        f1.written.length === 1 && f1.written[0] === 'q' + CR
        && r1.wrote === Buffer.byteLength(f1.written[0]) && r1.wrote === 2,
        'written=' + JSON.stringify(f1.written) + ' wrote=' + r1.wrote);

  const f2 = fakeTerm();
  const r2 = f2.api.write('t1', 'q', { enter: false });
  check('facade: enter=false submits exact text, no terminator',
        f2.written[0] === 'q' && r2.wrote === 1,
        'written=' + JSON.stringify(f2.written));

  const cjk = String.fromCodePoint(0x4e2d);
  const f3 = fakeTerm();
  const r3 = f3.api.write('t1', cjk, { enter: true });
  check('facade: multibyte + CR -> wrote=4 for ONE character',
        f3.written[0] === cjk + CR && r3.wrote === 4,
        'wrote=' + r3.wrote);

  console.log('\n' + '='.repeat(54));
  console.log('term write byte theorem: ' + pass + '/' + (pass + fail) + ' passed');
  process.exit(fail === 0 ? 0 : 1);
}

main();
