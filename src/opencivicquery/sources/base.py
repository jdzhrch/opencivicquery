from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from opencivicquery.models import (
    CatalogEntry,
    DatasetDescription,
    QueryRequest,
    SourceQueryResult,
)


class SourceError(RuntimeError):
    """A data source returned an invalid or unsuccessful response."""


class SourceUnreachable(SourceError):
    """The request never got an answer: the connection failed before any response."""


class SourceAnswerNotJson(SourceError):
    """A source that answers in JSON sent something else with HTTP 200."""


class SourceQueryError(SourceError):
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


class AddressGeocoder(Protocol):
    """A connector that places one street address in official geographies."""

    host: str
    """The fixed host it calls. It doubles as the per-minute usage bucket."""
    row_grain: str

    def validate(self, address: str) -> str:
        """Return the address as it will be sent, or raise SourceQueryError."""
        ...

    async def geocode(self, address: str) -> SourceQueryResult: ...

    def warnings_for(self, result: SourceQueryResult) -> list[str]:
        """The documented caveats that apply to this result, most urgent first."""
        ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Lookup:
    """One finished record lookup: the result to freeze and what a reader must be told."""

    result: SourceQueryResult
    row_grain: str
    """What one row of result.rows represents, from the source's own documentation."""
    warnings: list[str]
    """The documented caveats that apply to this result, most urgent first."""
    rules: list[str] = field(default_factory=list)
    """Presentation rules specific to this result, most urgent first."""
    escalations: list[str] = field(default_factory=list)
    """Stable codes for the urgent rules above, for the audit record."""


@dataclass(frozen=True)
class PreparedLookup:
    """A validated request to one fixed host, ready to run once the usage check passes.

    Building one must not touch the network, so an invalid request never costs an
    allowance or reaches the source.
    """

    host: str
    """The fixed host it calls. It doubles as the per-minute usage bucket."""
    arguments: dict[str, Any]
    """The normalized arguments, recorded in the audit log."""
    run: Callable[[], Awaitable[Lookup]]
