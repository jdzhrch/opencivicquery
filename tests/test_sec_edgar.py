from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from opencivicquery import __version__
from opencivicquery.models import Completeness
from opencivicquery.sources.base import SourceError, SourceQueryError
from opencivicquery.sources.sec_edgar import (
    CONTENT_WARNING,
    DATES_WARNING,
    FORM_TYPE_WARNING,
    FRESHNESS_WARNING,
    ONE_CIK_WARNING,
    OTHER_FILERS_WARNING,
    ROW_GRAIN,
    SUBMISSIONS_HOST,
    TICKERS_TTL_SECONDS,
    TICKERS_URL,
    SecEdgar,
)

CONTACT = "test@example.org"

# Entries from the live ticker file on 2026-11-20. SUN is Sunoco's ticker and also the
# exact name of another company; WISeQey Corp. is the name of two CIKs.
TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1418121, "ticker": "APLE", "title": "Apple Hospitality REIT, Inc."},
    "2": {"cik_str": 63330, "ticker": "MLP", "title": "MAUI LAND & PINEAPPLE CO INC"},
    "3": {"cik_str": 1552275, "ticker": "SUN", "title": "Sunoco LP"},
    "4": {"cik_str": 2070845, "ticker": "SUNI", "title": "SUN"},
    "5": {"cik_str": 2107968, "ticker": "WQEY", "title": "WISeQey Corp."},
    "6": {"cik_str": 1738699, "ticker": "WSKEF", "title": "WISeQey Corp."},
    "7": {"cik_str": 1652044, "ticker": "GOOGL", "title": "Alphabet Inc."},
    "8": {"cik_str": 1652044, "ticker": "GOOG", "title": "Alphabet Inc."},
}

# Trimmed from Apple's live submissions file on 2026-11-20, newest first as EDGAR lists them.
FILINGS = [
    {
        "accessionNumber": "0001140361-26-044952",
        "filingDate": "2026-11-19",
        "reportDate": "2026-11-17",
        "form": "4",
        "primaryDocument": "xslF345X06/form4.xml",
        "primaryDocDescription": "FORM 4",
    },
    {
        "accessionNumber": "0001921094-26-001188",
        "filingDate": "2026-11-16",
        "reportDate": "",
        "form": "144",
        "primaryDocument": "xsl144X01/primary_doc.xml",
        "primaryDocDescription": "",
    },
    {
        "accessionNumber": "0000320193-26-000026",
        "filingDate": "2026-11-03",
        "reportDate": "2026-09-26",
        "form": "10-K",
        "primaryDocument": "aapl-20260926.htm",
        "primaryDocDescription": "10-K",
    },
    {
        "accessionNumber": "0000320193-26-000020",
        "filingDate": "2026-10-30",
        "reportDate": "2026-10-30",
        "form": "8-K",
        "primaryDocument": "aapl-20261030.htm",
        "primaryDocDescription": "8-K",
    },
    {
        "accessionNumber": "0000320193-25-000079",
        "filingDate": "2025-10-31",
        "reportDate": "2025-09-27",
        "form": "10-K",
        "primaryDocument": "aapl-20250927.htm",
        "primaryDocDescription": "10-K",
    },
    {
        "accessionNumber": "0000320193-24-000123",
        "filingDate": "2024-11-01",
        "reportDate": "2024-09-28",
        "form": "10-K",
        "primaryDocument": "aapl-20240928.htm",
        "primaryDocDescription": "10-K",
    },
]
OLDER_FILE = {
    "name": "CIK0000320193-submissions-001.json",
    "filingCount": 1278,
    "filingFrom": "1994-01-26",
    "filingTo": "2024-10-31",
}


def submissions(
    filings: list[dict[str, Any]] | None = None,
    *,
    files: list[Any] | None = None,
    cik: str = "0000320193",
    name: str = "Apple Inc.",
) -> dict[str, Any]:
    filings = FILINGS if filings is None else filings
    columns = [*FILINGS[0], "acceptanceDateTime", "items", "size"]
    recent = {column: [filing.get(column, "") for filing in filings] for column in columns}
    return {
        "cik": cik,
        "name": name,
        "tickers": ["AAPL"],
        "filings": {"recent": recent, "files": files or []},
    }


