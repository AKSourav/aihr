"""Write-boundary checks for tenant-supplied outbound base URLs.

This narrows SSRF reach; it does not close it. A hostname that passes here can
still resolve to a private address, and can change between this check and the
request (DNS rebinding). Resolution-time pinning and an egress proxy are the
real fix and are not implemented.
"""

import socket
from ipaddress import ip_address
from urllib.parse import urlsplit

from core.settings import get_settings

BLOCKED_SUFFIXES = (".local", ".internal")


def _is_ip_literal(host: str) -> bool:
    try:
        ip_address(host)
        return True
    except ValueError:
        pass
    # inet_aton accepts the legacy hex, octal and packed forms the resolver also
    # accepts, e.g. 0x7f.0.0.1 reaches 127.0.0.1 but is not a valid ip_address.
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


def validate_base_url(value: str | None) -> str | None:
    if not value:
        return value

    try:
        parts = urlsplit(value.strip())
    except ValueError:
        raise ValueError("base_url is not a valid URL") from None

    allowed = ("https", "http") if get_settings().dev_routes_enabled else ("https",)
    if parts.scheme not in allowed:
        raise ValueError(f"base_url scheme must be one of {', '.join(allowed)}")

    if "@" in parts.netloc:
        raise ValueError("base_url must not contain credentials")

    try:
        host = parts.hostname
    except ValueError:
        raise ValueError("base_url is not a valid URL") from None

    if not host:
        raise ValueError("base_url must contain a hostname")

    host = host.rstrip(".").lower()

    if _is_ip_literal(host):
        raise ValueError("base_url must use a hostname, not a literal IP address")

    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise ValueError("base_url must not point at an internal hostname")

    if "." not in host:
        raise ValueError("base_url must use a fully qualified hostname")

    return value
