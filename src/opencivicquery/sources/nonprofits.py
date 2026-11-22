"""ProPublica Nonprofit Explorer: IRS records of tax-exempt organizations and their Form 990s.

ProPublica republishes the IRS Exempt Organizations Business Master File, the IRS annual
extract of tax-exempt financial data, and the filed Form 990 documents. The API needs no
key. The host is fixed, so no caller can steer a request elsewhere.

API documentation: https://projects.propublica.org/nonprofits/api
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any
from urllib.parse import urlencode

import httpx

from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources.base import Lookup, PreparedLookup, SourceError, SourceQueryError
from opencivicquery.sources.http import FixedHostClient

NONPROFITS_HOST = "projects.propublica.org"
_API = f"https://{NONPROFITS_HOST}/nonprofits/api/v2"
SEARCH_PAGE = f"https://{NONPROFITS_HOST}/nonprofits/search"
ORGANIZATION_PAGE = f"https://{NONPROFITS_HOST}/nonprofits/organizations/"
SOURCE_NAME = "ProPublica Nonprofit Explorer API"

PAGE_SIZE = 25
# official: API changelog, September 12, 2023: "Reduced the number of results per page on
# the search endpoint from 100 to 25." checked 2026-11-21: per_page was 25.
MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 100
# Our own bounds, not the source's: one character matches nearly everything, and a name
# longer than 100 characters is not a name.
TOTAL_CAP = 10_000
# checked 2026-11-21: "Foundation" and "Mission | Housing" both reported total_results
# 10000 over 400 pages, so 10,000 means "at least 10,000".

STATE_CODES = frozenset(
    {
        *("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID"),
        *("IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO"),
        *("MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA"),
        *("RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"),
        *("AS", "GU", "MP", "PR", "VI", "FM", "MH", "PW", "AA", "AE", "AP", "ZZ"),
    }
)
# official: /search.json state[id]: "Must use the two-letter U.S. Postal Service
# abbreviation. Code "ZZ" is used for entities based outside the United States".

FORM_NAMES = {0: "990", 1: "990-EZ", 2: "990-PF"}
# official: Filing object, formtype: "0 (Form 990), 1 (Form 990_EZ), 2 (Form 990-PF)".

_EIN = re.compile(r"[0-9]{2}-?[0-9]{7}")
# ASCII digits only; \d would also accept other scripts' digits.
_OPERATORS = re.compile(r'["+\-|*~()]')
# official: /search.json q: quotation marks make a phrase, a leading plus sign requires a
# word, and a leading minus sign excludes one. checked 2026-11-21: "Mission -Housing" in CA
# returned 2,878 organizations, against 3,362 for "Mission", so a stray minus sign silently
# drops matches; the API also honors | (or), * (prefix), ~ (fuzzy), and parentheses, so
# "Harv*" matched 5,420 and "Harvard~" 2,022. All of them become spaces.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})

SEARCH_FIELDS = ("strein", "name", "sub_name", "city", "state", "ntee_code", "subseccd")
FILING_FIELDS = (
    "tax_prd",
    "tax_prd_yr",
    "form",
    "financial_data",
    "totrevenue",
    "totfuncexpns",
    "totassetsend",
    "totliabend",
    "pdf_url",
    "updated",
)
AMOUNT_FIELDS = ("totrevenue", "totfuncexpns", "totassetsend", "totliabend")
# official: Filing object, "Basic financial data": total revenue, total expenses ("Total
# functional expenses" for Form 990), total assets and total liabilities at end of year.
# checked 2026-11-21: a 990-PF filing of the Walter and Elise Haas Fund (EIN 94-6068564)
# carried totrevenue equal to its totrcptperbks.

SEARCH_GRAIN = (
    "One organization in ProPublica's copy of the IRS Exempt Organizations Business Master "
    "File whose name, alternate name, or city matched the search words."
)
FILING_GRAIN = (
    "One tax return of one organization for one tax period, as ProPublica lists it: either "
    "with figures from the IRS annual extract of tax-exempt financial data, or as a filed "
    "document with no extracted figures."
)
# official: Filing objects "are unique combinations of ein and tax_prd"; filings_without_data
# "represent raw filing documents available as PDF files, without the corresponding
# extracted numerical data."

SEARCH_MATCH_WARNING = (
    "The search matches the words against organization names, alternate names, and cities, "
    "so an organization based in a city with that name also appears. Rows come in "
    "ProPublica's relevance order. Check each name and city before you rely on a row, and "
    "use its EIN with get_nonprofit_filings."
)
# official: /search.json q "will search (in order) organization name, organization alternate
# name, city". checked 2026-11-21: "Hayward" in CA matched 859 organizations, most of them
# by city.

SEARCH_SCOPE_WARNING = (
    "The list holds organizations in the IRS list of tax-exempt organizations. An "
    "organization the IRS does not list, or lists under another legal name, does not appear."
)
# official: About This Data, "Which Organizations Are Here?", and the data source note: the
# organization profiles come from the IRS Exempt Organizations Business Master File.

NO_FILINGS_WARNING = (
    "Nonprofit Explorer lists no Form 990 filing for this organization. Organizations with "
    "less than $50,000 in revenue file only the Form 990-N e-Postcard, which has no "
    "financial figures, and churches are not required to file Form 990, so this does not "
    "show that the organization failed to file."
)
NO_FILINGS_RULE = (
    "NO FILINGS LISTED. Say that Nonprofit Explorer lists no Form 990 filing for this "
    "organization. Do not say that the organization filed no tax return."
)
# official: About This Data: organizations "making less than $50,000 don't have to file
# either form" but file "a Form 990N e-Postcard, which does not contain financial details",
# and "Churches are not required to file Form 990." checked 2026-11-21: EIN 51-0183500,
# a neighborhood association, had both filing lists empty.

NO_MATCH_WARNING = (
    "No organization matched. Before you report that none exists, try its legal name, a "
    "shorter part of the name, or no state filter."
)

TAX_PERIOD_WARNING = (
    "tax_prd is the month the organization's fiscal year ended, as YYYYMM, and tax_prd_yr is "
    "that year. A filing with tax_prd 202406 covers a fiscal year that ended in June 2024, so "
    "name the fiscal year end, not a calendar year, beside any figure."
)
# official: Filing object, tax_prd: "The tax period (month that the organization's fiscal
# year ended) for the given Filing data. Integer in YYYYMM format."

AMOUNTS_WARNING = (
    "totrevenue, totfuncexpns, totassetsend, and totliabend are ProPublica's summary fields "
    "for total revenue, total expenses, and end-of-year assets and liabilities as the "
    "organization reported them. They come from different lines on Forms 990, 990-EZ, and "
    "990-PF, so compare them only within one form type, and read the filing before you "
    "quote a figure."
)
# official: "Filing Object: Basic financial data": "total revenue (book value) is listed
# under one of "totrevnue", "totrevenue", or "totrcptperbks", depending on which form type
# is in question."

PDF_ONLY_WARNING = (
    "Rows whose financial_data is pdf only have a filed document but no extracted figures. "
    "Open pdf_url for their numbers; ProPublica rate limits those downloads."
)
# official: pdf_url: "Note that these download links are rate limited."

LAG_WARNING = (
    "The figures come from the IRS annual extract of tax-exempt financial data, which arrives "
    "well after a return is filed, so the latest years may appear as pdf only or not at all. "
    "updated is when ProPublica last refreshed each filing's figures."
)
# official: Filing object, updated: "Date that this filing data was updated in Nonprofit
# Explorer. Data is extracted from the IRS Annual Extract of Tax-Exempt Organization
# Financial Data." checked 2026-11-21: EIN 51-0187791 had figures through tax period 202312
# and only a document for 202412.

CITATION_RULE = (
    "Name ProPublica Nonprofit Explorer and the IRS as the source of these records, because "
    "ProPublica's data terms require publications that use the data to cite ProPublica."
)
# official: the API page says "Usage constitues agreement to our Data Terms of Use", and
# those terms (projects.propublica.org/datastore/terms) say "If you use the data for
# publication, you must cite ProPublica."


def _invalid(argument: str | None, message: str) -> SourceQueryError:
    return SourceQueryError(
        f"Invalid {argument or 'request'}",
        code="INVALID_QUERY_ARGUMENT",
        argument=argument,
        public_message=message,
    )


def validate_ein(value: Any) -> int:
    """Return the EIN as the integer the API takes, from 12-3456789 or 123456789."""
    text = value.strip() if isinstance(value, str) else None
    if text is None or not _EIN.fullmatch(text):
        raise _invalid(
            "ein",
            "Give ein as nine digits, with or without the hyphen, for example 94-6068564. "
            "Use search_nonprofits to find an organization's EIN.",
        )
    number = int(text.replace("-", ""))
    if number == 0:
        raise _invalid("ein", "An EIN of all zeros does not identify an organization.")
    return number


def formatted_ein(number: int) -> str:
    digits = f"{number:09d}"
    return f"{digits[:2]}-{digits[2:]}"


def validate_name(value: Any) -> str:
    message = (
        f"Give name as {MIN_NAME_LENGTH} to {MAX_NAME_LENGTH} characters of an organization's "
        "name, in plain words."
    )
    if not isinstance(value, str):
        raise _invalid("name", message)
    words = " ".join(_OPERATORS.sub(" ", value).split())
    if not MIN_NAME_LENGTH <= len(words) <= MAX_NAME_LENGTH or any(
        unicodedata.category(character) in _UNSAFE_CATEGORIES for character in words
    ):
        raise _invalid("name", message)
    return words


def validate_state(value: Any) -> str | None:
    if value is None:
        return None
    code = value.strip().upper() if isinstance(value, str) else None
    if code not in STATE_CODES:
        raise _invalid(
            "state",
            "Give state as a two-letter postal code, for example CA, or ZZ for organizations "
            "based outside the United States.",
        )
    return code


def _schema_hash(kind: str, fields: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps({"kind": kind, "fields": list(fields)}, separators=(",", ":")).encode()
    ).hexdigest()


def _normalized_form(label: str) -> str:
    """ProPublica's document label in the same spelling as FORM_NAMES where they match."""
    compact = label.strip().upper().replace("-", "")
    return {"990": "990", "990EZ": "990-EZ", "990PF": "990-PF"}.get(compact, label.strip())


