from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from opencivicquery.models import Completeness
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.nonprofits import (
    CITATION_RULE,
    FILING_FIELDS,
    LAG_WARNING,
    NO_FILINGS_RULE,
    NO_FILINGS_WARNING,
    NO_MATCH_WARNING,
    NONPROFITS_HOST,
    PDF_ONLY_WARNING,
    SEARCH_MATCH_WARNING,
    TAX_PERIOD_WARNING,
    NonprofitExplorer,
)

# Trimmed from live responses on 2026-11-21.
ORGANIZATION = {
    "id": 510187791,
    "ein": 510187791,
    "name": "Mission Economic Development Agency",
    "city": "San Francisco",
    "state": "CA",
    "updated_at": "2026-10-21T13:30:38.186Z",
}
WITH_DATA = [
    {
        "tax_prd": 202212,
        "tax_prd_yr": 2022,
        "formtype": 0,
        "pdf_url": "https://projects.propublica.org/nonprofits/download-filing?path=a.pdf",
        "updated": "2025-08-05T16:09:39.859Z",
        "totrevenue": 25000000,
        "totfuncexpns": 24000000,
        "totassetsend": 200000000,
        "totliabend": 190000000,
        "totcntrbgfts": 15695758,
    },
    {
        "tax_prd": 202312,
        "tax_prd_yr": 2023,
        "formtype": 0,
        "pdf_url": None,
        "updated": "2025-08-05T16:09:39.859Z",
        "totrevenue": 26474112,
        "totfuncexpns": 31693130,
        "totassetsend": 210455685,
        "totliabend": 197958967,
    },
]
WITHOUT_DATA = [
    {
        "tax_prd": 202412,
        "tax_prd_yr": 2024,
        "formtype": 0,
        "formtype_str": "990",
        "pdf_url": "https://projects.propublica.org/nonprofits/download-filing?path=b.pdf",
    },
    {
        "tax_prd": 200812,
        "tax_prd_yr": 2008,
        "formtype": 0,
        "formtype_str": "990R",
        "pdf_url": "https://projects.propublica.org/nonprofits/download-filing?path=c.pdf",
    },
]
FILINGS = {
    "organization": ORGANIZATION,
    "filings_with_data": WITH_DATA,
    "filings_without_data": WITHOUT_DATA,
    "data_source": "ProPublica Nonprofit Explorer API",
    "api_version": 2,
}
SEARCH = {
    "total_results": 30,
    "num_pages": 2,
    "cur_page": 0,
    "per_page": 25,
    "organizations": [
        {
            "ein": 42103580,
            "strein": "04-2103580",
            "name": "President and Fellows of Harvard College",
            "sub_name": None,
            "city": "Cambridge",
            "state": "MA",
            "ntee_code": "B43",
            "subseccd": 3,
            "score": 10.0,
        }
    ],
}


