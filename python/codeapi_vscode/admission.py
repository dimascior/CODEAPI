"""Execution admission evidence (A2-A4).

The distinction this module exists for:

    wrapper execution  !=  wrapped-command admission

A successful `term run` proves the wrapper delivered bytes to a PTY. It does
not prove the target shell accepted the intended command. The pre-existing
completion detection could not tell the difference, because it had only an
END marker: a marker that never arrived meant "never admitted", "admitted and
still running", or "completed but the marker was lost", with nothing in the
evidence to separate them.

An envelope with a BEGIN emitted BY THE SHELL separates them:

    no BEGIN                admission NOT PROVEN
    BEGIN, no END           admitted, completion unknown
    BEGIN and END           completed, exit code captured

This module is PURE: it builds envelope text and classifies a buffer. It
performs no I/O, opens no terminal and knows nothing about transport, which
is what makes the critical theorem structural rather than aspirational -
`classify()` does not accept `wrote`, a status code, or any transport signal,
so no amount of transport success can set begin_observed.

Deliberately NOT in scope (A7): identifying which program owns the terminal
foreground, shell readiness, or recovery. Admission evidence comes first; the
system has to be able to distinguish "never admitted" from "admitted, still
running" before "who is holding the terminal" is a meaningful question.
"""

import hashlib
import re
import uuid

from .errors import CodeAPIError

# --- execution identity (A2) ---------------------------------------------
#
# Distinct from terminal_id (which terminal), from the Helios gate
# correlation_id (which authorization), and from the wrapper process identity
# (which CodeAPI invocation). One `term run` is one execution_id. A retry is a
# NEW execution_id, because the question "was THIS submission admitted" cannot
# be answered by evidence belonging to a previous one.
EXECUTION_ID_PREFIX = "codeapi"


def new_execution_id():
    """A fresh execution identity: codeapi-<32 hex>."""
    return "{}-{}".format(EXECUTION_ID_PREFIX, uuid.uuid4().hex)


def command_sha256(command):
    """Identity of the WRAPPED command text, so evidence names what was meant
    to run rather than only that something ran."""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


# --- envelope text (A3) ---------------------------------------------------
#
# THE SPLIT-LITERAL IS LOad-BEARING, not decoration.
#
# A shell ECHOES the line it was given before running it, so the submitted
# text itself lands in the read buffer. If the envelope contained the literal
# string "CODEAPI-BEGIN <id>", a buffer search would match that ECHO - and the
# echo is produced by the terminal displaying input, which is exactly the
# thing that still happens when a pager, not the shell, is receiving the
# keystrokes. Admission would then be "proven" by the failure case.
#
# Splitting the literal means the text that appears in the echo
# ("CODEAPI-BEG"+"IN x") and the text that appears in the OUTPUT
# (CODEAPI-BEGIN x) are different strings. Only the shell evaluating the
# expression can produce the joined form.
BEGIN_WORD = "CODEAPI-BEGIN"
END_WORD = "CODEAPI-END"

# PowerShell result semantics (A17).
#
# $LASTEXITCODE alone is NOT the result. It is set only by native executables
# and PERSISTS, so a cmdlet that fails leaves the exit code of whatever native
# process ran before it - and reporting that as the cmdlet's result would be a
# stale value presented as evidence. Two signals are therefore captured:
#
#     $?             did the LAST statement succeed (cmdlets included)
#     $LASTEXITCODE  the last NATIVE process exit code, or nothing
#
# $LASTEXITCODE is cleared to $null BEFORE the wrapped command, so a "-" in
# the END line means "no native process ran in this execution" rather than
# "an earlier one left this behind". Both are captured in that order and
# IMMEDIATELY: assignment itself sets $?, so $? must be read first or it
# reports the success of the capture rather than of the command.
_PS_TEMPLATE = (
    '$global:LASTEXITCODE=$null; Write-Host ("{b0}"+"{b1} {eid}"); '
    '{command}; '
    '$__capiOk=$?; $__capiLec=$LASTEXITCODE; '
    'Write-Host ("{e0}"+"{e1} {eid} " + [int][bool]$__capiOk + " " '
    '+ $(if ($null -eq $__capiLec) {{ "-" }} else {{ [string]$__capiLec }}))'
)
# POSIX is a PROTOTYPE and is not exercised by any test (A25). It is not
# claimed as supported.
_POSIX_TEMPLATE = (
    "printf '%s\\n' \"{b0}\"\"{b1} {eid}\"; {command}; "
    "__capi_rc=$?; printf '%s\\n' \"{e0}\"\"{e1} {eid} $__capi_rc $__capi_rc\""
)

