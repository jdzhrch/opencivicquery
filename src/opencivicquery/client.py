from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

ClientAddressSource = Literal["peer", "x-forwarded-for-last"]
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

UNKNOWN_CLIENT = "unknown"

_HASH_SALT = secrets.token_bytes(16)
"""Per-process salt. Audit records can group one client's calls without storing the
address, and the salt never leaves memory, so the hash cannot be reversed later."""


@dataclass(frozen=True)
class ClientIdentity:
    """Who a call comes from, as far as its network address can tell.

    capped is False when the address stands for many people at once: a hosted AI
    connector's egress, or an address that is not a public unicast one. Those calls
    skip the per-client cap and still count against the server-wide caps. A single
    person scripting calls from their own address is capped.
    """

    key: str
    capped: bool

    def __repr__(self) -> str:
        # Keep the raw address out of any accidental log of this object.
        return f"ClientIdentity(key=<{client_hash(self.key)}>, capped={self.capped})"


def identify(address: str | None, shared_networks: Iterable[Network] = ()) -> ClientIdentity:
    """Turn a client address into a usage key.

    An IPv4 address is its own key. An IPv6 client usually controls a whole /64, so the
    key is that network; otherwise one person could rotate through billions of keys.
    """
    if not address:
        return ClientIdentity(UNKNOWN_CLIENT, capped=False)
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return ClientIdentity(UNKNOWN_CLIENT, capped=False)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv6Address):
        key = str(ipaddress.IPv6Network(f"{ip}/64", strict=False))
    else:
        key = str(ip)
    shared = not ip.is_global or any(ip in network for network in shared_networks)
    return ClientIdentity(key, capped=not shared)


def client_address(
    headers: Any,
    peer: str | None,
    source: ClientAddressSource,
) -> str | None:
    """Read the caller's address the way the hosting platform reports it.

    Cloud Run appends the real client address as the last X-Forwarded-For entry and keeps
    anything the client sent in front of it; it also merges repeated header lines into
    one. Every header line is still joined here, so a host that keeps repeats cannot let
    a caller pick the key through a first line. Use "x-forwarded-for-last" only behind
    exactly one proxy that appends this way.
    """
    if source == "peer":
        return peer
    if headers is None:
        return None
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        lines = list(getlist("x-forwarded-for"))
    elif isinstance(headers, Mapping):
        value = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        lines = [value] if value else []
    else:
        lines = []
    entries = [entry.strip() for line in lines for entry in line.split(",") if entry.strip()]
    return entries[-1] if entries else None


def client_hash(key: str) -> str:
    return hashlib.sha256(_HASH_SALT + key.encode()).hexdigest()[:12]


def parse_networks(values: Iterable[str]) -> list[Network]:
    return [ipaddress.ip_network(value.strip(), strict=False) for value in values]


def load_networks_file(path: Path) -> list[Network]:
    """Read a published egress list in the {"prefixes": [{"ipv4Prefix": ...}]} format."""
    payload = json.loads(path.read_text())
    prefixes = payload.get("prefixes") if isinstance(payload, dict) else None
    if not isinstance(prefixes, list):
        raise ValueError("expected a JSON object with a 'prefixes' list")
    values: list[str] = []
    for item in prefixes:
        if not isinstance(item, dict):
            raise ValueError("each prefix must be an object")
        value = item.get("ipv4Prefix") or item.get("ipv6Prefix")
        if not isinstance(value, str):
            raise ValueError("each prefix needs ipv4Prefix or ipv6Prefix")
        values.append(value)
    return parse_networks(values)
