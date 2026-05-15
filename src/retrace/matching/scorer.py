from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from retrace.llm.client import LLMClient
from retrace.matching.routes import RouteDefinition, load_route_manifest, route_matches
from retrace.matching.sourcemaps import load_source_maps, map_stack_paths


_TEXT_EXTS = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".py",
    ".go",
    ".java",
    ".rb",
    ".php",
}

_DIR_BONUS = {
    "client/src/pages": 2.5,
    "client/src/components": 2.0,
    "server/routes": 2.0,
    "server": 1.0,
}

_STOPWORDS = {
    "the",
    "and",
    "with",
    "from",
    "that",
    "this",
    "what",
    "happened",
    "likely",
    "cause",
    "reproduction",
    "navigate",
    "observe",
    "user",
    "users",
    "page",
    "pages",
    "element",
    "elements",
    "session",
    "sample",
    "category",
    "confidence",
    "signals",
    "high",
    "medium",
    "low",
    "critical",
    "functional",
    "error",
    "https",
    "http",
    "www",
    "com",
}


@dataclass(frozen=True)
class CodeCandidate:
    file_path: str
    score: float
    rationale: str
    symbol: str | None = None
    owners: list[str] = field(default_factory=list)


def _tokenize(text: str) -> list[str]:
    cleaned = re.sub(r"https?://\S+", " ", text.lower())
    toks = [t for t in re.split(r"[^a-z0-9]+", cleaned) if len(t) >= 4]
    return [t for t in toks if t not in _STOPWORDS]


def _keywords(title: str, category: str, evidence_text: str) -> list[str]:
    base = _tokenize(" ".join([title, category]))
    evidence_l = evidence_text.lower()
    extras = []
    if "store" in base or "store" in evidence_l:
        extras += ["store", "checkout", "payment", "purchase", "product"]
    if (
        "click" in base
        or "unresponsive" in base
        or "rage" in base
        or "click" in evidence_l
    ):
        extras += ["click", "button", "onClick", "pointer", "disabled"]
    if "home" in base or "homepage" in base or "homepage" in evidence_l:
        extras += ["home", "landing", "cta", "hero"]
    out = []
    seen: set[str] = set()
    for k in base + extras:
        if k not in seen:
            out.append(k)
            seen.add(k)
    return out[:40]


def _stack_frame_paths(evidence_text: str) -> list[str]:
    paths: list[str] = []
    for match in re.finditer(
        r"((?:client|server|shared|src)/[A-Za-z0-9_./-]+\.(?:tsx?|jsx?|py|go|java|rb|php))(?::\d+)?(?::\d+)?",
        evidence_text,
    ):
        value = match.group(1).strip()
        if value not in paths:
            paths.append(value)
    return paths[:10]


def _api_routes(evidence_text: str) -> list[str]:
    routes: list[str] = []
    for match in re.finditer(
        r"(?:\b(?:GET|POST|PUT|PATCH|DELETE)\b\s*)?(/api/[A-Za-z0-9_./:-]+)",
        evidence_text,
    ):
        route = match.group(1).rstrip(".,);")
        if route not in routes:
            routes.append(route)
    return routes[:10]


def _api_methods(evidence_text: str) -> dict[str, str]:
    methods: dict[str, str] = {}
    for match in re.finditer(
        r"\b(GET|POST|PUT|PATCH|DELETE)\b\s+(/api/[A-Za-z0-9_./:-]+)",
        evidence_text,
        re.IGNORECASE,
    ):
        methods[match.group(2).rstrip(".,);")] = match.group(1).upper()
    return methods


def _is_dynamic_route_segment(segment: str) -> bool:
    value = segment.strip().lower()
    if not value:
        return True
    if value.isdigit() or value.startswith(":"):
        return True
    if value.startswith("[") and value.endswith("]"):
        return True
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value,
    ):
        return True
    if re.fullmatch(r"[a-z]{2,8}_[a-z0-9]{6,}", value):
        return True
    if re.fullmatch(r"[0-9a-f]{12,}", value):
        return True
    if re.fullmatch(r"(?=.*[a-z])(?=.*\d)[a-z0-9_-]{10,}", value):
        return True
    return bool(re.fullmatch(r"[a-z]{1,4}\d{2,}", value))


