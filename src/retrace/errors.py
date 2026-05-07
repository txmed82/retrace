from __future__ import annotations

import httpx
from urllib.parse import urlsplit, urlunsplit


def format_user_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "network unavailable or host could not be resolved"
    if isinstance(exc, httpx.TimeoutException):
        return "network request timed out"
    if isinstance(exc, httpx.TransportError):
        return f"network transport error: {exc.__class__.__name__}"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        parts = urlsplit(str(exc.request.url))
        safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        return f"HTTP {status} from {safe_url}"
    return str(exc)