SHELL_POWERSHELL = "powershell"
SHELL_POSIX = "posix"

# --- execution dialects (S3) ----------------------------------------------
#
# The dialect is chosen from the terminal's RECORDED spawn-time shell, never
# from COMSPEC, the platform, the current environment or the terminal's name.
# Those describe the environment, not the process actually running, and a
# terminal created earlier under different conditions would be misread.
DIALECT_POWERSHELL = "powershell"
DIALECT_CMD = "cmd"
DIALECT_POSIX = "posix"
DIALECT_UNKNOWN = "unknown"

# Only PowerShell has a tested envelope AND a result contract.
#
# cmd.exe is measured, not assumed. A non-echoable marker IS achievable there:
#
#     for %i in (IN) do @echo CODEAPI-BEG%i <id>  ->  CODEAPI-BEGIN <id>
#
# assembles the marker from a loop variable at execution time, so the joined
# form never appears in the submitted text. END joins the same way.
#
# What was NOT achieved: the TESTED candidates did not recover a reliable
# dynamic ERRORLEVEL. Each %%ERRORLEVEL%% form resolved to the literal %0% in
# an interactive session, whether deferred through a `call` re-parse or a
# for-token. That is a statement about the candidates tried, not a proof that
# cmd can never report a result - other mechanisms (delayed expansion via
# `cmd /v:on`, a temp file, a wrapper process) were not evaluated here.
#
# A dialect with BEGIN and END but no result would report COMPLETED while
# unable to say what happened, which is a worse failure than refusing: it
# would look like evidence. So for THIS boundary cmd is explicitly
# UNSUPPORTED and run() refuses before writing any bytes, rather than sending
# PowerShell syntax into it. Implementing a cmd dialect remains open.
SUPPORTED_DIALECTS = (DIALECT_POWERSHELL,)

_POWERSHELL_BASENAMES = frozenset(
    ("powershell.exe", "powershell", "pwsh.exe", "pwsh"))
_CMD_BASENAMES = frozenset(("cmd.exe", "cmd"))
_POSIX_BASENAMES = frozenset(
    ("bash", "sh", "zsh", "dash", "ash", "bash.exe", "sh.exe", "zsh.exe"))


def dialect_for_shell(shell):
    """The execution dialect implied by a terminal's recorded shell.

    An absent shell is UNKNOWN, never defaulted to a platform guess: guessing
    is exactly how PowerShell syntax reached cmd.exe.
    """
    if not shell:
        return DIALECT_UNKNOWN
    base = str(shell).replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if base in _POWERSHELL_BASENAMES:
        return DIALECT_POWERSHELL
    if base in _CMD_BASENAMES:
        return DIALECT_CMD
    if base in _POSIX_BASENAMES:
        return DIALECT_POSIX
    return DIALECT_UNKNOWN


def is_supported_dialect(dialect):
    return dialect in SUPPORTED_DIALECTS


class UnsupportedShellError(CodeAPIError):
    """A terminal whose shell has no tested execution dialect.

    Raised BEFORE any PTY write. Refusing loudly is the point: the previous
    behaviour submitted PowerShell syntax to whatever was there and reported
    ADMISSION_UNKNOWN, which is indistinguishable from a foreground
    interception and therefore not an acceptable answer for a KNOWN
    unsupported dialect.
    """

    def __init__(self, terminal_id, shell, dialect):
        self.terminal_id = terminal_id
        self.shell = shell
        self.dialect = dialect
        super().__init__(
            "terminal {!r} runs {!r} (dialect {!r}), which has no tested "
            "execution envelope; supported: {}. Nothing was written to the "
            "terminal. Create the terminal with an explicit "
            "shell='powershell.exe'.".format(
                terminal_id, shell, dialect, ", ".join(SUPPORTED_DIALECTS)))


