from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO
from uuid import uuid4


class AuditLogger:
    def __init__(
        self, logger: logging.Logger | None = None, *, stream: TextIO | None = None
    ) -> None:
        """Write one JSON line per call. Pass stream=sys.stderr under the stdio transport,
        where stdout carries the MCP protocol and any other output would corrupt it."""
        if logger is not None:
            self._logger = logger
            return
        self._logger = logging.getLogger("opencivicquery.audit")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        # Replace any earlier handler so the stream chosen here always wins.
        for existing in list(self._logger.handlers):
            self._logger.removeHandler(existing)
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

    def record(self, action: str, **fields: Any) -> str:
        event_id = str(uuid4())
        payload = {
            "event": "opencivicquery_usage",
            "event_id": event_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            "action": action,
            **fields,
        }
        self._logger.info(json.dumps(payload, sort_keys=True, default=str))
        return event_id
