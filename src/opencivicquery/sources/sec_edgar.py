"""SEC EDGAR: one company's list of filings from the SEC's submissions API.

It needs no key, but the SEC asks every automated client to declare a contact in its
User-Agent header, so the server operator supplies one. Every request goes to a URL this
module builds on www.sec.gov or data.sec.gov; the caller names only a company.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from opencivicquery import __version__
from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources.base import Lookup, PreparedLookup, SourceError, SourceQueryError
from opencivicquery.sources.http import FixedHostClient

SUBMISSIONS_HOST = "data.sec.gov"
"""The host that serves filing lists. It doubles as the per-minute usage bucket."""
SOURCE_NAME = "SEC's EDGAR system"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
# official: sec.gov/os/accessing-edgar-data, "company_tickers.json: data file for ticker,
# CIK, EDGAR conformed company name associations."
SEARCH_PAGE = "https://www.sec.gov/search-filings"
# checked 2026-11-20: sec.gov/edgar/searchedgar/companysearch redirects here.

TICKERS_TTL_SECONDS = 24 * 60 * 60
MIN_REQUEST_INTERVAL_SECONDS = 0.125
# official: sec.gov/os/accessing-edgar-data, "Current max request rate: 10
# requests/second." The developer resources page adds that the SEC may block IP addresses
# that submit excessive requests, so this process starts at most 8 requests a second.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# checked 2026-11-20: the Goldman Sachs Group submissions file (16,647 recent filings)
# was 2.6 MB and the ticker file was 0.8 MB.

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_COMPANY_LENGTH = 150
MAX_FORM_TYPES = 20
MAX_CANDIDATES = 10

_DIGITS = re.compile(r"[0-9]+")
# official: sec.gov/search-filings/edgar-application-programming-interfaces, "the
# entity's 10-digit central index key (CIK), including leading zeros".
_FORM_TYPE = re.compile(r"[A-Z0-9][A-Z0-9 ./-]{0,24}")
# checked 2026-11-20: form types in live lists use capitals, digits, spaces, hyphens,
# slashes, and periods, such as 10-K/A, DEF 14A, S-8 POS, and SCHEDULE 13G/A.
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
_COLUMNS = (
    "accessionNumber",
    "filingDate",
    "reportDate",
    "form",
    "primaryDocument",
    "primaryDocDescription",
)

ROW_GRAIN = (
    "One row is one EDGAR submission, identified by its accession number, in the filing "
    "list of one company's CIK."
)
# official: sec.gov/os/accessing-edgar-data, the accession number is "a unique identifier
# assigned automatically to an accepted submission by EDGAR".

RECENT_BLOCK_NOTE = (
    "EDGAR's recent filings block holds at least one year of filings or 1,000 filings, "
    "whichever is more."
)
# official: sec.gov/search-filings/edgar-application-programming-interfaces, "at least one
# year's of filing or to 1,000 (whichever is more) of the most recent filings"; "If the
# entity has additional filings, files will contain an array of additional JSON files".
# checked 2026-11-20: Apple's block held 1,005 filings back to 2015-10-07; Goldman Sachs
# Group's held 16,647 filings back to 2025-11-20, with 39 older files.

OTHER_FILERS_WARNING = (
    "This list includes filings that others submitted about the company, such as Form 4 "
    "filings by insiders. Open the filing index to see who filed before you attribute a "
    "filing to the company."
)
# checked 2026-11-20: Apple's list held 598 Form 4, 51 Form 144, and 22 SC 13G/A filings.
# The filing index of Form 4 0001140361-26-044952 names Apple Inc. as Issuer and a
# separate CIK as Reporting. official: sec.gov/about/webmaster-frequently-asked-questions,
# the first 10 digits of an accession number are the CIK of the submitter, which "could be
# the company or a third-party filer agent".

CONTENT_WARNING = (
    "A row shows only that a filing was submitted. Read the document before you describe "
    "what it says. EDGAR lists an amendment under its own form type, such as 10-K/A or "
    "8-K/A, so check for one before you rely on the original."
)
# checked 2026-11-20: Apple's list showed 8-K/A, 4/A, 3/A, 144/A, and SC 13G/A as form
# types of their own.

FORM_TYPE_WARNING = (
    "Form types match exactly, so a filter for 10-K leaves out 10-K/A. EDGAR has also "
    "renamed some form types: Apple's list shows Schedule 13G amendments as SC 13G/A "
    "through 2024-02-14 and as SCHEDULE 13G/A from 2025-07-29. Include every name that "
    "applies to the years asked about."
)
# checked 2026-11-20 in Apple's live filing list.

DATES_WARNING = (
    "The date filters apply to filingDate, the official filing date EDGAR assigned, which "
    "a post-acceptance correction can change. reportDate is the end of the period the "
    "filing reports on, such as the fiscal year a 10-K covers, and it is blank for many "
    "filings. Say which date you mean."
)
# official: sec.gov/about/webmaster-frequently-asked-questions, "How can I understand
# EDGAR Timestamps?": "FILED AS OF DATE: EDGAR assigned official filing date, or post
# acceptance new filing date"; "CONFORMED PERIOD OF REPORT: End date of reporting period
# of filing." checked 2026-11-20: Apple's 10-K filed 2026-11-03 has reportDate
# 2026-09-26, and 226 of its 1,005 recent filings have no reportDate.

ONE_CIK_WARNING = (
    "This list covers one CIK. A related company, such as a predecessor or a parent, can "
    "file under its own CIK; for example, Google Inc. and Alphabet Inc. have separate CIKs "
    "and separate filing lists."
)
# checked 2026-11-20: CIK 0001288776 is GOOGLE INC. with its own filing list; the ticker
# file maps GOOGL to CIK 1652044, Alphabet Inc.

FRESHNESS_WARNING = (
    "EDGAR does not report an update time for this list. The SEC says the submissions API "
    "is updated with a typical processing delay of less than a second."
)
# official: sec.gov/search-filings/edgar-application-programming-interfaces.

TICKER_FILE_NOTE = (
    "The SEC says it updates its ticker file periodically but does not guarantee its "
    "accuracy or scope."
)
# official: sec.gov/about/webmaster-frequently-asked-questions, "Do you have a file that
# maps company ticker / CIK / company name?".

NOT_IN_TICKER_FILE_NOTE = (
    "The SEC's ticker file lists only companies with a ticker symbol. For any other filer, "
    f"find its CIK with EDGAR search at {SEARCH_PAGE} and give the CIK."
)


def _invisible(text: str) -> bool:
    """Control and format characters, such as NUL, ESC, zero-width space, or a bidi override."""
    return any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in text)


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _invalid(argument: str, message: str) -> SourceQueryError:
    return SourceQueryError(
        f"Invalid {argument}",
        code="INVALID_QUERY_ARGUMENT",
        argument=argument,
        public_message=message,
    )


def padded_cik(cik: int) -> str:
    return f"{cik:010d}"


def submissions_url(cik: int) -> str:
    return f"https://{SUBMISSIONS_HOST}/submissions/CIK{padded_cik(cik)}.json"


def company_page_url(cik: int) -> str:
    return f"https://www.sec.gov/edgar/browse/?CIK={cik}"


def filing_index_url(cik: int, accession: str) -> str:
    # official: sec.gov/os/accessing-edgar-data shows
    # /Archives/edgar/data/1122304/0001193125-15-118890-index.html; checked 2026-11-20:
    # .../data/320193/000032019326000026/0000320193-26-000026-index.htm answered 200.
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
        f"{accession}-index.htm"
    )


def document_url(cik: int, accession: str, document: str | None) -> str | None:
    # checked 2026-11-20: .../data/320193/000032019326000026/aapl-20260926.htm answered 200.
    if not document or ".." in document.split("/") or document.startswith("/"):
        return None
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
        f"{quote(document, safe='/')}"
    )


def validate_company(company: Any) -> tuple[str, int | None]:
    """Return the company as it will be matched and, when it is a CIK, the CIK."""
    if not isinstance(company, str):
        raise _invalid("company", "Give company as text.")
    text = " ".join(company.split())
    if not text or len(text) > MAX_COMPANY_LENGTH or _invisible(text):
        raise _invalid(
            "company",
            f"Give one company of 1 to {MAX_COMPANY_LENGTH} characters: a ticker such as "
            "AAPL, a CIK of up to 10 digits such as 320193, or the company name.",
        )
    if _DIGITS.fullmatch(text):
        if len(text) > 10 or int(text) == 0:
            raise _invalid("company", "A CIK has 1 to 10 digits and is not zero.")
        return text, int(text)
    return text, None


def validate_form_types(form_types: Any) -> list[str]:
    if form_types is None:
        return []
    if not isinstance(form_types, list) or len(form_types) > MAX_FORM_TYPES:
        raise _invalid("form_types", f"Give form_types as a list of up to {MAX_FORM_TYPES}.")
    normalized: list[str] = []
    for form in form_types:
        ascii_text = isinstance(form, str) and form.isascii()
        value = " ".join(form.split()).upper() if ascii_text else ""
        if not _FORM_TYPE.fullmatch(value):
            raise _invalid(
                "form_types",
                "Give each form type as EDGAR names it, such as 10-K, 10-K/A, 8-K, 4, or D.",
            )
        if value not in normalized:
            normalized.append(value)
    return normalized


def _parse_date(value: Any) -> date | None:
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def validate_date(argument: str, value: Any) -> date | None:
    if value is None:
        return None
    parsed = _parse_date(value)
    if parsed is None:
        raise _invalid(argument, f"Give {argument} as a date in YYYY-MM-DD form.")
    return parsed


def validate_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise _invalid("limit", f"Give limit as a whole number from 1 to {MAX_LIMIT}.")
    return limit


@dataclass(frozen=True)
class _Listing:
    cik: int
    ticker: str
    title: str
    key: str
    """The normalized ticker and title, for substring matching."""


@dataclass(frozen=True)
class _TickerIndex:
    listings: list[_Listing]
    """Every entry in file order."""
    exact: dict[str, list[int]]
    """Normalized ticker or title to the CIKs it names, in file order."""
    first: dict[int, _Listing]
    """The first entry for each CIK, which names it in an error."""


def _ticker_index(payload: Any) -> _TickerIndex:
    if not isinstance(payload, dict) or not payload:
        raise SourceError("The SEC ticker file is not a non-empty object")
    listings: list[_Listing] = []
    exact: dict[str, list[int]] = {}
    first: dict[int, _Listing] = {}
    for entry in payload.values():
        cik = entry.get("cik_str") if isinstance(entry, dict) else None
        ticker = entry.get("ticker") if isinstance(entry, dict) else None
        title = entry.get("title") if isinstance(entry, dict) else None
        if (
            isinstance(cik, bool)
            or not isinstance(cik, int)
            or cik <= 0
            or not isinstance(ticker, str)
            or not isinstance(title, str)
        ):
            raise SourceError("The SEC ticker file has a malformed entry")
        listing = _Listing(
            cik=cik,
            ticker=ticker,
            title=title,
            key=f"{_normalized(ticker)}\n{_normalized(title)}",
        )
        listings.append(listing)
        first.setdefault(cik, listing)
        for key in {_normalized(ticker), _normalized(title)}:
            ciks = exact.setdefault(key, [])
            if cik not in ciks:
                ciks.append(cik)
    return _TickerIndex(listings=listings, exact=exact, first=first)


def _describe(listings: list[_Listing]) -> str:
    return "; ".join(
        f"{item.title} (CIK {padded_cik(item.cik)}, ticker {item.ticker})" for item in listings
    )


def resolve_company(index: _TickerIndex, company: str) -> int:
    """Return the one CIK whose ticker or name equals the input, or raise with candidates.

    Matching ignores only case and repeated whitespace. It never picks among near matches.
    """
    key = _normalized(company)
    ciks = index.exact.get(key, [])
    if len(ciks) == 1:
        return ciks[0]
    if ciks:
        raise SourceQueryError(
            f"Company {company!r} matches {len(ciks)} CIKs",
            code="AMBIGUOUS_COMPANY",
            argument="company",
            public_message=(
                f"{company} matches more than one company in the SEC's ticker file: "
                f"{_describe([index.first[cik] for cik in ciks[:MAX_CANDIDATES]])}. "
                "Give the CIK of the one you mean."
            ),
        )
    close: list[_Listing] = []
    seen: set[int] = set()
    for item in index.listings:
        if item.cik not in seen and key in item.key:
            seen.add(item.cik)
            close.append(item)
    if close:
        shown = close[:MAX_CANDIDATES]
        count = f"{len(shown)} of {len(close)}" if len(close) > len(shown) else f"{len(close)}"
        found = (
            f"No ticker or company name in the SEC's ticker file equals {company}. "
            f"Close matches ({count}): {_describe(shown)}. Give the CIK of the one you mean, "
            "or its exact name."
        )
    else:
        found = f"No ticker or company name in the SEC's ticker file equals {company}."
    raise SourceQueryError(
        f"Company {company!r} not in the ticker file",
        code="NOT_FOUND",
        argument="company",
        public_message=f"{found} {NOT_IN_TICKER_FILE_NOTE}",
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _recent_rows(cik: int, recent: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn the columnar recent block into rows in the source's field names, plus URLs."""
    if not isinstance(recent, dict):
        raise SourceError("The EDGAR filing list has no recent block")
    columns = [recent.get(name) for name in _COLUMNS]
    if not all(isinstance(column, list) for column in columns):
        raise SourceError("The EDGAR filing list is missing a column")
    lengths = {len(column) for column in columns}
    if len(lengths) != 1:
        raise SourceError("The EDGAR filing list has columns of different lengths")
    rows: list[dict[str, Any]] = []
    for accession, filed, reported, form, document, description in zip(*columns, strict=True):
        if (
            not isinstance(accession, str)
            or not _ACCESSION.fullmatch(accession)
            or not isinstance(filed, str)
            or not _DATE.fullmatch(filed)
            or not isinstance(form, str)
            or not form
        ):
            raise SourceError("The EDGAR filing list has a malformed filing")
        primary = _text(document)
        rows.append(
            {
                "form": form,
                "filingDate": filed,
                "reportDate": _text(reported),
                "accessionNumber": accession,
                "primaryDocument": primary,
                "primaryDocDescription": _text(description),
                "filingIndexUrl": filing_index_url(cik, accession),
                "primaryDocumentUrl": document_url(cik, accession, primary),
            }
        )
    return rows, sorted(name for name in recent if isinstance(name, str))


