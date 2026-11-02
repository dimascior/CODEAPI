#!/usr/bin/env python3
"""build_runtime.py — package the Python runtime into a standalone executable.

PyInstaller **onedir** builds a host-native binary, not a cross-compiled one.

No magic: by default this only PRINTS the exact PyInstaller command
(`--dry-run`). Pass `--run` to actually build (requires `pip install pyinstaller`).

    python python/tools/build_runtime.py            # print the command
    python python/tools/build_runtime.py --run      # actually build

Expected output:
    Windows: bin/codeapi-runtime/codeapi-runtime.exe
    macOS/Linux: bin/codeapi-runtime/codeapi-runtime

The JS adapter launches the native binary when packaged, or the host's Python
interpreter in dev mode.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_PARENT = os.path.dirname(HERE)                 # .../python
REPO_ROOT = os.path.dirname(PKG_PARENT)            # repo root
# Absolute-import entry: PyInstaller runs the entry as top-level __main__, so it
# cannot be codeapi_vscode/runtime.py (its relative imports would fail). The
# wrapper does `from codeapi_vscode.runtime import main`; --paths PKG_PARENT puts
# .../python on the module path so `codeapi_vscode` resolves during analysis.
ENTRY = os.path.join(PKG_PARENT, "codeapi_runtime_main.py")
DIST = os.path.join(REPO_ROOT, "bin")
NAME = "codeapi-runtime"


def build_command():
    """The exact PyInstaller invocation (onedir, current host)."""
    return [
        sys.executable, "-m", "PyInstaller",
        "--name", NAME,
        "--onedir",                 # inspectable folder build (not --onefile yet)
        "--console",                # runtime logs to stdout/stderr -> adapter channel
        "--distpath", DIST,         # -> bin/codeapi-runtime/
        "--specpath", os.path.join(PKG_PARENT, "build"),
        "--workpath", os.path.join(PKG_PARENT, "build", "work"),
        "--paths", PKG_PARENT,      # .../python so `codeapi_vscode` resolves
        "--noconfirm",
        # stdlib-only Tier-1 core; declare hidden imports here if the Tier-2
        # server later adds any (keep the runtime dependency-light).
        ENTRY,
    ]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    run = "--run" in argv
    cmd = build_command()
    print("PyInstaller command:\n  " + " ".join(cmd))
    print(f"\nEntry:  {ENTRY}")
    suffix = ".exe" if os.name == "nt" else ""
    print(f"Output: {os.path.join(DIST, NAME, NAME + suffix)}")
    if not run:
        print("\n(dry run — pass --run to build; requires: pip install pyinstaller)")
        return 0
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print("PyInstaller not installed. Run: pip install pyinstaller", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
