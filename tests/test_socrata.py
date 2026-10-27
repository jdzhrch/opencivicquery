from __future__ import annotations

import gzip
import json
from typing import Any

import httpx
import pytest
import respx

from opencivicquery.models import Completeness, QueryRequest, SourceQueryResult
from opencivicquery.sources.socrata import (
    SocrataError,
    SocrataNetwork,
    SocrataQueryError,
    build_soql,
)

CATALOG = SocrataNetwork.catalog_url


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


CATALOG_HOST = "api.us.socrata.com"

pytestmark = pytest.mark.respx(assert_all_called=False)


def metadata(domain: str = "data.sf.gov") -> dict[str, Any]:
    return {
        "id": "abcd-1234",
        "name": "Test Dataset",
        "domainCName": domain,
        "rowsUpdatedAt": 1_700_000_000,
        "columns": [
            {
                "name": "Value",
                "fieldName": "value",
                "dataTypeName": "number",
            }
        ],
    }


def catalog_hit(domain: str) -> dict[str, Any]:
    return {"results": [{"resource": {"id": "abcd-1234"}, "metadata": {"domain": domain}}]}


def network(**kwargs: Any) -> SocrataNetwork:
    kwargs.setdefault("app_token", "app-token")
    kwargs.setdefault("portals", None)
    kwargs.setdefault("max_attempts", 1)
    return SocrataNetwork(**kwargs)


def resolves_to(respx_mock: respx.MockRouter, canonical: str) -> respx.Route:
    """Route Socrata catalog lookups (limit=1) to a portal that the catalog confirms."""
    return respx_mock.get(CATALOG, params={"limit": "1"}).mock(
        return_value=httpx.Response(200, json=catalog_hit(canonical))
    )


# SoQL construction -----------------------------------------------------------------------


def test_build_soql_adds_sentinel_row() -> None:
    query = build_soql(
        QueryRequest(
            domain="data.sf.gov",
            dataset_id="abcd-1234",
            select="category, count(*) AS total",
            where="created_at >= '2026-01-01T00:00:00'",
            group_by="category",
            order_by="total DESC",
            max_rows=25,
        )
    )

    assert query.endswith("LIMIT 26")
    assert "GROUP BY category" in query


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("select", "*; DROP TABLE x"),
        ("where", "true -- hide limit"),
        ("group_by", "category LIMIT 50000"),
        ("order_by", "value /* comment */"),
        ("where", "a = 1 UNION SELECT *"),
        ("select", "value OFFSET 10"),
        ("where", "value &gt; 1"),
    ],
)
def test_build_soql_rejects_structural_injection(field: str, value: str) -> None:
    values = {
        "domain": "data.sf.gov",
        "dataset_id": "abcd-1234",
        "select": "*",
        field: value,
    }

    with pytest.raises(SocrataQueryError) as caught:
        build_soql(QueryRequest(**values))
    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == field


def test_build_soql_allows_keywords_inside_string_literals() -> None:
    query = build_soql(
        QueryRequest(
            domain="data.sf.gov",
            dataset_id="abcd-1234",
            select="*",
            where="description = 'select where limit'",
        )
    )

    assert "WHERE description = 'select where limit'" in query


@pytest.mark.parametrize("dataset_id", ["ABCD-1234", "abcd1234", "../views", "abcd-12345"])
def test_build_soql_rejects_malformed_dataset_ids(dataset_id: str) -> None:
    with pytest.raises(SocrataQueryError) as caught:
        build_soql(QueryRequest(domain="data.sf.gov", dataset_id=dataset_id, select="*"))
    assert caught.value.argument == "dataset_id"


