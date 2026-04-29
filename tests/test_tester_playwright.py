"""Playwright-runner action and assertion coverage.

Skipped automatically when the playwright extra (or the chromium binary) is
not installed.  Run locally with `uv pip install playwright && playwright
install chromium` to exercise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        pytest.skip("chromium binary not available")
        return False


@pytest.fixture
def html_app(tmp_path: Path) -> str:
    """Serve a tiny static HTML file via file:// so we don't need an HTTP server."""
    html = tmp_path / "page.html"
    html.write_text(
        """
        <html><body>
            <h1>Welcome</h1>
            <a href="#target" id="link" data-testid="link">Click me</a>
            <input id="text" data-testid="text" />
            <select id="dropdown" data-testid="dropdown">
                <option value="a">A</option>
                <option value="b">B</option>
            </select>
            <button id="hover-btn" data-testid="hover-btn">Hover</button>
            <div id="results" class="result-row" data-testid="row">one</div>
            <div class="result-row">two</div>
            <div class="result-row">three</div>
            <script>
              setTimeout(() => {
                const el = document.createElement('div');
                el.id = 'late';
                el.textContent = 'now-visible';
                document.body.appendChild(el);
              }, 100);
            </script>
        </body></html>
        """
    )
    return f"file://{html}"


def _spec(*, app_url: str, steps, assertions, tmp_path: Path):
    from retrace.tester import (
        SPEC_SCHEMA_VERSION,
        TesterSpec,
    )

    return TesterSpec(
        schema_version=SPEC_SCHEMA_VERSION,
        spec_id="abc123",
        name="pw-test",
        mode="describe",
        prompt="",
        app_url=app_url,
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
        execution_engine="native",
        exact_steps=steps,
        assertions=assertions,
        browser_settings={"runtime": "playwright", "headless": True},
    )


def test_playwright_runner_handles_select_scroll_wait_for(
    chromium_available: bool, tmp_path: Path, html_app: str
) -> None:
    from retrace.tester import _run_playwright_spec

    spec = _spec(
        app_url=html_app,
        steps=[
            {"id": "open", "action": "get", "url": ""},
            {"id": "wait", "action": "wait_for", "selector": "#late", "timeout_ms": 3000},
            {"id": "pick", "action": "select", "selector": "#dropdown", "value": "b"},
            {"id": "scroll", "action": "scroll", "selector": "#hover-btn"},
        ],
        assertions=[
            {"type": "selector_visible", "selector": "#late"},
            {"type": "selector_count", "selector": ".result-row", "expected": 3},
            {"type": "url_contains", "expected": "page.html"},
        ],
        tmp_path=tmp_path,
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log_path = run_dir / "harness.log"
    exit_code, artifacts, assertion_results, error = _run_playwright_spec(
        spec=spec,
        app_url=html_app,
        run_dir=run_dir,
        log_path=log_path,
        steps=spec.exact_steps,
    )

    assert error == "", error
    assert exit_code == 0, assertion_results
    by_type = {a["assertion_type"]: a for a in assertion_results}
    assert by_type["selector_visible"]["ok"] is True
    assert by_type["selector_count"]["ok"] is True
    assert by_type["url_contains"]["ok"] is True


def test_playwright_runner_text_matches_regex(
    chromium_available: bool, tmp_path: Path, html_app: str
) -> None:
    from retrace.tester import _run_playwright_spec

    spec = _spec(
        app_url=html_app,
        steps=[{"id": "open", "action": "get", "url": ""}],
        assertions=[
            {"type": "text_matches", "expected": r"Welc[a-z]+"},
            {"type": "selector_text", "selector": "h1", "expected": "Welcome"},
        ],
        tmp_path=tmp_path,
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log_path = run_dir / "harness.log"
    exit_code, artifacts, assertion_results, error = _run_playwright_spec(
        spec=spec,
        app_url=html_app,
        run_dir=run_dir,
        log_path=log_path,
        steps=spec.exact_steps,
    )

    assert error == "", error
    assert exit_code == 0
    types = {a["assertion_type"] for a in assertion_results}
    assert "text_matches" in types
    assert "selector_text" in types
    assert all(a["ok"] for a in assertion_results)


def test_playwright_runner_invalid_regex_does_not_crash(
    chromium_available: bool, tmp_path: Path, html_app: str
) -> None:
    """Invalid regex must produce a failed assertion, not raise re.error."""
    from retrace.tester import _run_playwright_spec

    spec = _spec(
        app_url=html_app,
        steps=[{"id": "open", "action": "get", "url": ""}],
        assertions=[
            {"type": "text_matches", "expected": "(unclosed"},
        ],
        tmp_path=tmp_path,
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    log_path = run_dir / "harness.log"
    exit_code, artifacts, assertion_results, error = _run_playwright_spec(
        spec=spec,
        app_url=html_app,
        run_dir=run_dir,
        log_path=log_path,
        steps=spec.exact_steps,
    )

    # The spec ran to completion (no top-level error from a re.error crash);
    # the regex assertion failed with an explanatory error payload.
    assert error == "", error
    matches = [a for a in assertion_results if a["assertion_type"] == "text_matches"]
    assert len(matches) == 1
    assert matches[0]["ok"] is False
    assert "invalid_regex" in matches[0]["actual"].get("error", "")
    assert exit_code != 0
