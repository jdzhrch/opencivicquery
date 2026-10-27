from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client
from starlette.testclient import TestClient

from opencivicquery import mcp_server
from opencivicquery.audit import AuditLogger
from opencivicquery.config import RuntimeSettings
from opencivicquery.limits import UsageLimiter
from opencivicquery.models import QueryResponse
from opencivicquery.snapshots import FileSnapshotStore, SnapshotError
from opencivicquery.sources.socrata import SocrataError, SocrataNetwork

CATALOG_HOST = "api.us.socrata.com"
COMMON_ARGUMENTS = {
    "domain": "data.sf.gov",
    "reporter_question": "OpenCivicQuery error reproduction test",
    "max_rows": 1,
}
GEOMETRY_SELECT = "geoid20, aland20, simplify_preserve_topology(the_geom, 0.00005) as geom"
SOURCE_COLUMN_ERROR = (
    "Query coordinator error: query.soql.no-such-column; No such column: the_geom; "
    'position: Map(row -> 1, column -> 57, line -> "SELECT geoid20, aland20, '
    'simplify_preserve_topology(the_geom, 0.00005) as geom LIMIT 2")'
)
FAILURE_CASES = [
    pytest.param(
        COMMON_ARGUMENTS
        | {
            "dataset_id": "e2st-aufe",
            "select": "count(*) as n",
            "group_by": "tractce20;",
        },
        {
            "code": "INVALID_QUERY_ARGUMENT",
            "message": "group_by contains a forbidden token: ';'",
            "argument": "group_by",
            "http_status": None,
        },
        id="forbidden-grouping-token",
    ),
    pytest.param(
        COMMON_ARGUMENTS
        | {
            "dataset_id": "e2st-aufe",
            "select": "count(*) as n",
            "where": "tractce20 &lt; '030000'",
        },
        {
            "code": "INVALID_QUERY_ARGUMENT",
            "message": "where contains an HTML-escaped operator: &lt;",
            "argument": "where",
            "http_status": None,
        },
        id="escaped-comparison",
    ),
    pytest.param(
        COMMON_ARGUMENTS | {"dataset_id": "p2fw-hsrv", "select": GEOMETRY_SELECT},
        {
            "code": "UNKNOWN_COLUMN",
            "message": "No such column: the_geom.",
            "argument": None,
            "http_status": 400,
        },
        id="missing-geometry-column",
    ),
]


def portal_requests(requests: list[httpx.Request]) -> list[httpx.Request]:
    """Requests that reached a data portal, as opposed to Socrata's shared catalog."""
    return [request for request in requests if request.url.host != CATALOG_HOST]


def build_test_server(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    resource_response: Callable[[], httpx.Response] | None = None,
    settings_overrides: dict[str, Any] | None = None,
    **deployment: Any,
) -> tuple[Any, httpx.AsyncClient, list[httpx.Request], str]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == CATALOG_HOST:
            return httpx.Response(200, json={"results": [{"metadata": {"domain": "data.sf.gov"}}]})
        if request.url.path.startswith("/api/views/"):
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[-1],
                    "name": "Census Tracts",
                    "domainCName": "data.sf.gov",
                    "rowsUpdatedAt": 1_700_000_000,
                    "columns": [
                        {"name": field, "fieldName": field, "dataTypeName": kind}
                        for field, kind in (
                            ("tractce20", "text"),
                            ("geoid20", "text"),
                            ("aland20", "number"),
                            ("multipolygon", "multipolygon"),
                        )
                    ],
                },
            )
        if resource_response is not None:
            return resource_response()
        if "the_geom" in request.url.params["$query"]:
            return httpx.Response(400, json={"message": SOURCE_COLUMN_ERROR})
        return httpx.Response(200, json=[{"n": "42"}])

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("TEST_SOCRATA_TOKEN", "test-token")
    deployment.setdefault("socrata", {"portals": "any", "app_token_env": "TEST_SOCRATA_TOKEN"})
    config_path = write_deployment(**deployment)
    monkeypatch.setattr(
        mcp_server,
        "SocrataNetwork",
        lambda **kwargs: SocrataNetwork(**kwargs, client=http_client, max_attempts=1),
    )
    logger_name = f"test-mcp-query-{tmp_path.name}"
    monkeypatch.setattr(
        mcp_server,
        "AuditLogger",
        lambda stream=None: AuditLogger(logging.getLogger(logger_name)),
    )
    settings = RuntimeSettings(
        config_path=config_path,
        snapshot_backend="local",
        snapshot_path=tmp_path / "snapshots",
        **(settings_overrides or {}),
    )
    return mcp_server.create_server(settings), http_client, requests, logger_name


