#!/usr/bin/env python3
"""P3/P4 — a production-created terminal reaches READY from the real bootstrap.

Nothing here is a probe. The terminal is created through the real API, the
shell integration comes from the production spawn path, the OSC parsing runs
in the extension at p.onData, and the state is read back through `term list`.

    python python/tests/test_shell_readiness_live.py --port <hostB>
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode.client import CodeAPIClient        # noqa: E402

PS_TID = "codeapi-ready-ps"
HEADLESS_TID = "codeapi-ready-headless"
CMD_TID = "codeapi-ready-cmd"


def check(results, label, cond, detail=""):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


def row(c, tid, kw):
    for r in c.term.list(**kw):
        if r.id == tid:
            return r
    return None


def wait_ready(c, tid, kw, timeout=30):
    """Poll term list until the shell reports READY, or give up."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = row(c, tid, kw)
        if r is not None:
            last = r
            if getattr(r, "shellState", None) == "READY":
                return r, True
        time.sleep(1.0)
    return last, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    kw = {"port": ap.parse_args().port}
    results = []
    c = CodeAPIClient()
    print("=== PRODUCTION READINESS (port {}) ===".format(kw["port"]))

    for tid in (PS_TID, HEADLESS_TID, CMD_TID):
        try:
            c.term.kill(tid, **kw)
        except Exception:      # noqa: BLE001
            pass

    try:
        # --- P3 visible PowerShell, created the ordinary way --------------
        made = c.term.create(id=PS_TID, shell="powershell.exe",
                             cwd="C:\\Users\\dimas", show=True, **kw)
        print("created {}".format(made))
        gen = getattr(made, "generation", None)
        check(results, "P3 create returns a generation", bool(gen), "gen={}".format(gen))

        r, ready = wait_ready(c, PS_TID, kw)
        print("  observed row: {}".format(r))
        check(results, "P3 bootstrap reports injected",
              getattr(r, "shellIntegrationBootstrap", None) == "injected",
              "bootstrap={}".format(getattr(r, "shellIntegrationBootstrap", None)))
        check(results, "P3 shell integration became ACTIVE from the real spawn",
              getattr(r, "shellIntegrationActive", None) is True,
              "the extension parsed OSC 633 out of its own p.onData")
        check(results, "P3 production PowerShell terminal reaches READY",
              ready and getattr(r, "shellState", None) == "READY",
              "shellState={}".format(getattr(r, "shellState", None)))
        check(results, "P3 generation is stable between create and list",
              getattr(r, "generation", None) == gen)
        check(results, "P3 no execution owns a freshly created terminal",
              getattr(r, "activeExecutionId", None) is None)
        check(results, "P3 the shell is idle, not mid-command",
              getattr(r, "shellCommandActive", None) is False)

        # --- P4 headless: same engine, no VS Code Terminal object ---------
        madeH = c.term.create(id=HEADLESS_TID, shell="powershell.exe",
                              cwd="C:\\Users\\dimas", show=False, **kw)
        print("created {}".format(madeH))
        check(results, "P4 headless terminal reports surface=process",
              getattr(madeH, "surface", None) == "process",
              "surface={}".format(getattr(madeH, "surface", None)))
        rh, readyH = wait_ready(c, HEADLESS_TID, kw)
        print("  observed row: {}".format(rh))
        check(results, "P4 headless reaches READY with NO vscode.Terminal",
              readyH and getattr(rh, "shellState", None) == "READY",
              "this is why the parser lives at node-pty, not "
              "Terminal.shellIntegration")
        check(results, "P4 headless bootstrap also injected",
              getattr(rh, "shellIntegrationBootstrap", None) == "injected")

        # --- cmd: bootstrap correctly declines, readiness never claimed ---
        c.term.create(id=CMD_TID, cwd="C:\\Users\\dimas", **kw)
        time.sleep(6.0)
        rc = row(c, CMD_TID, kw)
        print("  observed row: {}".format(rc))
        check(results, "cmd terminal is not bootstrapped",
              getattr(rc, "shellIntegrationBootstrap", None) == "not_powershell",
              "bootstrap={}".format(getattr(rc, "shellIntegrationBootstrap", None)))
        check(results, "cmd terminal never claims READY",
              getattr(rc, "shellState", None) != "READY",
              "shellState={} — unready and it can say why".format(
                  getattr(rc, "shellState", None)))
    finally:
        for tid in (PS_TID, HEADLESS_TID, CMD_TID):
            try:
                print("cleanup {}: {}".format(tid, c.term.kill(tid, **kw)))
            except Exception as e:      # noqa: BLE001
                print("cleanup {} failed: {}".format(tid, e))

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*54}")
    print(f"production readiness: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
