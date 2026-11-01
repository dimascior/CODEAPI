"""RunResult — structured evidence from verified terminal execution.

Ported from the retired seed CLI (Phase 10): the RunResult dataclass and
the two parsers (_ps_wrapper, _parse_status_line). Behavior is the seed's.

The evidence model is the point of run_verified: a PTY write returning {wrote:N}
does not mean the command succeeded, so verified runs wrap the command in
start/status/end markers, poll the buffer, and record every stage.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class RunResult:
    """Structured execution evidence from run_result/run_verified."""
    ok: bool
    run_id: str
    command: str
    output: str
    warnings: List[str] = field(default_factory=list)
    session: Optional[dict] = None
    terminal_before: Optional[dict] = None
    dispatch: Optional[dict] = None
    completion: Optional[dict] = None
    status: Optional[dict] = None
    terminal_after: Optional[dict] = None


def _ps_wrapper(run_id, command):
    """Build a PowerShell wrapper that emits start/status/end markers.
    Semicolons chain the capture so $? reflects the user command."""
    sm = f"__CodeAPI_{run_id}_STATUS__"
    em = f"__CodeAPI_{run_id}_END__"
    return (
        f"{command}; "
        f"$__cs=$?; $__cl=$LASTEXITCODE; "
        f"if($null -eq $__cl){{if($__cs){{$__ce=0}}else{{$__ce=1}}}}"
        f"else{{$__ce=$__cl}}; "
        f'Write-Output "{sm} exit_code=$__ce success=$__cs'
        f' last_exit_code=$__cl"; '
        f'Write-Output "{em}"'
    )


def _parse_status_line(text, run_id):
    """Parse __CodeAPI_<id>_STATUS__ key=value pairs from a buffer line."""
    marker = f"__CodeAPI_{run_id}_STATUS__"
    idx = text.find(marker)
    if idx < 0:
        return None
    rest = text[idx + len(marker):].strip()
    parts = {}
    for token in rest.split():
        if "=" in token:
            k, v = token.split("=", 1)
            parts[k] = v
    try:
        exit_code = int(parts.get("exit_code", "-1"))
    except ValueError:
        exit_code = -1
    ps_success = parts.get("success", "False").lower() == "true"
    lec = parts.get("last_exit_code", "")
    if not lec or lec.lower() in ("$null", "none", ""):
        last_exit = None
    else:
        try:
            last_exit = int(lec)
        except ValueError:
            last_exit = None
    return {"exit_code": exit_code, "powershell_success": ps_success,
            "last_exit_code": last_exit}
