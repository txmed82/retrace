from __future__ import annotations

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


def _routes_from_source(repo_path: Path) -> list[RouteDefinition]:
    routes: list[RouteDefinition] = []
    for path in repo_path.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        parts = path.relative_to(repo_path).parts
        if ".git" in parts or "node_modules" in parts or "dist" in parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(repo_path).as_posix()
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
