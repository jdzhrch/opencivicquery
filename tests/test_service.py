from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from opencivicquery import service as service_module
from opencivicquery.audit import AuditLogger
from opencivicquery.limits import UsageLimiter, UsageLimitExceeded
from opencivicquery.models import (
    CatalogEntry,
    Completeness,
    DatasetDescription,
    DatasetField,
    DeploymentBundle,
    QueryRequest,
    QuerySnapshot,
    SourceQueryResult,
    UsageLimits,
    normalize_domain,
)
from opencivicquery.packs import PackRegistry
from opencivicquery.service import (
    BASE_PRESENTATION_RULES,
    OPEN_ACTOR,
    SNAPSHOT_KEY,
    AuthorizationError,
    CallContext,
    OpenCivicQueryService,
    _bounded,
    build_presentation_rules,
    error_detail,
    inflated_count_warning,
)
from opencivicquery.snapshots import FileSnapshotStore
from opencivicquery.sources.socrata import SocrataQueryError, build_soql

ALIASES = {"data.sfgov.org": "data.sf.gov"}


class FakeSource:
    """A source that answers from memory and records every call that would go upstream."""

    def __init__(
        self,
        completeness: Completeness = Completeness.COMPLETE,
        count_error: Exception | None = None,
        count_updated_at: str = "2026-08-05T11:00:00+00:00",
        known_portals: set[str] | None = None,
    ):
        self.completeness = completeness
        self.count_error = count_error
        self.count_updated_at = count_updated_at
        self.known_portals = known_portals
        self.count_selects: list[str] = []
        self.calls: list[tuple[str, Any]] = []
        self.confirmed: dict[str, str] = {}

    def _confirm(self, domain: str) -> str:
        domain = normalize_domain(domain)
        canonical = ALIASES.get(domain, domain)
        if self.known_portals is not None and canonical not in self.known_portals:
            raise SocrataQueryError("unknown", code="UNKNOWN_PORTAL", argument="domain")
        self.confirmed[domain] = canonical
        self.confirmed[canonical] = canonical
        return canonical

    def cached_portal(self, domain: str) -> str | None:
        return self.confirmed.get(domain)

    def validate_query(self, request: QueryRequest) -> None:
        build_soql(request)

    async def resolve_portal(self, domain: str) -> str:
        self.calls.append(("resolve_portal", domain))
        return self._confirm(domain)

    async def search(
        self, search_term: str, limit: int = 10, *, domain: str | None = None
    ) -> list[CatalogEntry]:
        self.calls.append(("search", domain))
        if domain is not None:
            self._confirm(domain)
        entries = [
            CatalogEntry(
                dataset_id="vw6y-z8j6",
                name=f"311 {search_term}",
                domain="data.sf.gov",
                source_url="https://data.sf.gov/d/vw6y-z8j6",
            ),
            CatalogEntry(
                dataset_id="zzzz-9999",
                name=f"Unprofiled {search_term}",
                domain="data.sf.gov",
                source_url="https://data.sf.gov/d/zzzz-9999",
            ),
            CatalogEntry(
                dataset_id="erm2-nwe9",
                name=f"NYC 311 {search_term}",
                domain="data.cityofnewyork.us",
                source_url="https://data.cityofnewyork.us/d/erm2-nwe9",
            ),
        ]
        return entries[:limit]

    async def describe(self, domain: str, dataset_id: str) -> DatasetDescription:
        self.calls.append(("describe", domain))
        canonical = self._confirm(domain)
        return DatasetDescription(
            dataset_id=dataset_id,
            name="311 Cases",
            domain=canonical,
            source_url=f"https://{canonical}/d/{dataset_id}",
            fields=[DatasetField(name="ID", field_name="service_request_id", data_type="text")],
            source_updated_at="2026-08-05T11:00:00+00:00",
            schema_sha256="b" * 64,
        )

    async def query(self, request: QueryRequest) -> SourceQueryResult:
        self.calls.append(("query", request.domain))
        return SourceQueryResult(
            dataset_id=request.dataset_id,
            dataset_name="311 Cases",
            domain=request.domain,
            source_url=f"https://{request.domain}/d/{request.dataset_id}",
            query="SELECT * LIMIT 2",
            query_url=(
                f"https://{request.domain}/resource/{request.dataset_id}.json?"
                "%24query=SELECT+%2A+LIMIT+2"
            ),
            rows=[{"service_request_id": "1"}],
            returned_row_count=1,
            completeness=self.completeness,
            source_updated_at=self.count_updated_at,
            source_request_id="request-1",
            source_etag='"abc"',
            schema_sha256="b" * 64,
        )

    async def count_rows(
        self,
        request: QueryRequest,
        *,
        reference: SourceQueryResult,
    ) -> SourceQueryResult:
        self.calls.append(("count_rows", request.domain))
        if self.count_error is not None:
            raise self.count_error
        select = "count(*) AS opencivicquery_total"
        self.count_selects.append(select)
        return SourceQueryResult(
            dataset_id=request.dataset_id,
            dataset_name=reference.dataset_name,
            domain=reference.domain,
            source_url=reference.source_url,
            query="SELECT count(*) AS opencivicquery_total LIMIT 2",
            query_url=(
                "https://data.sf.gov/resource/vw6y-z8j6.json?"
                "%24query=SELECT+count%28%2A%29+AS+opencivicquery_total+LIMIT+2"
            ),
            rows=[{"opencivicquery_total": "4210"}],
            returned_row_count=1,
            completeness=Completeness.COMPLETE,
            source_updated_at="2026-08-05T11:00:00+00:00",
            source_request_id="count-request-1",
            source_etag='"count-abc"',
            schema_sha256=reference.schema_sha256,
        )

    def upstream_calls(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def close(self) -> None:
        return None


class AuditCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.events.append(json.loads(record.getMessage()))


def capturing_audit() -> tuple[AuditLogger, AuditCapture]:
    logger = logging.getLogger(f"test-audit-{uuid4().hex}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    capture = AuditCapture()
    logger.handlers = [capture]
    return AuditLogger(logger), capture


def build_service(
    bundle: DeploymentBundle,
    tmp_path: Path,
    *,
    source: FakeSource | None = None,
    auth_mode: str = "open",
    limits: UsageLimits | None = None,
) -> tuple[OpenCivicQueryService, FakeSource, AuditCapture]:
    source = source or FakeSource()
    audit, capture = capturing_audit()
    service = OpenCivicQueryService(
        bundle=bundle,
        source=source,
        snapshot_store=FileSnapshotStore(tmp_path, bundle.config.deployment_id),
        audit_logger=audit,
        limiter=UsageLimiter(limits or bundle.config.limits),
        auth_mode=auth_mode,  # type: ignore[arg-type]
    )
    return service, source, capture


@pytest.fixture
def open_service(local_bundle: DeploymentBundle, tmp_path: Path) -> OpenCivicQueryService:
    return build_service(local_bundle, tmp_path)[0]


@pytest.fixture
def oauth_service(oauth_bundle: DeploymentBundle, tmp_path: Path) -> OpenCivicQueryService:
    return build_service(oauth_bundle, tmp_path, auth_mode="oauth")[0]


def frozen_snapshot(uri: str) -> dict[str, Any]:
    return json.loads(Path(uri.removeprefix("file://")).read_text())


# Authorization ---------------------------------------------------------------------------


def test_open_mode_admits_only_the_anonymous_actor(open_service: OpenCivicQueryService) -> None:
    assert OPEN_ACTOR == "anonymous"
    assert open_service.authorize("anonymous") == "anonymous"
    assert open_service.authorize("  Anonymous ") == "anonymous"
    for actor in ("someone@example.com", "", "organization-anonymous"):
        with pytest.raises(AuthorizationError):
            open_service.authorize(actor)


def test_open_mode_never_consults_an_access_list(
    oauth_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, _, _ = build_service(oauth_bundle, tmp_path, auth_mode="open")

    # Even a listed address is refused: without login, nobody may claim an identity.
    with pytest.raises(AuthorizationError):
        service.authorize("collaborator@example.com")
    assert service.authorize(OPEN_ACTOR) == OPEN_ACTOR


def test_oauth_mode_without_an_access_list_refuses_everyone(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, _, _ = build_service(local_bundle, tmp_path, auth_mode="oauth")

    for actor in (OPEN_ACTOR, "reporter@example.org"):
        with pytest.raises(AuthorizationError):
            service.authorize(actor)


def test_service_rejects_unlisted_reporter(oauth_service: OpenCivicQueryService) -> None:
    with pytest.raises(AuthorizationError):
        oauth_service.authorize("someone@example.com")
    with pytest.raises(AuthorizationError):
        oauth_service.authorize(OPEN_ACTOR)


def test_service_admits_any_address_in_an_allowed_domain(
    oauth_service: OpenCivicQueryService,
) -> None:
    # A reporter hired today, never named in any configuration file.
    assert oauth_service.authorize("Newcomer@Example.org") == "newcomer@example.org"
    with pytest.raises(AuthorizationError):
        oauth_service.authorize("outsider@example.net")
    with pytest.raises(AuthorizationError):
        oauth_service.authorize("outsider@sub.example.org")
    with pytest.raises(AuthorizationError):
        oauth_service.authorize("outsider@example.org.attacker.example")


def test_service_admits_only_the_named_external_user(
    oauth_service: OpenCivicQueryService,
) -> None:
    assert oauth_service.authorize("Collaborator@Example.com") == "collaborator@example.com"
    with pytest.raises(AuthorizationError):
        oauth_service.authorize("someone-else@example.com")


def test_service_rejects_an_identity_with_no_domain_separator(
    oauth_service: OpenCivicQueryService,
) -> None:
    with pytest.raises(AuthorizationError):
        oauth_service.authorize("example.org")


def test_list_packs_returns_every_pack_to_an_authorized_caller(
    open_service: OpenCivicQueryService,
) -> None:
    packs = open_service.list_packs(actor_id=OPEN_ACTOR)

    assert [(pack.pack_id, pack.domain) for pack in packs] == [
        ("us-ca-san-francisco", "data.sf.gov"),
        ("us-ny-new-york-city", "data.cityofnewyork.us"),
    ]
    assert {
        "dataset_id": "wg3w-h783",
        "name": "Police Department Incident Reports, 2018 to Present",
    } in packs[0].datasets
    with pytest.raises(AuthorizationError):
        open_service.list_packs(actor_id="someone@example.com")


# Search and describe ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_marks_which_results_a_pack_documents(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, source, capture = build_service(local_bundle, tmp_path)

    entries = await service.search_datasets(
        actor_id=OPEN_ACTOR,
        context=CallContext(reporter_question="How many 311 cases?"),
        search_term="311",
    )

    assert [(entry.dataset_id, entry.has_profile) for entry in entries] == [
        ("vw6y-z8j6", True),
        ("zzzz-9999", False),
        ("erm2-nwe9", True),
    ]
    assert source.calls == [("search", None)]
    [event] = capture.events
    assert event["action"] == "search_datasets"
    assert event["status"] == "success"
    assert event["deployment_id"] == "local"
    assert event["actor_id"] == OPEN_ACTOR
    assert event["reporter_question"] == "How many 311 cases?"
    assert event["result_count"] == 3


@pytest.mark.asyncio
async def test_search_in_one_domain_passes_the_domain_through(
    open_service: OpenCivicQueryService,
) -> None:
    await open_service.search_datasets(actor_id=OPEN_ACTOR, search_term="311", domain="data.sf.gov")

    assert open_service.source.calls == [("search", "data.sf.gov")]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_search_with_an_invalid_domain_never_reaches_the_source(
    open_service: OpenCivicQueryService,
) -> None:
    with pytest.raises(SocrataQueryError) as caught:
        await open_service.search_datasets(
            actor_id=OPEN_ACTOR, search_term="311", domain="127.0.0.1"
        )

    assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert open_service.source.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_describe_attaches_the_profile_through_an_alias(
    open_service: OpenCivicQueryService,
) -> None:
    description = await open_service.describe_dataset(
        actor_id=OPEN_ACTOR, domain="data.sfgov.org", dataset_id="wg3w-h783"
    )

    assert description.domain == "data.sf.gov"
    assert description.row_grain == "One combination of an incident report and an incident code"
    assert any("one or more incident codes" in caveat for caveat in description.caveats)


@pytest.mark.asyncio
async def test_business_profile_returns_claim_specific_stop_signs_without_a_status(
    open_service: OpenCivicQueryService,
) -> None:
    description = await open_service.describe_dataset(
        actor_id=OPEN_ACTOR,
        domain="data.sf.gov",
        dataset_id="g8m3-pdis",
    )

    assert "review_status" not in description.model_dump(mode="json")
    assert description.row_grain == (
        "One business location registered with the Treasurer and Tax Collector"
    )
    assert description.default_time_field is None
    assert any("another City and County department" in caveat for caveat in description.caveats)

    response = await open_service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(
            domain="data.sf.gov",
            dataset_id="g8m3-pdis",
            select="count(*) AS location_rows",
        ),
    )

    assert "review_status" not in response.model_dump(mode="json")
    assert response.methodology["count_distinct_field"] == "certificate_number"
    assert response.methodology["count_distinct_unit"] == "distinct registered business accounts"
    assert any("count(distinct certificate_number)" in warning for warning in response.warnings)
    assert any("Administratively Closed" in warning for warning in response.warnings)
    assert response.presentation_rules[0].startswith("INFLATED COUNT")


# Query -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profiled_query_returns_methodology_and_frozen_snapshot(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, _, capture = build_service(local_bundle, tmp_path)

    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        context=CallContext(reporter_question="How many 311 requests?"),
        request=QueryRequest(
            domain="data.sf.gov",
            dataset_id="vw6y-z8j6",
            select="count(*) AS total",
        ),
    )

    assert "review_status" not in type(response).model_fields
    assert response.methodology["row_grain"] == "One SF311 service request"
    assert response.domain == "data.sf.gov"
    assert response.source_url == "https://data.sf.gov/d/vw6y-z8j6"
    assert response.query_url is not None
    assert response.query_url.startswith("https://data.sf.gov/resource/vw6y-z8j6.json?")
    assert response.snapshot.retention == "temporary"
    frozen = frozen_snapshot(response.snapshot.uri)
    assert frozen["query"] == response.query
    assert frozen["query_url"] == response.query_url
    assert frozen["source_updated_at"] == response.source_updated_at
    assert frozen["actor_id"] == OPEN_ACTOR
    assert frozen["deployment_id"] == "local"
    assert frozen["config_sha256"] == local_bundle.config_sha256
    assert frozen["snapshot_version"] == 1
    assert "/temporary/local/" in response.snapshot.uri
    [event] = capture.events
    assert event["status"] == "success"
    assert event["deployment_id"] == "local"
    assert event["domain"] == "data.sf.gov"
    assert event["snapshot_id"] == response.snapshot.snapshot_id
    assert event["reporter_question"] == "How many 311 requests?"


@pytest.mark.asyncio
async def test_query_resolves_the_canonical_domain_before_the_profile_lookup(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, source, _ = build_service(local_bundle, tmp_path)

    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sfgov.org", dataset_id="wg3w-h783", select="count(*)"),
    )

    # The alias found the San Francisco profile, so the inflated-count guard fired.
    assert source.calls[:2] == [("resolve_portal", "data.sfgov.org"), ("query", "data.sf.gov")]
    assert response.domain == "data.sf.gov"
    assert response.methodology["row_grain"] is not None
    assert response.presentation_rules[0].startswith("INFLATED COUNT")
    assert frozen_snapshot(response.snapshot.uri)["domain"] == "data.sf.gov"


@pytest.mark.asyncio
async def test_truncated_result_has_explicit_warning(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, _, _ = build_service(local_bundle, tmp_path, source=FakeSource(Completeness.TRUNCATED))

    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(
            domain="data.sf.gov",
            dataset_id="vw6y-z8j6",
            select="*",
            max_rows=1,
        ),
    )

    assert response.completeness == Completeness.TRUNCATED
    assert any("row limit" in warning for warning in response.warnings)
    assert response.presentation_rules[0].startswith("PARTIAL RESULT")


@pytest.mark.parametrize(
    "select",
    [
        "count(*) AS n",
        "COUNT( * ) AS n",
        "incident_category, count(*) AS n",
    ],
)
def test_inflated_count_warning_fires_on_bare_count_star(
    local_bundle: DeploymentBundle, select: str
) -> None:
    profile = PackRegistry(local_bundle).profile_for("data.sf.gov", "wg3w-h783")
    warning = inflated_count_warning(select, profile)

    assert warning is not None
    assert "count(distinct incident_id)" in warning


def test_inflated_count_warning_silent_when_distinct_key_also_counted(
    local_bundle: DeploymentBundle,
) -> None:
    profile = PackRegistry(local_bundle).profile_for("data.sf.gov", "wg3w-h783")

    select = "count(*) AS occurrences, count(distinct incident_id) AS reports"
    assert inflated_count_warning(select, profile) is None


def test_inflated_count_warning_ignores_count_text_inside_string_literals(
    local_bundle: DeploymentBundle,
) -> None:
    profile = PackRegistry(local_bundle).profile_for("data.sf.gov", "wg3w-h783")

    assert inflated_count_warning("'count(*)' AS label", profile) is None
    warning = inflated_count_warning(
        "count(*) AS n, 'count(distinct incident_id)' AS label",
        profile,
    )
    assert warning is not None


def test_inflated_count_warning_silent_when_one_row_is_one_entity(
    local_bundle: DeploymentBundle,
) -> None:
    registry = PackRegistry(local_bundle)

    assert (
        inflated_count_warning("count(*) AS n", registry.profile_for("data.sf.gov", "vw6y-z8j6"))
        is None
    )
    assert inflated_count_warning("count(*) AS n", None) is None


def test_inflated_count_warning_covers_the_new_york_pack(local_bundle: DeploymentBundle) -> None:
    profile = PackRegistry(local_bundle).profile_for("data.cityofnewyork.us", "43nn-pn8j")

    warning = inflated_count_warning("count(*) AS n", profile)

    assert warning is not None
    assert "count(distinct camis)" in warning


def test_profiles_are_keyed_by_domain(local_bundle: DeploymentBundle) -> None:
    registry = PackRegistry(local_bundle)

    assert registry.profile_for("data.sf.gov", "wg3w-h783") is not None
    # The same dataset ID on another portal is a different dataset.
    assert registry.profile_for("data.cityofnewyork.us", "wg3w-h783") is None
    assert registry.deployment_id == "local"
    assert registry.config_sha256 == local_bundle.config_sha256


def test_dataset_profiles_carry_safeguards_without_quality_status(
    local_bundle: DeploymentBundle,
) -> None:
    business_profile = PackRegistry(local_bundle).profile_for("data.sf.gov", "g8m3-pdis")

    assert business_profile is not None
    assert "review_status" not in type(business_profile).model_fields
    assert "review_status" not in CatalogEntry.model_fields
    assert "review_status" not in DatasetDescription.model_fields
    assert "review_status" not in QuerySnapshot.model_fields


def test_presentation_rules_put_every_escalation_before_the_base_rules() -> None:
    rules = build_presentation_rules(
        completeness=Completeness.TRUNCATED,
        inflated_count="anything",
        row_grain="One source row",
        source_updated_at="2026-08-05T11:00:00+00:00",
    ).rules

    assert [rule.split(".")[0] for rule in rules[:2]] == [
        "INFLATED COUNT",
        "PARTIAL RESULT",
    ]
    assert rules[2] == BASE_PRESENTATION_RULES[0]
    assert "2026-08-05T11:00:00+00:00" in rules[3]
    assert "completeness exactly as 'truncated'" in rules[4]
    assert rules[5:] == list(BASE_PRESENTATION_RULES[1:])


def test_presentation_rules_flag_completeness_that_is_neither_complete_nor_truncated() -> None:
    rules = build_presentation_rules(
        completeness=Completeness.UNKNOWN,
        inflated_count=None,
        row_grain="One source row",
        source_updated_at="2026-08-05T11:00:00+00:00",
    ).rules

    assert rules[0].startswith("COMPLETENESS IS UNKNOWN")


def test_clean_query_carries_only_the_base_rules() -> None:
    rules = build_presentation_rules(
        completeness=Completeness.COMPLETE,
        inflated_count=None,
        row_grain="One source row",
        source_updated_at="2026-08-05T11:00:00+00:00",
    ).rules

    assert rules == [
        BASE_PRESENTATION_RULES[0],
        (
            "Say that the source reports an update time of 2026-08-05T11:00:00+00:00 "
            "for every figure you state. Do not call the data current or complete through "
            "that time unless the source separately establishes that coverage."
        ),
        (
            "State completeness exactly as 'complete' for this query response. The value "
            "describes whether OpenCivicQuery returned the full query result under its row limit. "
            "It does not establish that the source contains every underlying event."
        ),
        *BASE_PRESENTATION_RULES[1:],
    ]


def test_presentation_rules_do_not_invent_a_missing_source_update_time() -> None:
    rules = build_presentation_rules(
        completeness=Completeness.COMPLETE,
        inflated_count=None,
        row_grain="One source row",
        source_updated_at=None,
    ).rules

    assert "did not provide an update time" in rules[1]
    assert "retrieval time" in rules[1]
    assert "completeness exactly as 'complete'" in rules[2]


@pytest.mark.asyncio
async def test_count_star_on_multi_row_grain_warns_and_escalates(
    open_service: OpenCivicQueryService,
) -> None:
    response = await open_service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(
            domain="data.sf.gov",
            dataset_id="wg3w-h783",
            select="count(*) AS n",
        ),
    )

    assert any("count(distinct incident_id)" in warning for warning in response.warnings)
    assert response.presentation_rules[0].startswith("INFLATED COUNT")

    frozen = frozen_snapshot(response.snapshot.uri)
    assert any("count(distinct incident_id)" in warning for warning in frozen["warnings"])


@pytest.mark.asyncio
async def test_dataset_without_profile_carries_no_quality_or_publication_label(
    open_service: OpenCivicQueryService,
) -> None:
    response = await open_service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(
            domain="data.sf.gov",
            dataset_id="zzzz-9999",
            select="count(*) AS n",
        ),
    )

    payload = response.model_dump(mode="json")
    rendered = json.dumps(payload).lower()
    assert "review_status" not in payload
    assert "unreviewed" not in rendered
    assert "publication" not in rendered
    assert response.methodology["row_grain"] is None
    assert response.presentation_rules[0].startswith("ROW GRAIN UNKNOWN")


