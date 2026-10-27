"""Build the ChatGPT workspace plugin package from the shared skill.

ChatGPT binds a plugin to a workspace app (the MCP connection), and the app ID is
specific to each workspace. Create the app first, then pass its ID here:

    uv run python scripts/build_chatgpt_plugin.py --app-id asdk_app_0123abcd...

The skill text comes from plugins/opencivicquery/skills/ask-open-data, the same source
the Claude plugin uses, and the version comes from the Claude plugin manifest, so the
two packages never drift apart.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_PLUGIN = ROOT / "plugins" / "opencivicquery"
SKILL_NAME = "ask-open-data"
SKILL_SOURCE = CLAUDE_PLUGIN / "skills" / SKILL_NAME
CHATGPT_SOURCE = ROOT / "chatgpt"
APP_ID = re.compile(r"^asdk_app_[0-9a-f]{16,64}$")

DISPLAY_NAME = "OpenCivicQuery"
DEVELOPER_NAME = "Rongcheng Zhang"
SHORT_DESCRIPTION = "Ask questions of government open data, with evidence a reporter can cite."
LONG_DESCRIPTION = (
    "Search, describe, and query government open data on every Socrata portal. Every "
    "answer carries the counting unit, completeness, the exact query, a public link that "
    "reruns it, and an evidence snapshot."
)


def plugin_version() -> str:
    manifest = json.loads((CLAUDE_PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    return str(manifest["version"])


def manifests(app_id: str, version: str) -> tuple[dict, dict, dict]:
    name = f"dev-{app_id.removeprefix('asdk_app_')}"
    interface = {
        "capabilities": [],
        "category": "Other",
        "defaultPrompt": None,
        "developerName": DEVELOPER_NAME,
        "displayName": DISPLAY_NAME,
        "longDescription": LONG_DESCRIPTION,
        "shortDescription": SHORT_DESCRIPTION,
    }
    plugin = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": name,
        "version": version,
        "description": SHORT_DESCRIPTION,
        "author": {"name": DEVELOPER_NAME},
        "extensions": {"com.openai": {"apps": "./.app.json", "interface": interface}},
    }
    codex = {
        "apps": "./.app.json",
        "author": {"name": DEVELOPER_NAME},
        "description": SHORT_DESCRIPTION,
        "interface": interface,
        "name": name,
        "version": version,
    }
    apps = {"apps": {name: {"id": app_id}}}
    return plugin, codex, apps


def build(app_id: str, out_dir: Path) -> Path:
    if not APP_ID.fullmatch(app_id):
        raise SystemExit("--app-id must look like asdk_app_ followed by lowercase hex")
    version = plugin_version()
    plugin, codex, apps = manifests(app_id, version)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"opencivicquery-chatgpt-{version}.zip"

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        (stage / ".codex-plugin").mkdir()
        (stage / "plugin.json").write_text(json.dumps(plugin, indent=2) + "\n")
        (stage / ".codex-plugin" / "plugin.json").write_text(json.dumps(codex, indent=2) + "\n")
        (stage / ".app.json").write_text(json.dumps(apps, indent=2) + "\n")
        shutil.copytree(CHATGPT_SOURCE / "assets", stage / "assets")
        skill = stage / "skills" / SKILL_NAME
        shutil.copytree(SKILL_SOURCE, skill)
        (skill / "agents").mkdir()
        shutil.copy(CHATGPT_SOURCE / "openai.yaml", skill / "agents" / "openai.yaml")
        shutil.copytree(CHATGPT_SOURCE / "assets", skill / "assets")

        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(stage.rglob("*")):
                if path.is_file() and path.name != ".DS_Store":
                    bundle.write(path, path.relative_to(stage).as_posix())
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-id", required=True, help="Workspace app ID, asdk_app_...")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="Output folder")
    args = parser.parse_args()
    print(build(args.app_id, args.out))


if __name__ == "__main__":
    main()