# Portal resolution -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_portal_canonicalizes_an_alias_and_caches_it(
    respx_mock: respx.MockRouter,
) -> None:
    route = resolves_to(respx_mock, "data.sf.gov")
    source = network()

    assert await source.resolve_portal("Data.SFgov.org") == "data.sf.gov"
    assert await source.resolve_portal("data.sfgov.org") == "data.sf.gov"
    assert await source.resolve_portal("data.sf.gov") == "data.sf.gov"
    await source.close()

    # One catalog call answered the alias and, through the cache, the canonical name too.
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.url.host == CATALOG_HOST
    assert dict(request.url.params) == {
        "domains": "data.sfgov.org",
        "search_context": "data.sfgov.org",
        "limit": "1",
    }
    assert request.headers["X-App-Token"] == "app-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"results": []}),
        httpx.Response(400, json={"message": "bad domain"}),
        httpx.Response(404, json={"message": "not found"}),
    ],
)
async def test_resolve_portal_refuses_a_portal_the_catalog_does_not_list(
    respx_mock: respx.MockRouter,
    response: httpx.Response,
) -> None:
    route = respx_mock.get(CATALOG).mock(return_value=response)
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.resolve_portal("data.invented-city.gov")
    await source.close()

    assert caught.value.code == "UNKNOWN_PORTAL"
    assert caught.value.argument == "domain"
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"resultSetSize": 0}),
        httpx.Response(200, json={"error": "temporarily unavailable"}),
        httpx.Response(202, json={}),
    ],
)
async def test_an_unusable_catalog_answer_never_marks_a_real_portal_unknown(
    respx_mock: respx.MockRouter,
    response: httpx.Response,
) -> None:
    route = respx_mock.get(CATALOG).mock(return_value=response)
    source = network()

    for _ in range(2):
        with pytest.raises(SocrataError) as caught:
            await source.resolve_portal("data.sf.gov")
        assert not isinstance(caught.value, SocrataQueryError)
    await source.close()

    # Both calls reached the catalog: a bad answer is not cached as "unknown".
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_resolve_portal_caches_an_unknown_portal_for_an_hour(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(CATALOG).mock(return_value=httpx.Response(200, json={"results": []}))
    clock = FakeClock()
    source = network(clock=clock)

    for _ in range(3):
        with pytest.raises(SocrataQueryError) as caught:
            await source.resolve_portal("Data.Invented-City.gov")
        assert caught.value.code == "UNKNOWN_PORTAL"
    assert route.call_count == 1

    clock.now += 3599.9
    with pytest.raises(SocrataQueryError):
        await source.resolve_portal("data.invented-city.gov")
    assert route.call_count == 1

    # At expiry the next lookup asks the catalog again.
    clock.now = 1_000.0 + 3600
    with pytest.raises(SocrataQueryError):
        await source.resolve_portal("data.invented-city.gov")
    assert route.call_count == 2
    await source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404])
async def test_resolve_portal_caches_a_catalog_rejection(
    respx_mock: respx.MockRouter, status: int
) -> None:
    route = respx_mock.get(CATALOG).mock(return_value=httpx.Response(status, json={}))
    source = network()

    for _ in range(2):
        with pytest.raises(SocrataQueryError) as caught:
            await source.resolve_portal("data.invented-city.gov")
        assert caught.value.code == "UNKNOWN_PORTAL"
    await source.close()

    assert route.call_count == 1


@pytest.mark.asyncio
async def test_resolve_portal_does_not_cache_a_catalog_outage(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(CATALOG).mock(
        side_effect=[
            httpx.Response(503, json={"message": "down"}),
            httpx.Response(200, json=catalog_hit("data.sf.gov")),
        ]
    )
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.resolve_portal("data.sf.gov")
    assert caught.value.code == "SOURCE_UNAVAILABLE"
    # An outage is not evidence that the portal does not exist.
    assert await source.resolve_portal("data.sf.gov") == "data.sf.gov"
    await source.close()

    assert route.call_count == 2


@pytest.mark.asyncio
async def test_unknown_portal_cache_stays_bounded(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(CATALOG).mock(return_value=httpx.Response(200, json={"results": []}))
    source = network()
    source._max_cached_portals = 3

    for index in range(5):
        with pytest.raises(SocrataQueryError):
            await source.resolve_portal(f"data.invented-{index}.gov")
    await source.close()

    assert list(source._unknown) == [
        "data.invented-2.gov",
        "data.invented-3.gov",
        "data.invented-4.gov",
    ]


@pytest.mark.asyncio
async def test_resolve_portal_surfaces_a_catalog_outage_as_unavailable(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(CATALOG).mock(return_value=httpx.Response(503, json={"message": "down"}))
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.resolve_portal("data.sf.gov")
    await source.close()

    assert caught.value.code == "SOURCE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_resolve_portal_enforces_the_allowlist_on_the_canonical_domain(
    respx_mock: respx.MockRouter,
) -> None:
    route = resolves_to(respx_mock, "data.sf.gov")
    portal = respx_mock.route(host="data.sf.gov")
    source = network(portals=["data.cityofnewyork.us"])

    for _ in range(2):
        with pytest.raises(SocrataQueryError) as caught:
            await source.resolve_portal("data.sfgov.org")
        assert caught.value.code == "PORTAL_NOT_ALLOWED"
        assert caught.value.argument == "domain"
    await source.close()

    # The second refusal came from the cache, and the disallowed portal never saw a request.
    assert route.call_count == 1
    assert not portal.called


@pytest.mark.asyncio
async def test_resolve_portal_admits_an_alias_of_an_allowed_portal(
    respx_mock: respx.MockRouter,
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    source = network(portals=["data.sf.gov"])

    assert await source.resolve_portal("data.sfgov.org") == "data.sf.gov"
    await source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["sample.demo.socrata.com", "Other.Demo.Socrata.com"])
async def test_resolve_portal_refuses_demonstration_portals(
    respx_mock: respx.MockRouter, domain: str
) -> None:
    anything = respx_mock.route()
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.resolve_portal(domain)
    await source.close()

    assert caught.value.code == "UNKNOWN_PORTAL"
    assert not anything.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "[::1]",
        "localhost",
        "portal.localhost",
        "data.sf.gov:8443",
        "https://data.sf.gov",
        "user:pass@data.sf.gov",
        "data.sf.gov/resource",
        "",
    ],
)
async def test_resolve_portal_refuses_non_hostnames_without_any_request(
    respx_mock: respx.MockRouter, domain: str
) -> None:
    anything = respx_mock.route()
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.resolve_portal(domain)
    await source.close()

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == "domain"
    assert not anything.called


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", ["127.0.0.1", "", None, "localhost"])
async def test_resolve_portal_refuses_an_invalid_domain_from_the_catalog(
    respx_mock: respx.MockRouter,
    reported: str | None,
) -> None:
    respx_mock.get(CATALOG).mock(
        return_value=httpx.Response(200, json={"results": [{"metadata": {"domain": reported}}]})
    )
    elsewhere = respx_mock.route(host__regex=r"^(?!api\.us\.socrata\.com$).*")
    source = network()

    with pytest.raises(SocrataError, match="invalid portal domain"):
        await source.resolve_portal("data.sf.gov")
    await source.close()

    assert not elsewhere.called


