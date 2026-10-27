from __future__ import annotations

import logging

import httpx
import pytest

from opencivicquery.logging_redaction import (
    REDACTED,
    SecretRedactingFilter,
    install,
    scrub,
)

TOKEN = "ya29.a0AdMD6EjuQcC7ry6e4mEeBYuxhtvkXSChXgILNLbVdnKCx5Fm7dZob8ty1MZ"


@pytest.mark.parametrize(
    "raw",
    [
        f"https://oauth2.googleapis.com/tokeninfo?access_token={TOKEN}",
        f"https://example.test/cb?code=abc123&id_token={TOKEN}",
        f"POST /oauth/token client_secret={TOKEN}",
        f"Authorization: Bearer {TOKEN}",
    ],
)
def test_scrub_removes_credentials(raw: str) -> None:
    cleaned = scrub(raw)
    assert TOKEN not in cleaned
    assert REDACTED in cleaned


def test_scrub_leaves_ordinary_text_alone() -> None:
    text = "GET https://data.sf.gov/resource/wg3w-h783.json?$limit=20 200 OK"
    assert scrub(text) == text


def test_filter_redacts_url_argument() -> None:
    # httpx logs the URL as an object in record.args, not as part of the message.
    url = httpx.URL(f"https://oauth2.googleapis.com/tokeninfo?access_token={TOKEN}")
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("GET", url, "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )
    assert SecretRedactingFilter().filter(record) is True
    rendered = record.getMessage()
    assert TOKEN not in rendered
    assert REDACTED in rendered


def test_filter_redacts_dict_arguments() -> None:
    record = logging.LogRecord(
        name="opencivicquery.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="calling %(url)s",
        # logging unwraps a single mapping argument, matching logger.info("%(url)s", {...})
        args=({"url": f"https://example.test?access_token={TOKEN}"},),
        exc_info=None,
    )
    SecretRedactingFilter().filter(record)
    assert TOKEN not in record.getMessage()


def test_filter_preserves_non_secret_arguments() -> None:
    record = logging.LogRecord(
        name="opencivicquery.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="rows=%d dataset=%s",
        args=(20, "wg3w-h783"),
        exc_info=None,
    )
    SecretRedactingFilter().filter(record)
    assert record.getMessage() == "rows=20 dataset=wg3w-h783"
    assert record.args == (20, "wg3w-h783")


def test_install_redacts_through_the_httpx_logger(caplog: pytest.LogCaptureFixture) -> None:
    install()
    logger = logging.getLogger("httpx")
    with caplog.at_level(logging.INFO, logger="httpx"):
        logger.info(
            'HTTP Request: %s %s "%s %d %s"',
            "GET",
            httpx.URL(f"https://oauth2.googleapis.com/tokeninfo?access_token={TOKEN}"),
            "HTTP/1.1",
            200,
            "OK",
        )
    assert caplog.records, "expected the httpx logger to emit a record"
    assert TOKEN not in caplog.records[-1].getMessage()


def test_install_is_idempotent() -> None:
    logger = logging.getLogger("httpx")
    install()
    install()
    filters = [f for f in logger.filters if isinstance(f, SecretRedactingFilter)]
    assert len(filters) == 1
