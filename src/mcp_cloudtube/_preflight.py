"""Dependency check that runs before anything third-party is imported.

Deliberately imports only the standard library. It exists to turn the single
most likely setup failure into an explanation:

The Claude Desktop extension defaults its interpreter to whatever `python` is
on PATH, which very often is not the one holding this project's dependencies.
When that happens the process dies during import, and the app can report only
"couldn't start" -- which says nothing about the cause or the fix.

This module cannot live in __main__ alongside the real imports, because those
imports are what fail. It has to be imported, and called, first.
"""

from __future__ import annotations

import importlib.util
import sys

#: Import name -> the distribution that provides it.
REQUIRED: dict[str, str] = {
    "mcp": "mcp",
    "googleapiclient": "google-api-python-client",
    "google.oauth2": "google-auth",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "structlog": "structlog",
    "anyio": "anyio",
    "pypdf": "pypdf",
}


def missing_dependencies() -> list[str]:
    """Distributions that are not importable by the running interpreter."""
    missing: list[str] = []
    for module, distribution in REQUIRED.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(distribution)
    return missing


def check_dependencies() -> None:
    """Exit with actionable guidance if this interpreter cannot run the server."""
    missing = missing_dependencies()
    if not missing:
        return

    win = "<project>" + '\\' + ".venv" + '\\' + "Scripts" + '\\' + "python.exe"
    lines = [
        "",
        "cloudtube: this Python cannot run the server.",
        f"  interpreter : {sys.executable}",
        f"  missing     : {', '.join(missing)}",
        "",
        "  Those dependencies are not installed for this interpreter.",
        "",
        "  Running as a Claude Desktop extension? Open its settings and set",
        "  'Python interpreter' to the project's virtual environment rather",
        "  than plain 'python':",
        "",
        f"    Windows          {win}",
        "    macOS / Linux    <project>/.venv/bin/python",
        "",
        "  Create it first with scripts/" + '\\' + "bootstrap.ps1 or `make setup`.",
        "",
    ]
    for line in lines:
        print(line, file=sys.stderr)
    raise SystemExit(3)
