"""Bounded LLM-driven exploratory tester engine (RET-21).

The explorer drives a browser via a small bounded tool surface and asks an
LLM, step by step, what to do next given the current observation. Successful
explorations persist a durable "skill" — a serialized sequence of the tool
calls that worked — so future runs against the same domain can replay or
extend the skill instead of rediscovering the path.

This module owns no Playwright code in tests: production uses `PlaywrightDriver`,
tests inject a `FakeDriver` that implements the same protocol.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# Bounded tool surface.  Anything not in this set is rejected before reaching
# the driver — keeps the agent honest and the action space auditable.
TOOL_SCHEMAS: dict[str, dict[str, list[str]]] = {
    "navigate": {"required": ["url"], "optional": []},
    "click": {"required": ["selector"], "optional": []},
    "type": {"required": ["selector", "text"], "optional": []},
    "press": {"required": ["key"], "optional": ["selector"]},
    "wait_for": {"required": ["selector"], "optional": ["timeout_ms"]},
    "snapshot": {"required": [], "optional": []},
    "finish": {"required": ["status"], "optional": ["summary"]},
}

ALLOWED_FINISH_STATUSES = {"success", "blocked", "needs_human", "abandoned"}

DEFAULT_MAX_STEPS = 20
DEFAULT_SNAPSHOT_TEXT_BUDGET = 6000


@dataclass
class ExplorerObservation:
    step_index: int
    url: str = ""
    title: str = ""
    snapshot_text: str = ""
    screenshot_path: str = ""
    console: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExplorerToolCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class ExplorerStep:
    index: int
    call: ExplorerToolCall
    observation: Optional[ExplorerObservation] = None
    error: str = ""
    ok: bool = True


@dataclass
class ExplorerResult:
    ok: bool
    finished: bool
    finish_status: str
    finish_summary: str
    steps: list[ExplorerStep]
    skill_path: str
    error: str
    artifacts: list[dict[str, Any]]


class BrowserDriver(Protocol):
    def navigate(self, url: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def type(self, selector: str, text: str) -> None: ...
    def press(self, key: str, selector: str = "") -> None: ...
    def wait_for(self, selector: str, timeout_ms: int = 5000) -> None: ...
    def screenshot(self, path: Path) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class LLMDriver(Protocol):
    def chat_json(self, *, system: str, user: str) -> dict[str, Any]: ...


# ----------------------------------------------------------------------------
# Tool-call validation
# ----------------------------------------------------------------------------


class ToolCallError(ValueError):
    pass


def parse_tool_call(payload: Any) -> ExplorerToolCall:
    """Parse and validate a tool-call payload from the LLM.

    Accepts the canonical `{"tool": ..., "args": {...}}` shape, plus a
    reasonable shorthand: top-level keys are treated as args when no `args`
    field is provided.  Always validates against TOOL_SCHEMAS; rejects
    anything outside the bounded surface.
    """
    if not isinstance(payload, dict):
        raise ToolCallError(f"tool call must be an object, got {type(payload).__name__}")
    tool = str(payload.get("tool") or "").strip()
    if not tool:
        raise ToolCallError("tool call missing 'tool' field")
    if tool not in TOOL_SCHEMAS:
        raise ToolCallError(
            f"unknown tool {tool!r}; allowed: {sorted(TOOL_SCHEMAS)}"
        )
    schema = TOOL_SCHEMAS[tool]
    raw_args = payload.get("args")
    if raw_args is None:
        # Shorthand: pull required/optional keys from the top level.
        raw_args = {
            k: payload[k]
            for k in (*schema["required"], *schema["optional"])
            if k in payload
        }
    if not isinstance(raw_args, dict):
        raise ToolCallError(f"tool args must be an object, got {type(raw_args).__name__}")
    missing = [k for k in schema["required"] if k not in raw_args]
    if missing:
        raise ToolCallError(f"tool {tool!r} missing required args: {missing}")
    rationale = str(payload.get("rationale") or "").strip()
    args = {k: raw_args[k] for k in raw_args if k in {*schema["required"], *schema["optional"]}}
    if tool == "finish":
        status = str(args.get("status") or "").strip().lower()
        if status not in ALLOWED_FINISH_STATUSES:
            raise ToolCallError(
                f"finish status must be one of {sorted(ALLOWED_FINISH_STATUSES)}; got {status!r}"
            )
        args["status"] = status
        args["summary"] = str(args.get("summary") or "").strip()
    return ExplorerToolCall(tool=tool, args=args, rationale=rationale)


# ----------------------------------------------------------------------------
# Driver execution
# ----------------------------------------------------------------------------


def _execute_tool(driver: BrowserDriver, call: ExplorerToolCall) -> None:
    if call.tool == "navigate":
        driver.navigate(str(call.args["url"]))
    elif call.tool == "click":
        driver.click(str(call.args["selector"]))
    elif call.tool == "type":
        driver.type(str(call.args["selector"]), str(call.args["text"]))
    elif call.tool == "press":
        driver.press(str(call.args["key"]), str(call.args.get("selector") or ""))
    elif call.tool == "wait_for":
        timeout_ms = int(call.args.get("timeout_ms") or 5000)
        driver.wait_for(str(call.args["selector"]), timeout_ms=timeout_ms)
    elif call.tool == "snapshot":
        # snapshot is a no-op; the loop always takes a fresh observation after
        # every tool call.  We keep the explicit tool so the model can request
        # an observation without altering page state.
        return
    elif call.tool == "finish":
        return
    else:  # pragma: no cover - parse_tool_call already rejects this
        raise ToolCallError(f"cannot execute unknown tool {call.tool!r}")


def _take_observation(
    *,
    driver: BrowserDriver,
    artifacts_dir: Path,
    step_index: int,
) -> ExplorerObservation:
    snapshot = driver.snapshot()
    text = str(snapshot.get("text") or "")
    if len(text) > DEFAULT_SNAPSHOT_TEXT_BUDGET:
        text = text[:DEFAULT_SNAPSHOT_TEXT_BUDGET] + "\n...[truncated]"
    screenshot_path = artifacts_dir / f"explore-step-{step_index:03d}.png"
    try:
        driver.screenshot(screenshot_path)
        screenshot_str = str(screenshot_path)
    except Exception as exc:
        logger.debug("screenshot failed at step %s: %s", step_index, exc)
        screenshot_str = ""
    console_raw = snapshot.get("console") or []
    console: list[dict[str, Any]] = [
        item for item in console_raw if isinstance(item, dict)
    ]
    return ExplorerObservation(
        step_index=step_index,
        url=str(snapshot.get("url") or ""),
        title=str(snapshot.get("title") or ""),
        snapshot_text=text,
        screenshot_path=screenshot_str,
        console=console,
    )


# ----------------------------------------------------------------------------
# LLM prompt assembly
# ----------------------------------------------------------------------------


_SYSTEM_PROMPT = """You are Retrace's exploratory UI tester.

