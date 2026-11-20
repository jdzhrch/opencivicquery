"""The U.S. Census Bureau's Census Geocoder: one street address to census geographies.

It needs no key. The host is fixed, so no caller can steer a request elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any
from urllib.parse import urlencode

import httpx

from opencivicquery import __version__
from opencivicquery.models import Completeness, SourceQueryResult
from opencivicquery.sources.base import SourceError, SourceQueryError

GEOCODER_HOST = "geocoding.geo.census.gov"
GEOCODER_PAGE = f"https://{GEOCODER_HOST}/geocoder/"
_ENDPOINT = f"https://{GEOCODER_HOST}/geocoder/geographies/onelineaddress"
BENCHMARK = "Public_AR_Current"
VINTAGE = "Current_Current"

MAX_ADDRESS_LENGTH = 100
# checked 2026-11-20: an empty address returned HTTP 400 "Address cannot be empty and
# cannot exceed 100 characters".

ROW_GRAIN = (
    "One row is one street address range that the Census Geocoder matched to the input "
    "address, with the census geographies that contain it."
)
# official: Census Geocoder FAQ, "How does the Census Geocoder obtain geocodes?": the
# geocoder matches the address to an existing street and address range.

APPROXIMATE_WARNING = (
    "The coordinates are approximate. The Census Geocoder interpolates them along the "
    "matched street's address range, so they do not mark the building itself."
)
# official: Census Geocoder FAQ ("approximate coordinates") and User Guide, May 2026
# ("interpolated coordinates").

MATCHED_ADDRESS_WARNING = (
    "Compare each matchedAddress with the address asked about. The geocoder can match an "
    "address spelled differently from the input."
)
# checked 2026-11-20: "1 Dr Carlton B Goodlett Pl, San Francisco, CA 94102" matched
# "1 DR CARLTON P GOODLETT PL, SAN FRANCISCO, CA, 94102".

MULTIPLE_MATCH_WARNING = (
    "More than one address matched. Ask which one is meant before reporting any geography."
)
# checked 2026-11-20: "100 Main St, Springfield" matched seven addresses in six states.

NO_MATCH_WARNING = (
    "No match does not mean the address does not exist. The Census Bureau lists these "
    "causes: a non-residential or commercial address, recent construction, a changed "
    "address, a missing address range, or demolished housing. It also withholds a match "
    "that would identify a person or business under Title 13 of the U.S. Code."
)
# official: Census Geocoder FAQ, "My address(es) did not obtain a geocode".

VINTAGE_WARNING = (
    "These geographies are the ones in place for the Current_Current vintage. Data for an "
    "earlier survey or census can use different boundaries, so confirm that a code matches "
    "the vintage of any data it is joined to."
)
# official: Census Geocoder FAQ, "What does each option in Vintage do?".

DISTRICT_WARNING = (
    "Each geography layer names its own term or year, such as 120th Congressional Districts "
    "or 2026 State Legislative Districts. A district layer can describe districts for a term "
    "that has not begun, so name the layer beside any district you report."
)
# checked 2026-11-20: the Current_Current vintage returned the layers "120th Congressional
# Districts" and "2026 State Legislative Districts - Upper" and "- Lower".

FRESHNESS_WARNING = (
    "The Census Geocoder does not report an update time. Its benchmark, Public_AR_Current, "
    "names the snapshot of address range data it used; the Census Bureau creates these "
    "snapshots generally twice a year."
)
# official: Census Geocoder FAQ, "What does each option in Benchmark do?".


def _invisible(text: str) -> bool:
    """Control and format characters, such as NUL, ESC, zero-width space, or a bidi override."""
    return any(unicodedata.category(character) in {"Cc", "Cf"} for character in text)


def validate_address(address: str) -> str:
    normalized = " ".join(address.split())
    if not normalized or len(normalized) > MAX_ADDRESS_LENGTH or _invisible(normalized):
        raise SourceQueryError(
            "Invalid address",
            code="INVALID_QUERY_ARGUMENT",
            argument="address",
            public_message=(
                f"Give one street address of 1 to {MAX_ADDRESS_LENGTH} characters, for "
                "example 1 Dr Carlton B Goodlett Pl, San Francisco, CA 94102."
            ),
        )
    return normalized


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _match_row(match: Any) -> dict[str, Any]:
    """Keep the identifying fields of one match in the source's own field names."""
    if not isinstance(match, dict):
        raise SourceError("The Census Geocoder returned a malformed match")
    geographies: dict[str, list[dict[str, str | None]]] = {}
    raw_geographies = match.get("geographies")
    if isinstance(raw_geographies, dict):
        for layer, features in raw_geographies.items():
            if not isinstance(layer, str) or not isinstance(features, list):
                continue
            geographies[layer] = [
                {"GEOID": _text(feature.get("GEOID")), "NAME": _text(feature.get("NAME"))}
                for feature in features
                if isinstance(feature, dict)
            ]
    coordinates = match.get("coordinates")
    tiger_line = match.get("tigerLine")
    return {
        "matchedAddress": _text(match.get("matchedAddress")),
        "coordinates": (
            {"x": coordinates.get("x"), "y": coordinates.get("y")}
            if isinstance(coordinates, dict)
            else None
        ),
        "tigerLine": (
            {
                "tigerLineId": _text(tiger_line.get("tigerLineId")),
                "side": _text(tiger_line.get("side")),
            }
            if isinstance(tiger_line, dict)
            else None
        ),
        "geographies": geographies,
    }


