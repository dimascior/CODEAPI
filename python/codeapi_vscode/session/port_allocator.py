"""PortAllocator — pick a free loopback port in the reserved adapter band.

ONE coherent policy across the stack: allocation (here and in the JS adapter's
bridge_core.pickFreePort) and discovery (discovery.py ADAPTER_RANGE) all use the
SAME band, 47337-47400. The VS Code window band (3637-3646) stays reserved for
legacy runtimes so the service never poses as a window (no phantom-session
poisoning), and a port OUTSIDE the adapter band is refused rather than accepted —
an out-of-band runtime would start fine and then be invisible to discovery.
Exhaustion of all 64 ports raises visibly instead of overflowing the band.
"""

import socket

RESERVED_BAND = range(47337, 47401)     # 47337-47400 — matches discovery ADAPTER_RANGE
WINDOW_BAND = range(3637, 3647)


class PortAllocationError(Exception):
    pass


class PortAllocator:
    """Resolve a bindable loopback port inside the reserved adapter band."""

    def __init__(self, band=RESERVED_BAND):
        self.band = band

    def resolve(self, preferred=None, exact=False):
        """Return a bindable port.

        With ``exact=True`` (the adapter-launched path) `preferred` is required
        AS-IS: if it cannot be bound the call raises instead of falling forward
        to another port. Fall-forward would silently break the launching
        adapter's probe -> spawn -> readiness handshake: the adapter would poll
        the port it chose while the runtime answered on a different one (or,
        worse, some OTHER runtime answered on the polled port). The adapter
        owns the retry: it sees the distinct failure and probes the next
        candidate itself.

        Without ``exact`` (no preferred port), scans the band for the first
        free port. A preferred port outside the band is always refused — it
        would be undiscoverable."""
        candidates = []
        if preferred is not None:
            if preferred in WINDOW_BAND:
                raise PortAllocationError(
                    f"port {preferred} is inside the VS Code window band "
                    f"{WINDOW_BAND.start}-{WINDOW_BAND.stop - 1}")
            if preferred not in self.band:
                raise PortAllocationError(
                    f"port {preferred} is outside the reserved adapter band "
                    f"{self.band.start}-{self.band.stop - 1}; discovery only "
                    f"scans that band, so the runtime would be undiscoverable")
            if exact:
                if self._free(preferred):
                    return preferred
                raise PortAllocationError(
                    f"preferred port {preferred} is already in use (exact bind "
                    f"required; the launching adapter retries the next candidate)")
            candidates.append(preferred)
        candidates.extend(p for p in self.band if p != preferred)
        for port in candidates:
            if self._free(port):
                return port
        raise PortAllocationError(
            f"adapter band {self.band.start}-{self.band.stop - 1} exhausted "
            f"({len(self.band)} ports): no free port for the runtime")

    @staticmethod
    def _free(port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            s.close()
