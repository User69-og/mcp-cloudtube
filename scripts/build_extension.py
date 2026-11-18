"""Build the Claude Desktop extension (.mcpb) from the project source.

Why the bundle does not vendor dependencies
-------------------------------------------
Vendoring the full dependency tree produces a 196 MB bundle containing 57
compiled binaries -- pydantic_core, cryptography, protobuf's _upb and others
-- every one of them built for one platform and one Python minor version. A
bundle like that is large, and worse, silently useless to anyone on a
different OS or Python than the machine that built it.

So the bundle carries only this project's own source, and the manifest asks
the installer for a Python interpreter that already has the dependencies.
That is not an extra burden: the repository has to be cloned and set up
anyway, and `scripts/bootstrap.ps1` / `make setup` produces exactly the
interpreter the dialog asks for.

What the extension genuinely removes is hand-editing a JSON config file in an
application support directory -- which is the part that actually goes wrong.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "build" / "extension"
DIST = ROOT / "dist"
PACKAGE = ROOT / "src" / "mcp_cloudtube"
MANIFEST = ROOT / "extension" / "manifest.json"

EXCLUDE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", "*.pyd", ".pytest_cache", "*.egg-info"
)


def main() -> int:
    if not MANIFEST.exists():
        print(f"manifest not found: {MANIFEST}", file=sys.stderr)
        return 1

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = manifest["version"]
    name = manifest["name"]

    # --- stage -----------------------------------------------------------
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)
    DIST.mkdir(exist_ok=True)

    shutil.copy2(MANIFEST, STAGING / "manifest.json")
    shutil.copytree(PACKAGE, STAGING / "server" / "mcp_cloudtube", ignore=EXCLUDE)

    # A README inside the bundle, for anyone who unpacks it to see what they
    # are about to run. An extension that cannot be inspected deserves
    # suspicion, so make inspecting it easy.
    (STAGING / "README.md").write_text(
        f"# {manifest['display_name']}\n\n"
        f"{manifest['description']}\n\n"
        "This bundle contains only this project's own Python source, under\n"
        "`server/mcp_cloudtube/`. It vendors no dependencies and executes\n"
        "nothing at install time.\n\n"
        "It runs using the Python interpreter you select during installation,\n"
        "which must already have the project's dependencies installed.\n\n"
        f"Source: {manifest['repository']['url']}\n",
        encoding="utf-8",
    )

    files = sum(1 for p in STAGING.rglob("*") if p.is_file())
    staged_bytes = sum(p.stat().st_size for p in STAGING.rglob("*") if p.is_file())
    print(f"staged  {files} files, {staged_bytes / 1024:.0f} KB")

    # --- validate ---------------------------------------------------------
    validate = subprocess.run(
        ["npx", "--yes", "@anthropic-ai/mcpb", "validate", str(STAGING / "manifest.json")],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    print(validate.stdout.strip() or validate.stderr.strip())
    if validate.returncode != 0:
        return validate.returncode

    # --- pack -------------------------------------------------------------
    output = DIST / f"{name}-{version}.mcpb"
    output.unlink(missing_ok=True)
    pack = subprocess.run(
        ["npx", "--yes", "@anthropic-ai/mcpb", "pack", str(STAGING), str(output)],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    print(pack.stdout.strip() or pack.stderr.strip())
    if pack.returncode != 0:
        return pack.returncode

    print()
    print(f"built: {output}")
    print(f"size : {output.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
