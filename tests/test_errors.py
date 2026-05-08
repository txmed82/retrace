import httpx

from retrace.errors import format_user_error


def test_format_user_error_sanitizes_network_connect_noise():
    exc = httpx.ConnectError("[Errno 8] nodename nor servname provided")

    assert format_user_error(exc) == "network unavailable or host could not be resolved"


def test_format_user_error_summarizes_timeout():
    exc = httpx.ReadTimeout("timed out")

    assert format_user_error(exc) == "network request timed out"


def test_format_user_error_summarizes_http_status():
    request = httpx.Request("GET", "https://api.example.test/x?token=secret")
    response = httpx.Response(401, request=request)
    exc = httpx.HTTPStatusError("bad credentials", request=request, response=response)

    assert format_user_error(exc) == "HTTP 401 from https://api.example.test/x"