def edgar_answering(
    routes: dict[str, httpx.Response | Exception] | None = None,
    *,
    contact: str | None = CONTACT,
    clock: Any = time.monotonic,
    min_request_interval: float = 0.0,
) -> tuple[SecEdgar, list[httpx.Request]]:
    answers = {
        TICKERS_URL: httpx.Response(200, json=TICKERS),
        "https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(
            200, json=submissions()
        ),
        **(routes or {}),
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        answer = answers.get(str(request.url))
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return httpx.Response(404, content=b"<Error><Code>NoSuchKey</Code></Error>")
        return answer

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    edgar = SecEdgar(
        contact=contact, client=client, clock=clock, min_request_interval=min_request_interval
    )
    return edgar, requests


@pytest.mark.asyncio
@pytest.mark.parametrize("contact", [None, "", "   "])
async def test_without_a_contact_the_server_refuses_before_any_request(
    contact: str | None,
) -> None:
    edgar, requests = edgar_answering(contact=contact)

    with pytest.raises(SourceQueryError) as caught:
        edgar.find_filings(company="AAPL")

    assert caught.value.code == "SOURCE_NOT_CONFIGURED"
    assert caught.value.public_message == (
        "This server has no SEC contact configured, so it cannot query EDGAR."
    )
    assert requests == []


def test_a_contact_that_cannot_be_a_header_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="printable ASCII"):
        SecEdgar(contact="test@example.org\x00")


@pytest.mark.asyncio
async def test_every_request_declares_the_contact_in_the_user_agent() -> None:
    edgar, requests = edgar_answering()

    await edgar.find_filings(company="AAPL").run()

    assert [str(request.url) for request in requests] == [
        TICKERS_URL,
        "https://data.sec.gov/submissions/CIK0000320193.json",
    ]
    for request in requests:
        assert request.method == "GET"
        assert request.headers["User-Agent"] == f"OpenCivicQuery/{__version__} {CONTACT}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "argument"),
    [
        ({"company": ""}, "company"),
        ({"company": "   "}, "company"),
        ({"company": "x" * 151}, "company"),
        ({"company": "Apple\x00"}, "company"),
        ({"company": "Apple​ Inc."}, "company"),
        ({"company": 320193}, "company"),
        ({"company": "12345678901"}, "company"),
        ({"company": "0000000000"}, "company"),
        ({"company": "AAPL", "form_types": "10-K"}, "form_types"),
        ({"company": "AAPL", "form_types": ["10-K'; DROP"]}, "form_types"),
        ({"company": "AAPL", "form_types": [""]}, "form_types"),
        ({"company": "AAPL", "form_types": ["X" * 26]}, "form_types"),
        ({"company": "AAPL", "form_types": ["10-Kı"]}, "form_types"),
        ({"company": "AAPL", "form_types": [10]}, "form_types"),
        ({"company": "AAPL", "form_types": ["4"] * 21}, "form_types"),
        ({"company": "AAPL", "filed_after": "2026-13-01"}, "filed_after"),
        ({"company": "AAPL", "filed_after": "20260101"}, "filed_after"),
        ({"company": "AAPL", "filed_after": "2026-01-01\n"}, "filed_after"),
        ({"company": "AAPL", "filed_before": "2026/01/01"}, "filed_before"),
        (
            {"company": "AAPL", "filed_after": "2026-02-01", "filed_before": "2026-01-01"},
            "filed_after",
        ),
        ({"company": "AAPL", "limit": 0}, "limit"),
        ({"company": "AAPL", "limit": 101}, "limit"),
        ({"company": "AAPL", "limit": True}, "limit"),
        ({"company": "AAPL", "limit": "5"}, "limit"),
    ],
)
async def test_an_invalid_argument_never_reaches_the_source(
    arguments: dict[str, Any], argument: str
) -> None:
    edgar, requests = edgar_answering()

    with pytest.raises(SourceQueryError) as caught:
        edgar.find_filings(**arguments)

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert caught.value.argument == argument
    assert requests == []