def audit_events(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[dict[str, Any]]:
    return [
        json.loads(record.getMessage()) for record in caplog.records if record.name == logger_name
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("arguments", "expected_error"), FAILURE_CASES)
async def test_exact_reproduction_calls_return_reasons_over_mcp(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    arguments: dict[str, Any],
    expected_error: dict[str, Any],
) -> None:
    server, http_client, requests, logger_name = build_test_server(
        monkeypatch, write_deployment, tmp_path
    )
    caplog.set_level(logging.INFO, logger=logger_name)
    try:
        # Legacy mode serializes the request and response through JSON-RPC, like an MCP client.
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("query_dataset", arguments)
    finally:
        await http_client.aclose()

    assert result.is_error is True
    assert result.structured_content == {"error": expected_error}
    assert len(result.content) == 1
    assert json.loads(result.content[0].text) == result.structured_content
    assert set(expected_error) == {"code", "message", "argument", "http_status"}
    assert not list((tmp_path / "snapshots").rglob("*.json"))
    if expected_error["http_status"] is None:
        # Unsafe SoQL is refused before any request, including the catalog lookup.
        assert requests == []
        return
    assert [request.url.host for request in requests] == [
        CATALOG_HOST,
        "data.sf.gov",
        "data.sf.gov",
    ]
    assert [request.url.path for request in portal_requests(requests)] == [
        "/api/views/p2fw-hsrv",
        "/resource/p2fw-hsrv.json",
    ]
    assert portal_requests(requests)[1].url.params["$query"] == f"SELECT {GEOMETRY_SELECT} LIMIT 2"
    events = audit_events(caplog, logger_name)
    assert len(events) == 1
    event = events[0]
    assert event["action"] == "query_dataset"
    assert event["status"] == "error"
    assert event["actor_id"] == "anonymous"
    assert event["deployment_id"] == "test-deployment"
    assert event["connection_id"] is None
    assert event["reporter_question"] == arguments["reporter_question"]
    assert event["domain"] == arguments["domain"]
    assert event["dataset_id"] == arguments["dataset_id"]
    for field in ("select", "where", "group_by", "order_by", "max_rows"):
        assert event[f"requested_{field}"] == arguments.get(field)


@pytest.mark.asyncio
async def test_successful_mcp_query_keeps_payload_and_citable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, http_client, requests, logger_name = build_test_server(
        monkeypatch, write_deployment, tmp_path
    )
    caplog.set_level(logging.INFO, logger=logger_name)
    arguments = COMMON_ARGUMENTS | {
        "domain": "Data.SFgov.org",
        "dataset_id": "e2st-aufe",
        "select": "count(*) as n",
        "where": "tractce20 < '030000'",
    }
    try:
        async with Client(server, mode="legacy") as client:
            tool = next(
                tool for tool in (await client.list_tools()).tools if tool.name == "query_dataset"
            )
            result = await client.call_tool("query_dataset", arguments)
    finally:
        await http_client.aclose()

    assert tool.output_schema["type"] == "object"
    assert result.is_error is False
    assert json.loads(result.content[0].text) == result.structured_content
    payload = result.structured_content
    assert set(payload) == set(QueryResponse.model_fields)
    assert payload["domain"] == "data.sf.gov"
    assert payload["rows"] == [{"n": "42"}]
    assert payload["completeness"] == "complete"
    assert payload["returned_row_count"] == 1
    assert payload["presentation_rules"]
    assert payload["query"] == "SELECT count(*) as n WHERE tractce20 < '030000' LIMIT 2"
    assert httpx.URL(payload["query_url"]).params["$query"] == payload["query"]
    snapshot_path = Path(payload["snapshot"]["uri"].removeprefix("file://"))
    frozen = json.loads(snapshot_path.read_text())
    assert frozen["rows"] == payload["rows"]
    assert frozen["query"] == payload["query"]
    assert frozen["deployment_id"] == "test-deployment"
    assert frozen["actor_id"] == "anonymous"
    # One catalog lookup for the alias, then metadata and rows from the canonical portal.
    assert [request.url.host for request in requests] == [
        CATALOG_HOST,
        "data.sf.gov",
        "data.sf.gov",
    ]
    assert all(request.headers["X-App-Token"] == "test-token" for request in requests)
    assert len(list((tmp_path / "snapshots").rglob("*.json"))) == 1
    assert [event["status"] for event in audit_events(caplog, logger_name)] == ["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, "SOURCE_ACCESS_DENIED"),
        (403, "SOURCE_ACCESS_DENIED"),
        (404, "DATASET_NOT_FOUND"),
        (503, "SOURCE_UNAVAILABLE"),
    ],
)
async def test_source_http_failures_return_safe_status_reason(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    status: int,
    expected_code: str,
) -> None:
    server, http_client, requests, _ = build_test_server(
        monkeypatch,
        write_deployment,
        tmp_path,
        resource_response=lambda: httpx.Response(
            status,
            json={"message": "private source detail: test-token access_token=private-oauth-value"},
        ),
    )
    try:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "query_dataset",
                COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"},
            )
    finally:
        await http_client.aclose()

    assert result.is_error is True
    error = result.structured_content["error"]
    assert error["code"] == expected_code
    assert error["http_status"] == status
    assert f"HTTP {status}" in error["message"]
    public = json.dumps(result.model_dump(mode="json"))
    for private in ("private source detail", "test-token", "private-oauth-value"):
        assert private not in public
    assert len(portal_requests(requests)) == 2
    assert not list((tmp_path / "snapshots").rglob("*.json"))


