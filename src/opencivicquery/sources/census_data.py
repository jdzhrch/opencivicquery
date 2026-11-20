"""The U.S. Census Bureau's Census Data API: American Community Survey 5-year estimates.

Every data query needs the server's API key, which the API takes only as a query
parameter. The key is added to the request at the last moment and never enters a
query, a URL, a row, a warning, or an error that leaves this module. Variable labels
come from the API's keyless metadata endpoint. The host is fixed, and the caller's
geography clauses are checked against the API's own level names before any request.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources.base import Lookup, PreparedLookup, SourceError, SourceQueryError
from opencivicquery.sources.http import FixedHostClient

CENSUS_DATA_HOST = "api.census.gov"
_BASE = f"https://{CENSUS_DATA_HOST}/data"
SOURCE_NAME = "Census Data API"
SOURCE_PAGE = "https://www.census.gov/data/developers/data-sets/acs-5year.html"

DATASETS = {
    "acs/acs5": "detailed tables",
    "acs/acs5/subject": "subject tables",
    "acs/acs5/profile": "data profiles",
}
FIRST_YEAR = {"acs/acs5": 2009, "acs/acs5/subject": 2010, "acs/acs5/profile": 2009}
# checked 2026-11-20: api.census.gov/data/{year}/{dataset}.json answered 200 for these
# first years through 2024 and 404 for 2008 (2009 for subject) and for 2025.

MAX_VARIABLES = 20
# official: Census Data API User Guide, May 21, 2026, "You can include up to 50 variables
# in a single API query." This server asks for one label per variable, so it allows fewer.
MAX_ROWS = 5000
# The API has no paging; it returns every matching geography at once. checked 2026-11-20:
# all California tracts were 9,129 rows and 899,151 bytes for four variables.
MAX_GEOGRAPHY_LENGTH = 500
MAX_CODES = 50

GEOGRAPHY_LEVELS = (
    "us",
    "region",
    "division",
    "state",
    "county",
    "county subdivision",
    "subminor civil division",
    "place/remainder (or part)",
    "tract",
    "block group",
    "county (or part)",
    "place",
    "consolidated city",
    "place (or part)",
    "alaska native regional corporation",
    "american indian area/alaska native area/hawaiian home land",
    "tribal subdivision/remainder",
    "american indian area/alaska native area (reservation or statistical entity only)",
    "american indian area (off-reservation trust land only)/hawaiian home land",
    "tribal census tract",
    "tribal block group",
    "state (or part)",
    "american indian area/alaska native area/hawaiian home land (or part)",
    "american indian area/alaska native area (reservation or statistical entity only) (or part)",
    "american indian area (off-reservation trust land only)/hawaiian home land (or part)",
    "tribal census tract (or part)",
    "tribal block group (or part)",
    "metropolitan statistical area/micropolitan statistical area",
    "principal city (or part)",
    "metropolitan division",
    "metropolitan statistical area/micropolitan statistical area (or part)",
    "metropolitan division (or part)",
    "combined statistical area",
    "combined statistical area (or part)",
    "urban area",
    "congressional district",
    "state legislative district (upper chamber)",
    "state legislative district (lower chamber)",
    "public use microdata area",
    "zip code tabulation area",
    "school district (elementary)",
    "school district (secondary)",
    "school district (unified)",
)
# checked 2026-11-20: every distinct level name in
# api.census.gov/data/2024/acs/acs5/geography.json. Which levels nest in which is left to
# the API, which answers HTTP 400 "unknown/unsupported geography hierarchy".

_LEVEL = "|".join(re.escape(level) for level in sorted(GEOGRAPHY_LEVELS, key=len, reverse=True))
_CLAUSE = re.compile(
    rf"(?P<level>(?i:{_LEVEL})):"
    rf"(?P<codes>\*|[0-9A-Za-z]{{1,7}}(?:,[0-9A-Za-z]{{1,7}}){{0,{MAX_CODES - 1}}})",
    re.ASCII,
)
# checked 2026-11-20: codes match exactly and in upper case. county:75 in state:06 and
# Vermont's upper chamber district chi returned HTTP 204; district CHI is listed as a code.
_VARIABLE = re.compile(r"[A-Za-z0-9_]{1,50}", re.ASCII)

ANNOTATION_CODES = frozenset(
    {"-666666666", "-999999999", "-888888888", "-222222222", "-333333333", "-555555555"}
)

ANNOTATION_WARNING = (
    "Negative values such as -666666666 are annotation codes that stand in for an estimate "
    "or margin of error. -666666666: the estimate could not be computed, for example because "
    "there were too few sample observations. -999999999: too few sample cases in the area to "
    "display the figure. -888888888: not applicable or not available. -222222222 or "
    "-333333333: the margin of error could not be computed. -555555555: the estimate is "
    "controlled to an independent population or housing estimate, so its margin of error may "
    "be treated as zero. A null value means no data is available for that geography. Keep "
    "every code out of sums, averages, and rankings."
)
# official: Census Bureau, "Notes on ACS Estimate and Annotation Values", revised
# 2026-07-20; checked 2026-11-20: 30 such codes among 244 San Francisco tracts for
# B19013_001E and B19013_001M in the 2024 ACS 5-year.

MOE_WARNING = (
    "ACS figures are survey estimates with margins of error. A variable ending in E is an "
    "estimate, and the variable with the same name ending in M is its margin of error; in "
    "data profiles, PE is a percent estimate and PM its margin of error. Published ACS margins "
    "of error use a 90 percent confidence level. Give the margin of error beside any estimate "
    "you report."
)
# official: Census Data API User Guide, May 21, 2026, "E and M" and "PE and PM"; ACS
# Multiyear Accuracy of the Data (5-year 2019-2023): "All ACS published margins of error
# are based on a 90 percent confidence level."

KEY_WARNING = (
    "query_url leaves out this server's API key. To rerun it, add &key= and your own free "
    "Census Data API key from census.gov/data/developers; without a key the API answers with "
    "its missing-key page instead of data."
)
# official: Census Data API User Guide, May 21, 2026, "An API key must be used with all
# data queries"; checked 2026-11-20: a keyless query returned HTTP 302 to
# api.census.gov/data/missing_key.html.

TERMS_NOTICE = (
    "This product uses the Census Bureau Data API but is not endorsed or certified by the "
    "Census Bureau."
)
# official: Census Data API Terms of Service, the notice every service that uses the API
# must display.

ANNOTATION_RULE = (
    "ANNOTATION CODES PRESENT. Some values are negative codes such as -666666666, which "
    "stand in for an estimate the Census Bureau could not compute or display. Never quote, "
    "sum, average, or rank them as numbers; name the geographies they affect."
)
CENSUS_RULE = (
    "Name the ACS 5-year period, such as 2020-2024, and give the margin of error beside any "
    "estimate you quote. Name each geography as its NAME value gives it."
)


# official: ACS Multiyear Accuracy of the Data (5-year 2019-2023), Introduction ("These
# estimates cannot be used to describe what is going on in any particular year in the
# period") and Estimation Methodology ("inflation-adjusted to the final year of the period").
def _period_warning(year: int) -> str:
    return (
        f"These are ACS 5-year estimates for {year - 4}-{year}. They describe the average over "
        f"January 1, {year - 4} through December 31, {year}, and cannot describe any single "
        f"year in that period. Dollar amounts are in {year} inflation-adjusted dollars."
    )


# official: ACS Multiyear Accuracy of the Data (5-year 2019-2023): "the tabulation geography
# for the data are referenced to the final year in the multiyear period".
def _geography_warning(year: int) -> str:
    return (
        f"Geographies follow the boundaries in place for {year}, the final year of the "
        "period, and all data collected over the period is tabulated as though those "
        "boundaries applied throughout. Codes and boundaries can differ in other years, so "
        "match the vintage before joining these rows to other data."
    )


# checked 2026-11-20: county:075,999 in state:06 returned HTTP 200 with county 075 only.
def _unmatched_warning(level: str, codes: list[str]) -> str:
    return (
        f"No row came back for {level} {', '.join(codes)}: the Census Data API returned no "
        "area with that code for this dataset, year, and geography_in. The rows cover only "
        f"the codes in the {level} column."
    )


def _invalid(argument: str, message: str) -> SourceQueryError:
    return SourceQueryError(
        message, code="INVALID_QUERY_ARGUMENT", argument=argument, public_message=message
    )


def validate_dataset(dataset: str) -> str:
    if not isinstance(dataset, str) or dataset not in DATASETS:
        raise _invalid(
            "dataset",
            "Give dataset as acs/acs5 (detailed tables), acs/acs5/subject (subject tables), "
            "or acs/acs5/profile (data profiles).",
        )
    return dataset


def validate_year(year: int, dataset: str) -> int:
    first = FIRST_YEAR[dataset]
    last = datetime.now(UTC).year - 1
    if isinstance(year, bool) or not isinstance(year, int) or not first <= year <= last:
        raise _invalid(
            "year",
            f"Give year as the final year of the 5-year period, a whole number from {first} "
            f"to {last} for {dataset}; 2024 means the 2020-2024 estimates.",
        )
    return year


def validate_variables(variables: list[str]) -> list[str]:
    message = (
        f"Give variables as a list of 1 to {MAX_VARIABLES} distinct Census variable names, "
        "such as NAME, B01003_001E, and B01003_001M."
    )
    if not isinstance(variables, (list, tuple)) or not 1 <= len(variables) <= MAX_VARIABLES:
        raise _invalid("variables", message)
    names = []
    for item in variables:
        if not isinstance(item, str) or not _VARIABLE.fullmatch(item.strip()):
            raise _invalid("variables", message)
        names.append(item.strip().upper())
    if len(set(names)) != len(names):
        raise _invalid("variables", message)
    return names


def validate_geography(
    argument: str, text: str, *, single: bool
) -> tuple[str, list[tuple[str, str]]]:
    """Return the clause text as it will be sent, and its (level, codes) pairs."""
    example = (
        "one clause such as tract:*, county:075, or place:67000"
        if single
        else "clauses separated by spaces, such as state:06 county:075"
    )
    message = (
        f"Give {argument} as {example}: a Census Data API geography level, a colon, and "
        f"either * or up to {MAX_CODES} comma-separated codes, such as FIPS codes."
    )
    if (
        not isinstance(text, str)
        or len(text) > MAX_GEOGRAPHY_LENGTH
        or not text.isascii()
        or not text.isprintable()
    ):
        raise _invalid(argument, message)
    spaced = " ".join(text.split())
    clauses: list[tuple[str, str]] = []
    position = 0
    while True:
        match = _CLAUSE.match(spaced, position)
        if match is None:
            raise _invalid(argument, message)
        clauses.append((match["level"].lower(), match["codes"].upper()))
        position = match.end()
        if position == len(spaced):
            break
        if single or spaced[position] != " ":
            raise _invalid(argument, message)
        position += 1
    levels = [level for level, _ in clauses]
    if len(set(levels)) != len(levels):
        raise _invalid(argument, f"Name each geography level once in {argument}.")
    for _, codes in clauses:
        listed = codes.split(",")
        if len(set(listed)) != len(listed):
            raise _invalid(argument, f"List each code once in {argument}.")
    return " ".join(f"{level}:{codes}" for level, codes in clauses), clauses


def _schema_hash(dataset: str, year: int, header: list[str], labels: dict[str, Any]) -> str:
    schema = {"dataset": dataset, "year": year, "header": header, "variables": labels}
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _rows(payload: Any, variables: list[str], level: str) -> tuple[list[str], list[dict]]:
    """Turn the API's array of arrays into one dict per geography, values untouched."""
    if not isinstance(payload, list) or not payload:
        raise SourceError("The Census Data API returned something other than a table")
    header = payload[0]
    if not isinstance(header, list) or not all(isinstance(name, str) for name in header):
        raise SourceError("The Census Data API returned a malformed header row")
    if header[: len(variables)] != variables or level not in header[len(variables) :]:
        # Every label and warning names these variables and this level, so an answer
        # that does not confirm them must never be frozen under them.
        raise SourceError("The Census Data API header did not confirm the request")
    if len(set(header)) != len(header):
        raise SourceError("The Census Data API returned a duplicate column")
    rows = []
    for record in payload[1:]:
        if (
            not isinstance(record, list)
            or len(record) != len(header)
            or not all(value is None or isinstance(value, str) for value in record)
        ):
            raise SourceError("The Census Data API returned a malformed row")
        rows.append(dict(zip(header, record, strict=True)))
    return header, rows


