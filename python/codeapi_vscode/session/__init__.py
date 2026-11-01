"""Session registry / port allocation / window identity (Phase 11, Tier 2).

Ports the session bookkeeping currently inside dist/extension.js. Encodes the
verified operating facts: ports are the stable targeting key (3637-3646, assigned
in window-open order), and session ids ROTATE under a stable pid — so identity
must be tracked, not assumed.
"""
