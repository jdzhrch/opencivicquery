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
