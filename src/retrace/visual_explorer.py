"""Visual CUA-style execution mode for the Retrace tester (RET-22).

Runs a bounded screenshot+coordinate tool loop alongside the existing
accessibility-snapshot explorer.  Use this engine for apps where the a11y
tree is sparse, hostile (e.g. canvas-heavy), or unreliable — at the cost of
requiring a multimodal LLM and giving up step caching.

Design notes (Linear RET-22):
- Tools are intentionally low level (`click_at(x,y)`, `keyboard_type`,
  `keyboard_press`, `scroll`, `screenshot`, `wait_ms`) and the LLM gets a
  fresh screenshot before every decision.  No DOM selectors are exposed.
- An explicit `goto(url)` tool covers the address-bar pattern so the model
  doesn't try to click a chrome that Playwright cannot reach.
- Coordinate-based actions are NOT cached: pixel positions don't survive
  viewport, theme, or layout changes, and replaying them is unsafe.
- The system prompt enumerates the bounded tool surface and demands a single
  JSON response so the same parser used by the snapshot explorer works.

Provider/model requirements live in docs/visual-execution-mode.md — short
version: any vision-capable chat model (Claude 3.5+ Sonnet, GPT-4o, etc.)
that returns JSON.  Gateways that strip image content (some Bedrock proxies,
some "text-only" routers) will fail silently and must be skipped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


# Bounded tool surface.  Anything outside is rejected before reaching the
# driver — same contract as the snapshot explorer, just coord-shaped.
TOOL_SCHEMAS: dict[str, dict[str, list[str]]] = {
    "goto": {"required": ["url"], "optional": []},
    "click_at": {"required": ["x", "y"], "optional": ["button"]},
    "keyboard_type": {"required": ["text"], "optional": []},
    "keyboard_press": {"required": ["key"], "optional": []},
    "scroll": {"required": ["dy"], "optional": ["dx"]},
    "wait_ms": {"required": ["ms"], "optional": []},
    "screenshot": {"required": [], "optional": []},
    "finish": {"required": ["status"], "optional": ["summary"]},
}

ALLOWED_FINISH_STATUSES = {"success", "blocked", "needs_human", "abandoned"}
DEFAULT_MAX_STEPS = 20
ALLOWED_MOUSE_BUTTONS = {"left", "middle", "right"}


@dataclass
class VisualObservation:
    step_index: int
    url: str = ""
    title: str = ""
    screenshot_path: str = ""
    viewport: dict[str, int] = field(default_factory=dict)
    console: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class VisualToolCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class VisualStep:
    index: int
    call: VisualToolCall
    observation: Optional[VisualObservation] = None
    error: str = ""
    ok: bool = True


@dataclass
class VisualResult:
    ok: bool
    finished: bool
    finish_status: str
    finish_summary: str
    steps: list[VisualStep]
    error: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)


class VisualBrowserDriver(Protocol):
    """Coordinate-driven browser surface for the visual loop."""

    def goto(self, url: str) -> None: ...
    def click_at(self, x: float, y: float, button: str = "left") -> None: ...
    def keyboard_type(self, text: str) -> None: ...
    def keyboard_press(self, key: str) -> None: ...
    def scroll(self, dx: int, dy: int) -> None: ...
    def wait_ms(self, ms: int) -> None: ...
    def screenshot(self, path: Path) -> None: ...
    def page_state(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class VisualLLMDriver(Protocol):
    """Multimodal LLM contract: takes a screenshot path and returns tool JSON."""

    def chat_visual_json(
        self, *, system: str, user: str, image_path: str
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Tool-call validation (mirrors snapshot explorer's parse_tool_call)
# ---------------------------------------------------------------------------


class VisualToolCallError(ValueError):
    pass


def parse_tool_call(payload: Any) -> VisualToolCall:
    if not isinstance(payload, dict):
        raise VisualToolCallError(
            f"tool call must be an object, got {type(payload).__name__}"
        )
    tool = str(payload.get("tool") or "").strip()
    if not tool:
        raise VisualToolCallError("tool call missing 'tool' field")
    if tool not in TOOL_SCHEMAS:
        raise VisualToolCallError(
            f"unknown tool {tool!r}; allowed: {sorted(TOOL_SCHEMAS)}"
        )
    schema = TOOL_SCHEMAS[tool]
    raw_args = payload.get("args")
    if raw_args is None:
        raw_args = {
            k: payload[k]
            for k in (*schema["required"], *schema["optional"])
            if k in payload
        }
    if not isinstance(raw_args, dict):
        raise VisualToolCallError(
            f"tool args must be an object, got {type(raw_args).__name__}"
        )
    missing = [k for k in schema["required"] if k not in raw_args]
    if missing:
        raise VisualToolCallError(
            f"tool {tool!r} missing required args: {missing}"
        )
    rationale = str(payload.get("rationale") or "").strip()
    args = {
        k: raw_args[k]
        for k in raw_args
        if k in {*schema["required"], *schema["optional"]}
    }
    if tool == "finish":
        status = str(args.get("status") or "").strip().lower()
        if status not in ALLOWED_FINISH_STATUSES:
            raise VisualToolCallError(
                f"finish status must be one of {sorted(ALLOWED_FINISH_STATUSES)}; "
                f"got {status!r}"
            )
        args["status"] = status
        args["summary"] = str(args.get("summary") or "").strip()
    if tool == "click_at":
        try:
            args["x"] = float(args["x"])
            args["y"] = float(args["y"])
        except (TypeError, ValueError) as exc:
            raise VisualToolCallError(f"click_at coords must be numeric: {exc}") from exc
        button = str(args.get("button") or "left").strip().lower()
        if button not in ALLOWED_MOUSE_BUTTONS:
            raise VisualToolCallError(
                f"click_at button must be one of {sorted(ALLOWED_MOUSE_BUTTONS)}; "
                f"got {button!r}"
            )
        args["button"] = button
    if tool == "scroll":
        try:
            args["dy"] = int(args["dy"])
            args["dx"] = int(args.get("dx", 0))
        except (TypeError, ValueError) as exc:
            raise VisualToolCallError(f"scroll deltas must be ints: {exc}") from exc
    if tool == "wait_ms":
        try:
            args["ms"] = max(0, min(int(args["ms"]), 30000))
        except (TypeError, ValueError) as exc:
            raise VisualToolCallError(f"wait_ms ms must be an int: {exc}") from exc
    if tool == "keyboard_type":
        args["text"] = str(args["text"])
    if tool == "keyboard_press":
        args["key"] = str(args["key"]).strip()
        if not args["key"]:
            raise VisualToolCallError("keyboard_press requires a non-empty key")
    return VisualToolCall(tool=tool, args=args, rationale=rationale)


# ---------------------------------------------------------------------------
# Driver execution
# ---------------------------------------------------------------------------


def _execute_tool(driver: VisualBrowserDriver, call: VisualToolCall) -> None:
    if call.tool == "goto":
        driver.goto(str(call.args["url"]))
    elif call.tool == "click_at":
        driver.click_at(
            float(call.args["x"]),
            float(call.args["y"]),
            button=str(call.args.get("button") or "left"),
        )
    elif call.tool == "keyboard_type":
        driver.keyboard_type(str(call.args["text"]))
    elif call.tool == "keyboard_press":
        driver.keyboard_press(str(call.args["key"]))
    elif call.tool == "scroll":
        driver.scroll(int(call.args.get("dx", 0)), int(call.args["dy"]))
    elif call.tool == "wait_ms":
        driver.wait_ms(int(call.args["ms"]))
    elif call.tool == "screenshot":
        # Observations always include a fresh screenshot, so this is a
        # deliberate no-op — kept in the surface so the model can ask for a
        # new frame after waiting on async UI without changing state.
        return
    elif call.tool == "finish":
        return
    else:  # pragma: no cover - parse_tool_call rejects unknown tools
        raise VisualToolCallError(f"cannot execute unknown tool {call.tool!r}")


def _take_observation(
    *,
    driver: VisualBrowserDriver,
    artifacts_dir: Path,
    step_index: int,
) -> VisualObservation:
    state = driver.page_state()
    screenshot_path = artifacts_dir / f"visual-step-{step_index:03d}.png"
    try:
        driver.screenshot(screenshot_path)
        screenshot_str = str(screenshot_path)
    except Exception as exc:
        logger.debug("visual screenshot failed at step %s: %s", step_index, exc)
        screenshot_str = ""
    console_raw = state.get("console") or []
    console: list[dict[str, Any]] = [
        item for item in console_raw if isinstance(item, dict)
    ]
    viewport = state.get("viewport")
    return VisualObservation(
        step_index=step_index,
        url=str(state.get("url") or ""),
        title=str(state.get("title") or ""),
        screenshot_path=screenshot_str,
        viewport=viewport if isinstance(viewport, dict) else {},
        console=console,
    )


# ---------------------------------------------------------------------------
# LLM prompt assembly
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """You are Retrace's visual UI tester.