@pytest.mark.asyncio
async def test_portal_outside_every_pack_carries_row_grain_unknown(
    open_service: OpenCivicQueryService,
) -> None:
    response = await open_service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.example.gov", dataset_id="vw6y-z8j6", select="*"),
    )

    assert response.methodology["row_grain"] is None
    assert response.presentation_rules[0].startswith("ROW GRAIN UNKNOWN")


@pytest.mark.asyncio
async def test_truncated_ungrouped_result_backfills_the_real_total(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    source = FakeSource(Completeness.TRUNCATED)
    service, _, _ = build_service(local_bundle, tmp_path, source=source)

    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="*", max_rows=1),
    )

    assert response.truncation_total == 4210
    assert response.truncation_total_unit == "rows"
    assert any("4210 matching source rows" in warning for warning in response.warnings)
    assert "part of 4210 matching source rows" in response.presentation_rules[0]
    assert source.count_selects == ["count(*) AS opencivicquery_total"]
    assert response.truncation_evidence is not None
    assert response.truncation_evidence.query == "SELECT count(*) AS opencivicquery_total LIMIT 2"
    assert response.truncation_evidence.query_url is not None
    assert "opencivicquery_total" in response.truncation_evidence.query_url
    assert response.truncation_evidence.source_request_id == "count-request-1"

    frozen = frozen_snapshot(response.snapshot.uri)
    assert frozen["snapshot_version"] == 1
    assert frozen["truncation_evidence"] == response.truncation_evidence.model_dump(mode="json")


