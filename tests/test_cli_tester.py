from pathlib import Path

from click.testing import CliRunner

from retrace.cli import main


_CONFIG_YAML = """posthog:
  host: https://us.i.posthog.com
  project_id: "42"
llm:
  provider: openai_compatible
  base_url: http://localhost:8080/v1
  model: llama
run:
  lookback_hours: 6
  max_sessions_per_run: 50
  output_dir: ./reports
  data_dir: ./data
detectors:
  console_error: true
cluster:
  min_size: 1
"""


def test_tester_create_list_and_run(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    create = runner.invoke(
        main,
        [
            "tester",
            "create",
            "--name",
            "Signup flow",
            "--prompt",
            "Open the page and click sign up",
            "--harness-cmd",
            "echo RUN {app_url_q} {prompt_q} > {run_dir}/out.txt",
        ],
    )
    assert create.exit_code == 0, create.output
    assert "Created tester spec:" in create.output

    listed = runner.invoke(main, ["tester", "list"])
    assert listed.exit_code == 0, listed.output
    assert "Signup flow" in listed.output
    spec_id = listed.output.splitlines()[0].split("\t")[0]

    ran = runner.invoke(main, ["tester", "run", spec_id])
    assert ran.exit_code == 0, ran.output
    assert '"ok": true' in ran.output

    specs_dir = tmp_path / "data" / "ui-tests" / "specs"
    runs_dir = tmp_path / "data" / "ui-tests" / "runs"
    assert any(specs_dir.glob("*.json"))
    assert any(runs_dir.glob("*/run.json"))


def test_tester_create_suite_defaults_to_explore_mode(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    create = runner.invoke(main, ["tester", "create-suite"])
    assert create.exit_code == 0, create.output

    listed = runner.invoke(main, ["tester", "list"])
    assert listed.exit_code == 0, listed.output
    assert "\texplore_suite\t" in listed.output


def test_tester_run_retries_and_marks_flaky(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    create = runner.invoke(
        main,
        [
            "tester",
            "create",
            "--name",
            "Flaky flow",
            "--prompt",
            "Run flow",
            "--harness-cmd",
            "if [ -f {run_dir_q}/ok ]; then echo ok {app_url_q} {prompt_q}; exit 0; else touch {run_dir_q}/ok; echo timeout {app_url_q} {prompt_q}; exit 1; fi",
        ],
    )
    assert create.exit_code == 0, create.output

    listed = runner.invoke(main, ["tester", "list"])
    assert listed.exit_code == 0, f"tester list failed: {listed.output}"
    spec_id = listed.output.splitlines()[0].split("\t")[0]

    ran = runner.invoke(main, ["tester", "run", spec_id, "--retries", "1"])
    assert ran.exit_code == 0, ran.output
    assert '"status": "flaky_passed"' in ran.output
    assert '"flaky": true' in ran.output


def test_tester_enqueue_and_worker_runs_queued_spec(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "config.yaml").write_text(_CONFIG_YAML)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    create = runner.invoke(
        main,
        [
            "tester",
            "create",
            "--name",
            "Queued flow",
            "--prompt",
            "Open queued page",
            "--harness-cmd",
            "echo QUEUED {app_url_q} {prompt_q} > {run_dir}/queued.txt",
        ],
    )
    assert create.exit_code == 0, create.output
    spec_id = create.output.strip().split(": ")[1]

    enqueue = runner.invoke(main, ["tester", "enqueue", spec_id])
    assert enqueue.exit_code == 0, enqueue.output
    assert '"status": "queued"' in enqueue.output

    worker = runner.invoke(main, ["tester", "worker", "--once"])
    assert worker.exit_code == 0, worker.output
    assert '"status": "succeeded"' in worker.output

    assert any((tmp_path / "data" / "ui-tests" / "queue" / "done").glob("*.json"))
    assert any((tmp_path / "data" / "ui-tests" / "runs").glob("*/run.json"))
