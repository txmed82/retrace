from __future__ import annotations

from pathlib import Path

from retrace.failures import CanonicalFailure, stable_failure_public_id
from retrace.pr_review import analyze_pr_diff, parse_unified_diff
from retrace.storage import Storage


def _store(tmp_path: Path) -> Storage:
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    return store


def _failure(
    *,
    project_id: str,
    environment_id: str,
    source_external_id: str = "checkout-prod",
) -> CanonicalFailure:
    return CanonicalFailure(
        public_id=stable_failure_public_id(
            project_id,
            environment_id,
            "monitor_incident",
            source_external_id,
        ),
        project_id=project_id,
        environment_id=environment_id,
        source_type="monitor_incident",
        source_external_id=source_external_id,
        fingerprint=source_external_id,
        title="Checkout API returned 500",
        summary="POST /api/checkout failed after deploy.",
        severity="high",
        confidence="high",
        status="new",
        metadata={
            "route": "/api/checkout",
            "likely_files": ["src/app/api/checkout/route.ts"],
        },
    )


def test_parse_unified_diff_extracts_changed_files_and_added_lines() -> None:
    diff = """diff --git a/src/app/page.tsx b/src/app/page.tsx
--- a/src/app/page.tsx
+++ b/src/app/page.tsx
@@ -1,2 +1,3 @@
 import React from "react"
+export default function Page() { return <main /> }
"""

    changed = parse_unified_diff(diff)

    assert [item.path for item in changed] == ["src/app/page.tsx"]
    assert changed[0].hunks[0].new_start == 1
    assert changed[0].hunks[0].added_lines == [
        'export default function Page() { return <main /> }'
    ]


def test_pr_review_lists_affected_api_flow_and_missing_api_test() -> None:
    diff = """diff --git a/src/app/api/checkout/route.ts b/src/app/api/checkout/route.ts
--- a/src/app/api/checkout/route.ts
+++ b/src/app/api/checkout/route.ts
@@ -1,3 +1,5 @@
+export async function POST() {
+  return Response.json({ ok: true })
+}
"""

    analysis = analyze_pr_diff(diff_text=diff)

    assert [flow.name for flow in analysis.affected_flows] == ["/api/checkout"]
    assert analysis.affected_flows[0].kind == "api"
    assert analysis.missing_tests[0].kind == "api"
    assert analysis.missing_tests[0].flow == "/api/checkout"


def test_pr_review_links_prior_failure_and_recommends_existing_spec(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace = store.ensure_workspace(
        org_name="Acme",
        project_name="Web",
        environment_name="production",
    )
    failure_id = store.upsert_failure(
        _failure(
            project_id=workspace.project_id,
            environment_id=workspace.environment_id,
        )
    )
    store.upsert_failure_test_link(
        failure_id=failure_id,
        spec_id="api checkout",
        spec_name="POST /api/checkout contract",
        spec_path="api-tests/specs/api_checkout.json",
        source="api_run",
    )
    diff = """diff --git a/src/app/api/checkout/route.ts b/src/app/api/checkout/route.ts
--- a/src/app/api/checkout/route.ts
+++ b/src/app/api/checkout/route.ts
@@ -1,3 +1,5 @@
+export async function POST() {
+  return Response.json({ ok: true })
+}
"""

    analysis = analyze_pr_diff(
        diff_text=diff,
        store=store,
        project_id=workspace.project_id,
        environment_id=workspace.environment_id,
    )

    assert len(analysis.prior_failures) == 1
    assert analysis.prior_failures[0].matched_files == [
        "src/app/api/checkout/route.ts"
    ]
    assert analysis.existing_tests[0].spec_id == "api checkout"
    assert analysis.existing_tests[0].command == "retrace tester api-run 'api checkout'"
    assert analysis.missing_tests == []


def test_pr_review_uses_route_manifest_for_affected_flow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    route_file = repo / "server/routes/billing.ts"
    route_file.parent.mkdir(parents=True)
    route_file.write_text(
        "export function billing() { return null }\n",
        encoding="utf-8",
    )
    (repo / "route-manifest.json").write_text(
        '{"/api/billing": "server/routes/billing.ts"}',
        encoding="utf-8",
    )
    diff = """diff --git a/server/routes/billing.ts b/server/routes/billing.ts
--- a/server/routes/billing.ts
+++ b/server/routes/billing.ts
@@ -1 +1,2 @@
+export function billing() { return null }
"""

    analysis = analyze_pr_diff(diff_text=diff, repo_path=repo)

    assert [flow.name for flow in analysis.affected_flows] == ["/api/billing"]
    assert analysis.missing_tests[0].command == (
        "retrace tester api-create --name /api/billing"
    )


def test_pr_review_suggests_ui_exploration_for_component_change() -> None:
    diff = """diff --git a/src/components/Checkout Button.tsx b/src/components/Checkout Button.tsx
--- a/src/components/Checkout Button.tsx
+++ b/src/components/Checkout Button.tsx
@@ -1 +1,2 @@
+export function CheckoutButton() { return <button /> }
"""

    analysis = analyze_pr_diff(diff_text=diff)

    assert analysis.affected_flows[0].kind == "component"
    assert analysis.affected_flows[0].name == "Checkout Button"
    assert analysis.missing_tests[0].kind == "ui"
    assert analysis.missing_tests[0].command == (
        "retrace tester explore --task 'Checkout Button'"
    )
