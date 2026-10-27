"""Container dependency pins must agree with the runtime lock."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "uv.lock"
PYPROJECT = ROOT / "pyproject.toml"

_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;\\]+)", re.MULTILINE)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_pins() -> dict[str, str]:
    return {
        canonical(m.group("name")): m.group("version")
        for m in _PIN_RE.finditer(REQUIREMENTS.read_text())
    }


def lock_versions() -> dict[str, str]:
    data = tomllib.loads(LOCK.read_text())
    return {canonical(p["name"]): p["version"] for p in data["package"] if "version" in p}


def declared_dependencies() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    names = set()
    for spec in data["project"]["dependencies"]:
        names.add(canonical(re.split(r"[<>=!~\[ ]", spec, maxsplit=1)[0]))
    return names


def test_requirements_file_is_pinned_and_hashed() -> None:
    text = REQUIREMENTS.read_text()
    pins = requirement_pins()
    assert pins, "requirements.txt has no pinned versions"
    assert "--hash=" in text, "requirements.txt must carry hashes; the image uses --require-hashes"
    # A range in this file would defeat the point of generating it from the lock.
    assert not re.search(r"^[A-Za-z0-9._-]+[<>~]=", text, re.MULTILINE)


def test_every_pin_matches_the_lock() -> None:
    versions = lock_versions()
    mismatched = {
        name: (pinned, versions.get(name))
        for name, pinned in requirement_pins().items()
        if versions.get(name) != pinned
    }
    assert not mismatched, (
        "requirements.txt disagrees with uv.lock; regenerate it with "
        f"`uv export --frozen --no-dev --no-emit-project --format requirements.txt "
        f"-o requirements.txt`. Differences (pinned, locked): {mismatched}"
    )


def test_every_declared_runtime_dependency_is_present() -> None:
    pins = requirement_pins()
    missing = sorted(name for name in declared_dependencies() if name not in pins)
    assert not missing, f"runtime dependencies absent from requirements.txt: {missing}"


def test_the_mcp_pin_is_explicit() -> None:
    # The regression that produced this file was an unpinned MCP SDK, so its
    # pin is asserted by name rather than only by the generic checks above.
    pins = requirement_pins()
    assert "mcp" in pins
    assert pins["mcp"] == lock_versions()["mcp"]