@dataclass(frozen=True)
class _OlderFiles:
    files: int
    filings: int | None
    """The filing count the source gives for those files, or None if one is missing."""
    first: str | None
    last: str | None


def _unread_older_files(files: Any, after: date | None, before: date | None) -> _OlderFiles | None:
    """The additional filing files whose date range overlaps the request, if any.

    A file whose dates cannot be read counts as overlapping, so a gap is never hidden.
    """
    # checked 2026-11-20: EDGAR sends files even when it is empty (CIK 2070845 has []), so a
    # missing array means the format changed and coverage cannot be judged.
    if not isinstance(files, list):
        raise SourceError("The EDGAR filing list has a malformed files array")
    overlapping: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise SourceError("The EDGAR filing list has a malformed files entry")
        start = _parse_date(item.get("filingFrom"))
        end = _parse_date(item.get("filingTo"))
        if (start is None or before is None or start <= before) and (
            end is None or after is None or end >= after
        ):
            overlapping.append(item)
    if not overlapping:
        return None
    starts = [item.get("filingFrom") for item in overlapping]
    ends = [item.get("filingTo") for item in overlapping]
    counts = [item.get("filingCount") for item in overlapping]
    return _OlderFiles(
        files=len(overlapping),
        filings=(sum(counts) if all(isinstance(count, int) for count in counts) else None),
        first=min((value for value in starts if _parse_date(value)), default=None),
        last=max((value for value in ends if _parse_date(value)), default=None),
    )


