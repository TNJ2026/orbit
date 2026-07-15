"""Shared HTTP helpers for Orbit's local-only JSON control surface."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse


LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


async def read_json(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def cors_headers(request: Request) -> dict[str, str]:
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname in LOCAL_HOSTNAMES:
        return {
            "access-control-allow-origin": origin,
            "access-control-allow-methods": "GET, POST, OPTIONS",
            "access-control-allow-headers": "content-type",
            "vary": "Origin",
        }
    return {}


def json_response(
    request: Request, data: Any, status_code: int = 200
) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers=cors_headers(request))


def json_error(
    message: str, status_code: int = 400, request: Request | None = None
) -> JSONResponse:
    headers = cors_headers(request) if request is not None else None
    return JSONResponse({"error": message}, status_code=status_code, headers=headers)


def is_loopback_peer(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        ip = ipaddress.ip_address(client.host)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return ip.is_loopback
    except ValueError:
        return False


def forbid_non_local(request: Request) -> JSONResponse | None:
    """Reject non-loopback peers, hostnames, and browser origins."""
    if not is_loopback_peer(request):
        return json_error("API is only served to local clients", 403, request)
    if request.url.hostname not in LOCAL_HOSTNAMES:
        return json_error("API is only served to local hostnames", 403, request)
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in LOCAL_HOSTNAMES:
        return json_error("cross-origin requests are not allowed", 403, request)
    return None