def _split(word):
    """Split a marker word so the echoed form never equals the emitted form."""
    cut = len(word) - 2
    return word[:cut], word[cut:]


def build_envelope(command, execution_id, shell="powershell"):
    """The exact text submitted to the terminal for one execution.

    ONE submission, not two. The previous implementation wrote the command and
    then wrote `echo <marker>` as a separate line, so a foreground application
    that had captured the terminal received the marker as its own input. A
    single line means the marker cannot be admitted independently of the
    command it is supposed to be evidence for.
    """
    b0, b1 = _split(BEGIN_WORD)
    e0, e1 = _split(END_WORD)
    template = _PS_TEMPLATE if shell == "powershell" else _POSIX_TEMPLATE
    return template.format(b0=b0, b1=b1, e0=e0, e1=e1,
                           eid=execution_id, command=command)


def _begin_re(execution_id):
    """The runtime BEGIN, matched ANYWHERE in the stream.

    Line anchoring was defence-in-depth and had to be dropped, on evidence.
    A real terminal wraps and repaints a line far wider than its width, and
    the measured buffer showed the marker preceded on its line by the tail of
    the echoed envelope and the integration nonce - ordinary word characters,
    with the OSC introducer no longer present anywhere for a stripper to
    remove. Under those conditions the anchor produced FALSE NEGATIVES: a
    command that had demonstrably run, with both markers in the buffer, read
    as ADMISSION_UNKNOWN. An evidence rule that fails when the terminal is
    merely narrow is not a safety property.

    The anti-echo guarantee never rested on the anchor. It rests on the SPLIT
    LITERAL: the submitted text carries ("CODEAPI-BEG"+"IN <id>") and only
    shell evaluation produces the joined form, so the echo cannot contain what
    this searches for. That property is asserted directly, including against
    adversarial commands.

    What is genuinely given up: a command that deliberately PRINTS the joined
    marker together with this execution's id would now admit. That requires
    knowing an id minted for this run, and anything running in the shell could
    forge admission regardless - as recorded, this is evidence about a
    cooperating shell, not a security control. A trailing boundary is kept so
    a longer id cannot match a shorter one's prefix.
    """
    return re.compile(re.escape(BEGIN_WORD) + r"[^\S\n]+"
                      + re.escape(execution_id) + r"(?![0-9A-Za-z-])")


def _end_re(execution_id):
    """END carries TWO results: PowerShell success (0/1) and the native exit
    code ('-' when no native process ran in this execution)."""
    return re.compile(re.escape(END_WORD) + r"[^\S\n]+"
                      + re.escape(execution_id)
                      + r"[^\S\n]+([01])[^\S\n]+(-|-?\d+)(?![0-9])")


# THE RESULT FACTS ARE TWO, AND ONLY TWO.
#
#   powershell_success   $?             did the LAST STATEMENT succeed
#   native_exit_code     $LASTEXITCODE  what did the last NATIVE PROCESS return
#                                       (None when no native process ran)
#
# They answer different questions and can disagree. Measured, not imagined:
# `cmd /c exit 7; Get-Item C:\Windows` reports $?=1 with $LASTEXITCODE=7 — the
# last statement SUCCEEDED while a native process earlier in the same wrapped
# command failed.
#
# An earlier version published a `wrapped_result_kind` naming five categories
# over these two fields. It has been removed: it was a second vocabulary
# describing facts the caller already had, and a consumer reading the kind
# instead of the fields was reading my interpretation rather than the shell's
# report. The one place the distinction actually needs a name is the
# disagreement itself, which is what this predicate is for.
def _native_exit_disagrees(powershell_success, native_exit_code):
    """True for the one case a single integer genuinely cannot express: the
    last statement succeeded while a native process in the same command did
    not. Recorded as an anomaly so it is visible rather than flattened."""
    return bool(powershell_success) and native_exit_code not in (None, 0)