@pytest.mark.asyncio
async def test_truncation_backfill_preserves_rows_on_a_multi_row_grain(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    source = FakeSource(Completeness.TRUNCATED)
    service, _, _ = build_service(local_bundle, tmp_path, source=source)

    await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="wg3w-h783", select="*", max_rows=1),
    )

    assert source.count_selects == ["count(*) AS opencivicquery_total"]


@pytest.mark.asyncio
async def test_truncated_distinct_projection_does_not_guess_a_total(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    source = FakeSource(Completeness.TRUNCATED)
    service, _, _ = build_service(local_bundle, tmp_path, source=source)

    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(
            domain="data.sf.gov",
            dataset_id="vw6y-z8j6",
            select="DISTINCT service_name",
            max_rows=1,
        ),
    )

    assert response.truncation_total is None
    assert response.truncation_total_unit is None
    assert response.truncation_evidence is None
    assert source.count_selects == []


@pytest.mark.asyncio
async def test_truncated_grouped_result_does_not_guess_a_total(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    source = FakeSource(Completeness.TRUNCATED)
    service, _, _ = build_service(local_bundle, tmp_path, source=source)

    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(
            domain="data.sf.gov",
            dataset_id="vw6y-z8j6",
            select="service_name, count(*) AS n",
            group_by="service_name",
            max_rows=1,
        ),
    )

    assert response.truncation_total is None
    assert source.count_selects == []
    assert response.presentation_rules[0].startswith("PARTIAL RESULT.")


