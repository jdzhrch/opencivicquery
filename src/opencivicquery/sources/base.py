from __future__ import annotations

from typing import Protocol

from opencivicquery.models import (
    CatalogEntry,
    DatasetDescription,
    QueryRequest,
    SourceQueryResult,
)


class StructuredSource(Protocol):
    """A connector that answers with numbers: search, describe, and query datasets."""

    async def resolve_portal(self, domain: str) -> str: ...

    def cached_portal(self, domain: str) -> str | None: ...

    def validate_query(self, request: QueryRequest) -> None: ...

    async def search(
        self, search_term: str, limit: int = 10, *, domain: str | None = None
    ) -> list[CatalogEntry]: ...

    async def describe(self, domain: str, dataset_id: str) -> DatasetDescription: ...

    async def query(self, request: QueryRequest) -> SourceQueryResult: ...

    async def count_rows(
        self,
        request: QueryRequest,
        *,
        reference: SourceQueryResult,
    ) -> SourceQueryResult: ...

    async def close(self) -> None: ...