def _compat_exit(powershell_success, native_exit_code):
    """The single integer, derived DELIBERATELY — an INTERPRETATION, not a fact.

    The rule, stated in full so it is not re-derived from the code:

        powershell_success is None            -> None      (no result observed)
        native_exit_code is None              -> 0 / 1     from $?
        native_exit_code == 0                 -> 0 / 1     from $?
        native nonzero and $? false           -> the native code
        native nonzero and $? true            -> 0, and an anomaly is recorded

    The last line is the whole difficulty. $? wins because it describes the
    statement that actually ended the command, and the native code is not
    discarded — it stays in native_exit_code. A caller that needs the real
    answer reads both raw fields; no single integer can carry "the last
    statement succeeded but a native process inside it did not".
    """
    if powershell_success is None:
        return None
    if native_exit_code is not None and not (powershell_success
                                             and native_exit_code != 0):
        return native_exit_code
    return 0 if powershell_success else 1


# --- state model (A4) ------------------------------------------------------

STATUS_SUBMITTED = "SUBMITTED"
# The command CODEAPI deliberately never wrote. Strictly stronger evidence
# than ADMISSION_UNKNOWN: nothing entered the PTY, so there is no possibility
# that the shell saw it. ADMISSION_UNKNOWN remains reserved for an envelope
# that WAS written and whose BEGIN was not observed — the two must never be
# collapsed, because one is a refusal and the other is an unknown.
STATUS_PROVEN_NOT_SUBMITTED = "PROVEN_NOT_SUBMITTED"
STATUS_ADMITTED = "ADMITTED"
STATUS_COMPLETED = "COMPLETED"
STATUS_ADMISSION_UNKNOWN = "ADMISSION_UNKNOWN"

# The states this phase is permitted to produce. PROVEN_NOT_ADMITTED is
# DELIBERATELY ABSENT.
#
# An earlier draft emitted it when the terminal was no longer alive, reasoning
# that a dead terminal can never admit the command. That is a plausible
# positive signal, but it is a LIVENESS theorem being used to answer an
# ADMISSION question, and it was not proven - the terminal could have died
# after admitting and running the command. Positive non-admission requires an
# independent shell-readiness / foreground-state theorem, which is the next
# phase. Until then absence of a BEGIN is ADMISSION_UNKNOWN and nothing
# stronger, because absence of evidence is not evidence of absence.
PROVEN_ADMITTED = "PROVEN_ADMITTED"
ADMISSION_UNKNOWN = "ADMISSION_UNKNOWN"


