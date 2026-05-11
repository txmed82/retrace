"""Tests for `retrace review` CLI surface."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from retrace.commands.review import review_command
from retrace.storage import Storage


_SAMPLE_DIFF = """diff --git a/server/routes/auth.ts b/server/routes/auth.ts
index 0000000..1111111 100644
--- a/server/routes/auth.ts
+++ b/server/routes/auth.ts
@@ -10,4 +10,7 @@ router.post('/api/login', async (req, res) => {
   const user = await users.find(req.body.email);
+  if (!user) {
+    return res.status(404).json({ error: 'not found' });
+  }
   res.json({ token: signJwt(user) });
 });
"""


def _config_file(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """posthog:
  host: https://us.i.posthog.com
  project_id: ""
llm:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: x
run:
  data_dir: """ + str(tmp_path / "data") + "\n"
    )
    return cfg


def test_review_from_diff_file(tmp_path: Path):
    cfg = _config_file(tmp_path)
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_SAMPLE_DIFF)

    runner = CliRunner()
    result = runner.invoke(
        review_command,
        [
            "--config", str(cfg),
            "--diff", str(diff_file),
            "--pr", "https://github.com/org/app/pull/42",
            "--no-file-incidents",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    paths = [f["path"] for f in payload["changed_files"]]
    assert "server/routes/auth.ts" in paths


def test_review_files_incidents_when_requested(tmp_path: Path):
    cfg = _config_file(tmp_path)
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_SAMPLE_DIFF)

    runner = CliRunner()
    result = runner.invoke(
        review_command,
        [
            "--config", str(cfg),
            "--diff", str(diff_file),
            "--pr", "https://github.com/org/app/pull/42",
            "--file-incidents",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    store = Storage(tmp_path / "data" / "retrace.db")
    # No prior failures and no route manifest exists, so missing_tests may be
    # zero — but the call must not crash, and the JSON shape is stable.
    assert "incidents_filed" in payload
    assert isinstance(payload["incidents_filed"], list)
    # If any incident was filed, it should be reachable in the store.
    for pid in payload["incidents_filed"]:
        assert store.get_qa_incident(pid) is not None


def test_review_rejects_bare_pr_number_without_repo(tmp_path: Path):
    cfg = _config_file(tmp_path)
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_SAMPLE_DIFF)

    runner = CliRunner()
    result = runner.invoke(
        review_command,
        [
            "--config", str(cfg),
            "--diff", str(diff_file),
            "--pr", "42",
            "--no-file-incidents",
            "--json",
        ],
    )
    # `--pr 42` without `--repo` should error cleanly, not crash.
    assert result.exit_code != 0
    assert "bare number" in result.output.lower() or "--repo" in result.output


def test_review_requires_diff_or_pr(tmp_path: Path):
    cfg = _config_file(tmp_path)
    runner = CliRunner()
    result = runner.invoke(review_command, ["--config", str(cfg)])
    assert result.exit_code != 0
    assert "--diff" in result.output or "--pr" in result.output


def test_review_post_comment_requires_pr(tmp_path: Path):
    """`--post-comment` without a real PR ref should error cleanly."""
    cfg = _config_file(tmp_path)
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_SAMPLE_DIFF)

    runner = CliRunner()
    result = runner.invoke(
        review_command,
        [
            "--config", str(cfg),
            "--diff", str(diff_file),
            "--post-comment",
            "--no-file-incidents",
        ],
    )
    assert result.exit_code != 0
    assert "--pr" in result.output or "post-comment" in result.output.lower()


def test_review_run_affected_tests_is_no_op_when_no_specs_cover(tmp_path: Path):
    """If `analysis.existing_tests` is empty (no specs match), the
    --run-affected-tests path just produces an empty list without
    crashing."""
    cfg = _config_file(tmp_path)
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_SAMPLE_DIFF)

    runner = CliRunner()
    result = runner.invoke(
        review_command,
        [
            "--config", str(cfg),
            "--diff", str(diff_file),
            "--pr", "https://github.com/org/app/pull/42",
            "--no-file-incidents",
            "--run-affected-tests",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "affected_test_results" in payload
    assert isinstance(payload["affected_test_results"], list)


def test_review_format_comment_body_includes_sections(tmp_path: Path):
    """The PR comment body should fold in incidents + test results."""
    from retrace.commands.review import _format_comment_body
    from retrace.pr_review import analyze_pr_diff

    analysis = analyze_pr_diff(diff_text=_SAMPLE_DIFF)
    body = _format_comment_body(
        analysis=analysis,
        incidents_filed=["INC-AB12CD", "INC-EF34GH"],
        affected_test_results=[
            {"spec_id": "spec-1", "spec_name": "login flow", "status": "pass"},
            {"spec_id": "spec-2", "spec_name": "checkout", "status": "fail"},
        ],
    )
    assert "INC-AB12CD" in body
    assert "INC-EF34GH" in body
    assert "spec-1" in body
    assert "spec-2" in body
    assert "1 pass / 1 fail" in body
    assert body.endswith("\n")