def test_preparing_records_normalized_arguments_for_the_data_host() -> None:
    edgar, requests = edgar_answering()

    prepared = edgar.find_filings(
        company="  Apple \n Inc. ", form_types=["10-k", "10-K", " def  14a "], limit=5
    )

    assert prepared.host == SUBMISSIONS_HOST == "data.sec.gov"
    assert prepared.arguments == {
        "company": "Apple Inc.",
        "form_types": ["10-K", "DEF 14A"],
        "filed_after": None,
        "filed_before": None,
        "limit": 5,
    }
    # These names belong to the audit record, so a lookup's own arguments may not use them.
    assert not {"domain", "query", "query_url", "status", "completeness", "portal"}.intersection(
        prepared.arguments
    )
    assert requests == []


@pytest.mark.asyncio
async def test_a_cik_is_zero_padded_and_skips_the_ticker_file() -> None:
    edgar, requests = edgar_answering()

    found = await edgar.find_filings(company="320193").run()

    assert [str(request.url) for request in requests] == [
        "https://data.sec.gov/submissions/CIK0000320193.json"
    ]
    result = found.result
    assert result.domain == SUBMISSIONS_HOST
    assert result.dataset_id == "submissions/CIK0000320193"
    assert result.dataset_name == "SEC EDGAR filings: Apple Inc. (CIK 0000320193)"
    assert result.source_url == "https://www.sec.gov/edgar/browse/?CIK=320193"
    assert result.query_url == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert result.query == "company=320193&cik=0000320193&limit=25"
    assert result.source_updated_at is None
    assert found.row_grain == ROW_GRAIN
    # A CIK needs no name match, so no ticker-file warning applies.
    assert not any("ticker file" in warning for warning in found.warnings)


@pytest.mark.asyncio
@pytest.mark.parametrize("company", ["AAPL", "aapl", "  apple   INC. ", "0000320193"])
async def test_a_ticker_or_exact_name_resolves_to_its_cik(company: str) -> None:
    edgar, requests = edgar_answering()

    found = await edgar.find_filings(company=company).run()

    assert str(requests[-1].url) == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert found.result.returned_row_count == len(FILINGS)


@pytest.mark.asyncio
async def test_a_resolved_name_is_named_in_a_warning() -> None:
    edgar, _ = edgar_answering()

    found = await edgar.find_filings(company="aapl").run()

    assert found.warnings[0] == (
        "OpenCivicQuery matched aapl to CIK 0000320193, Apple Inc., through the SEC's "
        "ticker file. The SEC says it updates its ticker file periodically but does not "
        "guarantee its accuracy or scope. Confirm that this is the company asked about."
    )


@pytest.mark.asyncio
async def test_the_ticker_file_is_cached_until_it_is_a_day_old() -> None:
    now = [1000.0]
    edgar, requests = edgar_answering(clock=lambda: now[0])

    await edgar.find_filings(company="AAPL").run()
    now[0] += TICKERS_TTL_SECONDS - 1
    await edgar.find_filings(company="Apple Inc.").run()
    assert [str(request.url) for request in requests].count(TICKERS_URL) == 1

    now[0] += 1
    await edgar.find_filings(company="AAPL").run()
    assert [str(request.url) for request in requests].count(TICKERS_URL) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("company", "candidates"),
    [
        ("sun", ["Sunoco LP (CIK 0001552275, ticker SUN)", "SUN (CIK 0002070845, ticker SUNI)"]),
        (
            "wiseqey corp.",
            [
                "WISeQey Corp. (CIK 0002107968, ticker WQEY)",
                "WISeQey Corp. (CIK 0001738699, ticker WSKEF)",
            ],
        ),
    ],
)
async def test_an_input_that_names_two_companies_is_never_guessed(
    company: str, candidates: list[str]
) -> None:
    edgar, requests = edgar_answering()

    with pytest.raises(SourceQueryError) as caught:
        await edgar.find_filings(company=company).run()

    assert caught.value.code == "AMBIGUOUS_COMPANY"
    assert caught.value.argument == "company"
    assert caught.value.public_message == (
        f"{company} matches more than one company in the SEC's ticker file: "
        f"{'; '.join(candidates)}. Give the CIK of the one you mean."
    )
    assert [str(request.url) for request in requests] == [TICKERS_URL]


