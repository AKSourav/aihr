"""Write-boundary checks for tenant-supplied outbound base URLs.

This narrows SSRF reach; it does not close it. A hostname that passes here can
still resolve to a private address, and can change between this check and the
request (DNS rebinding). Resolution-time pinning and an egress proxy are the
real fix and are not implemented.
"""

from ipaddress import ip_address
from urllib.parse import urlsplit

from core.settings import get_settings

BLOCKED_SUFFIXES = (".local", ".internal")


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

    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("base_url must use a hostname, not a literal IP address")

    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise ValueError("base_url must not point at an internal hostname")

    if "." not in host:
        raise ValueError("base_url must use a fully qualified hostname")

    return value
