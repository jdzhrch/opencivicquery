from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from opencivicquery.models import Completeness
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.govinfo import (
    COLLECTION_PAGE,
    COVERAGE_WARNING,
    DATE_WARNING,
    FIELDS,
    GOVINFO_HOST,
    MAX_LIMIT,
    NO_MATCH_RULE,
    ROW_GRAIN,
    SEVERAL_OPINIONS_WARNING,
    GovInfo,
)

KEY = "test-key-0123456789"

# Trimmed from a live search answer on 2026-11-20.
OPINION = {
    "title": "Derrick Sanderlin, et al v. Jason Dwyer, et al",
    "packageId": "USCOURTS-ca9-23-15487",
    "granuleId": "USCOURTS-ca9-23-15487-0",
    "lastModified": "2026-10-31T17:41:14Z",
    "governmentAuthor": ["United States Court of Appeals for the Ninth Circuit"],
    "dateIssued": "2024-09-04",
    "collectionCode": "USCOURTS",
    "resultLink": (
        "https://api.govinfo.gov/packages/USCOURTS-ca9-23-15487/granules/"
        "USCOURTS-ca9-23-15487-0/summary"
    ),
    "relatedLink": None,
    "dateIngested": "2024-09-05",
    "download": {
        "pdfLink": (
            "https://api.govinfo.gov/packages/USCOURTS-ca9-23-15487/granules/"
            "USCOURTS-ca9-23-15487-0/pdf"
        ),
        "zipLink": "https://api.govinfo.gov/packages/USCOURTS-ca9-23-15487/zip",
    },
}
DISTRICT_OPINION = OPINION | {
    "title": "Maho v. Hankins et al",
    "packageId": "USCOURTS-nmd-1_19-cv-00182",
    "granuleId": "USCOURTS-nmd-1_19-cv-00182-2",
    "governmentAuthor": ["United States District Court District of New Mexico"],
    "dateIssued": "2020-05-19",
    "dateIngested": "2026-05-18",
}

AMENDED_OPINION = OPINION | {
    "title": "Sydney Rieman, et al v. Gloria Vazquez, et al",
    "packageId": "USCOURTS-ca9-22-56054",
    "granuleId": "USCOURTS-ca9-22-56054-1",
    "dateIssued": "2024-04-02",
    "dateIngested": "2024-04-03",
}