@pytest.mark.asyncio
async def test_a_near_name_lists_close_candidates_and_the_ticker_file_limit() -> None:
    edgar, requests = edgar_answering()

    with pytest.raises(SourceQueryError) as caught:
        await edgar.find_filings(company="Apple").run()

    assert caught.value.code == "NOT_FOUND"
    assert caught.value.argument == "company"
    assert caught.value.public_message == (
        "No ticker or company name in the SEC's ticker file equals Apple. Close matches (3): "
        "Apple Inc. (CIK 0000320193, ticker AAPL); Apple Hospitality REIT, Inc. "
        "(CIK 0001418121, ticker APLE); MAUI LAND & PINEAPPLE CO INC (CIK 0000063330, "
        "ticker MLP). Give the CIK of the one you mean, or its exact name. The SEC's ticker "
        "file lists only companies with a ticker symbol. For any other filer, find its CIK "
        "with EDGAR search at https://www.sec.gov/search-filings and give the CIK."
    )
    assert [str(request.url) for request in requests] == [TICKERS_URL]


@pytest.mark.asyncio
async def test_candidates_are_capped_at_ten_and_one_cik_appears_once() -> None:
    many = {
        str(index): {"cik_str": 5000 + index, "ticker": f"P{index}", "title": f"Pine {index}"}
        for index in range(14)
    }
    many["99"] = {"cik_str": 5000, "ticker": "P0B", "title": "Pine 0 Class B"}
    edgar, _ = edgar_answering({TICKERS_URL: httpx.Response(200, json=many)})

    with pytest.raises(SourceQueryError) as caught:
        await edgar.find_filings(company="pine").run()

    message = caught.value.public_message
    assert "Close matches (10 of 14)" in message
    assert message.count("(CIK ") == 10
    assert "Pine 0 Class B" not in message


@pytest.mark.asyncio
async def test_no_match_at_all_points_to_edgar_search() -> None:
    edgar, _ = edgar_answering()

    with pytest.raises(SourceQueryError) as caught:
        await edgar.find_filings(company="Zebra Holdings").run()

    assert caught.value.code == "NOT_FOUND"
    assert caught.value.public_message.startswith(
        "No ticker or company name in the SEC's ticker file equals Zebra Holdings. The SEC's "
        "ticker file lists only companies with a ticker symbol."
    )


@pytest.mark.asyncio
async def test_form_and_date_filters_are_exact_and_inclusive() -> None:
    edgar, _ = edgar_answering()

    found = await edgar.find_filings(
        company="320193",
        form_types=["10-k"],
        filed_after="2025-10-31",
        filed_before="2026-11-03",
    ).run()

    result = found.result
    assert [row["accessionNumber"] for row in result.rows] == [
        "0000320193-26-000026",
        "0000320193-25-000079",
    ]
    assert result.completeness == Completeness.COMPLETE
    assert result.query == (
        "company=320193&cik=0000320193&form_types=10-K&filed_after=2025-10-31"
        "&filed_before=2026-11-03&limit=25"
    )
    assert FORM_TYPE_WARNING in found.warnings


@pytest.mark.asyncio
async def test_rows_come_newest_first_even_if_the_source_order_changes() -> None:
    shuffled = [FILINGS[4], FILINGS[0], FILINGS[5], FILINGS[2]]
    edgar, _ = edgar_answering(
        {
            "https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(
                200, json=submissions(shuffled)
            )
        }
    )

    found = await edgar.find_filings(company="320193").run()

    assert [row["filingDate"] for row in found.result.rows] == [
        "2026-11-19",
        "2026-11-03",
        "2025-10-31",
        "2024-11-01",
    ]


