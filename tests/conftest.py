from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from opencivicquery.config import load_deployment
from opencivicquery.models import DeploymentBundle

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENTS = ROOT / "deployments"
PACKS = ROOT / "packs"
SF_PACK = PACKS / "us-ca-san-francisco.yaml"
NYC_PACK = PACKS / "us-ny-new-york-city.yaml"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without any OpenCivicQuery setting inherited from the shell."""
    for name in list(os.environ):
        if name.startswith("OPENCIVICQUERY_"):
            monkeypatch.delenv(name)


@pytest.fixture
def local_config_path() -> Path:
    return DEPLOYMENTS / "local.yaml"


@pytest.fixture
def local_bundle(local_config_path: Path) -> DeploymentBundle:
    return load_deployment(local_config_path)


@pytest.fixture
def oauth_config_path(monkeypatch: pytest.MonkeyPatch) -> Path:
    """The login example, with its external address list and app token supplied by env."""
    monkeypatch.setenv("OPENCIVICQUERY_ALLOWED_USERS", "collaborator@example.com")
    monkeypatch.setenv("OPENCIVICQUERY_SOCRATA_APP_TOKEN", "test-token")
    return DEPLOYMENTS / "newsroom-with-login.example.yaml"


@pytest.fixture
def oauth_bundle(oauth_config_path: Path) -> DeploymentBundle:
    return load_deployment(oauth_config_path, auth_mode="oauth")


def deployment_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "deployment_id": "test-deployment",
        "display_name": "Test deployment",
        "socrata": {"portals": "any"},
        "limits": {"daily_requests": 100, "per_portal_per_minute": 100},
        "packs": [str(SF_PACK)],
    }
    document.update(overrides)
    return document


@pytest.fixture
def write_deployment(tmp_path: Path) -> Callable[..., Path]:
    """Write a deployment file under tmp_path. Pack paths may be absolute."""

    def write(name: str = "deployment.yaml", **overrides: Any) -> Path:
        path = tmp_path / "config" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(deployment_document(**overrides)))
        return path

    return write


@pytest.fixture
def write_pack(tmp_path: Path) -> Callable[..., Path]:
    """Write a minimal source pack under tmp_path and return its absolute path."""

    def write(name: str, *, domain: str) -> Path:
        path = tmp_path / "packs" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "pack_id": name,
                    "name": name,
                    "platform": "socrata",
                    "domain": domain,
                    "checked_on": "2026-10-01",
                    "datasets": [],
                }
            )
        )
        return path

    return write
