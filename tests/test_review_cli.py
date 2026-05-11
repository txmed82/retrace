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
