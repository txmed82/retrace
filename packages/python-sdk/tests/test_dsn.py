"""DSN parser tests."""

from __future__ import annotations

import pytest

from retrace_sdk.dsn import DsnError, parse_dsn


def test_parse_dsn_basic():
    dsn = parse_dsn("http://rtpk_abc@127.0.0.1:8788/proj_1")
    assert dsn.scheme == "http"
    assert dsn.public_key == "rtpk_abc"
    assert dsn.host == "127.0.0.1"
    assert dsn.port == 8788
    assert dsn.path_prefix == ""
    assert dsn.project_id == "proj_1"
    assert dsn.base_url == "http://127.0.0.1:8788"
    assert dsn.envelope_url == "http://127.0.0.1:8788/api/sentry/proj_1/envelope/"
    assert dsn.store_url == "http://127.0.0.1:8788/api/sentry/proj_1/store/"


def test_parse_dsn_with_path_prefix():
    """Reverse-proxy mounts like `/retrace` must round-trip."""
    dsn = parse_dsn("https://rtpk_xyz@retrace.example.com/retrace/proj_42")
    assert dsn.path_prefix == "retrace"
    assert dsn.project_id == "proj_42"
    assert dsn.base_url == "https://retrace.example.com/retrace"
    assert dsn.envelope_url == "https://retrace.example.com/retrace/api/sentry/proj_42/envelope/"


def test_parse_dsn_with_no_port():
    dsn = parse_dsn("https://rtpk_a@retrace.example.com/proj")
    assert dsn.port is None
    assert dsn.base_url == "https://retrace.example.com"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-url",
        "http://no-key-host/proj",         # no public key
        "http://rtpk_a@/proj",              # no host
        "http://rtpk_a@host",               # no project id
        "http://rtpk_a@host/",              # empty path after split
    ],
)
def test_parse_dsn_rejects_malformed(bad):
    with pytest.raises(DsnError):
        parse_dsn(bad)


def test_parse_dsn_rejects_none():
    with pytest.raises(DsnError):
        parse_dsn(None)  # type: ignore[arg-type]