def _search_rows(payload: Any) -> tuple[list[dict[str, Any]], int, int]:
    if not isinstance(payload, dict):
        raise SourceError("The Nonprofit Explorer search returned a body that is not an object")
    organizations = payload.get("organizations")
    total = payload.get("total_results")
    pages = payload.get("num_pages")
    if (
        not isinstance(organizations, list)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or isinstance(pages, bool)
        or not isinstance(pages, int)
    ):
        raise SourceError("The Nonprofit Explorer search returned no usable result list")
    rows = []
    for organization in organizations:
        if not isinstance(organization, dict) or not isinstance(organization.get("ein"), int):
            raise SourceError("The Nonprofit Explorer search returned a malformed organization")
        row = {field: organization.get(field) for field in SEARCH_FIELDS}
        row["strein"] = formatted_ein(organization["ein"])
        row["organization_page_url"] = f"{ORGANIZATION_PAGE}{organization['ein']}"
        rows.append(row)
    return rows, total, pages


def _filing_row(filing: Any, *, with_data: bool) -> dict[str, Any]:
    if not isinstance(filing, dict) or isinstance(filing.get("tax_prd"), bool):
        raise SourceError("The Nonprofit Explorer API returned a malformed filing")
    if not isinstance(filing.get("tax_prd"), int):
        raise SourceError("The Nonprofit Explorer API returned a filing without a tax period")
    formtype = filing.get("formtype")
    form_label = filing.get("formtype_str")
    form = FORM_NAMES.get(formtype) if isinstance(formtype, int) else None
    if not with_data and isinstance(form_label, str) and form_label.strip():
        # checked 2026-11-21: document-only filings carry formtype_str, such as 990, 990PF,
        # or 990R, which names the document more exactly than formtype. Labels other than
        # the three forms are kept as ProPublica gives them.
        form = _normalized_form(form_label)
    row: dict[str, Any] = {
        "tax_prd": filing["tax_prd"],
        "tax_prd_yr": filing.get("tax_prd_yr"),
        "form": form,
        "financial_data": "extracted" if with_data else "pdf only",
    }
    for field in AMOUNT_FIELDS:
        row[field] = filing.get(field) if with_data else None
    row["pdf_url"] = filing.get("pdf_url")
    row["updated"] = filing.get("updated") if with_data else None
    return row


