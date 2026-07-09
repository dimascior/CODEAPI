"""/add-endpoint, /remove-endpoint, and the canned dynamic GET (Phase 11).

Fully portable — no vscode API. Shapes match docs/WIRE_CONTRACT.md:
  POST /add-endpoint {path, response}  -> {success:true, message:"Endpoint <p> registered."}
  POST /remove-endpoint {path}         -> {success:true, message:"Endpoint <p> removed."}
                                          or 404 {error:"Endpoint not found."}
  GET <registered path>                -> {success:true, dynamic:true, path, response}
"""

_MISSING = object()


class DynamicRegistry:
    def __init__(self):
        self._reg = {}

    def add(self, path, response):
        self._reg[path] = response

    def remove(self, path):
        return self._reg.pop(path, _MISSING) is not _MISSING

    def has(self, path):
        return path in self._reg

    def get(self, path):
        return self._reg.get(path)


def register(router, registry):
    def add_handler(req):
        body = req.json()
        path = body.get("path")
        if not path:
            return (400, {"error": "path required"})
        registry.add(path, body.get("response"))
        return (200, {"success": True, "message": f"Endpoint {path} registered."})

    def remove_handler(req):
        body = req.json()
        path = body.get("path")
        if path and registry.remove(path):
            return (200, {"success": True, "message": f"Endpoint {path} removed."})
        return (404, {"error": "Endpoint not found."})

    router.register("POST", "/add-endpoint", add_handler)
    router.register("POST", "/remove-endpoint", remove_handler)
    router.set_dynamic_getter(
        lambda path: ({"success": True, "dynamic": True, "path": path,
                       "response": registry.get(path)}
                      if registry.has(path) else None))
