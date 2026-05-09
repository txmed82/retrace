from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from retrace.matching.routes import RouteDefinition, load_route_manifest
from retrace.storage import FailureRow, FailureTestLinkRow, Storage


_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,\d+)? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@")
_ROUTE_CALL_RE = re.compile(
    r"\b(?:router|app)\.(get|post|put|patch|delete|all)\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
_API_PATH_RE = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)?\s*(/api/[A-Za-z0-9_./:[\]-]+)")
_FILE_REF_RE = re.compile(
    r"\b((?:app|client|server|shared|src|pages)/[A-Za-z0-9_./[\]-]+\.(?:tsx?|jsx?|py|go|java|rb|php))\b"
)


@dataclass(frozen=True)
class DiffHunk:
    file_path: str
    new_start: int
    new_count: int
    added_lines: list[str] = field(default_factory=list)
    added_line_numbers: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChangedFile:
    path: str
    hunks: list[DiffHunk] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "hunks": [h.to_dict() for h in self.hunks]}


@dataclass(frozen=True)
class AffectedFlow:
    kind: str
    name: str
    files: list[str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PriorFailureReference:
    failure_id: str
    public_id: str
    title: str
    severity: str
    status: str
    matched_files: list[str]
    matched_flows: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExistingTestRecommendation:
    spec_id: str
    spec_name: str
    spec_path: str
    source: str
    coverage_state: str
    failure_public_id: str
    command: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MissingTestRecommendation:
    kind: str
    flow: str
    files: list[str]
    reason: str
    command: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PRReviewAnalysis:
    changed_files: list[ChangedFile]
    affected_flows: list[AffectedFlow]
    prior_failures: list[PriorFailureReference]
    existing_tests: list[ExistingTestRecommendation]
    missing_tests: list[MissingTestRecommendation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_files": [item.to_dict() for item in self.changed_files],
            "affected_flows": [item.to_dict() for item in self.affected_flows],
            "prior_failures": [item.to_dict() for item in self.prior_failures],
            "existing_tests": [item.to_dict() for item in self.existing_tests],
            "missing_tests": [item.to_dict() for item in self.missing_tests],
        }


@dataclass(frozen=True)
class PRInlineComment:
    path: str
    line: int
    body: str
    side: str = "RIGHT"
    suggestion_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PRReviewCommentPlan:
    summary_body: str
    inline_comments: list[PRInlineComment]
    summary_marker: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_body": self.summary_body,
            "inline_comments": [item.to_dict() for item in self.inline_comments],
            "summary_marker": self.summary_marker,
        }


