"""P1.4 follow-up — HAR → APITestSpec param tests.

These tests are pure (no I/O, no filesystem). They pin the
filtering rules + header redaction + body extraction shape of the
HAR importer so we don't regress when adding filter knobs later.
"""

from __future__ import annotations

from retrace.api_har_import import import_har, import_summary, looks_like_har


def _har(*entries) -> dict:
    return {"log": {"version": "1.2", "entries": list(entries)}}


def _entry(
    *,
    method: str = "GET",
    url: str = "https://api.example.com/v1/users",
    headers: list | None = None,
    query: list | None = None,
    post_text: str | None = None,
    post_mime: str = "",
    status: int = 200,
) -> dict:
    request = {
        "method": method,
        "url": url,
        "headers": headers or [],
        "queryString": query or [],
    }
    if post_text is not None:
        request["postData"] = {"text": post_text, "mimeType": post_mime}
    return {"request": request, "response": {"status": status, "statusText": "OK"}}


def test_empty_har_yields_no_specs():
    assert import_har({"log": {"entries": []}}) == []


def test_har_without_log_envelope():
    """Some tools emit `{"entries": [...]}` without the `log` wrapper."""
    har = {"entries": [_entry(method="GET", url="https://x.com/a")]}
    assert len(import_har(har)) == 1


def test_basic_get_request_becomes_spec_params():
    har = _har(_entry(method="GET", url="https://api.example.com/v1/users"))
    result = import_har(har)
    assert len(result) == 1
    params = result[0]
    assert params["method"] == "GET"
    assert params["url"] == "https://api.example.com/v1/users"
    assert params["name"] == "GET /v1/users"
    assert params["expected_status"] == 200
    assert params["query"] == {}
    assert params["headers"] == {}
    assert params["body"] is None


def test_query_string_extracted_separately():
    har = _har(
        _entry(
            method="GET",
            url="https://api.example.com/v1/search?q=hi&page=2",
            query=[
                {"name": "q", "value": "hi"},
                {"name": "page", "value": "2"},
            ],
        )
    )
    params = import_har(har)[0]
    assert params["query"] == {"q": "hi", "page": "2"}
    # URL is stored bare; query lives on the spec separately.
    assert "?" not in params["url"]


def test_sensitive_headers_are_stripped():
    """Authorization, Cookie, X-API-Key etc. would leak credentials
    if committed into a spec file. The importer drops them; users
    re-attach via env_profile / headers_env."""
    har = _har(
        _entry(
            headers=[
                {"name": "Authorization", "value": "Bearer super-secret-token"},
                {"name": "Cookie", "value": "session=hunter2"},
                {"name": "X-API-Key", "value": "rt_pk_real_key"},
                {"name": "Accept", "value": "application/json"},
            ]
        )
    )
    headers = import_har(har)[0]["headers"]
    assert "Authorization" not in headers
    assert "Cookie" not in headers
    assert "X-API-Key" not in headers
    # Non-sensitive header survives.
    assert headers.get("Accept") == "application/json"


def test_noisy_browser_headers_are_stripped():
    """User-Agent, sec-fetch-*, Accept-Encoding etc. would make specs
    brittle. Drop them."""
    har = _har(
        _entry(
            headers=[
                {"name": "User-Agent", "value": "Mozilla/5.0 ..."},
                {"name": "sec-fetch-mode", "value": "cors"},
                {"name": "Accept-Encoding", "value": "gzip, deflate, br"},
                {"name": "X-Custom-Trace", "value": "keep-me"},
            ]
        )
    )
    headers = import_har(har)[0]["headers"]
    assert "User-Agent" not in headers
    assert "sec-fetch-mode" not in headers
    assert "Accept-Encoding" not in headers
    assert headers.get("X-Custom-Trace") == "keep-me"


def test_http2_pseudo_headers_dropped():
    """HAR captured from HTTP/2 can include `:authority`, `:method`,
    `:path`. Those aren't real request headers."""
    har = _har(
        _entry(
            headers=[
                {"name": ":authority", "value": "api.example.com"},
                {"name": ":method", "value": "GET"},
                {"name": "Accept", "value": "application/json"},
            ]
        )
    )
    headers = import_har(har)[0]["headers"]
    assert ":authority" not in headers
    assert ":method" not in headers
    assert "Accept" in headers


def test_include_host_filter_glob():
    har = _har(
        _entry(url="https://api.staging.example.com/v1/x"),
        _entry(url="https://api.example.com/v1/x"),
        _entry(url="https://www.unrelated.com/x"),
    )
    result = import_har(har, include_hosts=["*.staging.example.com"])
    assert len(result) == 1
    assert "staging" in result[0]["url"]


def test_include_method_filter_case_insensitive():
    har = _har(
        _entry(method="GET", url="https://x/a"),
        _entry(method="POST", url="https://x/b"),
        _entry(method="DELETE", url="https://x/c"),
    )
    result = import_har(har, include_methods=["post", "DELETE"])
    methods = {p["method"] for p in result}
    assert methods == {"POST", "DELETE"}


def test_exclude_path_filter_glob():
    har = _har(
        _entry(url="https://x/api/users"),
        _entry(url="https://x/static/app.js"),
        _entry(url="https://x/static/app.css"),
        _entry(url="https://x/metrics"),
    )
    result = import_har(
        har, exclude_paths=["/static/*", "/metrics"]
    )
    paths = {p["url"].rsplit("/", 1)[-1] for p in result}
    assert paths == {"users"}


def test_json_body_parsed_into_structured_form():
    har = _har(
        _entry(
            method="POST",
            url="https://x/api/users",
            post_text='{"email":"a@b.com","name":"A"}',
            post_mime="application/json",
        )
    )
    params = import_har(har)[0]
    assert params["body"] == {"email": "a@b.com", "name": "A"}
    # Content-Type re-attached when missing on the request.
    assert params["headers"].get("Content-Type") == "application/json"