@pytest.mark.asyncio
async def test_source_query_reason_redacts_credentials_and_limits_size(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, http_client, _, logger_name = build_test_server(
        monkeypatch,
        write_deployment,
        tmp_path,
        resource_response=lambda: httpx.Response(
            400,
            json={
                "message": "Invalid comparison. test-token access_token=private-oauth-value "
                + "x" * 1000
            },
        ),
    )
    caplog.set_level(logging.INFO, logger=logger_name)
    try:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "query_dataset",
                COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"},
            )
    finally:
        await http_client.aclose()

    assert result.is_error is True
    error = result.structured_content["error"]
    assert error["code"] == "QUERY_REJECTED"
    assert error["message"].startswith("Invalid comparison.")
    assert len(error["message"]) == 500
    public_and_audit = json.dumps(result.model_dump(mode="json")) + caplog.text
    assert "test-token" not in public_and_audit
    assert "private-oauth-value" not in public_and_audit
    assert "[REDACTED]" in error["message"]
    assert len(audit_events(caplog, logger_name)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source_message", ["", "   "])
async def test_blank_source_reason_retains_a_nonempty_status_reason(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    source_message: str,
) -> None:
    server, http_client, _, _ = build_test_server(
        monkeypatch,
        write_deployment,
        tmp_path,
        resource_response=lambda: httpx.Response(400, json={"message": source_message}),
    )
    try:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "query_dataset",
                COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"},
            )
    finally:
        await http_client.aclose()

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "QUERY_REJECTED"
    assert result.structured_content["error"]["http_status"] == 400
    assert result.structured_content["error"]["message"] == (
        "Socrata rejected the request (HTTP 400)."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("exception_type", [SocrataError, RuntimeError])
async def test_other_source_failures_do_not_disclose_internal_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    exception_type: type[Exception],
) -> None:
    server, http_client, requests, _ = build_test_server(monkeypatch, write_deployment, tmp_path)

    async def fail_query(self: SocrataNetwork, request: Any) -> Any:
        raise exception_type("private storage /private/deployment.json token=private-value")

    monkeypatch.setattr(SocrataNetwork, "query", fail_query)
    try:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "query_dataset",
                COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"},
            )
    finally:
        await http_client.aclose()

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == (
        "SOURCE_ERROR" if exception_type is SocrataError else "INTERNAL_ERROR"
    )
    public = json.dumps(result.model_dump(mode="json"))
    assert "private storage" not in public
    assert "private-value" not in public
    assert portal_requests(requests) == []
    assert not list((tmp_path / "snapshots").rglob("*.json"))