class AdmissionEvidence(object):
    """Evidence FIELDS first, a synthesized status second.

    The fields are what was observed; the status is an interpretation of them.
    Keeping both means a caller that disagrees with the interpretation can
    still see what it was built from.
    """

    __slots__ = ("execution_id", "terminal_id", "terminal_generation",
                 "wrapped_command", "wrapped_command_sha256",
                 "submitted", "submission_accepted", "begin_observed",
                 "end_observed", "exit_code", "observation_start_offset",
                 "terminal_alive", "anomalies", "powershell_success",
                 "native_exit_code",
                 "observation_end_offset", "retained_buffer_bytes",
                 "possible_output_loss", "not_submitted_reason",
                 "pre_submit_state")

    def __init__(self, execution_id, terminal_id=None, terminal_generation=None,
                 wrapped_command=None, wrapped_command_sha256=None,
                 submitted=False, submission_accepted=None,
                 begin_observed=False, end_observed=False,
                 exit_code=None, observation_start_offset=None,
                 terminal_alive=None, anomalies=(),
                 powershell_success=None, native_exit_code=None,
                 observation_end_offset=None,
                 retained_buffer_bytes=None, possible_output_loss=None,
                 not_submitted_reason=None, pre_submit_state=None):
        # The extension's refusal reason, verbatim. Set only when CODEAPI
        # declined to write; never inferred from a missing BEGIN.
        self.not_submitted_reason = not_submitted_reason
        self.pre_submit_state = pre_submit_state
        # The two authoritative result facts. exit_code below is derived from
        # them by _compat_exit and is an interpretation, not a third fact.
        self.powershell_success = powershell_success
        self.native_exit_code = native_exit_code
        # A21: the ring buffer truncates from the front, so a window wider
        # than what the buffer retained means evidence may have been evicted
        # before anyone looked at it.
        self.observation_end_offset = observation_end_offset
        self.retained_buffer_bytes = retained_buffer_bytes
        self.possible_output_loss = possible_output_loss
        self.execution_id = execution_id
        self.terminal_id = terminal_id
        # `unavailable` rather than a fabricated value: nothing currently mints
        # a terminal generation, and inventing one would make the field look
        # like evidence (A12).
        self.terminal_generation = terminal_generation
        self.wrapped_command = wrapped_command
        self.wrapped_command_sha256 = wrapped_command_sha256
        self.submitted = submitted
        # Transport acceptance, recorded as its OWN field precisely so it can
        # never be mistaken for admission. A run where this is True and
        # begin_observed is False is the whole point of the phase.
        self.submission_accepted = submission_accepted
        self.begin_observed = begin_observed
        self.end_observed = end_observed
        self.exit_code = exit_code
        # The read `bytes` counter captured immediately before submission.
        # extension.js increments it on every onData and never resets it on
        # clear, so it is a genuine monotonic boundary rather than an invented
        # one (A6).
        self.observation_start_offset = observation_start_offset
        self.terminal_alive = terminal_alive
        self.anomalies = tuple(anomalies)

    @property
    def status(self):
        if self.not_submitted_reason is not None:
            return STATUS_PROVEN_NOT_SUBMITTED
        if not self.submitted:
            return STATUS_SUBMITTED
        if self.begin_observed and self.end_observed:
            return STATUS_COMPLETED
        if self.begin_observed:
            return STATUS_ADMITTED
        return STATUS_ADMISSION_UNKNOWN

    @property
    def verdict(self):
        """The admission claim, kept separate from lifecycle position.

        Only two values in this phase. Absence of a BEGIN is NOT proof of
        non-admission - the buffer may have been cleared, evicted by the
        extension's ring truncation, or read before the shell got to it - so
        it is ADMISSION_UNKNOWN, never a negative proof.
        """
        return PROVEN_ADMITTED if self.begin_observed else ADMISSION_UNKNOWN

    @property
    def completed(self):
        return self.end_observed and self.begin_observed

    def as_dict(self):
        return {
            "execution_id": self.execution_id,
            "terminal_id": self.terminal_id,
            "terminal_generation": self.terminal_generation,
            "wrapped_command": self.wrapped_command,
            "wrapped_command_sha256": self.wrapped_command_sha256,
            "submitted": self.submitted,
            "submission_accepted": self.submission_accepted,
            "not_submitted_reason": self.not_submitted_reason,
            "pre_submit_state": self.pre_submit_state,
            "begin_observed": self.begin_observed,
            "end_observed": self.end_observed,
            "powershell_success": self.powershell_success,
            "native_exit_code": self.native_exit_code,
            # Derived from the two above; kept last so the facts read first.
            "exit_code": self.exit_code,
            "observation_start_offset": self.observation_start_offset,
            "observation_end_offset": self.observation_end_offset,
            "retained_buffer_bytes": self.retained_buffer_bytes,
            "possible_output_loss": self.possible_output_loss,
            "terminal_alive": self.terminal_alive,
            "status": self.status,
            "verdict": self.verdict,
            "completed": self.completed,
            "anomalies": list(self.anomalies),
        }


