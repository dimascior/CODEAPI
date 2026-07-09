"""PortAllocator — pick a free loopback port in the reserved Python-service band.

Phase 11. The band is 47337-47346 — deliberately OUTSIDE the VS Code window band
(3637-3646), so the runtime never poses as a VS Code window (no phantom-session
poisoning) before the Phase-14 cutover.
"""

import socket

RESERVED_BAND = range(47337, 47347)
WINDOW_BAND = range(3637, 3647)


class PortAllocationError(Exception):
    pass


class PortAllocator:
    """Resolve a bindable loopback port, refusing the VS Code window band."""

    def __init__(self, band=RESERVED_BAND):
        self.band = band

    def resolve(self, preferred=None):
        """Return the first bindable port: `preferred` (if free and out-of-band),
        else the first free port in the reserved band."""
        candidates = []
        if preferred is not None:
            if preferred in WINDOW_BAND:
                raise PortAllocationError(
                    f"port {preferred} is inside the VS Code window band "
                    f"{WINDOW_BAND.start}-{WINDOW_BAND.stop - 1}")
            candidates.append(preferred)
        candidates.extend(p for p in self.band if p != preferred)
        for port in candidates:
            if self._free(port):
                return port
        raise PortAllocationError(
            f"no free port in reserved band {self.band.start}-{self.band.stop - 1}")

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