@pytest.mark.asyncio
async def test_a_failed_backfill_never_costs_the_caller_its_result(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    # SocrataError subclasses RuntimeError, so a narrow except tuple would let it escape.
    source = FakeSource(Completeness.TRUNCATED, count_error=RuntimeError("source down"))
    service, _, _ = build_service(local_bundle, tmp_path, source=source)

    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="*", max_rows=1),
    )

    assert response.truncation_total is None
    assert response.rows == [{"service_request_id": "1"}]
    assert response.presentation_rules[0].startswith("PARTIAL RESULT.")


@pytest.mark.asyncio
async def test_backfill_from_a_different_source_version_is_not_reported(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    source = FakeSource(Completeness.TRUNCATED, count_updated_at="2026-08-05T12:00:00+00:00")
    service, _, _ = build_service(local_bundle, tmp_path, source=source)

    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="*", max_rows=1),
    )

    assert response.truncation_total is None
    assert response.truncation_evidence is None
    assert response.presentation_rules[0].startswith("PARTIAL RESULT.")


@pytest.mark.asyncio
async def test_local_snapshot_does_not_claim_an_unenforced_expiry(
    open_service: OpenCivicQueryService,
) -> None:
    response = await open_service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="count(*) AS n"),
    )

    assert response.snapshot.retention == "temporary"
    assert response.snapshot.expires_at is None

    pinned = await open_service.pin_snapshot(
        actor_id=OPEN_ACTOR,
        snapshot_uri=response.snapshot.uri,
    )
    assert pinned.retention == "pinned"
    assert pinned.expires_at is None
    restored = await open_service.get_snapshot(actor_id=OPEN_ACTOR, snapshot_uri=pinned.uri)
    assert restored.deployment_id == "local"