# Search ------------------------------------------------------------------------------------


def search_results() -> dict[str, Any]:
    return {
        "results": [
            {
                "resource": {
                    "id": "5cei-gny5",
                    "name": "Eviction Notices",
                    "description": "Notices filed with the Rent Board",
                },
                "classification": {"domain_category": "Housing and Buildings"},
                "metadata": {"domain": "data.sf.gov"},
            },
            {
                "resource": {"id": "6z8x-wfk4", "name": "Evictions"},
                "metadata": {"domain": "data.cityofnewyork.us"},
            },
            {
                "resource": {"id": "abcd-5678", "name": "Sample evictions"},
                "metadata": {"domain": "sample.demo.socrata.com"},
            },
            {"resource": {"id": "not-an-id", "name": "Broken"}, "metadata": {"domain": "x.gov"}},
            {"resource": {"id": "efgh-5678", "name": "No domain"}, "metadata": {}},
            {"resource": "not-a-dict"},
        ]
    }


@pytest.mark.asyncio
async def test_search_without_a_domain_spans_every_portal(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(CATALOG).mock(return_value=httpx.Response(200, json=search_results()))
    source = network()

    entries = await source.search("  eviction  ", limit=20)
    await source.close()

    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert dict(params) == {"q": "eviction", "only": "dataset", "limit": "20"}
    assert [(entry.domain, entry.dataset_id) for entry in entries] == [
        ("data.sf.gov", "5cei-gny5"),
        ("data.cityofnewyork.us", "6z8x-wfk4"),
    ]
    assert entries[0].source_url == "https://data.sf.gov/d/5cei-gny5"
    assert entries[0].category == "Housing and Buildings"
    assert entries[0].has_profile is False
    assert entries[1].source_url == "https://data.cityofnewyork.us/d/6z8x-wfk4"


@pytest.mark.asyncio
async def test_search_without_a_domain_stays_inside_an_allowlist(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(CATALOG).mock(return_value=httpx.Response(200, json=search_results()))
    source = network(portals=["data.sf.gov", "data.example.gov"])

    entries = await source.search("eviction")
    await source.close()

    assert route.calls[0].request.url.params["domains"] == "data.example.gov,data.sf.gov"
    assert [entry.domain for entry in entries] == ["data.sf.gov"]


@pytest.mark.asyncio
async def test_search_in_one_domain_resolves_it_and_filters_other_portals(
    respx_mock: respx.MockRouter,
) -> None:
    lookup = resolves_to(respx_mock, "data.sf.gov")
    search = respx_mock.get(CATALOG, params={"only": "dataset"}).mock(
        return_value=httpx.Response(200, json=search_results())
    )
    source = network()

    entries = await source.search("eviction", domain="data.sfgov.org")
    await source.close()

    assert lookup.call_count == 1
    assert search.call_count == 1
    params = search.calls[0].request.url.params
    assert params["domains"] == "data.sf.gov"
    assert params["search_context"] == "data.sf.gov"
    assert [(entry.domain, entry.dataset_id) for entry in entries] == [("data.sf.gov", "5cei-gny5")]


@pytest.mark.asyncio
async def test_search_in_an_unknown_domain_stops_at_the_lookup(
    respx_mock: respx.MockRouter,
) -> None:
    lookup = respx_mock.get(CATALOG, params={"limit": "1"}).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    search = respx_mock.get(CATALOG, params={"only": "dataset"})
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.search("eviction", domain="data.invented-city.gov")
    await source.close()

    assert caught.value.code == "UNKNOWN_PORTAL"
    assert lookup.call_count == 1
    assert not search.called


@pytest.mark.asyncio
@pytest.mark.parametrize(("term", "limit"), [("  ", 10), ("eviction", 0), ("eviction", 51)])
async def test_search_validates_arguments_before_any_request(
    respx_mock: respx.MockRouter, term: str, limit: int
) -> None:
    anything = respx_mock.route()
    source = network()

    with pytest.raises(SocrataQueryError):
        await source.search(term, limit)
    await source.close()

    assert not anything.called


# Describe and query ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_reads_metadata_from_the_canonical_portal(
    respx_mock: respx.MockRouter,
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    views = respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata())
    )
    alias = respx_mock.route(host="data.sfgov.org")
    source = network()

    description = await source.describe("data.sfgov.org", "abcd-1234")
    await source.close()

    assert views.call_count == 1
    assert not alias.called
    assert description.domain == "data.sf.gov"
    assert description.source_url == "https://data.sf.gov/d/abcd-1234"
    assert description.source_updated_at == "2023-11-14T22:13:20+00:00"
    assert [field.field_name for field in description.fields] == ["value"]
    assert len(description.schema_sha256) == 64


