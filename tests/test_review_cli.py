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
    # With no specs covering the diff, the path must return exactly an
    # empty list — not just "a list".
    assert payload["affected_test_results"] == []


def test_review_llm_flag_off_skips_llm_call(tmp_path: Path, monkeypatch):
    """`--no-llm` must not invoke the LLM client even when one is configured."""
    cfg = _config_file(tmp_path)
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_SAMPLE_DIFF)

    from retrace import commands as _cmds  # noqa: F401
    from retrace.commands import review as review_mod

    called = {"count": 0}

    def _fake_llm_review(**kwargs):
        called["count"] += 1
        from retrace.llm_pr_review import LLMReviewResult

        return LLMReviewResult(summary="should-not-be-called")

    monkeypatch.setattr(review_mod, "llm_review", _fake_llm_review)

    runner = CliRunner()
    result = runner.invoke(
        review_mod.review_command,
        [
            "--config", str(cfg),
            "--diff", str(diff_file),
            "--pr", "https://github.com/org/app/pull/42",
            "--no-file-incidents",
            "--no-llm",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert called["count"] == 0
    payload = json.loads(result.output)
    # `llm_review` defaults to empty when --no-llm.
    assert payload["llm_review"]["summary"] == ""


def test_review_llm_flag_on_without_configured_llm_errors(tmp_path: Path):
    """`--llm` with no LLM in config should error cleanly, not crash."""
    cfg = tmp_path / "config.yaml"
    # Note: empty LLM base_url
    cfg.write_text(
        f"""posthog:
  host: https://us.i.posthog.com
  project_id: demo
llm:
  provider: openai_compatible
  base_url: ""
  model: x
run:
  data_dir: {tmp_path / "data"}
"""
    )
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(_SAMPLE_DIFF)

    from retrace.commands.review import review_command

    runner = CliRunner()
    result = runner.invoke(
        review_command,
        [
            "--config", str(cfg),
            "--diff", str(diff_file),
            "--pr", "https://github.com/org/app/pull/42",
            "--no-file-incidents",
            "--llm",
        ],
    )
    assert result.exit_code != 0
    assert "no LLM is configured" in result.output or "llm" in result.output.lower()


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


def test_review_format_comment_body_puts_llm_section_first(tmp_path: Path):
    """When an LLM review is present, it goes ABOVE the templated
    summary — the LLM is the meat for human readers."""
    from retrace.commands.review import _format_comment_body
    from retrace.llm_pr_review import InlineSuggestion, LLMReviewResult
    from retrace.pr_review import analyze_pr_diff

    analysis = analyze_pr_diff(diff_text=_SAMPLE_DIFF)
    llm = LLMReviewResult(
        summary="Adds 404 handling on /api/login.",
        walkthrough=["server: returns 404 when user is missing"],
        inline_suggestions=[
            InlineSuggestion(
                path="server/routes/auth.ts",
                line=12,
                body="Consider 401 instead of 404 to avoid disclosing user existence.",
            )
        ],
        model="test-model",
    )
    body = _format_comment_body(
        analysis=analysis,
        incidents_filed=[],
        affected_test_results=[],
        llm_result=llm,
    )
    llm_pos = body.find("Retrace LLM review")
    # The templated review uses build_pr_review_comment_plan which starts
    # with its own header. Just assert the LLM section is at the top.
    assert llm_pos != -1
    assert llm_pos < 100  # within the first ~5 lines
    assert "Adds 404 handling" in body
    assert "401 instead of 404" in body
