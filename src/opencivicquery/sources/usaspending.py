"""USAspending.gov: federal prime awards (contracts, grants, loans, and other assistance).

It needs no key. The host is fixed, so no caller can steer a request elsewhere.

Endpoint contract:
https://github.com/fedspendingtransparency/usaspending-api/blob/master/usaspending_api/api_contracts/contracts/v2/search/spending_by_award.md
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources.base import Lookup, PreparedLookup, SourceError, SourceQueryError
from opencivicquery.sources.http import FixedHostClient

USASPENDING_HOST = "api.usaspending.gov"
_SEARCH_ENDPOINT = f"https://{USASPENDING_HOST}/api/v2/search/spending_by_award/"
_LAST_UPDATED_ENDPOINT = f"https://{USASPENDING_HOST}/api/v2/awards/last_updated/"
ADVANCED_SEARCH_PAGE = "https://www.usaspending.gov/search"
AWARD_PAGE = "https://www.usaspending.gov/award/"
# checked 2026-11-20: the website routes /award/:awardId to the award profile (usaspending-
# website RouterRoutes.jsx), and /api/v2/awards/CONT_AWD_1605JW21C0005_1605_-NONE-_-NONE-/
# returned that award, so generated_internal_id is the key the award page uses.
AWARD_PAGE_FIELD = "award_page_url"

EARLIEST_START_DATE = date(2007, 10, 1)
# official: search_filters.md, Time Period: "Currently limited to an earliest date of
# 2007-10-01 (FY2008)". checked 2026-11-20: start_date 2007-09-30 returned HTTP 422.
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
# official: usaspending-api PAGINATION validator, limit max 100.
MAX_KEYWORDS = 5
MIN_KEYWORD_LENGTH = 3
# official: usaspending-api award filter validator, keywords text_min 3. checked 2026-11-20:
# the keyword "ab" returned HTTP 422.
MAX_KEYWORD_LENGTH = 100
MIN_RECIPIENT_LENGTH = 3
MAX_RECIPIENT_LENGTH = 200

AWARD_TYPE_CODES: dict[str, tuple[str, ...]] = {
    "contracts": ("A", "B", "C", "D"),
    "grants": ("02", "03", "04", "05", "F001", "F002"),
    "direct_payments": ("06", "10", "F006", "F007"),
    "loans": ("07", "08", "F003", "F004"),
    "other": ("09", "11", "-1", "F005", "F008", "F009", "F010"),
}
# official: usaspending-website awardType.js awardTypeGroups, which the Advanced Search
# table tabs send (the Grants tab adds cooperative agreements 05 and F002). checked
# 2026-11-20: mixing groups returned HTTP 422 "'award_type_codes' must only contain types
# from one group.", and each group above returned HTTP 200.

AWARD_TYPE_LABELS = {
    "contracts": "contracts",
    "grants": "grants and cooperative agreements",
    "direct_payments": "direct payments",
    "loans": "loans",
    "other": "other financial assistance",
}

_SHARED_HEAD = ("Award ID", "Recipient Name", "Recipient UEI")
_SHARED_TAIL = (
    "Awarding Agency",
    "Awarding Sub Agency",
    "Base Obligation Date",
)
_CONTRACT_FIELDS = (
    *_SHARED_HEAD,
    "Award Amount",
    "Total Outlays",
    "Description",
    "Contract Award Type",
    *_SHARED_TAIL,
    "Start Date",
    "End Date",
    "Primary Place of Performance",
    "generated_internal_id",
)
_ASSISTANCE_FIELDS = (
    *_SHARED_HEAD,
    "Award Amount",
    "Total Outlays",
    "Description",
    "Award Type",
    *_SHARED_TAIL,
    "Start Date",
    "End Date",
    "Assistance Listings",
    "Primary Place of Performance",
    "generated_internal_id",
)
_LOAN_FIELDS = (
    *_SHARED_HEAD,
    "Subsidy Cost",
    "Loan Value",
    "Description",
    "Award Type",
    *_SHARED_TAIL,
    "Issued Date",
    "Assistance Listings",
    "Primary Place of Performance",
    "generated_internal_id",
)
# official: spending_by_award.md, SpendingByAwardFields. Contract and assistance fields
# differ; loans have Loan Value and Subsidy Cost instead of Award Amount.
FIELDS: dict[str, tuple[str, ...]] = {
    "contracts": _CONTRACT_FIELDS,
    "grants": _ASSISTANCE_FIELDS,
    "direct_payments": _ASSISTANCE_FIELDS,
    "loans": _LOAN_FIELDS,
    "other": _ASSISTANCE_FIELDS,
}
SORT_FIELD = {award_type: "Award Amount" for award_type in AWARD_TYPE_CODES}
SORT_FIELD["loans"] = "Subsidy Cost"
# official: USAspending glossary, "Loan Subsidy Cost": "for a loan, the award amount is the
# subsidy cost". The Advanced Search Loans tab sorts by it too (awardTableColumns.jsx).

STATE_CODES = frozenset(
    {
        *("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID"),
        *("IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO"),
        *("MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA"),
        *("RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"),
        *("AS", "GU", "MP", "PR", "VI", "UM"),
    }
)
# official: usaspending-api data_constants.py state_to_code_dict, less FM, MH, and PW. About
# the Data, "Freely Associated States": from October 1, 2026 those three appear as foreign
# countries, not U.S. states or territories.

ROW_GRAIN = (
    "One row is one prime award summary: every transaction that shares one award key, with "
    "the recipient, agency, and period of performance generally taken from the latest "
    "transaction. Its amounts are totals for the whole award to date. Subawards are not rows."
)
# official: USAspending glossary, "Prime Award Summary". The Award Amount field maps to the
# award's total_obligation (usaspending-api elasticsearch_lookups.py).

DATE_RANGE_WARNING = (
    "The date range selects awards with activity in it, not awards made in it. An award "
    "appears when its first transaction is on or before end_date and its latest transaction "
    "is on or after start_date, so an award signed years earlier appears if it changed in "
    "the range. Base Obligation Date shows when each award was first signed."
)
# official: About the Data, "Time Period Filter" (results "overlap with the selected time
# period based on their prime award base transaction action date and prime award latest
# transaction action date"); search_filters.md, Award Search Time Period Object.
# checked 2026-11-20: a fiscal 2025 search returned a Department of Labor contract that
# started 2020-12-14.

AMOUNT_WARNING = (
    "Award Amount is the amount the government has obligated, meaning promised to pay, on "
    "the whole award to date, not only within the date range. It is not the amount paid "
    "out; that is Total Outlays."
)
# official: USAspending glossary, "Award Amount" and "Outlay". checked 2026-11-20: that
# Department of Labor contract carried its full Award Amount of $98,395,863.72 in a fiscal
# 2025 search.

LOAN_AMOUNT_WARNING = (
    "Loan Value is the loan's face value. USAspending does not count face value as spending, "
    "because loans are expected to be repaid. Subsidy Cost is the government's estimate of "
    "what the loan will cost it; it is the award amount for a loan, it can be negative, and "
    "rows are sorted by it."
)
# official: USAspending glossary, "Face Value of Loan" and "Loan Subsidy Cost".

KEYWORD_WARNING = (
    "Keyword search looks for each keyword as a phrase anywhere in several fields at once, "
    "and the last word may be the start of a longer word, so homeless also matches "
    "homelessness. The fields are award descriptions, recipient names, product, service, "
    "and industry descriptions, and award and recipient ID numbers. An award matching any "
    "one keyword is included. The search can include awards that mention a word only in "
    "passing or in a recipient's name, and it misses awards described with other words, "
    "codes, or abbreviations. Read each Description before you rely on a row."
)
# checked 2026-11-20: usaspending-api common/query_with_filters.py, _Keywords, builds a
# phrase_prefix match over those text fields, a query_string prefix match over piid, fain,
# and uri, and joins keywords with dis_max (any match).
# official: About the Data, "Award Description Data Quality": descriptions can be technical,
# use codes or abbreviations, or lack meaningful information.

RECIPIENT_WARNING = (
    "Recipient search finds the text anywhere in the recipient's name or its parent "
    "company's name, even inside a longer word, and also matches UEI and DUNS numbers. On "
    "2026-11-20, RAND in Massachusetts grants for fiscal 2025 matched only Brandeis "
    "University. It can also miss awards made under a different legal name. Check "
    "Recipient Name in every row."
)
# official: spending_by_award.md, recipient_search_text: "Text searched across a recipient's
# name, UEI, and DUNS". checked 2026-11-20: usaspending-api _RecipientSearchText also
# searches parent_recipient_name with a phrase_prefix match. checked 2026-11-20: recipient
# "RAND", Massachusetts grants, 2024-10-01 to 2025-09-30, returned only BRANDEIS UNIVERSITY.

PLACE_WARNING = (
    "The state and county filter uses the place of performance, where the agency reported "
    "that most of the work is done. It is not the recipient's business address, and results "
    "differ between the two."
)
# official: USAspending glossary, "Primary Place of Performance" and "Recipient Location";
# About the Data, "Location Filter".

AGGREGATE_RECORDS_WARNING = (
    "A row whose recipient is MULTIPLE RECIPIENTS is an aggregate record: many awards to "
    "individuals, grouped by county, state, or country to protect their privacy. Count it "
    "as a group of awards, not as one recipient."
)
# official: About the Data, "PII and Aggregate Records".

REDACTED_RECORDS_WARNING = (
    "A row whose recipient is REDACTED DUE TO PII is one award to an individual whose name "
    "was removed. Rows with that name can be different people, so do not count them as one "
    "recipient."
)
# official: About the Data, "PII and Redacted Records".

NO_MATCH_WARNING = (
    "No award matched. Before you report that none exist, confirm the county code, because "
    "USAspending answers a county code that does not exist with an empty result, and try "
    "other spellings of any keyword or recipient."
)
# checked 2026-11-20: usaspending-api award_filter.py accepts any text as county, and
# _PlaceOfPerformanceLocations matches pop_county_code without checking that it exists.

CONTRACT_DELAY_WARNING = (
    "Contract data generally reach USAspending within five days, but Department of Defense "
    "and U.S. Army Corps of Engineers contracts are published on a 90-day delay, so their "
    "most recent three months are missing."
)
# official: About the Data, "Delay in DOD Procurement Data" and "Frequency of Updates to
# Prime Award Data for Contracts".

ASSISTANCE_DELAY_WARNING = (
    "Agencies have two weeks to report a grant or other financial assistance award, and "
    "loans must be published within 30 days, so the most recent weeks can be incomplete."
)
# official: About the Data, "Frequency of Updates to Prime Award Data for Financial
# Assistance".

FRESHNESS_WARNING = (
    "The update date is the most recent day that any award record changed in USAspending's "
    "award database. The search reads a separate search index, so treat the date as context "
    "for these rows, not their exact as-of time. It also does not mean every agency has "
    "reported through that day."
)
# checked 2026-11-20: usaspending-api awards/v2/views/awards.py, AwardLastUpdatedViewSet,
# returns the latest update_date over the Award table, while spending_by_award queries the
# Elasticsearch award index (AwardSearch).

SUBAWARD_WARNING = (
    "These are prime awards only. Subawards, the money a prime recipient passes on to other "
    "organizations, are not included, and adding subaward amounts to these amounts double "
    "counts the money."
)
# official: USAspending glossary, "Sub-Award". The request sets spending_level to awards.

# ASCII digits only: \d also matches other scripts' digits, which the source would not read
# as a FIPS code and would answer with an empty, falsely complete result.
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_COUNTY = re.compile(r"[0-9]{3}")
_LAST_UPDATED = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")
LAST_UPDATED_TIMEOUT_SECONDS = 10.0
# The search and the date share the service's 60-second deadline, so a stalled date call
# must give up early instead of failing a search that already succeeded.

_KEYWORD_STRIPPED = frozenset('|{}()?\\"+[]<>')
_QUERY_OPERATORS = frozenset({"AND", "OR", "NOT"})
# checked 2026-11-20: usaspending-api search/v2/es_sanitization.py, es_sanitize, deletes these
# characters before the keyword reaches a query_string query, where capitalized AND, OR, and
# NOT are operators. A keyword made of them alone becomes a bare wildcard that matches every
# award, and NOT inverts the match.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _invalid(argument: str | None, message: str) -> SourceQueryError:
    return SourceQueryError(
        f"Invalid {argument or 'search'}",
        code="INVALID_QUERY_ARGUMENT",
        argument=argument,
        public_message=message,
    )


def _invisible(text: str) -> bool:
    """Control, format, surrogate, private-use, and unassigned characters.

    These include NUL, ESC, a zero-width space, or a bidi override. A lone surrogate also
    cannot be encoded as UTF-8, so it would fail only after the request costs an allowance.
    """
    return any(unicodedata.category(character) in _UNSAFE_CATEGORIES for character in text)


def _text(value: Any, argument: str, low: int, high: int, message: str) -> str:
    if not isinstance(value, str):
        raise _invalid(argument, message)
    normalized = " ".join(value.split())
    if not low <= len(normalized) <= high or _invisible(normalized):
        raise _invalid(argument, message)
    return normalized


def _date(value: Any, argument: str) -> date:
    message = f"Give {argument} as YYYY-MM-DD, for example 2024-10-01."
    if not isinstance(value, str) or not _DATE.fullmatch(value.strip()):
        raise _invalid(argument, message)
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise _invalid(argument, message) from exc


def _keywords(value: Sequence[str] | None) -> list[str] | None:
    message = (
        f"Give keywords as a list of 1 to {MAX_KEYWORDS} words or phrases, each "
        f"{MIN_KEYWORD_LENGTH} to {MAX_KEYWORD_LENGTH} characters."
    )
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _invalid("keywords", message)
    keywords = [
        _text(item, "keywords", MIN_KEYWORD_LENGTH, MAX_KEYWORD_LENGTH, message) for item in value
    ]
    if len(keywords) > MAX_KEYWORDS:
        raise _invalid("keywords", message)
    for keyword in keywords:
        if _KEYWORD_STRIPPED.intersection(keyword) or _QUERY_OPERATORS.intersection(
            keyword.split()
        ):
            raise _invalid(
                "keywords",
                'Use plain words in keywords, without the characters | { } ( ) ? \\ " + [ ] '
                "< > and without AND, OR, or NOT in capitals. Give alternatives as separate "
                "keywords; an award matching any one is included.",
            )
    return keywords or None


def _award_page_url(generated_internal_id: Any) -> str | None:
    if not isinstance(generated_internal_id, str) or not generated_internal_id:
        return None
    return AWARD_PAGE + quote(generated_internal_id, safe="")


def _rows(
    payload: Any, fields: Sequence[str], sort_field: str
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """The requested fields of each award in the source's names, plus its public page.

    Also returns the requested fields that some award lacked. The source sends every
    requested field, null when empty, so a missing one means its schema changed.
    """
    if not isinstance(payload, dict):
        raise SourceError("The USAspending API returned a body that is not an object")
    if payload.get("spending_level") != "awards":
        # The row grain and the subaward warning assume prime awards, so an answer that
        # does not confirm that level must never be frozen under them.
        raise SourceError("The USAspending API did not confirm prime award results")
    results = payload.get("results")
    metadata = payload.get("page_metadata")
    has_next = metadata.get("hasNext") if isinstance(metadata, dict) else None
    if not isinstance(results, list) or not isinstance(has_next, bool):
        raise SourceError("The USAspending API returned no usable results and page_metadata")
    rows: list[dict[str, Any]] = []
    missing: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise SourceError("The USAspending API returned a malformed award")
        missing.update(field for field in fields if field not in result)
        row = {field: result.get(field) for field in fields}
        row[AWARD_PAGE_FIELD] = _award_page_url(result.get("generated_internal_id"))
        rows.append(row)
    # checked 2026-11-20: the API answers an unknown or renamed field with null rather than
    # dropping it, so a key check alone cannot see a schema change. An award always has an
    # identifier and an amount, so null in every row means the answer cannot be trusted.
    for required in (AWARD_PAGE_FIELD, "Award ID", sort_field):
        if rows and all(row.get(required) is None for row in rows):
            raise SourceError(f"The USAspending API returned no {required} for any award")
    return rows, has_next, sorted(missing)


def _recipient_is(row: dict[str, Any], name: str) -> bool:
    recipient = row.get("Recipient Name")
    return isinstance(recipient, str) and recipient.strip().upper() == name


def warnings_for(
    award_type: str,
    *,
    keywords: bool,
    recipient: bool,
    place: bool,
    rows: Sequence[dict[str, Any]],
    updated: bool,
) -> list[str]:
    """The documented caveats that apply to one search, most urgent first."""
    warnings = [] if rows else [NO_MATCH_WARNING]
    warnings += [
        DATE_RANGE_WARNING,
        LOAN_AMOUNT_WARNING if award_type == "loans" else AMOUNT_WARNING,
    ]
    if keywords:
        warnings.append(KEYWORD_WARNING)
    if recipient:
        warnings.append(RECIPIENT_WARNING)
    if place:
        warnings.append(PLACE_WARNING)
    if any(_recipient_is(row, "MULTIPLE RECIPIENTS") for row in rows):
        warnings.append(AGGREGATE_RECORDS_WARNING)
    if any(_recipient_is(row, "REDACTED DUE TO PII") for row in rows):
        warnings.append(REDACTED_RECORDS_WARNING)
    warnings.append(
        CONTRACT_DELAY_WARNING if award_type == "contracts" else ASSISTANCE_DELAY_WARNING
    )
    if updated:
        warnings.append(FRESHNESS_WARNING)
    warnings.append(SUBAWARD_WARNING)
    return warnings


class USAspending:
    """Search federal prime awards with USAspending's spending_by_award endpoint."""

    host = USASPENDING_HOST
    row_grain = ROW_GRAIN

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._http = FixedHostClient(source_name="USAspending API", client=client)

    def search_awards(
        self,
        *,
        award_type: str,
        start_date: str,
        end_date: str,
        keywords: Sequence[str] | None = None,
        recipient: str | None = None,
        state: str | None = None,
        county: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> PreparedLookup:
        """Validate one search without touching the network and return it ready to run."""
        kind = award_type.strip().lower() if isinstance(award_type, str) else None
        if kind not in AWARD_TYPE_CODES:
            raise _invalid(
                "award_type",
                "Choose one award_type: contracts, grants, direct_payments, loans, or other.",
            )
        start = _date(start_date, "start_date")
        end = _date(end_date, "end_date")
        if start < EARLIEST_START_DATE:
            raise _invalid(
                "start_date",
                "USAspending's award search starts on 2007-10-01 (fiscal year 2008). Use a "
                "start_date on or after that day; older awards are only in its bulk downloads.",
            )
        if end < start:
            raise _invalid("end_date", "Give an end_date on or after start_date.")
        words = _keywords(keywords)
        name = (
            None
            if recipient is None
            else _text(
                recipient,
                "recipient",
                MIN_RECIPIENT_LENGTH,
                MAX_RECIPIENT_LENGTH,
                f"Give recipient as a name of {MIN_RECIPIENT_LENGTH} to "
                f"{MAX_RECIPIENT_LENGTH} characters.",
            )
        )
        if name is not None and '"' in name:
            # checked 2026-11-20: usaspending-api _RecipientSearchText switches a quoted
            # value to an exact full-name match, which the recipient warning does not
            # describe and which misses every other form of the name.
            raise _invalid("recipient", "Give recipient without quotation marks.")
        state_code = None
        if state is not None:
            state_code = state.strip().upper() if isinstance(state, str) else None
            if state_code not in STATE_CODES:
                raise _invalid(
                    "state",
                    "Give state as a two-letter postal code for a state, DC, or a U.S. "
                    "territory, for example CA.",
                )
        county_code = None
        if county is not None:
            county_code = county.strip() if isinstance(county, str) else None
            if county_code is None or not _COUNTY.fullmatch(county_code):
                raise _invalid(
                    "county",
                    "Give county as a three-digit county FIPS code, for example 075.",
                )
            if state_code is None:
                raise _invalid("county", "A county needs its state as well, for example CA.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise _invalid("limit", f"Give limit as a whole number from 1 to {MAX_LIMIT}.")
        if words is None and name is None and state_code is None:
            raise _invalid(None, "Give at least one of keywords, recipient, or state.")

        fields = FIELDS[kind]
        filters: dict[str, Any] = {
            "award_type_codes": list(AWARD_TYPE_CODES[kind]),
            "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
        }
        if words is not None:
            filters["keywords"] = words
        if name is not None:
            filters["recipient_search_text"] = [name]
        if state_code is not None:
            location = {"country": "USA", "state": state_code}
            if county_code is not None:
                location["county"] = county_code
            filters["place_of_performance_locations"] = [location]
        body = {
            "fields": list(fields),
            "filters": filters,
            "limit": limit,
            "order": "desc",
            "page": 1,
            "sort": SORT_FIELD[kind],
            "spending_level": "awards",
        }
        arguments = {
            "award_type": kind,
            "keywords": words,
            "recipient": name,
            "state": state_code,
            "county": county_code,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "limit": limit,
        }

        async def run() -> Lookup:
            payload = await self._http.json("POST", _SEARCH_ENDPOINT, json_body=body)
            rows, has_next, missing_fields = _rows(payload, fields, SORT_FIELD[kind])
            updated = await self._last_updated()
            schema = {"fields": list(fields), "missing_fields": missing_fields}
            result = SourceQueryResult(
                dataset_id=f"spending_by_award/{kind}",
                dataset_name=(
                    f"USAspending prime award search: {AWARD_TYPE_LABELS[kind]} "
                    f"(award type codes {', '.join(AWARD_TYPE_CODES[kind])})"
                ),
                domain=USASPENDING_HOST,
                source_url=ADVANCED_SEARCH_PAGE,
                query=json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                query_url=None,
                rows=rows,
                returned_row_count=len(rows),
                completeness=Completeness.TRUNCATED if has_next else Completeness.COMPLETE,
                source_updated_at=updated,
                schema_sha256=hashlib.sha256(
                    json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            )
            return Lookup(
                result=result,
                row_grain=ROW_GRAIN,
                warnings=warnings_for(
                    kind,
                    keywords=words is not None,
                    recipient=name is not None,
                    place=state_code is not None,
                    rows=rows,
                    updated=updated is not None,
                ),
            )

        return PreparedLookup(host=USASPENDING_HOST, arguments=arguments, run=run)

    async def _last_updated(self) -> str | None:
        """The day any award record last changed, as YYYY-MM-DD, or None if unavailable.

        The date is context for the result, so its failure must not sink a search that
        already succeeded; the evidence contract then says the source gave no date.
        """
        try:
            async with asyncio.timeout(LAST_UPDATED_TIMEOUT_SECONDS):
                payload = await self._http.json("GET", _LAST_UPDATED_ENDPOINT)
        except (SourceError, TimeoutError):
            return None
        value = payload.get("last_updated") if isinstance(payload, dict) else None
        match = _LAST_UPDATED.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            return None
        month, day, year = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    async def close(self) -> None:
        await self._http.close()