@pytest.mark.asyncio
async def test_rows_carry_source_fields_and_built_urls() -> None:
    edgar, _ = edgar_answering()

    found = await edgar.find_filings(company="320193", form_types=["4", "144"]).run()

    assert found.result.rows == [
        {
            "form": "4",
            "filingDate": "2026-11-19",
            "reportDate": "2026-11-17",
            "accessionNumber": "0001140361-26-044952",
            "primaryDocument": "xslF345X06/form4.xml",
            "primaryDocDescription": "FORM 4",
            "filingIndexUrl": (
                "https://www.sec.gov/Archives/edgar/data/320193/000114036126044952/"
                "0001140361-26-044952-index.htm"
            ),
            "primaryDocumentUrl": (
                "https://www.sec.gov/Archives/edgar/data/320193/000114036126044952/"
                "xslF345X06/form4.xml"
            ),
        },
        {
            "form": "144",
            "filingDate": "2026-11-16",
            "reportDate": None,
            "accessionNumber": "0001921094-26-001188",
            "primaryDocument": "xsl144X01/primary_doc.xml",
            "primaryDocDescription": None,
            "filingIndexUrl": (
                "https://www.sec.gov/Archives/edgar/data/320193/000192109426001188/"
                "0001921094-26-001188-index.htm"
            ),
            "primaryDocumentUrl": (
                "https://www.sec.gov/Archives/edgar/data/320193/000192109426001188/"
                "xsl144X01/primary_doc.xml"
            ),
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "url"),
    [
        ("", None),
        ("../../other.htm", None),
        ("/etc/passwd", None),
        (
            "report #1?.htm",
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/"
            "report%20%231%3F.htm",
        ),
    ],
)
async def test_a_document_link_never_leaves_the_filing_folder(
    document: str, url: str | None
) -> None:
    filing = {**FILINGS[2], "primaryDocument": document}
    edgar, _ = edgar_answering(
        {
            "https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(
                200, json=submissions([filing])
            )
        }
    )

    found = await edgar.find_filings(company="320193").run()

    assert found.result.rows[0]["primaryDocumentUrl"] == url


@pytest.mark.asyncio
async def test_more_matches_than_the_limit_is_truncated_newest_first() -> None:
    edgar, _ = edgar_answering()

    found = await edgar.find_filings(company="320193", limit=2).run()

    result = found.result
    assert result.completeness == Completeness.TRUNCATED
    assert result.returned_row_count == 2
    assert [row["filingDate"] for row in result.rows] == ["2026-11-19", "2026-11-16"]
    assert found.warnings[0] == (
        "6 filings in the block that was read match the request; this result shows the "
        "newest 2. Raise limit (up to 100) or narrow the dates or form types to see the rest."
    )


@pytest.mark.asyncio
async def test_a_range_that_reaches_unread_older_files_is_truncated() -> None:
    newer = {**OLDER_FILE, "filingFrom": "2024-10-31", "filingTo": "2024-10-31"}
    edgar, requests = edgar_answering(
        {
            "https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(
                200, json=submissions(files=[newer, OLDER_FILE])
            )
        }
    )

    found = await edgar.find_filings(company="320193", form_types=["10-K"]).run()

    result = found.result
    assert result.completeness == Completeness.TRUNCATED
    assert result.returned_row_count == 3
    assert found.warnings[0] == (
        "PARTIAL: this tool read only EDGAR's recent filings block for this company, which "
        "starts at 2024-11-01. 2,556 older filings filed from 1994-01-26 to 2024-10-31, in 2 "
        "additional files that overlap the requested dates, were not read. Do not say a "
        "filing does not exist unless it would fall inside the block that was read."
    )
    # The older files are named, never fetched.
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_a_range_inside_the_recent_block_is_complete_despite_older_files() -> None:
    edgar, _ = edgar_answering(
        {
            "https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(
                200, json=submissions(files=[OLDER_FILE])
            )
        }
    )

    found = await edgar.find_filings(company="320193", filed_after="2024-11-01").run()

    assert found.result.completeness == Completeness.COMPLETE
    assert not any(warning.startswith("PARTIAL") for warning in found.warnings)
    assert (
        "This list comes from EDGAR's recent filings block for this company, which starts at "
        "2024-11-01. EDGAR's recent filings block holds at least one year of filings or 1,000 "
        "filings, whichever is more. Older filings sit in additional files outside the "
        "requested dates."
    ) in found.warnings