You drive a real web browser via a small bounded tool surface.  After each tool
call you receive a fresh observation: URL, page title, accessible text snapshot
(truncated), and console events.  Decide the next tool call.

Rules:
- Respond with a single JSON object: {"tool": "...", "args": {...}, "rationale": "..."}.
- Allowed tools: navigate, click, type, press, wait_for, snapshot, finish.
- Use durable selectors (data-testid, aria-label, role, visible text). Avoid raw nth-child indexes.
- Stay focused on the goal. Do not browse outside the app under test.
- When the goal is satisfied, return {"tool": "finish", "args": {"status": "success", "summary": "..."}}.
- If the path is blocked (auth wall, 2FA, missing fixture), return finish with status="blocked".
- Never type real credentials. If credentials are required, finish with status="needs_human".
"""


def _build_user_prompt(
    *,
    spec_name: str,
    goals: list[str],
    app_url: str,
    history: list[ExplorerStep],
    observation: ExplorerObservation,
    skills_summary: str,
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
    if skills_summary.strip():
        lines.append("Known durable skills for this domain (use as a head-start):")
        lines.append(skills_summary)
        lines.append("")
    lines.append(f"Step {observation.step_index} observation:")
    lines.append(f"URL: {observation.url}")
    lines.append(f"Title: {observation.title}")
    if observation.console:
        lines.append("Recent console events:")
        for event in observation.console[-5:]:
            lines.append(f"- {event}")
    lines.append("")
    lines.append("Page snapshot (truncated):")
    lines.append(observation.snapshot_text or "<empty>")
    lines.append("")
    if history:
        lines.append("Previous steps:")
        for step in history[-6:]:
            lines.append(
                f"  [{step.index}] {step.call.tool}({_compact_args(step.call.args)})"
                + (f" -> error: {step.error}" if step.error else "")
            )
        lines.append("")
    lines.append("Reply with the next tool call as a single JSON object.")
    return "\n".join(lines)


def _compact_args(args: dict[str, Any]) -> str:
    items = []
    for k, v in args.items():
        rendered = str(v)
        if len(rendered) > 60:
            rendered = rendered[:57] + "..."
        items.append(f"{k}={rendered!r}")
    return ", ".join(items)


# ----------------------------------------------------------------------------
# Skills persistence
# ----------------------------------------------------------------------------


def _host_slug(url: str) -> str:
    host = urlparse(url).netloc or url
    host = host.split(":")[0]
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "unknown-host"


def load_skills_summary(skills_dir: Path, app_url: str) -> str:
    host = _host_slug(app_url)
    domain_dir = skills_dir / host
    if not domain_dir.is_dir():
        return ""
    summaries: list[str] = []
    for skill_file in sorted(domain_dir.glob("*.json")):
        try:
            data = json.loads(skill_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        goal = str(data.get("goal") or skill_file.stem)
        steps = data.get("steps") or []
        summaries.append(f"- {goal} ({len(steps)} steps): {skill_file.name}")
    return "\n".join(summaries[:10])


def load_skill_prefix(
    skills_dir: Path,
    app_url: str,
    *,
    goals: list[str] | None = None,
) -> tuple[list[ExplorerToolCall], str]:
    """Pick the most relevant durable skill for this domain and return its
    sequence of tool calls plus the source filename.

    "Most relevant" today is: the most recently modified skill in the
    host-slug directory whose `goal` shares a token with the active spec
    goals.  If none match by token, fall back to the most recent skill.
    Returns ([], "") when no skills exist for the domain.
    """
    host = _host_slug(app_url)
    domain_dir = skills_dir / host
    if not domain_dir.is_dir():
        return [], ""
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    goal_tokens = _goal_tokens(goals or [])
    for skill_file in domain_dir.glob("*.json"):
        try:
            data = json.loads(skill_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        candidates.append((skill_file.stat().st_mtime, skill_file, data))
    if not candidates:
        return [], ""
    candidates.sort(key=lambda c: -c[0])

    chosen: tuple[Path, dict[str, Any]] | None = None
    if goal_tokens:
        for _, path, data in candidates:
            skill_goal_tokens = _goal_tokens([str(data.get("goal") or "")])
            if goal_tokens & skill_goal_tokens:
                chosen = (path, data)
                break
    if chosen is None:
        chosen = (candidates[0][1], candidates[0][2])

    path, data = chosen
    raw_steps = data.get("steps") or []
    calls: list[ExplorerToolCall] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        try:
            calls.append(parse_tool_call(raw))
        except ToolCallError:
            # Stop at the first invalid step rather than silently dropping it;
            # half a skill is more dangerous than none.
            break
    return calls, str(path)


def _goal_tokens(goals: list[str]) -> set[str]:
    tokens: set[str] = set()
    for goal in goals:
        for tok in re.split(r"[^a-z0-9]+", str(goal).lower()):
            if len(tok) >= 3:
                tokens.add(tok)
    return tokens


def _persist_skill(
    *,
    skills_dir: Path,
    spec_id: str,
    app_url: str,
    goals: list[str],
    successful_calls: list[ExplorerToolCall],
    summary: str,
) -> str:
    if not successful_calls:
        return ""
    host = _host_slug(app_url)
    domain_dir = skills_dir / host
    domain_dir.mkdir(parents=True, exist_ok=True)
    skill_path = domain_dir / f"{spec_id}.json"
    payload = {
        "spec_id": spec_id,
        "app_url": app_url,
        "host": host,
        "goal": "; ".join(goals)[:300] if goals else summary[:300],
        "summary": summary,
        "steps": [asdict(call) for call in successful_calls],
    }
    skill_path.write_text(json.dumps(payload, indent=2) + "\n")
    return str(skill_path)


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------


def run_explorer(
    *,
    spec_id: str,
    spec_name: str,
    app_url: str,
    exploratory_goals: list[str],
    run_dir: Path,
    driver: BrowserDriver,
    llm: LLMDriver,
    skills_dir: Path,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> ExplorerResult:
    """Drive the bounded exploratory loop end-to-end.

    Caller supplies a live driver and llm; this function never touches Playwright
    directly so it stays test-friendly and re-targetable to other CDP backends.
    """
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    skills_summary = load_skills_summary(skills_dir, app_url)
    skill_prefix, prefix_source = load_skill_prefix(
        skills_dir, app_url, goals=exploratory_goals
    )

    # Open the app at app_url to give the model a starting frame of reference.
    try:
        driver.navigate(app_url)
    except Exception as exc:
        return ExplorerResult(
            ok=False,
            finished=False,
            finish_status="",
            finish_summary="",
            steps=[],
            skill_path="",
            error=f"initial navigate failed: {exc}",
            artifacts=[],
        )

    steps: list[ExplorerStep] = []
    successful_calls: list[ExplorerToolCall] = []
    finish_status = ""
    finish_summary = ""
    finished = False
    error = ""
    prefix_aborted = False

    try:
        # Phase 1: replay any durable skill prefix.  These steps are NOT
        # counted against max_steps because they came from a previously
        # validated success run; the LLM still gets the full budget afterward.
        for prefix_call in skill_prefix:
            if prefix_call.tool == "finish":
                # Don't let a prefix declare success before the LLM gets a turn.
                break
            try:
                _execute_tool(driver, prefix_call)
                steps.append(
                    ExplorerStep(
                        index=len(steps),
                        call=ExplorerToolCall(
                            tool=prefix_call.tool,
                            args=dict(prefix_call.args),
                            rationale=f"replayed from skill {prefix_source}",
                        ),
                        observation=None,
                        ok=True,
                    )
                )
                successful_calls.append(prefix_call)
            except Exception as exc:
                steps.append(
                    ExplorerStep(
                        index=len(steps),
                        call=ExplorerToolCall(
                            tool=prefix_call.tool,
                            args=dict(prefix_call.args),
                            rationale=f"replayed from skill {prefix_source}",
                        ),
                        observation=None,
                        error=f"skill replay failed: {exc}",
                        ok=False,
                    )
                )
                # Stale skill — drop the prefix and let the LLM start fresh.
                prefix_aborted = True
                successful_calls.clear()
                break

        if prefix_aborted:
            # Re-anchor the page so the LLM doesn't inherit a half-broken state.
            try:
                driver.navigate(app_url)
            except Exception:
                pass

        prefix_offset = len(steps)
        for llm_step in range(max_steps):
            step_index = prefix_offset + llm_step
            observation = _take_observation(
                driver=driver, artifacts_dir=artifacts_dir, step_index=step_index
            )
            user_prompt = _build_user_prompt(
                spec_name=spec_name,
                goals=exploratory_goals,
                app_url=app_url,
                history=steps,
                observation=observation,
                skills_summary=skills_summary,
            )
            try:
                raw = llm.chat_json(system=_SYSTEM_PROMPT, user=user_prompt)
            except Exception as exc:
                error = f"llm call failed at step {step_index}: {exc}"
                break

            try:
                call = parse_tool_call(raw)
            except ToolCallError as exc:
                steps.append(
                    ExplorerStep(
                        index=step_index,
                        call=ExplorerToolCall(tool="<invalid>", args={}, rationale=""),
                        observation=observation,
                        error=str(exc),
                        ok=False,
                    )
                )
                # Give the model one chance to recover by asking again on the next loop.
                # If two consecutive parse errors, abort.
                if (
                    len(steps) >= 2
                    and steps[-2].call.tool == "<invalid>"
                ):
                    error = f"two consecutive invalid tool calls; last: {exc}"
                    break
                continue

            if call.tool == "finish":
                steps.append(
                    ExplorerStep(
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
                    ExplorerStep(
                        index=step_index,
                        call=call,
                        observation=observation,
                        ok=True,
                    )
                )
                successful_calls.append(call)
            except Exception as exc:
                steps.append(
                    ExplorerStep(
                        index=step_index,
                        call=call,
                        observation=observation,
                        error=str(exc),
                        ok=False,
                    )
                )
                # Allow the model to recover; abort on three consecutive failures.
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
            logger.debug("driver close failed: %s", exc)

    skill_path = ""
    if finished and finish_status == "success":
        skill_path = _persist_skill(
            skills_dir=skills_dir,
            spec_id=spec_id,
            app_url=app_url,
            goals=exploratory_goals,
            successful_calls=successful_calls,
            summary=finish_summary,
        )

    artifacts = _build_artifacts(run_dir=run_dir, steps=steps, skill_path=skill_path)
    ok = finished and finish_status == "success" and not error
    return ExplorerResult(
        ok=ok,
        finished=finished,
        finish_status=finish_status,
        finish_summary=finish_summary,
        steps=steps,
        skill_path=skill_path,
        error=error,
        artifacts=artifacts,
    )


def _build_artifacts(
    *, run_dir: Path, steps: list[ExplorerStep], skill_path: str
) -> list[dict[str, Any]]:
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    trace_path = artifacts_dir / "explore-trace.json"
    trace_payload = {
        "step_count": len(steps),
        "steps": [
            {
                "index": s.index,
                "tool": s.call.tool,
                "args": s.call.args,
                "rationale": s.call.rationale,
                "ok": s.ok,
                "error": s.error,
                "observation": asdict(s.observation) if s.observation else None,
            }
            for s in steps
        ],
    }
    trace_path.write_text(json.dumps(trace_payload, indent=2) + "\n")
    artifacts: list[dict[str, Any]] = [
        {
            "artifact_id": "explore-trace",
            "artifact_type": "explore_trace",
            "path": str(trace_path),
            "label": "Exploratory tool-call trace",
            "metadata": {"step_count": len(steps)},
        }
    ]
    for step in steps:
        if step.observation and step.observation.screenshot_path:
            artifacts.append(
                {
                    "artifact_id": f"explore-screenshot-{step.index:03d}",
                    "artifact_type": "screenshot",
                    "path": step.observation.screenshot_path,
                    "label": f"Screenshot before step {step.index}",
                    "metadata": {
                        "url": step.observation.url,
                        "title": step.observation.title,
                    },
                }
            )
    if skill_path:
        artifacts.append(
            {
                "artifact_id": "explore-skill",
                "artifact_type": "skill",
                "path": skill_path,
                "label": "Durable skill from exploration",
                "metadata": {},
            }
        )
    return artifacts


# ----------------------------------------------------------------------------
# Production driver (Playwright)
# ----------------------------------------------------------------------------


def build_playwright_driver(
    *, browser_settings: dict[str, Any]
) -> "PlaywrightDriver":
    """Build a Playwright-backed driver.  Imports playwright lazily so the rest
    of the explorer module remains importable without the optional extra."""
    return PlaywrightDriver(browser_settings=browser_settings)


class PlaywrightDriver:
    """Playwright sync_api browser driver.  Lazily imported."""

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
        self._context = self._browser.new_context(
            viewport=viewport if isinstance(viewport, dict) else None
        )
        self._page = self._context.new_page()
        self._console: list[dict[str, Any]] = []
        self._page.on(
            "console",
            lambda msg: self._console.append({"type": msg.type, "text": msg.text}),
        )

    def navigate(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def click(self, selector: str) -> None:
        self._page.locator(selector).first.click()

    def type(self, selector: str, text: str) -> None:
        self._page.locator(selector).first.fill(text)

    def press(self, key: str, selector: str = "") -> None:
        if selector:
            self._page.locator(selector).first.press(key)
        else:
            self._page.keyboard.press(key)

    def wait_for(self, selector: str, timeout_ms: int = 5000) -> None:
        self._page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._page.screenshot(path=str(path), full_page=True)

    def snapshot(self) -> dict[str, Any]:
        try:
            text = self._page.locator("body").inner_text(timeout=3000)
        except Exception:
            text = ""
        console_drained = list(self._console)
        self._console.clear()
        return {
            "url": self._page.url,
            "title": self._page.title(),
            "text": text,
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
