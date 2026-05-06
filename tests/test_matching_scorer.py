from pathlib import Path

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
