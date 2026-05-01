from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from retrace.tester import (
    create_spec,
    run_spec,
    runs_dir_for_data_dir,
    set_visual_factories,
    specs_dir_for_data_dir,
)


@dataclass
class _FakeDriver:
    actions: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    closed: bool = False
    url: str = ""

    def __init__(self, *, browser_settings: dict[str, Any]) -> None:
        self.actions = []
        self.closed = False
        self.url = ""
        self.browser_settings = browser_settings

    def goto(self, url: str) -> None:
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
        path.write_bytes(b"\x89PNG\r\n\x1a\n")

    def page_state(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": "fixture",
            "viewport": {"width": 1280, "height": 800},
            "console": [],
        }

    def close(self) -> None:
        self.closed = True


class _FakeLLM:
    def __init__(self) -> None:
        self.responses = [
            {"tool": "click_at", "args": {"x": 10, "y": 20}},
            {"tool": "finish", "args": {"status": "success", "summary": "ok"}},
        ]
        self.images: list[str] = []

    def chat_visual_json(
        self, *, system: str, user: str, image_path: str
    ) -> dict[str, Any]:
        self.images.append(image_path)
        return self.responses.pop(0)


def test_run_spec_dispatches_visual_engine_through_factories(tmp_path: Path) -> None:
    fake_llm = _FakeLLM()
    set_visual_factories(
        driver_factory=lambda *, browser_settings: _FakeDriver(
            browser_settings=browser_settings
        ),
        llm_factory=lambda: fake_llm,
    )
    try:
        spec = create_spec(
            specs_dir=specs_dir_for_data_dir(tmp_path),
            name="Visual integration",
            prompt="",
            app_url="http://app.example",
            start_command="",
            harness_command="",
            execution_engine="visual",
            exploratory_goals=["Click the primary CTA"],
        )
        result = run_spec(spec=spec, runs_dir=runs_dir_for_data_dir(tmp_path))
    finally:
        set_visual_factories(driver_factory=None, llm_factory=None)

    assert result.ok is True
    assert result.execution_engine == "visual"
    assert any(
        item.get("artifact_type") == "visual_explore_trace" for item in result.artifacts
    )
    # The visual loop drove the LLM with an attached screenshot per step.
    assert all(Path(p).is_file() for p in fake_llm.images)
    # Run JSON should reflect the visual engine for downstream tooling.
    run_json = json.loads((Path(result.run_dir) / "run.json").read_text())
    assert run_json["execution_engine"] == "visual"
