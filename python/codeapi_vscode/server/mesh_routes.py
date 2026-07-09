"""/mesh/peers and /mesh/broadcast/:endpoint routes (Phase 11).

Shapes match docs/WIRE_CONTRACT.md. Mesh discovery is inert on the runtime (no
peers), so /mesh/peers -> [] and /mesh/broadcast/:endpoint -> {success:true,
results:[]}. Porting the contract now; real peer discovery is separate work.
"""


def register(router, session_manager=None):
    router.register("GET", "/mesh/peers", lambda req: (200, []))
    router.register_param(
        "POST", "/mesh/broadcast/:endpoint",
        lambda req: (200, {"success": True, "results": []}))