def test_json_body_detected_without_mimetype():
    """Curl-saved HARs sometimes omit mimeType. Auto-detect from
    leading `{`/`[`."""
    har = _har(
        _entry(
            method="POST",
            url="https://x/api/users",
            post_text='[1,2,3]',
            post_mime="",
        )
    )
    params = import_har(har)[0]
    assert params["body"] == [1, 2, 3]


def test_malformed_json_body_kept_as_text():
    har = _har(
        _entry(
            method="POST",
            url="https://x/api/users",
            post_text='{not valid json',
            post_mime="application/json",
        )
    )
    params = import_har(har)[0]
    assert params["body"] == "{not valid json"


def test_non_json_body_kept_as_text():
    har = _har(
        _entry(
            method="POST",
            url="https://x/api/form",
            post_text="email=a%40b&name=A",
            post_mime="application/x-www-form-urlencoded",
        )
    )
    params = import_har(har)[0]
    assert params["body"] == "email=a%40b&name=A"


def test_expected_status_taken_from_response():
    har = _har(_entry(url="https://x/api/x", status=201))
    assert import_har(har)[0]["expected_status"] == 201


def test_zero_or_negative_status_falls_back_to_default():
    """Browsers sometimes record `status: 0` for cancelled/failed
    requests. Use 200 as a sane default rather than persisting 0."""
    har = _har(_entry(status=0))
    assert import_har(har)[0]["expected_status"] == 200


def test_unsupported_method_dropped():
    har = _har(
        _entry(method="WHAT", url="https://x/api/x"),
        _entry(method="GET", url="https://x/api/x"),
    )
    result = import_har(har)
    assert len(result) == 1
    assert result[0]["method"] == "GET"


def test_skip_entries_missing_url_or_method():
    har = _har(
        _entry(method="", url="https://x/api/x"),
        _entry(method="GET", url=""),
        _entry(method="GET", url="https://x/api/x"),
    )
    result = import_har(har)
    assert len(result) == 1


def test_skip_entries_with_invalid_url():
    """A `url` of `/foo/bar` (no scheme) means we can't host-filter,
    can't dedupe — drop it."""
    har = _har(_entry(url="not-a-url"))
    assert import_har(har) == []


def test_env_profile_pass_through():
    har = _har(_entry(method="GET", url="https://x/a"))
    params = import_har(har, env_profile="staging")[0]
    assert params["env_profile"] == "staging"


def test_name_prefix_pass_through():
    har = _har(_entry(method="POST", url="https://x/v1/orders"))
    params = import_har(har, name_prefix="checkout")[0]
    assert params["name"] == "checkout POST /v1/orders"


def test_long_paths_compacted_in_spec_name():
    """The spec name is for humans browsing `tester api-list`. Long
    UUID-laden paths get truncated with `...` in the middle."""
    har = _har(
        _entry(
            method="GET",
            url="https://x/api/v1/orgs/a-very-long-uuid-here-abc-123/projects/another-long-id-xyz/items",
        )
    )
    name = import_har(har)[0]["name"]
    assert "..." in name


def test_summary_counts_filtered_entries():
    har = _har(
        _entry(method="GET", url="https://x/a"),
        _entry(method="POST", url="https://x/b"),
        _entry(method="DELETE", url="https://x/c"),
    )
    result = import_har(har, include_methods=["GET"])
    summary = import_summary(har, result)
    assert summary == {"total_entries": 3, "kept": 1}


def test_url_userinfo_stripped_from_persisted_url():
    """Regression: URLs with `user:password@host` would otherwise be
    written verbatim to the spec file, leaking creds. Strip the
    userinfo at import time — same posture as the sensitive-header
    drop. (CodeRabbit critical finding on PR #137.)"""
    har = _har(
        _entry(
            method="GET",
            url="https://admin:hunter2@api.example.com/v1/users",
        )
    )
    params = import_har(har)[0]
    assert "admin" not in params["url"]
    assert "hunter2" not in params["url"]
    assert params["url"] == "https://api.example.com/v1/users"


def test_url_port_preserved():
    """Stripping userinfo must not also strip the port."""
    har = _har(_entry(url="https://api.example.com:8443/v1/x"))
    params = import_har(har)[0]
    assert params["url"] == "https://api.example.com:8443/v1/x"


def test_non_http_schemes_rejected():
    """HAR captures can include ws://, file://, chrome-extension://,
    etc. None of those make sense as API regression specs."""
    har = _har(
        _entry(url="https://api.example.com/v1/keep"),
        _entry(url="ws://realtime.example.com/socket"),
        _entry(url="file:///etc/passwd"),
        _entry(url="chrome-extension://abc/popup.html"),
    )
    result = import_har(har)
    assert len(result) == 1
    assert result[0]["url"].endswith("/v1/keep")


def test_looks_like_har_positive_negative():
    assert looks_like_har('{"log": {"entries": []}}') is True
    assert looks_like_har('{"random": "json"}') is False
    assert looks_like_har('not even json') is False


def test_looks_like_har_accepts_bare_entries_form():
    """`{"entries": [...]}` without the `log` wrapper — some HAR-
    adjacent tools (curl --har style) emit this. The importer
    accepts it, so the sigil-check must too."""
    assert looks_like_har('{"entries": [{"request": {}}]}') is True


def test_non_dict_input_safe():
    """Importer shouldn't crash on garbage input — return empty."""
    for bad in [None, [], "", 42, "garbage"]:
        assert import_har(bad) == []  # type: ignore[arg-type]