@pytest.mark.asyncio
async def test_snapshot_failure_returns_reason_without_data_or_private_path(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
) -> None:
    server, http_client, _, _ = build_test_server(monkeypatch, write_deployment, tmp_path)

    async def fail_snapshot(self: FileSnapshotStore, snapshot: Any) -> Any:
        raise SnapshotError("private path /private/deployment-secret.json")

    monkeypatch.setattr(FileSnapshotStore, "put", fail_snapshot)
    try:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "query_dataset",
                COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"},
            )
    finally:
        await http_client.aclose()

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "SNAPSHOT_ERROR"
    assert set(result.structured_content) == {"error"}
    assert "deployment-secret" not in json.dumps(result.model_dump(mode="json"))
    assert not list((tmp_path / "snapshots").rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "calls", "expected_code", "expected_requests"),
    [
        ({"daily_requests": 1, "per_portal_per_minute": 10}, 2, "DAILY_LIMIT_REACHED", 3),
        # The first call confirms the portal through the catalog bucket, the second uses
        # the portal's own bucket, and the third finds that bucket full.
        ({"daily_requests": 10, "per_portal_per_minute": 1}, 3, "PORTAL_RATE_LIMITED", 5),
    ],
)
async def test_usage_limit_becomes_a_tool_error_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    limits: dict[str, int],
    calls: int,
    expected_code: str,
    expected_requests: int,
) -> None:
    server, http_client, requests, logger_name = build_test_server(
        monkeypatch, write_deployment, tmp_path, limits=limits
    )
    caplog.set_level(logging.INFO, logger=logger_name)
    arguments = COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"}
    try:
        async with Client(server, mode="legacy") as client:
            results = [await client.call_tool("query_dataset", arguments) for _ in range(calls)]
    finally:
        await http_client.aclose()

    *admitted, refused = results
    assert all(result.is_error is False for result in admitted)
    assert refused.is_error is True
    error = refused.structured_content["error"]
    assert error["code"] == expected_code
    assert error["argument"] is None
    assert error["http_status"] is None
    assert error["message"]
    assert json.loads(refused.content[0].text) == refused.structured_content
    # The refused call sent nothing upstream and left no snapshot.
    assert len(requests) == expected_requests
    assert len(list((tmp_path / "snapshots").rglob("*.json"))) == calls - 1
    events = audit_events(caplog, logger_name)
    assert [event["status"] for event in events] == ["success"] * (calls - 1) + ["refused"]
    assert events[-1]["error_type"] == expected_code