class SecEdgar:
    """Read one company's EDGAR filing list from the SEC's submissions API."""

    host = SUBMISSIONS_HOST
    row_grain = ROW_GRAIN

    def __init__(
        self,
        *,
        contact: str | None,
        client: httpx.AsyncClient | None = None,
        min_request_interval: float = MIN_REQUEST_INTERVAL_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        contact = " ".join(contact.split()) if contact else ""
        if contact and not (contact.isascii() and contact.isprintable()):
            raise ValueError("The SEC contact must be printable ASCII, such as an email address")
        self._contact = contact or None
        self._http = FixedHostClient(
            source_name=SOURCE_NAME,
            client=client,
            max_response_bytes=max_response_bytes,
            # official: sec.gov/os/accessing-edgar-data, "Please declare your user agent
            # in request headers", with a company name and an admin contact email.
            user_agent=f"OpenCivicQuery/{__version__} {contact}" if contact else None,
        )
        self._min_interval = min_request_interval
        self._next_start = 0.0
        self._pace_lock = asyncio.Lock()
        self._clock = clock
        self._tickers: _TickerIndex | None = None
        self._tickers_loaded_at = 0.0
        self._tickers_lock = asyncio.Lock()

    async def _get_json(self, url: str) -> Any:
        async with self._pace_lock:
            wait = self._next_start - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_start = time.monotonic() + self._min_interval
        return await self._http.json("GET", url)

    async def _ticker_file(self) -> _TickerIndex:
        async with self._tickers_lock:
            stale = self._clock() - self._tickers_loaded_at >= TICKERS_TTL_SECONDS
            if self._tickers is None or stale:
                self._tickers = _ticker_index(await self._get_json(TICKERS_URL))
                self._tickers_loaded_at = self._clock()
            return self._tickers

    async def _submissions(self, cik: int) -> dict[str, Any]:
        try:
            payload = await self._get_json(submissions_url(cik))
        except SourceQueryError as exc:
            if exc.http_status != 404:
                raise
            raise SourceQueryError(
                f"EDGAR has no submissions file for CIK {padded_cik(cik)}",
                code="NOT_FOUND",
                argument="company",
                http_status=404,
                public_message=(
                    f"EDGAR has no filing list for CIK {padded_cik(cik)}. Check the number with "
                    f"EDGAR search at {SEARCH_PAGE}."
                ),
            ) from exc
        if not isinstance(payload, dict):
            raise SourceError("The EDGAR filing list is not an object")
        if payload.get("cik") != padded_cik(cik) or not _text(payload.get("name")):
            # Every warning and URL names this CIK, so an answer for another filer must
            # never be frozen under it.
            raise SourceError("The EDGAR filing list did not confirm the CIK and name")
        return payload

    def find_filings(
        self,
        *,
        company: str,
        form_types: list[str] | None = None,
        filed_after: str | None = None,
        filed_before: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> PreparedLookup:
        """Validate without touching the network; the returned lookup fetches and filters."""
        if self._contact is None:
            raise SourceQueryError(
                "No SEC contact configured",
                code="SOURCE_NOT_CONFIGURED",
                public_message=(
                    "This server has no SEC contact configured, so it cannot query EDGAR."
                ),
            )
        text, given_cik = validate_company(company)
        forms = validate_form_types(form_types)
        after = validate_date("filed_after", filed_after)
        before = validate_date("filed_before", filed_before)
        if after is not None and before is not None and after > before:
            raise _invalid("filed_after", "filed_after must be on or before filed_before.")
        count = validate_limit(limit)
        arguments: dict[str, Any] = {
            "company": text,
            "form_types": forms or None,
            "filed_after": filed_after,
            "filed_before": filed_before,
            "limit": count,
        }

        async def run() -> Lookup:
            cik = given_cik
            if cik is None:
                cik = resolve_company(await self._ticker_file(), text)
            payload = await self._submissions(cik)
            filings = payload.get("filings")
            if not isinstance(filings, dict):
                raise SourceError("The EDGAR filing list has no filings object")
            rows, columns = _recent_rows(cik, filings.get("recent"))
            older = _unread_older_files(filings.get("files"), after, before)
            matched = [
                row
                for row in rows
                if (not forms or row["form"] in forms)
                and (after is None or row["filingDate"] >= after.isoformat())
                and (before is None or row["filingDate"] <= before.isoformat())
            ]
            # Stable, so filings from one day keep the source's own order.
            matched.sort(key=lambda row: row["filingDate"], reverse=True)
            name = payload["name"]
            query = urlencode(
                {
                    "company": text,
                    "cik": padded_cik(cik),
                    **({"form_types": ",".join(forms)} if forms else {}),
                    **({"filed_after": filed_after} if filed_after else {}),
                    **({"filed_before": filed_before} if filed_before else {}),
                    "limit": str(count),
                }
            )
            result = SourceQueryResult(
                dataset_id=f"submissions/CIK{padded_cik(cik)}",
                dataset_name=f"SEC EDGAR filings: {name} (CIK {padded_cik(cik)})",
                domain=SUBMISSIONS_HOST,
                source_url=company_page_url(cik),
                query=query,
                query_url=submissions_url(cik),
                rows=matched[:count],
                returned_row_count=min(len(matched), count),
                completeness=(
                    Completeness.TRUNCATED
                    if older is not None or len(matched) > count
                    else Completeness.COMPLETE
                ),
                schema_sha256=hashlib.sha256(
                    json.dumps({"recent": columns}, separators=(",", ":")).encode()
                ).hexdigest(),
            )
            warnings = _warnings(
                oldest=min((row["filingDate"] for row in rows), default=None),
                older=older,
                has_older_files=bool(filings.get("files")),
                matched=len(matched),
                limit=count,
                resolved=None if given_cik is not None else (text, cik, name),
                forms=bool(forms),
            )
            return Lookup(result=result, row_grain=ROW_GRAIN, warnings=warnings)

        return PreparedLookup(host=SUBMISSIONS_HOST, arguments=arguments, run=run)

    async def close(self) -> None:
        await self._http.close()


def _warnings(
    *,
    oldest: str | None,
    older: _OlderFiles | None,
    has_older_files: bool,
    matched: int,
    limit: int,
    resolved: tuple[str, int, str] | None,
    forms: bool,
) -> list[str]:
    """The caveats for one filing list, most urgent first."""
    warnings: list[str] = []
    start = f"starts at {oldest}" if oldest else "is empty"
    if older is not None:
        count = f"{older.filings:,}" if older.filings is not None else "Its"
        span = f" filed from {older.first} to {older.last}" if older.first and older.last else ""
        warnings.append(
            f"PARTIAL: this tool read only EDGAR's recent filings block for this company, "
            f"which {start}. {count} older filings{span}, in {older.files} "
            "additional files that overlap the requested dates, were not read. Do not say "
            "a filing does not exist unless it would fall inside the block that was read."
        )
    if matched > limit:
        warnings.append(
            f"{matched:,} filings in the block that was read match the request; this result "
            f"shows the newest {limit}. Raise limit (up to {MAX_LIMIT}) or narrow the "
            "dates or form types to see the rest."
        )
    if resolved is not None:
        given, cik, name = resolved
        warnings.append(
            f"OpenCivicQuery matched {given} to CIK {padded_cik(cik)}, {name}, through the "
            f"SEC's ticker file. {TICKER_FILE_NOTE} Confirm that this is the company asked "
            "about."
        )
    warnings.append(OTHER_FILERS_WARNING)
    warnings.append(CONTENT_WARNING)
    if forms:
        warnings.append(FORM_TYPE_WARNING)
    warnings.append(DATES_WARNING)
    if older is None:
        rest = (
            "Older filings sit in additional files outside the requested dates."
            if has_older_files
            else "EDGAR lists no older filings for this CIK."
        )
        warnings.append(
            f"This list comes from EDGAR's recent filings block for this company, which "
            f"{start}. {RECENT_BLOCK_NOTE} {rest}"
        )
    warnings.append(ONE_CIK_WARNING)
    warnings.append(FRESHNESS_WARNING)
    return warnings
