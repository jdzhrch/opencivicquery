from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from opencivicquery.models import AccessConfig, DeploymentBundle, DeploymentConfig, SourcePack

logger = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    """A required OpenCivicQuery configuration value is missing or invalid."""


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_path: Path
    auth_mode: Literal["open", "oauth"] = "open"
    """Login is off by default. 'oauth' turns on Google sign-in and the access list."""
    transport: Literal["http", "stdio"] = "http"
    host: str = "127.0.0.1"
    """HTTP bind address. Containers set 0.0.0.0; a laptop stays on loopback."""
    snapshot_backend: Literal["local", "gcs"] = "local"
    snapshot_path: Path | None = None
    snapshot_bucket: str | None = None
    oauth_issuer_url: str | None = None
    oauth_resource_url: str | None = None
    oauth_client_id: str | None = None
    oauth_additional_client_ids: tuple[str, ...] = ()
    oauth_redirect_uris: tuple[str, ...] = ()
    log_level: str = "INFO"
    port: int = Field(default=8080, ge=1, le=65535)

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        config_path = os.getenv("OPENCIVICQUERY_CONFIG_PATH")
        if not config_path:
            raise ConfigurationError(
                "Missing required environment variable: OPENCIVICQUERY_CONFIG_PATH"
            )

        def csv(name: str) -> tuple[str, ...]:
            return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())

        settings = cls(
            config_path=Path(config_path),
            auth_mode=os.getenv("OPENCIVICQUERY_AUTH_MODE", "open"),
            transport=os.getenv("OPENCIVICQUERY_TRANSPORT", "http"),
            host=os.getenv("OPENCIVICQUERY_HOST", "127.0.0.1"),
            snapshot_backend=os.getenv("OPENCIVICQUERY_SNAPSHOT_BACKEND", "local"),
            snapshot_path=(
                Path(os.environ["OPENCIVICQUERY_SNAPSHOT_PATH"]).expanduser()
                if os.getenv("OPENCIVICQUERY_SNAPSHOT_PATH")
                else None
            ),
            snapshot_bucket=os.getenv("OPENCIVICQUERY_SNAPSHOT_BUCKET"),
            oauth_issuer_url=os.getenv("OPENCIVICQUERY_OAUTH_ISSUER_URL"),
            oauth_resource_url=os.getenv("OPENCIVICQUERY_OAUTH_RESOURCE_URL"),
            oauth_client_id=os.getenv("OPENCIVICQUERY_OAUTH_CLIENT_ID"),
            oauth_additional_client_ids=csv("OPENCIVICQUERY_OAUTH_ADDITIONAL_CLIENT_IDS"),
            oauth_redirect_uris=csv("OPENCIVICQUERY_OAUTH_REDIRECT_URIS"),
            log_level=os.getenv("OPENCIVICQUERY_LOG_LEVEL", "INFO"),
            port=int(os.getenv("PORT", "8080")),
        )
        if settings.snapshot_backend == "local" and settings.snapshot_path is None:
            settings = settings.model_copy(
                update={"snapshot_path": Path.home() / ".opencivicquery" / "snapshots"}
            )
        if settings.snapshot_backend == "gcs" and not settings.snapshot_bucket:
            raise ConfigurationError("OPENCIVICQUERY_SNAPSHOT_BUCKET is required for GCS snapshots")
        if settings.transport == "stdio" and settings.auth_mode != "open":
            raise ConfigurationError("The stdio transport runs locally and supports only open mode")
        if settings.auth_mode == "oauth":
            oauth_fields = {
                "OPENCIVICQUERY_OAUTH_ISSUER_URL": settings.oauth_issuer_url,
                "OPENCIVICQUERY_OAUTH_RESOURCE_URL": settings.oauth_resource_url,
                "OPENCIVICQUERY_OAUTH_CLIENT_ID": settings.oauth_client_id,
                "OPENCIVICQUERY_OAUTH_REDIRECT_URIS": settings.oauth_redirect_uris,
            }
            missing = [name for name, value in oauth_fields.items() if not value]
            if missing:
                raise ConfigurationError(
                    f"Missing required OAuth environment variables: {', '.join(missing)}"
                )
        return settings

    @property
    def oauth_client_ids(self) -> tuple[str, ...]:
        primary = (self.oauth_client_id or "").strip()
        client_ids = (primary, *(item.strip() for item in self.oauth_additional_client_ids))
        return tuple(dict.fromkeys(client_id for client_id in client_ids if client_id))


