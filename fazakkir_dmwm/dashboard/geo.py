"""Best-effort client IP detection and country resolution.

The dashboard uses these to show where visitors come from. Resolution is done
with a free, key-less endpoint and cached so a given IP is only looked up
once per process (and results are stored on the ``ActivityLog`` row anyway).
"""
from __future__ import annotations

import ipaddress
import json
import logging
from urllib.request import urlopen
from urllib.error import URLError

from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_PREFIX = "fazakkir:geo:country:"
CACHE_TTL = 60 * 60 * 24  # 24 hours


def get_client_ip(request) -> str | None:
    """Return the best guess at the visitor's IP, honouring proxies."""
    # Cloudflare / reverse proxies put the original client in X-Forwarded-For.
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        # The left-most entry is the original client.
        ip = xff.split(",")[0].strip()
        if ip:
            return ip
    real_ip = request.META.get("HTTP_X_REAL_IP")
    if real_ip:
        return real_ip.strip()
    return request.META.get("REMOTE_ADDR")


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_multicast


def resolve_country(ip: str | None) -> str:
    """Resolve an IP to a country name, cached. Returns 'Unknown' on failure."""
    if not ip:
        return "Unknown"
    if _is_private(ip):
        return "Local"
    cache_key = CACHE_PREFIX + ip
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    country = "Unknown"
    try:
        with urlopen(f"http://ip-api.com/json/{ip}?fields=country", timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("country"):
            country = str(data["country"])
    except (URLError, OSError, ValueError, TimeoutError):
        logger.debug("Country lookup failed for %s", ip, exc_info=True)
    cache.set(cache_key, country, CACHE_TTL)
    return country