from pathlib import Path
import json

from retrace.matching.scorer import score_repo_for_finding


def test_score_repo_for_finding_ranks_store_files(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "client/src/pages").mkdir(parents=True, exist_ok=True)
    (repo / "server/routes").mkdir(parents=True, exist_ok=True)

    (repo / "client/src/pages/store.tsx").write_text(
        "export default function Store(){ const onClick = () => fetch('/api/store/payment-intent'); return null; }"
    )
    (repo / "server/routes/assessment.ts").write_text(
        "export function assessmentRoutes() { return 'assessment'; }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Unresponsive buttons causing rage clicks in the store",
        category="functional_error",
        evidence_text="Navigate to the store page and click button with no response.",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "client/src/pages/store.tsx"


def test_score_repo_for_finding_prioritizes_top_stack_frame_file(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "client/src/components").mkdir(parents=True, exist_ok=True)
    (repo / "client/src/pages").mkdir(parents=True, exist_ok=True)

    (repo / "client/src/components/SignupForm.tsx").write_text(
        "export function SignupForm(){ throw new Error('Signup crashed'); }"
    )
    (repo / "client/src/pages/signup.tsx").write_text(
        "export default function Signup(){ return <SignupForm />; }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Signup crashes after submit",
        category="functional_error",
        evidence_text=(
            "Correlated evidence:\n"
            "- Top stack frame: client/src/components/SignupForm.tsx:42:13\n"
            "TypeError: Cannot read properties of undefined"
        ),
        top_n=3,
    )

    assert out
    assert out[0].file_path == "client/src/components/SignupForm.tsx"
    assert "stack_frame" in out[0].rationale


def test_score_repo_for_finding_prioritizes_failed_api_route_handler(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "server/routes").mkdir(parents=True, exist_ok=True)
    (repo / "client/src/pages").mkdir(parents=True, exist_ok=True)

    (repo / "server/routes/auth.ts").write_text(
        "router.post('/api/auth/signup', async function signupHandler(req, res) { return res.status(500); });"
    )
    (repo / "client/src/pages/signup.tsx").write_text(
        "export function Signup(){ return fetch('/api/auth/signup'); }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Signup submit returns 500",
        category="functional_error",
        evidence_text="Network 5xx near failure: POST /api/auth/signup returned 500",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "server/routes/auth.ts"
    assert "api_route:/api/auth/signup" in out[0].rationale


def test_score_repo_for_finding_extracts_bare_api_route(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "server/routes").mkdir(parents=True, exist_ok=True)
    (repo / "client/src/pages").mkdir(parents=True, exist_ok=True)

    (repo / "server/routes/billing.ts").write_text(
        "router.post('/api/billing/checkout', checkoutHandler);"
    )
    (repo / "client/src/pages/billing.tsx").write_text(
        "export function Billing(){ return fetch('/api/billing/checkout'); }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Checkout request fails",
        category="functional_error",
        evidence_text="The failed request to /api/billing/checkout returned 500.",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "server/routes/billing.ts"
    assert "api_route:/api/billing/checkout" in out[0].rationale


def test_score_repo_for_finding_treats_top_level_src_as_server_route(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    (repo / "src/routes").mkdir(parents=True, exist_ok=True)
    (repo / "client/src/pages").mkdir(parents=True, exist_ok=True)

    (repo / "src/routes/auth.ts").write_text(
        "router.post('/api/auth/signup', signupHandler);"
    )
    (repo / "client/src/pages/signup.tsx").write_text(
        "export function Signup(){ return fetch('/api/auth/signup'); }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Signup request fails",
        category="functional_error",
        evidence_text="POST /api/auth/signup returned 500.",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "src/routes/auth.ts"
    assert "api_route:/api/auth/signup" in out[0].rationale


def test_score_repo_for_finding_does_not_treat_all_src_as_server_route(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    (repo / "src/routes").mkdir(parents=True, exist_ok=True)
    (repo / "src/components").mkdir(parents=True, exist_ok=True)

    (repo / "src/routes/profile.ts").write_text(
        "router.get('/api/profile', profileHandler);"
    )
    (repo / "src/components/ProfileButton.tsx").write_text(
        "export function ProfileButton(){ return fetch('/api/profile'); }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Profile request fails",
        category="functional_error",
        evidence_text="GET /api/profile returned 500.",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "src/routes/profile.ts"


def test_score_repo_for_finding_matches_dynamic_api_route_handlers(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    (repo / "server/routes").mkdir(parents=True, exist_ok=True)
    (repo / "client/src/pages").mkdir(parents=True, exist_ok=True)

    (repo / "server/routes/checkout.ts").write_text(
        "router.get('/api/checkout/:cartId', checkoutHandler);"
    )
    (repo / "client/src/pages/checkout.tsx").write_text(
        "export function Checkout(){ return fetch('/api/checkout/42'); }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Checkout API returns 500",
        category="api_failure",
        evidence_text="API reproduction: GET /api/checkout/42 expected 200 got 500.",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "server/routes/checkout.ts"
    assert "api_route_pattern:/api/checkout/42" in out[0].rationale


def test_score_repo_for_finding_ignores_opaque_route_ids(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    (repo / "server/routes").mkdir(parents=True, exist_ok=True)
    (repo / "server/routes/customers.ts").write_text(
        "router.get('/api/customers/:customerId/invoices', invoiceHandler);"
    )
    (repo / "server/routes/customer-fixtures.ts").write_text(
        "export const sample = 'cus_123456789 invoices';"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Customer invoices API returns 500",
        category="api_failure",
        evidence_text="GET /api/customers/cus_123456789/invoices returned 500.",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "server/routes/customers.ts"
    assert "api_route_pattern:/api/customers/cus_123456789/invoices" in out[0].rationale


def test_score_repo_for_finding_uses_sourcemap_sources_for_stack_frames(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    (repo / "dist").mkdir(parents=True, exist_ok=True)
    (repo / "src/components").mkdir(parents=True, exist_ok=True)
    (repo / "src/pages").mkdir(parents=True, exist_ok=True)
    (repo / "dist/app.js").write_text("function minified(){throw Error('boom')}")
    (repo / "dist/app.js.map").write_text(
        json.dumps(
            {
                "version": 3,
                "file": "app.js",
                "sources": ["../src/components/CheckoutButton.tsx"],
                "mappings": "",
            }
        )
    )
    (repo / "src/components/CheckoutButton.tsx").write_text(
        "export function CheckoutButton(){ return <button />; }"
    )
    (repo / "src/pages/checkout.tsx").write_text(
        "export function Checkout(){ return <CheckoutButton />; }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Checkout crash",
        category="frontend_error",
        evidence_text="TypeError at dist/app.js:1:143",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "src/components/CheckoutButton.tsx"
    assert "stack_frame:src/components/CheckoutButton.tsx" in out[0].rationale
    assert out[0].symbol == "CheckoutButton"


def test_score_repo_for_finding_uses_framework_route_manifest(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".next/server").mkdir(parents=True, exist_ok=True)
    (repo / "src/app/api/accounts/[accountId]").mkdir(parents=True, exist_ok=True)
    (repo / "src/components").mkdir(parents=True, exist_ok=True)
    (repo / ".next/server/app-paths-manifest.json").write_text(
        json.dumps(
            {
                "/api/accounts/[accountId]": "app/api/accounts/[accountId]/route.js",
            }
        )
    )
    (repo / "src/app/api/accounts/[accountId]/route.ts").write_text(
        "export async function GET(){ return Response.json({ok:false}); }"
    )
    (repo / "src/components/AccountLink.tsx").write_text(
        "export function AccountLink(){ return fetch('/api/accounts/acct_123'); }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Account API returns 500",
        category="api_failure",
        evidence_text="GET /api/accounts/acct_123 returned 500.",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "src/app/api/accounts/[accountId]/route.ts"
    assert "route_manifest:/api/accounts/acct_123" in out[0].rationale
    assert out[0].symbol == "GET"


def test_score_repo_for_finding_includes_codeowners_context(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True, exist_ok=True)
    (repo / "server/routes").mkdir(parents=True, exist_ok=True)
    (repo / ".github/CODEOWNERS").write_text(
        "server/routes/ @backend-team @api-reviewer\n"
    )
    (repo / "server/routes/billing.ts").write_text(
        "export function billingHandler(){ router.post('/api/billing/pay', billingHandler); }"
    )

    out = score_repo_for_finding(
        repo_path=repo,
        title="Billing API returns 500",
        category="api_failure",
        evidence_text="POST /api/billing/pay returned 500.",
        top_n=3,
    )

    assert out
    assert out[0].file_path == "server/routes/billing.ts"
    assert out[0].owners == ["@backend-team", "@api-reviewer"]
    assert "codeowners:@backend-team @api-reviewer" in out[0].rationale
