from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from opencivicquery import __version__
from opencivicquery.logging_redaction import scrub
from opencivicquery.models import (
    CatalogEntry,
    Completeness,
    DatasetDescription,
    DatasetField,
    QueryRequest,
    SourceQueryResult,
    normalize_domain,
)


class SocrataError(RuntimeError):
    """The Socrata source returned an invalid or unsuccessful response."""


class SocrataQueryError(SocrataError):
    """An expected failure with a safe diagnostic for the MCP caller."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "QUERY_REJECTED",
        argument: str | None = None,
        http_status: int | None = None,
        public_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.argument = argument
        self.http_status = http_status
        self.public_message = public_message if public_message is not None else message

    def tool_error(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.public_message,
            "argument": self.argument,
            "http_status": self.http_status,
        }


_DATASET_ID = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")
# SoQL accepts both quote styles for string literals, so both must be stripped before
# keyword checks, or a double-quoted string can hide a second, chained query.
_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"")
_DANGEROUS_TOKENS = re.compile(r";|--|/\*|\*/|\|>")
_STRUCTURAL_KEYWORDS = re.compile(
    r"\b(select|where|group\s+by|having|order\s+by|limit|offset|union)\b",
    re.IGNORECASE,
)


_EXCLUDED_DOMAINS = frozenset({"opendata.socrata.com", "evergreen.data.socrata.com"})
_EXCLUDED_SUFFIXES = (".demo.socrata.com",)
"""Socrata's community, sample, and demonstration portals hold uploads and sample data,
not government records, so they must never be cited as an agency's own data."""


def _is_excluded(domain: str) -> bool:
    return domain in _EXCLUDED_DOMAINS or domain.endswith(_EXCLUDED_SUFFIXES)


def _excluded_error(domain: str) -> SocrataQueryError:
    return SocrataQueryError(
        f"{domain} is a Socrata community or sample portal, not a government portal.",
        code="UNKNOWN_PORTAL",
        argument="domain",
    )


def _validate_domain_argument(domain: str) -> str:
    try:
        normalized = normalize_domain(domain)
    except ValueError as exc:
        raise SocrataQueryError(
            str(exc), code="INVALID_QUERY_ARGUMENT", argument="domain"
        ) from exc
    if _is_excluded(normalized):
        raise _excluded_error(normalized)
    return normalized


def _validate_dataset_id(dataset_id: str) -> str:
    if not _DATASET_ID.fullmatch(dataset_id):
        raise SocrataQueryError(
            "Invalid Socrata dataset ID",
            code="INVALID_QUERY_ARGUMENT",
            argument="dataset_id",
        )
    return dataset_id


def _validate_clause(name: str, value: str, *, allow_structural: bool = False) -> str:
    normalized = value.strip()

    def reject(message: str) -> None:
        raise SocrataQueryError(message, code="INVALID_QUERY_ARGUMENT", argument=name)

    if not normalized:
        reject(f"{name} must not be empty")
    if len(normalized) > 8_000:
        reject(f"{name} exceeds the 8,000-character limit")
    without_strings = _STRING_LITERAL.sub("''", normalized)
    entity = re.search(r"&(?:lt|gt|amp);", without_strings, re.IGNORECASE)
    if entity:
        reject(f"{name} contains an HTML-escaped operator: {entity.group(0)}")
    forbidden = _DANGEROUS_TOKENS.search(normalized)
    if forbidden:
        reject(f"{name} contains a forbidden token: {forbidden.group(0)!r}")
    if not allow_structural and _STRUCTURAL_KEYWORDS.search(without_strings):
        reject(f"{name} contains a structural query keyword")
    return normalized


