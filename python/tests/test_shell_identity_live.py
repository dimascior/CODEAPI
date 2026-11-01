#!/usr/bin/env python3
"""S11-S15 — terminal shell identity drives execution dialect.

Runs against an ISOLATED Extension Development Host (Host B) that loaded the
current working tree. Host A — the pre-existing window holding tmbrwfix1 — is
only ever READ, never reloaded, written to or killed.

    python python/tests/test_shell_identity_live.py --port <hostB> [--host-a 47337]
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode import admission as adm            # noqa: E402
from codeapi_vscode.client import CodeAPIClient        # noqa: E402
from codeapi_vscode.terminal_client import TermClient  # noqa: E402

CMD_TID = "codeapi-shell-cmd"
PS_TID = "codeapi-shell-ps"


def check(results, label, cond, detail=""):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


class _StubTerminals(TermClient):
    """Client boundary with a canned /term/list. No runtime, no PTY.

    Used for S14: shells that cannot be produced on demand (absent, empty,
    unrecognised, POSIX-on-Windows) still have to be refused, and the refusal
    has to happen before any adapter call. `write` raises so a write attempt
    cannot pass silently."""

    def __init__(self, rows):
        self._rows = rows
        self.writes = []

    def list(self, **kw):
        return self._rows

    def write(self, *a, **k):
        self.writes.append((a, k))
        raise AssertionError("write must never be reached for an "
                             "unsupported dialect")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host-a", type=int, default=47337)
    args = ap.parse_args()
    kw = {"port": args.port}
    results = []
    c = CodeAPIClient()

    print("=== S15 HOST A BEFORE (read-only) ===")
    a_kw = {"port": args.host_a}
    a_before = {t.id: t for t in c.term.list(**a_kw)}
    brw_before = a_before.get("tmbrwfix1")
    print("  hostA terminals={} tmbrwfix1={}".format(
        len(a_before),
        {"pid": brw_before.pid, "alive": brw_before.alive} if brw_before else None))

    for tid in (CMD_TID, PS_TID):
        try:
            c.term.kill(tid, **kw)
        except Exception:      # noqa: BLE001
            pass

    # --- S11 default-create: no shell argument --------------------------
    made_cmd = c.term.create(id=CMD_TID, cwd="C:\\Users\\dimas", **kw)
    # --- S12 explicit PowerShell ----------------------------------------
    made_ps = c.term.create(id=PS_TID, shell="powershell.exe",
                            cwd="C:\\Users\\dimas", **kw)
    time.sleep(6.0)

    try:
        # S13 step 3/4: DISCARD the create results for the selection path and
        # rediscover both terminals through term list, by id alone.
        rows = {r.id: r for r in c.term.list(**kw)}
        cmd_row, ps_row = rows.get(CMD_TID), rows.get(PS_TID)

        check(results, "S10 Host B term list exposes shell",
              cmd_row is not None and cmd_row.shell is not None,
              "shell={!r}".format(getattr(cmd_row, "shell", "<missing row>")))
        check(results, "S10 tmbrwfix1 is absent from Host B",
              "tmbrwfix1" not in rows, "ids={}".format(sorted(rows)))

        check(results, "S11 default create reports cmd.exe (COMSPEC contract)",
              adm.dialect_for_shell(made_cmd.shell) == adm.DIALECT_CMD,
              "shell={!r}".format(made_cmd.shell))
        check(results, "S11 list shell == create shell, same id, byte-identical",
              cmd_row.shell == made_cmd.shell
              and ps_row.shell == made_ps.shell,
              "list={!r} create={!r}".format(cmd_row.shell, made_cmd.shell))
        check(results, "S13 the two terminals carry DIFFERENT shells",
              cmd_row.shell != ps_row.shell,
              "cmd={!r} ps={!r}".format(cmd_row.shell, ps_row.shell))

        # S11/S13: cmd terminal, addressed BY ID only -> refused, zero write.
        before = c.term.read(CMD_TID, tail=1, **kw).bytes
        refused = None
        try:
            c.term.run_evidence(CMD_TID, "cmd /c exit 0", timeout=10, **kw)
        except adm.UnsupportedShellError as e:
            refused = e
        after = c.term.read(CMD_TID, tail=1, **kw).bytes
        check(results, "S11 cmd terminal: run refuses the unsupported dialect",
              refused is not None and refused.dialect == adm.DIALECT_CMD
              and refused.terminal_id == CMD_TID,
              str(refused)[:90] if refused else "no exception raised")
        check(results, "S11 rejection wrote ZERO bytes (byte counter unchanged)",
              after == before, "before={} after={}".format(before, after))

        # S12/S13: PowerShell terminal, addressed BY ID only -> completes.
        ev, _ = c.term.run_evidence(PS_TID, "cmd /c exit 0", timeout=25, **kw)
        check(results, "S12 PowerShell terminal: BEGIN, END, COMPLETED, exit 0",
              ev.begin_observed and ev.end_observed
              and ev.status == adm.STATUS_COMPLETED and ev.exit_code == 0,
              "status={} exit={}".format(ev.status, ev.exit_code))
        ev2, _ = c.term.run_evidence(PS_TID, "cmd /c exit 5", timeout=25, **kw)
        check(results, "S12 PowerShell terminal: native failure captured",
              ev2.exit_code == 5 and ev2.native_exit_code == 5
              and ev2.powershell_success is False,
              "exit={}".format(ev2.exit_code))

        # The selection is metadata-driven, not platform-driven: ONE process,
        # ONE COMSPEC, two terminals, opposite outcomes.
        check(results, "S13 selection came from terminal metadata, not the OS",
              adm.dialect_for_shell(cmd_row.shell) == adm.DIALECT_CMD
              and adm.dialect_for_shell(ps_row.shell) == adm.DIALECT_POWERSHELL
              and refused is not None and ev.status == adm.STATUS_COMPLETED,
              "same process and same environment, opposite dialects")
    finally:
        print("cleanup: {}".format(c.term.kill(CMD_TID, **kw)))
        print("cleanup: {}".format(c.term.kill(PS_TID, **kw)))

    # --- S14 unknown shells, at the client boundary ----------------------
    from codeapi_vscode.types import TermInfo             # noqa: E402
    for label, shell in [
        ("absent (legacy runtime)", None),
        ("empty string", ""),
        ("unrecognised executable", "C:\\tools\\nushell\\nu.exe"),
        ("POSIX shell", "/bin/bash"),
    ]:
        stub = _StubTerminals([TermInfo(id="tX", pid=1, bytes=0, shell=shell,
                                        alive=True, exitCode=None)])
        raised = None
        try:
            stub._dialect("tX")
        except adm.UnsupportedShellError as e:
            raised = e
        check(results, "S14 shell {} -> refused, zero adapter write".format(label),
              raised is not None and not stub.writes
              and raised.dialect in (adm.DIALECT_UNKNOWN, adm.DIALECT_POSIX),
              "dialect={}".format(raised.dialect if raised else None))

    check(results, "S14 an absent shell is never guessed from the OS",
          adm.dialect_for_shell(None) == adm.DIALECT_UNKNOWN)

    # --- S15 Host A preservation -----------------------------------------
    print("\n=== S15 HOST A AFTER (read-only) ===")
    a_after = {t.id: t for t in c.term.list(**a_kw)}
    brw_after = a_after.get("tmbrwfix1")
    check(results, "S15 tmbrwfix1 still exists on Host A with the same id",
          brw_after is not None)
    check(results, "S15 tmbrwfix1 is still alive with the same pid",
          brw_after is not None and brw_before is not None
          and brw_after.alive is True and brw_after.pid == brw_before.pid,
          "pid before={} after={} alive={}".format(
              getattr(brw_before, "pid", None), getattr(brw_after, "pid", None),
              getattr(brw_after, "alive", None)))
    check(results, "S15 Host A terminal set is unchanged",
          sorted(a_after) == sorted(a_before),
          "before={} after={}".format(len(a_before), len(a_after)))
    # Byte equality is deliberately NOT asserted: a live terminal may still be
    # producing output, so a changed counter is not evidence of interference.
    print("  note: tmbrwfix1 bytes before={} after={} (not an invariant)".format(
        getattr(brw_before, "bytes", None), getattr(brw_after, "bytes", None)))

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*54}")
    print(f"shell identity live: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
