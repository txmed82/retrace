from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


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
    repo_path: Path, file_path: Path, terms: list[str]
) -> tuple[float, str]:
    rel = file_path.relative_to(repo_path).as_posix()
    score = 0.0
    hits: list[str] = []

    rel_l = rel.lower()
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
        return 0.0, ""
    text_l = text.lower()
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
    return score, ", ".join(hits[:6])


def score_repo_for_finding(
    *,
    repo_path: Path,
    title: str,
    category: str,
    evidence_text: str,
    top_n: int = 8,
) -> list[CodeCandidate]:
    terms = _keywords(title, category, evidence_text)
    scored: list[CodeCandidate] = []
    for p in _iter_source_files(repo_path):
        s, rationale = _score_file(repo_path, p, terms)
        if s <= 0:
            continue
        scored.append(
            CodeCandidate(
                file_path=p.relative_to(repo_path).as_posix(),
                score=round(s, 2),
                rationale=rationale or "keyword overlap",
            )
        )
    scored.sort(key=lambda c: (-c.score, c.file_path))
    return scored[:top_n]