class CensusGeocoder:
    """Look up one address with the Census Geocoder's one-line address service."""

    host = GEOCODER_HOST
    row_grain = ROW_GRAIN

    @staticmethod
    def validate(address: str) -> str:
        return validate_address(address)

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        max_response_bytes: int = 1024 * 1024,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), follow_redirects=False
        )
        self._owns_client = client is None
        self._max_response_bytes = max_response_bytes
        self._headers = {
            "Accept": "application/json",
            "User-Agent": f"OpenCivicQuery/{__version__}",
        }

    async def _fetch(self, params: dict[str, str]) -> tuple[int, bytes]:
        try:
            async with self._client.stream(
                "GET", _ENDPOINT, params=params, headers=self._headers
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self._max_response_bytes:
                        raise SourceError("The Census Geocoder response exceeded the size limit")
                return response.status_code, bytes(content)
        except httpx.TimeoutException as exc:
            raise SourceQueryError(
                "The Census Geocoder did not answer in time.",
                code="SOURCE_TIMEOUT",
                public_message="The geocoder did not answer in time. Try again.",
            ) from exc
        except httpx.RequestError as exc:
            raise SourceError("The Census Geocoder request failed") from exc

    async def geocode(self, address: str) -> SourceQueryResult:
        normalized = validate_address(address)
        params = {
            "address": normalized,
            "benchmark": BENCHMARK,
            "vintage": VINTAGE,
            "format": "json",
        }
        query = urlencode(params)
        status, content = await self._fetch(params)
        try:
            payload = json.loads(content)
        except ValueError:
            payload = None
        if status != 200:
            errors = payload.get("errors") if isinstance(payload, dict) else None
            detail = (
                " ".join(error for error in errors if isinstance(error, str))[:300]
                if isinstance(errors, list)
                else ""
            )
            raise SourceQueryError(
                f"Census Geocoder HTTP {status}: {detail}",
                code="QUERY_REJECTED" if status == 400 else "SOURCE_UNAVAILABLE",
                http_status=status,
                public_message=(
                    detail or f"The Census Geocoder could not complete the request (HTTP {status})."
                ),
            )
        result = payload.get("result") if isinstance(payload, dict) else None
        matches = result.get("addressMatches") if isinstance(result, dict) else None
        if not isinstance(result, dict) or not isinstance(matches, list):
            raise SourceError("The Census Geocoder returned no usable addressMatches list")
        rows = [_match_row(match) for match in matches]
        source_input = _mapping(result.get("input"))
        benchmark = _mapping(source_input.get("benchmark"))
        vintage = _mapping(source_input.get("vintage"))
        benchmark_name = _text(benchmark.get("benchmarkName"))
        vintage_name = _text(vintage.get("vintageName"))
        if (benchmark_name, vintage_name) != (BENCHMARK, VINTAGE):
            # The warnings name this benchmark and vintage, so an answer that does not
            # confirm them must never be frozen under them.
            raise SourceError("The Census Geocoder did not confirm the benchmark and vintage")
        layers = sorted({layer for row in rows for layer in row["geographies"]})
        schema = {"benchmark": benchmark_name, "vintage": vintage_name, "layers": layers}
        return SourceQueryResult(
            dataset_id=f"{benchmark_name}/{vintage_name}",
            dataset_name=(
                "Census Geocoder: "
                f"{_text(benchmark.get('benchmarkDescription')) or benchmark_name}; "
                f"{_text(vintage.get('vintageDescription')) or vintage_name}"
            ),
            domain=GEOCODER_HOST,
            source_url=GEOCODER_PAGE,
            query=query,
            query_url=f"{_ENDPOINT}?{query}",
            rows=rows,
            returned_row_count=len(rows),
            completeness=Completeness.COMPLETE,
            schema_sha256=hashlib.sha256(
                json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def warnings_for(self, result: SourceQueryResult) -> list[str]:
        warnings = [APPROXIMATE_WARNING, DISTRICT_WARNING, VINTAGE_WARNING, FRESHNESS_WARNING]
        if result.returned_row_count == 0:
            return [NO_MATCH_WARNING, *warnings]
        if result.returned_row_count > 1:
            return [MULTIPLE_MATCH_WARNING, MATCHED_ADDRESS_WARNING, *warnings]
        return [MATCHED_ADDRESS_WARNING, *warnings]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