@pytest.mark.asyncio
async def test_query_reports_truncation_and_source_version(respx_mock: respx.MockRouter) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata())
    )
    resource = respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(
            200,
            json=[{"value": "1"}, {"value": "2"}, {"value": "3"}],
            headers={
                "Last-Modified": "Wed, 05 Aug 2026 12:00:00 GMT",
                "ETag": '"abc"',
                "X-Socrata-RequestId": "request-1",
            },
        )
    )
    source = network()
    result = await source.query(
        QueryRequest(domain="data.sf.gov", dataset_id="abcd-1234", select="*", max_rows=2)
    )
    await source.close()

    assert result.completeness == Completeness.TRUNCATED
    assert result.returned_row_count == 2
    assert result.rows == [{"value": "1"}, {"value": "2"}]
    assert result.domain == "data.sf.gov"
    assert result.source_updated_at == "2026-08-05T12:00:00+00:00"
    assert result.source_request_id == "request-1"
    assert result.source_etag == '"abc"'
    assert all(call.request.headers["X-App-Token"] == "app-token" for call in respx_mock.calls)
    query_request = resource.calls[0].request
    assert "LIMIT 3" in query_request.url.params["$query"]
    assert result.query_url is not None
    assert result.query_url == str(query_request.url)
    assert result.query_url.startswith("https://data.sf.gov/resource/abcd-1234.json?")
    assert httpx.URL(result.query_url).params["$query"] == result.query
    assert "app-token" not in result.query_url


@pytest.mark.asyncio
async def test_query_complete_result_falls_back_to_metadata_update_time(
    respx_mock: respx.MockRouter,
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata())
    )
    respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(200, json=[{"value": "1"}])
    )
    source = network()
    result = await source.query(
        QueryRequest(domain="data.sf.gov", dataset_id="abcd-1234", select="*", max_rows=2)
    )
    await source.close()

    assert result.completeness == Completeness.COMPLETE
    assert result.source_updated_at == "2023-11-14T22:13:20+00:00"


@pytest.mark.asyncio
async def test_requests_carry_no_app_token_header_without_a_token(
    respx_mock: respx.MockRouter,
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata())
    )
    source = network(app_token="   ")

    await source.describe("data.sf.gov", "abcd-1234")
    await source.close()

    assert len(respx_mock.calls) == 2
    for call in respx_mock.calls:
        assert "X-App-Token" not in call.request.headers
        assert call.request.headers["User-Agent"].startswith("OpenCivicQuery/")


@pytest.mark.asyncio
async def test_query_rejects_invalid_soql_before_reaching_any_host(
    respx_mock: respx.MockRouter,
) -> None:
    anything = respx_mock.route()
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.query(
            QueryRequest(
                domain="data.sf.gov", dataset_id="abcd-1234", select="*", group_by="value;"
            )
        )
    await source.close()

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert not anything.called


