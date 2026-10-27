from __future__ import annotations

import importlib.util
import json
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "opencivicquery"
SKILL = PLUGIN / "skills" / "ask-open-data"


def load_build_script():
    spec = importlib.util.spec_from_file_location(
        "build_chatgpt_plugin", ROOT / "scripts" / "build_chatgpt_plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def server_tool_names() -> set[str]:
    source = (ROOT / "src" / "opencivicquery" / "mcp_server.py").read_text()
    return set(re.findall(r'@server\.tool\(\s*name="([a-z_]+)"', source))


def skill_frontmatter() -> dict[str, str]:
    text = (SKILL / "SKILL.md").read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md needs YAML front matter"
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"')
    return fields


def test_marketplace_lists_the_plugin_folder() -> None:
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    [entry] = marketplace["plugins"]

    assert entry["name"] == "opencivicquery"
    assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()


def test_plugin_manifest_has_a_semantic_version() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())

    assert manifest["name"] == "opencivicquery"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


def test_plugin_connects_to_the_documented_public_instance() -> None:
    url = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["opencivicquery"]["url"]

    assert url.startswith("https://") and url.endswith("/mcp")
    assert url in (ROOT / "README.md").read_text()


def test_skill_points_to_the_same_connection_as_the_plugin() -> None:
    url = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["opencivicquery"]["url"]
    text = (SKILL / "SKILL.md").read_text()

    assert url in text
    assert "`list_source_packs`" in text.split("## How every request starts")[0]


def test_skill_front_matter_names_the_skill_folder() -> None:
    fields = skill_frontmatter()

    assert fields["name"] == SKILL.name
    assert 100 < len(fields["description"]) <= 1024


def test_skill_mentions_only_tools_the_server_offers() -> None:
    tools = server_tool_names()
    text = (SKILL / "SKILL.md").read_text()
    mentioned = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text)) - {
        "reporter_question",
        "presentation_rules",
        "source_url",
        "query_url",
    }

    assert tools == {
        "list_source_packs",
        "search_datasets",
        "describe_dataset",
        "query_dataset",
        "pin_snapshot",
        "get_snapshot",
    }
    assert mentioned <= tools
    assert tools <= mentioned


def test_skill_links_resolve() -> None:
    text = (SKILL / "SKILL.md").read_text()
    for target in re.findall(r"\]\(([^)]+)\)", text):
        assert (SKILL / target).is_file(), target


@pytest.mark.parametrize("path", [SKILL / "SKILL.md", SKILL / "references" / "counting-traps.md"])
def test_skill_text_uses_no_dashes(path: Path) -> None:
    assert "—" not in path.read_text()
    assert "–" not in path.read_text()


def test_chatgpt_package_carries_the_shared_skill_and_version(tmp_path: Path) -> None:
    build = load_build_script()
    app_id = "asdk_app_0123456789abcdef0123456789abcdef"

    archive = build.build(app_id, tmp_path)

    version = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
    assert archive.name == f"opencivicquery-chatgpt-{version}.zip"
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        plugin = json.loads(bundle.read("plugin.json"))
        apps = json.loads(bundle.read(".app.json"))
        skill_text = bundle.read("skills/ask-open-data/SKILL.md").decode()
    assert {
        ".app.json",
        ".codex-plugin/plugin.json",
        "plugin.json",
        "skills/ask-open-data/SKILL.md",
        "skills/ask-open-data/references/counting-traps.md",
        "skills/ask-open-data/agents/openai.yaml",
    } <= names
    assert plugin["version"] == version
    assert apps["apps"][plugin["name"]]["id"] == app_id
    assert skill_text == (SKILL / "SKILL.md").read_text()


@pytest.mark.parametrize("app_id", ["", "app_123", "asdk_app_XYZ", "asdk_app_12; rm -rf /"])
def test_chatgpt_package_refuses_a_malformed_app_id(tmp_path: Path, app_id: str) -> None:
    build = load_build_script()

    with pytest.raises(SystemExit):
        build.build(app_id, tmp_path)


PACKS = sorted((ROOT / "packs").glob("*.yaml"))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _statement_blocks(lines: list[str], key: str) -> list[list[str]]:
    """Raw lines of every `key:` value or `caveats` item, with the comment lines above it."""
    starts: list[int] = []
    caveat_indent: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if key == "caveats":
            if stripped == "caveats:":
                caveat_indent = _indent(line)
            elif caveat_indent is not None and stripped.startswith("- "):
                if _indent(line) == caveat_indent:
                    starts.append(index)
                else:
                    caveat_indent = None
            elif (
                stripped and not stripped.startswith("#") and _indent(line) <= (caveat_indent or 0)
            ):
                caveat_indent = None
        elif stripped.startswith(f"{key}:") and not stripped.endswith(":"):
            starts.append(index)

    blocks: list[list[str]] = []
    for index in starts:
        line = lines[index]
        block = [line]
        for follow in lines[index + 1 :]:
            if (
                follow.strip()
                and _indent(follow) > _indent(line)
                and not follow.strip().startswith("- ")
            ):
                block.append(follow)
            else:
                break
        above = index - 1
        while above >= 0 and lines[above].strip().startswith("#"):
            block.insert(0, lines[above])
            above -= 1
        blocks.append(block)
    return blocks


@pytest.mark.parametrize("pack", PACKS, ids=[pack.stem for pack in PACKS])
@pytest.mark.parametrize("key", ["row_grain", "counting_guidance", "caveats"])
def test_every_pack_statement_names_its_basis(pack: Path, key: str) -> None:
    # A source pack states only what the agency documents or a dated live check shows.
    lines = pack.read_text().splitlines()
    blocks = _statement_blocks(lines, key)

    assert blocks, f"{pack.name} has no {key} statements"
    for block in blocks:
        text = "\n".join(block)
        assert "# official" in text or "# checked" in text, f"{pack.name}: no basis for\n{text}"
