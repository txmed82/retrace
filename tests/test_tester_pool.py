"""P3.2 — tester_pool parallel runner tests.

We don't actually fire up browser harnesses here — we inject a
fake `runner` callable so we can test the orchestration layer in
isolation: concurrency, ordering, exception handling, glob
filtering.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from retrace.tester import TesterSpec
from retrace.tester_pool import (
    PoolRunResult,
    run_specs_parallel,
    select_specs,
)


def _spec(spec_id: str, *, name: str = "") -> TesterSpec:
    """Minimal TesterSpec for orchestration tests."""
    return TesterSpec(
        schema_version=1,
        spec_id=spec_id,
        name=name or spec_id,
        mode="exploratory",
        prompt="",
        app_url="",
        start_command="",
        harness_command="",
        auth_required=False,
        auth_mode="",
        auth_login_url="",
        auth_username="",
        auth_password_env="",
        auth_jwt_env="",
        auth_headers_env="",
        created_at="",
        updated_at="",
    )


def _fake_result(spec, ok: bool, *, sleep: float = 0.0):
    """Tiny stand-in for TesterRunResult."""
    if sleep:
        time.sleep(sleep)
    return SimpleNamespace(
        spec_id=spec.spec_id,
        run_id=f"run-{spec.spec_id}",
        ok=ok,
        status="passed" if ok else "failed",
        exit_code=0 if ok else 1,
        run_dir=f"/tmp/{spec.spec_id}",
        execution_engine="harness",
    )


# ---------------------------------------------------------------------------
# select_specs
# ---------------------------------------------------------------------------


def test_select_specs_filter_by_id(tmp_path):
    from retrace.tester import save_spec, specs_dir_for_data_dir

    specs_dir = specs_dir_for_data_dir(tmp_path)
    specs_dir.mkdir(parents=True)
    save_spec(specs_dir, _spec("spec_a"))
    save_spec(specs_dir, _spec("spec_b"))
    save_spec(specs_dir, _spec("spec_c"))

    selected = select_specs(data_dir=tmp_path, spec_ids=["spec_a", "spec_c"])
    assert sorted(s.spec_id for s in selected) == ["spec_a", "spec_c"]


def test_select_specs_match_glob(tmp_path):
    from retrace.tester import save_spec, specs_dir_for_data_dir

    specs_dir = specs_dir_for_data_dir(tmp_path)
    specs_dir.mkdir(parents=True)
    save_spec(specs_dir, _spec("login_happy"))
    save_spec(specs_dir, _spec("login_error"))
    save_spec(specs_dir, _spec("checkout"))

    selected = select_specs(data_dir=tmp_path, match_pattern="login*")
    assert sorted(s.spec_id for s in selected) == ["login_error", "login_happy"]


def test_select_specs_empty_pattern_returns_all(tmp_path):
    from retrace.tester import save_spec, specs_dir_for_data_dir

    specs_dir = specs_dir_for_data_dir(tmp_path)
    specs_dir.mkdir(parents=True)
    save_spec(specs_dir, _spec("a"))
    save_spec(specs_dir, _spec("b"))
    selected = select_specs(data_dir=tmp_path)
    assert sorted(s.spec_id for s in selected) == ["a", "b"]


# ---------------------------------------------------------------------------
# run_specs_parallel — orchestration
# ---------------------------------------------------------------------------


def test_empty_specs_returns_empty_result(tmp_path):
    out = run_specs_parallel(
        [], data_dir=tmp_path, cwd=tmp_path, workers=4,
    )
    assert out.total == 0
    assert out.per_spec == []


def test_results_preserve_input_order(tmp_path):
    """Even when specs finish out of order, the result list mirrors
    the input order so downstream tooling can reason about index."""
    specs = [_spec(f"s{i}") for i in range(5)]
    # Even-indexed specs sleep longer so they finish later despite
    # being submitted first; the result list must still be ordered.
    def runner(spec, **kwargs):
        delay = 0.05 if int(spec.spec_id[1:]) % 2 == 0 else 0.01
        return _fake_result(spec, ok=True, sleep=delay)

    out = run_specs_parallel(
        specs,
        data_dir=tmp_path,
        cwd=tmp_path,
        workers=5,
        runner=runner,
    )
    assert [r.spec_id for r in out.per_spec] == [s.spec_id for s in specs]


def test_ok_and_fail_counts(tmp_path):
    specs = [_spec("a"), _spec("b"), _spec("c")]

    def runner(spec, **kwargs):
        return _fake_result(spec, ok=(spec.spec_id == "b"))

    out = run_specs_parallel(
        specs, data_dir=tmp_path, cwd=tmp_path, workers=2, runner=runner
    )
    assert out.total == 3
    assert out.ok_count == 1
    assert out.fail_count == 2
    assert out.skipped_count == 0


def test_exception_in_runner_is_skipped_not_raised(tmp_path):
    """One bad spec mustn't abort the whole pool."""
    specs = [_spec("good"), _spec("bad"), _spec("good2")]

    def runner(spec, **kwargs):
        if spec.spec_id == "bad":
            raise RuntimeError("synthetic")
        return _fake_result(spec, ok=True)

    out = run_specs_parallel(
        specs, data_dir=tmp_path, cwd=tmp_path, workers=2, runner=runner
    )
    assert out.total == 3
    assert out.ok_count == 2
    assert out.skipped_count == 1
    # The two good results are in their original order.
    assert [r.spec_id for r in out.per_spec] == ["good", "good2"]


def test_workers_clamped_to_spec_count(tmp_path):
    """Requesting 16 workers for 3 specs should still finish without
    spawning idle workers."""
    specs = [_spec(f"s{i}") for i in range(3)]

    def runner(spec, **kwargs):
        return _fake_result(spec, ok=True, sleep=0.01)

    out = run_specs_parallel(
        specs, data_dir=tmp_path, cwd=tmp_path, workers=16, runner=runner
    )
    assert out.total == 3
    assert out.ok_count == 3


def test_parallel_runs_actually_overlap(tmp_path):
    """Sanity check the pool is parallel, not sequential. Four
    200ms tasks under 4 workers should finish in well under the
    sequential 800ms. The bound is generous because thread-pool
    startup + test-process noise can add tens of ms; what we care
    about is "concurrent, not serial."
    """
    specs = [_spec(f"s{i}") for i in range(4)]

    def runner(spec, **kwargs):
        return _fake_result(spec, ok=True, sleep=0.2)

    started = time.perf_counter()
    run_specs_parallel(
        specs, data_dir=tmp_path, cwd=tmp_path, workers=4, runner=runner
    )
    elapsed = time.perf_counter() - started
    # Sequential would be 4 * 0.2 = 0.8s; parallel ~0.2s. Bound at
    # 0.5s leaves room for slow CI without missing a regression to
    # sequential execution.
    assert elapsed < 0.5, f"pool ran sequentially? took {elapsed:.3f}s"


def test_pool_result_dataclass_shape(tmp_path):
    """The CLI emits keys from this; pin the shape."""
    specs = [_spec("a")]
    out = run_specs_parallel(
        specs,
        data_dir=tmp_path,
        cwd=tmp_path,
        workers=1,
        runner=lambda spec, **kw: _fake_result(spec, ok=True),
    )
    assert isinstance(out, PoolRunResult)
    assert hasattr(out, "duration_seconds")
    assert out.duration_seconds >= 0
