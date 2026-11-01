"""Exception types for the CODEAPI Python port.

Canonical error module for the whole package (Tier 1–3). ``util`` helpers and
``server`` routes raise these; the CLI maps them to exit codes.

Seed: ``the retired seed CLI`` (CodeAPIError, ValidationError).
Phase 10 ports the real definitions here. Skeleton for now.
"""


class CodeAPIError(Exception):
    """Base class for all CODEAPI errors (unreachable host, bad target, etc.)."""


class ValidationError(CodeAPIError):
    """Server data did not match the expected shape (strict mode)."""


class TargetError(CodeAPIError):
    """No session matched the requested id / workspace / port."""


class AdapterError(CodeAPIError):
    """The minimal JS adapter failed or returned an error (Tier 3)."""