def test_presentation_contract_names_every_escalation_with_a_stable_code() -> None:
    contract = build_presentation_rules(
        completeness=Completeness.TRUNCATED,
        inflated_count="incident_id",
        row_grain="One incident-code row",
        source_updated_at=None,
        truncation_total=8230,
    )

    # Codes, not rule text: an audit record has to stay queryable when the wording of a
    # rule is edited.
    assert contract.escalations == [
        "INFLATED_COUNT",
        "PARTIAL_RESULT",
    ]
    assert len(contract.rules) > len(contract.escalations)


def test_presentation_contract_reports_no_escalation_on_a_clean_result() -> None:
    contract = build_presentation_rules(
        completeness=Completeness.COMPLETE,
        inflated_count=None,
        row_grain="One source row",
        source_updated_at="2026-08-05T11:00:00+00:00",
    )

    assert contract.escalations == []


def test_missing_row_grain_escalates_without_a_dataset_quality_label() -> None:
    contract = build_presentation_rules(
        completeness=Completeness.COMPLETE,
        inflated_count=None,
        row_grain=None,
        source_updated_at="2026-08-05T11:00:00+00:00",
    )

    assert contract.escalations == ["ROW_GRAIN_UNKNOWN"]
    assert contract.rules[0].startswith("ROW GRAIN UNKNOWN")
    assert "quality" not in contract.rules[0].lower()
    assert "review" not in contract.rules[0].lower()


def test_error_detail_collapses_and_caps_an_upstream_message() -> None:
    detail = error_detail(RuntimeError("upstream said\n   no   " + "x" * 400))

    assert detail.startswith("upstream said no ")
    assert len(detail) == 300


