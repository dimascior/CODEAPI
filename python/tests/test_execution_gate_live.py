#!/usr/bin/env python3
"""P6-P12 — the wired execution gate, end to end on a production terminal.

Every case goes through the real TerminalClient against a real CODEAPI
terminal whose readiness comes from the production shell-integration
bootstrap. Nothing is simulated.

    python python/tests/test_execution_gate_live.py --port <hostB>
"""

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode import admission as adm            # noqa: E402
from codeapi_vscode.client import CodeAPIClient        # noqa: E402

TID = "codeapi-gate-ps"
GEN_TID = "codeapi-gate-gen"
HEADLESS_TID = "codeapi-gate-headless"


def check(results, label, cond, detail=""):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


def row(c, tid, kw):
    for r in c.term.list(**kw):
        if r.id == tid:
            return r
    return None


def wait_state(c, tid, kw, want, timeout=30):
    end = time.time() + timeout
    r = None
    while time.time() < end:
        r = row(c, tid, kw)
        if r is not None and getattr(r, "shellState", None) == want:
            return r, True
        time.sleep(0.7)
    return r, False


def make(c, tid, kw, show=True):
    try:
        c.term.kill(tid, **kw)
    except Exception:      # noqa: BLE001
        pass
    made = c.term.create(id=tid, shell="powershell.exe",
                         cwd="C:\\Users\\dimas", show=show, **kw)
    wait_state(c, tid, kw, "READY")
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    kw = {"port": ap.parse_args().port}
    results = []
    c = CodeAPIClient()
    print("=== EXECUTION GATE (port {}) ===".format(kw["port"]))

    try:
        made = make(c, TID, kw)
        gen = made.generation

        # --- successful lifecycle + execution-id identity ----------------
        ev, out = c.term.run_evidence(TID, "cmd /c exit 0", timeout=30,
                                      expected_generation=gen, **kw)
        check(results, "P6 run completes through the atomic submit",
              ev.status == adm.STATUS_COMPLETED and ev.exit_code == 0,
              "status={} exit={}".format(ev.status, ev.exit_code))
        check(results, "P6 ONE execution id: submit == BEGIN/END == evidence",
              ev.execution_id and adm.begin_marker(ev.execution_id)
              and ev.begin_observed and ev.end_observed,
              "execution_id={}".format(ev.execution_id))
        check(results, "P6 evidence carries the terminal generation",
              ev.terminal_generation == gen)
        check(results, "P6 pre-submit state was READY",
              ev.pre_submit_state == "READY",
              "pre_submit_state={}".format(ev.pre_submit_state))

        # --- automatic release: no /release call was made -----------------
        r, back = wait_state(c, TID, kw, "READY")
        check(results, "P8 shell returns to READY after the run", back)
        check(results, "P8 extension AUTOMATICALLY cleared activeExecutionId",
              getattr(r, "activeExecutionId", None) is None,
              "python never called /release")

        # --- pager prevention --------------------------------------------
        before = c.term.read(TID, tail=1, **kw).bytes
        c.term.write(TID, "1..500 | more", enter=True, **kw)
        rb, busy = wait_state(c, TID, kw, "BUSY", timeout=15)
        check(results, "P-pager foreground child drives the shell BUSY", busy,
              "shellState={}".format(getattr(rb, "shellState", None)))

        gated = c.term.read(TID, tail=1, **kw).bytes
        ev2, _ = c.term.run_evidence(TID, "cmd /c exit 0", timeout=15,
                                     expected_generation=gen, **kw)
        after = c.term.read(TID, tail=1, **kw).bytes
        check(results, "P-pager second run is PROVEN_NOT_SUBMITTED",
              ev2.status == adm.STATUS_PROVEN_NOT_SUBMITTED
              and ev2.not_submitted_reason == "SHELL_NOT_READY"
              and ev2.submitted is False and ev2.submission_accepted is False,
              "status={} reason={}".format(ev2.status, ev2.not_submitted_reason))
        check(results, "P-pager ZERO PTY bytes were written",
              after == gated,
              "bytes {} -> {} (the pager kept printing before the gate)".format(
                  gated, after))

        # raw write is still permitted while BUSY — this is the escape hatch
        c.term.write(TID, "q", enter=False, **kw)
        r3, back2 = wait_state(c, TID, kw, "READY", timeout=20)
        check(results, "P-pager raw term write still works while BUSY", back2,
              "q reached the pager and the shell returned to READY")

        ev3, _ = c.term.run_evidence(TID, "cmd /c exit 7", timeout=30,
                                     expected_generation=gen, **kw)
        check(results, "P-pager the next run then completes normally",
              ev3.status == adm.STATUS_COMPLETED and ev3.exit_code == 7,
              "status={} exit={}".format(ev3.status, ev3.exit_code))

        # --- concurrency --------------------------------------------------
        wait_state(c, TID, kw, "READY")
        outcomes = []
        lock = threading.Lock()

        def fire():
            e, _o = c.term.run_evidence(TID, "Start-Sleep -Seconds 3",
                                        timeout=25, expected_generation=gen, **kw)
            with lock:
                outcomes.append(e)

        ts = [threading.Thread(target=fire) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        subs = [e for e in outcomes if e.submitted]
        refused = [e for e in outcomes if e.status == adm.STATUS_PROVEN_NOT_SUBMITTED]
        check(results, "P-concurrency exactly one of two runs was submitted",
              len(subs) == 1 and len(refused) == 1,
              "submitted={} refused={} reasons={}".format(
                  len(subs), len(refused), [e.not_submitted_reason for e in refused]))
        check(results, "P-concurrency the refusal names a real exclusion reason",
              refused and refused[0].not_submitted_reason in
              ("EXECUTION_IN_PROGRESS", "SHELL_NOT_READY"),
              "no dual reservation, no interleaved envelopes")

        # --- generation reset ---------------------------------------------
        madeG = make(c, GEN_TID, kw)
        g1 = madeG.generation
        c.term.kill(GEN_TID, **kw)
        madeG2 = make(c, GEN_TID, kw)
        g2 = madeG2.generation
        check(results, "P-generation recreate yields a NEW generation",
              g1 and g2 and g1 != g2, "g1={} g2={}".format(g1, g2))
        gb = c.term.read(GEN_TID, tail=1, **kw).bytes
        evG, _ = c.term.run_evidence(GEN_TID, "cmd /c exit 0", timeout=15,
                                     expected_generation=g1, **kw)
        ga = c.term.read(GEN_TID, tail=1, **kw).bytes
        check(results, "P-generation stale generation is refused",
              evG.status == adm.STATUS_PROVEN_NOT_SUBMITTED
              and evG.not_submitted_reason == "TERMINAL_GENERATION_MISMATCH",
              "reason={}".format(evG.not_submitted_reason))
        check(results, "P-generation stale submission wrote ZERO bytes",
              ga == gb, "bytes {} -> {}".format(gb, ga))
        evG2, _ = c.term.run_evidence(GEN_TID, "cmd /c exit 0", timeout=30,
                                      expected_generation=g2, **kw)
        check(results, "P-generation the current generation succeeds",
              evG2.status == adm.STATUS_COMPLETED)

        # --- run_verified convergence -------------------------------------
        wait_state(c, TID, kw, "READY")
        rv = c.term.run_verified(TID, "cmd /c exit 0", timeout=30, **kw)
        check(results, "P-run_verified uses the one engine and still returns ok",
              rv.ok is True and rv.run_id.startswith("CodeAPI_codeapi-"),
              "run_id={}".format(rv.run_id))
        check(results, "P-run_verified carries no __CodeAPI_ protocol",
              "__CodeAPI_" not in str(rv.dispatch),
              "dispatch={}".format(rv.dispatch))

        # --- headless execution -------------------------------------------
        madeH = make(c, HEADLESS_TID, kw, show=False)
        evH, _ = c.term.run_evidence(HEADLESS_TID, "cmd /c exit 4", timeout=30,
                                     expected_generation=madeH.generation, **kw)
        check(results, "P-headless executes through the same gate",
              evH.status == adm.STATUS_COMPLETED and evH.exit_code == 4,
              "surface=process, no vscode.Terminal; status={}".format(evH.status))
    finally:
        for tid in (TID, GEN_TID, HEADLESS_TID):
            try:
                c.term.kill(tid, **kw)
            except Exception:      # noqa: BLE001
                pass
        print("cleanup done")

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*54}")
    print(f"execution gate live: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