@pytest.mark.asyncio
async def test_usage_limit_on_search_and_describe_becomes_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
) -> None:
    server, http_client, requests, _ = build_test_server(monkeypatch, write_deployment, tmp_path)
    monkeypatch.setattr(
        mcp_server,
        "UsageLimiter",
        lambda limits: UsageLimiter(limits.model_copy(update={"daily_requests": 1})),
    )
    question = {"reporter_question": "Which datasets cover evictions?"}
    try:
        async with Client(server, mode="legacy") as client:
            await client.call_tool("search_datasets", {"search_term": "eviction"} | question)
            search = await client.call_tool("search_datasets", {"search_term": "x"} | question)
            describe = await client.call_tool(
                "describe_dataset",
                {"domain": "data.sf.gov", "dataset_id": "e2st-aufe"} | question,
            )
    finally:
        await http_client.aclose()

    for result in (search, describe):
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "DAILY_LIMIT_REACHED"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain",
    ["127.0.0.1", "localhost", "data.sf.gov:8443", "https://data.sf.gov", "[::1]"],
)
async def test_query_validation_error_becomes_an_invalid_argument_tool_error(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    domain: str,
) -> None:
    server, http_client, requests, logger_name = build_test_server(
        monkeypatch, write_deployment, tmp_path
    )
    caplog.set_level(logging.INFO, logger=logger_name)
    try:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(
                "query_dataset",
                COMMON_ARGUMENTS
                | {"domain": domain, "dataset_id": "e2st-aufe", "select": "count(*) as n"},
            )
    finally:
        await http_client.aclose()

    assert result.is_error is True
    error = result.structured_content["error"]
    assert error["code"] == "INVALID_QUERY_ARGUMENT"
    assert error["argument"] == "domain"
    assert error["http_status"] is None
    assert "bare public hostname" in error["message"]
    assert requests == []
    assert not list((tmp_path / "snapshots").rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["describe_dataset", "search_datasets"])
async def test_invalid_domain_on_describe_and_search_never_leaves_the_server(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    tool: str,
) -> None:
    server, http_client, requests, _ = build_test_server(monkeypatch, write_deployment, tmp_path)
    arguments: dict[str, Any] = {
        "domain": "169.254.169.254",
        "reporter_question": "Probe",
    }
    if tool == "describe_dataset":
        arguments["dataset_id"] = "e2st-aufe"
    else:
        arguments["search_term"] = "anything"
    try:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(tool, arguments)
    finally:
        await http_client.aclose()

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "INVALID_QUERY_ARGUMENT"
    assert result.structured_content["error"]["argument"] == "domain"
    assert requests == []


@pytest.mark.asyncio
async def test_unknown_and_disallowed_portals_return_their_codes(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
) -> None:
    server, http_client, requests, _ = build_test_server(
        monkeypatch,
        write_deployment,
        tmp_path,
        socrata={"portals": ["data.cityofnewyork.us"], "app_token_env": "TEST_SOCRATA_TOKEN"},
        packs=[],
    )
    try:
        async with Client(server, mode="legacy") as client:
            disallowed = await client.call_tool(
                "query_dataset",
                COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"},
            )
            demo = await client.call_tool(
                "describe_dataset",
                {
                    "domain": "sample.demo.socrata.com",
                    "dataset_id": "e2st-aufe",
                    "reporter_question": "Demo",
                },
            )
    finally:
        await http_client.aclose()

    assert disallowed.structured_content["error"]["code"] == "PORTAL_NOT_ALLOWED"
    assert disallowed.structured_content["error"]["argument"] == "domain"
    assert demo.structured_content["error"]["code"] == "UNKNOWN_PORTAL"
    # The catalog answered once. Neither portal saw a request.
    assert [request.url.host for request in requests] == [CATALOG_HOST]


@pytest.mark.asyncio
async def test_http_transport_serializes_explicit_query_error(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
) -> None:
    server, http_client, requests, _ = build_test_server(monkeypatch, write_deployment, tmp_path)
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True
    )
    arguments = COMMON_ARGUMENTS | {
        "dataset_id": "e2st-aufe",
        "select": "count(*) as n",
        "group_by": "tractce20;",
    }
    try:
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            response = client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "query_dataset", "arguments": arguments},
                },
            )
    finally:
        await http_client.aclose()

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["message"] == (
        "group_by contains a forbidden token: ';'"
    )
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert requests == []


@pytest.mark.asyncio
async def test_chained_query_bypass_is_refused_over_mcp_without_any_request(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
) -> None:
    server, http_client, requests, _ = build_test_server(monkeypatch, write_deployment, tmp_path)
    arguments = COMMON_ARGUMENTS | {
        "domain": "data.cityofnewyork.us",
        "dataset_id": "43nn-pn8j",
        "select": "camis",
        "where": (
            "boro = \"a'\" OR camis IS NOT NULL LIMIT 3 |> SELECT count(*) AS n WHERE '1'='1'"
        ),
    }
    try:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool("query_dataset", arguments)
    finally:
        await http_client.aclose()

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "INVALID_QUERY_ARGUMENT"
    assert result.structured_content["error"]["argument"] == "where"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["search_datasets", "describe_dataset", "query_dataset"])
