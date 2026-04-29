from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from retrace.explorer import (
    ToolCallError,
    parse_tool_call,
    run_explorer,
)


# --------------------------- Fakes ---------------------------------------


@dataclass
class _FakeDriver:
    pages: list[dict[str, Any]] = field(default_factory=list)
    actions: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    closed: bool = False
    snapshot_response: dict[str, Any] = field(
        default_factory=lambda: {
            "url": "https://app.example/",
            "title": "Home",
            "text": "Welcome",
            "console": [],
        }
    )
    fail_on: dict[str, int] = field(default_factory=dict)  # tool -> times to fail

    def navigate(self, url: str) -> None:
        self._maybe_fail("navigate")
        self.actions.append(("navigate", (url,)))
        self.snapshot_response = {
            **self.snapshot_response,
            "url": url,
            "title": "Welcome page",
        }

    def click(self, selector: str) -> None:
        self._maybe_fail("click")
        self.actions.append(("click", (selector,)))

    def type(self, selector: str, text: str) -> None:
        self._maybe_fail("type")
        self.actions.append(("type", (selector, text)))

    def press(self, key: str, selector: str = "") -> None:
        self._maybe_fail("press")
        self.actions.append(("press", (key, selector)))

    def wait_for(self, selector: str, timeout_ms: int = 5000) -> None:
        self._maybe_fail("wait_for")
        self.actions.append(("wait_for", (selector, timeout_ms)))

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    def snapshot(self) -> dict[str, Any]:
        return dict(self.snapshot_response)

    def close(self) -> None:
        self.closed = True

    def _maybe_fail(self, tool: str) -> None:
        n = self.fail_on.get(tool, 0)
        if n > 0:
            self.fail_on[tool] = n - 1
            raise RuntimeError(f"{tool} failure (mock)")


class _ScriptedLLM:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, *, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        if not self.responses:
            return {"tool": "finish", "args": {"status": "abandoned", "summary": ""}}
        head = self.responses.pop(0)
        if isinstance(head, Exception):
            raise head
        return head


# --------------------------- parse_tool_call ----------------------------


def test_parse_tool_call_canonical_shape() -> None:
    call = parse_tool_call(
        {"tool": "click", "args": {"selector": "button[type=submit]"}, "rationale": "ok"}
    )
    assert call.tool == "click"
    assert call.args == {"selector": "button[type=submit]"}
    assert call.rationale == "ok"


def test_parse_tool_call_shorthand_args_at_top_level() -> None:
    call = parse_tool_call({"tool": "navigate", "url": "https://x"})
    assert call.tool == "navigate"
    assert call.args == {"url": "https://x"}


def test_parse_tool_call_rejects_unknown_tool() -> None:
    with pytest.raises(ToolCallError):
        parse_tool_call({"tool": "rm_rf", "args": {}})


def test_parse_tool_call_rejects_missing_required_arg() -> None:
    with pytest.raises(ToolCallError):
        parse_tool_call({"tool": "type", "args": {"selector": "x"}})  # missing text


def test_parse_tool_call_rejects_bad_finish_status() -> None:
    with pytest.raises(ToolCallError):
        parse_tool_call({"tool": "finish", "args": {"status": "made_it_up"}})


# --------------------------- run_explorer -------------------------------