def test_error_detail_falls_back_to_the_exception_type() -> None:
    assert error_detail(RuntimeError()) == "RuntimeError"


def test_call_context_defaults_to_empty_audit_fields() -> None:
    assert CallContext().audit_fields() == {
        "reporter_question": None,
        "connection_id": None,
        "client_hash": None,
        "client_capped": None,
    }


def test_call_context_audits_a_client_hash_and_never_the_address() -> None:
    from opencivicquery.client import identify

    context = CallContext(client=identify("8.8.8.8"))
    fields = context.audit_fields()

    assert fields["client_hash"] is not None
    assert len(fields["client_hash"]) == 12
    assert fields["client_capped"] is True
    assert "8.8.8.8" not in json.dumps(fields)
    assert "8.8.8.8" not in repr(context)
    assert context.capped_client_key == "8.8.8.8"


def test_shared_clients_have_no_capped_key() -> None:
    from opencivicquery.client import identify

    assert CallContext(client=identify("0.0.0.0")).capped_client_key is None
    assert CallContext().capped_client_key is None


def test_snapshot_stays_evidence_only() -> None:
    # The snapshot is what a reporter cites. The question is a model's paraphrase of the
    # request, so it belongs in the audit log and not in the evidence record.
    assert "reporter_question" not in QuerySnapshot.model_fields
    assert "connection_id" not in QuerySnapshot.model_fields


# Usage limits ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_query_is_audited_and_never_reaches_the_source(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, source, capture = build_service(
        local_bundle,
        tmp_path,
        limits=UsageLimits(daily_requests=1, per_portal_per_minute=10),
    )
    request = QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="count(*)")

    await service.query_dataset(actor_id=OPEN_ACTOR, request=request)
    calls_before = list(source.calls)
    with pytest.raises(UsageLimitExceeded) as caught:
        await service.query_dataset(
            actor_id=OPEN_ACTOR,
            context=CallContext(reporter_question="Again?"),
            request=request,
        )

    assert caught.value.code == "DAILY_LIMIT_REACHED"
    assert source.calls == calls_before
    assert len(list(tmp_path.rglob("*.json"))) == 1
    refused = capture.events[-1]
    assert refused["action"] == "query_dataset"
    assert refused["status"] == "refused"
    assert refused["error_type"] == "DAILY_LIMIT_REACHED"
    assert refused["portal"] == "data.sf.gov"
    assert refused["deployment_id"] == "local"
    assert refused["actor_id"] == OPEN_ACTOR
    assert refused["reporter_question"] == "Again?"
    assert [event["status"] for event in capture.events] == ["success", "refused"]


