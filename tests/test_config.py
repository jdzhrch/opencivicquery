from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import NYC_PACK, SF_PACK
from pydantic import ValidationError

from opencivicquery.config import ConfigurationError, RuntimeSettings, load_deployment

OAUTH_VARIABLES = (
    "OPENCIVICQUERY_OAUTH_ISSUER_URL",
    "OPENCIVICQUERY_OAUTH_RESOURCE_URL",
    "OPENCIVICQUERY_OAUTH_CLIENT_ID",
    "OPENCIVICQUERY_OAUTH_ADDITIONAL_CLIENT_IDS",
    "OPENCIVICQUERY_OAUTH_REDIRECT_URIS",
)


def set_base_environment(
    monkeypatch: pytest.MonkeyPatch, config_path: Path, snapshot_path: Path
) -> None:
    monkeypatch.setenv("OPENCIVICQUERY_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("OPENCIVICQUERY_SNAPSHOT_PATH", str(snapshot_path))


def set_oauth_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCIVICQUERY_AUTH_MODE", "oauth")
    monkeypatch.setenv("OPENCIVICQUERY_OAUTH_ISSUER_URL", "https://opencivicquery.example")
    monkeypatch.setenv("OPENCIVICQUERY_OAUTH_RESOURCE_URL", "https://opencivicquery.example/mcp")
    monkeypatch.setenv("OPENCIVICQUERY_OAUTH_CLIENT_ID", "client.apps.googleusercontent.com")
    monkeypatch.setenv(
        "OPENCIVICQUERY_OAUTH_REDIRECT_URIS", "https://claude.ai/api/mcp/auth_callback"
    )


# Runtime settings ---------------------------------------------------------------------


def test_runtime_settings_require_a_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="OPENCIVICQUERY_CONFIG_PATH"):
        RuntimeSettings.from_environment()


