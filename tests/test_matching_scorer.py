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