@pytest.mark.asyncio
async def test_search_across_portals_counts_against_the_catalog_bucket(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, source, capture = build_service(
        local_bundle,
        tmp_path,
        limits=UsageLimits(daily_requests=100, per_portal_per_minute=1),
    )

    await service.search_datasets(actor_id=OPEN_ACTOR, search_term="311")
    with pytest.raises(UsageLimitExceeded) as caught:
        await service.search_datasets(actor_id=OPEN_ACTOR, search_term="311")
    # A portal not yet confirmed also costs a catalog lookup, so it draws on the same bucket.
    with pytest.raises(UsageLimitExceeded):
        await service.search_datasets(actor_id=OPEN_ACTOR, search_term="311", domain="data.sf.gov")

    assert caught.value.code == "PORTAL_RATE_LIMITED"
    assert source.upstream_calls() == ["search"]
    assert [(event["status"], event.get("portal")) for event in capture.events] == [
        ("success", None),
        ("refused", "catalog"),
        ("refused", "catalog"),
    ]


@pytest.mark.asyncio
async def test_a_refused_describe_is_audited_and_never_reaches_the_source(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, source, capture = build_service(
        local_bundle,
        tmp_path,
        limits=UsageLimits(daily_requests=100, per_portal_per_minute=1),
    )

    await service.describe_dataset(
        actor_id=OPEN_ACTOR, domain="DATA.SF.GOV", dataset_id="vw6y-z8j6"
    )
    await service.describe_dataset(
        actor_id=OPEN_ACTOR, domain="data.sf.gov", dataset_id="vw6y-z8j6"
    )
    with pytest.raises(UsageLimitExceeded):
        await service.describe_dataset(
            actor_id=OPEN_ACTOR, domain="data.sf.gov", dataset_id="vw6y-z8j6"
        )

    assert source.upstream_calls() == ["describe", "describe"]
    refused = capture.events[-1]
    assert refused["action"] == "describe_dataset"
    assert refused["status"] == "refused"
    assert refused["portal"] == "data.sf.gov"


@pytest.mark.asyncio
async def test_a_confirmed_portal_and_its_aliases_share_one_bucket(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, source, capture = build_service(
        local_bundle,
        tmp_path,
        limits=UsageLimits(daily_requests=100, per_portal_per_minute=1),
    )

    # The first call confirms the portal through the catalog bucket. Later calls to the
    # portal, or to any alias of it, share one bucket keyed on the canonical domain.
    await service.describe_dataset(
        actor_id=OPEN_ACTOR, domain="DATA.SFGOV.ORG", dataset_id="vw6y-z8j6"
    )
    await service.describe_dataset(
        actor_id=OPEN_ACTOR, domain="data.sf.gov", dataset_id="vw6y-z8j6"
    )
    with pytest.raises(UsageLimitExceeded) as caught:
        await service.query_dataset(
            actor_id=OPEN_ACTOR,
            request=QueryRequest(domain="data.sfgov.org", dataset_id="vw6y-z8j6", select="*"),
        )
    with pytest.raises(UsageLimitExceeded):
        await service.search_datasets(
            actor_id=OPEN_ACTOR, search_term="311", domain="data.sfgov.org"
        )

    assert caught.value.code == "PORTAL_RATE_LIMITED"
    assert source.upstream_calls() == ["describe", "describe"]
    refused = [event for event in capture.events if event["status"] == "refused"]
    assert [(event["action"], event["portal"]) for event in refused] == [
        ("query_dataset", "data.sf.gov"),
        ("search_datasets", "data.sf.gov"),
    ]


@pytest.mark.asyncio
async def test_invented_domains_cannot_open_fresh_buckets(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    source = FakeSource(known_portals={"data.sf.gov"})
    service, _, capture = build_service(
        local_bundle,
        tmp_path,
        source=source,
        limits=UsageLimits(daily_requests=100, per_portal_per_minute=2),
    )

    for domain in ("data.invented-a.gov", "data.invented-b.gov"):
        with pytest.raises(SocrataQueryError):
            await service.describe_dataset(actor_id=OPEN_ACTOR, domain=domain, dataset_id="x")
    with pytest.raises(UsageLimitExceeded) as caught:
        await service.describe_dataset(
            actor_id=OPEN_ACTOR, domain="data.invented-c.gov", dataset_id="x"
        )

    assert caught.value.code == "PORTAL_RATE_LIMITED"
    assert capture.events[-1]["portal"] == "catalog"
    assert source.cached_portal("data.invented-a.gov") is None


@pytest.mark.asyncio
async def test_invalid_soql_costs_no_allowance_and_no_upstream_call(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, source, _ = build_service(
        local_bundle,
        tmp_path,
        limits=UsageLimits(daily_requests=1, per_portal_per_minute=1),
    )

    for clause in ("value;", "value LIMIT 50000", 'boro = "a\'" OR camis IS NOT NULL LIMIT 3'):
        with pytest.raises(SocrataQueryError) as caught:
            await service.query_dataset(
                actor_id=OPEN_ACTOR,
                request=QueryRequest(
                    domain="data.sf.gov", dataset_id="vw6y-z8j6", select="*", where=clause
                ),
            )
        assert caught.value.code == "INVALID_QUERY_ARGUMENT"
    assert source.calls == []

    # The single daily call is still unspent.
    await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="count(*)"),
    )


@pytest.mark.asyncio
async def test_rejected_soql_is_still_audited(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, _, capture = build_service(local_bundle, tmp_path)

    with pytest.raises(SocrataQueryError):
        await service.query_dataset(
            actor_id=OPEN_ACTOR,
            request=QueryRequest(
                domain="data.sf.gov", dataset_id="vw6y-z8j6", select="*", group_by="value;"
            ),
        )

    assert [event["status"] for event in capture.events] == ["error"]


@pytest.mark.asyncio
async def test_unauthorized_calls_do_not_consume_the_limit(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, source, _ = build_service(
        local_bundle,
        tmp_path,
        limits=UsageLimits(daily_requests=1, per_portal_per_minute=1),
    )

    for _ in range(3):
        with pytest.raises(AuthorizationError):
            await service.search_datasets(actor_id="someone@example.com", search_term="311")

    await service.search_datasets(actor_id=OPEN_ACTOR, search_term="311")
    assert source.upstream_calls() == ["search"]


@pytest.mark.asyncio
async def test_snapshot_reads_and_pins_share_their_own_rate_limit(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, _, capture = build_service(
        local_bundle,
        tmp_path,
        limits=UsageLimits(daily_requests=100, per_portal_per_minute=2),
    )
    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="count(*)"),
    )
    uri = response.snapshot.uri

    await service.get_snapshot(actor_id=OPEN_ACTOR, snapshot_uri=uri)
    await service.pin_snapshot(actor_id=OPEN_ACTOR, snapshot_uri=uri)
    for call in (service.get_snapshot, service.pin_snapshot):
        with pytest.raises(UsageLimitExceeded) as caught:
            await call(actor_id=OPEN_ACTOR, snapshot_uri=uri)
        assert caught.value.code == "PORTAL_RATE_LIMITED"

    refused = [event for event in capture.events if event["status"] == "refused"]
    assert [(event["action"], event["portal"]) for event in refused] == [
        ("get_snapshot", SNAPSHOT_KEY),
        ("pin_snapshot", SNAPSHOT_KEY),
    ]
    # The snapshot bucket is separate: a query still has room.
    await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="count(*)"),
    )


@pytest.mark.asyncio
async def test_snapshot_calls_count_against_the_daily_cap(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, _, _ = build_service(
        local_bundle,
        tmp_path,
        limits=UsageLimits(daily_requests=2, per_portal_per_minute=100),
    )
    response = await service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="count(*)"),
    )

    await service.get_snapshot(actor_id=OPEN_ACTOR, snapshot_uri=response.snapshot.uri)
    with pytest.raises(UsageLimitExceeded) as caught:
        await service.get_snapshot(actor_id=OPEN_ACTOR, snapshot_uri=response.snapshot.uri)
    assert caught.value.code == "DAILY_LIMIT_REACHED"


@pytest.mark.asyncio
async def test_source_failures_are_audited_with_the_deployment(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    class FailingSource(FakeSource):
        async def query(self, request: QueryRequest) -> SourceQueryResult:
            raise SocrataQueryError("rejected", code="QUERY_REJECTED", http_status=400)

    service, _, capture = build_service(local_bundle, tmp_path, source=FailingSource())

    with pytest.raises(SocrataQueryError):
        await service.query_dataset(
            actor_id=OPEN_ACTOR,
            request=QueryRequest(domain="data.sfgov.org", dataset_id="vw6y-z8j6", select="*"),
        )

    [event] = capture.events
    assert event["status"] == "error"
    assert event["error_type"] == "SocrataQueryError"
    assert event["deployment_id"] == "local"
    assert event["domain"] == "data.sf.gov"
    assert not list(tmp_path.rglob("*.json"))


# Inflated counts beyond count(*) -----------------------------------------------------------


@pytest.mark.parametrize(
    "select",
    [
        "count(1) AS n",
        "count(row_id) AS n",
        "COUNT( :id ) AS n",
        "incident_category, count(incident_number) AS n",
        "count(distinctive_code) AS n",
    ],
)
def test_inflated_count_warning_flags_every_non_distinct_count(
    local_bundle: DeploymentBundle, select: str
) -> None:
    profile = PackRegistry(local_bundle).profile_for("data.sf.gov", "wg3w-h783")

    warning = inflated_count_warning(select, profile)

    assert warning is not None
    assert warning.startswith("This query counts rows.")
    assert "count(distinct incident_id)" in warning


@pytest.mark.parametrize(
    "select",
    [
        "count(distinct incident_id) AS reports",
        "count(distinct row_id) AS rows",
        "incident_category",
    ],
)
def test_inflated_count_warning_ignores_distinct_only_and_count_free_queries(
    local_bundle: DeploymentBundle, select: str
) -> None:
    profile = PackRegistry(local_bundle).profile_for("data.sf.gov", "wg3w-h783")

    assert inflated_count_warning(select, profile) is None


def test_inflated_count_warning_ignores_double_quoted_literals(
    local_bundle: DeploymentBundle,
) -> None:
    profile = PackRegistry(local_bundle).profile_for("data.sf.gov", "wg3w-h783")

    assert inflated_count_warning('"count(1)" AS label', profile) is None
    # A distinct count hidden in a string must not silence the warning.
    assert inflated_count_warning('count(1) AS n, "count(distinct incident_id)" AS x', profile)
    assert inflated_count_warning("count(1) AS n, 'it''s count(distinct incident_id)'", profile)


@pytest.mark.asyncio
async def test_count_one_on_a_multi_row_grain_escalates(
    open_service: OpenCivicQueryService,
) -> None:
    response = await open_service.query_dataset(
        actor_id=OPEN_ACTOR,
        request=QueryRequest(domain="data.sf.gov", dataset_id="wg3w-h783", select="count(1)"),
    )

    assert response.presentation_rules[0].startswith("INFLATED COUNT")
    assert any(warning.startswith("This query counts rows.") for warning in response.warnings)


def test_truncation_backfill_skip_sees_through_both_quote_styles() -> None:
    blanked = service_module._without_soql_string_literals("'a''b' x \"c\"\"d\" y 'say \"hi\"'")

    assert blanked.split() == ["x", "y"]
    assert len(blanked) == len("'a''b' x \"c\"\"d\" y 'say \"hi\"'")


# Upstream deadline ------------------------------------------------------------------------


class HangingSource(FakeSource):
    async def resolve_portal(self, domain: str) -> str:
        self.calls.append(("resolve_portal", domain))
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def search(self, search_term, limit=10, *, domain=None):  # type: ignore[override]
        self.calls.append(("search", domain))
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def describe(self, domain, dataset_id):  # type: ignore[override]
        self.calls.append(("describe", domain))
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


@pytest.fixture
def short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "UPSTREAM_DEADLINE_SECONDS", 0.05)


@pytest.mark.asyncio
async def test_a_hanging_query_becomes_a_source_timeout(
    local_bundle: DeploymentBundle, tmp_path: Path, short_deadline: None
) -> None:
    service, source, capture = build_service(local_bundle, tmp_path, source=HangingSource())

    with pytest.raises(SocrataQueryError) as caught:
        await asyncio.wait_for(
            service.query_dataset(
                actor_id=OPEN_ACTOR,
                request=QueryRequest(domain="data.sf.gov", dataset_id="vw6y-z8j6", select="*"),
            ),
            timeout=5,
        )

    assert caught.value.code == "SOURCE_TIMEOUT"
    assert "did not answer in time" in caught.value.tool_error()["message"]
    assert source.upstream_calls() == ["resolve_portal"]
    assert not list(tmp_path.rglob("*.json"))
    [event] = capture.events
    assert event["status"] == "error"
    assert event["error_type"] == "SOURCE_TIMEOUT"
    assert event["deployment_id"] == "local"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["search", "describe"])