@pytest.mark.asyncio
async def test_an_older_file_without_readable_dates_counts_as_unread() -> None:
    edgar, _ = edgar_answering(
        {
            "https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(
                200, json=submissions(files=[{"name": "CIK0000320193-submissions-001.json"}])
            )
        }
    )

    found = await edgar.find_filings(company="320193", filed_after="2026-01-01").run()

    assert found.result.completeness == Completeness.TRUNCATED
    assert found.warnings[0].startswith(
        "PARTIAL: this tool read only EDGAR's recent filings block for this company, which "
        "starts at 2024-11-01. Its older filings, in 1 additional files"
    )


@pytest.mark.asyncio
async def test_a_complete_list_carries_every_standing_warning_in_order() -> None:
    edgar, _ = edgar_answering()

    found = await edgar.find_filings(company="320193").run()

    assert found.result.completeness == Completeness.COMPLETE
    assert found.warnings == [
        OTHER_FILERS_WARNING,
        CONTENT_WARNING,
        DATES_WARNING,
        "This list comes from EDGAR's recent filings block for this company, which starts at "
        "2024-11-01. EDGAR's recent filings block holds at least one year of filings or 1,000 "
        "filings, whichever is more. EDGAR lists no older filings for this CIK.",
        ONE_CIK_WARNING,
        FRESHNESS_WARNING,
    ]
    assert found.rules == []


@pytest.mark.asyncio
async def test_column_names_feed_the_schema_hash() -> None:
    edgar, _ = edgar_answering()
    first = await edgar.find_filings(company="320193").run()
    payload = submissions()
    del payload["filings"]["recent"]["size"]
    edgar, _ = edgar_answering(
        {"https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(200, json=payload)}
    )
    second = await edgar.find_filings(company="320193").run()

    assert first.result.schema_sha256 != second.result.schema_sha256


@pytest.mark.asyncio
async def test_an_unknown_cik_is_a_clear_not_found() -> None:
    edgar, _ = edgar_answering()

    with pytest.raises(SourceQueryError) as caught:
        await edgar.find_filings(company="9999999999").run()

    assert caught.value.tool_error() == {
        "code": "NOT_FOUND",
        "message": (
            "EDGAR has no filing list for CIK 9999999999. Check the number with EDGAR search at "
            "https://www.sec.gov/search-filings."
        ),
        "argument": "company",
        "http_status": 404,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 503])
async def test_an_unavailable_source_is_reported_with_its_status(status: int) -> None:
    edgar, _ = edgar_answering(
        {
            "https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(
                status, text="<html>Request Rate Threshold Exceeded</html>"
            )
        }
    )

    with pytest.raises(SourceQueryError) as caught:
        await edgar.find_filings(company="320193").run()

    assert caught.value.code == "SOURCE_UNAVAILABLE"
    assert caught.value.http_status == status
    assert caught.value.public_message == (
        f"The SEC's EDGAR system could not complete the request (HTTP {status})."
    )


@pytest.mark.asyncio
async def test_a_redirect_is_not_followed() -> None:
    edgar, requests = edgar_answering(
        {
            "https://data.sec.gov/submissions/CIK0000320193.json": httpx.Response(
                301, headers={"Location": "https://example.com/elsewhere"}
            )
        }
    )

    with pytest.raises(SourceQueryError) as caught:
        await edgar.find_filings(company="320193").run()

    assert caught.value.http_status == 301
    assert len(requests) == 1