def _iter_source_files(repo_path: Path) -> list[Path]:
    files: list[Path] = []
    allow_prefixes = ("client/", "server/", "shared/", "src/")
    for p in repo_path.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(repo_path).as_posix()
        if not rel.startswith(allow_prefixes):
            continue
        # Use repo-relative path parts for filtering
        try:
            rel_path = p.relative_to(repo_path)
            parts_to_check = rel_path.parts
        except ValueError:
            # Fall back to absolute path parts if not under repo_path
            parts_to_check = p.parts
        if (
            ".git" in parts_to_check
            or "node_modules" in parts_to_check
            or "dist" in parts_to_check
        ):
            continue
        if any(part.startswith(".") for part in parts_to_check):
            continue
        if p.suffix.lower() not in _TEXT_EXTS:
            continue
        files.append(p)
    return files


def _score_file(
    repo_path: Path,
    file_path: Path,
    terms: list[str],
    *,
    stack_paths: list[str],
    api_routes: list[str],
    api_methods: dict[str, str],
    route_manifest: list[RouteDefinition],
    churn_scores: dict[str, float],
) -> tuple[float, str, str | None]:
    rel = file_path.relative_to(repo_path).as_posix()
    score = 0.0
    hits: list[str] = []

    rel_l = rel.lower()
    for stack_path in stack_paths:
        stack_l = stack_path.lower()
        if rel_l == stack_l or rel_l.endswith("/" + stack_l):
            score += 45.0
            hits.append(f"stack_frame:{stack_path}")
        elif file_path.name.lower() == Path(stack_path).name.lower():
            score += 12.0
            hits.append(f"stack_name:{Path(stack_path).name}")

    for d, bonus in _DIR_BONUS.items():
        if rel_l.startswith(d):
            score += bonus
            hits.append(f"dir:{d}")

    name_l = file_path.name.lower()
    for t in terms:
        if t in rel_l:
            score += 1.6
            hits.append(f"path:{t}")
        if t in name_l:
            score += 1.0

    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0.0, "", None
    text_l = text.lower()
    server_route_file = rel_l.startswith(
        (
            "server/",
            "src/server/",
            "src/routes/",
            "src/controllers/",
            "src/api/",
            "src/pages/api/",
            "src/app/api/",
        )
    )
    for route in api_routes:
        route_l = route.lower()
        method = api_methods.get(route, "")
        for definition in route_manifest:
            if definition.file_path == rel and route_matches(definition, route, method):
                score += 28.0
                hits.append(f"route_manifest:{route}")
        if route_l in text_l:
            route_bonus = 14.0 if server_route_file else 5.0
            score += route_bonus
            hits.append(f"api_route:{route}")
        route_parts = [p for p in route_l.split("/") if p and p != "api"]
        stable_route_parts = [
            part
            for part in route_parts
            if not _is_dynamic_route_segment(part)
        ]
        if server_route_file and stable_route_parts:
            stable_hits = sum(1 for part in stable_route_parts if part in text_l)
            if stable_hits == len(stable_route_parts):
                score += 10.0
                hits.append(f"api_route_pattern:{route}")
        if server_route_file:
            overlap = sum(1 for part in route_parts if part in rel_l)
            if overlap:
                score += min(6.0, overlap * 2.0)
                hits.append("api_route_path")
    for t in terms:
        count = text_l.count(t)
        if count:
            score += min(4.0, 0.35 * count)

    if "onclick" in text_l and ("click" in terms or "button" in terms):
        score += 1.4
        hits.append("contains:onClick")
    if "/api/store/" in text_l and "store" in terms:
        score += 1.2
        hits.append("contains:/api/store/*")
    if "store" in terms:
        if "/store" in rel_l or "store" in rel_l:
            score += 5.0
            hits.append("domain:store")
        if rel_l.endswith("store.tsx") or rel_l.endswith("store.ts"):
            score += 3.5
            hits.append("page:store")
        if "admin" in rel_l:
            score -= 2.0
    if "home" in terms or "homepage" in terms:
        if "/home" in rel_l or rel_l.endswith("home.tsx"):
            score += 12.0
            hits.append("domain:home")
        if "admin" in rel_l:
            score -= 1.5
    if "admin" not in terms and "admin" in rel_l:
        score -= 4.0
    if rel in churn_scores:
        score += churn_scores[rel]
        hits.append("recent_churn")
    return score, ", ".join(hits[:8]), _best_symbol(text, terms, api_routes)


