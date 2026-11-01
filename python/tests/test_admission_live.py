#!/usr/bin/env python3
"""A22-A25 — end-to-end execution admission through a REAL PTY.

Every case here goes through the production TerminalClient.run_evidence path
against a live CodeAPI runtime. Nothing is fed to the classifier by hand: that
is the difference between a parser test and an admission proof.

Creates its own disposable terminals and deletes them. tmbrwfix1 is never
named, read, written or deleted — it stays as preserved historical evidence.

    python python/tests/test_admission_live.py --port 47337
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode import admission as adm            # noqa: E402
from codeapi_vscode.client import CodeAPIClient        # noqa: E402

# The runtime port is a PARAMETER, not a constant. This suite has to be able to
# run against an isolated extension host as well as the ambient one, and a
# hard-coded port silently pins it to whichever runtime happens to be at 47337
# — which is precisely the stale-extension situation these tests exist to
# distinguish from a real failure.
PORT = 47337
NORMAL_TID = "codeapi-live-a"
PAGER_TID = "codeapi-live-b"


def check(results, label, cond, detail=""):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


def make_terminal(c, tid, kw):
    """A disposable PowerShell terminal. The shell is named EXPLICITLY: the
    extension defaults to COMSPEC on Windows, which measured as cmd.exe here,
    and a cmd.exe terminal cannot evaluate a PowerShell envelope."""
    try:
        c.term.kill(tid, **kw)          # in case a prior run left one behind
    except Exception:                   # noqa: BLE001
        pass
    created = c.term.create(id=tid, shell="powershell.exe",
                            cwd="C:\\Users\\dimas", **kw)
    time.sleep(6.0)                     # reach a prompt
    return created


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    kw = {"port": ap.parse_args().port}
    print("=== runtime port {} ===".format(kw["port"]))
    results = []
    c = CodeAPIClient()
    print("=== LIVE ADMISSION ACCEPTANCE ===")

    created = make_terminal(c, NORMAL_TID, kw)
    print("created {}".format(created))
    try:
        # --- A22 normal success -------------------------------------------
        ev, out = c.term.run_evidence(NORMAL_TID, "cmd /c exit 0",
                                      timeout=25, **kw)
        check(results, "LIVE success: submission accepted AND admitted",
              ev.submission_accepted is True and ev.begin_observed is True)
        check(results, "LIVE success: COMPLETED with native exit 0",
              ev.end_observed is True and ev.status == adm.STATUS_COMPLETED
              and ev.exit_code == 0
              and ev.powershell_success is True and ev.native_exit_code == 0,
              "ps_ok={} native={} exit={}".format(
                  ev.powershell_success, ev.native_exit_code, ev.exit_code))
        check(results, "LIVE success: observation boundary captured",
              isinstance(ev.observation_start_offset, int)
              and isinstance(ev.observation_end_offset, int)
              and ev.observation_end_offset >= ev.observation_start_offset,
              "start={} end={}".format(ev.observation_start_offset,
                                       ev.observation_end_offset))
        check(results, "LIVE success: evidence names the command by sha256",
              ev.wrapped_command_sha256 == adm.command_sha256("cmd /c exit 0"))

        # --- A22 native failure -------------------------------------------
        ev, _ = c.term.run_evidence(NORMAL_TID, "cmd /c exit 3", timeout=25, **kw)
        check(results, "LIVE native failure: COMPLETED, exit 3 captured",
              ev.status == adm.STATUS_COMPLETED and ev.exit_code == 3
              and ev.native_exit_code == 3
              and ev.powershell_success is False,
              "status={} reason={} pre={} ps_ok={} exit={}".format(
                  ev.status, ev.not_submitted_reason, ev.pre_submit_state,
                  ev.powershell_success, ev.exit_code))

        # --- A22 PowerShell failure semantics ------------------------------
        ev, _ = c.term.run_evidence(
            NORMAL_TID, "Get-Item C:\\definitely\\missing\\path",
            timeout=25, **kw)
        check(results, "LIVE cmdlet failure: powershell_failure, no native code",
              ev.status == adm.STATUS_COMPLETED
              and ev.powershell_success is False
              and ev.native_exit_code is None
              and ev.exit_code == 1,
              "ps_ok={} native={}".format(ev.powershell_success,
                                          ev.native_exit_code))

        ev, _ = c.term.run_evidence(NORMAL_TID, "Get-Item C:\\Windows",
                                    timeout=25, **kw)
        check(results, "LIVE cmdlet success: no stale native code leaks in",
              ev.powershell_success is True and ev.native_exit_code is None
              and ev.exit_code == 0,
              "the PREVIOUS live case set $LASTEXITCODE=3; clearing it "
              "before the envelope is what keeps this None")

        # --- A18/A22 admitted but unfinished -------------------------------
        # Sleeps past the observation window: BEGIN executes, END does not.
        ev, _ = c.term.run_evidence(NORMAL_TID, "Start-Sleep -Seconds 30",
                                    timeout=10, **kw)
        check(results, "LIVE unfinished: ADMITTED, completion unknown",
              ev.begin_observed is True and ev.end_observed is False
              and ev.status == adm.STATUS_ADMITTED
              and ev.exit_code is None
              and ev.powershell_success is None,
              "status={} exit={}".format(ev.status, ev.exit_code))
        time.sleep(22)                  # let the sleep finish before reuse

        # --- A18 command scoping -------------------------------------------
        for label, command, want_ok in [
            ("semicolon chain", "Write-Output 'a'; Write-Output 'b'", True),
            ("quoted semicolon", "Write-Output 'a;b'", True),
            ("pipeline", "Get-ChildItem C:\\Windows | Select-Object -First 1",
             True),
            ("parentheses", "(Get-Item C:\\Windows).Name", True),
            ("script block", "& { Write-Output 'sb' }", True),
            ("quoted string with quotes", 'Write-Output "he said ''hi''"', True),
            ("$LASTEXITCODE reference", "Write-Output $LASTEXITCODE", True),
            ("$? reference", "Write-Output $?", True),
            ("Write-Host", "Write-Host 'host'", True),
        ]:
            ev, out = c.term.run_evidence(NORMAL_TID, command, timeout=25, **kw)
            check(results, "LIVE scoping {}: envelope survives".format(label),
                  ev.begin_observed and ev.end_observed
                  and ev.powershell_success is want_ok,
                  "status={} ps_ok={} out={!r}".format(
                      ev.status, ev.powershell_success, out[:40]))

        # A18: a terminating error legitimately yields BEGIN without END.
        ev, _ = c.term.run_evidence(NORMAL_TID, "throw 'boom'", timeout=12, **kw)
        check(results, "LIVE throw: BEGIN observed, END absent -> ADMITTED",
              ev.begin_observed is True and ev.end_observed is False
              and ev.status == adm.STATUS_ADMITTED,
              "a terminating error prevents the trailing statement; that is "
              "legitimate evidence, not a detector failure")
    finally:
        print("cleanup A: {}".format(c.term.kill(NORMAL_TID, **kw)))

    # --- A24/A25 foreground interception, on a SEPARATE disposable ---------
    created = make_terminal(c, PAGER_TID, kw)
    print("created {}".format(created))
    try:
        ev, _ = c.term.run_evidence(PAGER_TID, "cmd /c exit 0", timeout=25, **kw)
        check(results, "LIVE pager: ordinary execution works FIRST",
              ev.status == adm.STATUS_COMPLETED,
              "establishes the terminal is healthy before interception")

        # A deterministic foreground owner that consumes stdin and never
        # returns to the prompt. `more` is chosen over a pager like less
        # because it ships with Windows and blocks on input predictably.
        c.term.write(PAGER_TID, "cmd /c more", enter=True, **kw)
        time.sleep(3.0)

        ev, _ = c.term.run_evidence(PAGER_TID, "Write-Output 'never-runs'",
                                    timeout=15, **kw)
        # These two assertions MOVED WITH THE BEHAVIOUR, and the move is the
        # point of the whole phase. Phase A could only observe this case AFTER
        # the write: the envelope went into a pager-held terminal and the
        # verdict was ADMISSION_UNKNOWN — honest, but the weakest true thing
        # that could be said. Phase B refuses it before any byte is written, so
        # what used to be "we cannot tell whether it ran" is now "it provably
        # did not". Asserting the old, weaker outcome here would be asserting
        # that the fix is absent. ADMISSION_UNKNOWN is still reachable — a
        # shell that accepts the write and never evaluates it — but a terminal
        # a foreground child is holding is no longer the way to reach it.
        check(results, "LIVE PAGER INTERCEPT: refused BEFORE any write",
              ev.submitted is False and ev.submission_accepted is False
              and ev.status == adm.STATUS_PROVEN_NOT_SUBMITTED,
              "status={} submitted={}".format(ev.status, ev.submitted))
        check(results, "LIVE PAGER INTERCEPT: the refusal names the shell state",
              ev.not_submitted_reason == "SHELL_NOT_READY"
              and ev.begin_observed is False and ev.end_observed is False,
              "reason={} pre={}".format(ev.not_submitted_reason,
                                        ev.pre_submit_state))
        check(results, "LIVE PAGER INTERCEPT: no result invented",
              ev.exit_code is None and ev.powershell_success is None
              and ev.native_exit_code is None)

        # A24: the submitted envelope IS visible through echo, and still does
        # not admit. This is the epistemic failure being closed, not a string
        # construction test.
        raw = c.term.read(PAGER_TID, tail=200000, **kw)
        buf = getattr(raw, "buffer", "") or ""
        fragment = adm.BEGIN_WORD[:len(adm.BEGIN_WORD) - 2]
        check(results, "LIVE ANTI-ECHO: submitted envelope IS visible in output",
              fragment in buf,
              "the encoded fragment {!r} was echoed by the PTY".format(fragment))
        check(results, "LIVE ANTI-ECHO: the runtime BEGIN marker is ABSENT",
              adm.begin_marker(ev.execution_id) not in buf,
              "echo displays the split form; only shell evaluation joins it")
        check(results, "LIVE ANTI-ECHO: begin_observed stays False despite echo",
              adm.classify(buf, ev.execution_id,
                           submitted=True).begin_observed is False)
    finally:
        print("cleanup B: {}".format(c.term.kill(PAGER_TID, **kw)))

    # --- S2/S5/S6 shell identity and dialect selection ---------------------
    DEFAULT_TID = "codeapi-live-c"
    PS_TID = "codeapi-live-d"
    for tid in (DEFAULT_TID, PS_TID):
        try:
            c.term.kill(tid, **kw)
        except Exception:      # noqa: BLE001
            pass

    # S5: the default-create path — the one that exposed the whole defect.
    default_created = c.term.create(id=DEFAULT_TID, cwd="C:\\Users\\dimas", **kw)
    explicit_created = c.term.create(id=PS_TID, shell="powershell.exe",
                                     cwd="C:\\Users\\dimas", **kw)
    time.sleep(6.0)
    try:
        rows = {r.id: r for r in c.term.list(**kw)}
        d_row = rows.get(DEFAULT_TID)
        p_row = rows.get(PS_TID)

        check(results, "S2 term list now reports shell at all",
              d_row is not None and d_row.shell is not None,
              "shell={!r}".format(getattr(d_row, "shell", "<missing row>")))
        check(results, "S2 list shell matches the create response EXACTLY",
              d_row.shell == default_created.shell
              and p_row.shell == explicit_created.shell,
              "list={!r} create={!r}".format(d_row.shell,
                                             default_created.shell))
        check(results, "S6 the two terminals report DIFFERENT shells",
              d_row.shell != p_row.shell,
              "default={!r} explicit={!r}".format(d_row.shell, p_row.shell))
        check(results, "S2 existing list fields are preserved",
              isinstance(d_row.pid, int) and isinstance(d_row.bytes, int)
              and d_row.alive is True and d_row.exitCode is None
              and d_row.surface == "vscode-terminal")

        # S4/S5: the measured Windows default is cmd.exe.
        check(results, "S4 default-created terminal reports cmd.exe",
              adm.dialect_for_shell(d_row.shell) == adm.DIALECT_CMD,
              "shell={!r}".format(d_row.shell))

        # S5 outcome 3: refuse BEFORE writing. Proven by byte count, not by
        # the exception alone — an exception raised after a write would still
        # have put PowerShell syntax into a cmd prompt.
        before = c.term.read(DEFAULT_TID, tail=1, **kw).bytes
        refused = None
        try:
            c.term.run_evidence(DEFAULT_TID, "cmd /c exit 0", timeout=10, **kw)
        except adm.UnsupportedShellError as e:
            refused = e
        after = c.term.read(DEFAULT_TID, tail=1, **kw).bytes
        check(results, "S5 default terminal: run REFUSES the unsupported shell",
              refused is not None and refused.dialect == adm.DIALECT_CMD,
              str(refused)[:80] if refused else "no exception raised")
        check(results, "S5 refusal wrote ZERO bytes to the terminal",
              after == before,
              "bytes before={} after={}".format(before, after))

        # S6: the same call against the PowerShell terminal proceeds, chosen
        # from metadata rather than from the environment.
        ev, _ = c.term.run_evidence(PS_TID, "cmd /c exit 0", timeout=25, **kw)
        check(results, "S6 PowerShell terminal: dialect accepted, COMPLETED",
              ev.status == adm.STATUS_COMPLETED and ev.exit_code == 0,
              "status={} exit={}".format(ev.status, ev.exit_code))
        check(results, "S6 selection used terminal metadata, not the "
              "environment",
              adm.dialect_for_shell(p_row.shell) == adm.DIALECT_POWERSHELL
              and adm.dialect_for_shell(d_row.shell) == adm.DIALECT_CMD,
              "same process, same COMSPEC, opposite outcomes")
    finally:
        print("cleanup C: {}".format(c.term.kill(DEFAULT_TID, **kw)))
        print("cleanup D: {}".format(c.term.kill(PS_TID, **kw)))

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*54}")
    print(f"live admission acceptance: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
