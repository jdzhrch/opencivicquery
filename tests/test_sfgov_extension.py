from __future__ import annotations

import importlib.util
import json
import re
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from opencivicquery.config import load_deployment

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "sfgov-desktop"


def builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_sfgov_extension", ROOT / "scripts" / "build_sfgov_extension.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sfgov_tool_names() -> set[str]:
    source = (ROOT / "src" / "opencivicquery" / "mcp_server.py").read_text()
    return set(re.findall(r'@tool\(\s*name="([a-z_]*sfgov[a-z_]*)"', source))


def test_the_manifest_runs_the_entry_file_with_uv_and_lists_the_sfgov_tools() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text())

    assert manifest["manifest_version"] == "0.4"
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", manifest["version"])
    server = manifest["server"]
    assert server["type"] == "uv" and server["entry_point"] == "server.py"
    assert server["mcp_config"]["command"] == "uv"
    assert server["mcp_config"]["args"] == [
        "run",
        "--frozen",
        "--no-dev",
        "--directory",
        "${__dirname}",
        "server.py",
    ]
    names = {tool["name"] for tool in manifest["tools"]}
    assert names == sfgov_tool_names() | {"pin_snapshot", "get_snapshot"}
    assert names == builder().EXPECTED_TOOLS
    entry = (EXTENSION / "server.py").read_text()
    offered = re.search(r'"OPENCIVICQUERY_TOOLS"\] = \(?\s*"([^"]+)"', entry)
    assert offered is not None and set(offered.group(1).split(",")) == names


def test_the_extension_deployment_loads() -> None:
    bundle = load_deployment(EXTENSION / "deployment.yaml")

    assert bundle.config.deployment_id == "sfgov-desktop"
    assert bundle.packs == []


def test_the_bundle_holds_the_server_and_nothing_built_or_tested() -> None:
    files = builder().bundle_files()

    for name in (
        "manifest.json",
        "server.py",
        "deployment.yaml",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "src/opencivicquery/__init__.py",
        "src/opencivicquery/mcp_server.py",
        "src/opencivicquery/sources/sfgov.py",
        "src/opencivicquery/sources/sfgov_copy.py",
    ):
        assert name in files, name
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in files)
    assert not any(name.startswith(("tests/", "operations/", "dist/")) for name in files)
    assert "AGENTS.md" not in files and "MEMORY_INBOX.md" not in files


def test_the_build_writes_the_same_bytes_every_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = builder()
    monkeypatch.setattr(module, "DIST", tmp_path)

    first = module.build().read_bytes()
    output = module.build()

    assert output.read_bytes() == first
    assert output.name == "opencivicquery-sfgov.mcpb"
    with zipfile.ZipFile(output) as bundle:
        names = bundle.namelist()
        assert "manifest.json" in names and "server.py" in names
        assert json.loads(bundle.read("manifest.json")) == module.manifest()