@pytest.mark.asyncio
async def test_count_rows_reuses_reference_metadata_and_makes_one_request(
    respx_mock: respx.MockRouter,
) -> None:
    resource = respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(
            200,
            json=[{"opencivicquery_total": "42"}],
            headers={
                "Last-Modified": "Wed, 05 Aug 2026 12:30:00 GMT",
                "ETag": '"count-abc"',
                "X-Socrata-RequestId": "count-request-1",
            },
        )
    )
    source = network()
    reference = SourceQueryResult(
        dataset_id="abcd-1234",
        dataset_name="Test Dataset",
        domain="data.sf.gov",
        source_url="https://data.sf.gov/d/abcd-1234",
        query="SELECT * LIMIT 2",
        query_url="https://data.sf.gov/resource/abcd-1234.json?%24query=SELECT+%2A+LIMIT+2",
        rows=[{"value": "1"}],
        returned_row_count=1,
        completeness=Completeness.TRUNCATED,
        source_updated_at="2026-08-05T12:00:00+00:00",
        source_request_id="request-1",
        source_etag='"abc"',
        schema_sha256="b" * 64,
    )
    result = await source.count_rows(
        QueryRequest(
            domain="data.sf.gov",
            dataset_id="abcd-1234",
            select="*",
            where="value > 0",
            max_rows=1,
        ),
        reference=reference,
    )
    await source.close()

    # No catalog lookup and no metadata fetch: the reference already carries both.
    assert len(respx_mock.calls) == 1
    request = resource.calls[0].request
    assert request.url.params["$query"] == (
        "SELECT count(*) AS opencivicquery_total WHERE value > 0 LIMIT 2"
    )
    assert result.rows == [{"opencivicquery_total": "42"}]
    assert result.dataset_name == "Test Dataset"
    assert result.query_url is not None
    assert result.query_url == str(request.url)
    assert httpx.URL(result.query_url).params["$query"] == result.query
    assert result.source_request_id == "count-request-1"
    assert result.schema_sha256 == "b" * 64


@pytest.mark.asyncio
async def test_metadata_cannot_resolve_to_another_portal(respx_mock: respx.MockRouter) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata("data.cityofnewyork.us"))
    )
    source = network()

    with pytest.raises(SocrataError, match="different portal"):
        await source.describe("data.sf.gov", "abcd-1234")
    await source.close()


@pytest.mark.asyncio
async def test_query_rejects_oversized_response(respx_mock: respx.MockRouter) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata())
    )
    resource = respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(200, json=[{"value": "x" * 5_000}])
    )
    source = network(max_response_bytes=1_000)

    with pytest.raises(SocrataQueryError) as caught:
        await source.query(QueryRequest(domain="data.sf.gov", dataset_id="abcd-1234", select="*"))
    await source.close()

    assert resource.call_count == 1
    assert caught.value.code == "RESULT_TOO_LARGE"
    assert "Narrow the query" in caught.value.tool_error()["message"]


@pytest.mark.asyncio
async def test_default_response_ceiling_is_four_mebibytes() -> None:
    source = SocrataNetwork(app_token=None, portals=None)

    assert source._max_response_bytes == 4 * 1024 * 1024
    await source.close()


@pytest.mark.asyncio
async def test_source_handles_compressed_json_without_double_decoding(
    respx_mock: respx.MockRouter,
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(
            200,
            content=gzip.compress(json.dumps(metadata()).encode()),
            headers={"Content-Encoding": "gzip"},
        )
    )
    source = network()

    description = await source.describe("data.sf.gov", "abcd-1234")
    await source.close()

    assert description.name == "Test Dataset"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "QUERY_REJECTED"),
        (401, "SOURCE_ACCESS_DENIED"),
        (403, "SOURCE_ACCESS_DENIED"),
        (404, "DATASET_NOT_FOUND"),
        (503, "SOURCE_UNAVAILABLE"),
    ],
)
async def test_source_errors_scrub_the_app_token_and_oauth_credentials(
    respx_mock: respx.MockRouter, status: int, code: str
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata())
    )
    respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(
            status,
            json={"message": "echo app-token access_token=private-oauth-value Bearer abc.def"},
        )
    )
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.query(QueryRequest(domain="data.sf.gov", dataset_id="abcd-1234", select="*"))
    await source.close()

    error = caught.value
    assert error.code == code
    assert error.http_status == status
    for text in (str(error), json.dumps(error.tool_error())):
        assert "app-token" not in text
        assert "private-oauth-value" not in text
        assert "abc.def" not in text
    assert "[REDACTED]" in str(error)


