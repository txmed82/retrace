"""P1.4 — HAR (HTTP Archive) → API test spec import.

`retrace tester record --har capture.har` is the "click around and
save" path. Users open DevTools → Network → Save all as HAR → run
the importer; one APITestSpec is created per matching request.

This module is the pure parsing layer: it does no I/O. It takes a
HAR dict and filter knobs, returns a list of "spec params" dicts in
the shape that `api_testing.create_api_spec(...)` accepts. The CLI
glue in `commands/tester.py` calls this and persists each one.

Why HAR import vs an interception proxy / browser-harness hook:
  - HAR is a W3C-ish format every browser exports natively.
  - No proxy means no TLS-MITM, no install ceremony, no setup tax.
  - The recording flow is "do the thing in your browser the way you
    normally would, then save the network log." That's the smallest
    onboarding step.

Filter knobs (all optional):
  - `include_hosts`: keep only entries whose URL hostname is in this
    set. Wildcards via glob (`*.staging.example.com`).
  - `include_methods`: keep only methods in this set (case-insensitive).
  - `exclude_paths`: drop entries whose URL path matches any of these
    globs. Useful for `*.js`, `*.css`, `/metrics`, etc.

Header handling: HAR captures real headers including `Authorization`,
`Cookie`, etc. We DO NOT pass those through to the spec — the spec
validator rejects sensitive headers, and committing real tokens to a
spec file is a credential leak. Sensitive headers are dropped at
import time; users should re-attach them via an `env_profile` /
`headers_env` / `auth_profile`.
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse


# Headers that must not be persisted as static headers on a spec.
# Kept in sync with `api_testing.SENSITIVE_HEADER_NAMES` — duplicating
# the list intentionally so this module doesn't need to import from
# api_testing (avoids a circular import in the CLI).
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
    }
)

# Headers that are noise in a saved spec — every browser sends them,
# but pinning them in a spec makes the spec brittle (UA strings change,
# Accept-Encoding negotiation is the server's problem, etc.).
_NOISY_HEADER_NAMES = frozenset(
    {
        "user-agent",
        "accept-encoding",
        "accept-language",
        "host",
        "connection",
        "referer",
        "origin",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-fetch-dest",
        "sec-fetch-user",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "upgrade-insecure-requests",
        "dnt",
        "pragma",
        "cache-control",
        "if-none-match",
        "if-modified-since",
        "content-length",
    }
)


def import_har(
    har: dict[str, Any],
    *,
    include_hosts: Iterable[str] = (),
    include_methods: Iterable[str] = (),
    exclude_paths: Iterable[str] = (),
    env_profile: str = "",
    name_prefix: str = "",
) -> list[dict[str, Any]]:
    """Parse a HAR dict into a list of spec-param dicts.

    Each result dict is ready to splat into
    `api_testing.create_api_spec(specs_dir=..., **result)`. The
    caller is responsible for persistence.

    Filter semantics:
      - If `include_hosts` is non-empty, an entry is kept only when
        its URL hostname matches one of the globs. Empty = no host
        filter (keep all).
      - If `include_methods` is non-empty, the method must match
        (case-insensitive). Empty = all methods.
      - `exclude_paths` is always applied: any matching path is
        dropped, regardless of include filters.
    """
    entries = _safe_entries(har)
    inc_hosts = [h.strip().lower() for h in include_hosts if str(h).strip()]
    inc_methods = {m.strip().upper() for m in include_methods if str(m).strip()}
    exc_paths = [p.strip() for p in exclude_paths if str(p).strip()]

    out: list[dict[str, Any]] = []
    for entry in entries:
        params = _entry_to_spec_params(
            entry,
            inc_hosts=inc_hosts,
            inc_methods=inc_methods,
            exc_paths=exc_paths,
            env_profile=env_profile.strip(),
            name_prefix=name_prefix.strip(),
        )
        if params is not None:
            out.append(params)
    return out


def _safe_entries(har: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull `log.entries` out of a HAR dict defensively.

    Real-world HAR files vary: some put `entries` at the top level,
    some nest under `log`. We accept either."""
    if not isinstance(har, dict):
        return []
    log = har.get("log")
    if isinstance(log, dict) and isinstance(log.get("entries"), list):
        return [e for e in log["entries"] if isinstance(e, dict)]
    if isinstance(har.get("entries"), list):
        return [e for e in har["entries"] if isinstance(e, dict)]
    return []


def _entry_to_spec_params(
    entry: dict[str, Any],
    *,
    inc_hosts: list[str],
    inc_methods: set[str],
    exc_paths: list[str],
    env_profile: str,
    name_prefix: str,
) -> dict[str, Any] | None:
    request = entry.get("request") or {}
    if not isinstance(request, dict):
        return None
    url = str(request.get("url") or "").strip()
    method = str(request.get("method") or "").strip().upper()
    if not url or not method:
        return None
    parsed = _parse_url(url)
    if parsed is None:
        return None
    host = parsed.hostname or ""
    path = parsed.path or "/"

    if inc_methods and method not in inc_methods:
        return None
    if inc_hosts and not _host_matches(host, inc_hosts):
        return None
    if exc_paths and any(fnmatch.fnmatchcase(path, glob) for glob in exc_paths):
        return None
    if method not in _SUPPORTED_METHODS:
        return None

    headers = _safe_name_value_pairs(request.get("headers") or [])
    headers = _clean_headers(headers)

    query = _safe_name_value_pairs(request.get("queryString") or [])

    body, body_mime = _safe_post_data(request.get("postData") or {})

    response = entry.get("response") or {}
    expected_status = _safe_int(response.get("status"), default=200)

    name = _spec_name(method=method, path=path, name_prefix=name_prefix)

    params: dict[str, Any] = {
        "name": name,
        "method": method,
        "url": _bare_url(parsed),
        "query": query,
        "headers": headers,
        "body": body,
        "expected_status": expected_status,
    }
    if env_profile:
        params["env_profile"] = env_profile
    if body_mime and "content-type" not in {k.lower() for k in headers}:
        # If the request had a body with a known mime, persist the
        # content-type so the spec replays with the right shape. Skip
        # if the user already kept a Content-Type header.
        params["headers"] = {**headers, "Content-Type": body_mime}
    return params


