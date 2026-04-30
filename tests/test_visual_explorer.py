from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from retrace.visual_explorer import (
    VisualToolCall,
    VisualToolCallError,
    parse_tool_call,
    run_visual_explorer,
)


# ---------- parse_tool_call --------------------------------------------------


def test_parse_tool_call_accepts_canonical_shape() -> None:
    call = parse_tool_call(
        {"tool": "click_at", "args": {"x": 100, "y": 200}, "rationale": "click submit"}
    )
    assert call.tool == "click_at"
    assert call.args == {"x": 100.0, "y": 200.0, "button": "left"}
    assert call.rationale == "click submit"


def test_parse_tool_call_accepts_shorthand_args_at_top_level() -> None:
    call = parse_tool_call({"tool": "scroll", "dy": 300})
    assert call.tool == "scroll"
    assert call.args == {"dx": 0, "dy": 300}


def test_parse_tool_call_rejects_unknown_tool() -> None:
    with pytest.raises(VisualToolCallError, match="unknown tool"):
        parse_tool_call({"tool": "execute_python", "args": {}})


def test_parse_tool_call_rejects_missing_required_args() -> None:
    with pytest.raises(VisualToolCallError, match="missing required args"):
        parse_tool_call({"tool": "click_at", "args": {"x": 1}})


def test_parse_tool_call_rejects_non_numeric_coords() -> None:
    with pytest.raises(VisualToolCallError, match="numeric"):
        parse_tool_call({"tool": "click_at", "args": {"x": "left", "y": 0}})


def test_parse_tool_call_rejects_unknown_mouse_button() -> None:
    with pytest.raises(VisualToolCallError, match="button"):
        parse_tool_call(
            {"tool": "click_at", "args": {"x": 1, "y": 1, "button": "xbutton1"}}
        )


def test_parse_tool_call_validates_finish_status() -> None:
    with pytest.raises(VisualToolCallError, match="finish status"):
        parse_tool_call({"tool": "finish", "args": {"status": "victory"}})


def test_parse_tool_call_clamps_wait_ms_to_30s() -> None:
    call = parse_tool_call({"tool": "wait_ms", "args": {"ms": 999_999}})
    assert call.args["ms"] == 30_000


# ---------- run_visual_explorer ---------------------------------------------


@dataclass
class FakeVisualDriver:
    """In-memory driver that records every call against the tool surface."""

    actions: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False
    url: str = ""
    title: str = "fixture"
    fail_on: str = ""

    def goto(self, url: str) -> None:
        if self.fail_on == "goto":
            raise RuntimeError("forced goto failure")
        self.url = url
        self.actions.append(("goto", {"url": url}))

    def click_at(self, x: float, y: float, button: str = "left") -> None:
        self.actions.append(("click_at", {"x": x, "y": y, "button": button}))

    def keyboard_type(self, text: str) -> None:
        self.actions.append(("keyboard_type", {"text": text}))

    def keyboard_press(self, key: str) -> None:
        self.actions.append(("keyboard_press", {"key": key}))

    def scroll(self, dx: int, dy: int) -> None:
        self.actions.append(("scroll", {"dx": dx, "dy": dy}))

    def wait_ms(self, ms: int) -> None:
        self.actions.append(("wait_ms", {"ms": ms}))

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Minimal valid PNG (1x1) so downstream tools that read the bytes
        # don't choke on an empty file.
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00"
            b"\x00\x00\x00IEND\xaeB`\x82"
        )

    def page_state(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "viewport": {"width": 1280, "height": 800},
            "console": [],
        }

    def close(self) -> None:
        self.closed = True


@dataclass
class ScriptedLLM:
    responses: list[dict[str, Any]]
    seen_image_paths: list[str] = field(default_factory=list)

    def chat_visual_json(
        self, *, system: str, user: str, image_path: str
    ) -> dict[str, Any]:
        self.seen_image_paths.append(image_path)
        if not self.responses:
            return {"tool": "finish", "args": {"status": "abandoned"}}
        return self.responses.pop(0)


