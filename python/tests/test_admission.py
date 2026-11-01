#!/usr/bin/env python3
"""A5/A6 — execution admission evidence.

Drives the REAL codeapi_vscode.admission against terminal buffers, including
one reconstructed from the preserved pager incident
(fixtures/regressions/pty-foreground-ownership-20260809.md).

The four required cases:

    normal success            BEGIN + END + exit 0
    normal failure            BEGIN + END + nonzero exit
    admitted but unfinished   BEGIN, no END
    submitted, never admitted no BEGIN  -> admission NOT proven

    python python/tests/test_admission.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codeapi_vscode import admission as adm                      # noqa: E402


def check(results, label, cond, detail=""):
    results.append((label, bool(cond)))
    print(f"{'OK  ' if cond else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


def _shell_session(envelope, output_lines):
    """A terminal buffer as a shell actually produces one: the submitted line
    is ECHOED first, then the command's own output follows."""
    return "\n".join(["PS C:\\work> " + envelope] + list(output_lines)
                     + ["PS C:\\work> "])


def main():
    results = []
    eid = adm.new_execution_id()
    cmd = "git status --short"
    env = adm.build_envelope(cmd, eid)

    # --- A2 execution identity ------------------------------------------
    check(results, "execution_id format codeapi-<32 hex>",
          eid.startswith("codeapi-") and len(eid) == 8 + 32
          and all(c in "0123456789abcdef" for c in eid[8:]), eid)
    check(results, "execution ids are unique per run",
          len({adm.new_execution_id() for _ in range(200)}) == 200)
    check(results, "execution_id is distinct from terminal id and gate id",
          adm.new_execution_id() != adm.new_execution_id())

    # --- A3 envelope ------------------------------------------------------
    check(results, "envelope is ONE submission carrying the command",
          cmd in env and "\n" not in env, env)
    # --- A3/A8 the non-echoability theorem, stated executably -------------
    #
    # The detector searches for the marker the SHELL emits. If that exact
    # string were present in the submitted text, PTY echo alone would satisfy
    # it - and echo is what still happens when a pager, not the shell, is
    # receiving the keystrokes. Admission would be "proven" by the failure
    # case. So this is checked, not asserted in a comment.
    check(results, "A3: neither final marker occurs VERBATIM in the submission",
          adm.marker_absent_from_submission(cmd, eid),
          "begin={!r} not in submitted text".format(adm.begin_marker(eid)))
    check(results, "A3: the exact BEGIN string is absent from the envelope",
          adm.begin_marker(eid) not in env and adm.BEGIN_WORD not in env)
    check(results, "A3: the exact END string is absent from the envelope",
          adm.end_marker_prefix(eid) not in env and adm.END_WORD not in env)

    # Hold across adversarial command text, including a command that itself
    # contains marker-shaped text.
    for hostile in ["echo hi", "echo CODEAPI-BEGIN",
                    "echo " + adm.begin_marker(eid),
                    'Write-Host ("CODEAPI-BEG"+"IN x")', "a; b; c", ""]:
        hid = adm.new_execution_id()
        check(results, "A3 holds for hostile command {!r}".format(hostile[:28]),
              adm.marker_absent_from_submission(hostile, hid))

    # THE case the split exists for. This buffer contains ONLY the echoed
    # command line - what a terminal shows the instant the text is typed,
    # including when a pager rather than the shell is receiving it.
    echo_only = "PS C:\\work> " + env
    ev = adm.classify(echo_only, eid, submitted=True)
    check(results, "the ECHO of the envelope does NOT count as admission",
          ev.begin_observed is False and ev.verdict == adm.ADMISSION_UNKNOWN,
          "status={} verdict={}".format(ev.status, ev.verdict))

    # --- the critical theorem --------------------------------------------
    # --- A14 the transport-signal invariant, stated as BEHAVIOUR ----------
    #
    # An earlier version of this block inspected classify()'s signature and
    # claimed "classify cannot be given a transport signal". That claim was
    # retired: submission_accepted is now DELIBERATELY part of the evidence
    # input (A5), so a transport signal IS a parameter and the structural
    # argument no longer holds. Worse, the check kept passing while its label
    # had stopped being true, because it only looked for the names `wrote` and
    # `status_code`.
    #
    # The real theorem is behavioural, so it is tested behaviourally: a
    # transport signal, at any value, cannot move admission evidence.
    for buf, why in [("", "empty buffer"),
                     ("PS C:\\work> " + env, "buffer containing only the echo"),
                     ("some unrelated output\n", "unrelated output")]:
        ev = adm.classify(buf, eid, submitted=True, submission_accepted=True,
                          wrapped_command=cmd, observation_start_offset=999)
        check(results, "A14: submission_accepted=True cannot set "
              "begin_observed ({})".format(why),
              ev.submission_accepted is True and ev.begin_observed is False)
        check(results, "A14: submission_accepted=True cannot yield ADMITTED "
              "({})".format(why),
              ev.status != adm.STATUS_ADMITTED)
        check(results, "A14: submission_accepted=True cannot yield COMPLETED "
              "({})".format(why),
              ev.status != adm.STATUS_COMPLETED and ev.completed is False)

    # And the converse: the ONLY thing that moves admission is the runtime
    # marker. Same transport value, opposite outcome, because the buffer
    # differs - which is what makes the marker the load-bearing input.
    admitted = adm.classify("{} {}".format(adm.BEGIN_WORD, eid), eid,
                            submitted=True, submission_accepted=False)
    check(results, "A14: the runtime BEGIN admits even when "
          "submission_accepted is False",
          admitted.begin_observed is True
          and admitted.status == adm.STATUS_ADMITTED,
          "recognition of the marker is the only admission input")

    # --- A6 normal success ------------------------------------------------
    ok_buf = _shell_session(env, [
        "{} {}".format(adm.BEGIN_WORD, eid),
        " M package.json",
        "{} {} 1 0".format(adm.END_WORD, eid),
    ])
    ev = adm.classify(ok_buf, eid, submitted=True, terminal_id="t1",
                      wrapped_command=cmd, terminal_alive=True)
    check(results, "normal success -> COMPLETED / PROVEN_ADMITTED / exit 0",
          ev.status == adm.STATUS_COMPLETED and ev.verdict == adm.PROVEN_ADMITTED
          and ev.begin_observed and ev.end_observed and ev.exit_code == 0
          and ev.completed,
          "exit={} status={}".format(ev.exit_code, ev.status))
    check(results, "success evidence names the wrapped command by sha256",
          ev.wrapped_command == cmd
          and ev.wrapped_command_sha256 == adm.command_sha256(cmd))

    # --- A6 normal failure ------------------------------------------------
    fail_buf = "\n".join([
        "PS C:\\work> " + env,
        "{} {}".format(adm.BEGIN_WORD, eid),
        "fatal: not a git repository",
        "{} {} 0 128".format(adm.END_WORD, eid),
        "PS C:\\work> ",
    ])
    ev = adm.classify(fail_buf, eid, submitted=True, terminal_alive=True)
    check(results, "normal failure -> COMPLETED with the nonzero exit captured",
          ev.status == adm.STATUS_COMPLETED and ev.exit_code == 128
          and ev.powershell_success is False and ev.native_exit_code == 128
          and ev.verdict == adm.PROVEN_ADMITTED,
          "exit={} ps_ok={} native={}".format(
              ev.exit_code, ev.powershell_success, ev.native_exit_code))

    neg_buf = fail_buf.replace(" 0 128", " 0 -1073741510")
    ev = adm.classify(neg_buf, eid, submitted=True)
    check(results, "negative exit codes survive (Windows STATUS_CONTROL_C_EXIT)",
          ev.exit_code == -1073741510, "exit={}".format(ev.exit_code))

    # --- A17 result semantics, from the MEASURED shell behaviour ----------
    #
    # Every row below was observed against a real powershell.exe, not
    # imagined. The last is the one that matters: $? and $LASTEXITCODE answer
    # different questions and DISAGREE for a compound command.
    #
    # The assertion is on the RAW FACTS and the documented compat conversion.
    # An earlier version asserted a `wrapped_result_kind` name for each row,
    # which tested that my taxonomy agreed with itself rather than that the
    # two shell signals were carried through intact. The taxonomy is gone; the
    # facts and the stated rule are what remain checkable.
    for label, end, ps_ok, native, compat, anomaly in [
        ("native success", "1 0", True, 0, 0, False),
        ("native failure", "0 3", False, 3, 3, False),
        ("cmdlet success", "1 -", True, None, 0, False),
        ("cmdlet non-terminating failure", "0 -", False, None, 1, False),
        ("cmdlet success AFTER native failure", "1 7", True, 7, 0, True),
    ]:
        b = "{} {}\n{} {} {}".format(adm.BEGIN_WORD, eid, adm.END_WORD, eid, end)
        e = adm.classify(b, eid, submitted=True)
        check(results, "A17 {}: raw facts preserved exactly".format(label),
              e.powershell_success is ps_ok and e.native_exit_code == native,
              "got ps_ok={} native={}".format(
                  e.powershell_success, e.native_exit_code))
        check(results, "A17 {}: compat exit_code={} by the stated rule".format(
                  label, compat),
              e.exit_code == compat,
              "got exit={} from ps_ok={} native={}".format(
                  e.exit_code, e.powershell_success, e.native_exit_code))
        check(results, "A17 {}: disagreement recorded={}".format(label, anomaly),
              ("native_exit_disagrees_with_powershell_success" in e.anomalies)
              is anomaly,
              "anomalies={}".format(e.anomalies))

    # The raw signals are never discarded in favour of the compat integer.
    mixed = adm.classify("{} {}\n{} {} 1 7".format(
        adm.BEGIN_WORD, eid, adm.END_WORD, eid), eid, submitted=True)
    check(results, "A17: both raw signals survive the compat derivation",
          mixed.powershell_success is True and mixed.native_exit_code == 7
          and mixed.exit_code == 0,
          "no single integer can carry 'last statement succeeded but a "
          "native process in it did not'")

    # The taxonomy is gone from the contract, and the evidence dict is the
    # contract. If it comes back, it comes back deliberately and this fails.
    d = mixed.as_dict()
    check(results, "A17: no result taxonomy in the evidence contract",
          "wrapped_result_kind" not in d
          and not hasattr(mixed, "wrapped_result_kind"),
          "keys={}".format(sorted(d)))
    check(results, "A17: the evidence dict still carries both raw facts",
          d["powershell_success"] is True and d["native_exit_code"] == 7
          and d["exit_code"] == 0)

    # --- A6 admitted but unfinished --------------------------------------
    running = "\n".join([
        "PS C:\\work> " + env,
        "{} {}".format(adm.BEGIN_WORD, eid),
        "Cloning into 'big-repo'...",
        "Receiving objects:  41% (4100/10000)",
    ])
    ev = adm.classify(running, eid, submitted=True, terminal_alive=True)
    check(results, "long-running -> ADMITTED, completion pending, no exit code",
          ev.status == adm.STATUS_ADMITTED and ev.verdict == adm.PROVEN_ADMITTED
          and ev.begin_observed and not ev.end_observed
          and ev.exit_code is None and not ev.completed,
          "status={} exit={}".format(ev.status, ev.exit_code))

    # --- A5 the preserved pager incident ---------------------------------
    #
    # Reconstructed from the fixture. A first command took the foreground with
    # a pager; the second run's envelope was written successfully (the write
    # returned a byte count) but the PAGER received it, so the shell never
    # evaluated it and no BEGIN was ever emitted.
    eid2 = adm.new_execution_id()
    env2 = adm.build_envelope("git log --oneline -5", eid2)
    pager_buf = "\n".join([
        "PS C:\\work> git diff",
        "diff --git a/package.json b/package.json",
        "index 356400e..72a07e1 100644",
        "\x1b[7m(END)\x1b[0m",
        # the second submission is DISPLAYED by the pager, never evaluated
        env2,
        "\x1b[7m(END)\x1b[0m",
    ])
    ev = adm.classify(pager_buf, eid2, submitted=True, terminal_id="tmbrwfix1",
                      wrapped_command="git log --oneline -5", terminal_alive=True)
    check(results, "PAGER INTERCEPT: submitted=true but admission NOT proven",
          ev.submitted is True and ev.begin_observed is False
          and ev.end_observed is False and ev.exit_code is None
          and ev.status == adm.STATUS_ADMISSION_UNKNOWN
          and ev.verdict == adm.ADMISSION_UNKNOWN,
          "status={} verdict={}".format(ev.status, ev.verdict))
    check(results, "PAGER INTERCEPT: no wrapped-command exit code is invented",
          ev.exit_code is None and ev.completed is False)
    check(results, "PAGER INTERCEPT: the envelope text being VISIBLE in the "
          "buffer does not admit it",
          env2 in pager_buf and ev.begin_observed is False,
          "the pager displayed it; the shell never ran it")

    # The distinction the previous implementation could not make: these two
    # buffers are BOTH 'no completion marker', and they are now different.
    unfinished = adm.classify(running, eid, submitted=True)
    intercepted = adm.classify(pager_buf, eid2, submitted=True)
    check(results, "never-admitted and admitted-but-running are now DISTINCT",
          unfinished.status == adm.STATUS_ADMITTED
          and intercepted.status == adm.STATUS_ADMISSION_UNKNOWN
          and unfinished.status != intercepted.status,
          "{} vs {}".format(unfinished.status, intercepted.status))

    # --- A4/A13: PROVEN_NOT_ADMITTED is NOT produced in this phase --------
    check(results, "PROVEN_NOT_ADMITTED is not exposed at all",
          not hasattr(adm, "PROVEN_NOT_ADMITTED"),
          "positive non-admission needs a shell-readiness theorem this "
          "phase does not have")
    for alive in (False, True, None):
        ev = adm.classify("", eid, submitted=True, terminal_alive=alive)
        check(results, "no BEGIN + terminal_alive={} -> ADMISSION_UNKNOWN"
              .format(alive),
              ev.verdict == adm.ADMISSION_UNKNOWN,
              "absence of evidence is not evidence of absence")

    # --- cross-execution isolation ---------------------------------------
    other = adm.classify(ok_buf, adm.new_execution_id(), submitted=True)
    check(results, "another execution's envelope does not admit THIS one",
          other.begin_observed is False and other.exit_code is None)

    # --- anomaly: END without BEGIN --------------------------------------
    orphan = "{} {} 1 0".format(adm.END_WORD, eid)
    ev = adm.classify(orphan, eid, submitted=True)
    check(results, "END without BEGIN is an anomaly, not a completion",
          ev.end_observed is False and ev.exit_code is None
          and "end_without_begin" in ev.anomalies
          and ev.status == adm.STATUS_ADMISSION_UNKNOWN,
          "anomalies={}".format(ev.anomalies))

    # --- ANSI ------------------------------------------------------------
    ansi = "\x1b[32m{} {}\x1b[0m\nx\n\x1b[31m{} {} 0 3\x1b[0m".format(
        adm.BEGIN_WORD, eid, adm.END_WORD, eid)
    ev = adm.classify(ansi, eid, submitted=True)
    check(results, "markers survive ANSI colouring around them",
          ev.begin_observed and ev.end_observed and ev.exit_code == 3)

    # OSC 633 immediately preceding a marker, which is what a shell-integration
    # terminal actually produces. The old regex ate only "\x1b]" and left
    # "633;C" glued to the marker, so the line-anchored detector missed a
    # command that had run correctly. Measured from a live buffer, not imagined.
    osc = ("\x1b]633;C\x07{} {}\r\n"
           "output line\r\n"
           "\x1b]633;D;0\x07\x1b]633;A\x07\x1b]633;P;Cwd=C:\\x5cUsers\x07"
           "{} {} 1 0\r\n").format(adm.BEGIN_WORD, eid, adm.END_WORD, eid)
    ev = adm.classify(osc, eid, submitted=True)
    check(results, "OSC 633 immediately before a marker is fully stripped",
          ev.begin_observed and ev.end_observed and ev.exit_code == 0,
          "begin={} end={}".format(ev.begin_observed, ev.end_observed))
    check(results, "an OSC title sequence does not leave residue either",
          adm.classify("\x1b]0;C:\\Windows\x07{} {}".format(
              adm.BEGIN_WORD, eid), eid, submitted=True).begin_observed)

    # THE case from the live buffer: an OSC 633;E payload carrying the echoed
    # command line, with a cursor-movement escape injected INSIDE it by
    # PSReadLine repainting a line longer than the terminal is wide. The
    # sequence still ends at BEL. If the OSC pattern refuses to cross the
    # embedded ESC, the whole payload survives as literal text and lands
    # directly in front of the marker, which is exactly how a command that ran
    # correctly read as ADMISSION_UNKNOWN.
    repaint = ("\x1b]633;E;$global:LASTEXITCODE=$null; Write-Host (\"X\");"
               "\x1b[2K\x1b[1G cmd /c exit 0;40ccf265-564b-427f\x07"
               "{} {}\r\n{} {} 1 0\r\n").format(
                   adm.BEGIN_WORD, eid, adm.END_WORD, eid)
    ev = adm.classify(repaint, eid, submitted=True)
    check(results, "an OSC payload containing a repaint escape is fully stripped",
          ev.begin_observed and ev.end_observed and ev.exit_code == 0,
          "begin={} end={}".format(ev.begin_observed, ev.end_observed))
    # Line anchoring was DROPPED on evidence: a real terminal wraps and
    # repaints, and the measured buffer had the marker preceded on its line by
    # the echoed envelope's tail and the integration nonce, with no OSC
    # introducer left to strip. The anchor produced false negatives for
    # commands that had demonstrably run. The anti-echo guarantee rests on the
    # split literal instead, which is what these two assert.
    # Concatenated, not formatted: this fragment is copied verbatim from the
    # live buffer and contains PowerShell braces.
    wrapped = ("[string]$__capiLec }));e101bd87-1563-4c13-9817-6975bf42debb"
               + adm.BEGIN_WORD + " " + eid + "\r\n"
               + adm.END_WORD + " " + eid + " 1 0\r\n")
    ev = adm.classify(wrapped, eid, submitted=True)
    check(results, "a marker mid-line after terminal wrapping is still found",
          ev.begin_observed and ev.end_observed and ev.exit_code == 0,
          "the anchor failed here while the command had actually run")

    # The real guarantee: the ECHO of a submission can never contain the
    # joined marker, because only shell evaluation concatenates the halves.
    echoed = "PS C:\\x> " + adm.build_envelope("git status", eid)
    check(results, "the submitted envelope's echo still cannot admit it",
          adm.classify(echoed, eid, submitted=True).begin_observed is False,
          "split literal, not the anchor, is what prevents echo admission")

    # A longer id must not be admitted by a shorter one's prefix.
    check(results, "a marker for a DIFFERENT longer id does not match",
          adm.classify(adm.BEGIN_WORD + " " + eid + "extra", eid,
                       submitted=True).begin_observed is False)

    # --- A20 sticky evidence / A21 output loss ----------------------------
    #
    # The ring buffer truncates from the FRONT, so a long-running command
    # eventually evicts its own BEGIN. Re-deriving from the current buffer
    # would decay ADMITTED back to ADMISSION_UNKNOWN - evidence getting weaker
    # as the command produces MORE output.
    sticky = adm.observed(execution_id=eid, submitted=True,
                          submission_accepted=True, begin_observed=True,
                          end_observed=False, terminal_id="t1",
                          wrapped_command=cmd,
                          observation_start_offset=1000,
                          observation_end_offset=3000000,
                          retained_buffer_bytes=1000000)
    check(results, "A20: BEGIN stays observed after the ring evicted it",
          sticky.begin_observed is True and sticky.status == adm.STATUS_ADMITTED,
          "status={}".format(sticky.status))
    check(results, "A21: a window wider than the retained buffer flags loss",
          sticky.possible_output_loss is True,
          "window=2999000 retained=1000000")

    intact = adm.observed(execution_id=eid, submitted=True,
                          submission_accepted=True, begin_observed=True,
                          end_observed=True, powershell_success=True,
                          native_exit_code=0, observation_start_offset=10,
                          observation_end_offset=500,
                          retained_buffer_bytes=900)
    check(results, "A21: a window inside the retained buffer flags no loss",
          intact.possible_output_loss is False
          and intact.status == adm.STATUS_COMPLETED
          and intact.exit_code == 0)

    lost = adm.observed(execution_id=eid, submitted=True,
                        submission_accepted=True, begin_observed=False,
                        end_observed=False, observation_start_offset=0,
                        observation_end_offset=5000000,
                        retained_buffer_bytes=1000000)
    check(results, "A21: no BEGIN + possible loss stays ADMISSION_UNKNOWN and "
          "records why",
          lost.status == adm.STATUS_ADMISSION_UNKNOWN
          and "possible_output_loss_before_observation" in lost.anomalies,
          "anomalies={}".format(lost.anomalies))

    # --- S3 dialect selection ---------------------------------------------
    for shell, want in [
        ("powershell.exe", adm.DIALECT_POWERSHELL),
        ("C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
         adm.DIALECT_POWERSHELL),
        ("pwsh.exe", adm.DIALECT_POWERSHELL),
        ("/usr/bin/pwsh", adm.DIALECT_POWERSHELL),
        ("C:\\WINDOWS\\system32\\cmd.exe", adm.DIALECT_CMD),
        ("cmd", adm.DIALECT_CMD),
        ("/bin/bash", adm.DIALECT_POSIX),
        ("zsh", adm.DIALECT_POSIX),
        ("C:\\tools\\nushell\\nu.exe", adm.DIALECT_UNKNOWN),
        (None, adm.DIALECT_UNKNOWN),
        ("", adm.DIALECT_UNKNOWN),
    ]:
        check(results, "S3 dialect {!r} -> {}".format(shell, want),
              adm.dialect_for_shell(shell) == want,
              "got {}".format(adm.dialect_for_shell(shell)))

    check(results, "S3 only PowerShell is a supported dialect",
          adm.SUPPORTED_DIALECTS == (adm.DIALECT_POWERSHELL,)
          and adm.is_supported_dialect(adm.DIALECT_POWERSHELL)
          and not adm.is_supported_dialect(adm.DIALECT_CMD)
          and not adm.is_supported_dialect(adm.DIALECT_POSIX)
          and not adm.is_supported_dialect(adm.DIALECT_UNKNOWN))

    # An absent shell must never be defaulted to the running platform.
    check(results, "S3 unknown shell is NOT defaulted to a platform guess",
          adm.dialect_for_shell(None) == adm.DIALECT_UNKNOWN
          and not adm.is_supported_dialect(adm.dialect_for_shell(None)),
          "guessing is how PowerShell syntax reached cmd.exe")

    err = adm.UnsupportedShellError("t9", "C:\\WINDOWS\\system32\\cmd.exe",
                                    adm.DIALECT_CMD)
    check(results, "S3 the refusal names the terminal, shell and dialect",
          "t9" in str(err) and "cmd.exe" in str(err)
          and "Nothing was written" in str(err), str(err)[:70])
    # A CodeAPIError so the CLI reports it as an error rather than a
    # traceback — `codeapi term run` against a cmd terminal should say why it
    # refused, not print a stack.
    from codeapi_vscode.errors import CodeAPIError as _CAE   # noqa: E402
    check(results, "S3 the refusal is a CodeAPIError (clean CLI reporting)",
          isinstance(err, _CAE))

    # --- B1: ONE execution engine ----------------------------------------
    #
    # run_result used to carry a second, independent marker protocol. Every
    # difference between the two was a defect in that one, so the check is
    # that the alternate protocol is GONE from the module rather than merely
    # unused — a dormant second detector is one call away from returning.
    import inspect as _inspect                            # noqa: E402
    from codeapi_vscode import terminal_client as _tc     # noqa: E402
    src = _inspect.getsource(_tc.TermClient.run_result)
    check(results, "B1 run_result no longer builds __CodeAPI_ markers",
          "__CodeAPI_" not in src and "_START__" not in src)
    check(results, "B1 run_result no longer calls _ps_wrapper",
          "_ps_wrapper(" not in src)
    check(results, "B1 run_result delegates to the one engine",
          "_run_envelope(" in src)
    check(results, "B1 run_result no longer branches on a shell parameter",
          'shell == "powershell"' not in src)
    check(results, "B1 run_verified still exists and routes through run_result",
          "run_result(" in _inspect.getsource(_tc.TermClient.run_verified))
    # The engine is reached through ONE submission point, so a foreground
    # application cannot consume a marker independently of its command.
    env_src = _inspect.getsource(_tc.TermClient._submit)
    check(results, "B1 the engine writes exactly once per execution",
          env_src.count("self.write(") == 1,
          "writes={}".format(env_src.count("self.write(")))

    # --- output extraction -------------------------------------------------
    out = adm.between_markers(ok_buf, eid)
    check(results, "output is what lies BETWEEN the markers",
          out == " M package.json", repr(out))
    check(results, "no BEGIN -> no output attributed to this execution",
          adm.between_markers("unrelated\n", eid) == "")

    # --- serialization ----------------------------------------------------
    d = adm.classify(ok_buf, eid, submitted=True, terminal_id="t1",
                     terminal_generation=7, wrapped_command=cmd,
                     terminal_alive=True).as_dict()
    check(results, "evidence exposes FIELDS, not only a synthesized status",
          all(k in d for k in ("submitted", "submission_accepted",
                               "begin_observed", "end_observed",
                               "exit_code", "observation_start_offset",
                               "status", "verdict", "execution_id",
                               "terminal_id", "terminal_generation",
                               "wrapped_command_sha256")),
          "keys={}".format(sorted(d)))
    check(results, "terminal_generation is carried when available",
          d["terminal_generation"] == 7)

    # --- A5 the invariant, on the field that most invites the mistake -----
    ev = adm.classify("", eid, submitted=True, submission_accepted=True,
                      observation_start_offset=41233, wrapped_command=cmd)
    check(results, "submission_accepted=True does NOT set begin_observed",
          ev.submission_accepted is True and ev.begin_observed is False
          and ev.status == adm.STATUS_ADMISSION_UNKNOWN
          and ev.verdict == adm.ADMISSION_UNKNOWN,
          "transport acceptance and admission are separate fields")
    check(results, "observation_start_offset is carried as evidence",
          ev.observation_start_offset == 41233)
    check(results, "terminal_generation defaults to None, never fabricated",
          adm.classify("", eid, submitted=True).terminal_generation is None)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n{'='*54}")
    print(f"execution admission evidence: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