def build_soql(request: QueryRequest) -> str:
    _validate_dataset_id(request.dataset_id)
    select = _validate_clause("select", request.select)
    parts = [f"SELECT {select}"]
    if request.where:
        parts.append(f"WHERE {_validate_clause('where', request.where)}")
    if request.group_by:
        parts.append(f"GROUP BY {_validate_clause('group_by', request.group_by)}")
    if request.order_by:
        parts.append(f"ORDER BY {_validate_clause('order_by', request.order_by)}")
    parts.append(f"LIMIT {request.max_rows + 1}")
    return " ".join(parts)


def _columns(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    columns = metadata.get("columns")
    if not isinstance(columns, list):
        return []
    return [column for column in columns if isinstance(column, dict)]


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise SocrataError("Socrata returned a response that is not JSON") from exc


def _schema_hash(metadata: dict[str, Any]) -> str:
    schema = {
        "id": metadata.get("id"),
        "name": metadata.get("name"),
        "columns": [
            {
                "name": column.get("name"),
                "fieldName": column.get("fieldName"),
                "dataTypeName": column.get("dataTypeName"),
                "description": column.get("description"),
            }
            for column in _columns(metadata)
        ],
    }
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _epoch_to_iso(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _http_date_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    with suppress(TypeError, ValueError):
        return parsedate_to_datetime(value).astimezone(UTC).isoformat()
    return None


class SocrataNetwork:
    """Every portal on the Socrata platform behind one connector.

    A portal is reachable only after the shared Socrata catalog, which Socrata itself
    hosts, confirms the domain. The server therefore never sends a request to a host the
    caller invents. Aliases resolve to the catalog's canonical domain.
    """

    catalog_url = "https://api.us.socrata.com/api/catalog/v1"
    _max_cached_portals = 4096
    _unknown_portal_ttl = 3600.0

    def __init__(
        self,
        *,
        app_token: str | None,
        portals: list[str] | None,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = 4,
        max_concurrency: int = 4,
        max_response_bytes: int = 4 * 1024 * 1024,
        clock: Any = time.monotonic,
    ) -> None:
        self._token = (app_token or "").strip() or None
        self._portals = set(portals) if portals is not None else None
        self._request_headers = {
            "Accept": "application/json",
            "User-Agent": f"OpenCivicQuery/{__version__}",
        }
        if self._token:
            self._request_headers["X-App-Token"] = self._token
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._max_attempts = max_attempts
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_response_bytes = max_response_bytes
        self._canonical: OrderedDict[str, str] = OrderedDict()
        self._unknown: OrderedDict[str, float] = OrderedDict()
        self._clock = clock

    @staticmethod
    def _dataset_url(domain: str, dataset_id: str) -> str:
        return f"https://{domain}/d/{dataset_id}"

    def _scrub(self, message: str) -> str:
        if self._token:
            message = message.replace(self._token, "[REDACTED]")
        return scrub(message)

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        idempotent_retries: bool = True,
    ) -> httpx.Response:
        """GET with bounded retries.

        Metadata and catalog reads retry on any transient failure. A data query passes
        idempotent_retries=False: it retries only when Socrata explicitly asks for a retry
        (202, 429, 503) or the connection never opened, so one expensive query that times
        out or fails is not replayed four times. The concurrency slot is released while
        waiting between attempts.
        """
        retryable_statuses = {202, 429, 500, 502, 503, 504} if idempotent_retries else {
            202,
            429,
            503,
        }
        last_response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            delay: float | None = None
            async with self._semaphore:
                try:
                    async with self._client.stream(
                        "GET", url, params=params, headers=self._request_headers
                    ) as streamed_response:
                        retryable = streamed_response.status_code in retryable_statuses
                        if retryable and attempt + 1 < self._max_attempts:
                            retry_after = streamed_response.headers.get("Retry-After")
                            delay = (
                                float(retry_after)
                                if retry_after and retry_after.isdigit()
                                else 0.25 * (2**attempt)
                            )
                        else:
                            content = bytearray()
                            async for chunk in streamed_response.aiter_bytes():
                                content.extend(chunk)
                                if len(content) > self._max_response_bytes:
                                    raise SocrataQueryError(
                                        "Socrata response exceeded the safe size limit.",
                                        code="RESULT_TOO_LARGE",
                                        public_message=(
                                            "The result is too large to return. Narrow the "
                                            "query, aggregate it, or lower max_rows."
                                        ),
                                    )
                            decoded_headers = {
                                key: value
                                for key, value in streamed_response.headers.items()
                                if key.lower() not in {"content-encoding", "content-length"}
                            }
                            last_response = httpx.Response(
                                status_code=streamed_response.status_code,
                                headers=decoded_headers,
                                content=bytes(content),
                                request=streamed_response.request,
                            )
                except httpx.RequestError as exc:
                    never_sent = isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
                    if attempt + 1 == self._max_attempts or not (
                        idempotent_retries or never_sent
                    ):
                        raise SocrataError("Socrata request failed") from exc
                    delay = 0.25 * (2**attempt)
            if last_response is not None:
                break
            await asyncio.sleep(min(delay or 0.0, 5.0))

        assert last_response is not None
        try:
            last_response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = "Socrata request failed"
            try:
                body = last_response.json()
                if isinstance(body, dict):
                    for key in ("message", "error"):
                        if isinstance(body.get(key), str):
                            message = body[key]
                            break
            except ValueError:
                pass
            # Source diagnostics can echo credentials. Remove our token and known OAuth
            # credential forms before either audit logging or a client response.
            message = self._scrub(message)
            status = last_response.status_code
            code = "SOURCE_UNAVAILABLE"
            public_message = f"Socrata could not complete the request (HTTP {status})."
            if status == 400:
                code = "QUERY_REJECTED"
                public_message = (
                    " ".join(message.split()) or "Socrata rejected the request (HTTP 400)."
                )[:500]
                column = re.search(r"No such column: ([a-zA-Z_][a-zA-Z0-9_]*)", message)
                if column:
                    code = "UNKNOWN_COLUMN"
                    public_message = f"No such column: {column.group(1)}."
            elif status == 404:
                code = "DATASET_NOT_FOUND"
            elif status in {401, 403}:
                code = "SOURCE_ACCESS_DENIED"
            raise SocrataQueryError(
                f"{message} (HTTP {status})",
                code=code,
                http_status=status,
                public_message=public_message,
            ) from exc
        return last_response

    def _check_allowed(self, domain: str) -> None:
        if self._portals is not None and domain not in self._portals:
            raise SocrataQueryError(
                f"{domain} is not one of the portals this server is configured to reach.",
                code="PORTAL_NOT_ALLOWED",
                argument="domain",
            )

    @staticmethod
    def _unknown_error(domain: str) -> SocrataQueryError:
        return SocrataQueryError(
            f"{domain} is not a portal in the Socrata catalog.",
            code="UNKNOWN_PORTAL",
            argument="domain",
        )

    def _remember_unknown(self, domain: str) -> None:
        self._unknown[domain] = self._clock() + self._unknown_portal_ttl
        self._unknown.move_to_end(domain)
        while len(self._unknown) > self._max_cached_portals:
            self._unknown.popitem(last=False)

    def cached_portal(self, domain: str) -> str | None:
        """The canonical domain if this portal was already confirmed, without any request."""
        try:
            return self._canonical.get(normalize_domain(domain))
        except ValueError:
            return None

    def validate_query(self, request: QueryRequest) -> None:
        """Reject an unsafe query before it costs any request or usage allowance."""
        build_soql(request)

    async def resolve_portal(self, domain: str) -> str:
        """Return the canonical domain of a Socrata portal, or refuse an unknown host."""
        domain = _validate_domain_argument(domain)
        cached = self._canonical.get(domain)
        if cached is not None:
            self._canonical.move_to_end(domain)
            self._check_allowed(cached)
            return cached
        expiry = self._unknown.get(domain)
        if expiry is not None and expiry > self._clock():
            raise self._unknown_error(domain)
        # The lookup goes only to Socrata's own catalog host, so resolving a domain that
        # turns out to be disallowed sends nothing to that domain.
        try:
            response = await self._get(
                self.catalog_url,
                params={"domains": domain, "search_context": domain, "limit": 1},
            )
        except SocrataQueryError as exc:
            if exc.http_status in {400, 404}:
                self._remember_unknown(domain)
                raise self._unknown_error(domain) from exc
            raise
        payload = _json(response)
        results = payload.get("results") if isinstance(payload, dict) else None
        if response.status_code != 200 or not isinstance(results, list):
            # A pending 202 or an odd body says nothing about the portal, so it is not
            # remembered as unknown. Only an explicit empty answer or a 400/404 is.
            raise SocrataError("Socrata catalog returned an unusable portal lookup")
        if not results:
            self._remember_unknown(domain)
            raise self._unknown_error(domain)
        metadata = results[0].get("metadata") if isinstance(results[0], dict) else None
        reported = metadata.get("domain") if isinstance(metadata, dict) else None
        try:
            canonical = normalize_domain(str(reported))
        except ValueError as exc:
            raise SocrataError("Socrata catalog returned an invalid portal domain") from exc
        if _is_excluded(canonical):
            raise _excluded_error(canonical)
        self._canonical[domain] = canonical
        self._canonical[canonical] = canonical
        while len(self._canonical) > self._max_cached_portals:
            self._canonical.popitem(last=False)
        self._check_allowed(canonical)
        return canonical

    async def search(
        self,
        search_term: str,
        limit: int = 10,
        *,
        domain: str | None = None,
    ) -> list[CatalogEntry]:
        term = search_term.strip()
        if not term:
            raise SocrataQueryError("search_term must not be empty", argument="search_term")
        if not 1 <= limit <= 50:
            raise SocrataQueryError("limit must be between 1 and 50", argument="limit")
        params: dict[str, Any] = {"q": term, "only": "dataset", "limit": limit}
        if domain is not None:
            domain = await self.resolve_portal(domain)
            params.update({"domains": domain, "search_context": domain})
        elif self._portals is not None:
            params["domains"] = ",".join(sorted(self._portals))
        response = await self._get(self.catalog_url, params=params)
        payload = _json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SocrataError("Socrata catalog returned an invalid response")

        entries: list[CatalogEntry] = []
        for item in payload["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("resource"), dict):
                continue
            resource = item["resource"]
            dataset_id = resource.get("id")
            if not isinstance(dataset_id, str) or not _DATASET_ID.fullmatch(dataset_id):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            try:
                item_domain = normalize_domain(str(metadata.get("domain") or ""))
            except ValueError:
                continue
            if _is_excluded(item_domain) or (domain is not None and item_domain != domain):
                continue
            if self._portals is not None and item_domain not in self._portals:
                continue
            classification = (
                item.get("classification") if isinstance(item.get("classification"), dict) else {}
            )
            entries.append(
                CatalogEntry(
                    dataset_id=dataset_id,
                    name=str(resource.get("name") or dataset_id),
                    description=(
                        str(resource["description"])[:1000] if resource.get("description") else None
                    ),
                    category=(
                        str(classification["domain_category"])
                        if classification.get("domain_category")
                        else None
                    ),
                    domain=item_domain,
                    source_url=self._dataset_url(item_domain, dataset_id),
                )
            )
        return entries

    async def _metadata(self, domain: str, dataset_id: str) -> dict[str, Any]:
        dataset_id = _validate_dataset_id(dataset_id)
        response = await self._get(f"https://{domain}/api/views/{dataset_id}")
        metadata = _json(response)
        if not isinstance(metadata, dict) or metadata.get("id") != dataset_id:
            raise SocrataError("Socrata metadata returned an invalid response")
        if metadata.get("domainCName") not in {None, domain}:
            raise SocrataError("Socrata metadata resolved to a different portal")
        return metadata

    async def describe(self, domain: str, dataset_id: str) -> DatasetDescription:
        domain = await self.resolve_portal(domain)
        metadata = await self._metadata(domain, dataset_id)
        fields = [
            DatasetField(
                name=str(column.get("name") or column.get("fieldName") or "unknown"),
                field_name=str(column.get("fieldName") or ""),
                data_type=str(column.get("dataTypeName") or "unknown"),
                description=(str(column["description"]) if column.get("description") else None),
            )
            for column in _columns(metadata)
            if column.get("fieldName")
        ]
        return DatasetDescription(
            dataset_id=dataset_id,
            name=str(metadata.get("name") or dataset_id),
            description=(str(metadata["description"]) if metadata.get("description") else None),
            domain=domain,
            source_url=self._dataset_url(domain, dataset_id),
            fields=fields,
            source_updated_at=_epoch_to_iso(metadata.get("rowsUpdatedAt")),
            schema_sha256=_schema_hash(metadata),
        )

    async def query(self, request: QueryRequest) -> SourceQueryResult:
        soql = build_soql(request)
        domain = await self.resolve_portal(request.domain)
        metadata = await self._metadata(domain, request.dataset_id)
        response = await self._get(
            f"https://{domain}/resource/{request.dataset_id}.json",
            params={"$query": soql},
            idempotent_retries=False,
        )
        payload = _json(response)
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise SocrataError("Socrata query returned an invalid response")

        was_truncated = len(payload) > request.max_rows
        rows = payload[: request.max_rows]
        last_modified = _http_date_to_iso(response.headers.get("Last-Modified"))

        return SourceQueryResult(
            dataset_id=request.dataset_id,
            dataset_name=str(metadata.get("name") or request.dataset_id),
            domain=domain,
            source_url=self._dataset_url(domain, request.dataset_id),
            query=soql,
            query_url=str(response.request.url),
            rows=rows,
            returned_row_count=len(rows),
            completeness=(Completeness.TRUNCATED if was_truncated else Completeness.COMPLETE),
            source_updated_at=last_modified or _epoch_to_iso(metadata.get("rowsUpdatedAt")),
            source_request_id=response.headers.get("X-Socrata-RequestId"),
            source_etag=response.headers.get("ETag"),
            schema_sha256=_schema_hash(metadata),
        )

    async def count_rows(
        self,
        request: QueryRequest,
        *,
        reference: SourceQueryResult,
    ) -> SourceQueryResult:
        """Count matching source rows without fetching unchanged dataset metadata again."""
        if request.dataset_id != reference.dataset_id:
            raise SocrataQueryError("Count reference does not match the requested dataset")
        domain = reference.domain
        count_request = QueryRequest(
            domain=domain,
            dataset_id=request.dataset_id,
            select="count(*) AS opencivicquery_total",
            where=request.where,
            max_rows=1,
        )
        soql = build_soql(count_request)
        response = await self._get(
            f"https://{domain}/resource/{request.dataset_id}.json",
            params={"$query": soql},
            idempotent_retries=False,
        )
        payload = _json(response)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise SocrataError("Socrata count query returned an invalid response")
        last_modified = _http_date_to_iso(response.headers.get("Last-Modified"))
        return SourceQueryResult(
            dataset_id=request.dataset_id,
            dataset_name=reference.dataset_name,
            domain=domain,
            source_url=reference.source_url,
            query=soql,
            query_url=str(response.request.url),
            rows=payload,
            returned_row_count=1,
            completeness=Completeness.COMPLETE,
            source_updated_at=last_modified or reference.source_updated_at,
            source_request_id=response.headers.get("X-Socrata-RequestId"),
            source_etag=response.headers.get("ETag"),
            schema_sha256=reference.schema_sha256,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