@pytest.mark.asyncio
async def test_retryable_status_is_retried_then_succeeds(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(CATALOG).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=catalog_hit("data.sf.gov")),
        ]
    )
    source = network(max_attempts=2)

    assert await source.resolve_portal("data.sf.gov") == "data.sf.gov"
    await source.close()

    assert route.call_count == 2


@pytest.mark.asyncio
async def test_transport_failures_become_a_source_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(CATALOG).mock(side_effect=httpx.ConnectError("unreachable"))
    source = network(max_attempts=1)

    with pytest.raises(SocrataError) as caught:
        await source.resolve_portal("data.sf.gov")
    await source.close()

    assert str(caught.value) == "Socrata request failed"
    assert not isinstance(caught.value, SocrataQueryError)


@pytest.mark.asyncio
async def test_close_leaves_an_injected_client_open() -> None:
    client = httpx.AsyncClient()
    source = network(client=client)

    await source.close()

    assert not client.is_closed
    await client.aclose()


# Community and sample portals --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain", ["opendata.socrata.com", "OpenData.Socrata.com", "evergreen.data.socrata.com"]
)
async def test_resolve_portal_refuses_community_portals_without_a_request(
    respx_mock: respx.MockRouter, domain: str
) -> None:
    anything = respx_mock.route()
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.resolve_portal(domain)
    await source.close()

    assert caught.value.code == "UNKNOWN_PORTAL"
    assert caught.value.argument == "domain"
    assert not anything.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical",
    ["opendata.socrata.com", "evergreen.data.socrata.com", "sample.demo.socrata.com"],
)
async def test_resolve_portal_checks_the_exclusion_on_the_canonical_domain(
    respx_mock: respx.MockRouter, canonical: str
) -> None:
    resolves_to(respx_mock, canonical)
    excluded = respx_mock.route(host=canonical)
    source = network()

    with pytest.raises(SocrataQueryError) as caught:
        await source.resolve_portal("data.example.gov")
    await source.close()

    assert caught.value.code == "UNKNOWN_PORTAL"
    assert source.cached_portal("data.example.gov") is None
    assert source.cached_portal(canonical) is None
    assert not excluded.called


@pytest.mark.asyncio
async def test_search_drops_community_portal_results(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(CATALOG).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"resource": {"id": "aaaa-1111"}, "metadata": {"domain": "data.sf.gov"}},
                    {
                        "resource": {"id": "bbbb-2222"},
                        "metadata": {"domain": "opendata.socrata.com"},
                    },
                    {
                        "resource": {"id": "cccc-3333"},
                        "metadata": {"domain": "evergreen.data.socrata.com"},
                    },
                ]
            },
        )
    )
    source = network()

    entries = await source.search("anything")
    await source.close()

    assert [entry.dataset_id for entry in entries] == ["aaaa-1111"]


# Cache accessor and query validation -------------------------------------------------------


@pytest.mark.asyncio
async def test_cached_portal_answers_only_from_confirmed_lookups(
    respx_mock: respx.MockRouter,
) -> None:
    route = resolves_to(respx_mock, "data.sf.gov")
    source = network()

    assert source.cached_portal("data.sfgov.org") is None
    assert route.call_count == 0
    await source.resolve_portal("data.sfgov.org")

    assert source.cached_portal("data.sfgov.org") == "data.sf.gov"
    assert source.cached_portal(" DATA.SF.GOV ") == "data.sf.gov"
    assert source.cached_portal("data.cityofnewyork.us") is None
    for invalid in ("127.0.0.1", "localhost", "data.sf.gov:443", ""):
        assert source.cached_portal(invalid) is None
    assert route.call_count == 1
    await source.close()


