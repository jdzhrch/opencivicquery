from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from mcp import Client
from starlette.testclient import TestClient

from opencivicquery import mcp_server
from opencivicquery.audit import AuditLogger
from opencivicquery.config import ConfigurationError, RuntimeSettings
from opencivicquery.mcp_server import create_server

OAUTH_ISSUER = "https://opencivicquery.example.org"
EXPECTED_TOOLS = {
    "list_source_packs",
    "search_datasets",
    "describe_dataset",
    "query_dataset",
    "pin_snapshot",
    "get_snapshot",
}


def open_settings(config_path: Path, tmp_path: Path, **overrides) -> RuntimeSettings:
    return RuntimeSettings(
        config_path=config_path,
        snapshot_backend="local",
        snapshot_path=tmp_path,
        **overrides,
    )


def oauth_settings(config_path: Path, tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        config_path=config_path,
        auth_mode="oauth",
        snapshot_backend="local",
        snapshot_path=tmp_path,
        oauth_issuer_url=OAUTH_ISSUER,
        oauth_resource_url=f"{OAUTH_ISSUER}/mcp",
        oauth_client_id="client-123.apps.googleusercontent.com",
        oauth_additional_client_ids=("client-456.apps.googleusercontent.com",),
        oauth_redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
    )


def test_mcp_tools_expose_source_identity_and_safety_fields(
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    server = create_server(open_settings(local_config_path, tmp_path))
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    assert set(tools) == EXPECTED_TOOLS
    query_schema = tools["query_dataset"].parameters
    assert {"domain", "dataset_id", "select"}.issubset(query_schema["required"])
    assert "source_id" not in query_schema["properties"]
    assert {"domain", "dataset_id"}.issubset(tools["describe_dataset"].parameters["required"])
    # Search spans every portal unless a domain narrows it.
    search_schema = tools["search_datasets"].parameters
    assert "domain" in search_schema["properties"]
    assert "domain" not in search_schema["required"]
    # Required, not optional: an optional field is the one a model quietly stops filling,
    # and the reporter's own wording is the part of the record nothing else reconstructs.
    for name in ("search_datasets", "describe_dataset", "query_dataset"):
        assert "reporter_question" in tools[name].parameters["required"], name
    for name in ("list_source_packs", "pin_snapshot", "get_snapshot"):
        assert "reporter_question" not in tools[name].parameters["properties"], name
    for name in ("pin_snapshot", "get_snapshot"):
        uri_schema = tools[name].parameters["properties"]["snapshot_uri"]
        assert uri_schema["maxLength"] == 512, name
        assert uri_schema["minLength"] == 1, name
    assert "completeness" in tools["query_dataset"].description
    assert "query_url" in tools["query_dataset"].description
    assert "presentation_rules" in tools["query_dataset"].description
    assert "ROW GRAIN UNKNOWN" in tools["list_source_packs"].description
    assert "row grain" in server.instructions
    assert "list_source_packs" in server.instructions
    assert "presentation_rules" in server.instructions
    assert "query_url" in server.instructions
    assert "specific claim" in server.instructions
    assert "Publication decisions belong" in server.instructions
    assert "review_status" not in server.instructions
    assert "completeness value" in server.instructions
    assert "will not open the tool output" in server.instructions


def test_open_server_has_no_protocol_authentication(
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    server = create_server(open_settings(local_config_path, tmp_path))

    assert server.settings.auth is None
    assert server._token_verifier is None


def test_oauth_server_relies_on_the_audience_check_not_resource_validation(
    oauth_config_path: Path,
    tmp_path: Path,
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        server = create_server(oauth_settings(oauth_config_path, tmp_path))

    assert server.settings.auth is not None
    assert server.settings.auth.validate_token_resource is False
    assert server._token_verifier is not None


def test_open_server_never_imports_the_oauth_code(local_config_path: Path, tmp_path: Path) -> None:
    # A fresh interpreter, because this test session imports the OAuth modules elsewhere.
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from opencivicquery.config import RuntimeSettings\n"
        "from opencivicquery.mcp_server import create_server\n"
        "create_server(RuntimeSettings(config_path=Path(sys.argv[1]), "
        "snapshot_path=Path(sys.argv[2])))\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('opencivicquery.oauth'))\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(local_config_path), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == ""


def test_stdio_transport_writes_audit_lines_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    # stdout carries the MCP protocol under stdio, so one audit line there corrupts it.
    audit_logger = logging.getLogger("opencivicquery.audit")
    monkeypatch.setattr(audit_logger, "handlers", [])

    mcp_server._build_service(open_settings(local_config_path, tmp_path, transport="stdio"))

    [handler] = audit_logger.handlers
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr


def test_audit_logger_replaces_earlier_handlers_so_the_last_stream_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_logger = logging.getLogger("opencivicquery.audit")
    monkeypatch.setattr(audit_logger, "handlers", [])
    first, second = io.StringIO(), io.StringIO()

    AuditLogger(stream=first)
    latest = AuditLogger(stream=second)
    latest.record("search_datasets", status="success")

    assert len(audit_logger.handlers) == 1
    assert first.getvalue() == ""
    [line] = second.getvalue().splitlines()
    assert json.loads(line)["action"] == "search_datasets"


def test_main_binds_to_the_configured_host(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    runs: list[dict[str, object]] = []

    class FakeServer:
        def run(self, **kwargs: object) -> None:
            runs.append(kwargs)

    monkeypatch.setattr(mcp_server, "create_server", lambda settings: FakeServer())
    monkeypatch.setattr(mcp_server, "install_log_redaction", lambda: None)
    monkeypatch.setenv("OPENCIVICQUERY_CONFIG_PATH", str(local_config_path))
    monkeypatch.setenv("OPENCIVICQUERY_SNAPSHOT_PATH", str(tmp_path))

    mcp_server.main()
    monkeypatch.setenv("OPENCIVICQUERY_HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9090")
    mcp_server.main()
    monkeypatch.setenv("OPENCIVICQUERY_TRANSPORT", "stdio")
    mcp_server.main()

    assert runs[0]["host"] == "127.0.0.1"
    assert runs[0]["transport"] == "streamable-http"
    assert runs[1]["host"] == "0.0.0.0"
    assert runs[1]["port"] == 9090
    assert runs[2] == {"transport": "stdio"}


def test_http_transport_writes_audit_lines_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    audit_logger = logging.getLogger("opencivicquery.audit")
    monkeypatch.setattr(audit_logger, "handlers", [])

    mcp_server._build_service(open_settings(local_config_path, tmp_path))

    [handler] = audit_logger.handlers
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout


def test_service_wiring_passes_the_portal_allowlist_and_token(
    monkeypatch: pytest.MonkeyPatch,
    oauth_config_path: Path,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_network(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(mcp_server, "SocrataNetwork", fake_network)
    service = mcp_server._build_service(oauth_settings(oauth_config_path, tmp_path))

    assert captured == {"app_token": "test-token", "portals": ["data.sf.gov"]}
    assert service.auth_mode == "oauth"
    assert service.bundle.config.deployment_id == "example-newsroom"
    assert service.bundle.config.access is not None
    assert service.bundle.config.access.allowed_users == ["collaborator@example.com"]


def test_open_server_refuses_a_configuration_with_an_access_list(
    oauth_config_path: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="login is off"):
        mcp_server._build_service(open_settings(oauth_config_path, tmp_path))


def test_service_wiring_leaves_any_portal_unrestricted(
    monkeypatch: pytest.MonkeyPatch,
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_network(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(mcp_server, "SocrataNetwork", fake_network)
    mcp_server._build_service(open_settings(local_config_path, tmp_path))

    assert captured == {"app_token": None, "portals": None}


def test_oauth_server_publishes_a_reachable_discovery_chain(
    oauth_config_path: Path,
    tmp_path: Path,
) -> None:
    server = create_server(oauth_settings(oauth_config_path, tmp_path))
    app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)

    with TestClient(app) as client:
        unauthenticated = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        # The 401 is what makes Claude start an OAuth flow. A 200 carrying a tool
        # error would be read as a normal result and no sign-in would ever appear.
        assert unauthenticated.status_code == 401
        challenge = unauthenticated.headers["www-authenticate"]
        assert 'resource_metadata="' in challenge

        metadata_url = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
        resource_metadata = client.get(urlparse(metadata_url).path).json()
        # RFC 9728: this must match the URL a reporter types into Claude, path included.
        assert resource_metadata["resource"] == f"{OAUTH_ISSUER}/mcp"
        assert resource_metadata["authorization_servers"] == [OAUTH_ISSUER]

        authorization_metadata = client.get("/.well-known/oauth-authorization-server").json()
        assert "offline_access" in authorization_metadata["scopes_supported"]
        assert authorization_metadata["authorization_response_iss_parameter_supported"] is True


def test_oauth_proxy_routes_are_reachable_without_a_token(
    oauth_config_path: Path,
    tmp_path: Path,
) -> None:
    server = create_server(oauth_settings(oauth_config_path, tmp_path))
    app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)

    with TestClient(app) as client:
        # A client cannot hold a token before walking through these endpoints, so
        # authentication must not guard them.
        assert client.get("/.well-known/oauth-authorization-server").status_code == 200
        assert client.post("/oauth/callback", data={}).status_code == 400
        redirect = client.get(
            "/oauth/authorize",
            params={
                "client_id": "client-123.apps.googleusercontent.com",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "state": "state-123",
            },
            follow_redirects=False,
        )
        assert redirect.status_code == 302
        assert redirect.headers["location"].startswith("https://accounts.google.com/")
        # The access list names an outside collaborator, so a hosted-domain hint would
        # hide the account chooser from that person.
        assert "hd" not in parse_qs(urlparse(redirect.headers["location"]).query)
        additional_redirect = client.get(
            "/oauth/authorize",
            params={
                "client_id": "client-456.apps.googleusercontent.com",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "state": "state-456",
            },
            follow_redirects=False,
        )
        assert additional_redirect.status_code == 302
        assert client.get("/health").status_code == 200


def test_oauth_server_sends_a_hosted_domain_hint_only_for_domain_only_access(
    write_deployment: Callable[..., Path],
    tmp_path: Path,
) -> None:
    config_path = write_deployment(access={"allowed_email_domains": ["example.org"]})
    server = create_server(oauth_settings(config_path, tmp_path))
    app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)

    with TestClient(app) as client:
        redirect = client.get(
            "/oauth/authorize",
            params={
                "client_id": "client-123.apps.googleusercontent.com",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "state": "state-123",
            },
            follow_redirects=False,
        )

    assert parse_qs(urlparse(redirect.headers["location"]).query)["hd"] == ["example.org"]


def test_oauth_server_refuses_a_configuration_without_an_access_list(
    local_config_path: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="no access list"):
        create_server(oauth_settings(local_config_path, tmp_path))


def test_oauth_server_cannot_start_without_required_values(
    monkeypatch: pytest.MonkeyPatch,
    oauth_config_path: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENCIVICQUERY_CONFIG_PATH", str(oauth_config_path))
    monkeypatch.setenv("OPENCIVICQUERY_AUTH_MODE", "oauth")
    monkeypatch.setenv("OPENCIVICQUERY_SNAPSHOT_BACKEND", "local")
    monkeypatch.setenv("OPENCIVICQUERY_SNAPSHOT_PATH", str(tmp_path))

    with pytest.raises(ConfigurationError):
        RuntimeSettings.from_environment()


@pytest.mark.asyncio
async def test_mcp_protocol_lists_all_tools(local_config_path: Path, tmp_path: Path) -> None:
    server = create_server(open_settings(local_config_path, tmp_path))

    async with Client(server) as client:
        result = await client.list_tools()

    assert {tool.name for tool in result.tools} == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_list_source_packs_needs_no_network(local_config_path: Path, tmp_path: Path) -> None:
    server = create_server(open_settings(local_config_path, tmp_path))

    async with Client(server, mode="legacy") as client:
        result = await client.call_tool("list_source_packs", {})

    assert result.is_error is False
    packs = result.structured_content["packs"]
    assert [pack["domain"] for pack in packs] == ["data.sf.gov", "data.cityofnewyork.us"]
    assert packs[0]["checked_on"] == "2026-08-26"
    assert {"dataset_id": "5cei-gny5", "name": "Eviction Notices"} in packs[0]["datasets"]
