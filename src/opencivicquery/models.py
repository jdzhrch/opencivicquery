from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_SLUG = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


def normalize_domain(value: str) -> str:
    """Return a lowercase public hostname, or raise ValueError.

    The top-level label must be alphabetic, which excludes IP literals. Ports, paths,
    schemes, and credentials are rejected because only the bare hostname is ever used
    to build an HTTPS URL.
    """
    domain = value.strip().lower().rstrip(".")
    if not _HOSTNAME.fullmatch(domain) or domain.endswith(".localhost"):
        raise ValueError("domain must be a bare public hostname such as data.cityofnewyork.us")
    return domain


class Completeness(StrEnum):
    COMPLETE = "complete"
    LIMITED = "limited"
    TRUNCATED = "truncated"
    UNKNOWN = "unknown"


class DatasetProfile(BaseModel):
    """What one dataset's rows mean and how to count them, checked against live data."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    row_grain: str = Field(min_length=1)
    unique_key: str | None = Field(default=None, min_length=1)
    default_time_field: str | None = Field(default=None, min_length=1)
    geography_fields: dict[str, str] = Field(default_factory=dict)
    counting_guidance: str = Field(min_length=1)
    caveats: list[str] = Field(default_factory=list)
    count_distinct_field: str | None = Field(default=None, min_length=1)
    """Set when one row is narrower than a second useful counting unit."""
    count_distinct_unit: str | None = Field(default=None, min_length=1)
    """Plain-language unit produced by count_distinct_field."""

    @model_validator(mode="after")
    def validate_profile_contract(self) -> DatasetProfile:
        if (self.count_distinct_field is None) != (self.count_distinct_unit is None):
            raise ValueError(
                "count_distinct_field and count_distinct_unit must be configured together"
            )
        return self


class SourcePack(BaseModel):
    """Curated knowledge for one data portal. Anyone can contribute a pack for a new city."""

    model_config = ConfigDict(extra="forbid")

    pack_id: str = Field(pattern=_SLUG)
    name: str = Field(min_length=1)
    platform: Literal["socrata"]
    domain: str
    checked_on: date
    """When the profiles were last checked against the live schema and field values."""
    datasets: list[DatasetProfile] = Field(default_factory=list)

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return normalize_domain(value)

    @model_validator(mode="after")
    def validate_unique_datasets(self) -> SourcePack:
        ids = [profile.dataset_id for profile in self.datasets]
        if len(ids) != len(set(ids)):
            raise ValueError("a pack must not profile the same dataset twice")
        return self


class AccessConfig(BaseModel):
    """Who may call a deployment that runs with login turned on."""

    model_config = ConfigDict(extra="forbid")

    allowed_users: list[str] = Field(default_factory=list)
    """Individually named addresses, for people outside the newsroom's own domains."""
    allowed_users_env: str | None = Field(
        default=None,
        min_length=1,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    """Environment variable that supplies comma-separated addresses outside the repository."""
    allowed_email_domains: list[str] = Field(default_factory=list)
    """Whole domains. Everyone with a verified address here is allowed."""

    @field_validator("allowed_users")
    @classmethod
    def normalize_users(cls, value: list[str]) -> list[str]:
        users = [user.strip().lower() for user in value]
        if any("@" not in user for user in users):
            raise ValueError("allowed_users must contain email addresses")
        if len(users) != len(set(users)):
            raise ValueError("allowed_users must not contain duplicates")
        return users

    @field_validator("allowed_email_domains")
    @classmethod
    def normalize_domains(cls, value: list[str]) -> list[str]:
        domains = [domain.strip().lower().lstrip("@") for domain in value]
        if any(not domain or "@" in domain or "." not in domain for domain in domains):
            raise ValueError("allowed_email_domains must contain bare domains")
        if len(domains) != len(set(domains)):
            raise ValueError("allowed_email_domains must not contain duplicates")
        return domains

    @model_validator(mode="after")
    def require_an_allowed_identity(self) -> AccessConfig:
        if not self.allowed_users and not self.allowed_users_env and not self.allowed_email_domains:
            raise ValueError(
                "access must allow at least one email address, address environment "
                "variable, or email domain"
            )
        return self


class SocrataSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portals: Literal["any"] | list[str]
    """'any' admits every portal the Socrata catalog lists. A list restricts access."""
    app_token_env: str | None = Field(default=None, min_length=1)
    """Environment variable holding a Socrata app token. Without one, requests share
    Socrata's unauthenticated pool, which suits local use but not a public instance."""

    @field_validator("portals")
    @classmethod
    def normalize_portals(cls, value: Literal["any"] | list[str]) -> Literal["any"] | list[str]:
        if value == "any":
            return value
        portals = [normalize_domain(portal) for portal in value]
        if not portals:
            raise ValueError("portals must be 'any' or a non-empty list of domains")
        if len(portals) != len(set(portals)):
            raise ValueError("portals must not contain duplicates")
        return portals


class UsageLimits(BaseModel):
    """Caps that bound cost and abuse on a deployment without login.

    Counters live in each server process, so a deployment that runs N instances admits
    up to N times these numbers.
    """

    model_config = ConfigDict(extra="forbid")

    daily_requests: int = Field(ge=1)
    """Tool calls that reach a data source, per UTC day."""
    per_portal_per_minute: int = Field(ge=1)
    """Tool calls against one portal (or the shared catalog) in any rolling minute."""


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(pattern=_SLUG)
    display_name: str = Field(min_length=1)
    socrata: SocrataSettings
    limits: UsageLimits
    packs: list[str] = Field(default_factory=list)
    """Pack files, relative to the deployment configuration file."""
    access: AccessConfig | None = None
    """Required when the server runs with login turned on. Ignored otherwise."""


class DeploymentBundle(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: DeploymentConfig
    packs: list[SourcePack]
    socrata_app_token: SecretStr | None
    config_sha256: str
    """Hash over the deployment file and every pack it loads, recorded in each snapshot."""


class CatalogEntry(BaseModel):
    dataset_id: str
    name: str
    description: str | None = None
    category: str | None = None
    domain: str
    source_url: str
    has_profile: bool = False
    """True when a source pack documents this dataset's row grain and counting rules."""


class DatasetField(BaseModel):
    name: str
    field_name: str
    data_type: str
    description: str | None = None


class DatasetDescription(BaseModel):
    dataset_id: str
    name: str
    description: str | None = None
    domain: str
    source_url: str
    row_grain: str | None = None
    unique_key: str | None = None
    default_time_field: str | None = None
    geography_fields: dict[str, str] = Field(default_factory=dict)
    counting_guidance: str | None = None
    caveats: list[str] = Field(default_factory=list)
    fields: list[DatasetField]
    source_updated_at: str | None = None
    schema_sha256: str


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    dataset_id: str
    select: str = Field(min_length=1)
    where: str | None = None
    group_by: str | None = None
    order_by: str | None = None
    max_rows: int = Field(default=1000, ge=1, le=5000)

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return normalize_domain(value)


class SourceQueryResult(BaseModel):
    dataset_id: str
    dataset_name: str
    domain: str
    source_url: str
    query: str
    query_url: str | None = None
    rows: list[dict[str, Any]]
    returned_row_count: int
    completeness: Completeness
    source_updated_at: str | None = None
    source_request_id: str | None = None
    source_etag: str | None = None
    schema_sha256: str


class SnapshotReference(BaseModel):
    snapshot_id: str
    uri: str
    retention: Literal["temporary", "pinned"]
    expires_at: str | None = None
    """Earliest deletion time for lifecycle-managed storage. None when no expiry is enforced."""


class TruncationEvidence(BaseModel):
    """Provenance for a source-row total that accompanies a truncated result."""

    query: str
    query_url: str | None = None
    value: int = Field(ge=0)
    unit: Literal["rows"] = "rows"
    source_updated_at: str | None = None
    source_request_id: str | None = None
    source_etag: str | None = None
    schema_sha256: str


class QuerySnapshot(BaseModel):
    snapshot_version: Literal[1] = 1
    snapshot_id: str
    created_at: datetime
    deployment_id: str
    actor_id: str
    config_sha256: str
    dataset_id: str
    dataset_name: str
    domain: str
    source_url: str
    query: str
    query_url: str | None = None
    rows: list[dict[str, Any]]
    returned_row_count: int
    completeness: Completeness
    source_updated_at: str | None = None
    source_request_id: str | None = None
    source_etag: str | None = None
    schema_sha256: str
    warnings: list[str] = Field(default_factory=list)
    methodology: dict[str, Any] = Field(default_factory=dict)
    truncation_evidence: TruncationEvidence | None = None


class QueryResponse(BaseModel):
    dataset_id: str
    dataset_name: str
    domain: str
    source_url: str
    rows: list[dict[str, Any]]
    returned_row_count: int
    completeness: Completeness
    source_updated_at: str | None = None
    query: str
    query_url: str | None = None
    methodology: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    presentation_rules: list[str] = Field(default_factory=list)
    truncation_total: int | None = None
    """Matching source rows behind a truncated result. None when they cannot be counted."""
    truncation_total_unit: Literal["rows"] | None = None
    truncation_evidence: TruncationEvidence | None = None
    snapshot: SnapshotReference


class PackSummary(BaseModel):
    pack_id: str
    name: str
    domain: str
    checked_on: date
    datasets: list[dict[str, str]]
    """dataset_id and name for each profiled dataset."""