def explorer_answering(
    route: Callable[[httpx.Request], httpx.Response],
) -> tuple[NonprofitExplorer, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return route(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NonprofitExplorer(client=client), requests


def always(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: response


@pytest.mark.asyncio
async def test_search_sends_plain_words_and_reports_a_partial_page() -> None:
    explorer, requests = explorer_answering(always(httpx.Response(200, json=SEARCH)))

    found = await explorer.search_nonprofits(name='  "Harvard"  -College +x ', state="ma").run()

    (sent,) = requests
    assert sent.url.host == NONPROFITS_HOST
    assert sent.url.path == "/nonprofits/api/v2/search.json"
    assert dict(sent.url.params) == {"q": "Harvard College x", "state[id]": "MA"}
    assert sent.headers["User-Agent"].startswith("OpenCivicQuery/")
    result = found.result
    assert result.completeness == Completeness.TRUNCATED
    (row,) = result.rows
    assert row["strein"] == "04-2103580"
    assert row["organization_page_url"].endswith("/nonprofits/organizations/42103580")
    assert "score" not in row
    assert "30 organizations match" in found.warnings[0]
    assert SEARCH_MATCH_WARNING in found.warnings
    assert found.rules == [CITATION_RULE]
    assert result.query_url is not None and result.query_url.startswith(
        f"https://{NONPROFITS_HOST}/nonprofits/api/v2/search.json?"
    )


@pytest.mark.asyncio
async def test_an_empty_search_is_complete_and_warns_first() -> None:
    # checked 2026-11-21: the API answers a search with no match with HTTP 404.
    empty = {"total_results": 0, "num_pages": 0, "organizations": []}
    explorer, _ = explorer_answering(always(httpx.Response(404, json=empty)))

    found = await explorer.search_nonprofits(name="zzzzqqqxx").run()

    assert found.result.completeness == Completeness.COMPLETE
    assert found.warnings[0] == NO_MATCH_WARNING


@pytest.mark.parametrize(
    ("arguments", "argument"),
    [
        ({"name": "x"}, "name"),
        ({"name": '" - +'}, "name"),
        ({"name": "a" * 101}, "name"),
        ({"name": "Harvard​ College"}, "name"),
        ({"name": "Harvard", "state": "Massachusetts"}, "state"),
        ({"name": "Harvard", "state": "XX"}, "state"),
    ],
)
def test_invalid_searches_are_refused_before_any_request(
    arguments: dict[str, str], argument: str
) -> None:
    explorer, requests = explorer_answering(always(httpx.Response(200, json=SEARCH)))

    with pytest.raises(SourceQueryError) as caught:
        explorer.search_nonprofits(**arguments)

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == argument
    assert requests == []


@pytest.mark.asyncio
async def test_filings_merge_both_lists_newest_first() -> None:
    explorer, requests = explorer_answering(always(httpx.Response(200, json=FILINGS)))

    found = await explorer.get_filings(ein="51-0187791").run()

    assert requests[0].url.path == "/nonprofits/api/v2/organizations/510187791.json"
    result = found.result
    assert result.completeness == Completeness.COMPLETE
    assert [row["tax_prd"] for row in result.rows] == [202412, 202312, 202212, 200812]
    assert [row["form"] for row in result.rows] == ["990", "990", "990", "990R"]
    assert all(list(row) == list(FILING_FIELDS) for row in result.rows)
    newest, extracted = result.rows[0], result.rows[1]
    assert newest["financial_data"] == "pdf only"
    assert newest["totrevenue"] is None and newest["updated"] is None
    assert extracted["financial_data"] == "extracted"
    assert extracted["totrevenue"] == 26474112
    assert "totcntrbgfts" not in result.rows[2]
    assert result.dataset_name == (
        "ProPublica Nonprofit Explorer filings: Mission Economic Development Agency "
        "(EIN 51-0187791, San Francisco, CA)"
    )
    assert result.source_url.endswith("/nonprofits/organizations/510187791")
    assert result.source_updated_at is None
    assert found.warnings[0] == TAX_PERIOD_WARNING
    assert PDF_ONLY_WARNING in found.warnings
    assert found.warnings[-1] == LAG_WARNING
    assert found.rules == [CITATION_RULE]


@pytest.mark.asyncio
async def test_a_leading_zero_ein_keeps_its_digits_in_the_citation() -> None:
    explorer, requests = explorer_answering(
        always(httpx.Response(200, json=FILINGS | {"filings_without_data": []}))
    )

    found = await explorer.get_filings(ein="042103580").run()

    assert requests[0].url.path == "/nonprofits/api/v2/organizations/42103580.json"
    assert "EIN 04-2103580" in found.result.dataset_name
    assert PDF_ONLY_WARNING not in found.warnings


@pytest.mark.parametrize(
    "ein", ["94-606856", "9460685641", "94 6068564", "abc", "000000000", "٩٤٦٠٦٨٥٦٤"]
)
def test_an_invalid_ein_is_refused_before_any_request(ein: str) -> None:
    explorer, requests = explorer_answering(always(httpx.Response(200, json=FILINGS)))

    with pytest.raises(SourceQueryError) as caught:
        explorer.get_filings(ein=ein)

    assert caught.value.argument == "ein"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(404, json={"error": "Organization not found"}), "NOT_FOUND"),
        (
            httpx.Response(
                200,
                json=FILINGS
                | {
                    "organization": {
                        "ein": 999999999,
                        "name": "Unknown Organization",
                        "state": None,
                    },
                    "filings_with_data": [],
                },
            ),
            "NOT_FOUND",
        ),
        (httpx.Response(500, text="oops"), "SOURCE_UNAVAILABLE"),
        (httpx.Response(429, json={"error": "slow down"}), "SOURCE_UNAVAILABLE"),
    ],
)
async def test_missing_organizations_and_failures_become_tool_errors(
    response: httpx.Response, code: str
) -> None:
    explorer, _ = explorer_answering(always(response))

    with pytest.raises(SourceQueryError) as caught:
        await explorer.get_filings(ein="99-9999999").run()

    assert caught.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        FILINGS | {"organization": None},
        FILINGS | {"filings_with_data": None},
        FILINGS | {"filings_with_data": [{"tax_prd": "202312"}]},
        FILINGS | {"filings_without_data": [{"tax_prd": True}]},
    ],
)
async def test_a_malformed_filing_list_is_a_source_error(body: dict) -> None:
    explorer, _ = explorer_answering(always(httpx.Response(200, json=body)))

    with pytest.raises(SourceError) as caught:
        await explorer.get_filings(ein="51-0187791").run()

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"organizations": [], "num_pages": 0},
        {"organizations": [{"ein": "1"}], "num_pages": 1, "total_results": 1},
        {"organizations": None, "num_pages": 1, "total_results": 1},
        [],
    ],
)
async def test_a_malformed_search_is_a_source_error(body: object) -> None:
    explorer, _ = explorer_answering(always(httpx.Response(200, json=body)))

    with pytest.raises(SourceError):
        await explorer.search_nonprofits(name="Harvard").run()


