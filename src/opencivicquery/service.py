from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from opencivicquery.audit import AuditLogger
from opencivicquery.limits import CATALOG_KEY, UsageLimiter, UsageLimitExceeded
from opencivicquery.models import (
    CatalogEntry,
    Completeness,
    DatasetDescription,
    DatasetProfile,
    DeploymentBundle,
    PackSummary,
    QueryRequest,
    QueryResponse,
    QuerySnapshot,
    SnapshotReference,
    SourceQueryResult,
    TruncationEvidence,
    normalize_domain,
)
from opencivicquery.packs import PackRegistry
from opencivicquery.snapshots import SnapshotStore
from opencivicquery.sources.base import StructuredSource
from opencivicquery.sources.socrata import SocrataQueryError

OPEN_ACTOR = "anonymous"
"""The identity every call carries when login is off. Questions are still recorded."""

SNAPSHOT_KEY = "snapshots"
"""Usage bucket for reading and pinning snapshots, which cost storage reads and egress."""

UPSTREAM_DEADLINE_SECONDS = 60.0
"""Ceiling on all source work for one tool call, so slow upstreams cannot hold an
instance indefinitely."""

_MAX_AUDIT_STRING = 2000


@dataclass(frozen=True)
class CallContext:
    """What the reporter actually wanted to know, recorded beside the machine query."""

    reporter_question: str | None = None
    connection_id: str | None = None

    def audit_fields(self) -> dict[str, str | None]:
        return {
            "reporter_question": self.reporter_question,
            "connection_id": self.connection_id,
        }


@dataclass(frozen=True)
class PresentationContract:
    """The rendering rules for one response, plus stable codes for the escalated ones."""

    rules: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)


def error_detail(exc: Exception) -> str:
    """One capped line describing a failure, for the audit record."""
    text = " ".join(str(exc).split())
    return text[:300] if text else type(exc).__name__


# Any non-distinct count counts rows: count(*), count(1), count(:id), count(some_field).
_ROW_COUNT = re.compile(r"\bcount\s*\(\s*(?!distinct\b)", re.IGNORECASE)

BASE_PRESENTATION_RULES: tuple[str, ...] = (
    "Name the counting unit supported by methodology.row_grain or the agency's official source "
    "documentation in the same sentence as any figure you state. If neither establishes the "
    "unit, state that limitation instead of a numeric entity claim. '170 eviction notices were "
    "filed' is correct. '170 evictions' misstates the record.",
    "Repeat each warning above that bears on a figure you quote. Do not drop a warning "
    "because the answer reads more cleanly without it.",
    "A count is not a share, rate, or percentage. Use those terms only after a query or "
    "calculation supplies a numerator and denominator, and state both inputs with the result.",
    "Include source_url for every figure you state and copy query verbatim from the response. "
    "When query_url is present, place it beside the query. When it is absent, say that the "
    "source did not provide a public live-result URL. Never rewrite, shorten, or reconstruct "
    "the query.",
    "Include the snapshot URI whenever the user may publish the figure, and offer "
    "pin_snapshot so the evidence outlives the temporary retention window.",
)


def _without_soql_string_literals(value: str) -> str:
    """Blank out SoQL string contents in either quote style, preserving positions."""
    cleaned: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is None:
            if character in {"'", '"'}:
                quote = character
                cleaned.append(" ")
            else:
                cleaned.append(character)
        elif character == quote:
            if index + 1 < len(value) and value[index + 1] == quote:
                cleaned.extend((" ", " "))
                index += 2
                continue
            quote = None
            cleaned.append(" ")
        else:
            cleaned.append(" ")
        index += 1
    return "".join(cleaned)


class AuthorizationError(PermissionError):
    """The current caller does not have access to this deployment."""


def inflated_count_warning(select: str, profile: DatasetProfile | None) -> str | None:
    """Flag a row count that overstates the entity count on a narrower-than-entity row grain.

    Returns None when the dataset counts one row per entity, when the query has no
    non-distinct count, or when the query also counts the distinct key. A query holding
    both forms is deliberately comparing occurrences against entities, so it needs no
    warning.
    """
    distinct_field = profile.count_distinct_field if profile else None
    expression = _without_soql_string_literals(select)
    if distinct_field is None or not _ROW_COUNT.search(expression):
        return None
    paired = re.compile(
        rf"count\s*\(\s*distinct\s+{re.escape(distinct_field)}\s*\)", re.IGNORECASE
    )
    if paired.search(expression):
        return None
    assert profile is not None
    return (
        f"This query counts rows. The row grain of this dataset is: "
        f"{profile.row_grain}. Use count(distinct {distinct_field}) to also count "
        f"{profile.count_distinct_unit}. Keep the two units separate and do not rename either "
        "one as a broader real-world entity that the records do not establish."
    )