def _read_yaml(path: Path, label: str) -> tuple[bytes, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"Cannot read {label}: {path}") from exc
    try:
        return raw, yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {label}: {path}") from exc


def _resolve_access(access: AccessConfig) -> AccessConfig:
    """Merge addresses kept outside the repository into the access list."""
    if not access.allowed_users_env:
        return access
    raw_users = os.getenv(access.allowed_users_env)
    if not raw_users or not raw_users.strip():
        raise ConfigurationError(f"Missing required allowed-user list: {access.allowed_users_env}")
    users = [user.strip() for user in raw_users.split(",")]
    if any(not user for user in users):
        raise ConfigurationError(f"Invalid allowed-user list: {access.allowed_users_env}")
    try:
        return AccessConfig.model_validate(
            {
                **access.model_dump(),
                "allowed_users": [*access.allowed_users, *users],
                "allowed_users_env": None,
            }
        )
    except ValueError as exc:
        raise ConfigurationError(f"Invalid allowed-user list: {access.allowed_users_env}") from exc


def load_deployment(
    path: Path, *, auth_mode: Literal["open", "oauth"] = "open"
) -> DeploymentBundle:
    raw_config, parsed = _read_yaml(path, "deployment configuration")
    try:
        config = DeploymentConfig.model_validate(parsed)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid deployment configuration: {path}") from exc

    digest = hashlib.sha256(raw_config)
    packs: list[SourcePack] = []
    for relative in config.packs:
        pack_path = (path.parent / relative).resolve()
        raw_pack, parsed_pack = _read_yaml(pack_path, "source pack")
        try:
            pack = SourcePack.model_validate(parsed_pack)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid source pack: {pack_path}") from exc
        digest.update(b"\0" + raw_pack)
        packs.append(pack)

    domains = [pack.domain for pack in packs]
    if len(domains) != len(set(domains)):
        raise ConfigurationError("Two source packs describe the same portal domain")
    portals = config.socrata.portals
    if portals != "any":
        outside = sorted(set(domains) - set(portals))
        if outside:
            raise ConfigurationError(
                f"Source packs describe portals outside socrata.portals: {', '.join(outside)}"
            )

    if auth_mode == "open" and config.access is not None:
        # Failing closed: a file that names who may sign in was written for a server
        # with login on. Starting it open would silently publish that newsroom's
        # token and storage to everyone.
        raise ConfigurationError(
            "This configuration lists who may sign in, but login is off. Set "
            "OPENCIVICQUERY_AUTH_MODE=oauth or remove the access section."
        )
    if auth_mode == "oauth":
        if config.access is None:
            raise ConfigurationError("Login is on but the configuration has no access list")
        config = config.model_copy(update={"access": _resolve_access(config.access)})

    token: SecretStr | None = None
    token_env = config.socrata.app_token_env
    if token_env:
        value = os.getenv(token_env)
        if not value or not value.strip():
            raise ConfigurationError(f"Missing required Socrata app token: {token_env}")
        token = SecretStr(value.strip())
    else:
        logger.warning(
            "No Socrata app token is configured. Requests share Socrata's unauthenticated pool."
        )

    return DeploymentBundle(
        config=config,
        packs=packs,
        socrata_app_token=token,
        config_sha256=digest.hexdigest(),
    )
