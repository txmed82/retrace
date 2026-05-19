from __future__ import annotations
from .models import (
    ALLOWED_AUTH_MODES as ALLOWED_AUTH_MODES,
    ALLOWED_EXECUTION_ENGINES as ALLOWED_EXECUTION_ENGINES,
    ALLOWED_MODES as ALLOWED_MODES,
    DEFAULT_APP_URL as DEFAULT_APP_URL,
    DEFAULT_HARNESS_COMMAND as DEFAULT_HARNESS_COMMAND,
    FAILURE_CLASSIFICATIONS as FAILURE_CLASSIFICATIONS,
    SPEC_SCHEMA_VERSION as SPEC_SCHEMA_VERSION,
    SUITE_PROPOSAL_SCHEMA_VERSION as SUITE_PROPOSAL_SCHEMA_VERSION,
    EngineSelection as EngineSelection,
    TesterArtifact as TesterArtifact,
    TesterAssertionResult as TesterAssertionResult,
    TesterRunResult as TesterRunResult,
    TesterSpec as TesterSpec,
    TesterStepCacheEvent as TesterStepCacheEvent,
    now_iso as now_iso,
    queue_dir_for_data_dir as queue_dir_for_data_dir,
    runs_dir_for_data_dir as runs_dir_for_data_dir,
    skills_dir_for_data_dir as skills_dir_for_data_dir,
    slugify as slugify,
    specs_dir_for_data_dir as specs_dir_for_data_dir,
)
from .specs import (
    create_spec as create_spec,
    list_specs as list_specs,
    load_spec as load_spec,
    save_spec as save_spec,
    select_execution_engine as select_execution_engine,
    validate_spec as validate_spec,
)
from .assertions import (
    _assertion_result as _assertion_result,
    _assertion_text_for_classification as _assertion_text_for_classification,
    _classify_failure as _classify_failure,
    _evaluate_consensus_assertion as _evaluate_consensus_assertion,
    _evaluate_model_backed_consensus_assertion as _evaluate_model_backed_consensus_assertion,
    _evaluate_native_assertion as _evaluate_native_assertion,
    _failed_selector_assertion as _failed_selector_assertion,
    _flake_reason_from_classification as _flake_reason_from_classification,
    _redacted_response_headers as _redacted_response_headers,
    _response_assertion_evidence as _response_assertion_evidence,
)
from . import harness as _harness
from .harness import (
    load_run_summaries as load_run_summaries,
    enqueue_spec_run as enqueue_spec_run,
    run_queued_spec_once as run_queued_spec_once,
    set_explore_factories as set_explore_factories,
    set_visual_factories as set_visual_factories,
    _run_playwright_spec as _run_playwright_spec,
    _run_shell as _run_shell,
)


def run_spec(*args, **kwargs):
    """Run a tester spec through the current package-level shell hook.

    Older tests and integrations monkeypatch `retrace.tester._run_shell`.
    The implementation now lives in `retrace.tester.harness`, so keep that
    facade contract by syncing the harness hook immediately before execution.
    """
    _harness._run_shell = _run_shell
    return _harness.run_spec(*args, **kwargs)
