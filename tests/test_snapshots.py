from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from opencivicquery.models import Completeness, QuerySnapshot
from opencivicquery.snapshots import FileSnapshotStore, GcsSnapshotStore, SnapshotError


class FakeBlob:
    def upload_from_string(self, *args, **kwargs) -> None:
        return None


class FakeBucket:
    def blob(self, name: str) -> FakeBlob:
        return FakeBlob()


class FakeStorageClient:
    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket()


def snapshot(
    snapshot_id: str = "20260805T120000Z-abc123",
    deployment_id: str = "newsroom-a",
) -> QuerySnapshot:
    return QuerySnapshot(
        snapshot_id=snapshot_id,
        created_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
        deployment_id=deployment_id,
        actor_id="reporter@example.org",
        config_sha256="a" * 64,
        dataset_id="abcd-1234",
        dataset_name="Test",
        domain="data.sf.gov",
        source_url="https://data.sf.gov/d/abcd-1234",
        query="SELECT count(*) AS total LIMIT 2",
        query_url=(
            "https://data.sf.gov/resource/abcd-1234.json?"
            "%24query=SELECT+count%28%2A%29+AS+total+LIMIT+2"
        ),
        rows=[{"total": "42"}],
        returned_row_count=1,
        completeness=Completeness.COMPLETE,
        source_updated_at="2026-08-05T11:00:00+00:00",
        schema_sha256="b" * 64,
    )


@pytest.mark.asyncio
async def test_snapshot_is_immutable_and_pin_preserves_content(tmp_path: Path) -> None:
    store = FileSnapshotStore(tmp_path, "newsroom-a")
    reference = await store.put(snapshot())
    assert reference.expires_at is None
    source_path = Path(reference.uri.removeprefix("file://"))
    assert source_path.is_relative_to(tmp_path.resolve() / "temporary" / "newsroom-a")
    source_bytes = source_path.read_bytes()

    with pytest.raises(SnapshotError, match="already exists"):
        await store.put(snapshot())

    pinned = await store.pin(reference.uri)
    pinned_path = Path(pinned.uri.removeprefix("file://"))
    assert pinned.retention == "pinned"
    assert pinned_path.read_bytes() == source_bytes
    assert json.loads(source_bytes)["source_updated_at"] == "2026-08-05T11:00:00+00:00"
    restored = await store.get(pinned.uri)
    assert restored.snapshot_id == "20260805T120000Z-abc123"
    assert restored.deployment_id == "newsroom-a"
    assert restored.rows == [{"total": "42"}]


@pytest.mark.asyncio
async def test_file_store_works_under_a_root_with_spaces_and_non_ascii(tmp_path: Path) -> None:
    root = tmp_path / "Mobile Documents 中文"
    store = FileSnapshotStore(root, "newsroom-a")

    reference = await store.put(snapshot())
    assert "%20" in reference.uri
    assert "%E4%B8%AD" in reference.uri

    restored = await store.get(reference.uri)
    pinned = await store.pin(reference.uri)
    pinned_restored = await store.get(pinned.uri)

    assert restored.snapshot_id == pinned_restored.snapshot_id == "20260805T120000Z-abc123"
    assert (root / "pinned" / "newsroom-a").is_dir()
    # Another deployment under the same unusual root is still refused.
    with pytest.raises(SnapshotError, match="does not belong"):
        await FileSnapshotStore(root, "newsroom-b").get(reference.uri)


@pytest.mark.asyncio
async def test_percent_encoding_cannot_escape_the_deployment(tmp_path: Path) -> None:
    store = FileSnapshotStore(tmp_path / "root", "newsroom-a")
    escape = (tmp_path / "root" / "temporary" / "newsroom-a").as_uri() + "/%2E%2E/newsroom-b/x.json"

    with pytest.raises(SnapshotError, match="does not belong"):
        await store.get(escape)


@pytest.mark.asyncio
async def test_deployment_cannot_pin_or_read_another_deployments_snapshot(
    tmp_path: Path,
) -> None:
    first = FileSnapshotStore(tmp_path, "newsroom-a")
    second = FileSnapshotStore(tmp_path, "newsroom-b")
    reference = await first.put(snapshot())

    with pytest.raises(SnapshotError, match="does not belong"):
        await second.pin(reference.uri)
    with pytest.raises(SnapshotError, match="does not belong"):
        await second.get(reference.uri)


