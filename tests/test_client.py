from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import pytest
from starlette.datastructures import Headers

from opencivicquery.client import (
    UNKNOWN_CLIENT,
    client_address,
    client_hash,
    identify,
    load_networks_file,
)

OPENAI_EGRESS = [ipaddress.ip_network("20.98.18.80/28")]


@pytest.mark.parametrize(
    ("address", "key", "capped"),
    [
        ("8.8.8.8", "8.8.8.8", True),
        (" 8.8.8.8 ", "8.8.8.8", True),
        ("2606:4700:4700::1111", "2606:4700:4700::/64", True),
        ("2606:4700:4700:0:ffff:ffff:ffff:ffff", "2606:4700:4700::/64", True),
        ("::ffff:8.8.8.8", "8.8.8.8", True),
        # Addresses that stand for no single public client are never capped.
        ("0.0.0.0", "0.0.0.0", False),
        ("10.1.2.3", "10.1.2.3", False),
        ("169.254.169.126", "169.254.169.126", False),
        ("127.0.0.1", "127.0.0.1", False),
        (None, UNKNOWN_CLIENT, False),
        ("", UNKNOWN_CLIENT, False),
        ("not-an-address", UNKNOWN_CLIENT, False),
        ("8.8.8.8:443", UNKNOWN_CLIENT, False),
    ],
)
def test_identify(address: str | None, key: str, capped: bool) -> None:
    identity = identify(address)
    assert identity.key == key
    assert identity.capped is capped


def test_shared_egress_networks_skip_the_cap() -> None:
    assert identify("20.98.18.85", OPENAI_EGRESS).capped is False
    assert identify("20.98.18.96", OPENAI_EGRESS).capped is True


def test_identity_repr_hides_the_address() -> None:
    assert "8.8.8.8" not in repr(identify("8.8.8.8"))


def test_peer_mode_ignores_forwarding_headers() -> None:
    headers = Headers({"x-forwarded-for": "1.1.1.1"})
    assert client_address(headers, "8.8.8.8", "peer") == "8.8.8.8"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("8.8.8.8", "8.8.8.8"),
        ("1.1.1.1,8.8.8.8", "8.8.8.8"),
        ("1.1.1.1, 9.9.9.9, 8.8.8.8", "8.8.8.8"),
        ("8.8.8.8, ", "8.8.8.8"),
    ],
)
def test_forwarded_mode_trusts_only_the_last_entry(header: str, expected: str) -> None:
    headers = Headers({"x-forwarded-for": header})
    assert client_address(headers, "169.254.1.1", "x-forwarded-for-last") == expected


def test_repeated_header_lines_are_joined_before_taking_the_last_entry() -> None:
    # Starlette's Headers.get returns only the first line, which a caller controls.
    headers = Headers(
        raw=[(b"x-forwarded-for", b"6.6.6.6"), (b"x-forwarded-for", b"1.1.1.1, 8.8.8.8")]
    )
    assert headers.get("x-forwarded-for") == "6.6.6.6"
    assert client_address(headers, "169.254.1.1", "x-forwarded-for-last") == "8.8.8.8"


def test_plain_mappings_still_work() -> None:
    assert client_address({"x-forwarded-for": "1.1.1.1,8.8.8.8"}, None, "x-forwarded-for-last") == (
        "8.8.8.8"
    )


@pytest.mark.parametrize(
    "headers",
    [None, Headers({}), Headers({"x-forwarded-for": ""}), Headers({"x-forwarded-for": " , "})],
)
def test_forwarded_mode_without_a_usable_header_returns_none(headers: Headers | None) -> None:
    # Never fall back to the proxy's own peer address: every caller would share it.
    assert client_address(headers, "169.254.1.1", "x-forwarded-for-last") is None


def test_client_hash_is_stable_short_and_hides_the_address() -> None:
    first = client_hash("8.8.8.8")

    assert first == client_hash("8.8.8.8")
    assert first != client_hash("8.8.4.4")
    assert len(first) == 12
    assert "8.8.8.8" not in first


def test_load_networks_file_reads_the_published_format(tmp_path: Path) -> None:
    path = tmp_path / "egress.json"
    path.write_text(
        json.dumps(
            {"prefixes": [{"ipv4Prefix": "20.98.18.80/28"}, {"ipv6Prefix": "2001:db8::/32"}]}
        )
    )

    networks = load_networks_file(path)

    assert [str(network) for network in networks] == ["20.98.18.80/28", "2001:db8::/32"]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"prefixes": "x"},
        {"prefixes": [1]},
        {"prefixes": [{}]},
        {"prefixes": [{"ipv4Prefix": "x"}]},
    ],
)
def test_load_networks_file_refuses_malformed_lists(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "egress.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError):
        load_networks_file(path)
