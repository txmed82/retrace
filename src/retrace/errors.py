from __future__ import annotations

import httpx


def format_user_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "network unavailable or host could not be resolved"
    if isinstance(exc, httpx.TimeoutException):
        return "network request timed out"
    if isinstance(exc, httpx.TransportError):
        return f"network transport error: {exc.__class__.__name__}"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return f"HTTP {status} from {exc.request.url}"
    return str(exc)