def test_runtime_settings_default_to_open_mode_over_http(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    set_base_environment(monkeypatch, local_config_path, tmp_path)

    settings = RuntimeSettings.from_environment()

    assert settings.auth_mode == "open"
    assert settings.transport == "http"
    assert settings.snapshot_backend == "local"
    assert settings.config_path == local_config_path
    assert settings.oauth_client_ids == ()


def test_runtime_settings_default_local_snapshots_to_the_home_directory(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENCIVICQUERY_CONFIG_PATH", str(local_config_path))

    settings = RuntimeSettings.from_environment()

    assert settings.snapshot_backend == "local"
    assert settings.snapshot_path == tmp_path / ".opencivicquery" / "snapshots"


def test_runtime_settings_expand_a_home_relative_snapshot_path(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENCIVICQUERY_CONFIG_PATH", str(local_config_path))
    monkeypatch.setenv("OPENCIVICQUERY_SNAPSHOT_PATH", "~/evidence")

    assert RuntimeSettings.from_environment().snapshot_path == tmp_path / "evidence"


def test_runtime_settings_require_a_bucket_for_gcs(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
) -> None:
    monkeypatch.setenv("OPENCIVICQUERY_CONFIG_PATH", str(local_config_path))
    monkeypatch.setenv("OPENCIVICQUERY_SNAPSHOT_BACKEND", "gcs")

    with pytest.raises(ConfigurationError, match="OPENCIVICQUERY_SNAPSHOT_BUCKET"):
        RuntimeSettings.from_environment()

    monkeypatch.setenv("OPENCIVICQUERY_SNAPSHOT_BUCKET", "evidence-bucket")
    settings = RuntimeSettings.from_environment()
    assert settings.snapshot_bucket == "evidence-bucket"
    assert settings.snapshot_path is None


def test_runtime_settings_bind_to_loopback_unless_told_otherwise(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    set_base_environment(monkeypatch, local_config_path, tmp_path)

    assert RuntimeSettings.from_environment().host == "127.0.0.1"

    monkeypatch.setenv("OPENCIVICQUERY_HOST", "0.0.0.0")
    assert RuntimeSettings.from_environment().host == "0.0.0.0"


def test_public_deployment_caps_each_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCIVICQUERY_SOCRATA_APP_TOKEN", "test-token")
    public = SF_PACK.parents[1] / "deployments" / "public.yaml"

    limits = load_deployment(public).config.limits

    assert limits.per_client_daily is not None
    assert limits.per_client_daily < limits.daily_requests
    assert limits.client_address == "x-forwarded-for-last"


def test_public_deployment_treats_hosted_connectors_as_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencivicquery.client import identify

    monkeypatch.setenv("OPENCIVICQUERY_SOCRATA_APP_TOKEN", "test-token")
    bundle = load_deployment(SF_PACK.parents[1] / "deployments" / "public.yaml")

    # ChatGPT connector addresses seen in production logs, and Anthropic's outbound range.
    for address in ("20.98.18.80", "20.98.18.94", "134.138.57.84", "160.79.105.3"):
        assert identify(address, bundle.shared_networks).capped is False
    assert identify("8.8.8.8", bundle.shared_networks).capped is True


def test_per_client_cap_requires_an_explicit_address_source(
    write_deployment: Callable[..., Path],
) -> None:
    path = write_deployment(
        limits={"daily_requests": 10, "per_portal_per_minute": 10, "per_client_daily": 5}
    )

    with pytest.raises(ConfigurationError):
        load_deployment(path)


def test_invalid_shared_networks_are_refused(write_deployment: Callable[..., Path]) -> None:
    path = write_deployment(
        limits={"daily_requests": 10, "per_portal_per_minute": 10, "shared_networks": ["x"]}
    )

    with pytest.raises(ConfigurationError):
        load_deployment(path)


def test_a_broken_shared_networks_file_is_refused(
    write_deployment: Callable[..., Path], tmp_path: Path
) -> None:
    (tmp_path / "bad.json").write_text("{}")
    path = write_deployment(
        limits={
            "daily_requests": 10,
            "per_portal_per_minute": 10,
            "shared_networks_files": [str(tmp_path / "bad.json")],
        }
    )

    with pytest.raises(ConfigurationError, match="shared networks"):
        load_deployment(path)


def test_container_image_binds_to_every_interface() -> None:
    dockerfile = (SF_PACK.parents[1] / "Dockerfile").read_text()

    assert "OPENCIVICQUERY_HOST=0.0.0.0" in dockerfile


@pytest.mark.parametrize("mode", ["anonymous", "authless-test", ""])
def test_runtime_settings_refuse_an_unknown_auth_mode(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
    mode: str,
) -> None:
    # A typo must fail closed, never fall back to open mode.
    set_base_environment(monkeypatch, local_config_path, tmp_path)
    monkeypatch.setenv("OPENCIVICQUERY_AUTH_MODE", mode)

    with pytest.raises(ValidationError):
        RuntimeSettings.from_environment()


def test_runtime_settings_accept_stdio_in_open_mode(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    set_base_environment(monkeypatch, local_config_path, tmp_path)
    monkeypatch.setenv("OPENCIVICQUERY_TRANSPORT", "stdio")

    assert RuntimeSettings.from_environment().transport == "stdio"


def test_runtime_settings_refuse_stdio_with_login(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    set_base_environment(monkeypatch, local_config_path, tmp_path)
    set_oauth_environment(monkeypatch)
    monkeypatch.setenv("OPENCIVICQUERY_TRANSPORT", "stdio")

    with pytest.raises(ConfigurationError, match="stdio"):
        RuntimeSettings.from_environment()


def test_runtime_settings_fail_closed_when_oauth_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    oauth_config_path: Path,
    tmp_path: Path,
) -> None:
    set_base_environment(monkeypatch, oauth_config_path, tmp_path)
    monkeypatch.setenv("OPENCIVICQUERY_AUTH_MODE", "oauth")
    for name in OAUTH_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ConfigurationError, match="Missing required OAuth"):
        RuntimeSettings.from_environment()


def test_runtime_settings_accept_open_mode_without_oauth(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    set_base_environment(monkeypatch, local_config_path, tmp_path)
    monkeypatch.setenv("OPENCIVICQUERY_AUTH_MODE", "open")
    for name in OAUTH_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    settings = RuntimeSettings.from_environment()

    assert settings.auth_mode == "open"


def test_runtime_settings_parse_oauth_redirect_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    oauth_config_path: Path,
    tmp_path: Path,
) -> None:
    set_base_environment(monkeypatch, oauth_config_path, tmp_path)
    set_oauth_environment(monkeypatch)
    monkeypatch.setenv(
        "OPENCIVICQUERY_OAUTH_ADDITIONAL_CLIENT_IDS",
        "old-client.apps.googleusercontent.com, client.apps.googleusercontent.com",
    )
    monkeypatch.setenv(
        "OPENCIVICQUERY_OAUTH_REDIRECT_URIS",
        "https://claude.ai/api/mcp/auth_callback, https://claude.com/api/mcp/auth_callback",
    )

    settings = RuntimeSettings.from_environment()

    assert settings.oauth_redirect_uris == (
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    )
    assert settings.oauth_client_ids == (
        "client.apps.googleusercontent.com",
        "old-client.apps.googleusercontent.com",
    )


# Deployment files shipped with the repository -------------------------------------------


def test_local_deployment_loads_without_any_secret(local_config_path: Path) -> None:
    bundle = load_deployment(local_config_path)

    assert bundle.config.deployment_id == "local"
    assert bundle.config.socrata.portals == "any"
    assert bundle.socrata_app_token is None
    assert [pack.domain for pack in bundle.packs] == ["data.sf.gov", "data.cityofnewyork.us"]
    assert len(bundle.config_sha256) == 64


def test_public_deployment_requires_its_named_app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    path = SF_PACK.parents[1] / "deployments" / "public.yaml"

    with pytest.raises(ConfigurationError, match="OPENCIVICQUERY_SOCRATA_APP_TOKEN"):
        load_deployment(path)

    monkeypatch.setenv("OPENCIVICQUERY_SOCRATA_APP_TOKEN", "  public-token  ")
    bundle = load_deployment(path)

    assert bundle.config.deployment_id == "public"
    assert bundle.socrata_app_token is not None
    assert bundle.socrata_app_token.get_secret_value() == "public-token"
    assert "public-token" not in repr(bundle)


def test_login_example_merges_the_external_allowed_users(oauth_config_path: Path) -> None:
    bundle = load_deployment(oauth_config_path, auth_mode="oauth")

    assert bundle.config.socrata.portals == ["data.sf.gov"]
    assert bundle.socrata_app_token is not None
    assert bundle.socrata_app_token.get_secret_value() == "test-token"
    assert bundle.config.access is not None
    assert bundle.config.access.allowed_users == ["collaborator@example.com"]
    assert bundle.config.access.allowed_users_env is None
    assert bundle.config.access.allowed_email_domains == ["example.org"]


def test_login_example_requires_the_external_allowed_user_secret(
    monkeypatch: pytest.MonkeyPatch,
    oauth_config_path: Path,
) -> None:
    monkeypatch.delenv("OPENCIVICQUERY_ALLOWED_USERS")

    with pytest.raises(ConfigurationError, match="Missing required allowed-user list"):
        load_deployment(oauth_config_path, auth_mode="oauth")


def test_open_mode_refuses_a_configuration_with_an_access_list(
    oauth_config_path: Path,
) -> None:
    # A file that names who may sign in was written for a server with login on.
    # Starting it without login would hand its token and storage to everyone.
    with pytest.raises(ConfigurationError, match="login is off"):
        load_deployment(oauth_config_path, auth_mode="open")
    with pytest.raises(ConfigurationError, match="login is off"):
        load_deployment(oauth_config_path)


def test_open_mode_refusal_names_the_fix(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
) -> None:
    path = write_deployment(access={"allowed_email_domains": ["example.org"]})

    with pytest.raises(ConfigurationError, match="OPENCIVICQUERY_AUTH_MODE=oauth"):
        load_deployment(path, auth_mode="open")
    assert load_deployment(path, auth_mode="oauth").config.access is not None


# Deployment rules -------------------------------------------------------------------------


def test_no_app_token_is_allowed_with_a_warning(
    write_deployment: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = write_deployment()

    with caplog.at_level(logging.WARNING, logger="opencivicquery.config"):
        bundle = load_deployment(path)

    assert bundle.socrata_app_token is None
    assert any("No Socrata app token" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_named_app_token_variable_must_hold_a_value(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    value: str | None,
) -> None:
    path = write_deployment(socrata={"portals": "any", "app_token_env": "TEST_SOCRATA_TOKEN"})
    if value is None:
        monkeypatch.delenv("TEST_SOCRATA_TOKEN", raising=False)
    else:
        monkeypatch.setenv("TEST_SOCRATA_TOKEN", value)

    with pytest.raises(ConfigurationError, match="Missing required Socrata app token"):
        load_deployment(path)


def test_two_packs_must_not_describe_the_same_portal(
    write_deployment: Callable[..., Path],
    write_pack: Callable[..., Path],
) -> None:
    duplicate = write_pack("another-sf", domain="DATA.SF.GOV")
    path = write_deployment(packs=[str(SF_PACK), str(duplicate)])

    with pytest.raises(ConfigurationError, match="same portal domain"):
        load_deployment(path)


def test_packs_must_sit_inside_a_portal_allowlist(write_deployment: Callable[..., Path]) -> None:
    path = write_deployment(
        socrata={"portals": ["data.sf.gov"]},
        packs=[str(SF_PACK), str(NYC_PACK)],
    )

    with pytest.raises(ConfigurationError, match="data.cityofnewyork.us"):
        load_deployment(path)


def test_packs_inside_a_portal_allowlist_load(write_deployment: Callable[..., Path]) -> None:
    path = write_deployment(
        socrata={"portals": ["Data.SF.gov", "data.cityofnewyork.us"]},
        packs=[str(SF_PACK)],
    )

    bundle = load_deployment(path)

    assert bundle.config.socrata.portals == ["data.sf.gov", "data.cityofnewyork.us"]


def test_relative_pack_paths_resolve_against_the_deployment_file(
    write_deployment: Callable[..., Path],
    write_pack: Callable[..., Path],
) -> None:
    write_pack("relative-city", domain="data.example.gov")
    path = write_deployment(packs=["../packs/relative-city.yaml"])

    bundle = load_deployment(path)

    assert [pack.domain for pack in bundle.packs] == ["data.example.gov"]


def test_login_requires_an_access_list(write_deployment: Callable[..., Path]) -> None:
    path = write_deployment()

    assert load_deployment(path, auth_mode="open").config.access is None
    with pytest.raises(ConfigurationError, match="no access list"):
        load_deployment(path, auth_mode="oauth")


def test_login_rejects_a_malformed_external_user_list(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
) -> None:
    path = write_deployment(
        access={
            "allowed_users": ["named@example.com"],
            "allowed_users_env": "TEST_ALLOWED_USERS",
        }
    )

    monkeypatch.setenv("TEST_ALLOWED_USERS", "first@example.com,,second@example.com")
    with pytest.raises(ConfigurationError, match="Invalid allowed-user list"):
        load_deployment(path, auth_mode="oauth")

    monkeypatch.setenv("TEST_ALLOWED_USERS", "not-an-address")
    with pytest.raises(ConfigurationError, match="Invalid allowed-user list"):
        load_deployment(path, auth_mode="oauth")

    monkeypatch.setenv("TEST_ALLOWED_USERS", "Named@Example.com")
    with pytest.raises(ConfigurationError, match="Invalid allowed-user list"):
        load_deployment(path, auth_mode="oauth")

    monkeypatch.setenv("TEST_ALLOWED_USERS", " Second@Example.com ")
    access = load_deployment(path, auth_mode="oauth").config.access
    assert access is not None
    assert access.allowed_users == ["named@example.com", "second@example.com"]


def test_tenant_era_keys_are_rejected(write_deployment: Callable[..., Path]) -> None:
    path = write_deployment(tenant_id="old-tenant")

    with pytest.raises(ConfigurationError, match="Invalid deployment configuration"):
        load_deployment(path)


def test_invalid_portal_names_are_rejected(write_deployment: Callable[..., Path]) -> None:
    path = write_deployment(socrata={"portals": ["https://data.sf.gov"]}, packs=[])

    with pytest.raises(ConfigurationError, match="Invalid deployment configuration"):
        load_deployment(path)


def test_unreadable_or_invalid_files_raise_configuration_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Cannot read"):
        load_deployment(tmp_path / "missing.yaml")

    broken = tmp_path / "broken.yaml"
    broken.write_text("deployment_id: [unterminated")
    with pytest.raises(ConfigurationError, match="Invalid YAML"):
        load_deployment(broken)


def test_invalid_pack_names_the_pack(
    write_deployment: Callable[..., Path],
    tmp_path: Path,
) -> None:
    pack = tmp_path / "bad-pack.yaml"
    pack.write_text("pack_id: Bad Pack\nname: x\nplatform: socrata\n")
    path = write_deployment(packs=[str(pack)])

    with pytest.raises(ConfigurationError, match="Invalid source pack"):
        load_deployment(path)


def test_config_hash_covers_every_loaded_pack(
    write_deployment: Callable[..., Path],
    write_pack: Callable[..., Path],
) -> None:
    pack = write_pack("hashed-city", domain="data.example.gov")
    path = write_deployment(packs=[str(pack)])
    before = load_deployment(path).config_sha256

    pack.write_text(pack.read_text().replace("2026-10-01", "2026-10-02"))
    after = load_deployment(path).config_sha256

    assert before != after