def build_presentation_rules(
    *,
    completeness: Completeness,
    inflated_count: str | None,
    row_grain: str | None,
    source_updated_at: str | None,
    truncation_total: int | None = None,
) -> PresentationContract:
    """Return the rendering contract for one response, most urgent rule first."""
    escalations: list[str] = []
    codes: list[str] = []
    if inflated_count is not None:
        codes.append("INFLATED_COUNT")
        escalations.append(
            "INFLATED COUNT. This figure counts the row unit. Re-run the query with the "
            "distinct key and use the distinct unit named in methodology. Do not rename that "
            "unit as a broader real-world entity."
        )
    if row_grain is None:
        codes.append("ROW_GRAIN_UNKNOWN")
        escalations.append(
            "ROW GRAIN UNKNOWN. No source pack documents this dataset, so this response does "
            "not establish what one source row represents. Use the live schema and the "
            "agency's official dataset documentation to identify the record unit. If they do "
            "not establish it, state that limit and do not rename rows as the broader entity "
            "in the reporter's question."
        )
    if completeness == Completeness.TRUNCATED:
        codes.append("PARTIAL_RESULT")
        seen = (
            f"These rows cover part of {truncation_total} matching source rows. "
            if truncation_total is not None
            else ""
        )
        escalations.append(
            f"PARTIAL RESULT. {seen}Call any figure drawn from these rows partial in the "
            "same sentence where you state it. Never add these rows into a total."
        )
    elif completeness != Completeness.COMPLETE:
        codes.append(f"COMPLETENESS_{completeness.value.upper()}")
        escalations.append(
            f"COMPLETENESS IS {completeness.value.upper()}. Say so where you state any figure "
            "from this result."
        )
    freshness_rule = (
        f"Say that the source reports an update time of {source_updated_at} for every figure "
        "you state. Do not call the data current or complete through that time unless the "
        "source separately establishes that coverage."
        if source_updated_at is not None
        else "State that the source did not provide an update time. Treat the query time only "
        "as the retrieval time, not as the source as-of time."
    )
    completeness_rule = (
        f"State completeness exactly as '{completeness.value}' for this query response. "
        "The value describes whether OpenCivicQuery returned the full query result under its "
        "row limit. It does not establish that the source contains every underlying event."
    )
    base_rules = [
        BASE_PRESENTATION_RULES[0],
        freshness_rule,
        completeness_rule,
        *BASE_PRESENTATION_RULES[1:],
    ]
    return PresentationContract(rules=escalations + base_rules, escalations=codes)


def _normalized(domain: str) -> str:
    try:
        return normalize_domain(domain)
    except ValueError as exc:
        raise SocrataQueryError(str(exc), code="INVALID_QUERY_ARGUMENT", argument="domain") from exc