@pytest.mark.asyncio
async def test_a_404_without_a_result_body_is_an_outage() -> None:
    explorer, _ = explorer_answering(always(httpx.Response(404, text="<html>gone</html>")))

    with pytest.raises(SourceQueryError) as caught:
        await explorer.search_nonprofits(name="Harvard").run()

    assert caught.value.code == "SOURCE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_a_capped_total_reads_as_at_least() -> None:
    capped = SEARCH | {"total_results": 10000, "num_pages": 400}
    explorer, requests = explorer_answering(always(httpx.Response(200, json=capped)))

    found = await explorer.search_nonprofits(name="Mission | Housing* (Harv~)").run()

    assert requests[0].url.params["q"] == "Mission Housing Harv"
    assert "at least 10,000 organizations match" in found.result.dataset_name
    assert found.warnings[0].startswith("at least 10,000 organizations match")


@pytest.mark.asyncio
async def test_no_listed_filings_escalate_before_anything_else() -> None:
    # checked 2026-11-21: EIN 51-0183500 had both filing lists empty.
    explorer, _ = explorer_answering(
        always(
            httpx.Response(
                200, json=FILINGS | {"filings_with_data": [], "filings_without_data": []}
            )
        )
    )

    found = await explorer.get_filings(ein="51-0183500").run()

    assert found.result.returned_row_count == 0
    assert found.warnings[0] == NO_FILINGS_WARNING
    assert found.rules[0] == NO_FILINGS_RULE
    assert found.escalations == ["NO_FILINGS_LISTED"]


@pytest.mark.asyncio
async def test_document_labels_share_the_form_spelling() -> None:
    labels = [
        {"tax_prd": 202412, "tax_prd_yr": 2024, "formtype": 2, "formtype_str": "990PF"},
        {"tax_prd": 202312, "tax_prd_yr": 2023, "formtype": 1, "formtype_str": "990EZ"},
        {"tax_prd": 202212, "tax_prd_yr": 2022, "formtype": 0, "formtype_str": "990T"},
    ]
    explorer, _ = explorer_answering(
        always(
            httpx.Response(
                200, json=FILINGS | {"filings_with_data": [], "filings_without_data": labels}
            )
        )
    )

    found = await explorer.get_filings(ein="51-0187791").run()

    assert [row["form"] for row in found.result.rows] == ["990-PF", "990-EZ", "990T"]