async def test_a_hanging_source_returns_a_timeout_tool_error(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    tool: str,
) -> None:
    import asyncio

    from opencivicquery import service as service_module

    server, http_client, _, _ = build_test_server(monkeypatch, write_deployment, tmp_path)

    async def hang(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(SocrataNetwork, "resolve_portal", hang)
    monkeypatch.setattr(SocrataNetwork, "search", hang)
    monkeypatch.setattr(service_module, "UPSTREAM_DEADLINE_SECONDS", 0.05)
    arguments: dict[str, Any] = {"reporter_question": "Timeout reproduction"}
    if tool == "search_datasets":
        arguments["search_term"] = "eviction"
    elif tool == "describe_dataset":
        arguments |= {"domain": "data.sf.gov", "dataset_id": "e2st-aufe"}
    else:
        arguments |= COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"}
    try:
        async with Client(server, mode="legacy") as client:
            result = await asyncio.wait_for(client.call_tool(tool, arguments), timeout=10)
    finally:
        await http_client.aclose()

    assert result.is_error is True
    error = result.structured_content["error"]
    assert error["code"] == "SOURCE_TIMEOUT"
    assert "did not answer in time" in error["message"]
    assert not list((tmp_path / "snapshots").rglob("*.json"))


async def first_snapshot_uri(client: Client) -> str:
    result = await client.call_tool(
        "query_dataset",
        COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"},
    )
    assert result.is_error is False
    return result.structured_content["snapshot"]["uri"]


@pytest.mark.asyncio
async def test_snapshot_tools_return_structured_results(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
) -> None:
    server, http_client, _, _ = build_test_server(monkeypatch, write_deployment, tmp_path)
    try:
        async with Client(server, mode="legacy") as client:
            uri = await first_snapshot_uri(client)
            fetched = await client.call_tool("get_snapshot", {"snapshot_uri": uri})
            pinned = await client.call_tool("pin_snapshot", {"snapshot_uri": uri})
    finally:
        await http_client.aclose()

    assert fetched.is_error is False
    assert fetched.structured_content["deployment_id"] == "test-deployment"
    assert fetched.structured_content["rows"] == [{"n": "42"}]
    assert json.loads(fetched.content[0].text) == fetched.structured_content
    assert pinned.is_error is False
    assert pinned.structured_content["retention"] == "pinned"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_snapshot", "pin_snapshot"])
async def test_snapshot_errors_become_tool_errors_without_private_paths(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    tool: str,
) -> None:
    server, http_client, _, _ = build_test_server(monkeypatch, write_deployment, tmp_path)
    foreign = (tmp_path / "private-area" / "temporary" / "other" / "x.json").as_uri()
    try:
        async with Client(server, mode="legacy") as client:
            results = [
                await client.call_tool(tool, {"snapshot_uri": uri})
                for uri in (foreign, "gs://bucket/temporary/test-deployment/x.json")
            ]
    finally:
        await http_client.aclose()

    for result in results:
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "SNAPSHOT_ERROR"
        assert "private-area" not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_snapshot", "pin_snapshot"])
async def test_snapshot_uri_longer_than_512_characters_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    tool: str,
) -> None:
    server, http_client, _, _ = build_test_server(monkeypatch, write_deployment, tmp_path)
    try:
        async with Client(server, mode="legacy") as client:
            result = await client.call_tool(tool, {"snapshot_uri": "file:///" + "a" * 520})
    finally:
        await http_client.aclose()

    # The SDK refuses the argument before the tool runs, so no store lookup happens.
    assert result.is_error is True
    assert result.structured_content is None
    assert "at most 512 characters" in result.content[0].text