def test_run_explorer_happy_path_persists_skill(tmp_path: Path) -> None:
    driver = _FakeDriver()
    llm = _ScriptedLLM(
        [
            {"tool": "click", "args": {"selector": "button.signup"}, "rationale": "open form"},
            {"tool": "type", "args": {"selector": "#email", "text": "demo@x.com"}},
            {"tool": "finish", "args": {"status": "success", "summary": "Signed up"}},
        ]
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    skills_dir = tmp_path / "skills"

    result = run_explorer(
        spec_id="spec-abc",
        spec_name="signup",
        app_url="https://app.example",
        exploratory_goals=["Complete the signup flow"],
        run_dir=run_dir,
        driver=driver,
        llm=llm,
        skills_dir=skills_dir,
    )

    assert result.ok is True
    assert result.finished is True
    assert result.finish_status == "success"
    assert result.skill_path
    skill_payload = json.loads(Path(result.skill_path).read_text())
    assert skill_payload["spec_id"] == "spec-abc"
    assert [s["tool"] for s in skill_payload["steps"]] == ["click", "type"]
    # Driver got navigate + 2 actions
    assert driver.actions[0] == ("navigate", ("https://app.example",))
    assert ("click", ("button.signup",)) in driver.actions
    assert ("type", ("#email", "demo@x.com")) in driver.actions
    assert driver.closed is True
    # Trace was written
    trace_path = run_dir / "artifacts" / "explore-trace.json"
    assert trace_path.exists()


def test_run_explorer_aborts_on_two_consecutive_invalid_calls(tmp_path: Path) -> None:
    driver = _FakeDriver()
    llm = _ScriptedLLM(
        [
            {"tool": "rm_rf", "args": {}},
            {"tool": "also_bad"},
        ]
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = run_explorer(
        spec_id="spec-bad",
        spec_name="bad",
        app_url="https://app.example",
        exploratory_goals=["x"],
        run_dir=run_dir,
        driver=driver,
        llm=llm,
        skills_dir=tmp_path / "skills",
    )

    assert result.ok is False
    assert result.finished is False
    assert "two consecutive invalid tool calls" in result.error
    assert result.skill_path == ""
    assert driver.closed is True


def test_run_explorer_step_budget_exhaustion(tmp_path: Path) -> None:
    driver = _FakeDriver()
    # LLM keeps clicking forever, never finishes.
    llm = _ScriptedLLM(
        [{"tool": "click", "args": {"selector": "a"}}] * 30
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = run_explorer(
        spec_id="spec-budget",
        spec_name="bug",
        app_url="https://app.example",
        exploratory_goals=["x"],
        run_dir=run_dir,
        driver=driver,
        llm=llm,
        skills_dir=tmp_path / "skills",
        max_steps=5,
    )

    assert result.ok is False
    assert "exhausted step budget" in result.error
    assert len(result.steps) == 5


def test_run_explorer_recovers_from_single_driver_failure(tmp_path: Path) -> None:
    driver = _FakeDriver(fail_on={"click": 1})  # first click fails, then OK
    llm = _ScriptedLLM(
        [
            {"tool": "click", "args": {"selector": "a"}},
            {"tool": "click", "args": {"selector": "b"}},
            {"tool": "finish", "args": {"status": "success", "summary": "done"}},
        ]
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = run_explorer(
        spec_id="spec-recover",
        spec_name="recover",
        app_url="https://app.example",
        exploratory_goals=["x"],
        run_dir=run_dir,
        driver=driver,
        llm=llm,
        skills_dir=tmp_path / "skills",
    )

    assert result.ok is True
    assert result.finished is True
    # Successful skill should only contain the second click (first failed)
    skill = json.loads(Path(result.skill_path).read_text())
    assert [s["tool"] for s in skill["steps"]] == ["click"]


def test_run_explorer_blocked_finish_does_not_persist_skill(tmp_path: Path) -> None:
    driver = _FakeDriver()
    llm = _ScriptedLLM(
        [
            {"tool": "click", "args": {"selector": "a"}},
            {"tool": "finish", "args": {"status": "blocked", "summary": "auth wall"}},
        ]
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = run_explorer(
        spec_id="spec-blocked",
        spec_name="blocked",
        app_url="https://app.example",
        exploratory_goals=["x"],
        run_dir=run_dir,
        driver=driver,
        llm=llm,
        skills_dir=tmp_path / "skills",
    )

    assert result.ok is False
    assert result.finished is True
    assert result.finish_status == "blocked"
    assert result.skill_path == ""


def test_observation_truncates_long_snapshot_text(tmp_path: Path) -> None:
    big_text = "x" * 20_000
    driver = _FakeDriver(
        snapshot_response={
            "url": "https://app.example",
            "title": "Big",
            "text": big_text,
            "console": [],
        }
    )
    llm = _ScriptedLLM(
        [{"tool": "finish", "args": {"status": "success", "summary": "ok"}}]
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    run_explorer(
        spec_id="spec-trunc",
        spec_name="trunc",
        app_url="https://app.example",
        exploratory_goals=["x"],
        run_dir=run_dir,
        driver=driver,
        llm=llm,
        skills_dir=tmp_path / "skills",
    )

    # The user prompt the LLM saw should contain the truncation marker.
    _, user = llm.calls[0]
    assert "[truncated]" in user
    assert len(user) < len(big_text)


def test_initial_navigate_failure_returns_error(tmp_path: Path) -> None:
    driver = _FakeDriver(fail_on={"navigate": 1})
    llm = _ScriptedLLM([])
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = run_explorer(
        spec_id="spec-nav",
        spec_name="nav",
        app_url="https://broken",
        exploratory_goals=["x"],
        run_dir=run_dir,
        driver=driver,
        llm=llm,
        skills_dir=tmp_path / "skills",
    )

    assert result.ok is False
    assert result.finished is False
    assert "initial navigate failed" in result.error


# --------------------------- Spec validation ----------------------------


def test_explore_engine_requires_exploratory_goals(tmp_path: Path) -> None:
    from retrace.tester import (
        TesterSpec,
        SPEC_SCHEMA_VERSION,
        validate_spec,
    )

    spec = TesterSpec(
        schema_version=SPEC_SCHEMA_VERSION,
        spec_id="abc",
        name="empty",
        mode="describe",
        prompt="explore",
        app_url="https://app.example",
        start_command="",
        harness_command="",
        auth_required=False,
        auth_mode="none",
        auth_login_url="",
        auth_username="",
        auth_password_env="",
        auth_jwt_env="",
        auth_headers_env="",
        created_at="",
        updated_at="",
        execution_engine="explore",
        exploratory_goals=[],
    )

    with pytest.raises(ValueError, match="exploratory_goal"):
        validate_spec(spec)


def test_auto_routing_picks_explore_for_goals_only(tmp_path: Path) -> None:
    from retrace.tester import create_spec, specs_dir_for_data_dir

    specs_dir = specs_dir_for_data_dir(tmp_path)
    spec = create_spec(
        specs_dir=specs_dir,
        name="goal-only",
        prompt="explore",
        app_url="https://app.example",
        start_command="",
        harness_command="",
        execution_engine="auto",
        exploratory_goals=["Sign up"],
    )
    # auto stays auto on the spec; resolution happens in run_spec.  Validation
    # must not require harness_command for auto when only goals are given.
    assert spec.execution_engine == "auto"


# --------------------------- Skill prefix replay ---------------------------


def _seed_skill(skills_dir: Path, host: str, *, goal: str, calls: list[dict]) -> Path:
    domain = skills_dir / host
    domain.mkdir(parents=True, exist_ok=True)
    path = domain / "spec-prev.json"
    path.write_text(
        json.dumps(
            {
                "spec_id": "spec-prev",
                "host": host,
                "goal": goal,
                "steps": calls,
            }
        )
    )
    return path


def test_skill_prefix_replays_before_llm(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _seed_skill(
        skills_dir,
        "app-example",
        goal="Complete signup",
        calls=[
            {"tool": "click", "args": {"selector": "button.signup"}},
            {"tool": "type", "args": {"selector": "#email", "text": "demo@x.com"}},
        ],
    )
    driver = _FakeDriver()
    llm = _ScriptedLLM(
        [{"tool": "finish", "args": {"status": "success", "summary": "ok"}}]
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = run_explorer(
        spec_id="spec-new",
        spec_name="signup",
        app_url="https://app.example",
        exploratory_goals=["Complete signup"],
        run_dir=run_dir,
        driver=driver,
        llm=llm,
        skills_dir=skills_dir,
    )

    assert result.ok is True
    # Driver actions: initial navigate, then click, then type, then no LLM action (finish).
    assert driver.actions[0] == ("navigate", ("https://app.example",))
    assert ("click", ("button.signup",)) in driver.actions
    assert ("type", ("#email", "demo@x.com")) in driver.actions
    # The first two recorded steps came from the skill replay.
    assert "replayed from skill" in result.steps[0].call.rationale
    assert "replayed from skill" in result.steps[1].call.rationale
    # LLM only got one turn (the finish).
    assert len(llm.calls) == 1


def test_skill_prefix_aborts_on_replay_failure(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _seed_skill(
        skills_dir,
        "app-example",
        goal="Complete signup",
        calls=[
            {"tool": "click", "args": {"selector": "button.signup"}},
            {"tool": "type", "args": {"selector": "#stale", "text": "x"}},
        ],
    )
    # First click succeeds, type fails — skill should abort and re-anchor.
    driver = _FakeDriver(fail_on={"type": 1})
    llm = _ScriptedLLM(
        [
            {"tool": "click", "args": {"selector": "button.fresh"}},
            {"tool": "finish", "args": {"status": "success", "summary": "ok"}},
        ]
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = run_explorer(
        spec_id="spec-new",
        spec_name="signup",
        app_url="https://app.example",
        exploratory_goals=["Complete signup"],
        run_dir=run_dir,
        driver=driver,
        llm=llm,
        skills_dir=skills_dir,
    )

    assert result.ok is True
    # Initial nav, partial skill (click ok, type fail), re-anchor nav, then LLM click + finish.
    nav_calls = [a for a in driver.actions if a[0] == "navigate"]
    assert len(nav_calls) == 2  # initial + re-anchor after stale skill aborted
    # Skill prefix discarded — only the LLM-driven click should be in successful_calls.
    assert ("click", ("button.fresh",)) in driver.actions


def test_load_skill_prefix_picks_goal_match_over_recency(tmp_path: Path) -> None:
    from retrace.explorer import load_skill_prefix

    skills_dir = tmp_path / "skills"
    older = _seed_skill(
        skills_dir,
        "app-example",
        goal="Complete signup",
        calls=[{"tool": "click", "args": {"selector": "a"}}],
    )
    # Touch the more-recent skill but with a goal mismatch.
    newer_path = (skills_dir / "app-example" / "newer.json")
    newer_path.write_text(
        json.dumps(
            {
                "spec_id": "spec-newer",
                "host": "app-example",
                "goal": "Edit profile picture",
                "steps": [{"tool": "click", "args": {"selector": "z"}}],
            }
        )
    )
    # Bump newer's mtime to be later.
    import os
    import time as _time

    _time.sleep(0.01)
    os.utime(newer_path, None)

    calls, source = load_skill_prefix(
        skills_dir,
        "https://app.example",
        goals=["Complete signup again"],
    )
    assert source == str(older)
    assert calls[0].args["selector"] == "a"
