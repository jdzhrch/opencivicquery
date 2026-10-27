from __future__ import annotations

import pytest
from pydantic import ValidationError

from opencivicquery.models import (
    AccessConfig,
    CatalogEntry,
    DatasetProfile,
    QueryRequest,
    SocrataSettings,
    SourcePack,
    UsageLimits,
    normalize_domain,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("data.sf.gov", "data.sf.gov"),
        ("  Data.CityOfNewYork.US  ", "data.cityofnewyork.us"),
        ("data.sf.gov.", "data.sf.gov"),
        ("data-portal.example.gov", "data-portal.example.gov"),
        ("a.b.c.example.org", "a.b.c.example.org"),
    ],
)
def test_normalize_domain_accepts_public_hostnames(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "localhost",
        "portal.localhost",
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "[::1]",
        "::1",
        "https://data.sf.gov",
        "data.sf.gov/resource",
        "data.sf.gov:8443",
        "user:pass@data.sf.gov",
        "data sf.gov",
        "-data.sf.gov",
        "data-.sf.gov",
        "data..sf.gov",
        "data.sf.g0v",
        "data.sf.gov?x=1",
        "data.sf.gov#frag",
        "a" * 64 + ".example.org",
        ("a" * 60 + ".") * 5 + "org",
    ],
)
def test_normalize_domain_refuses_everything_but_a_bare_hostname(raw: str) -> None:
    with pytest.raises(ValueError, match="bare public hostname"):
        normalize_domain(raw)


def test_query_request_normalizes_its_domain() -> None:
    request = QueryRequest(domain=" DATA.SF.GOV ", dataset_id="abcd-1234", select="*")

    assert request.domain == "data.sf.gov"
    assert request.max_rows == 1000


@pytest.mark.parametrize("domain", ["127.0.0.1", "localhost", "data.sf.gov:443"])
def test_query_request_refuses_a_non_public_domain(domain: str) -> None:
    with pytest.raises(ValidationError) as caught:
        QueryRequest(domain=domain, dataset_id="abcd-1234", select="*")

    assert caught.value.errors()[0]["loc"] == ("domain",)


@pytest.mark.parametrize("max_rows", [0, 5001])
def test_query_request_keeps_a_row_ceiling(max_rows: int) -> None:
    with pytest.raises(ValidationError):
        QueryRequest(domain="data.sf.gov", dataset_id="abcd-1234", select="*", max_rows=max_rows)


def test_query_request_has_no_source_id() -> None:
    with pytest.raises(ValidationError):
        QueryRequest(source_id="datasf", dataset_id="abcd-1234", select="*")  # type: ignore[call-arg]


def test_socrata_settings_accept_any_or_a_normalized_list() -> None:
    assert SocrataSettings(portals="any").portals == "any"
    assert SocrataSettings(portals=["Data.SF.gov"]).portals == ["data.sf.gov"]


@pytest.mark.parametrize("portals", [[], ["data.sf.gov", "DATA.SF.GOV"], ["127.0.0.1"], "all"])
def test_socrata_settings_refuse_bad_portal_lists(portals: object) -> None:
    with pytest.raises(ValidationError):
        SocrataSettings(portals=portals)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "limits",
    [
        {"daily_requests": 0, "per_portal_per_minute": 1},
        {"daily_requests": 1, "per_portal_per_minute": 0},
    ],
)
def test_usage_limits_must_be_positive(limits: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        UsageLimits(**limits)


def pack_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "pack_id": "us-xx-example",
        "name": "Example City",
        "platform": "socrata",
        "domain": "Data.Example.gov",
        "checked_on": "2026-10-01",
        "datasets": [],
    }
    document.update(overrides)
    return document


def profile_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "name": "Example",
        "dataset_id": "abcd-1234",
        "row_grain": "One example row",
        "counting_guidance": "Count rows.",
    }
    document.update(overrides)
    return document


def test_source_pack_normalizes_its_domain() -> None:
    assert SourcePack.model_validate(pack_document()).domain == "data.example.gov"


@pytest.mark.parametrize(
    "overrides",
    [
        {"pack_id": "Not A Slug"},
        {"platform": "ckan"},
        {"domain": "10.0.0.1"},
        {"datasets": [profile_document(), profile_document()]},
        {"unexpected": True},
    ],
)
def test_source_pack_refuses_invalid_documents(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SourcePack.model_validate(pack_document(**overrides))


def test_dataset_profile_has_no_source_id() -> None:
    assert "source_id" not in DatasetProfile.model_fields
    with pytest.raises(ValidationError):
        DatasetProfile.model_validate(profile_document(source_id="datasf"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"count_distinct_field": "incident_id"},
        {"count_distinct_unit": "distinct incident reports"},
    ],
)
def test_dataset_profile_requires_both_distinct_count_fields(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="configured together"):
        DatasetProfile.model_validate(profile_document(**overrides))


def test_catalog_entry_defaults_to_no_profile() -> None:
    entry = CatalogEntry(
        dataset_id="abcd-1234",
        name="Example",
        domain="data.example.gov",
        source_url="https://data.example.gov/d/abcd-1234",
    )

    assert entry.has_profile is False
    assert "source_id" not in CatalogEntry.model_fields


def test_access_config_rejects_a_configuration_that_allows_nobody() -> None:
    with pytest.raises(ValueError, match="at least one email address"):
        AccessConfig(allowed_users=[], allowed_users_env=None, allowed_email_domains=[])


def test_access_config_rejects_a_domain_written_as_an_address() -> None:
    with pytest.raises(ValueError, match="bare domains"):
        AccessConfig(allowed_email_domains=["someone@example.org"])


def test_access_config_normalizes_addresses_and_domains() -> None:
    access = AccessConfig(
        allowed_users=[" Collaborator@Example.com "],
        allowed_email_domains=["@Example.org"],
    )

    assert access.allowed_users == ["collaborator@example.com"]
    assert access.allowed_email_domains == ["example.org"]


@pytest.mark.parametrize(
    "fields",
    [
        {"allowed_users": ["not-an-address"]},
        {"allowed_users": ["a@example.com", "A@example.com"]},
        {"allowed_email_domains": ["example.org", "Example.org"]},
        {"allowed_users_env": "lowercase_name"},
    ],
)
def test_access_config_refuses_malformed_entries(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AccessConfig(**fields)  # type: ignore[arg-type]