def _bounded(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_AUDIT_STRING:
        return value[:_MAX_AUDIT_STRING] + f"...[truncated {len(value) - _MAX_AUDIT_STRING}]"
    return value


class OpenCivicQueryService:
    def __init__(
        self,
        *,
        bundle: DeploymentBundle,
        source: StructuredSource,
        snapshot_store: SnapshotStore,
        audit_logger: AuditLogger,
        limiter: UsageLimiter,
        auth_mode: Literal["open", "oauth"],
    ) -> None:
        self.bundle = bundle
        self.registry = PackRegistry(bundle)
        self.source = source
        self.snapshot_store = snapshot_store
        self.audit = audit_logger
        self.limiter = limiter
        self.auth_mode = auth_mode

    def authorize(self, actor_id: str) -> str:
        actor = actor_id.strip().lower()
        if self.auth_mode == "open":
            if actor != OPEN_ACTOR:
                raise AuthorizationError("Calls without login must use the anonymous identity")
            return actor
        access = self.bundle.config.access
        if access is None:
            raise AuthorizationError("Login is on but no access list is configured")
        if actor in access.allowed_users:
            return actor
        # The address itself was already proved by the token verifier.
        _, separator, domain = actor.partition("@")
        if separator and domain in access.allowed_email_domains:
            return actor
        raise AuthorizationError("User is not allowed for this deployment")

    def _record(self, action: str, actor: str, ctx: dict[str, Any], **fields: Any) -> None:
        self.audit.record(
            action,
            deployment_id=self.registry.deployment_id,
            actor_id=actor,
            **{key: _bounded(value) for key, value in {**ctx, **fields}.items()},
        )

    def _portal_key(self, domain: str | None) -> str:
        """Key a per-portal bucket on the confirmed canonical domain.

        A domain not yet confirmed costs a lookup in the shared catalog, so it draws on
        the catalog bucket. Inventing many fake domains therefore cannot open many fresh
        buckets, and two aliases of one portal share a single bucket.
        """
        if domain is None:
            return CATALOG_KEY
        return self.source.cached_portal(_normalized(domain)) or CATALOG_KEY

    def _acquire(
        self, action: str, actor: str, ctx: dict[str, Any], portal_key: str, start: float
    ) -> None:
        try:
            self.limiter.acquire(portal_key)
        except UsageLimitExceeded as exc:
            self._record(
                action,
                actor,
                ctx,
                portal=portal_key,
                status="refused",
                error_type=exc.code,
                duration_ms=round((perf_counter() - start) * 1000),
            )
            raise

    async def _truncation_evidence(
        self,
        *,
        request: QueryRequest,
        reference: SourceQueryResult,
    ) -> TruncationEvidence | None:
        """Count source rows behind a truncated result, using one extra source request.

        Grouped and distinct projections are skipped because count(*) has a different unit
        from their returned rows. Any failure returns None because the caller already has a
        usable partial result.
        """
        select_expression = _without_soql_string_literals(request.select)
        if request.group_by is not None or re.match(
            r"^\s*distinct\b", select_expression, re.IGNORECASE
        ):
            return None
        try:
            result = await self.source.count_rows(request, reference=reference)
            if (
                reference.source_updated_at is not None
                and result.source_updated_at is not None
                and reference.source_updated_at != result.source_updated_at
            ):
                return None
            value = int(result.rows[0]["opencivicquery_total"])
            return TruncationEvidence(
                query=result.query,
                query_url=result.query_url,
                value=value,
                source_updated_at=result.source_updated_at,
                source_request_id=result.source_request_id,
                source_etag=result.source_etag,
                schema_sha256=result.schema_sha256,
            )
        except Exception:  # noqa: BLE001 - a nicety must never sink the caller's real result
            return None

    def list_packs(self, *, actor_id: str) -> list[PackSummary]:
        self.authorize(actor_id)
        return self.registry.summaries()

    async def search_datasets(
        self,
        *,
        actor_id: str,
        context: CallContext | None = None,
        search_term: str,
        limit: int = 10,
        domain: str | None = None,
    ) -> list[CatalogEntry]:
        actor = self.authorize(actor_id)
        ctx = (context or CallContext()).audit_fields()
        start = perf_counter()
        self._acquire("search_datasets", actor, ctx, self._portal_key(domain), start)
        try:
            async with asyncio.timeout(UPSTREAM_DEADLINE_SECONDS):
                entries = await self.source.search(search_term, limit, domain=domain)
            entries = [
                entry.model_copy(
                    update={
                        "has_profile": self.registry.profile_for(entry.domain, entry.dataset_id)
                        is not None
                    }
                )
                for entry in entries
            ]
        except Exception as exc:
            self._record(
                "search_datasets",
                actor,
                ctx,
                search_term=search_term,
                domain=domain,
                status="error",
                error_type=type(exc).__name__,
                error_message=error_detail(exc),
                duration_ms=round((perf_counter() - start) * 1000),
            )
            raise
        self._record(
            "search_datasets",
            actor,
            ctx,
            search_term=search_term,
            domain=domain,
            status="success",
            result_count=len(entries),
            duration_ms=round((perf_counter() - start) * 1000),
        )
        return entries

    async def describe_dataset(
        self,
        *,
        actor_id: str,
        context: CallContext | None = None,
        domain: str,
        dataset_id: str,
    ) -> DatasetDescription:
        actor = self.authorize(actor_id)
        ctx = (context or CallContext()).audit_fields()
        start = perf_counter()
        self._acquire("describe_dataset", actor, ctx, self._portal_key(domain), start)
        try:
            async with asyncio.timeout(UPSTREAM_DEADLINE_SECONDS):
                description = await self.source.describe(domain, dataset_id)
            profile = self.registry.profile_for(description.domain, dataset_id)
            if profile:
                description = description.model_copy(
                    update={
                        "row_grain": profile.row_grain,
                        "unique_key": profile.unique_key,
                        "default_time_field": profile.default_time_field,
                        "geography_fields": profile.geography_fields,
                        "counting_guidance": profile.counting_guidance,
                        "caveats": profile.caveats,
                    }
                )
        except Exception as exc:
            self._record(
                "describe_dataset",
                actor,
                ctx,
                domain=domain,
                dataset_id=dataset_id,
                status="error",
                error_type=type(exc).__name__,
                error_message=error_detail(exc),
                duration_ms=round((perf_counter() - start) * 1000),
            )
            raise
        self._record(
            "describe_dataset",
            actor,
            ctx,
            domain=description.domain,
            dataset_id=dataset_id,
            status="success",
            duration_ms=round((perf_counter() - start) * 1000),
        )
        return description

    async def query_dataset(
        self,
        *,
        actor_id: str,
        context: CallContext | None = None,
        request: QueryRequest,
    ) -> QueryResponse:
        actor = self.authorize(actor_id)
        ctx = (context or CallContext()).audit_fields()
        start = perf_counter()
        try:
            self.source.validate_query(request)
        except SocrataQueryError as exc:
            # Refused before any request or allowance, but still recorded: rejected
            # queries are how maintainers spot injection attempts and confusing tools.
            self._record(
                "query_dataset",
                actor,
                ctx,
                **self._request_fields(request),
                status="error",
                error_type=exc.code,
                error_message=error_detail(exc),
                duration_ms=round((perf_counter() - start) * 1000),
            )
            raise
        self._acquire("query_dataset", actor, ctx, self._portal_key(request.domain), start)
        warnings: list[str] = []
        try:
            async with asyncio.timeout(UPSTREAM_DEADLINE_SECONDS):
                return await self._run_query(actor, ctx, request, warnings, start)
        except TimeoutError as exc:
            self._record(
                "query_dataset",
                actor,
                ctx,
                **self._request_fields(request),
                status="error",
                error_type="SOURCE_TIMEOUT",
                duration_ms=round((perf_counter() - start) * 1000),
            )
            raise SocrataQueryError(
                "The data source did not answer in time.",
                code="SOURCE_TIMEOUT",
                public_message=(
                    "The data source did not answer in time. Narrow the query or try again."
                ),
            ) from exc

    @staticmethod
    def _request_fields(request: QueryRequest) -> dict[str, Any]:
        return {
            "domain": request.domain,
            "dataset_id": request.dataset_id,
            "requested_select": request.select,
            "requested_where": request.where,
            "requested_group_by": request.group_by,
            "requested_order_by": request.order_by,
            "requested_max_rows": request.max_rows,
        }

    async def _run_query(
        self,
        actor: str,
        ctx: dict[str, Any],
        request: QueryRequest,
        warnings: list[str],
        start: float,
    ) -> QueryResponse:
        try:
            canonical = await self.source.resolve_portal(request.domain)
            request = request.model_copy(update={"domain": canonical})
            profile = self.registry.profile_for(canonical, request.dataset_id)
            inflated_count = inflated_count_warning(request.select, profile)
            if inflated_count is not None:
                warnings.append(inflated_count)
            methodology = {
                "row_grain": profile.row_grain if profile else None,
                "count_distinct_field": profile.count_distinct_field if profile else None,
                "count_distinct_unit": profile.count_distinct_unit if profile else None,
                "default_time_field": profile.default_time_field if profile else None,
                "geography_fields": profile.geography_fields if profile else {},
                "counting_guidance": profile.counting_guidance if profile else None,
            }
            result = await self.source.query(request)
            truncation_evidence: TruncationEvidence | None = None
            truncation_total: int | None = None
            if result.completeness == Completeness.TRUNCATED:
                warnings.append(
                    "The result reached the row limit. Do not treat the returned rows as a "
                    "complete total."
                )
                truncation_evidence = await self._truncation_evidence(
                    request=request,
                    reference=result,
                )
                if truncation_evidence is not None:
                    truncation_total = truncation_evidence.value
                    warnings.append(
                        f"The full result covers {truncation_total} matching source rows."
                    )
            if profile:
                warnings.extend(profile.caveats)
            if result.source_updated_at is None:
                warnings.append(
                    "The source did not report its last update time. The query is frozen, but "
                    "the source version cannot be stated precisely."
                )
            created_at = datetime.now(UTC)
            snapshot = QuerySnapshot(
                snapshot_id=f"{created_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:12]}",
                created_at=created_at,
                deployment_id=self.registry.deployment_id,
                actor_id=actor,
                config_sha256=self.registry.config_sha256,
                dataset_id=result.dataset_id,
                dataset_name=result.dataset_name,
                domain=result.domain,
                source_url=result.source_url,
                query=result.query,
                query_url=result.query_url,
                rows=result.rows,
                returned_row_count=result.returned_row_count,
                completeness=result.completeness,
                source_updated_at=result.source_updated_at,
                source_request_id=result.source_request_id,
                source_etag=result.source_etag,
                schema_sha256=result.schema_sha256,
                warnings=warnings,
                methodology=methodology,
                truncation_evidence=truncation_evidence,
            )
            snapshot_reference = await self.snapshot_store.put(snapshot)
        except TimeoutError:
            # query_dataset owns timeouts and writes their single audit record.
            raise
        except Exception as exc:
            self._record(
                "query_dataset",
                actor,
                ctx,
                domain=request.domain,
                dataset_id=request.dataset_id,
                requested_select=request.select,
                requested_where=request.where,
                requested_group_by=request.group_by,
                requested_order_by=request.order_by,
                requested_max_rows=request.max_rows,
                status="error",
                error_type=type(exc).__name__,
                error_message=error_detail(exc),
                duration_ms=round((perf_counter() - start) * 1000),
            )
            raise

        contract = build_presentation_rules(
            completeness=result.completeness,
            inflated_count=inflated_count,
            row_grain=profile.row_grain if profile else None,
            source_updated_at=result.source_updated_at,
            truncation_total=truncation_total,
        )
        self._record(
            "query_dataset",
            actor,
            ctx,
            domain=result.domain,
            dataset_id=request.dataset_id,
            query=result.query,
            query_url=result.query_url,
            status="success",
            completeness=result.completeness,
            returned_row_count=result.returned_row_count,
            # Which safeguards fired, so a reviewer can ask whether the reply that
            # reached the reporter actually carried them.
            presentation_escalations=contract.escalations,
            warnings=warnings,
            truncation_total=truncation_total,
            truncation_evidence=(
                truncation_evidence.model_dump(mode="json")
                if truncation_evidence is not None
                else None
            ),
            snapshot_id=snapshot_reference.snapshot_id,
            duration_ms=round((perf_counter() - start) * 1000),
        )
        return QueryResponse(
            dataset_id=result.dataset_id,
            dataset_name=result.dataset_name,
            domain=result.domain,
            source_url=result.source_url,
            rows=result.rows,
            returned_row_count=result.returned_row_count,
            completeness=result.completeness,
            source_updated_at=result.source_updated_at,
            query=result.query,
            query_url=result.query_url,
            methodology=methodology,
            warnings=warnings,
            presentation_rules=contract.rules,
            truncation_total=truncation_total,
            truncation_total_unit=(
                truncation_evidence.unit if truncation_evidence is not None else None
            ),
            truncation_evidence=truncation_evidence,
            snapshot=snapshot_reference,
        )

    async def pin_snapshot(
        self,
        *,
        actor_id: str,
        context: CallContext | None = None,
        snapshot_uri: str,
    ) -> SnapshotReference:
        actor = self.authorize(actor_id)
        ctx = (context or CallContext()).audit_fields()
        start = perf_counter()
        self._acquire("pin_snapshot", actor, ctx, SNAPSHOT_KEY, start)
        try:
            reference = await self.snapshot_store.pin(snapshot_uri)
        except Exception as exc:
            self._record(
                "pin_snapshot",
                actor,
                ctx,
                snapshot_uri=snapshot_uri,
                status="error",
                error_type=type(exc).__name__,
                error_message=error_detail(exc),
                duration_ms=round((perf_counter() - start) * 1000),
            )
            raise
        self._record(
            "pin_snapshot",
            actor,
            ctx,
            snapshot_id=reference.snapshot_id,
            status="success",
            duration_ms=round((perf_counter() - start) * 1000),
        )
        return reference

    async def get_snapshot(
        self,
        *,
        actor_id: str,
        context: CallContext | None = None,
        snapshot_uri: str,
    ) -> QuerySnapshot:
        actor = self.authorize(actor_id)
        ctx = (context or CallContext()).audit_fields()
        start = perf_counter()
        self._acquire("get_snapshot", actor, ctx, SNAPSHOT_KEY, start)
        try:
            snapshot = await self.snapshot_store.get(snapshot_uri)
        except Exception as exc:
            self._record(
                "get_snapshot",
                actor,
                ctx,
                snapshot_uri=snapshot_uri,
                status="error",
                error_type=type(exc).__name__,
                error_message=error_detail(exc),
                duration_ms=round((perf_counter() - start) * 1000),
            )
            raise
        self._record(
            "get_snapshot",
            actor,
            ctx,
            snapshot_id=snapshot.snapshot_id,
            status="success",
            duration_ms=round((perf_counter() - start) * 1000),
        )
        return snapshot

    async def close(self) -> None:
        await self.source.close()