_SUPPORTED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_SUPPORTED_SCHEMES = frozenset({"http", "https"})


def _parse_url(url: str):
    """Parse the URL and reject anything that isn't a normal http(s)
    request. HAR files can capture `ws://`, `file://`, `chrome-
    extension://`, etc.; none of those make sense as API test specs."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme not in _SUPPORTED_SCHEMES:
        return None
    if not parsed.hostname:
        return None
    return parsed


def _bare_url(parsed) -> str:
    """Return the URL without query string OR userinfo.

    `parsed.netloc` for a URL like `https://admin:hunter2@api.x.com/y`
    is `admin:hunter2@api.x.com` — persisting that into a spec file
    leaks credentials. Rebuild from `hostname` + `port` so the spec
    only carries the safe pieces. The `query` is stored separately on
    the spec, so we also drop it here."""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    return f"{parsed.scheme}://{host}{port}{path}"


def _host_matches(host: str, globs: list[str]) -> bool:
    host = host.lower()
    return any(fnmatch.fnmatchcase(host, glob) for glob in globs)


def _safe_name_value_pairs(items: list[Any]) -> dict[str, str]:
    """HAR represents headers / query as `[{"name": ..., "value": ...}]`.
    Convert to dict; on duplicate names, last wins (matches browser
    behavior for headers; queries with repeated keys we collapse
    intentionally — specs don't model repeated query keys today)."""
    out: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = str(item.get("value") or "")
        out[name] = value
    return out


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop sensitive headers (would leak credentials into a spec
    file) and browser-noise headers (would make specs brittle)."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        lk = key.lower().strip()
        if not lk or lk.startswith(":"):  # HTTP/2 pseudo-headers
            continue
        if lk in _SENSITIVE_HEADER_NAMES:
            continue
        if lk in _NOISY_HEADER_NAMES:
            continue
        out[key] = value
    return out


def _safe_post_data(post_data: dict[str, Any]) -> tuple[Any, str]:
    """Extract a request body from HAR `postData`. Returns
    `(body, mime)` or `(None, "")` if there's nothing usable.

    HAR puts the body in `postData.text` (string) or a multipart
    `params` array. We only handle text bodies; multipart and binary
    are skipped (a spec that re-uploads a file is out of scope
    today — those rarely belong in API regression tests anyway)."""
    if not isinstance(post_data, dict):
        return None, ""
    mime = str(post_data.get("mimeType") or "").split(";")[0].strip()
    text = post_data.get("text")
    if text in (None, ""):
        return None, ""
    if not isinstance(text, str):
        return None, ""
    # Try to parse JSON bodies into structured form so the spec
    # diffs cleanly in git.
    if mime in {"application/json", "text/json"} or _looks_like_json(text):
        try:
            return json.loads(text), mime or "application/json"
        except (ValueError, TypeError):
            return text, mime or "application/json"
    return text, mime


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and stripped[0] in "{["


def _safe_int(value: Any, *, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _spec_name(*, method: str, path: str, name_prefix: str) -> str:
    """Build a spec name from method + URL path. Stable so importing
    the same HAR twice yields the same names (different spec_ids —
    those embed a uuid suffix in `create_api_spec`)."""
    short_path = _short_path(path)
    base = f"{method} {short_path}".strip()
    if name_prefix:
        return f"{name_prefix.rstrip()} {base}".strip()
    return base


def _short_path(path: str) -> str:
    """Compact long paths so the spec list is readable.
    `/api/v1/users/abc-123/posts/456` → `/api/v1/users/.../posts/...`."""
    if len(path) <= 60:
        return path
    parts = [p for p in path.split("/") if p]
    if len(parts) <= 4:
        return path
    return "/" + "/".join(parts[:2] + ["..."] + parts[-2:])


# Useful for the CLI to surface "we filtered N entries, kept M".
def import_summary(har: dict[str, Any], result: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total_entries": len(_safe_entries(har)),
        "kept": len(result),
    }


__all__ = ["import_har", "import_summary", "looks_like_har"]


# Loose regex for HAR sanity-check, used by the CLI when reading a
# file. Match either the standard `log: { entries: [...] }` envelope
# OR the bare `entries: [...]` form that the importer also accepts
# (curl --har, some test fixtures). Keep this lenient — false-negatives
# block valid input; false-positives are fine since the importer
# returns an empty list for non-HAR JSON anyway.
_HAR_FILE_HINT = re.compile(r'"(log|entries)"\s*:\s*[\{\[]', re.IGNORECASE)


def looks_like_har(text: str) -> bool:
    return bool(_HAR_FILE_HINT.search(text))
