"""codeapi_runtime_main — packaged-runtime entrypoint (Phase 14).

PyInstaller freezes the entry module as a top-level ``__main__`` script, so
``codeapi_vscode/runtime.py`` cannot be the entry directly: its relative imports
(``from .server`` / ``.session`` / ``.vscode`` ...) raise
``ImportError: attempted relative import with no known parent package`` when run
without a parent package.

This absolute-import wrapper imports the package form and delegates to
``main()``. Behavior is identical to ``python -m codeapi_vscode.runtime`` — the
same argparse config, port guard, and server wiring. Dev mode keeps using the
``-m`` form; only the packaged build uses this entry.
"""

from codeapi_vscode.runtime import main

if __name__ == "__main__":
    raise SystemExit(main())