class NonprofitExplorer:
    """Find tax-exempt organizations and list their Form 990 filings."""

    host = NONPROFITS_HOST

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._http = FixedHostClient(source_name=SOURCE_NAME, client=client)

    def search_nonprofits(self, *, name: str, state: str | None = None) -> PreparedLookup:
        """Validate one search without touching the network and return it ready to run."""
        words = validate_name(name)
        code = validate_state(state)
        params = {"q": words, **({"state[id]": code} if code else {})}
        url = f"{_API}/search.json?{urlencode(params)}"

        async def run() -> Lookup:
            status, content = await self._http.send("GET", f"{_API}/search.json", params=params)
            try:
                payload = json.loads(content)
            except ValueError:
                payload = None
            if status == 404 and isinstance(payload, dict) and payload.get("total_results") == 0:
                # checked 2026-11-21: a search with no match, such as "zzzzqqqxx", returned
                # HTTP 404 with a normal result body and total_results 0.
                status = 200
            if status != 200:
                raise SourceQueryError(
                    f"{SOURCE_NAME} HTTP {status}",
                    code="SOURCE_UNAVAILABLE",
                    http_status=status,
                    public_message=(
                        f"The {SOURCE_NAME} could not complete the request (HTTP {status})."
                    ),
                )
            rows, total, pages = _search_rows(payload)
            at_least = "at least " if total >= TOTAL_CAP else ""
            result = SourceQueryResult(
                dataset_id="search",
                dataset_name=(
                    f"ProPublica Nonprofit Explorer organization search: {at_least}{total:,} "
                    f"organizations match{' in ' + code if code else ''}"
                ),
                domain=NONPROFITS_HOST,
                source_url=f"{SEARCH_PAGE}?{urlencode(params)}",
                query=urlencode(params),
                query_url=url,
                rows=rows,
                returned_row_count=len(rows),
                completeness=(
                    Completeness.TRUNCATED
                    if pages > 1 or total > len(rows)
                    else Completeness.COMPLETE
                ),
                schema_sha256=_schema_hash("search", SEARCH_FIELDS),
            )
            warnings = [] if rows else [NO_MATCH_WARNING]
            if total > len(rows):
                warnings.append(
                    f"{at_least}{total:,} organizations match; this result shows "
                    f"ProPublica's first {len(rows)}. Add more of the name or a state to "
                    "narrow the search."
                )
            warnings += [SEARCH_MATCH_WARNING, SEARCH_SCOPE_WARNING]
            return Lookup(
                result=result,
                row_grain=SEARCH_GRAIN,
                warnings=warnings,
                rules=[CITATION_RULE],
            )

        return PreparedLookup(
            host=NONPROFITS_HOST, arguments={"name": words, "state": code}, run=run
        )

    def get_filings(self, *, ein: str) -> PreparedLookup:
        """Validate one EIN without touching the network and return the lookup."""
        number = validate_ein(ein)
        url = f"{_API}/organizations/{number}.json"

        async def run() -> Lookup:
            status, content = await self._http.send("GET", url)
            try:
                payload = json.loads(content)
            except ValueError:
                payload = None
            if status == 404:
                # checked 2026-11-21: EINs 12-3456780 and 00-0000001 returned HTTP 404 with
                # {"error": "Organization not found"}.
                raise SourceQueryError(
                    f"No organization with EIN {formatted_ein(number)}",
                    code="NOT_FOUND",
                    argument="ein",
                    http_status=status,
                    public_message=(
                        f"Nonprofit Explorer has no organization with EIN "
                        f"{formatted_ein(number)}. Check the number, or use search_nonprofits."
                    ),
                )
            if status != 200 or not isinstance(payload, dict):
                raise SourceQueryError(
                    f"{SOURCE_NAME} HTTP {status}",
                    code="SOURCE_UNAVAILABLE",
                    http_status=status,
                    public_message=(
                        f"The {SOURCE_NAME} could not complete the request (HTTP {status})."
                    ),
                )
            organization = payload.get("organization")
            with_data = payload.get("filings_with_data")
            without_data = payload.get("filings_without_data")
            if (
                not isinstance(organization, dict)
                or not isinstance(with_data, list)
                or not isinstance(without_data, list)
            ):
                raise SourceError("The Nonprofit Explorer API returned no organization record")
            if organization.get("name") == "Unknown Organization" and not organization.get("state"):
                # checked 2026-11-21: EIN 99-9999999 returned HTTP 200 with this placeholder
                # name, no address, and five unrelated documents.
                raise SourceQueryError(
                    f"Placeholder organization for EIN {formatted_ein(number)}",
                    code="NOT_FOUND",
                    argument="ein",
                    public_message=(
                        f"Nonprofit Explorer holds only a placeholder record for EIN "
                        f"{formatted_ein(number)}, not an organization."
                    ),
                )
            rows = [_filing_row(filing, with_data=True) for filing in with_data]
            rows += [_filing_row(filing, with_data=False) for filing in without_data]
            # checked 2026-11-21: the two lists hold different tax periods; newest first.
            rows.sort(key=lambda row: row["tax_prd"], reverse=True)
            name = organization.get("name") or "name not given"
            place = ", ".join(
                part for part in (organization.get("city"), organization.get("state")) if part
            )
            result = SourceQueryResult(
                dataset_id=f"organizations/{number}",
                dataset_name=(
                    f"ProPublica Nonprofit Explorer filings: {name} (EIN {formatted_ein(number)}"
                    f"{', ' + place if place else ''})"
                ),
                domain=NONPROFITS_HOST,
                source_url=f"{ORGANIZATION_PAGE}{number}",
                query=f"ein={formatted_ein(number)}",
                query_url=url,
                rows=rows,
                returned_row_count=len(rows),
                completeness=Completeness.COMPLETE,
                schema_sha256=_schema_hash("filings", FILING_FIELDS),
            )
            warnings = [] if rows else [NO_FILINGS_WARNING]
            warnings += [TAX_PERIOD_WARNING, AMOUNTS_WARNING]
            if any(row["financial_data"] == "pdf only" for row in rows):
                warnings.append(PDF_ONLY_WARNING)
            warnings.append(LAG_WARNING)
            return Lookup(
                result=result,
                row_grain=FILING_GRAIN,
                warnings=warnings,
                rules=([] if rows else [NO_FILINGS_RULE]) + [CITATION_RULE],
                escalations=[] if rows else ["NO_FILINGS_LISTED"],
            )

        return PreparedLookup(
            host=NONPROFITS_HOST, arguments={"ein": formatted_ein(number)}, run=run
        )

    async def close(self) -> None:
        await self._http.close()
