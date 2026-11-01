"""Machine-readable CODEAPI operation digest (pure Python, additive).

  GET /operations               -> {"protocol":"codeapi.operations.v1","operations":[...]}
  GET /operations/:operationId  -> one operation, or 404

The digest organizes the ALREADY-IMPLEMENTED public routes into complete, ordered
operations so an unfamiliar agent selects the CODEAPI terminal corridor directly. It is
functional metadata (routes, required fields, verification dependencies, postconditions)
— NOT prose and NOT runtime facts: it lists no wrong mechanisms and manufactures no
liveness (that is the adapter's, surfaced only through /term/* at call time). It is
independent of /apis (whose 13 fixed VS Code descriptors are unchanged).
"""

PROTOCOL = "codeapi.operations.v1"

# Ordered operation definitions. Each names the exact route(s), the fields to capture,
# the verification dependency, and the valid success claim. $create.id / $id denote a
# value produced by an earlier step in the same operation.
OPERATIONS = [
    {
        "id": "terminal.discover",
        "purpose": "Find live CODEAPI runtimes and select ONE adapter runtime that can "
                   "own a terminal, by durable identity, failing on ambiguity.",
        "request": {"method": "GET", "path": "/session"},
        "scan": {"probe": "GET /term/list per responder",
                 "adapter_when": "terminal routes present (live)"},
        "select": {"identity_precedence": ["codeapiRuntimeId", "vscodeSessionId",
                                           "codeapiWorkspaceId", "extensionHostPid",
                                           "workspaceUri", "serverPort"],
                   "require": {"runtime_type": "adapter", "terminal_routes": "live"},
                   "ambiguity": "error",
                   "not_identity": ["/health 200", "first responding port",
                                    "workspace path resemblance", "foreground window"]},
        "claim": "Only an adapter runtime with terminal_routes:live can own a terminal.",
    },
    {
        "id": "terminal.open_visible",
        "purpose": "Create and verify an adapter-owned VS Code integrated terminal.",
        "target": {"runtime_type": "adapter", "terminal_routes": "live",
                   "identity": "codeapiRuntimeId", "ambiguity": "error"},
        "request": {"method": "POST", "path": "/term/create",
                    "body": {"shell": "powershell.exe", "show": True}},
        "capture": ["id", "pid", "shell", "cwd", "surface"],
        "require_create": {"surface": "vscode-terminal"},
        "verify": {"method": "GET", "path": "/term/list",
                   "match": {"id": "$create.id"},
                   "require": {"surface": "vscode-terminal", "alive": True,
                               "exitCode": None}},
        "channels": {"input": "POST /term/$id/write",
                     "output": "GET /term/$id/read",
                     "state": "GET /term/list",
                     "cleanup": "DELETE /term/$id"},
        "failure_codes": ["TARGET_AMBIGUOUS", "TERMINAL_ROUTES_UNAVAILABLE",
                          "CREATE_FAILED", "SURFACE_MISMATCH", "TERMINAL_NOT_LISTED",
                          "TERMINAL_DEAD", "STATE_INCOMPLETE"],
        "claim": "A visible CODEAPI terminal is operationally open only after the same "
                 "id reports surface:vscode-terminal and alive:true.",
    },
    {
        "id": "terminal.open_headless",
        "purpose": "Create an adapter-owned headless PTY with NO VS Code terminal UI.",
        "request": {"method": "POST", "path": "/term/create",
                    "body": {"shell": "powershell.exe", "show": False}},
        "capture": ["id", "pid", "shell", "cwd", "surface"],
        "expect_create": {"surface": "process"},
        "claim": "surface:process is NOT a visible terminal and must never be reported "
                 "as an open VS Code integrated terminal.",
    },
    {
        "id": "terminal.verify",
        "purpose": "Confirm a terminal is a live visible terminal (or read its state).",
        "request": {"method": "GET", "path": "/term/list"},
        "match": {"id": "$id"},
        "visible_terminal_when": {"surface": "vscode-terminal", "alive": True},
        "exited_when": {"alive": False, "exitCode": "<integer>"},
        "claim": "Liveness comes from /term/list (alive + exitCode), never from a write "
                 "response, buffer bytes, list presence, or DELETE.",
    },
    {
        "id": "terminal.write",
        "purpose": "Send input to a terminal.",
        "request": {"method": "POST", "path": "/term/$id/write",
                    "body": {"data": "<text>", "enter": True}},
        "returns": {"wrote": "<int>"},
        "meaning": "bytes accepted into PTY input",
        "not_meaning": ["command executed", "command succeeded", "shell alive",
                        "output produced"],
    },
    {
        "id": "terminal.read",
        "purpose": "Read the actual captured terminal output.",
        "request": {"method": "GET", "path": "/term/$id/read",
                    "query": {"tail": "<int>", "clear": "<bool>"}},
        "returns": {"id": "<id>", "bytes": "<int>", "buffer": "<raw ANSI/VT>"},
        "notes": ["buffer is raw terminal data, not clean stdout",
                  "account for the echoed command; strip ANSI when interpreting",
                  "do not clear=true before the needed output / exit marker is captured"],
        "claim": "The read buffer — not wrote — is the evidence a command produced output.",
    },
    {
        "id": "terminal.run_verified",
        "purpose": "Run a command in a verified live terminal and read the actual output.",
        "preconditions": ["target runtime uniquely selected (ambiguity is an error)",
                          "terminal id present in /term/list",
                          "surface:vscode-terminal", "alive:true", "exitCode:null"],
        "steps": [
            {"operation": "write", "route": "POST /term/$id/write",
             "meaning": "bytes accepted only"},
            {"operation": "read", "route": "GET /term/$id/read",
             "meaning": "actual captured terminal output"},
            {"operation": "state", "route": "GET /term/list",
             "meaning": "terminal liveness and exit state"},
        ],
        # Completion and user-output are DIFFERENT facts, read from DIFFERENT regions
        # of the same /read buffer. A command can complete successfully with no output.
        "completion_source": "read.buffer start/end/status markers + parsed exit status",
        "output_source": "read.buffer command-output region between the markers",
        "output_may_be_empty": True,
        "success_when": {
            "completion_observed": True,      # start + end markers seen
            "command_exit_code": 0,           # parsed from the status marker
            "terminal": {"listed": True, "surface": "vscode-terminal",
                         "alive": True, "exitCode": None},
        },
        "not_success_sources": ["write.wrote", "process id alone",
                                "absence of an exception"],
        "notes": ["command completion and user-output presence are different facts",
                  "an empty output body is NOT a failure for a zero-output command",
                  "success additionally requires the terminal to remain a live "
                  "surface:vscode-terminal (a process surface is a separate operation)"],
    },
    {
        "id": "terminal.leave_open",
        "purpose": "Leave a verified terminal open for later use.",
        "do": ["final GET /term/list check (surface:vscode-terminal, alive:true)"],
        "do_not": ["write 'exit'", "DELETE /term/$id", "dispose the terminal"],
    },
    {
        "id": "terminal.observe_exit",
        "purpose": "Establish that a terminal has exited.",
        "request": {"method": "GET", "path": "/term/list"},
        "require": {"alive": False, "exitCode": "<integer>"},
        "claim": "Both alive:false AND an integer exitCode are required; DELETE is not "
                 "the liveness signal.",
    },
    {
        "id": "terminal.reap",
        "purpose": "Clean up a terminal and remove it from the registry.",
        "request": {"method": "DELETE", "path": "/term/$id"},
        "returns": {"killed": "<bool>"},
        "notes": ["killed:false after an observed exit is expected (already gone)",
                  "a read after delete returns 404"],
        "claim": "DELETE is cleanup/removal, not liveness observation.",
    },
]

_BY_ID = {op["id"]: op for op in OPERATIONS}


def register(router):
    router.register("GET", "/operations",
                    lambda req: (200, {"protocol": PROTOCOL, "operations": OPERATIONS}))

    def _one(req):
        op = _BY_ID.get(req.params.get("operationId"))
        if op is None:
            return (404, {"error": "operation not found"})
        return (200, op)

    router.register_param("GET", "/operations/:operationId", _one)