def _broken(change: Any) -> dict[str, Any]:
    payload = submissions()
    change(payload)
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        _broken(lambda p: p.pop("filings")),
        _broken(lambda p: p["filings"].pop("recent")),
        _broken(lambda p: p["filings"]["recent"].pop("form")),
        _broken(lambda p: p["filings"]["recent"]["form"].pop()),
        _broken(lambda p: p["filings"]["recent"]["accessionNumber"].__setitem__(0, "x")),
        _broken(lambda p: p["filings"]["recent"]["accessionNumber"].__setitem__(0, None)),
        _broken(lambda p: p["filings"]["recent"]["filingDate"].__setitem__(0, "Nov 19")),
        _broken(lambda p: p["filings"]["recent"]["form"].__setitem__(0, "")),
        # EDGAR always sends files, even empty, so its absence means a changed format.
        _broken(lambda p: p["filings"].pop("files")),
        _broken(lambda p: p["filings"].__setitem__("files", "none")),
        _broken(lambda p: p["filings"].__setitem__("files", ["x"])),
        # An answer about another filer, or one with no name, is never frozen.
        _broken(lambda p: p.__setitem__("cik", "0000789019")),
        _broken(lambda p: p.__setitem__("cik", 320193)),
        _broken(lambda p: p.pop("name")),
    ],
)
async def test_a_malformed_filing_list_is_a_source_error(body: bytes | dict[str, Any]) -> None:
    response = (
        httpx.Response(200, content=body)
        if isinstance(body, bytes)
        else httpx.Response(200, json=body)
    )
    edgar, _ = edgar_answering({"https://data.sec.gov/submissions/CIK0000320193.json": response})

    with pytest.raises(SourceError) as caught:
        await edgar.find_filings(company="320193").run()

    assert not isinstance(caught.value, SourceQueryError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tickers",
    [
        {},
        [],
        {"0": "AAPL"},
        {"0": {"cik_str": "320193", "ticker": "AAPL", "title": "Apple Inc."}},
        {"0": {"cik_str": True, "ticker": "AAPL", "title": "Apple Inc."}},
        {"0": {"cik_str": 320193, "ticker": None, "title": "Apple Inc."}},
    ],
)
async def test_a_malformed_ticker_file_is_a_source_error(tickers: Any) -> None:
    edgar, requests = edgar_answering({TICKERS_URL: httpx.Response(200, json=tickers)})

    with pytest.raises(SourceError) as caught:
        await edgar.find_filings(company="AAPL").run()

    assert not isinstance(caught.value, SourceQueryError)
    assert [str(request.url) for request in requests] == [TICKERS_URL]


@pytest.mark.asyncio
async def test_a_stalled_source_is_a_timeout_the_caller_can_retry() -> None:
    edgar, _ = edgar_answering(
        {"https://data.sec.gov/submissions/CIK0000320193.json": httpx.ReadTimeout("stalled")}
    )

    with pytest.raises(SourceQueryError) as caught:
        await edgar.find_filings(company="320193").run()

    assert caught.value.code == "SOURCE_TIMEOUT"


@pytest.mark.asyncio
async def test_requests_are_spaced_to_respect_the_sec_rate_limit() -> None:
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(time.monotonic())
        if str(request.url) == TICKERS_URL:
            return httpx.Response(200, json=TICKERS)
        return httpx.Response(200, json=submissions())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    edgar = SecEdgar(contact=CONTACT, client=client, min_request_interval=0.05)

    await edgar.find_filings(company="AAPL").run()
    await edgar.find_filings(company="320193").run()

    assert len(starts) == 3
    assert all(later - earlier >= 0.045 for earlier, later in zip(starts, starts[1:], strict=False))


@pytest.mark.asyncio
async def test_closing_leaves_a_borrowed_client_open() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    await SecEdgar(contact=CONTACT, client=client).close()
    assert client.is_closed is False
    await client.aclose()