@pytest.mark.asyncio
async def test_snapshot_content_must_match_the_deployment_path(tmp_path: Path) -> None:
    # A file planted under this deployment's directory still carries its own
    # deployment_id, and a mismatch is refused rather than trusted by location.
    store = FileSnapshotStore(tmp_path, "newsroom-a")
    foreign = snapshot(deployment_id="newsroom-b")
    path = tmp_path / "temporary" / "newsroom-a" / "2026" / "08" / "05" / "planted.json"
    path.parent.mkdir(parents=True)
    path.write_text(foreign.model_dump_json())

    with pytest.raises(SnapshotError, match="does not match this deployment"):
        await store.get(path.as_uri())


@pytest.mark.asyncio
async def test_file_store_refuses_uris_outside_its_root(tmp_path: Path) -> None:
    store = FileSnapshotStore(tmp_path / "root", "newsroom-a")

    with pytest.raises(SnapshotError, match="local file snapshot URI"):
        await store.get("gs://bucket/temporary/newsroom-a/x.json")
    with pytest.raises(SnapshotError, match="does not belong"):
        await store.get((tmp_path / "elsewhere.json").as_uri())
    traversal = tmp_path / "root" / "temporary" / "newsroom-a" / ".." / "newsroom-b" / "x.json"
    with pytest.raises(SnapshotError, match="does not belong"):
        await store.get(traversal.as_uri())


@pytest.mark.asyncio
async def test_gcs_snapshot_reports_its_lifecycle_expiry() -> None:
    store = GcsSnapshotStore(
        "test-bucket",
        "newsroom-a",
        client=FakeStorageClient(),  # type: ignore[arg-type]
    )

    reference = await store.put(snapshot())

    assert reference.retention == "temporary"
    assert reference.expires_at == "2026-11-03T12:00:00+00:00"
    assert reference.uri.startswith("gs://test-bucket/temporary/newsroom-a/2026/08/05/")


@pytest.mark.asyncio
async def test_gcs_store_refuses_another_deployment_or_bucket() -> None:
    store = GcsSnapshotStore(
        "test-bucket",
        "newsroom-a",
        client=FakeStorageClient(),  # type: ignore[arg-type]
    )

    with pytest.raises(SnapshotError, match="does not belong"):
        await store.get("gs://test-bucket/temporary/newsroom-b/2026/08/05/x.json")
    with pytest.raises(SnapshotError, match="does not belong"):
        await store.pin("gs://test-bucket/pinned/newsroom-a/2026/08/05/x.json")
    with pytest.raises(SnapshotError, match="bucket does not match"):
        await store.get("gs://other-bucket/temporary/newsroom-a/2026/08/05/x.json")


def test_snapshot_without_query_url_remains_readable() -> None:
    payload = snapshot().model_dump(mode="json", exclude={"query_url"})

    restored = QuerySnapshot.model_validate(payload)

    assert restored.snapshot_version == 1
    assert restored.query_url is None
    assert restored.truncation_evidence is None


@pytest.mark.parametrize("snapshot_version", [2, 3])
def test_snapshot_versions_from_the_tenant_era_are_refused(snapshot_version: int) -> None:
    payload = snapshot().model_dump(mode="json") | {"snapshot_version": snapshot_version}

    with pytest.raises(ValidationError):
        QuerySnapshot.model_validate(payload)


def test_legacy_snapshot_review_status_is_ignored() -> None:
    payload = snapshot().model_dump(mode="json") | {"review_status": "reviewed"}

    restored = QuerySnapshot.model_validate(payload)

    assert "review_status" not in restored.model_dump(mode="json")


def test_snapshot_with_legacy_portal_urls_remains_readable() -> None:
    payload = snapshot().model_dump(mode="json") | {
        "source_url": "https://data.sfgov.org/d/abcd-1234",
        "query_url": (
            "https://data.sfgov.org/resource/abcd-1234.json?"
            "%24query=SELECT+count%28%2A%29+AS+total+LIMIT+2"
        ),
    }

    restored = QuerySnapshot.model_validate(payload)

    assert restored.source_url == "https://data.sfgov.org/d/abcd-1234"
    assert restored.query_url is not None
    assert restored.query_url.startswith("https://data.sfgov.org/resource/abcd-1234.json?")


def test_snapshot_records_the_deployment_and_not_a_tenant() -> None:
    fields = QuerySnapshot.model_fields

    assert "deployment_id" in fields
    assert "tenant_id" not in fields
    assert "source_id" not in fields
    assert "domain" in fields
