"""Strip OAuth credentials out of log records before any handler sees them.

Google's tokeninfo endpoint takes the access token as a query parameter, so the
HTTP client library writes the whole token into its INFO-level request line.
Cloud Logging then holds a live bearer credential for as long as the log is
retained. The filter below rewrites those records instead, so the leak cannot
come back through a different call site or a different library.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"((?:access|id|refresh)_token=)[^&\s\"'&]+", re.IGNORECASE),
    re.compile(r"((?:client_secret|code|assertion)=)[^&\s\"'&]+", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/\-]+=*", re.IGNORECASE),
)


def scrub(text: str) -> str:
    """Replace every credential-shaped value in ``text``."""
    for pattern in _PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    return text


def _scrub_value(value: object) -> object:
    # Log arguments are often URL objects rather than strings, so compare on the
    # rendered form and only substitute when something actually matched. Anything
    # untouched keeps its original type and formatting behaviour.
    if isinstance(value, (int, float, bool, type(None))):
        return value
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - a broken __str__ is not our problem
        return value
    cleaned = scrub(text)
    return cleaned if cleaned != text else value


class SecretRedactingFilter(logging.Filter):
    """Rewrite records that carry OAuth credentials."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _scrub_value(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {key: _scrub_value(val) for key, val in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(_scrub_value(item) for item in record.args)
        return True


def install(*, logger_names: tuple[str, ...] = ("httpx", "httpcore", "urllib3")) -> None:
    """Attach the filter to the root handlers and to known chatty HTTP loggers.

    Handler-level filters run for every record that reaches the handler, including
    records propagated from child loggers. The named loggers are covered directly
    as well, so a library that installs its own handler is still redacted.
    """
    redactor = SecretRedactingFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(existing, SecretRedactingFilter) for existing in handler.filters):
            handler.addFilter(redactor)
    for name in logger_names:
        logger = logging.getLogger(name)
        if not any(isinstance(existing, SecretRedactingFilter) for existing in logger.filters):
            logger.addFilter(redactor)