def _api_error_text(content: bytes, key: str) -> str:
    """The API's plain-text reason for a 400, with the key removed, capped at 300 characters."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    text = " ".join(text.replace(key, "[REDACTED]").split())
    if not text or text.startswith("<") or not text.isprintable():
        return ""
    return text[:300]


class CensusData:
    """Read ACS 5-year estimates from the Census Data API with this server's key."""

    host = CENSUS_DATA_HOST

    def __init__(self, *, api_key: str | None, client: httpx.AsyncClient | None = None) -> None:
        self._key = SecretStr(api_key.strip()) if api_key and api_key.strip() else None
        self._http = FixedHostClient(source_name=SOURCE_NAME, client=client)

    async def _data(self, year: int, dataset: str, params: dict[str, str], key: str) -> Any:
        status, content = await self._http.send(
            "GET", f"{_BASE}/{year}/{dataset}", params={**params, "key": key}
        )
        kind = DATASETS[dataset]
        if status == 200:
            try:
                return json.loads(content)
            except ValueError as exc:
                raise SourceError("The Census Data API returned a body that is not JSON") from exc
        if status == 204:
            # checked 2026-11-20: county:999 in state:06 returned HTTP 204 with no body.
            raise SourceQueryError(
                f"Census Data API HTTP 204 for {params}",
                code="NOT_FOUND",
                argument=None if "in" in params else "geography_for",
                http_status=status,
                public_message=(
                    f"The Census Data API has no ACS 5-year {kind} for {year} at this "
                    "geography: no area matched the codes given. Check the codes in "
                    "geography_for and geography_in, and that the area existed in that year."
                ),
            )
        if status == 302:
            # checked 2026-11-20: an unregistered key returned HTTP 302 to
            # api.census.gov/data/invalid_key.html.
            raise SourceQueryError(
                "Census Data API HTTP 302: the key was refused",
                code="SOURCE_KEY_REJECTED",
                http_status=status,
                public_message="The Census Data API did not accept this server's API key.",
            )
        if status == 404:
            # checked 2026-11-20: year 2025 of acs/acs5 returned an HTML 404.
            raise SourceQueryError(
                f"Census Data API HTTP 404 for {year}/{dataset}",
                code="NOT_FOUND",
                argument="year",
                http_status=status,
                public_message=(
                    f"The Census Data API does not serve ACS 5-year {kind} for {year}. That "
                    "release may not be out yet; try an earlier year."
                ),
            )
        if status == 400:
            # checked 2026-11-20: HTTP 400 bodies are plain text, such as "error: unknown
            # variable 'B99999_999E'" or "error: unknown/unsupported geography hierarchy".
            detail = _api_error_text(content, key)
            raise SourceQueryError(
                f"Census Data API HTTP 400: {detail}",
                code="QUERY_REJECTED",
                http_status=status,
                public_message=detail or "The Census Data API rejected the request (HTTP 400).",
            )
        raise SourceQueryError(
            f"Census Data API HTTP {status}",
            code="SOURCE_UNAVAILABLE",
            http_status=status,
            public_message=f"The Census Data API could not complete the request (HTTP {status}).",
        )

    async def _label(self, year: int, dataset: str, name: str) -> dict[str, Any]:
        """One variable's label, concept, and margin-of-error partner, from keyless metadata."""
        payload = await self._http.json("GET", f"{_BASE}/{year}/{dataset}/variables/{name}.json")
        # checked 2026-11-20: this endpoint answers without a key, for example
        # {"name": "B01003_001E", "label": "Estimate!!Total", "concept": "Total Population",
        # "group": "B01003", "attributes": "B01003_001EA,B01003_001M,B01003_001MA"}.
        if not isinstance(payload, dict) or payload.get("name") != name:
            raise SourceError("The Census Data API variable metadata did not confirm the name")
        label = payload.get("label")
        if not isinstance(label, str) or not label:
            raise SourceError("The Census Data API variable metadata has no label")
        group = payload.get("group")
        concept = payload.get("concept")
        # NAME and GEO_ID belong to every table, so their group and concept list them all.
        single_table = isinstance(group, str) and "," not in group and group != "N/A"
        attributes = payload.get("attributes")
        partners = attributes.split(",") if isinstance(attributes, str) else []
        if name.endswith("PE"):
            moe = name[:-2] + "PM"
        elif name.endswith("E"):
            moe = name[:-1] + "M"
        else:
            moe = None
        return {
            "label": label,
            "concept": concept if single_table and isinstance(concept, str) else None,
            "moe": moe if moe in partners else None,
        }

    def get_data(
        self,
        *,
        year: int,
        variables: list[str],
        geography_for: str,
        geography_in: str | None = None,
        dataset: str = "acs/acs5",
    ) -> PreparedLookup:
        """Validate without touching the network; the returned lookup fetches and labels."""
        secret = self._key
        if secret is None:
            raise SourceQueryError(
                "No Census Data API key configured",
                code="SOURCE_NOT_CONFIGURED",
                public_message="This server has no Census Data API key configured.",
            )
        path = validate_dataset(dataset)
        vintage = validate_year(year, path)
        names = validate_variables(variables)
        for_text, ((level, for_codes),) = validate_geography(
            "geography_for", geography_for, single=True
        )
        in_text = None
        if geography_in is not None:
            in_text, in_clauses = validate_geography("geography_in", geography_in, single=False)
            if level in {name for name, _ in in_clauses}:
                raise _invalid("geography_in", "Do not repeat the geography_for level here.")
        params = {"get": ",".join(names), "for": for_text}
        if in_text is not None:
            params["in"] = in_text
        query = f"/data/{vintage}/{path}?{urlencode(params)}"

        async def run() -> Lookup:
            payload = await self._data(vintage, path, params, secret.get_secret_value())
            header, rows = _rows(payload, names, level)
            if len(rows) > MAX_ROWS:
                raise SourceQueryError(
                    f"Census Data API returned {len(rows)} rows",
                    code="RESULT_TOO_LARGE",
                    argument="geography_for",
                    public_message=(
                        f"The Census Data API returned {len(rows)} geographies, more than this "
                        f"server returns at once ({MAX_ROWS}). Narrow geography_for or add "
                        "geography_in, for example one county at a time."
                    ),
                )
            labels = dict(
                zip(
                    names,
                    await asyncio.gather(*(self._label(vintage, path, name) for name in names)),
                    strict=True,
                )
            )
            described = "; ".join(
                f"{name} = {meta['label']}" + (f" ({meta['concept']})" if meta["concept"] else "")
                for name, meta in labels.items()
            )
            missing = [
                meta["moe"]
                for meta in labels.values()
                if meta["moe"] is not None and meta["moe"] not in names
            ]
            moe_warning = MOE_WARNING + (
                f" This request did not include these margins of error: {', '.join(missing)}."
                if missing
                else ""
            )
            # Codes in a margin of error are routine: every controlled county or state
            # total carries -555555555. Only a coded estimate is escalated.
            estimates = [
                name for name in names if name not in {"NAME", "GEO_ID"} and not name.endswith("M")
            ]
            coded = any(
                row[name] is None or row[name] in ANNOTATION_CODES
                for row in rows
                for name in estimates
            )
            returned = {row[level] for row in rows}
            unmatched = [code for code in for_codes.split(",") if code not in returned]
            warnings = [
                moe_warning,
                _period_warning(vintage),
                _geography_warning(vintage),
                ANNOTATION_WARNING,
                KEY_WARNING,
                TERMS_NOTICE,
            ]
            if coded:
                warnings.remove(ANNOTATION_WARNING)
                warnings.insert(0, ANNOTATION_WARNING)
            if for_codes != "*" and unmatched:
                warnings.insert(0, _unmatched_warning(level, unmatched))
            result = SourceQueryResult(
                dataset_id=f"{vintage}/{path}",
                dataset_name=(
                    f"American Community Survey 5-year estimates {vintage - 4}-{vintage}, "
                    f"{DATASETS[path]}; variables: {described}"
                ),
                domain=CENSUS_DATA_HOST,
                source_url=SOURCE_PAGE,
                query=query,
                query_url=f"https://{CENSUS_DATA_HOST}{query}",
                rows=rows,
                returned_row_count=len(rows),
                completeness=Completeness.COMPLETE,
                schema_sha256=_schema_hash(path, vintage, header, labels),
            )
            return Lookup(
                result=result,
                row_grain=(
                    f"One row is one geographic unit at the {level} level that the Census Data "
                    "API returned for the request, with each requested variable as text exactly "
                    "as the API gives it: an estimate, a margin of error, or an annotation code. "
                    "The last columns are the geography codes."
                ),
                warnings=warnings,
                rules=([ANNOTATION_RULE] if coded else []) + [CENSUS_RULE],
                escalations=["ANNOTATION_CODES_PRESENT"] if coded else [],
            )

        return PreparedLookup(
            host=CENSUS_DATA_HOST,
            arguments={
                "year": vintage,
                "dataset": path,
                "variables": names,
                "geography_for": for_text,
                "geography_in": in_text,
            },
            run=run,
        )

    async def close(self) -> None:
        await self._http.close()