@pytest.mark.asyncio
async def test_cached_portal_reports_a_confirmed_portal_even_when_disallowed(
    respx_mock: respx.MockRouter,
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    source = network(portals=["data.cityofnewyork.us"])

    with pytest.raises(SocrataQueryError):
        await source.resolve_portal("data.sf.gov")
    await source.close()

    # The cache records what the catalog confirmed. Access is still refused on every use.
    assert source.cached_portal("data.sf.gov") == "data.sf.gov"


@pytest.mark.asyncio
async def test_validate_query_checks_soql_without_any_request(
    respx_mock: respx.MockRouter,
) -> None:
    anything = respx_mock.route()
    source = network()

    assert (
        source.validate_query(
            QueryRequest(domain="data.sf.gov", dataset_id="abcd-1234", select="count(*)")
        )
        is None
    )
    with pytest.raises(SocrataQueryError) as caught:
        source.validate_query(
            QueryRequest(domain="data.sf.gov", dataset_id="abcd-1234", select="*; DROP")
        )
    await source.close()

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert not anything.called


BYPASS = "boro = \"a'\" OR camis IS NOT NULL LIMIT 3 |> SELECT count(*) AS n WHERE '1'='1'"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("where", BYPASS),
        # Without the pipe, the double-quoted literal no longer hides the LIMIT keyword.
        ("where", 'boro = "a\'" OR camis IS NOT NULL LIMIT 3'),
        ("where", 'boro = "x" OR 1 = 1 UNION SELECT *'),
        ("select", "camis |> SELECT count(*)"),
        ("order_by", "camis |> x"),
    ],
)
async def test_chained_query_bypass_is_refused_before_any_request(
    respx_mock: respx.MockRouter, field: str, value: str
) -> None:
    anything = respx_mock.route()
    source = network()
    values = {"domain": "data.cityofnewyork.us", "dataset_id": "43nn-pn8j", "select": "*"}
    values[field] = value
    request = QueryRequest(**values)

    for attempt in (lambda: source.validate_query(request), lambda: build_soql(request)):
        with pytest.raises(SocrataQueryError) as caught:
            attempt()
        assert caught.value.code == "INVALID_QUERY_ARGUMENT"
        assert caught.value.argument == field
    with pytest.raises(SocrataQueryError):
        await source.query(request)
    await source.close()

    assert not anything.called


def test_build_soql_allows_keywords_inside_double_quoted_literals() -> None:
    query = build_soql(
        QueryRequest(
            domain="data.sf.gov",
            dataset_id="abcd-1234",
            select="*",
            where="description = \"select where limit\" AND name = 'it''s'",
        )
    )

    assert 'WHERE description = "select where limit"' in query