class PRReviewPublisher(Protocol):
    def upsert_issue_comment(
        self,
        *,
        repo: str,
        number: int,
        marker: str,
        body: str,
    ) -> dict[str, Any]: ...

    def list_pull_request_review_comments(
        self,
        *,
        repo: str,
        number: int,
    ) -> list[dict[str, Any]]: ...

    def create_pull_request_review(
        self,
        *,
        repo: str,
        number: int,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


SUMMARY_MARKER = "<!-- retrace-pr-review-summary -->"
INLINE_MARKER_PREFIX = "<!-- retrace-pr-review-inline:"
INLINE_MARKER_SUFFIX = "-->"


def analyze_pr_diff(
    *,
    diff_text: str,
    repo_path: Path | None = None,
    store: Storage | None = None,
    project_id: str = "",
    environment_id: str = "",
) -> PRReviewAnalysis:
    changed_files = parse_unified_diff(diff_text)
    route_manifest = load_route_manifest(repo_path) if repo_path is not None else []
    affected_flows = infer_affected_flows(
        changed_files=changed_files,
        route_manifest=route_manifest,
    )
    prior_failures: list[PriorFailureReference] = []
    existing_tests: list[ExistingTestRecommendation] = []
    if store is not None and project_id and environment_id:
        prior_failures = link_prior_failures(
            store=store,
            project_id=project_id,
            environment_id=environment_id,
            changed_files=changed_files,
            affected_flows=affected_flows,
        )
        existing_tests = recommend_existing_tests(
            store=store,
            prior_failures=prior_failures,
        )
    missing_tests = recommend_missing_tests(
        affected_flows=affected_flows,
        existing_tests=existing_tests,
    )
    return PRReviewAnalysis(
        changed_files=changed_files,
        affected_flows=affected_flows,
        prior_failures=prior_failures,
        existing_tests=existing_tests,
        missing_tests=missing_tests,
    )


def parse_unified_diff(diff_text: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    current_path = ""
    current_hunks: list[DiffHunk] = []
    current_added: list[str] = []
    current_added_line_numbers: list[int] = []
    current_start = 0
    current_count = 0
    current_new_line = 0

    def flush_hunk() -> None:
        nonlocal current_added, current_added_line_numbers, current_start, current_count
        if current_path and current_start:
            current_hunks.append(
                DiffHunk(
                    file_path=current_path,
                    new_start=current_start,
                    new_count=current_count,
                    added_lines=list(current_added),
                    added_line_numbers=list(current_added_line_numbers),
                )
            )
        current_added = []
        current_added_line_numbers = []
        current_start = 0
        current_count = 0

    def flush_file() -> None:
        nonlocal current_hunks
        flush_hunk()
        if current_path:
            files.append(ChangedFile(path=current_path, hunks=list(current_hunks)))
        current_hunks = []

    for raw_line in diff_text.splitlines():
        file_match = _DIFF_FILE_RE.match(raw_line)
        if file_match:
            flush_file()
            current_path = file_match.group(1).strip()
            continue
        hunk_match = _HUNK_RE.match(raw_line)
        if hunk_match:
            flush_hunk()
            current_start = int(hunk_match.group("new_start"))
            current_count = int(hunk_match.group("new_count") or "1")
            current_new_line = current_start
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            current_added.append(raw_line[1:])
            current_added_line_numbers.append(current_new_line)
            current_new_line += 1
            continue
        if raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        if current_start and not raw_line.startswith("\\"):
            current_new_line += 1
    flush_file()
    return files


def build_pr_review_comment_plan(
    analysis: PRReviewAnalysis,
) -> PRReviewCommentPlan:
    inline_comments: list[PRInlineComment] = []
    for missing in analysis.missing_tests:
        inline = _missing_test_inline_comment(analysis.changed_files, missing)
        if inline:
            inline_comments.append(inline)
    for failure in analysis.prior_failures:
        inline = _prior_failure_inline_comment(
            analysis.changed_files,
            failure,
            analysis.existing_tests,
        )
        if inline:
            inline_comments.append(inline)
    return PRReviewCommentPlan(
        summary_body=_summary_comment_body(analysis),
        inline_comments=inline_comments,
        summary_marker=SUMMARY_MARKER,
    )


def publish_pr_review_comments(
    *,
    client: PRReviewPublisher,
    repo: str,
    pr_number: int,
    commit_id: str,
    analysis: PRReviewAnalysis,
) -> dict[str, int]:
    plan = build_pr_review_comment_plan(analysis)
    client.upsert_issue_comment(
        repo=repo,
        number=pr_number,
        marker=plan.summary_marker,
        body=plan.summary_body,
    )
    existing_bodies = [
        str(comment.get("body") or "")
        for comment in client.list_pull_request_review_comments(
            repo=repo,
            number=pr_number,
        )
    ]
    new_inline = [
        comment
        for comment in plan.inline_comments
        if not any(_inline_marker(comment.suggestion_id) in body for body in existing_bodies)
    ]
    if new_inline:
        client.create_pull_request_review(
            repo=repo,
            number=pr_number,
            commit_id=commit_id,
            body="Retrace QA suggestions for this diff.",
            comments=[
                {
                    "path": comment.path,
                    "line": comment.line,
                    "side": comment.side,
                    "body": comment.body,
                }
                for comment in new_inline
            ],
        )
    return {
        "summary_comments": 1,
        "inline_comments": len(new_inline),
        "skipped_inline_comments": len(plan.inline_comments) - len(new_inline),
    }


def recommended_sandbox_commands(
    analysis: PRReviewAnalysis,
    *,
    include_missing_test_generation: bool = False,
) -> list[str]:
    commands = [test.command for test in analysis.existing_tests]
    if include_missing_test_generation:
        commands.extend(test.command for test in analysis.missing_tests)
    return _unique(commands)


def infer_affected_flows(
    *,
    changed_files: list[ChangedFile],
    route_manifest: list[RouteDefinition] | None = None,
) -> list[AffectedFlow]:
    route_manifest = route_manifest or []
    flows: list[AffectedFlow] = []
    seen: set[tuple[str, str]] = set()
    for changed in changed_files:
        added_text = "\n".join(
            line for hunk in changed.hunks for line in hunk.added_lines
        )
        for route in _routes_for_changed_file(changed.path, added_text, route_manifest):
            _append_flow(
                flows,
                seen,
                AffectedFlow(
                    kind="api" if route.startswith("/api/") else "ui",
                    name=route,
                    files=[changed.path],
                    reason="changed route handler or route manifest entry",
                ),
            )
        component = _component_name(changed.path)
        if component:
            _append_flow(
                flows,
                seen,
                AffectedFlow(
                    kind="component",
                    name=component,
                    files=[changed.path],
                    reason="changed reusable UI component",
                ),
            )
    return flows


def link_prior_failures(
    *,
    store: Storage,
    project_id: str,
    environment_id: str,
    changed_files: list[ChangedFile],
    affected_flows: list[AffectedFlow],
) -> list[PriorFailureReference]:
    changed_paths = {item.path for item in changed_files}
    flow_names = {flow.name for flow in affected_flows}
    references: list[PriorFailureReference] = []
    for failure in store.list_failures(
        project_id=project_id,
        environment_id=environment_id,
        limit=500,
    ):
        failure_files, failure_flows = _failure_context(store, failure)
        matched_files = sorted(changed_paths.intersection(failure_files))
        matched_flows = sorted(flow_names.intersection(failure_flows))
        if not matched_files and not matched_flows:
            continue
        references.append(
            PriorFailureReference(
                failure_id=failure.id,
                public_id=failure.public_id,
                title=failure.title,
                severity=failure.severity,
                status=failure.status,
                matched_files=matched_files,
                matched_flows=matched_flows,
            )
        )
    return references


def recommend_existing_tests(
    *,
    store: Storage,
    prior_failures: list[PriorFailureReference],
) -> list[ExistingTestRecommendation]:
    recommendations: list[ExistingTestRecommendation] = []
    seen: set[str] = set()
    for failure in prior_failures:
        links = store.list_failure_test_links(failure_id=failure.failure_id, limit=50)
        for link in links:
            if link.spec_id in seen:
                continue
            seen.add(link.spec_id)
            recommendations.append(_existing_test_recommendation(link, failure))
    return recommendations


def recommend_missing_tests(
    *,
    affected_flows: list[AffectedFlow],
    existing_tests: list[ExistingTestRecommendation],
) -> list[MissingTestRecommendation]:
    covered = {_normalize_flow_name(test.spec_name) for test in existing_tests}
    covered.update(_normalize_flow_name(test.spec_id) for test in existing_tests)
    missing: list[MissingTestRecommendation] = []
    for flow in affected_flows:
        if _normalize_flow_name(flow.name) in covered:
            continue
        if flow.kind == "api":
            missing.append(
                MissingTestRecommendation(
                    kind="api",
                    flow=flow.name,
                    files=flow.files,
                    reason="changed API flow has no linked Retrace spec",
                    command=f"retrace tester api-create --name {shlex.quote(flow.name)}",
                )
            )
        elif flow.kind == "ui":
            missing.append(
                MissingTestRecommendation(
                    kind="ui",
                    flow=flow.name,
                    files=flow.files,
                    reason="changed UI route has no linked Retrace spec",
                    command=f"retrace tester create --name {shlex.quote(flow.name)}",
                )
            )
        elif flow.kind == "component":
            missing.append(
                MissingTestRecommendation(
                    kind="ui",
                    flow=flow.name,
                    files=flow.files,
                    reason="changed shared component may affect multiple UI flows",
                    command=f"retrace tester explore --task {shlex.quote(flow.name)}",
                )
            )
    return missing


def _routes_for_changed_file(
    path: str,
    added_text: str,
    route_manifest: list[RouteDefinition],
) -> list[str]:
    routes: list[str] = []
    for route in route_manifest:
        if route.file_path == path:
            routes.append(route.route)
    inferred = _route_from_path(path)
    if inferred:
        routes.append(inferred)
    for match in _ROUTE_CALL_RE.finditer(added_text):
        routes.append(_normalize_route(match.group(2)))
    for match in _API_PATH_RE.finditer(added_text):
        routes.append(_normalize_route(match.group(1)))
    return _unique(routes)


def _route_from_path(path: str) -> str:
    parts = list(Path(path).parts)
    if "api" in parts:
        idx = parts.index("api")
        route_parts = parts[idx:]
        if route_parts[-1].split(".", 1)[0] in {"route", "index"}:
            route_parts = route_parts[:-1]
        else:
            route_parts[-1] = route_parts[-1].split(".", 1)[0]
        return _normalize_route("/" + "/".join(route_parts))
    for marker in ("app", "pages"):
        if marker not in parts:
            continue
        idx = parts.index(marker)
        route_parts = parts[idx + 1 :]
        if not route_parts:
            continue
        stem = route_parts[-1].split(".", 1)[0]
        if stem in {"page", "index"}:
            route_parts = route_parts[:-1]
        else:
            route_parts[-1] = stem
        if route_parts and route_parts[0] != "api":
            return _normalize_route("/" + "/".join(route_parts))
    return ""


def _component_name(path: str) -> str:
    parts = [part for part in Path(path).parts if part]
    if "components" not in parts:
        return ""
    stem = Path(path).stem
    return stem if stem else path


def _failure_context(
    store: Storage,
    failure: FailureRow,
) -> tuple[set[str], set[str]]:
    texts = [
        failure.title,
        failure.summary,
        failure.source_external_id,
        str(failure.metadata),
    ]
    files = set(_file_refs_from_obj(failure.metadata))
    flows = set(_route_refs_from_text(" ".join(texts)))
    for evidence in store.list_failure_evidence(
        failure_id=failure.id,
        include_sensitive=False,
    ):
        files.update(_file_refs_from_obj(evidence.payload))
        flows.update(_route_refs_from_text(str(evidence.payload)))
        flows.update(_route_refs_from_text(evidence.source))
    for key in ("route", "route_path", "current_url", "url", "transaction"):
        value = failure.metadata.get(key)
        if isinstance(value, str):
            flows.update(_route_refs_from_text(value))
    return files, flows


def _file_refs_from_obj(value: object) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"file", "file_path", "path"} and isinstance(child, str):
                refs.add(child)
            refs.update(_file_refs_from_obj(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_file_refs_from_obj(child))
    elif isinstance(value, str):
        refs.update(match.group(1) for match in _FILE_REF_RE.finditer(value))
    return refs


def _route_refs_from_text(text: str) -> set[str]:
    routes = {_normalize_route(match.group(1)) for match in _API_PATH_RE.finditer(text)}
    for raw in re.findall(r"https?://[^/\s]+(/[A-Za-z0-9_./:[\]-]+)", text):
        routes.add(_normalize_route(raw))
    if text.startswith("/"):
        routes.add(_normalize_route(text))
    return routes


def _existing_test_recommendation(
    link: FailureTestLinkRow,
    failure: PriorFailureReference,
) -> ExistingTestRecommendation:
    command = (
        f"retrace tester api-run {shlex.quote(link.spec_id)}"
        if "api" in (link.spec_path + link.spec_id).lower()
        else f"retrace tester run {shlex.quote(link.spec_id)}"
    )
    return ExistingTestRecommendation(
        spec_id=link.spec_id,
        spec_name=link.spec_name,
        spec_path=link.spec_path,
        source=link.source,
        coverage_state=link.coverage_state,
        failure_public_id=failure.public_id,
        command=command,
    )


def _summary_comment_body(analysis: PRReviewAnalysis) -> str:
    lines = [
        SUMMARY_MARKER,
        "## Retrace QA review",
        "",
        f"- Changed files: {len(analysis.changed_files)}",
        f"- Affected flows: {len(analysis.affected_flows)}",
        f"- Prior failures linked: {len(analysis.prior_failures)}",
        f"- Existing Retrace specs: {len(analysis.existing_tests)}",
        f"- Missing test suggestions: {len(analysis.missing_tests)}",
    ]
    if analysis.affected_flows:
        lines.extend(["", "### Affected flows"])
        for flow in analysis.affected_flows[:10]:
            lines.append(f"- `{flow.kind}` `{flow.name}` via `{', '.join(flow.files)}`")
    if analysis.prior_failures:
        lines.extend(["", "### Prior failures"])
        for failure in analysis.prior_failures[:10]:
            matches = ", ".join(failure.matched_files + failure.matched_flows)
            lines.append(
                f"- `{failure.public_id}` {failure.title} "
                f"({failure.severity}, {failure.status}) matched {matches}"
            )
    if analysis.existing_tests:
        lines.extend(["", "### Run existing specs"])
        for test in analysis.existing_tests[:10]:
            lines.append(f"- `{test.command}` for `{test.spec_name}`")
    if analysis.missing_tests:
        lines.extend(["", "### Generate missing specs"])
        for missing in analysis.missing_tests[:10]:
            lines.append(f"- `{missing.command}` for `{missing.flow}`")
    lines.extend(
        [
            "",
            "React with thumbs up to accept a suggestion or confused to dismiss it.",
        ]
    )
    return "\n".join(lines)


def _missing_test_inline_comment(
    changed_files: list[ChangedFile],
    missing: MissingTestRecommendation,
) -> PRInlineComment | None:
    path, line = _first_comment_position(changed_files, missing.files)
    if not path or line <= 0:
        return None
    suggestion_id = _suggestion_id("missing", missing.kind, missing.flow, path, line)
    body = "\n".join(
        [
            _inline_marker(suggestion_id),
            f"Retrace did not find linked coverage for `{missing.flow}`.",
            "",
            f"Suggested command: `{missing.command}`",
            "",
            "React with thumbs up to accept or confused to dismiss this suggestion.",
        ]
    )
    return PRInlineComment(
        path=path,
        line=line,
        body=body,
        suggestion_id=suggestion_id,
    )


def _prior_failure_inline_comment(
    changed_files: list[ChangedFile],
    failure: PriorFailureReference,
    existing_tests: list[ExistingTestRecommendation],
) -> PRInlineComment | None:
    path, line = _first_comment_position(changed_files, failure.matched_files)
    if not path or line <= 0:
        return None
    related_commands = [
        test.command
        for test in existing_tests
        if test.failure_public_id == failure.public_id
    ]
    suggestion_id = _suggestion_id("failure", failure.public_id, path, line)
    lines = [
        _inline_marker(suggestion_id),
        f"Retrace linked this change to prior failure `{failure.public_id}`: "
        f"{failure.title}",
    ]
    if related_commands:
        lines.extend(["", "Run the linked spec before merging:"])
        lines.extend(f"- `{command}`" for command in related_commands[:5])
    lines.extend(
        [
            "",
            "React with thumbs up to accept or confused to dismiss this suggestion.",
        ]
    )
    return PRInlineComment(
        path=path,
        line=line,
        body="\n".join(lines),
        suggestion_id=suggestion_id,
    )


def _first_comment_position(
    changed_files: list[ChangedFile],
    candidate_paths: list[str],
) -> tuple[str, int]:
    candidates = [path for path in candidate_paths if path]
    if not candidates and changed_files:
        candidates = [changed_files[0].path]
    changed_by_path = {changed.path: changed for changed in changed_files}
    for path in candidates:
        changed = changed_by_path.get(path)
        if not changed:
            continue
        for hunk in changed.hunks:
            if hunk.added_line_numbers:
                return path, hunk.added_line_numbers[0]
        if changed.hunks:
            return path, changed.hunks[0].new_start
    return "", 0


def _suggestion_id(*parts: object) -> str:
    raw = "\0".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _inline_marker(suggestion_id: str) -> str:
    return f"{INLINE_MARKER_PREFIX}{suggestion_id}{INLINE_MARKER_SUFFIX}"


def _append_flow(
    flows: list[AffectedFlow],
    seen: set[tuple[str, str]],
    flow: AffectedFlow,
) -> None:
    key = (flow.kind, flow.name)
    if key in seen:
        return
    seen.add(key)
    flows.append(flow)


def _normalize_route(route: str) -> str:
    clean = route.strip().split("?", 1)[0].rstrip("/")
    return clean or "/"


def _normalize_flow_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out