def _best_symbol(text: str, terms: list[str], api_routes: list[str]) -> str | None:
    symbols = _extract_symbols(text)
    if not symbols:
        return None
    haystack = " ".join([*terms, *api_routes]).lower()
    for symbol in symbols:
        if symbol.lower() in haystack:
            return symbol
    return symbols[0]


def _extract_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    patterns = [
        r"\bexport\s+default\s+function\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        r"\bexport\s+(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        r"\bclass\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = match.group(1)
            if value not in symbols:
                symbols.append(value)
    return symbols[:8]


def _load_codeowners(repo_path: Path) -> list[tuple[str, list[str]]]:
    for rel in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
        path = repo_path / rel
        if not path.exists():
            continue
        owners: list[tuple[str, list[str]]] = []
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                owners.append((parts[0], parts[1:]))
        return owners
    return []


def _owners_for_path(rel: str, rules: list[tuple[str, list[str]]]) -> list[str]:
    matched: list[str] = []
    for pattern, owners in rules:
        clean = pattern.lstrip("/")
        if _codeowners_pattern_matches(clean, rel):
            matched = owners
    return matched


def _codeowners_pattern_matches(pattern: str, rel: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("/"):
        return rel.startswith(pattern)
    if "/" not in pattern:
        return fnmatch.fnmatch(Path(rel).name, pattern) or fnmatch.fnmatch(rel, pattern)
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, f"{pattern.rstrip('/')}/*")


def _recent_churn_scores(repo_path: Path) -> dict[str, float]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "log", "--since=90 days ago", "--name-only", "--format="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    counts: dict[str, int] = {}
    for raw in result.stdout.splitlines():
        rel = raw.strip()
        if rel:
            counts[rel] = counts.get(rel, 0) + 1
    return {rel: min(3.0, 0.4 * count) for rel, count in counts.items()}


def score_repo_for_finding(
    *,
    repo_path: Path,
    title: str,
    category: str,
    evidence_text: str,
    top_n: int = 8,
    llm: Optional[LLMClient] = None,
) -> list[CodeCandidate]:
    terms = _keywords(title, category, evidence_text)
    stack_paths = _stack_frame_paths(evidence_text)
    source_maps = load_source_maps(repo_path)
    mapped_stack_paths = map_stack_paths(evidence_text, source_maps=source_maps)
    stack_paths = [*stack_paths, *mapped_stack_paths]
    api_routes = _api_routes(evidence_text)
    api_methods = _api_methods(evidence_text)
    route_manifest = load_route_manifest(repo_path)
    codeowners = _load_codeowners(repo_path)
    churn_scores = _recent_churn_scores(repo_path)
    scored: list[CodeCandidate] = []
    for p in _iter_source_files(repo_path):
        s, rationale, symbol = _score_file(
            repo_path,
            p,
            terms,
            stack_paths=stack_paths,
            api_routes=api_routes,
            api_methods=api_methods,
            route_manifest=route_manifest,
            churn_scores=churn_scores,
        )
        if s <= 0:
            continue
        rel = p.relative_to(repo_path).as_posix()
        owners = _owners_for_path(rel, codeowners)
        if owners:
            rationale = f"{rationale}, codeowners:{' '.join(owners)}" if rationale else f"codeowners:{' '.join(owners)}"
        scored.append(
            CodeCandidate(
                file_path=rel,
                score=round(s, 2),
                rationale=rationale or "keyword overlap",
                symbol=symbol,
                owners=owners,
            )
        )
    scored.sort(key=lambda c: (-c.score, c.file_path))

    if llm is not None:
        from retrace.matching.ai_scorer import rerank_candidates_with_ai

        ai_shortlist_size = max(top_n, 20)
        shortlist = scored[:ai_shortlist_size]
        return rerank_candidates_with_ai(
            llm=llm,
            candidates=shortlist,
            title=title,
            category=category,
            evidence_text=evidence_text,
            top_n=top_n,
        )

    return scored[:top_n]
