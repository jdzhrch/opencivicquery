"""Build the OpenCivicQuery SF.gov extension for Claude Desktop as one .mcpb file.

    uv run python scripts/build_sfgov_extension.py [--validate] [--smoke]

The bundle holds the manifest, a small entry file, the server's source, pyproject.toml and
uv.lock. Claude Desktop installs the dependencies with uv on the reporter's computer, so the
reporter needs no Python of their own. The file lands in dist/.

--validate checks the manifest with the official MCPB command-line tool (needs Node.js).
--smoke unpacks the built file, starts its server the way the manifest says, and lists its
tools over stdio. Add --call to also run one live title search.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "sfgov-desktop"
DIST = ROOT / "dist"
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"
EXPECTED_TOOLS = {
    "find_sfgov_news",
    "find_sfgov_meetings",
    "search_sfgov",
    "pin_snapshot",
    "get_snapshot",
}
ROOT_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE")
EXTENSION_FILES = ("manifest.json", "server.py", "deployment.yaml")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def bundle_files() -> dict[str, Path]:
    """Each path inside the bundle, mapped to its source file."""
    files = {name: EXTENSION / name for name in EXTENSION_FILES}
    files |= {name: ROOT / name for name in ROOT_FILES}
    package = ROOT / "src" / "opencivicquery"
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files[path.relative_to(ROOT).as_posix()] = path
    return files


def manifest() -> dict:
    return json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))


def build() -> Path:
    data = manifest()
    tools = {tool["name"] for tool in data["tools"]}
    if tools != EXPECTED_TOOLS:
        raise SystemExit(f"manifest tools {sorted(tools)} differ from {sorted(EXPECTED_TOOLS)}")
    DIST.mkdir(exist_ok=True)
    output = DIST / f"opencivicquery-sfgov-{data['version']}.mcpb"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, source in sorted(bundle_files().items()):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, source.read_bytes())
    return output


def validate() -> None:
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("--validate needs Node.js (npx) for the official MCPB tool")
    subprocess.run([npx, "-y", MCPB_CLI, "validate", str(EXTENSION / "manifest.json")], check=True)


async def smoke(bundle: Path, *, call: bool) -> None:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    with tempfile.TemporaryDirectory() as unpacked, tempfile.TemporaryDirectory() as snapshots:
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(unpacked)
        config = json.loads((Path(unpacked) / "manifest.json").read_text())["server"]
        args = [arg.replace("${__dirname}", unpacked) for arg in config["mcp_config"]["args"]]
        parameters = StdioServerParameters(
            command=config["mcp_config"]["command"],
            args=args,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "OPENCIVICQUERY_SNAPSHOT_PATH": snapshots,
            },
        )
        async with Client(parameters, mode="legacy", read_timeout_seconds=300) as client:
            names = sorted(tool.name for tool in (await client.list_tools()).tools)
            print(f"tools: {names}")
            if set(names) != EXPECTED_TOOLS:
                raise SystemExit("the packaged server offers other tools than expected")
            if call:
                result = await client.call_tool(
                    "search_sfgov",
                    {
                        "text": "point in time count",
                        "reporter_question": "Extension smoke test: pages titled PIT count",
                    },
                )
                payload = result.structured_content or {}
                print(
                    "search_sfgov:",
                    "error" if result.is_error else "ok",
                    payload.get("returned_row_count"),
                    payload.get("completeness"),
                    "snapshot" if payload.get("snapshot") else "no snapshot",
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--call", action="store_true", help="with --smoke, one live search")
    args = parser.parse_args()
    if args.validate:
        validate()
    output = build()
    print(f"built {output.relative_to(ROOT)} ({output.stat().st_size:,} bytes)")
    if args.smoke:
        asyncio.run(smoke(output, call=args.call))


if __name__ == "__main__":
    sys.exit(main())