def marker_absent_from_submission(command, execution_id, shell="powershell"):
    """The A3 theorem, as a callable check rather than a claim in a comment.

    Returns True when NEITHER final marker string occurs verbatim anywhere in
    the text that will be submitted to the PTY. If this is ever False the
    detector can be satisfied by input echo, and admission would be 'proven'
    by the very case it exists to catch.
    """
    envelope = build_envelope(command, execution_id, shell=shell)
    return (begin_marker(execution_id) not in envelope
            and end_marker_prefix(execution_id) not in envelope)


def begin_marker(execution_id):
    """The exact string the SHELL emits. Never present in the submission."""
    return "{} {}".format(BEGIN_WORD, execution_id)


def end_marker_prefix(execution_id):
    """The exact END string minus its trailing exit code."""
    return "{} {}".format(END_WORD, execution_id)


def classify(buffer, execution_id, submitted, terminal_id=None,
             terminal_generation=None, wrapped_command=None,
             terminal_alive=None, submission_accepted=None,
             observation_start_offset=None):
    """Admission evidence from an OBSERVED terminal buffer.

    Note what this function cannot be given: `wrote`, an HTTP status, or any
    other transport signal. That is the critical theorem made structural -

        transport write success does not set admitted=true

    Only the envelope appearing in output can do that, and there is no
    parameter through which a transport result could reach the decision.
    """
    text = _strip_ansi(buffer or "")
    begin = bool(_begin_re(execution_id).search(text))
    m = _end_re(execution_id).search(text)

    powershell_success = None
    native_exit_code = None
    if m:
        powershell_success = m.group(1) == "1"
        native_exit_code = None if m.group(2) == "-" else int(m.group(2))
    exit_code = _compat_exit(powershell_success, native_exit_code)

    anomalies = []
    if _native_exit_disagrees(powershell_success, native_exit_code):
        anomalies.append("native_exit_disagrees_with_powershell_success")
    if m and not begin:
        # An END with no BEGIN is not a completion. Something produced the end
        # marker without the shell having produced the start of the envelope,
        # so the exit code is not trustworthy evidence of this execution.
        anomalies.append("end_without_begin")
        exit_code = None
        powershell_success = None
        native_exit_code = None

    return AdmissionEvidence(
        execution_id=execution_id,
        terminal_id=terminal_id,
        terminal_generation=terminal_generation,
        wrapped_command=wrapped_command,
        wrapped_command_sha256=(command_sha256(wrapped_command)
                                if wrapped_command is not None else None),
        submitted=bool(submitted),
        submission_accepted=submission_accepted,
        begin_observed=begin,
        end_observed=bool(m) and begin,
        exit_code=exit_code,
        powershell_success=powershell_success,
        native_exit_code=native_exit_code,
        observation_start_offset=observation_start_offset,
        terminal_alive=terminal_alive,
        anomalies=anomalies)


def observed(execution_id, submitted, submission_accepted, begin_observed,
             end_observed, powershell_success=None, native_exit_code=None,
             terminal_id=None, terminal_generation=None, wrapped_command=None,
             observation_start_offset=None, observation_end_offset=None,
             retained_buffer_bytes=None, terminal_alive=None, anomalies=(),
             not_submitted_reason=None, pre_submit_state=None):
    """Evidence built from ACCUMULATED observations rather than one buffer.

    A20: admission and completion are historical facts. Once a runtime BEGIN
    has been seen for an execution it stays seen, even though the extension's
    ring buffer truncates from the front at maxBytes and will eventually evict
    the marker. Re-deriving state from only the currently retained buffer
    would let a long-running command decay from ADMITTED back to
    ADMISSION_UNKNOWN - the evidence would get WEAKER as the command produced
    MORE output, which is precisely backwards.

    A21: if the observation window is wider than what the buffer retained,
    output may have been evicted before anyone looked at it, so
    possible_output_loss is recorded. That does not repair a missed BEGIN; it
    stops a truncated observation being mistaken for a complete one.
    """
    loss = None
    if (observation_start_offset is not None
            and observation_end_offset is not None
            and retained_buffer_bytes is not None):
        window = observation_end_offset - observation_start_offset
        loss = window > retained_buffer_bytes

    anomalies = list(anomalies)
    if loss and not begin_observed:
        anomalies.append("possible_output_loss_before_observation")

    exit_code = None
    if end_observed and powershell_success is not None:
        exit_code = _compat_exit(powershell_success, native_exit_code)
        if _native_exit_disagrees(powershell_success, native_exit_code):
            anomalies.append("native_exit_disagrees_with_powershell_success")

    return AdmissionEvidence(
        execution_id=execution_id, terminal_id=terminal_id,
        terminal_generation=terminal_generation,
        wrapped_command=wrapped_command,
        wrapped_command_sha256=(command_sha256(wrapped_command)
                                if wrapped_command is not None else None),
        submitted=bool(submitted), submission_accepted=submission_accepted,
        begin_observed=bool(begin_observed),
        end_observed=bool(end_observed and begin_observed),
        exit_code=exit_code, powershell_success=powershell_success,
        native_exit_code=native_exit_code,
        observation_start_offset=observation_start_offset,
        observation_end_offset=observation_end_offset,
        retained_buffer_bytes=retained_buffer_bytes,
        possible_output_loss=loss, terminal_alive=terminal_alive,
        anomalies=anomalies, not_submitted_reason=not_submitted_reason,
        pre_submit_state=pre_submit_state)


