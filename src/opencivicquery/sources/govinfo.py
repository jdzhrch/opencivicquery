"""GovInfo search service: federal court opinions in the USCOURTS collection.

GovInfo needs an api.data.gov key. The key travels only in the X-Api-Key header, never in
a URL, and nothing this module returns or raises carries it. The caller supplies only
search words, a court code, and dates; this module writes the whole GovInfo query, so no
caller text can act as GovInfo query syntax or reach another collection.

Search service: https://www.govinfo.gov/features/search-service-overview
Collection help: https://www.govinfo.gov/help/uscourts
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources.base import Lookup, PreparedLookup, SourceError, SourceQueryError
from opencivicquery.sources.http import FixedHostClient, source_reason

GOVINFO_HOST = "api.govinfo.gov"
SOURCE_NAME = "GovInfo API"
_SEARCH_ENDPOINT = f"https://{GOVINFO_HOST}/search"
COLLECTION = "USCOURTS"
COLLECTION_PAGE = "https://www.govinfo.gov/app/collection/uscourts"
_PUBLIC_SEARCH = "https://www.govinfo.gov/app/search/"
# checked 2026-11-20: this web search link, carrying the same query and offset 0, showed
# "48 Records" and the same first four opinions in the same order as the API search for
# Ninth Circuit opinions issued in 2024 with the words qualified and immunity.
_DETAILS_PAGE = "https://www.govinfo.gov/app/details/"
_CONTENT = "https://www.govinfo.gov/content/pkg/"
# official: govinfo.gov/help/uscourts, "Details Page for an Opinion" is
# https://www.govinfo.gov/app/details/{Package ID}/{Granule ID}, and "PDF File for an
# Opinion" is https://www.govinfo.gov/content/pkg/{Package ID}/pdf/{Granule ID}.pdf.
# checked 2026-11-20: a granule summary's detailsLink followed the first pattern, and the
# PDF for USCOURTS-ca9-23-15487-0 answered HTTP 200 with no key.

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
# official: search-service-overview, pageSize "Maximum is 1000". This server keeps one
# page small. checked 2026-11-20: pageSize 1001 returned HTTP 400 "pageSize should not be
# greater than 1000".
MIN_TEXT_LENGTH = 2
MAX_TEXT_LENGTH = 200
EARLIEST_DATE = date(1900, 1, 1)
LATEST_DATE = date(2100, 12, 31)

_API_KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_WORD = re.compile(r"[^\W_]+")
_PACKAGE_ID = re.compile(r"^USCOURTS-([A-Za-z0-9]+)-([A-Za-z0-9_.-]+)$")
_GRANULE_SUFFIX = re.compile(r"^-\d{1,4}$")
# official: govinfo.gov/help/uscourts, Package ID is "USCOURTS-{Court Code}-{Case Number}"
# with "Any colons (:) in the Case Number ... converted to underscores (_)", and Granule ID
# is "{Package ID}-{Sequence Number}".

COURTS: dict[str, str] = {
    # checked 2026-11-20: the Browse list on the collection page
    # (www.govinfo.gov/wssearch/rb/uscourts/{appellate,district,bankruptcy,national}),
    # which names each court that has opinions in USCOURTS, with its court code. It
    # named 168 courts. The court table on govinfo.gov/help/uscourts lists 37 more, such
    # as nysd and tc, that have no browse entry, and misspells some names, such as nynd.
    # The Northern District of Mississippi bankruptcy court has no code in the browse
    # list; msnb is its code in the help table. checked 2026-11-20: courtname phrases for
    # ca9, cand, caDC, and cofc each returned only opinions whose packageId carried that
    # court code.
    "ca1": "United States Court of Appeals for the First Circuit",
    "ca2": "United States Court of Appeals for the Second Circuit",
    "ca3": "United States Court of Appeals for the Third Circuit",
    "ca4": "United States Court of Appeals for the Fourth Circuit",
    "ca5": "United States Court of Appeals for the Fifth Circuit",
    "ca6": "United States Court of Appeals for the Sixth Circuit",
    "ca7": "United States Court of Appeals for the Seventh Circuit",
    "ca8": "United States Court of Appeals for the Eighth Circuit",
    "ca9": "United States Court of Appeals for the Ninth Circuit",
    "ca10": "United States Court of Appeals for the Tenth Circuit",
    "ca11": "United States Court of Appeals for the Eleventh Circuit",
    "caDC": "United States Court of Appeals for the District of Columbia Circuit",
    "ca13": "United States Court of Appeals for the Federal Circuit",
    "alnd": "United States District Court Northern District of Alabama",
    "alsd": "United States District Court Southern District of Alabama",
    "almd": "United States District Court Middle District of Alabama",
    "akd": "United States District Court District of Alaska",
    "azd": "United States District Court District of Arizona",
    "ared": "United States District Court Eastern District of Arkansas",
    "arwd": "United States District Court Western District of Arkansas",
    "caed": "United States District Court Eastern District of California",
    "cand": "United States District Court Northern District of California",
    "casd": "United States District Court Southern District of California",
    "cod": "United States District Court District of Colorado",
    "ctd": "United States District Court District of Connecticut",
    "ded": "United States District Court District of Delaware",
    "dcd": "United States District Court District of Columbia",
    "flnd": "United States District Court Northern District of Florida",
    "flsd": "United States District Court Southern District of Florida",
    "gand": "United States District Court Northern District of Georgia",
    "gamd": "United States District Court Middle District of Georgia",
    "gud": "United States District Court District of Guam",
    "hid": "United States District Court District of Hawaii",
    "idd": "United States District Court District of Idaho",
    "ilnd": "United States District Court Northern District of Illinois",
    "ilsd": "United States District Court Southern District of Illinois",
    "insd": "United States District Court Southern District of Indiana",
    "iand": "United States District Court Northern District of Iowa",
    "ksd": "United States District Court District of Kansas",
    "kyed": "United States District Court Eastern District of Kentucky",
    "kywd": "United States District Court Western District of Kentucky",
    "laed": "United States District Court Eastern District of Louisiana",
    "lamd": "United States District Court Middle District of Louisiana",
    "med": "United States District Court District of Maine",
    "mdd": "United States District Court District of Maryland",
    "mad": "United States District Court District of Massachusetts",
    "mied": "United States District Court Eastern District of Michigan",
    "miwd": "United States District Court Western District of Michigan",
    "mnd": "United States District Court District of Minnesota",
    "moed": "United States District Court Eastern District of Missouri",
    "mowd": "United States District Court Western District of Missouri",
    "mtd": "United States District Court District of Montana",
    "ned": "United States District Court District of Nebraska",
    "nhd": "United States District Court District of New Hampshire",
    "njd": "United States District Court District of New Jersey",
    "nmd": "United States District Court District of New Mexico",
    "nyed": "United States District Court Eastern District of New York",
    "nynd": "United States District Court Northern District of New York",
    "nywd": "United States District Court Western District of New York",
    "ncwd": "United States District Court Western District of North Carolina",
    "ncmd": "United States District Court Middle District of North Carolina",
    "ndd": "United States District Court District of North Dakota",
    "nmid": "United States District Court District of Northern Mariana Islands",
    "ohnd": "United States District Court Northern District of Ohio",
    "ohsd": "United States District Court Southern District of Ohio",
    "oked": "United States District Court Eastern District of Oklahoma",
    "okwd": "United States District Court Western District of Oklahoma",
    "ord": "United States District Court District of Oregon",
    "paed": "United States District Court Eastern District of Pennsylvania",
    "pawd": "United States District Court Western District of Pennsylvania",
    "pamd": "United States District Court Middle District of Pennsylvania",
    "prd": "United States District Court District of Puerto Rico",
    "rid": "United States District Court District of Rhode Island",
    "sdd": "United States District Court District of South Dakota",
    "tned": "United States District Court Eastern District of Tennessee",
    "tnwd": "United States District Court Western District of Tennessee",
    "tnmd": "United States District Court Middle District of Tennessee",
    "txed": "United States District Court Eastern District of Texas",
    "txnd": "United States District Court Northern District of Texas",
    "txsd": "United States District Court Southern District of Texas",
    "txwd": "United States District Court Western District of Texas",
    "utd": "United States District Court District of Utah",
    "vtd": "United States District Court District of Vermont",
    "vid": "United States District Court of the Virgin Islands",
    "waed": "United States District Court Eastern District of Washington",
    "wvnd": "United States District Court Northern District of West Virginia",
    "wiwd": "United States District Court Western District of Wisconsin",
    "wyd": "United States District Court District of Wyoming",
    "alnb": "United States Bankruptcy Court Northern District of Alabama",
    "alsb": "United States Bankruptcy Court Southern District of Alabama",
    "almb": "United States Bankruptcy Court Middle District of Alabama",
    "akb": "United States Bankruptcy Court District of Alaska",
    "azb": "United States Bankruptcy Court District of Arizona",
    "areb": "United States Bankruptcy Court Eastern District of Arkansas",
    "arwb": "United States Bankruptcy Court Western District of Arkansas",
    "caeb": "United States Bankruptcy Court Eastern District of California",
    "canb": "United States Bankruptcy Court Northern District of California",
    "cacb": "United States Bankruptcy Court Central District of California",
    "cob": "United States Bankruptcy Court District of Colorado",
    "ctb": "United States Bankruptcy Court District of Connecticut",
    "deb": "United States Bankruptcy Court District of Delaware",
    "dcb": "United States Bankruptcy Court District of Columbia",
    "flsb": "United States Bankruptcy Court Southern District of Florida",
    "flmb": "United States Bankruptcy Court Middle District of Florida",
    "ganb": "United States Bankruptcy Court Northern District of Georgia",
    "gamb": "United States Bankruptcy Court Middle District of Georgia",
    "hib": "United States Bankruptcy Court District of Hawaii",
    "idb": "United States Bankruptcy Court District of Idaho",
    "ilnb": "United States Bankruptcy Court Northern District of Illinois",
    "ilsb": "United States Bankruptcy Court Southern District of Illinois",
    "ilcb": "United States Bankruptcy Court Central District of Illinois",
    "insb": "United States Bankruptcy Court Southern District of Indiana",
    "ianb": "United States Bankruptcy Court Northern District of Iowa",
    "kyeb": "United States Bankruptcy Court Eastern District of Kentucky",
    "kywb": "United States Bankruptcy Court Western District of Kentucky",
    "laeb": "United States Bankruptcy Court Eastern District of Louisiana",
    "lawb": "United States Bankruptcy Court Western District of Louisiana",
    "lamb": "United States Bankruptcy Court Middle District of Louisiana",
    "meb": "United States Bankruptcy Court District of Maine",
    "mdb": "United States Bankruptcy Court District of Maryland",
    "mab": "United States Bankruptcy Court District of Massachusetts",
    "miwb": "United States Bankruptcy Court Western District of Michigan",
    "mnb": "United States Bankruptcy Court District of Minnesota",
    "msnb": "United States Bankruptcy Court Northern District of Mississippi",
    "moeb": "United States Bankruptcy Court Eastern District of Missouri",
    "mowb": "United States Bankruptcy Court Western District of Missouri",
    "mtb": "United States Bankruptcy Court District of Montana",
    "neb": "United States Bankruptcy Court District of Nebraska",
    "nvb": "United States Bankruptcy Court District of Nevada",
    "nhb": "United States Bankruptcy Court District of New Hampshire",
    "njb": "United States Bankruptcy Court District of New Jersey",
    "nmb": "United States Bankruptcy Court District of New Mexico",
    "nyeb": "United States Bankruptcy Court Eastern District of New York",
    "nynb": "United States Bankruptcy Court Northern District of New York",
    "nysb": "United States Bankruptcy Court Southern District of New York",
    "nywb": "United States Bankruptcy Court Western District of New York",
    "nceb": "United States Bankruptcy Court Eastern District of North Carolina",
    "ncwb": "United States Bankruptcy Court Western District of North Carolina",
    "ncmb": "United States Bankruptcy Court Middle District of North Carolina",
    "ndb": "United States Bankruptcy Court District of North Dakota",
    "ohnb": "United States Bankruptcy Court Northern District of Ohio",
    "ohsb": "United States Bankruptcy Court Southern District of Ohio",
    "okeb": "United States Bankruptcy Court Eastern District of Oklahoma",
    "oknb": "United States Bankruptcy Court Northern District of Oklahoma",
    "okwb": "United States Bankruptcy Court Western District of Oklahoma",
    "orb": "United States Bankruptcy Court District of Oregon",
    "paeb": "United States Bankruptcy Court Eastern District of Pennsylvania",
    "pawb": "United States Bankruptcy Court Western District of Pennsylvania",
    "pamb": "United States Bankruptcy Court Middle District of Pennsylvania",
    "prb": "United States Bankruptcy Court District of Puerto Rico",
    "rib": "United States Bankruptcy Court District of Rhode Island",
    "scb": "United States Bankruptcy Court District of South Carolina",
    "sdb": "United States Bankruptcy Court District of South Dakota",
    "tneb": "United States Bankruptcy Court Eastern District of Tennessee",
    "tnmb": "United States Bankruptcy Court Middle District of Tennessee",
    "txsb": "United States Bankruptcy Court Southern District of Texas",
    "txwb": "United States Bankruptcy Court Western District of Texas",
    "vawb": "United States Bankruptcy Court Western District of Virginia",
    "vib": "United States Bankruptcy Court District of the Virgin Islands",
    "waeb": "United States Bankruptcy Court Eastern District of Washington",
    "wawb": "United States Bankruptcy Court Western District of Washington",
    "wvnb": "United States Bankruptcy Court Northern District of West Virginia",
    "wieb": "United States Bankruptcy Court Eastern District of Wisconsin",
    "wiwb": "United States Bankruptcy Court Western District of Wisconsin",
    "wyb": "United States Bankruptcy Court District of Wyoming",
    "cofc": "United States Court of Federal Claims",
    "cit": "United States Court of International Trade",
    "jpml": "United States Judicial Panel on Multidistrict Litigation",
}
_COURT_BY_LOWER = {code.lower(): code for code in COURTS}

SOURCE_FIELDS = (
    "title",
    "packageId",
    "granuleId",
    "dateIssued",
    "dateIngested",
    "lastModified",
    "governmentAuthor",
    "collectionCode",
)
DERIVED_FIELDS = ("court_code", "case_number", "details_url", "pdf_url")
FIELDS = (*SOURCE_FIELDS, *DERIVED_FIELDS)

ROW_GRAIN = (
    "One court opinion document (a GovInfo granule) within one federal court case (a GovInfo "
    "package). A case with several opinions appears once per opinion."
)
# official: govinfo.gov/help/uscourts, "The Package ID is used to identify an individual
# case ... The Granule ID is used to identify individual opinions within each case."
# checked 2026-11-20: every search result carried a granuleId, and the same search with
# resultLevel package returned a count of 0.

SEVERAL_OPINIONS_WARNING = (
    "The count is of opinion documents, not cases or rulings. One case can appear more than "
    "once, for example as an opinion and later as its amended opinion. Count distinct "
    "packageId values to count cases."
)
# checked 2026-11-20: Ninth Circuit case 22-56054 matched twice, as the opinion filed
# 2024-03-05 (granule -0) and the amended opinion filed 2024-04-02 (granule -1).

COVERAGE_WARNING = (
    "GovInfo's United States Courts Opinions collection holds opinions from selected federal "
    "appellate, district, and bankruptcy courts and a few national courts, generally from 2004 "
    "on, and its holdings for some courts are incomplete in the early years. Many federal "
    "courts are not in it at all: on 2026-11-20 its court list omitted, for example, the "
    "Southern District of New York and the Central District of California. It holds "
    "opinions, not dockets or other filings, so a missing opinion here does not show that a "
    "court never ruled. GovInfo points readers to PACER, which requires registration, for "
    "case and docket information."
)
# official: govinfo.gov/help/uscourts, "provide public access to opinions from selected
# United States appellate, district, and bankruptcy courts. The content of this collection
# dates back, generally, to 2004, though searchable electronic holdings for some courts may
# be incomplete for this earlier time period"; "Opinions from select National courts";
# Related Resources lists PACER for "case and docket information" and "Requires
# registration for access." checked 2026-11-20: the collection's Browse list (see COURTS)
# had no entry for nysd or cacd.

WORDS_WARNING = (
    "GovInfo matched every search word anywhere in each opinion's full text or metadata, not "
    "as a phrase, so a match does not show what the opinion decided. Read each opinion before "
    "counting it as relevant. Search operators and field names in the text, such as OR or "
    "collection:, are searched as ordinary words."
)
# official: govinfo.gov/help/finding-info, Basic Search searches "over the metadata and
# full text"; govinfo.gov/help/search-operators, a word in quotes "is treated as a phrase
# instead of being treated as an operator". checked 2026-11-20: the words qualified and
# immunity matched 97,230 opinions and the exact phrase matched 82,161.

RANKING_WARNING = (
    "Rows come in GovInfo's relevance order. When the result is partial, the rows are the "
    "top-ranked matches, not the newest and not a random sample, and the other matches are "
    "counted but not returned."
)
# official: search-service-overview, sort field score is "equivalent to 'Relevancy' in the
# user interface"; this module sorts by score descending.

DATE_WARNING = (
    "dateIssued is the date GovInfo lists for each opinion, and issued_from and issued_to "
    "filter on it. GovInfo's help defines it as the date the document was first made "
    "available to the public; in checks it matched the filing date in the court's docket "
    "entry. dateIngested, when GovInfo received the file, can be years later. Confirm the "
    "date in the opinion itself before reporting when a court ruled."
)
# official: govinfo.gov/help/uscourts, Publication Date (publishdate:) is "The date the
# document was first made available to the public." checked 2026-11-20: a publishdate range
# for 2024 returned rows with dateIssued from 2024-01-11 to 2024-12-30 and dateIngested up
# to 2026-09-03; ca9 23-15487 had dateIssued 2024-09-04 and docket text "FILED OPINION ...
# Wednesday, September 4, 2024"; nmd 1:19-cv-00182 had dateIssued 2020-05-19 and
# dateIngested 2026-05-18.

GOVINFO_RULE = (
    "Cite each opinion by its details_url, with the court, the case_number, and dateIssued "
    "beside it. Say the count is of opinion documents that contain the search words."
)
NO_MATCH_RULE = (
    "NO OPINIONS MATCHED. Say that GovInfo's court opinions collection returned no opinion "
    "with these words. Do not say that no court ruled on the subject."
)


def _invalid(argument: str, message: str) -> SourceQueryError:
    return SourceQueryError(
        message, code="INVALID_QUERY_ARGUMENT", argument=argument, public_message=message
    )


def search_words(text: str) -> list[str]:
    """Return the words to search for, or raise. Everything else in the text is dropped."""
    if not isinstance(text, str):
        raise _invalid("text", "Give text as search words.")
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    if not MIN_TEXT_LENGTH <= len(normalized) <= MAX_TEXT_LENGTH or any(
        unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in normalized
    ):
        raise _invalid(
            "text", f"Give text as {MIN_TEXT_LENGTH} to {MAX_TEXT_LENGTH} characters of words."
        )
    words = _WORD.findall(normalized)
    if not words:
        raise _invalid("text", "Give text with at least one word of letters or digits.")
    return words


def validate_court(court: str | None) -> str | None:
    """Return the canonical GovInfo court code, or raise."""
    if court is None:
        return None
    code = _COURT_BY_LOWER.get(court.strip().lower()) if isinstance(court, str) else None
    if code is None:
        raise _invalid(
            "court",
            "Give court as the code of a court in GovInfo's court opinions collection, such "
            "as ca9 for the Ninth Circuit or cand for the Northern District of California. "
            "Some federal courts, such as nysd, are not in the collection.",
        )
    return code


def _date(argument: str, value: str | None) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.match(value.strip()):
        raise _invalid(argument, f"Give {argument} as YYYY-MM-DD.")
    try:
        day = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise _invalid(argument, f"{argument} is not a real calendar date.") from exc
    if not EARLIEST_DATE <= day <= LATEST_DATE:
        raise _invalid(argument, f"Give {argument} between 1900-01-01 and 2100-12-31.")
    return day


def build_query(
    words: list[str], court: str | None, issued_from: date | None, issued_to: date | None
) -> str:
    """Write the GovInfo query. Each word sits alone in quotes, so none acts as an operator."""
    parts = [f"collection:({COLLECTION})"]
    if court is not None:
        parts.append(f'courtname:"{COURTS[court]}"')
    if issued_from is not None or issued_to is not None:
        first = issued_from.isoformat() if issued_from else ""
        last = issued_to.isoformat() if issued_to else ""
        # official: govinfo.gov/help/search-operators, publishdate:range(2006-01-01,
        # 2006-07-01) covers "January 1, 2006 through July 1, 2006", and an open end is
        # written as publishdate:range(2006-01-01,).
        parts.append(f"publishdate:range({first},{last})")
    parts.append("(" + " ".join(f'"{word}"' for word in words) + ")")
    return " and ".join(parts)


def public_search_url(query: str) -> str:
    state = json.dumps({"query": query, "offset": 0}, separators=(",", ":"))
    return _PUBLIC_SEARCH + quote(state, safe="")


class _KeyedClient(FixedHostClient):
    """FixedHostClient that also sends the api.data.gov key header on every request."""

    def __init__(self, *, api_key: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if api_key is not None:
            # official: api.data.gov/docs/developer-manual, "Pass the API key into the
            # X-Api-Key header". The header keeps the key out of every URL.
            self._headers = {**self._headers, "X-Api-Key": api_key}


class GovInfo:
    """Search federal court opinions in GovInfo's USCOURTS collection."""

    host = GOVINFO_HOST
    row_grain = ROW_GRAIN

    def __init__(self, *, api_key: str | None, client: httpx.AsyncClient | None = None) -> None:
        key = api_key.strip() if api_key else ""
        if key and not _API_KEY.fullmatch(key):
            raise ValueError("The GovInfo API key must be letters, digits, hyphens, or underscores")
        if client is not None and client.follow_redirects:
            # httpx keeps custom headers on a redirect to another host, so the key would
            # follow it there.
            raise ValueError("The GovInfo client must not follow redirects")
        self._api_key = key or None
        self._http = _KeyedClient(api_key=self._api_key, source_name=SOURCE_NAME, client=client)

    def _redact(self, value: Any) -> Any:
        """Remove the key from every string in a JSON value."""
        if isinstance(value, str):
            return value.replace(self._api_key, "[redacted]") if self._api_key else value
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        return value

    def search_opinions(
        self,
        *,
        text: str,
        court: str | None = None,
        issued_from: str | None = None,
        issued_to: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> PreparedLookup:
        """Validate one search without touching the network and return it ready to run."""
        if self._api_key is None:
            raise SourceQueryError(
                "No GovInfo API key configured",
                code="SOURCE_NOT_CONFIGURED",
                public_message="This server has no GovInfo API key configured.",
            )
        words = search_words(text)
        code = validate_court(court)
        first = _date("issued_from", issued_from)
        last = _date("issued_to", issued_to)
        if first and last and last < first:
            raise _invalid("issued_to", "issued_to must be on or after issued_from.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise _invalid("limit", f"Give limit as a whole number from 1 to {MAX_LIMIT}.")
        query = build_query(words, code, first, last)
        body = {
            "offsetMark": "*",
            "pageSize": limit,
            "query": query,
            "sorts": [{"field": "score", "sortOrder": "DESC"}],
        }
        arguments = {
            "text": " ".join(unicodedata.normalize("NFKC", text).split()),
            "court": code,
            "issued_from": first.isoformat() if first else None,
            "issued_to": last.isoformat() if last else None,
            "limit": limit,
        }

        async def run() -> Lookup:
            payload = await self._post(body)
            count, rows = self._rows(payload, limit, code)
            court_label = f", court {code} ({COURTS[code]})" if code else ""
            result = SourceQueryResult(
                dataset_id=COLLECTION,
                dataset_name=(
                    f"GovInfo United States Courts Opinions (USCOURTS) search{court_label}: "
                    f"GovInfo counts {count} matching opinion documents"
                ),
                domain=GOVINFO_HOST,
                source_url=COLLECTION_PAGE,
                query=json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                query_url=public_search_url(query),
                rows=rows,
                returned_row_count=len(rows),
                completeness=(
                    Completeness.TRUNCATED if count > len(rows) else Completeness.COMPLETE
                ),
                schema_sha256=hashlib.sha256(
                    json.dumps({"fields": list(FIELDS)}, separators=(",", ":")).encode()
                ).hexdigest(),
            )
            return Lookup(
                result=result,
                row_grain=ROW_GRAIN,
                warnings=[
                    SEVERAL_OPINIONS_WARNING,
                    COVERAGE_WARNING,
                    WORDS_WARNING,
                    RANKING_WARNING,
                    DATE_WARNING,
                ],
                rules=([NO_MATCH_RULE] if count == 0 else []) + [GOVINFO_RULE],
                escalations=["NO_OPINIONS_MATCHED"] if count == 0 else [],
            )

        return PreparedLookup(host=GOVINFO_HOST, arguments=arguments, run=run)

    async def _post(self, body: dict[str, Any]) -> Any:
        status, content = await self._http.send("POST", _SEARCH_ENDPOINT, json_body=body)
        try:
            payload = json.loads(content) if content else None
        except (ValueError, RecursionError):
            payload = None
        if status == 200:
            if payload is None:
                raise SourceError("The GovInfo API returned a body that is not JSON")
            return payload
        error = payload.get("error") if isinstance(payload, dict) else None
        # Redact before source_reason shortens the text, so no part of the key survives.
        reason = source_reason(self._redact(error if isinstance(error, dict) else payload))
        reason = reason or f"HTTP {status}"
        if status in {401, 403}:
            # checked 2026-11-20: a search with no key returned HTTP 401 with error code
            # API_KEY_MISSING.
            code = "SOURCE_ACCESS_DENIED"
            public = f"GovInfo refused this server's API key (HTTP {status})."
        elif status == 429:
            # official: api.data.gov/docs/developer-manual, over the limit "you will receive
            # a response with an HTTP status code of 429 (Too Many Requests)".
            code = "SOURCE_RATE_LIMITED"
            public = "GovInfo's request limit for this server's key is used up. Try again later."
        elif status == 400:
            code = "QUERY_REJECTED"
            public = f"GovInfo rejected the search: {reason}"
        else:
            code = "SOURCE_UNAVAILABLE"
            public = f"The GovInfo API could not complete the request (HTTP {status})."
        raise SourceQueryError(
            f"GovInfo HTTP {status}: {reason}", code=code, http_status=status, public_message=public
        )

    def _rows(
        self, payload: Any, limit: int, court: str | None
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return GovInfo's match count and the projected rows, or raise SourceError."""
        if not isinstance(payload, dict):
            raise SourceError("The GovInfo search answer is not an object")
        count = payload.get("count")
        results = payload.get("results")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SourceError("The GovInfo search answer has no match count")
        if not isinstance(results, list) or len(results) > limit or len(results) > count:
            raise SourceError("The GovInfo search answer does not agree with its count")
        rows = [self._row(record, court) for record in results]
        if len({row["granuleId"] for row in rows}) < len(rows):
            raise SourceError("The GovInfo search returned the same opinion twice")
        return count, rows

    def _row(self, record: Any, court: str | None) -> dict[str, Any]:
        if not isinstance(record, dict) or record.get("collectionCode") != COLLECTION:
            # The query always names the collection; anything else means it was not applied.
            raise SourceError("The GovInfo search returned a record outside USCOURTS")
        package = record.get("packageId")
        granule = record.get("granuleId")
        match = _PACKAGE_ID.fullmatch(package) if isinstance(package, str) else None
        if (
            match is None
            or not isinstance(granule, str)
            or not granule.startswith(package)
            or not _GRANULE_SUFFIX.fullmatch(granule[len(package) :])
            or (self._api_key is not None and self._api_key in granule)
        ):
            raise SourceError("The GovInfo search returned an opinion without valid IDs")
        code, case = match.groups()
        if court is not None and code.lower() != court.lower():
            # The court filter is a name phrase; an opinion from another court means the
            # filter did not hold, so the count cannot be trusted either.
            raise SourceError("The GovInfo search returned an opinion from another court")
        row = {name: self._clean(record.get(name)) for name in SOURCE_FIELDS}
        row["court_code"] = code
        row["case_number"] = case.replace("_", ":")
        row["details_url"] = f"{_DETAILS_PAGE}{package}/{granule}"
        row["pdf_url"] = f"{_CONTENT}{package}/pdf/{granule}.pdf"
        return row

    def _clean(self, value: Any) -> Any:
        """Keep scalars and lists of strings; redact the key from any text."""
        if isinstance(value, list):
            return [self._redact(item) for item in value if isinstance(item, str)]
        if value is None or isinstance(value, (str, bool, int, float)):
            return self._redact(value)
        return None

    async def close(self) -> None:
        await self._http.close()