def test_run_visual_explorer_executes_tool_sequence(tmp_path: Path) -> None:
    driver = FakeVisualDriver()
    llm = ScriptedLLM(
        responses=[
            {"tool": "click_at", "args": {"x": 50, "y": 60}},
            {"tool": "keyboard_type", "args": {"text": "hello"}},
            {"tool": "finish", "args": {"status": "success", "summary": "done"}},
        ]
    )

    result = run_visual_explorer(
        spec_id="spec-visual",
        spec_name="Visual smoke",
        app_url="http://app.example",
        exploratory_goals=["Click and type"],
        run_dir=tmp_path,
        driver=driver,
        llm=llm,
        max_steps=10,
    )

    assert result.ok is True
    assert result.finished is True
    assert result.finish_status == "success"
    # initial goto + click_at + keyboard_type
    actions = [a[0] for a in driver.actions]
    assert actions[:3] == ["goto", "click_at", "keyboard_type"]
    assert driver.closed is True
    # Each LLM call gets a screenshot path (3 LLM calls => 3 screenshots).
    assert len(llm.seen_image_paths) == 3
    for p in llm.seen_image_paths:
        assert Path(p).is_file(), p
    # Trace artifact is always written.
    trace_artifacts = [
        a for a in result.artifacts if a["artifact_type"] == "visual_explore_trace"
    ]
    assert len(trace_artifacts) == 1
    trace = json.loads(Path(trace_artifacts[0]["path"]).read_text())
    assert trace["step_count"] == len(result.steps)


def test_run_visual_explorer_fails_when_initial_goto_fails(tmp_path: Path) -> None:
    driver = FakeVisualDriver(fail_on="goto")
    llm = ScriptedLLM(responses=[])

    result = run_visual_explorer(
        spec_id="spec",
        spec_name="bad-init",
        app_url="http://app.example",
        exploratory_goals=["doesn't matter"],
        run_dir=tmp_path,
        driver=driver,
        llm=llm,
        max_steps=3,
    )

    assert result.ok is False
    assert result.finished is False
    assert "initial goto failed" in result.error
    # The LLM should NEVER have been consulted because we bailed pre-loop.
    assert llm.seen_image_paths == []


def test_run_visual_explorer_aborts_on_two_consecutive_invalid_tool_calls(
    tmp_path: Path,
) -> None:
    driver = FakeVisualDriver()
    # Both responses are invalid (unknown tool); after two strikes the loop
    # should give up rather than spam the LLM forever.
    llm = ScriptedLLM(
        responses=[
            {"tool": "execute_python", "args": {"code": "import os"}},
            {"tool": "another_unknown", "args": {}},
        ]
    )

    result = run_visual_explorer(
        spec_id="spec",
        spec_name="invalid-loop",
        app_url="http://app.example",
        exploratory_goals=["doesn't matter"],
        run_dir=tmp_path,
        driver=driver,
        llm=llm,
        max_steps=10,
    )

    assert result.ok is False
    assert "invalid tool calls" in result.error
    assert all(s.call.tool == "<invalid>" for s in result.steps)


def test_run_visual_explorer_handles_goto_via_address_bar_pattern(
    tmp_path: Path,
) -> None:
    """The visual loop's `goto` tool covers the address-bar pattern (RET-22 AC#3)."""
    driver = FakeVisualDriver()
    llm = ScriptedLLM(
        responses=[
            {"tool": "goto", "args": {"url": "http://app.example/login"}},
            {"tool": "finish", "args": {"status": "success"}},
        ]
    )

    result = run_visual_explorer(
        spec_id="spec",
        spec_name="goto-pattern",
        app_url="http://app.example",
        exploratory_goals=["Open the login page"],
        run_dir=tmp_path,
        driver=driver,
        llm=llm,
    )
    assert result.ok is True
    # Initial goto + the tool's goto = 2 entries.
    gotos = [a for a in driver.actions if a[0] == "goto"]
    assert [a[1]["url"] for a in gotos] == [
        "http://app.example",
        "http://app.example/login",
    ]
