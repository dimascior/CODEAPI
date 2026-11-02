#!/usr/bin/env python3
"""Opt-in CODEAPI public API auth regression, no external dependencies."""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from codeapi_vscode.server.http_server import _Handler, HttpServer
from codeapi_vscode.server.router import Router


class AuthTests(unittest.TestCase):
    def setUp(self):
        router = Router()
        router.register("GET", "/health", lambda req: (200, {"status": "ok"}))
        router.register("GET", "/session", lambda req: (200, {"id": "window"}))
        router.register("GET", "/term/list", lambda req: (200, []))
        router.register("POST", "/exec", lambda req: (200, {"executed": True}))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.router = router
        self.httpd.public_token = "test-secret"
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join(timeout=5)

    def request(self, path, method="GET", token=None):
        headers = {} if token is None else {"X-CODEAPI-TOKEN": token}
        data = b"" if method == "POST" else None
        url = "http://127.0.0.1:%s%s" % (self.httpd.server_address[1], path)
        req = urllib.request.Request(url, headers=headers, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_discovery_stays_open(self):
        self.assertEqual(self.request("/health")[0], 200)
        self.assertEqual(self.request("/session")[0], 200)

    def test_reject_missing_and_wrong_token(self):
        for method, path in (("GET", "/term/list"), ("POST", "/exec")):
            self.assertEqual(self.request(path, method)[0], 401)
            self.assertEqual(self.request(path, method, "wrong")[0], 401)

    def test_accept_valid_token(self):
        self.assertEqual(self.request("/term/list", token="test-secret"), (200, []))
        self.assertEqual(self.request("/exec", "POST", "test-secret"),
                         (200, {"executed": True}))

    def test_legacy_default(self):
        self.httpd.public_token = ""
        self.assertEqual(self.request("/exec", "POST"), (200, {"executed": True}))

    def test_environment_configuration(self):
        old = os.environ.get("CODEAPI_PUBLIC_TOKEN")
        try:
            os.environ["CODEAPI_PUBLIC_TOKEN"] = "from-env"
            self.assertEqual(HttpServer().public_token, "from-env")
        finally:
            if old is None:
                os.environ.pop("CODEAPI_PUBLIC_TOKEN", None)
            else:
                os.environ["CODEAPI_PUBLIC_TOKEN"] = old


if __name__ == "__main__":
    unittest.main()