# Retry policy ------------------------------------------------------------------------------


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff sleeps instead of waiting, without touching the global asyncio."""
    import asyncio
    import types

    from opencivicquery.sources import socrata as socrata_module

    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(
        socrata_module,
        "asyncio",
        types.SimpleNamespace(sleep=fake_sleep, Semaphore=asyncio.Semaphore),
    )
    return recorded


def data_query_routes(respx_mock: respx.MockRouter) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata())
    )


DATA_REQUEST = QueryRequest(domain="data.sf.gov", dataset_id="abcd-1234", select="*")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 504])
async def test_data_query_is_not_replayed_on_a_server_error(
    respx_mock: respx.MockRouter, sleeps: list[float], status: int
) -> None:
    data_query_routes(respx_mock)
    resource = respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(status, json={"message": "boom"})
    )
    source = network(max_attempts=4)

    with pytest.raises(SocrataQueryError) as caught:
        await source.query(DATA_REQUEST)
    await source.close()

    assert resource.call_count == 1
    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert caught.value.http_status == status
    assert sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [202, 429, 503])
async def test_data_query_retries_when_socrata_asks_for_it(
    respx_mock: respx.MockRouter, sleeps: list[float], status: int
) -> None:
    data_query_routes(respx_mock)
    resource = respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        side_effect=[httpx.Response(status), httpx.Response(200, json=[{"value": "1"}])]
    )
    source = network(max_attempts=4)

    result = await source.query(DATA_REQUEST)
    await source.close()

    assert resource.call_count == 2
    assert result.rows == [{"value": "1"}]
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_retry_after_is_honored_and_capped(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    data_query_routes(respx_mock)
    respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(200, json=[]),
        ]
    )
    source = network(max_attempts=4)

    await source.query(DATA_REQUEST)
    await source.close()

    assert sleeps == [2.0, 5.0]


@pytest.mark.asyncio
async def test_data_query_gives_up_after_the_last_attempt(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    data_query_routes(respx_mock)
    resource = respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(503, json={"message": "busy"})
    )
    source = network(max_attempts=3)

    with pytest.raises(SocrataQueryError) as caught:
        await source.query(DATA_REQUEST)
    await source.close()

    assert resource.call_count == 3
    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert sleeps == [0.25, 0.5]


@pytest.mark.asyncio
async def test_data_query_is_not_replayed_after_a_read_timeout(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    data_query_routes(respx_mock)
    resource = respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    source = network(max_attempts=4)

    with pytest.raises(SocrataError) as caught:
        await source.query(DATA_REQUEST)
    await source.close()

    assert resource.call_count == 1
    assert str(caught.value) == "Socrata request failed"
    assert sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_data_query_retries_a_connection_that_never_opened(
    respx_mock: respx.MockRouter, sleeps: list[float], failure: type[Exception]
) -> None:
    data_query_routes(respx_mock)
    resource = respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        side_effect=[failure("refused"), httpx.Response(200, json=[{"value": "1"}])]
    )
    source = network(max_attempts=4)

    result = await source.query(DATA_REQUEST)
    await source.close()

    assert resource.call_count == 2
    assert result.returned_row_count == 1


@pytest.mark.asyncio
async def test_count_rows_is_not_replayed_on_a_server_error(
    respx_mock: respx.MockRouter, sleeps: list[float]
) -> None:
    resource = respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(500)
    )
    source = network(max_attempts=4)
    reference = SourceQueryResult(
        dataset_id="abcd-1234",
        dataset_name="Test Dataset",
        domain="data.sf.gov",
        source_url="https://data.sf.gov/d/abcd-1234",
        query="SELECT * LIMIT 2",
        rows=[],
        returned_row_count=0,
        completeness=Completeness.TRUNCATED,
        schema_sha256="b" * 64,
    )

    with pytest.raises(SocrataQueryError):
        await source.count_rows(DATA_REQUEST, reference=reference)
    await source.close()

    assert resource.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first",
    [httpx.Response(500), httpx.Response(502), httpx.Response(504), httpx.ReadTimeout("slow")],
)
async def test_metadata_reads_retry_on_any_transient_failure(
    respx_mock: respx.MockRouter, sleeps: list[float], first: object
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    views = respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        side_effect=[first, httpx.Response(200, json=metadata())]
    )
    source = network(max_attempts=4)

    description = await source.describe("data.sf.gov", "abcd-1234")
    await source.close()

    assert views.call_count == 2
    assert description.name == "Test Dataset"


@pytest.mark.asyncio
async def test_concurrency_slot_is_released_while_waiting_to_retry(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import types

    from opencivicquery.sources import socrata as socrata_module

    locked_during_sleep: list[bool] = []
    holder: dict[str, SocrataNetwork] = {}

    async def fake_sleep(delay: float) -> None:
        locked_during_sleep.append(holder["source"]._semaphore.locked())

    monkeypatch.setattr(
        socrata_module,
        "asyncio",
        types.SimpleNamespace(sleep=fake_sleep, Semaphore=asyncio.Semaphore),
    )
    respx_mock.get(CATALOG).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=catalog_hit("data.sf.gov"))]
    )
    source = network(max_attempts=2, max_concurrency=1)
    holder["source"] = source

    await source.resolve_portal("data.sf.gov")
    await source.close()

    assert locked_during_sleep == [False]


# Malformed upstream responses -------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_json_catalog_body_is_a_source_error(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(CATALOG).mock(return_value=httpx.Response(200, text="<html></html>"))
    source = network()

    for _ in range(2):
        with pytest.raises(SocrataError, match="not JSON") as caught:
            await source.resolve_portal("data.sf.gov")
        assert not isinstance(caught.value, SocrataQueryError)
    await source.close()

    # A garbled answer is not remembered as an unknown portal.
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_non_json_query_body_is_a_source_error(respx_mock: respx.MockRouter) -> None:
    data_query_routes(respx_mock)
    respx_mock.get("https://data.sf.gov/resource/abcd-1234.json").mock(
        return_value=httpx.Response(200, text="not json")
    )
    source = network()

    with pytest.raises(SocrataError, match="not JSON"):
        await source.query(DATA_REQUEST)
    await source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("columns", "expected_fields"),
    [
        (None, []),
        ("not-a-list", []),
        ([1, "x", None, {"name": "No field name"}], []),
        ([1, {"name": "Value", "fieldName": "value", "dataTypeName": "number"}], ["value"]),
    ],
)
async def test_describe_tolerates_malformed_columns(
    respx_mock: respx.MockRouter, columns: object, expected_fields: list[str]
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata() | {"columns": columns})
    )
    source = network()

    description = await source.describe("data.sf.gov", "abcd-1234")
    await source.close()

    assert [field.field_name for field in description.fields] == expected_fields
    assert len(description.schema_sha256) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [10**20, -(10**20), 1e300, "1700000000", True, None])
async def test_unusable_update_times_become_none(
    respx_mock: respx.MockRouter, value: object
) -> None:
    resolves_to(respx_mock, "data.sf.gov")
    respx_mock.get("https://data.sf.gov/api/views/abcd-1234").mock(
        return_value=httpx.Response(200, json=metadata() | {"rowsUpdatedAt": value})
    )
    source = network()

    description = await source.describe("data.sf.gov", "abcd-1234")
    await source.close()

    assert description.source_updated_at is None