def between_markers(buffer, execution_id):
    """The wrapped command's own output: what lies between the two markers."""
    text = _strip_ansi(buffer or "")
    b = _begin_re(execution_id).search(text)
    if not b:
        return ""
    tail = text[b.end():]
    e = _end_re(execution_id).search(tail)
    return (tail[:e.start()] if e else tail).strip("\r\n").rstrip()


# OSC FIRST. The previous order put the two-character-escape branch first, and
# its class [@-Z\\-_] contains the range \x5C-\x5F, which INCLUDES ']' (0x5D).
# So "\x1b]" matched that branch, consuming only two characters and leaving the
# OSC payload behind: a shell-integration sequence stripped to "633;C" glued
# itself to whatever followed. With shell integration now enabled in
# production, that residue landed immediately before CODEAPI-BEGIN and the
# line-anchored detector could not match it — the command had run correctly and
# the markers were present, but admission read as UNKNOWN. It presented as
# flakiness because whether the marker still began a line depended on where the
# terminal wrapped. Alternation is left-to-right, so the longer OSC form has to
# come first or it can never win.
# The OSC payload may CONTAIN escape sequences. An earlier version of this fix
# narrowed it to [^\x07\x1b]*, which was wrong: PSReadLine repaints a long
# input line and injects cursor-movement escapes INSIDE the 633;E payload that
# carries the echoed command, so the match stopped at that embedded ESC, the
# whole OSC failed, and its entire payload was left as literal text - the
# echoed envelope, ending in the nonce and a stranded BEL, sitting directly in
# front of CODEAPI-BEGIN. An OSC ends at BEL (or ST); it does not end at an
# ESC. [^\x07]* is bounded by the next BEL, so it cannot run away.
_ANSI_RE = re.compile(
    r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-9;?]*[ -/]*[@-~]|[@-Z\\-_])")


# Residual C0 controls, keeping TAB, LF and CR because those carry layout.
#
# Stripping escape sequences is not sufficient on a real terminal. A long
# command line makes PSReadLine REPAINT the input, interleaving cursor-movement
# CSI sequences into the middle of an OSC payload; the OSC pattern then stops
# at that embedded ESC, the introducer is consumed elsewhere, and the
# sequence's terminating BEL is left orphaned in the stream. Measured: a bare
# \x07 sat immediately before CODEAPI-BEGIN, and since BEL is not whitespace
# the line-anchored detector could not skip it — the command had run and both
# markers were present, yet admission read as UNKNOWN.
#
# BEL is a terminator or a bell, never content, and the same is true of the
# other C0 controls kept out here. Removing them makes line anchoring robust
# without weakening it, which matters because the anchor is part of the
# anti-echo defence and must not be relaxed to make a marker match.
_C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_ansi(text):
    return _C0_RE.sub("", _ANSI_RE.sub("", text))
