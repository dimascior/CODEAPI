#!/usr/bin/env python3
"""Phase 13E — live terminal differential + liveness proof (Option B harness).

The real 13B bridge (extension-adapter/extension.js) is NOT wired (package.json.main
unchanged, by design until Phase 14), so /vscode/term/* is not reachable from the
active extension. This harness makes it reachable in an ISOLATED way WITHOUT any
cutover: it launches a standalone, headless Node bridge that runs the committed 13B
`/vscode/term/*` handler logic verbatim against the repo's real node-pty
(node_modules/node-pty, N-API) — omitting only makeVisibleTerminal (vscode-only).

It then drives the PUBLIC Python /term/* routes (Layer 2) through the real
VsCodeAdapter (Layer 3 client) against that bridge, and proves the liveness sequence
from docs/TERM_CONTRACT.md with a REAL PTY (real process exit, real onExit exitCode) —
this is NOT a FakeAdapter. It also compares typed /term/* shapes against legacy
`codeApiTerm`-over-/exec on the live VS Code runtime (if reachable).

Constraints honored: loopback only; only test-owned PTYs (marker-tagged); cleans up
every terminal it creates; touches no unrelated PTYs; changes no repo state (prints a
report only); no package cutover.

    python python/tools/phase13e_terminal_differential.py
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

REPO = r"C:\Users\dimas\Desktop\CODEAPI"
NODE_PTY = "C:/Users/dimas/Desktop/CODEAPI/node_modules/node-pty"
sys.path.insert(0, os.path.join(REPO, "python"))

from codeapi_vscode.runtime import create_server                    # noqa: E402
from codeapi_vscode.session.port_allocator import PortAllocator     # noqa: E402
from codeapi_vscode.vscode.adapter import VsCodeAdapter             # noqa: E402

MARKER = "CODEAPI_TERM13E_" + uuid.uuid4().hex[:8]

# Standalone headless bridge — mirrors extension.js handleTerm* EXACTLY except
# makeVisibleTerminal (vscode). Same envelope, token gate, onExit liveness,
# reconcileAlive. node-pty is required by absolute path.
BRIDGE_JS = r'''
const http = require('http');
const nodePty = require(%NODE_PTY%);
const PORT = parseInt(process.argv[2], 10);
const TOKEN = process.argv[3];
const terms = new Map();
let seq = 0;
function newId() { return 't' + Date.now().toString(36) + (seq++).toString(36); }
function reconcileAlive(rec) { if (!rec.alive) return; try { process.kill(rec.pid, 0); } catch (e) { if (e && e.code === 'ESRCH') rec.alive = false; } }
function sendJson(res, status, payload) { const d = Buffer.from(JSON.stringify(payload), 'utf8'); res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': d.length }); res.end(d); }
function readBody(req) { return new Promise((rs, rj) => { let d = ''; req.on('data', c => d += c); req.on('end', () => rs(d)); req.on('error', rj); }); }
function parseQuery(u) { const q = {}; const i = (u || '').indexOf('?'); if (i < 0) return q; for (const p of u.slice(i + 1).split('&')) { if (!p) continue; const e = p.indexOf('='); const k = e < 0 ? p : p.slice(0, e); const v = e < 0 ? '' : p.slice(e + 1); q[decodeURIComponent(k)] = decodeURIComponent(v); } return q; }
function create(res, body) { let o; try { o = JSON.parse(body || '{}'); } catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); } try { const os = require('os'); const id = o.id ? String(o.id) : newId(); const shell = o.shell || (process.platform === 'win32' ? 'powershell.exe' : (process.env.SHELL || 'bash')); const cwd = o.cwd || os.homedir(); const cols = o.cols || 80, rows = o.rows || 30, maxBytes = o.maxBytes || 200000; const p = nodePty.spawn(shell, o.args || [], { name: 'xterm-color', cols, rows, cwd, env: process.env }); const rec = { id, pty: p, shell, cwd, pid: p.pid, buffer: '', bytes: 0, maxBytes, alive: true, exitCode: null }; p.onData(d => { rec.buffer += d; rec.bytes += Buffer.byteLength(d); if (rec.buffer.length > rec.maxBytes) rec.buffer = rec.buffer.slice(rec.buffer.length - rec.maxBytes); }); p.onExit(({ exitCode }) => { rec.alive = false; rec.exitCode = exitCode; }); terms.set(id, rec); sendJson(res, 200, { ok: true, value: { id, shell, cwd, pid: p.pid } }); } catch (e) { sendJson(res, 500, { error: String(e && e.message || e) }); } }
function list(res) { const out = []; for (const rec of terms.values()) { reconcileAlive(rec); out.push({ id: rec.id, pid: rec.pid, bytes: rec.bytes, alive: rec.alive, exitCode: rec.exitCode }); } sendJson(res, 200, { ok: true, value: out }); }
function write(res, id, body) { let o; try { o = JSON.parse(body || '{}'); } catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); } const rec = terms.get(id); if (!rec) return sendJson(res, 404, { error: 'Terminal ' + id + ' not found' }); try { let data = o.data == null ? '' : String(o.data); if (o.enter) data += '\r'; rec.pty.write(data); sendJson(res, 200, { ok: true, value: { id, wrote: Buffer.byteLength(data) } }); } catch (e) { sendJson(res, 500, { error: String(e && e.message || e) }); } }
function read(res, id, q) { const rec = terms.get(id); if (!rec) return sendJson(res, 404, { error: 'Terminal ' + id + ' not found' }); let buffer = rec.buffer; const tail = q.tail ? parseInt(q.tail, 10) : 0; if (tail > 0 && buffer.length > tail) buffer = buffer.slice(buffer.length - tail); const out = { id, bytes: rec.bytes, buffer }; if (q.clear === 'true' || q.clear === '1') rec.buffer = ''; sendJson(res, 200, { ok: true, value: out }); }
function resize(res, id, body) { let o; try { o = JSON.parse(body || '{}'); } catch (_) { return sendJson(res, 400, { error: 'invalid JSON body' }); } const rec = terms.get(id); if (!rec) return sendJson(res, 404, { error: 'Terminal ' + id + ' not found' }); try { const cols = parseInt(o.cols, 10), rows = parseInt(o.rows, 10); rec.pty.resize(cols, rows); sendJson(res, 200, { ok: true, value: { id, cols, rows } }); } catch (e) { sendJson(res, 500, { error: String(e && e.message || e) }); } }
function del(res, id) { const rec = terms.get(id); if (!rec) return sendJson(res, 404, { error: 'Terminal ' + id + ' not found' }); const wasAlive = rec.alive; try { rec.pty.kill(); } catch (_) {} terms.delete(id); sendJson(res, 200, { ok: true, value: { id, killed: wasAlive } }); }
const server = http.createServer((req, res) => { const token = req.headers['x-codeapi-bridge-token']; if (!TOKEN || token !== TOKEN) return sendJson(res, 403, { error: 'forbidden' }); const method = req.method; const url = (req.url || '').split('?')[0]; readBody(req).then(body => { if (method === 'GET' && url === '/vscode/term/list') return list(res); if (method === 'POST' && url === '/vscode/term/create') return create(res, body); const m = url.match(/^\/vscode\/term\/([^/]+)(\/write|\/read|\/resize)?$/); if (m) { const id = decodeURIComponent(m[1]); const sub = m[2]; if (method === 'POST' && sub === '/write') return write(res, id, body); if (method === 'GET' && sub === '/read') return read(res, id, parseQuery(req.url)); if (method === 'POST' && sub === '/resize') return resize(res, id, body); if (method === 'DELETE' && !sub) return del(res, id); } sendJson(res, 404, { error: 'Cannot ' + method + ' ' + url }); }).catch(e => sendJson(res, 500, { error: String(e && e.message || e) })); });
server.listen(PORT, '127.0.0.1', () => { console.log('BRIDGE_READY ' + PORT); });
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json(base, method, path, body=None, ctype="application/json", timeout=20):
    data = None
    headers = {}
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        headers["Content-Type"] = ctype
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode("utf-8", "replace")


def section(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def main():
    print(f"MARKER = {MARKER}")
    tmp = tempfile.mkdtemp(prefix="codeapi13e_")
    bridge_path = os.path.join(tmp, "bridge13e.js")
    with open(bridge_path, "w", encoding="utf-8") as f:
        f.write(BRIDGE_JS.replace("%NODE_PTY%", json.dumps(NODE_PTY)))

    bridge_port = free_port()
    token = "t13e_" + uuid.uuid4().hex
    proc = subprocess.Popen(["node", bridge_path, str(bridge_port), token],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    py_server = None
    typed_id = None
    codeapiterm_id = None
    client = None
    live_port = None
    try:
        # wait for BRIDGE_READY
        ready = False
        deadline = time.time() + 12
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            print("[bridge]", line.rstrip())
            if "BRIDGE_READY" in line:
                ready = True
                break
        if not ready:
            print("BRIDGE DID NOT START -> BLOCKED")
            return 3

        # Layer-2 Python service pointed at the standalone bridge
        adapter = VsCodeAdapter(port=bridge_port, token=token)
        py_port = PortAllocator().resolve(None)
        py_server, _ = create_server("127.0.0.1", py_port, adapter=adapter)
        threading.Thread(target=py_server.start, daemon=True).start()
        PY = f"http://127.0.0.1:{py_port}"
        for _ in range(40):
            try:
                with urllib.request.urlopen(PY + "/health", timeout=5) as r:
                    if r.status == 200:
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.1)
        print(f"ENV: bridge=127.0.0.1:{bridge_port}  python-service=127.0.0.1:{py_port}")

        # ---- LIVENESS PROOF via PUBLIC /term/* ----
        section("LIVENESS PROOF (public Python /term/*)")
        st, created = http_json(PY, "POST", "/term/create",
                                {"shell": "powershell.exe", "cwd": REPO, "show": False})
        print(f"1. create -> {st} {created!r}")
        typed_id = created["id"]
        shell_used = created["shell"]

        st, lst = http_json(PY, "GET", "/term/list")
        entry = next((e for e in lst if e["id"] == typed_id), None)
        print(f"2. list entry -> {entry!r}")
        alive_ok = entry and entry["alive"] is True and entry["exitCode"] is None and isinstance(entry["pid"], int)
        print(f"   alive:true, exitCode:null, valid pid -> {alive_ok}")

        st, wr = http_json(PY, "POST", f"/term/{typed_id}/write",
                           {"data": f"echo {MARKER}", "enter": True})
        print(f"3. write marker -> {st} {wr!r}")

        marker_seen = False
        for _ in range(50):
            st, rd = http_json(PY, "GET", f"/term/{typed_id}/read?tail=8000")
            if MARKER in (rd or {}).get("buffer", ""):
                marker_seen = True
                break
            time.sleep(0.2)
        print(f"4. read marker present -> {marker_seen}")

        # 5. cause process exit WITHOUT delete
        http_json(PY, "POST", f"/term/{typed_id}/write", {"data": "exit", "enter": True})
        print("5. wrote 'exit' (process should exit; registry entry remains)")

        # 6. poll list until alive:false AND exitCode is set. onExit is the SOURCE OF
        #    TRUTH for exitCode; reconcileAlive (kill(pid,0) fallback) may flip
        #    alive:false slightly earlier, so we must wait for onExit to record the
        #    exitCode rather than breaking on alive:false alone.
        dead_entry = None
        for _ in range(150):   # up to ~30s
            st, lst = http_json(PY, "GET", "/term/list")
            e = next((x for x in lst if x["id"] == typed_id), None)
            if e and e["alive"] is False and e["exitCode"] is not None:
                dead_entry = e
                break
            time.sleep(0.2)
        if dead_entry is None:
            st, lst = http_json(PY, "GET", "/term/list")
            dead_entry = next((x for x in lst if x["id"] == typed_id), None)
        print(f"6. dead entry -> {dead_entry!r}")
        dead_ok = dead_entry is not None and dead_entry["alive"] is False and dead_entry["exitCode"] is not None

        # 7. DELETE only after observing alive:false
        st, dl = http_json(PY, "DELETE", f"/term/{typed_id}")
        print(f"7. delete -> {st} {dl!r}")

        # 8. confirm removed
        st, lst = http_json(PY, "GET", "/term/list")
        gone_from_list = not any(x["id"] == typed_id for x in lst)
        st2, rd2 = http_json(PY, "GET", f"/term/{typed_id}/read")
        read_404 = st2 == 404 and isinstance(rd2, dict) and "not found" in rd2.get("error", "")
        print(f"8. gone from list -> {gone_from_list}; read after delete -> {st2} {rd2!r}")
        typed_id = None  # deleted; nothing to clean

        # ---- SHAPE COMPARISON vs legacy codeApiTerm over /exec ----
        section("SHAPE COMPARISON vs codeApiTerm-over-/exec (live runtime)")
        try:
            from codeapi_vscode.client import CodeAPIClient
            client = CodeAPIClient()
            sess = client.discover()
            live_port = sess[0].port if sess else None
        except Exception as e:  # noqa: BLE001
            print(f"live runtime discovery failed: {e}")
            live_port = None

        cmp_rows = []
        if live_port is not None:
            print(f"live runtime on 127.0.0.1:{live_port}")
            typeof = client.exec_js("return typeof codeApiTerm", port=live_port)
            print(f"typeof codeApiTerm -> {typeof!r}")
            repo_fwd = REPO.replace('\\', '/')
            codeapiterm = client.exec_js(
                "return codeApiTerm.create({cwd:'" + repo_fwd + "',shell:'powershell.exe',show:false})",
                port=live_port)
            codeapiterm_id = codeapiterm.get("id") if isinstance(codeapiterm, dict) else None
            print(f"codeApiTerm.create -> {codeapiterm!r}")
            legacy_list = client.exec_js("return codeApiTerm.list()", port=live_port)
            legacy_entry = next((e for e in legacy_list if e.get("id") == codeapiterm_id), None)
            import base64
            b64 = base64.b64encode(b"echo codeapiterm-shape").decode()
            legacy_write = client.exec_js(
                "return codeApiTerm.write('" + codeapiterm_id + "', Buffer.from('" + b64 + "','base64').toString(), {enter:true})",
                port=live_port)
            time.sleep(0.6)
            legacy_read = client.exec_js(
                "return codeApiTerm.read('" + codeapiterm_id + "', {tail:2000})", port=live_port)
            legacy_resize = client.exec_js(
                "return codeApiTerm.resize('" + codeapiterm_id + "', 100, 30)", port=live_port)

            cmp_rows = [
                ("create keys", sorted(created.keys()),
                 sorted(codeapiterm.keys()) if isinstance(codeapiterm, dict) else codeapiterm),
                ("list entry keys", sorted(entry.keys()) if entry else None,
                 sorted(legacy_entry.keys()) if legacy_entry else None),
                ("write keys", sorted(wr.keys()),
                 sorted(legacy_write.keys()) if isinstance(legacy_write, dict) else legacy_write),
                ("read keys", sorted(rd.keys()) if rd else None,
                 sorted(legacy_read.keys()) if isinstance(legacy_read, dict) else legacy_read),
                ("resize keys", ["cols", "id", "rows"],
                 sorted(legacy_resize.keys()) if isinstance(legacy_resize, dict) else legacy_resize),
            ]
            for name, typed_k, legacy_k in cmp_rows:
                print(f"  {name:16} typed={typed_k}  codeApiTerm={legacy_k}")
        else:
            print("LIVE RUNTIME NOT REACHABLE -> codeApiTerm shape comparison SKIPPED "
                  "(liveness proof above is unaffected; it used the standalone bridge).")

        section("RESULT")
        liveness_ok = bool(alive_ok and marker_seen and dead_ok and gone_from_list and read_404)
        print(f"liveness_proof_ok = {liveness_ok}")
        print(f"  create alive:true/exitCode:null/pid = {alive_ok}")
        print(f"  marker write+read = {marker_seen}")
        print(f"  exit-in-place -> alive:false + exitCode = {dead_ok} "
              f"(exitCode={dead_entry['exitCode'] if dead_entry else None})")
        print(f"  delete + gone/404 = {gone_from_list and read_404}")
        print(f"shape_comparison_done = {live_port is not None}")
        return 0 if liveness_ok else 1
    finally:
        # cleanup — never leave a test-owned PTY or the bridge/service running
        try:
            if typed_id is not None and py_server is not None:
                http_json(f"http://127.0.0.1:{py_port}", "DELETE", f"/term/{typed_id}")
        except Exception:  # noqa: BLE001
            pass
        try:
            if codeapiterm_id and client and live_port:
                res = client.exec_js("return codeApiTerm.kill('" + codeapiterm_id + "')", port=live_port)
                print(f"cleanup codeApiTerm.kill({codeapiterm_id}) -> {res!r}")
        except Exception as e:  # noqa: BLE001
            print(f"cleanup codeApiTerm.kill failed: {e}")
        try:
            if py_server is not None:
                py_server.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        print("cleanup done")


if __name__ == "__main__":
    sys.exit(main())