You drive a real web browser visually.  Before every decision you receive a
fresh screenshot of the page plus the current URL, title, viewport, and
recent console events.  You decide the next tool call.

Bounded tool surface:
- goto(url)            navigate via the address bar
- click_at(x, y)       low-level mouse click at pixel coordinates
- keyboard_type(text)  type text into whatever currently has focus
- keyboard_press(key)  press a single key (e.g. Enter, Tab, Escape)
- scroll(dx, dy)       wheel scroll the viewport
- wait_ms(ms)          pause for async UI (max 30000)
- screenshot()         request a fresh frame without changing state
- finish(status)       end the run; status in {success, blocked, needs_human, abandoned}

Rules:
- Respond with a single JSON object: {"tool": "...", "args": {...}, "rationale": "..."}.
- Coordinates are CSS pixels relative to the viewport top-left.
- Don't guess coordinates from memory: read them off the latest screenshot.
- Use goto() for full-URL navigation rather than clicking the address bar.
- Don't type real credentials.  If the path requires them, finish needs_human.
- Stay focused on the goal.  Don't browse outside the app under test.
- When the goal is satisfied, finish with status="success" and a summary.
"""


def _build_user_prompt(
    *,
    spec_name: str,
    goals: list[str],
    app_url: str,
    history: list[VisualStep],
    observation: VisualObservation,
) -> str:
    lines: list[str] = []
    lines.append(f"Spec: {spec_name}")
    lines.append(f"App URL: {app_url}")
    lines.append("")
    if goals:
        lines.append("Exploratory goals:")
        for g in goals:
            lines.append(f"- {g}")
        lines.append("")
    lines.append(f"Step {observation.step_index} observation:")
    lines.append(f"URL: {observation.url}")
    lines.append(f"Title: {observation.title}")
    if observation.viewport:
        lines.append(
            f"Viewport: {observation.viewport.get('width', 0)}x"
            f"{observation.viewport.get('height', 0)}"
        )
    if observation.console:
        lines.append("Recent console events:")
        for event in observation.console[-5:]:
            lines.append(f"- {event}")
    lines.append("")
    if history:
        lines.append("Previous steps:")
        for step in history[-6:]:
            lines.append(
                f"  [{step.index}] {step.call.tool}({_compact_args(step.call.args)})"
                + (f" -> error: {step.error}" if step.error else "")
            )
        lines.append("")
    lines.append("The screenshot for this step is attached.  Reply with the next tool call.")
    return "\n".join(lines)


def _compact_args(args: dict[str, Any]) -> str:
    items = []
    for k, v in args.items():
        rendered = str(v)
        if len(rendered) > 60:
            rendered = rendered[:57] + "..."
        items.append(f"{k}={rendered!r}")
    return ", ".join(items)


# ---------------------------------------------------------------------------
# Loop driver
# ---------------------------------------------------------------------------


def run_visual_explorer(
    *,
    spec_id: str,
    spec_name: str,
    app_url: str,
    exploratory_goals: list[str],
    run_dir: Path,
    driver: VisualBrowserDriver,
    llm: VisualLLMDriver,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> VisualResult:
    """Drive the bounded visual tool loop end-to-end.

    Caller supplies a live driver and llm; nothing in this function imports
    Playwright directly so it stays test-friendly and re-targetable.
    """
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    try:
        driver.goto(app_url)
    except Exception as exc:
        return VisualResult(
            ok=False,
            finished=False,
            finish_status="",
            finish_summary="",
            steps=[],
            error=f"initial goto failed: {exc}",
            artifacts=[],
        )

    steps: list[VisualStep] = []
    finish_status = ""
    finish_summary = ""
    finished = False
    error = ""

    try:
        for step_index in range(max_steps):
            observation = _take_observation(
                driver=driver,
                artifacts_dir=artifacts_dir,
                step_index=step_index,
            )
            user_prompt = _build_user_prompt(
                spec_name=spec_name,
                goals=exploratory_goals,
                app_url=app_url,
                history=steps,
                observation=observation,
            )
            try:
                raw = llm.chat_visual_json(
                    system=_SYSTEM_PROMPT,
                    user=user_prompt,
                    image_path=observation.screenshot_path,
                )
            except Exception as exc:
                error = f"llm call failed at step {step_index}: {exc}"
                break

            try:
                call = parse_tool_call(raw)
            except VisualToolCallError as exc:
                steps.append(
                    VisualStep(
                        index=step_index,
                        call=VisualToolCall(tool="<invalid>", args={}, rationale=""),
                        observation=observation,
                        error=str(exc),
                        ok=False,
                    )
                )
                # Two consecutive parse errors → give up.
                if (
                    len(steps) >= 2
                    and steps[-2].call.tool == "<invalid>"
                ):
                    error = f"two consecutive invalid tool calls; last: {exc}"
                    break
                continue

            if call.tool == "finish":
                steps.append(
                    VisualStep(
                        index=step_index,
                        call=call,
                        observation=observation,
                        ok=True,
                    )
                )
                finish_status = str(call.args.get("status") or "")
                finish_summary = str(call.args.get("summary") or "")
                finished = True
                break

            try:
                _execute_tool(driver, call)
                steps.append(
                    VisualStep(
                        index=step_index,
                        call=call,
                        observation=observation,
                        ok=True,
                    )
                )
            except Exception as exc:
                steps.append(
                    VisualStep(
                        index=step_index,
                        call=call,
                        observation=observation,
                        error=str(exc),
                        ok=False,
                    )
                )
                tail = steps[-3:]
                if len(tail) == 3 and all(not s.ok for s in tail):
                    error = f"three consecutive driver failures; last: {exc}"
                    break
        else:
            error = f"exhausted step budget ({max_steps}) without finishing"
    finally:
        try:
            driver.close()
        except Exception as exc:
            logger.debug("visual driver close failed: %s", exc)

    artifacts = _build_artifacts(run_dir=run_dir, steps=steps, spec_id=spec_id)
    ok = finished and finish_status == "success" and not error
    return VisualResult(
        ok=ok,
        finished=finished,
        finish_status=finish_status,
        finish_summary=finish_summary,
        steps=steps,
        error=error,
        artifacts=artifacts,
    )


def _build_artifacts(
    *, run_dir: Path, steps: list[VisualStep], spec_id: str
) -> list[dict[str, Any]]:
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    trace_path = artifacts_dir / "visual-trace.json"
    trace_payload = {
        "spec_id": spec_id,
        "step_count": len(steps),
        "steps": [
            {
                "index": s.index,
                "ok": s.ok,
                "error": s.error,
                "call": asdict(s.call),
                "observation": asdict(s.observation) if s.observation else None,
            }
            for s in steps
        ],
    }
    trace_path.write_text(json.dumps(trace_payload, indent=2) + "\n")
    artifacts: list[dict[str, Any]] = [
        {
            "artifact_id": "visual-trace",
            "artifact_type": "visual_explore_trace",
            "path": str(trace_path),
            "label": "Visual exploration trace",
            "metadata": {"step_count": len(steps)},
        }
    ]
    for step in steps:
        if step.observation and step.observation.screenshot_path:
            artifacts.append(
                {
                    "artifact_id": f"visual-screenshot-{step.index:03d}",
                    "artifact_type": "visual_screenshot",
                    "path": step.observation.screenshot_path,
                    "label": f"Screenshot before step {step.index}",
                    "metadata": {
                        "step_index": step.index,
                        "url": step.observation.url,
                    },
                }
            )
    return artifacts


# ---------------------------------------------------------------------------
# Production driver (Playwright)
# ---------------------------------------------------------------------------


def build_playwright_visual_driver(
    *, browser_settings: dict[str, Any]
) -> "PlaywrightVisualDriver":
    """Build the Playwright visual driver.  Imports playwright lazily."""
    return PlaywrightVisualDriver(browser_settings=browser_settings)


class PlaywrightVisualDriver:
    """Playwright sync_api driver for the visual tool surface."""

    def __init__(self, *, browser_settings: dict[str, Any]) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright extra not installed; install with `pip install retrace[browser]`"
            ) from exc
        self._pw_ctx = sync_playwright().start()
        browser_name = str(browser_settings.get("browser") or "chromium")
        browser_type = getattr(self._pw_ctx, browser_name)
        self._browser = browser_type.launch(
            headless=bool(browser_settings.get("headless", True))
        )
        viewport = browser_settings.get("viewport")
        if not isinstance(viewport, dict):
            viewport = {"width": 1280, "height": 800}
        self._viewport = {
            "width": int(viewport.get("width") or 1280),
            "height": int(viewport.get("height") or 800),
        }
        self._context = self._browser.new_context(viewport=self._viewport)
        self._page = self._context.new_page()
        self._console: list[dict[str, Any]] = []
        self._page.on(
            "console",
            lambda msg: self._console.append({"type": msg.type, "text": msg.text}),
        )

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def click_at(self, x: float, y: float, button: str = "left") -> None:
        self._page.mouse.click(float(x), float(y), button=button)

    def keyboard_type(self, text: str) -> None:
        self._page.keyboard.type(text)

    def keyboard_press(self, key: str) -> None:
        self._page.keyboard.press(key)

    def scroll(self, dx: int, dy: int) -> None:
        self._page.mouse.wheel(int(dx), int(dy))

    def wait_ms(self, ms: int) -> None:
        self._page.wait_for_timeout(int(ms))

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._page.screenshot(path=str(path), full_page=False)

    def page_state(self) -> dict[str, Any]:
        console_drained = list(self._console)
        self._console.clear()
        return {
            "url": self._page.url,
            "title": self._page.title(),
            "viewport": dict(self._viewport),
            "console": console_drained,
        }

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            try:
                self._browser.close()
            finally:
                try:
                    self._pw_ctx.stop()
                except Exception:
                    pass
