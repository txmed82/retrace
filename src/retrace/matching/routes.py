from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RouteDefinition:
    route: str
    file_path: str
    method: str = ""
    source: str = "source_scan"


_ROUTE_CALL_RE = re.compile(
    r"\b(?:router|app)\.(get|post|put|patch|delete|all)\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_NEXT_HANDLER_RE = re.compile(r"\bexport\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE)\b")

# Python frameworks
#
# FastAPI / Flask: `@app.get("/api/x")`, `@router.post("/api/x")`,
# `@blueprint.put("/api/x")`. Same shape as JS, captured separately so
# we can pre-filter by extension.
_PY_ROUTE_DECORATOR_RE = re.compile(
    r"@\s*(?:app|router|blueprint|bp)\s*\.\s*(get|post|put|patch|delete|head|options|websocket)\s*\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
# Flask classic: `@app.route("/api/x", methods=["POST"])` (with optional methods).
_PY_FLASK_ROUTE_RE = re.compile(
    r"@\s*(?:app|router|blueprint|bp)\s*\.\s*route\s*\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*methods\s*=\s*\[([^\]]*)\])?",
    re.IGNORECASE,
)
# Django urls.py: `path("api/x", ...)` or `re_path(r"^api/x$", ...)`.
_PY_DJANGO_PATH_RE = re.compile(
    r"\b(?:path|re_path)\s*\(\s*[rR]?['\"]([^'\"]+)['\"]",
)

# Ruby on Rails routes.rb: `get '/api/x'`, `post '/api/x', to: '...'`,
# `resources :foo`, etc. We catch the verb forms; `resources`/`scope`
# would need a much richer parser.
_RB_RAILS_VERB_RE = re.compile(
    r"^\s*(get|post|put|patch|delete|match)\s+['\"]([^'\"]+)['\"]",
    re.MULTILINE | re.IGNORECASE,
)


def load_route_manifest(repo_path: Path) -> list[RouteDefinition]:
    routes: list[RouteDefinition] = []
    routes.extend(_routes_from_framework_manifests(repo_path))
    routes.extend(_routes_from_source(repo_path))
    out: list[RouteDefinition] = []
    seen: set[tuple[str, str, str]] = set()
    for route in routes:
        key = (route.method.upper(), route.route, route.file_path)
        if route.route.startswith("/api/") and key not in seen:
            seen.add(key)
            out.append(route)
    return out


def route_matches(definition: RouteDefinition, route: str, method: str = "") -> bool:
    if method and definition.method and method.upper() != definition.method.upper():
        return False
    return _route_pattern(definition.route).fullmatch(route.rstrip("/")) is not None


def _routes_from_framework_manifests(repo_path: Path) -> list[RouteDefinition]:
    candidates = [
        repo_path / ".next/server/app-paths-manifest.json",
        repo_path / ".next/server/pages-manifest.json",
        repo_path / ".next/server/middleware-manifest.json",
        repo_path / "route-manifest.json",
        repo_path / "routes-manifest.json",
    ]
    routes: list[RouteDefinition] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for route, target in _manifest_routes(data):
            source_file = _source_file_for_manifest_target(repo_path, str(target))
            routes.append(
                RouteDefinition(
                    route=_normalize_route(route),
                    file_path=source_file,
                    source=path.relative_to(repo_path).as_posix(),
                )
            )
    return routes


def _manifest_routes(data: object) -> list[tuple[str, str]]:
    if isinstance(data, dict):
        if isinstance(data.get("sortedMiddleware"), list):
            return []
        routes: list[tuple[str, str]] = []
        for key, value in data.items():
            if isinstance(value, str) and str(key).startswith("/"):
                routes.append((str(key), value))
            elif isinstance(value, dict):
                route = str(value.get("route") or value.get("page") or key)
                target = str(
                    value.get("file")
                    or value.get("src")
                    or value.get("page")
                    or value.get("name")
                    or ""
                )
                if route.startswith("/") and target:
                    routes.append((route, target))
        return routes
    if isinstance(data, list):
        routes = []
        for item in data:
            if not isinstance(item, dict):
                continue
            route = str(item.get("route") or item.get("path") or "")
            target = str(item.get("file") or item.get("src") or item.get("handler") or "")
            if route.startswith("/") and target:
                routes.append((route, target))
        return routes
    return []


def _source_file_for_manifest_target(repo_path: Path, target: str) -> str:
    clean = target.lstrip("./")
    possible = [clean]
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        possible.append(str(Path(clean).with_suffix(ext)))
    if clean.startswith("app/") or clean.startswith("pages/"):
        possible.extend(f"src/{item}" for item in list(possible))
    for item in possible:
        if (repo_path / item).exists():
            return item
    return possible[-1] if possible else clean


_SCANNABLE_EXTS = {
    ".ts", ".tsx", ".js", ".jsx",
    ".py",   # FastAPI / Flask / Django
    ".rb",   # Rails
}

_SKIP_DIR_PARTS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".tox",
    ".pytest_cache",
    "site-packages",
    "vendor",         # ruby vendor/
    ".bundle",
}