def answer(count: int, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": count, "offsetMark": "AoIIQgN4", "results": results}


def govinfo_answering(
    route: Callable[[httpx.Request], httpx.Response], *, api_key: str | None = KEY
) -> tuple[GovInfo, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return route(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GovInfo(api_key=api_key, client=client), requests


def always(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: response


def sent_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_a_missing_key_is_refused_before_any_request(api_key: str | None) -> None:
    govinfo, requests = govinfo_answering(always(httpx.Response(200)), api_key=api_key)

    with pytest.raises(SourceQueryError) as caught:
        govinfo.search_opinions(text="qualified immunity")

    assert caught.value.code == "SOURCE_NOT_CONFIGURED"
    assert caught.value.public_message == "This server has no GovInfo API key configured."
    assert requests == []


def test_a_malformed_key_is_refused_without_echoing_it() -> None:
    with pytest.raises(ValueError) as caught:
        GovInfo(api_key="bad key?secret")

    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_search_posts_one_body_with_the_key_only_in_a_header() -> None:
    govinfo, requests = govinfo_answering(
        always(httpx.Response(200, json=answer(48, [OPINION, AMENDED_OPINION])))
    )

    prepared = govinfo.search_opinions(
        text="  qualified   immunity ",
        court="CA9",
        issued_from="2024-01-01",
        issued_to="2024-12-31",
        limit=2,
    )
    found = await prepared.run()

    (sent,) = requests
    assert sent.method == "POST"
    assert str(sent.url) == f"https://{GOVINFO_HOST}/search"
    assert sent.headers["X-Api-Key"] == KEY
    assert sent.headers["User-Agent"].startswith("OpenCivicQuery/")
    query = (
        'collection:(USCOURTS) and courtname:"United States Court of Appeals for the Ninth '
        'Circuit" and publishdate:range(2024-01-01,2024-12-31) and ("qualified" "immunity")'
    )
    body = {
        "offsetMark": "*",
        "pageSize": 2,
        "query": query,
        "sorts": [{"field": "score", "sortOrder": "DESC"}],
    }
    assert sent_body(sent) == body
    assert prepared.host == GOVINFO_HOST
    assert prepared.arguments == {
        "text": "qualified immunity",
        "court": "ca9",
        "issued_from": "2024-01-01",
        "issued_to": "2024-12-31",
        "limit": 2,
    }
    result = found.result
    assert result.query == json.dumps(body, sort_keys=True, separators=(",", ":"))
    assert result.query_url is not None
    assert result.query_url.startswith("https://www.govinfo.gov/app/search/")
    state = json.loads(
        unquote(result.query_url.removeprefix("https://www.govinfo.gov/app/search/"))
    )
    assert state == {"query": query, "offset": 0}
    assert result.source_url == COLLECTION_PAGE
    assert result.domain == GOVINFO_HOST
    assert result.dataset_id == "USCOURTS"
    assert "48 matching opinion documents" in result.dataset_name
    assert "court ca9" in result.dataset_name
    assert result.completeness == Completeness.TRUNCATED
    assert result.returned_row_count == 2
    assert found.row_grain == ROW_GRAIN
    assert found.warnings[0] == SEVERAL_OPINIONS_WARNING
    assert COVERAGE_WARNING in found.warnings
    assert DATE_WARNING in found.warnings
    assert found.escalations == []


@pytest.mark.asyncio
async def test_rows_keep_source_names_and_add_public_links() -> None:
    govinfo, _ = govinfo_answering(
        always(httpx.Response(200, json=answer(2, [OPINION, DISTRICT_OPINION])))
    )

    found = await govinfo.search_opinions(text="qualified immunity").run()

    first, second = found.result.rows
    assert set(first) == set(FIELDS)
    assert "download" not in first
    assert "resultLink" not in first
    assert first["title"] == OPINION["title"]
    assert first["dateIssued"] == "2024-09-04"
    assert first["governmentAuthor"] == ["United States Court of Appeals for the Ninth Circuit"]
    assert first["court_code"] == "ca9"
    assert first["case_number"] == "23-15487"
    assert first["details_url"] == (
        "https://www.govinfo.gov/app/details/USCOURTS-ca9-23-15487/USCOURTS-ca9-23-15487-0"
    )
    assert first["pdf_url"] == (
        "https://www.govinfo.gov/content/pkg/USCOURTS-ca9-23-15487/pdf/USCOURTS-ca9-23-15487-0.pdf"
    )
    assert second["court_code"] == "nmd"
    assert second["case_number"] == "1:19-cv-00182"
    assert found.result.completeness == Completeness.COMPLETE


@pytest.mark.asyncio
async def test_open_ended_dates_and_no_court() -> None:
    govinfo, requests = govinfo_answering(always(httpx.Response(200, json=answer(0, []))))

    await govinfo.search_opinions(text="eviction", issued_from="2025-01-01").run()
    await govinfo.search_opinions(text="eviction", issued_to="2025-01-01").run()

    assert [sent_body(request)["query"] for request in requests] == [
        'collection:(USCOURTS) and publishdate:range(2025-01-01,) and ("eviction")',
        'collection:(USCOURTS) and publishdate:range(,2025-01-01) and ("eviction")',
    ]


@pytest.mark.asyncio
async def test_no_match_carries_a_rule_against_overclaiming() -> None:
    govinfo, _ = govinfo_answering(always(httpx.Response(200, json=answer(0, []))))

    found = await govinfo.search_opinions(text="zzqx", court="cofc").run()

    assert found.result.completeness == Completeness.COMPLETE
    assert found.result.rows == []
    assert found.rules[0] == NO_MATCH_RULE
    assert found.escalations == ["NO_OPINIONS_MATCHED"]


_SAFE_QUERY = re.compile(r'^collection:\(USCOURTS\) and \((?:"[^\W_]+" )*"[^\W_]+"\)$')


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("x OR collection:BILLS", ["x", "OR", "collection", "BILLS"]),
        (") or (collection:(BILLS", ["or", "collection", "BILLS"]),
        ('"ways and means"', ["ways", "and", "means"]),
        ('x" or collection:"BILLS', ["x", "or", "collection", "BILLS"]),
        (
            "cardin near/10 congress* capitali?e",
            ["cardin", "near", "10", "congress", "capitali", "e"],
        ),
        ("-report +cardin title:(x) \\ {y}", ["report", "cardin", "title", "x", "y"]),
        (
            "lastModified:range(2023-08-15T12:00:05Z,)",
            ["lastModified", "range", "2023", "08", "15T12", "00", "05Z"],
        ),
        (
            "\uff23\uff2f\uff2c\uff2c\uff25\uff23\uff34\uff29\uff2f\uff2e\uff1abills",
            ["COLLECTION", "bills"],
        ),
        ("Pe\u00f1a v. Doe", ["Pe\u00f1a", "v", "Doe"]),
    ],
)
@pytest.mark.asyncio
async def test_query_syntax_in_text_is_neutralized(text: str, words: list[str]) -> None:
    govinfo, requests = govinfo_answering(always(httpx.Response(200, json=answer(0, []))))

    await govinfo.search_opinions(text=text).run()

    query = sent_body(requests[0])["query"]
    assert _SAFE_QUERY.fullmatch(query), query
    sent_words = re.findall(r'"([^"]*)"', query.removeprefix("collection:(USCOURTS) and "))
    assert sent_words == words
    assert query.count("collection:") == 1


@pytest.mark.parametrize(
    ("arguments", "argument"),
    [
        ({"text": "x"}, "text"),
        ({"text": "!! ** ::"}, "text"),
        ({"text": "immunity\u202e"}, "text"),
        ({"text": "immunity\x00"}, "text"),
        ({"text": "immunity\u200b"}, "text"),
        ({"text": "immunity\ud800"}, "text"),
        ({"text": "a" * 201}, "text"),
        ({"text": None}, "text"),
        ({"court": "zz9"}, "court"),
        ({"court": "Ninth Circuit"}, "court"),
        ({"court": "nysd"}, "court"),
        ({"court": "tc"}, "court"),
        ({"court": ""}, "court"),
        ({"court": 9}, "court"),
        ({"issued_from": "2024/01/01"}, "issued_from"),
        ({"issued_from": "2024-02-30"}, "issued_from"),
        ({"issued_from": "\u0662\u0660\u0662\u0664-01-01"}, "issued_from"),
        ({"issued_to": "9999-12-31"}, "issued_to"),
        ({"issued_from": "2024-02-01", "issued_to": "2024-01-31"}, "issued_to"),
        ({"limit": 0}, "limit"),
        ({"limit": MAX_LIMIT + 1}, "limit"),
        ({"limit": True}, "limit"),
        ({"limit": "5"}, "limit"),
    ],
)
def test_invalid_arguments_are_refused_before_any_request(
    arguments: dict[str, Any], argument: str
) -> None:
    govinfo, requests = govinfo_answering(always(httpx.Response(200, json=answer(0, []))))

    with pytest.raises(SourceQueryError) as caught:
        govinfo.search_opinions(**({"text": "qualified immunity"} | arguments))

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == argument
    assert requests == []


@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (400, {"message": f"pageSize is wrong for api_key={KEY}"}, "QUERY_REJECTED"),
        (
            401,
            {"error": {"code": "API_KEY_MISSING", "message": "No api_key was supplied."}},
            "SOURCE_ACCESS_DENIED",
        ),
        (
            403,
            {"error": {"code": "API_KEY_INVALID", "message": f"Bad key {KEY}"}},
            "SOURCE_ACCESS_DENIED",
        ),
        (
            429,
            {"error": {"code": "OVER_RATE_LIMIT", "message": "Over the limit."}},
            "SOURCE_RATE_LIMITED",
        ),
        (500, {"message": f"Internal error for {KEY}"}, "SOURCE_UNAVAILABLE"),
        (502, None, "SOURCE_UNAVAILABLE"),
        (503, None, "SOURCE_UNAVAILABLE"),
    ],
)
@pytest.mark.asyncio
async def test_http_failures_are_typed_and_never_carry_the_key(
    status: int, payload: dict[str, Any] | None, code: str
) -> None:
    response = (
        httpx.Response(status, json=payload)
        if payload is not None
        else httpx.Response(status, text="<html>Bad gateway</html>")
    )
    govinfo, _ = govinfo_answering(always(response))

    with pytest.raises(SourceQueryError) as caught:
        await govinfo.search_opinions(text="qualified immunity").run()

    assert caught.value.code == code
    assert caught.value.http_status == status
    assert KEY not in str(caught.value)
    assert KEY not in json.dumps(caught.value.tool_error())
    if status == 400:
        assert "pageSize is wrong" in caught.value.public_message