async def test_a_hanging_search_or_describe_raises_a_timeout(
    local_bundle: DeploymentBundle, tmp_path: Path, short_deadline: None, action: str
) -> None:
    service, _, capture = build_service(local_bundle, tmp_path, source=HangingSource())

    with pytest.raises(TimeoutError):
        if action == "search":
            await asyncio.wait_for(
                service.search_datasets(actor_id=OPEN_ACTOR, search_term="311"), timeout=5
            )
        else:
            await asyncio.wait_for(
                service.describe_dataset(
                    actor_id=OPEN_ACTOR, domain="data.sf.gov", dataset_id="vw6y-z8j6"
                ),
                timeout=5,
            )

    [event] = capture.events
    assert event["status"] == "error"
    assert event["error_type"] == "TimeoutError"


def test_the_upstream_deadline_is_sixty_seconds() -> None:
    assert service_module.UPSTREAM_DEADLINE_SECONDS == 60


# Audit bounds -------------------------------------------------------------------------------


def test_bounded_truncates_only_long_strings() -> None:
    assert _bounded("x" * 2000) == "x" * 2000
    assert _bounded("x" * 2001) == "x" * 2000 + "...[truncated 1]"
    assert _bounded(12345) == 12345
    assert _bounded(None) is None
    assert _bounded(["y" * 5000]) == ["y" * 5000]


@pytest.mark.asyncio
async def test_audit_records_cap_long_strings(
    local_bundle: DeploymentBundle, tmp_path: Path
) -> None:
    service, _, capture = build_service(local_bundle, tmp_path)

    await service.search_datasets(
        actor_id=OPEN_ACTOR,
        context=CallContext(reporter_question="q" * 2500),
        search_term="s" * 7000,
    )

    [event] = capture.events
    assert event["search_term"] == "s" * 2000 + "...[truncated 5000]"
    assert event["reporter_question"] == "q" * 2000 + "...[truncated 500]"
    assert event["status"] == "success"
    assert event["deployment_id"] == "local"
