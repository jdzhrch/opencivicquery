from __future__ import annotations

import asyncio
import json
import shutil
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

from opencivicquery.models import QuerySnapshot, SnapshotReference


class SnapshotError(RuntimeError):
    """A query snapshot could not be stored or pinned."""


class SnapshotStore(Protocol):
    async def put(self, snapshot: QuerySnapshot) -> SnapshotReference: ...

    async def pin(self, snapshot_uri: str) -> SnapshotReference: ...

    async def get(self, snapshot_uri: str) -> QuerySnapshot: ...


def _snapshot_bytes(snapshot: QuerySnapshot) -> bytes:
    payload = snapshot.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


TEMPORARY_RETENTION_DAYS = 90
"""Deletion age for `temporary/`. Must match the age in `operations/gcs-lifecycle.json`.

The bucket lifecycle rule performs the deletion. This constant only lets a response state
when that will happen, so a reporter can pin the evidence before it disappears. Lifecycle
runs asynchronously and never deletes early, so the stated time is the earliest the
snapshot can vanish, which is the safe direction to report.
"""


def _object_name(snapshot: QuerySnapshot, retention: str) -> str:
    date_path = snapshot.created_at.strftime("%Y/%m/%d")
    return f"{retention}/{snapshot.deployment_id}/{date_path}/{snapshot.snapshot_id}.json"


def _temporary_expiry(snapshot: QuerySnapshot) -> str:
    return (snapshot.created_at + timedelta(days=TEMPORARY_RETENTION_DAYS)).isoformat()


class FileSnapshotStore:
    def __init__(self, root: Path, deployment_id: str) -> None:
        self._root = root.resolve()
        self._deployment_id = deployment_id

    async def put(self, snapshot: QuerySnapshot) -> SnapshotReference:
        object_name = _object_name(snapshot, "temporary")
        path = (self._root / object_name).resolve()
        if self._root not in path.parents:
            raise SnapshotError("Snapshot path escaped the configured root")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(_snapshot_bytes(snapshot))
        except FileExistsError as exc:
            raise SnapshotError("Snapshot ID already exists") from exc
        return SnapshotReference(
            snapshot_id=snapshot.snapshot_id,
            uri=path.as_uri(),
            retention="temporary",
        )

    async def pin(self, snapshot_uri: str) -> SnapshotReference:
        source = self._resolve_uri(snapshot_uri, allowed_retention={"temporary"})
        relative = source.relative_to(self._root / "temporary")
        destination = (self._root / "pinned" / relative).resolve()
        if self._root not in destination.parents:
            raise SnapshotError("Pinned snapshot path escaped the configured root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
                shutil.copyfileobj(source_handle, destination_handle)
        except FileExistsError:
            pass
        except FileNotFoundError as exc:
            raise SnapshotError("Snapshot does not exist") from exc
        return SnapshotReference(
            snapshot_id=source.stem,
            uri=destination.as_uri(),
            retention="pinned",
        )

    def _resolve_uri(self, snapshot_uri: str, *, allowed_retention: set[str]) -> Path:
        parsed = urlparse(snapshot_uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise SnapshotError("Expected a local file snapshot URI")
        # as_uri() percent-encodes spaces and non-ASCII characters, so decode first.
        source = Path(unquote(parsed.path)).resolve()
        if not any(
            (self._root / retention / self._deployment_id).resolve() in source.parents
            for retention in allowed_retention
        ):
            raise SnapshotError("Snapshot does not belong to this deployment")
        return source

    async def get(self, snapshot_uri: str) -> QuerySnapshot:
        source = self._resolve_uri(
            snapshot_uri,
            allowed_retention={"temporary", "pinned"},
        )
        try:
            payload = json.loads(source.read_bytes())
            snapshot = QuerySnapshot.model_validate(payload)
        except (OSError, ValueError) as exc:
            raise SnapshotError("Snapshot could not be read") from exc
        if snapshot.deployment_id != self._deployment_id:
            raise SnapshotError("Snapshot content does not match this deployment")
        return snapshot


class GcsSnapshotStore:
    def __init__(
        self,
        bucket_name: str,
        deployment_id: str,
        *,
        client: storage.Client | None = None,
    ) -> None:
        if not bucket_name or "/" in bucket_name:
            raise SnapshotError("Invalid snapshot bucket name")
        self._bucket_name = bucket_name
        self._deployment_id = deployment_id
        self._client = client or storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    async def put(self, snapshot: QuerySnapshot) -> SnapshotReference:
        object_name = _object_name(snapshot, "temporary")
        blob = self._bucket.blob(object_name)
        try:
            await asyncio.to_thread(
                blob.upload_from_string,
                _snapshot_bytes(snapshot),
                content_type="application/json",
                if_generation_match=0,
            )
        except PreconditionFailed as exc:
            raise SnapshotError("Snapshot ID already exists") from exc
        return SnapshotReference(
            snapshot_id=snapshot.snapshot_id,
            uri=f"gs://{self._bucket_name}/{object_name}",
            retention="temporary",
            expires_at=_temporary_expiry(snapshot),
        )

    async def pin(self, snapshot_uri: str) -> SnapshotReference:
        source_name = self._resolve_uri(snapshot_uri, allowed_retention={"temporary"})
        destination_name = source_name.replace("temporary/", "pinned/", 1)
        source = self._bucket.blob(source_name)

        def copy() -> None:
            if not source.exists():
                raise SnapshotError("Snapshot does not exist")
            with suppress(PreconditionFailed):
                self._bucket.copy_blob(
                    source,
                    self._bucket,
                    destination_name,
                    if_generation_match=0,
                )

        await asyncio.to_thread(copy)
        return SnapshotReference(
            snapshot_id=Path(source_name).stem,
            uri=f"gs://{self._bucket_name}/{destination_name}",
            retention="pinned",
        )

    def _resolve_uri(self, snapshot_uri: str, *, allowed_retention: set[str]) -> str:
        parsed = urlparse(snapshot_uri)
        source_name = parsed.path.lstrip("/")
        if parsed.scheme != "gs" or parsed.netloc != self._bucket_name:
            raise SnapshotError("Snapshot bucket does not match this service")
        if not any(
            source_name.startswith(f"{retention}/{self._deployment_id}/")
            for retention in allowed_retention
        ):
            raise SnapshotError("Snapshot does not belong to this deployment")
        return source_name

    async def get(self, snapshot_uri: str) -> QuerySnapshot:
        source_name = self._resolve_uri(
            snapshot_uri,
            allowed_retention={"temporary", "pinned"},
        )
        blob = self._bucket.blob(source_name)
        try:
            raw_bytes = await asyncio.to_thread(blob.download_as_bytes)
            snapshot = QuerySnapshot.model_validate_json(raw_bytes)
        except (NotFound, ValueError) as exc:
            raise SnapshotError("Snapshot could not be read") from exc
        if snapshot.deployment_id != self._deployment_id:
            raise SnapshotError("Snapshot content does not match this deployment")
        return snapshot