@pytest.mark.asyncio
async def test_a_network_failure_never_carries_the_key() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.headers['X-Api-Key']}", request=request)

    govinfo, _ = govinfo_answering(route)

    with pytest.raises(SourceError) as caught:
        await govinfo.search_opinions(text="qualified immunity").run()

    assert KEY not in str(caught.value)


@pytest.mark.asyncio
async def test_the_key_never_appears_in_anything_returned() -> None:
    keyed = OPINION | {
        "title": f"Sanderlin v. Dwyer {KEY}",
        "resultLink": f"{OPINION['resultLink']}?api_key={KEY}",
        "relatedLink": f"https://api.govinfo.gov/related/USCOURTS-ca9-23-15487-0?api_key={KEY}",
        "download": {"pdfLink": f"https://api.govinfo.gov/packages/x/pdf?api_key={KEY}"},
        "packageLink": f"https://api.govinfo.gov/packages/USCOURTS-ca9-23-15487?api_key={KEY}",
    }
    govinfo, _ = govinfo_answering(always(httpx.Response(200, json=answer(5, [keyed]))))

    prepared = govinfo.search_opinions(text="qualified immunity", limit=1)
    found = await prepared.run()

    serialized = json.dumps(
        {
            "result": found.result.model_dump(mode="json"),
            "warnings": found.warnings,
            "rules": found.rules,
            "row_grain": found.row_grain,
            "arguments": prepared.arguments,
            "host": prepared.host,
        }
    )
    assert KEY not in serialized
    assert "api_key" not in serialized
    assert "api.govinfo.gov/packages" not in serialized


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"results": []},
        {"count": True, "results": []},
        {"count": -1, "results": []},
        {"count": "3", "results": []},
        {"count": 3, "results": None},
        {"count": 1, "results": [OPINION, OPINION]},
        {"count": 9, "results": [OPINION, OPINION, OPINION]},
        {"count": 1, "results": ["USCOURTS-ca9-23-15487"]},
        {"count": 1, "results": [OPINION | {"collectionCode": "BILLS"}]},
        {"count": 1, "results": [OPINION | {"packageId": "BILLS-118hr1ih"}]},
        {"count": 1, "results": [OPINION | {"packageId": f"USCOURTS-ca9-1?api_key={KEY}"}]},
        {"count": 1, "results": [OPINION | {"granuleId": "USCOURTS-ca9-99-00001-0"}]},
        {"count": 1, "results": [OPINION | {"granuleId": None}]},
        {"count": 2, "results": [OPINION, OPINION]},
        {
            "count": 1,
            "results": [
                OPINION | {"packageId": f"USCOURTS-ca9-{KEY}", "granuleId": f"USCOURTS-ca9-{KEY}-0"}
            ],
        },
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_answer_is_a_source_error(payload: Any) -> None:
    govinfo, _ = govinfo_answering(always(httpx.Response(200, json=payload)))

    with pytest.raises(SourceError) as caught:
        await govinfo.search_opinions(text="qualified immunity", limit=2).run()

    assert type(caught.value) is SourceError
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("body", ["<html>maintenance</html>", "", "[" * 200_000])
@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_a_source_error(body: str) -> None:
    govinfo, _ = govinfo_answering(always(httpx.Response(200, text=body)))

    with pytest.raises(SourceError) as caught:
        await govinfo.search_opinions(text="qualified immunity").run()

    assert type(caught.value) is SourceError