def _routes_from_source(repo_path: Path) -> list[RouteDefinition]:
    routes: list[RouteDefinition] = []
    for path in repo_path.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _SCANNABLE_EXTS:
            continue
        parts = path.relative_to(repo_path).parts
        if any(part in _SKIP_DIR_PARTS for part in parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(repo_path).as_posix()
        suffix = path.suffix.lower()

        if suffix in {".ts", ".tsx", ".js", ".jsx"}:
            for match in _ROUTE_CALL_RE.finditer(text):
                routes.append(
                    RouteDefinition(
                        method=match.group(1).upper(),
                        route=_normalize_route(match.group(2)),
                        file_path=rel,
                    )
                )
            if "/api/" in rel or rel.startswith(("app/api/", "src/app/api/")):
                route = _next_api_route_from_path(rel)
                if route:
                    for match in _NEXT_HANDLER_RE.finditer(text):
                        routes.append(
                            RouteDefinition(
                                method=match.group(1).upper(),
                                route=route,
                                file_path=rel,
                                source="next_app_route",
                            )
                        )

        elif suffix == ".py":
            # FastAPI / Flask decorator form: @app.get("/api/x")
            for match in _PY_ROUTE_DECORATOR_RE.finditer(text):
                routes.append(
                    RouteDefinition(
                        method=match.group(1).upper(),
                        route=_normalize_route(match.group(2)),
                        file_path=rel,
                        source="python_decorator",
                    )
                )
            # Flask classic: @app.route("/api/x", methods=["POST"]) — emit
            # one route per declared method; default to GET when methods
            # isn't supplied (Flask's documented default).
            for match in _PY_FLASK_ROUTE_RE.finditer(text):
                methods_blob = (match.group(2) or "").strip()
                if methods_blob:
                    methods = [
                        m.strip().strip("'\"").upper()
                        for m in methods_blob.split(",")
                        if m.strip().strip("'\"")
                    ]
                else:
                    methods = ["GET"]
                for verb in methods:
                    routes.append(
                        RouteDefinition(
                            method=verb,
                            route=_normalize_route(match.group(1)),
                            file_path=rel,
                            source="flask_route",
                        )
                    )
            # Django: urlpatterns = [path("api/x", ...), re_path(r"^api/x$", ...)]
            # Match only inside files literally named urls.py — using
            # endswith() would also accept `admin_urls.py` etc.
            if path.name == "urls.py":
                for match in _PY_DJANGO_PATH_RE.finditer(text):
                    normalized = _normalize_django_route(match.group(1))
                    routes.append(
                        RouteDefinition(
                            method="",  # django paths don't carry verb info
                            route=normalized,
                            file_path=rel,
                            source="django_urlconf",
                        )
                    )

        elif suffix == ".rb":
            # Rails routes.rb — basename check (not endswith) so a file
            # named `myroutes.rb` doesn't masquerade as a route file.
            if path.name == "routes.rb":
                for match in _RB_RAILS_VERB_RE.finditer(text):
                    verb = match.group(1).upper()
                    if verb == "MATCH":
                        verb = ""  # match supports multiple verbs; leave blank.
                    routes.append(
                        RouteDefinition(
                            method=verb,
                            route=_normalize_route(match.group(2)),
                            file_path=rel,
                            source="rails_routes",
                        )
                    )

    return routes


def _next_api_route_from_path(rel: str) -> str:
    parts = list(Path(rel).parts)
    try:
        idx = parts.index("api")
    except ValueError:
        return ""
    route_parts = parts[idx:]
    if route_parts and route_parts[-1].split(".", 1)[0] in {"route", "index"}:
        route_parts = route_parts[:-1]
    else:
        route_parts[-1] = route_parts[-1].split(".", 1)[0]
    return "/" + "/".join(route_parts)


def _normalize_route(route: str) -> str:
    clean = route.strip().split("?", 1)[0].rstrip("/")
    return clean or "/"


def _normalize_django_route(raw_route: str) -> str:
    """Project Django path/re_path syntax onto the route shape `route_matches`
    understands (`:id` placeholders).

    Examples:
      * `path("api/users/<int:pk>", ...)`         -> `/api/users/:pk`
      * `path("api/users/<slug:name>", ...)`      -> `/api/users/:name`
      * `re_path(r"^api/legacy/(?P<id>[0-9]+)$")` -> `/api/legacy/:id`

    Without this, the manifest entry would still contain Django's raw
    `<int:pk>` form, and `route_matches("/api/users/42")` would never
    match — breaking route-to-file ownership for any dynamic Django
    endpoint.
    """
    cleaned = raw_route.lstrip("^").rstrip("$")
    # Order matters: `(?P<id>[0-9]+)` first so the surrounding parens
    # and the `?` are consumed in one shot. Doing the bare `<id>`
    # substitution first would leave a stray `?` that
    # `_normalize_route`'s query-string strip then eats.
    cleaned = re.sub(r"\(\?P<([^>]+)>[^)]*\)", r":\1", cleaned)
    # `<int:pk>` / `<pk>` / `<slug:name>` -> `:pk` / `:name`
    cleaned = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r":\1", cleaned)
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return _normalize_route(cleaned)


@functools.lru_cache(maxsize=256)
def _route_pattern(route: str) -> re.Pattern[str]:
    parts = []
    for segment in route.rstrip("/").split("/"):
        if not segment:
            continue
        if segment.startswith(":") or (segment.startswith("[") and segment.endswith("]")):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(segment))
    return re.compile("/" + "/".join(parts))
