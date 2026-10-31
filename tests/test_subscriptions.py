"""Static MCP catalogs must not hold an idle subscription open."""

import anyio
import httpx
import pytest


async def verify_static_transport(server, headers=None):
    lowlevel = server._lowlevel_server
    for version in ("2025-06-18", "2025-11-25", "2026-07-28"):
        capabilities = lowlevel.get_capabilities(protocol_version=version)
        assert not capabilities.tools.list_changed
        assert not capabilities.resources.subscribe
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        for method in ("tools/list", "subscriptions/listen"):
            params = {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            }
            if method == "subscriptions/listen":
                params["notifications"] = {"toolsListChanged": True}
            with anyio.fail_after(2):
                response = await client.post(
                    "/mcp",
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2026-07-28",
                        "Mcp-Method": method,
                        **(headers or {}),
                    },
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
            assert response.headers["content-type"].startswith("application/json")
            if method == "tools/list":
                assert response.status_code == 200 and response.json()["result"]["tools"]
            else:
                assert response.status_code == 404
                assert response.json()["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_static_catalog_has_no_idle_subscription(local_config_path, tmp_path):
    from test_mcp_server import open_settings

    from opencivicquery.mcp_server import create_server

    await verify_static_transport(create_server(open_settings(local_config_path, tmp_path)))


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [None, "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"])
@pytest.mark.parametrize(
    "accept", ["text/event-stream", "*/*", "application/json, text/event-stream"]
)
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/", "/%6dcp"])
async def test_http_get_does_not_open_an_idle_stream(
    local_config_path, tmp_path, protocol, accept, resume, path
):
    from test_mcp_server import open_settings

    from opencivicquery.mcp_server import create_server

    server = create_server(open_settings(local_config_path, tmp_path))
    app = server.streamable_http_app(json_response=True, stateless_http=True)
    headers = {"Accept": accept}
    if protocol:
        headers["MCP-Protocol-Version"] = protocol
    if resume:
        headers["Last-Event-ID"] = "previous-event"
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
            follow_redirects=True,
        ) as client,
    ):
        with anyio.fail_after(1):
            response = await client.get(path, headers=headers)

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [None, "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"])
async def test_post_handshake_list_and_call_still_work(local_config_path, tmp_path, protocol):
    from test_mcp_server import EXPECTED_TOOLS, open_settings

    from opencivicquery.mcp_server import create_server

    server = create_server(open_settings(local_config_path, tmp_path))
    app = server.streamable_http_app(json_response=True, stateless_http=True)
    headers = {"Accept": "application/json, text/event-stream"}
    if protocol:
        headers["MCP-Protocol-Version"] = protocol
    modern = protocol == "2026-07-28"
    metadata = (
        {
            "io.modelcontextprotocol/protocolVersion": protocol,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        if modern
        else None
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client,
    ):

        async def request(method, params):
            if metadata:
                params = {**params, "_meta": metadata}
            request_headers = {**headers, "Mcp-Method": method}
            if method == "tools/call":
                request_headers["Mcp-Name"] = params["name"]
            with anyio.fail_after(2):
                response = await client.post(
                    "/mcp",
                    headers=request_headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/json")
            return response.json()["result"]

        if modern:
            initialized = await request("server/discover", {})
        else:
            initialized = await request(
                "initialize",
                {
                    "protocolVersion": protocol or "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "transport-regression", "version": "1.0"},
                },
            )
        assert "row grain" in initialized["instructions"]
        tools = await request("tools/list", {})
        assert {tool["name"] for tool in tools["tools"]} == EXPECTED_TOOLS
        called = await request("tools/call", {"name": "list_source_packs", "arguments": {}})
        assert not called.get("isError", False)
        assert called["structuredContent"]["packs"]
        assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_idle_get_guard_follows_a_custom_mounted_path(local_config_path, tmp_path):
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from test_mcp_server import open_settings

    from opencivicquery.mcp_server import create_server

    server = create_server(open_settings(local_config_path, tmp_path))
    app = server.streamable_http_app(
        streamable_http_path="/rpc", json_response=True, stateless_http=True
    )
    mounted = Starlette(routes=[Mount("/tools", app=app)])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mounted),
            base_url="http://127.0.0.1:8000",
            follow_redirects=True,
        ) as client,
    ):
        with anyio.fail_after(1):
            response = await client.get("/tools/rpc/", headers={"Accept": "text/event-stream"})
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"
        assert (await client.get("/tools/health")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,status", [(None, 401), ("invalid", 401), ("limited", 403), ("valid", 200)]
)
async def test_idle_get_guard_preserves_post_authentication(
    monkeypatch, oauth_config_path, tmp_path, token, status
):
    from mcp.server.auth.provider import AccessToken
    from test_mcp_server import oauth_settings

    from opencivicquery.mcp_server import create_server

    server = create_server(oauth_settings(oauth_config_path, tmp_path))

    async def verify(value):
        if value == "invalid":
            return None
        return AccessToken(
            token=value,
            client_id="test-client",
            scopes=["openid", "email"] if value == "valid" else [],
            subject="test-user",
            claims={"email": "collaborator@example.com"},
        )

    monkeypatch.setattr(server._token_verifier, "verify_token", verify)
    app = server.streamable_http_app(json_response=True, stateless_http=True)
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client,
    ):
        with anyio.fail_after(2):
            assert (await client.get("/mcp", headers=headers)).status_code == 405
            assert (await client.head("/mcp", headers=headers)).status_code == 405
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "auth-regression", "version": "1.0"},
                    },
                },
            )
        assert response.status_code == status
        if status == 401:
            assert "resource_metadata=" in response.headers["www-authenticate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["OPTIONS", "DELETE"])
async def test_other_http_methods_keep_sdk_behavior(local_config_path, tmp_path, method):
    from mcp.server import MCPServer
    from test_mcp_server import open_settings

    from opencivicquery.mcp_server import create_server

    responses = []
    for guarded in (False, True):
        server = create_server(open_settings(local_config_path, tmp_path))
        if guarded:
            app = server.streamable_http_app(json_response=True, stateless_http=True)
        else:
            app = MCPServer.streamable_http_app(server, json_response=True, stateless_http=True)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
            ) as client,
        ):
            with anyio.fail_after(1):
                response = await client.request(method, "/mcp")
            responses.append((response.status_code, response.headers.get("allow")))
    assert responses[0] == responses[1]


@pytest.mark.asyncio
async def test_idle_get_guard_works_with_a_proxy_root_path(local_config_path, tmp_path):
    from test_mcp_server import open_settings

    from opencivicquery.mcp_server import create_server

    server = create_server(open_settings(local_config_path, tmp_path))
    app = server.streamable_http_app(json_response=True, stateless_http=True)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, root_path="/proxy"),
            base_url="http://127.0.0.1:8000",
        ) as client,
    ):
        with anyio.fail_after(1):
            response = await client.get("/proxy/mcp", headers={"Accept": "text/event-stream"})
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"