@pytest.mark.asyncio
async def test_snapshot_rate_limit_becomes_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, http_client, _, logger_name = build_test_server(
        monkeypatch,
        write_deployment,
        tmp_path,
        limits={"daily_requests": 100, "per_portal_per_minute": 1},
    )
    caplog.set_level(logging.INFO, logger=logger_name)
    try:
        async with Client(server, mode="legacy") as client:
            uri = await first_snapshot_uri(client)
            await client.call_tool("get_snapshot", {"snapshot_uri": uri})
            refused = await client.call_tool("pin_snapshot", {"snapshot_uri": uri})
    finally:
        await http_client.aclose()

    assert refused.is_error is True
    assert refused.structured_content["error"]["code"] == "PORTAL_RATE_LIMITED"
    last = audit_events(caplog, logger_name)[-1]
    assert (last["action"], last["status"], last["portal"]) == (
        "pin_snapshot",
        "refused",
        "snapshots",
    )


def _post_query(client: TestClient, arguments: dict[str, Any], forwarded_for: str) -> dict:
    response = client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            "X-Forwarded-For": forwarded_for,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "query_dataset", "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]


@pytest.mark.asyncio
async def test_per_client_cap_uses_the_last_forwarded_address_and_resists_spoofing(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, http_client, _, logger_name = build_test_server(
        monkeypatch,
        write_deployment,
        tmp_path,
        limits={
            "daily_requests": 100,
            "per_portal_per_minute": 100,
            "per_client_daily": 2,
            "client_address": "x-forwarded-for-last",
        },
    )
    caplog.set_level(logging.INFO, logger=logger_name)
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True
    )
    arguments = COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"}
    try:
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            # The proxy appends the real address last; whatever the caller sends in
            # front of it must not create a fresh allowance.
            first = _post_query(client, arguments, "8.8.8.8")
            second = _post_query(client, arguments, "1.1.1.1, 8.8.8.8")
            third = _post_query(client, arguments, "9.9.9.9,8.8.8.8")
            other_client = _post_query(client, arguments, "8.8.4.4")
    finally:
        await http_client.aclose()

    assert first["isError"] is False
    assert second["isError"] is False
    assert third["isError"] is True
    assert third["structuredContent"]["error"]["code"] == "CLIENT_DAILY_LIMIT_REACHED"
    assert other_client["isError"] is False
    events = audit_events(caplog, logger_name)
    hashes = [event["client_hash"] for event in events]
    assert hashes[0] == hashes[1] == hashes[2] != hashes[3]
    assert all("8.8.8.8" not in json.dumps(event) for event in events)
    assert all(event["client_capped"] is True for event in events)


@pytest.mark.asyncio
async def test_ipv6_slash_64_is_one_client_and_shared_egress_is_not_capped(
    monkeypatch: pytest.MonkeyPatch,
    write_deployment: Callable[..., Path],
    tmp_path: Path,
) -> None:
    server, http_client, _, _ = build_test_server(
        monkeypatch,
        write_deployment,
        tmp_path,
        limits={
            "daily_requests": 100,
            "per_portal_per_minute": 100,
            "per_client_daily": 1,
            "client_address": "x-forwarded-for-last",
            "shared_networks": ["20.98.18.80/28"],
        },
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True
    )
    arguments = COMMON_ARGUMENTS | {"dataset_id": "e2st-aufe", "select": "count(*) as n"}
    try:
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            first = _post_query(client, arguments, "2606:4700:4700::1111")
            rotated = _post_query(client, arguments, "2606:4700:4700:0:ffff::9")
            connector_calls = [_post_query(client, arguments, "20.98.18.85") for _ in range(3)]
            proxyless = [_post_query(client, arguments, "0.0.0.0") for _ in range(3)]
    finally:
        await http_client.aclose()

    assert first["isError"] is False
    assert rotated["structuredContent"]["error"]["code"] == "CLIENT_DAILY_LIMIT_REACHED"
    # Many users share a hosted connector's egress, so it meets only the global caps.
    assert all(result["isError"] is False for result in connector_calls)
    assert all(result["isError"] is False for result in proxyless)