@pytest.mark.asyncio
async def test_an_opinion_from_another_court_is_a_source_error() -> None:
    govinfo, _ = govinfo_answering(
        always(httpx.Response(200, json=answer(2, [OPINION, DISTRICT_OPINION])))
    )

    with pytest.raises(SourceError) as caught:
        await govinfo.search_opinions(text="qualified immunity", court="ca9").run()

    assert type(caught.value) is SourceError


@pytest.mark.asyncio
async def test_the_court_code_matches_regardless_of_case() -> None:
    dc = OPINION | {
        "packageId": "USCOURTS-caDC-21-5200",
        "granuleId": "USCOURTS-caDC-21-5200-0",
    }
    govinfo, requests = govinfo_answering(always(httpx.Response(200, json=answer(1, [dc]))))

    found = await govinfo.search_opinions(text="contract", court="cadc").run()

    assert found.result.rows[0]["court_code"] == "caDC"
    assert (
        'courtname:"United States Court of Appeals for the District of Columbia Circuit"'
        in sent_body(requests[0])["query"]
    )


@pytest.mark.asyncio
async def test_a_long_source_message_is_redacted_before_it_is_shortened() -> None:
    message = "x" * 290 + KEY
    govinfo, _ = govinfo_answering(always(httpx.Response(400, json={"message": message})))

    with pytest.raises(SourceQueryError) as caught:
        await govinfo.search_opinions(text="qualified immunity").run()

    assert KEY[:6] not in caught.value.public_message
    assert KEY[:6] not in str(caught.value)


def test_a_client_that_follows_redirects_is_refused() -> None:
    client = httpx.AsyncClient(follow_redirects=True)

    with pytest.raises(ValueError):
        GovInfo(api_key=KEY, client=client)
