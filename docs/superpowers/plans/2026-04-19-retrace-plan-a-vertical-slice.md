# Retrace Plan A — Vertical Slice (v0.1-alpha) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an end-to-end vertical slice of Retrace: `retrace run` pulls session recordings from PostHog, extracts 3 signal types, sends flagged sessions to an OpenAI-compatible LLM, and writes a dated markdown bug report.

**Architecture:** Linear pipeline (`ingester → detectors → llm → sink`) where each stage is a pure function over the previous stage's output. SQLite caches raw session events + run history; flat YAML + `.env` for config. No clustering in Plan A — each flagged session produces one finding.

**Tech Stack:** Python 3.11+, `uv` for env management, `httpx` for HTTP, `pydantic` v2 for config/models, `click` for CLI, `pytest` for tests, stdlib `sqlite3` for storage. Target LLM interface is OpenAI-compatible `/v1/chat/completions`.

---

## File Structure

```
retrace/
├── pyproject.toml
├── .gitignore
├── .env.example
├── config.example.yaml
├── README.md
├── src/retrace/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── storage.py
│   ├── ingester.py
│   ├── pipeline.py
│   ├── detectors/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── console_error.py
│   │   ├── network_5xx.py
│   │   └── rage_click.py
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   └── analyst.py
│   └── sinks/
│       ├── __init__.py
│       ├── base.py
│       └── markdown.py
└── tests/
    ├── fixtures/
    │   ├── __init__.py
    │   ├── events.py          # helpers that build rrweb event streams
    │   ├── session_console_error.json
    │   ├── session_network_5xx.json
    │   └── session_rage_click.json
    ├── test_config.py
    ├── test_storage.py
    ├── test_ingester.py
    ├── test_detectors/
    │   ├── __init__.py
    │   ├── test_console_error.py
    │   ├── test_network_5xx.py
    │   └── test_rage_click.py
    ├── test_llm_client.py
    ├── test_llm_analyst.py
    ├── test_markdown_sink.py
    └── test_pipeline.py
```

**File responsibilities:**

- `config.py` — loads `config.yaml` + `.env` into typed `RetraceConfig` model.
- `storage.py` — SQLite DAL. Owns schema + all CRUD for sessions, signals, findings, runs.
- `ingester.py` — PostHog HTTP client; fetches recordings + snapshots, writes raw JSON to disk and metadata to SQLite.
- `detectors/base.py` — `Signal` dataclass + `Detector` protocol + registry.
- `detectors/*.py` — one file per detector, each exporting a `detect(events) -> list[Signal]` function registered on import.
- `llm/client.py` — thin OpenAI-compatible chat-completions wrapper with retries and JSON-mode support.
- `llm/analyst.py` — turns a flagged session (events + signals) into a `Finding` via the LLM.
- `sinks/base.py` — `Sink` protocol + `Finding` dataclass (shared output shape).
- `sinks/markdown.py` — writes `reports/YYYY-MM-DD-HHMM.md`.
- `pipeline.py` — orchestrates: ingest → extract signals → for flagged → LLM → sink.
- `cli.py` — `click` entrypoint; Plan A only ships `retrace run`.

---

## Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `config.example.yaml`
- Create: `src/retrace/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Initialize git and create `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
dist/
build/
*.egg-info/
.env
data/
reports/
.DS_Store
```

Run: `git init && git add .gitignore && git commit -m "chore: initial gitignore"`

- [ ] **Step 2: Create `pyproject.toml`**

```toml
[project]
name = "retrace"
version = "0.1.0a1"
description = "Your real users are your QA team. Retrace finds the bugs they hit."
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "pydantic>=2.5",
    "pydantic-settings>=2.1",
    "click>=8.1",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
]

[project.scripts]
retrace = "retrace.cli:main"

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-httpx>=0.30",
    "ruff>=0.5",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/retrace"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 3: Create `src/retrace/__init__.py` and `tests/__init__.py`**

```python
# src/retrace/__init__.py
__version__ = "0.1.0a1"
```

```python
# tests/__init__.py
```

- [ ] **Step 4: Install deps with uv**

Run: `uv venv && uv pip install -e ".[dev]"`
Expected: no errors, venv at `.venv/`.

- [ ] **Step 5: Verify pytest collects**

Run: `uv run pytest --collect-only`
Expected: no errors; "collected 0 items" is fine.

- [ ] **Step 6: Create `.env.example` and `config.example.yaml`**

```bash
# .env.example
RETRACE_POSTHOG_API_KEY=phx_your_key_here
RETRACE_LLM_API_KEY=
```

```yaml
# config.example.yaml
posthog:
  host: https://us.i.posthog.com
  project_id: "12345"

llm:
  base_url: http://localhost:8080/v1
  model: llama-3.1-8b-instruct

run:
  lookback_hours: 6
  max_sessions_per_run: 50
  output_dir: ./reports
  data_dir: ./data

detectors:
  console_error: true
  network_5xx: true
  rage_click: true
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/retrace/__init__.py tests/__init__.py .env.example config.example.yaml
git commit -m "chore: project scaffolding"
```

---

## Task 2: Config Module

**Files:**
- Create: `src/retrace/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import os
from pathlib import Path
import textwrap

from retrace.config import RetraceConfig, load_config


def test_load_config_merges_yaml_and_env(tmp_path: Path, monkeypatch):
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        textwrap.dedent(
            """
            posthog:
              host: https://eu.i.posthog.com
              project_id: "42"
            llm:
              base_url: http://localhost:8080/v1
              model: llama-3.1-8b-instruct
            run:
              lookback_hours: 3
              max_sessions_per_run: 25
              output_dir: ./reports
              data_dir: ./data
            detectors:
              console_error: true
              network_5xx: true
              rage_click: false
            """
        )
    )
    monkeypatch.setenv("RETRACE_POSTHOG_API_KEY", "phx_test")
    monkeypatch.setenv("RETRACE_LLM_API_KEY", "")

    cfg = load_config(config_yaml)

    assert isinstance(cfg, RetraceConfig)
    assert cfg.posthog.host == "https://eu.i.posthog.com"
    assert cfg.posthog.project_id == "42"
    assert cfg.posthog.api_key == "phx_test"
    assert cfg.llm.base_url == "http://localhost:8080/v1"
    assert cfg.llm.api_key is None
    assert cfg.run.lookback_hours == 3
    assert cfg.detectors.rage_click is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'RetraceConfig'`.

- [ ] **Step 3: Implement `config.py`**

```python
# src/retrace/config.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl


class PostHogConfig(BaseModel):
    host: str
    project_id: str
    api_key: str


class LLMConfig(BaseModel):
    base_url: str
    model: str
    api_key: Optional[str] = None
    timeout_seconds: int = 120


class RunConfig(BaseModel):
    lookback_hours: int = 6
    max_sessions_per_run: int = 50
    output_dir: Path = Path("./reports")
    data_dir: Path = Path("./data")


class DetectorsConfig(BaseModel):
    console_error: bool = True
    network_5xx: bool = True
    rage_click: bool = True


class RetraceConfig(BaseModel):
    posthog: PostHogConfig
    llm: LLMConfig
    run: RunConfig = Field(default_factory=RunConfig)
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)


def load_config(path: Path) -> RetraceConfig:
    load_dotenv(override=False)
    raw = yaml.safe_load(Path(path).read_text()) or {}

    posthog_key_env = os.environ.get("RETRACE_POSTHOG_API_KEY")
    if posthog_key_env:
        raw.setdefault("posthog", {})["api_key"] = posthog_key_env
    elif "api_key" not in raw.setdefault("posthog", {}):
        raw["posthog"]["api_key"] = ""

    llm_key_env = os.environ.get("RETRACE_LLM_API_KEY")
    if llm_key_env:
        raw.setdefault("llm", {})["api_key"] = llm_key_env

    return RetraceConfig.model_validate(raw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrace/config.py tests/test_config.py
git commit -m "feat(config): load config.yaml + .env into typed model"
```

---

## Task 3: Storage Module

**Files:**
- Create: `src/retrace/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage.py
from datetime import datetime, timezone
from pathlib import Path

from retrace.storage import Storage, SessionMeta


def test_storage_round_trips_session_meta(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    session = SessionMeta(
        id="sess-abc",
        project_id="42",
        started_at=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
        duration_ms=60_000,
        distinct_id="user-1",
        event_count=123,
    )
    store.upsert_session(session)

    out = store.get_session("sess-abc")
    assert out == session


def test_storage_last_cursor_defaults_to_none_then_persists(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    assert store.get_last_run_cursor() is None

    ts = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    store.set_last_run_cursor(ts)
    assert store.get_last_run_cursor() == ts


def test_storage_start_and_finish_run(tmp_path: Path):
    store = Storage(tmp_path / "retrace.db")
    store.init_schema()

    run_id = store.start_run()
    assert isinstance(run_id, int) and run_id > 0

    store.finish_run(run_id, sessions_scanned=5, findings_count=2, status="ok")
    row = store.get_run(run_id)
    assert row.sessions_scanned == 5
    assert row.findings_count == 2
    assert row.status == "ok"
    assert row.finished_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `storage.py`**

```python
# src/retrace/storage.py
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    distinct_id TEXT,
    event_count INTEGER NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sessions_scanned INTEGER DEFAULT 0,
    findings_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',
    error TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class SessionMeta:
    id: str
    project_id: str
    started_at: datetime
    duration_ms: int
    distinct_id: Optional[str]
    event_count: int


@dataclass
class RunRow:
    id: int
    started_at: datetime
    finished_at: Optional[datetime]
    sessions_scanned: int
    findings_count: int
    status: str
    error: Optional[str]


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def upsert_session(self, s: SessionMeta) -> None:
        if s.started_at.tzinfo is None:
            raise ValueError("SessionMeta.started_at must be timezone-aware")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, project_id, started_at, duration_ms, distinct_id, event_count)
                VALUES (:id, :project_id, :started_at, :duration_ms, :distinct_id, :event_count)
                ON CONFLICT(id) DO UPDATE SET
                    duration_ms = excluded.duration_ms,
                    event_count = excluded.event_count
                """,
                {**asdict(s), "started_at": s.started_at.isoformat()},
            )

    def get_session(self, sid: str) -> Optional[SessionMeta]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, project_id, started_at, duration_ms, distinct_id, event_count FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
        if not row:
            return None
        return SessionMeta(
            id=row["id"],
            project_id=row["project_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            duration_ms=row["duration_ms"],
            distinct_id=row["distinct_id"],
            event_count=row["event_count"],
        )

    def get_last_run_cursor(self) -> Optional[datetime]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_run_cursor'"
            ).fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row["value"])

    def set_last_run_cursor(self, ts: datetime) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO meta (key, value) VALUES ('last_run_cursor', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (ts.isoformat(),),
            )

    def start_run(self) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at) VALUES (?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            rid = cur.lastrowid
            assert rid is not None
            return rid

    def finish_run(
        self,
        run_id: int,
        *,
        sessions_scanned: int,
        findings_count: int,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE runs SET finished_at = ?, sessions_scanned = ?, findings_count = ?, status = ?, error = ?
                WHERE id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    sessions_scanned,
                    findings_count,
                    status,
                    error,
                    run_id,
                ),
            )

    def get_run(self, run_id: int) -> Optional[RunRow]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return RunRow(
            id=row["id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            sessions_scanned=row["sessions_scanned"],
            findings_count=row["findings_count"],
            status=row["status"],
            error=row["error"],
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrace/storage.py tests/test_storage.py
git commit -m "feat(storage): SQLite DAL for sessions, runs, and cursor"
```

---

## Task 4: Detector Protocol & Registry

**Files:**
- Create: `src/retrace/detectors/__init__.py`
- Create: `src/retrace/detectors/base.py`
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/events.py`

- [ ] **Step 1: Create fixtures helper (no tests yet — helper is used by detector tests)**

```python
# tests/fixtures/__init__.py
```

```python
# tests/fixtures/events.py
"""
Builders for minimal rrweb-shaped event streams.

rrweb event types we care about:
  2 = FullSnapshot
  3 = IncrementalSnapshot (source 2 = MouseInteraction, source 5 = Input)
  6 = Plugin (console, network)
"""
from __future__ import annotations

from typing import Any


def meta(ts: int = 0, href: str = "https://example.com/") -> dict[str, Any]:
    return {"type": 4, "timestamp": ts, "data": {"href": href}}


def console_event(ts: int, level: str, message: str) -> dict[str, Any]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "rrweb/console@1",
            "payload": {"level": level, "payload": [message], "trace": []},
        },
    }


def network_event(
    ts: int, url: str, status: int, method: str = "GET"
) -> dict[str, Any]:
    return {
        "type": 6,
        "timestamp": ts,
        "data": {
            "plugin": "posthog/network@1",
            "payload": {
                "method": method,
                "url": url,
                "status_code": status,
            },
        },
    }


def click_event(ts: int, x: int, y: int, target_id: int = 42) -> dict[str, Any]:
    return {
        "type": 3,
        "timestamp": ts,
        "data": {"source": 2, "type": 2, "id": target_id, "x": x, "y": y},
    }
```

- [ ] **Step 2: Write the failing test for the registry**

```python
# tests/test_detectors/__init__.py
```

```python
# tests/test_detectors/test_registry.py
from retrace.detectors import all_detectors, get_detector


def test_registry_lists_enabled_detectors():
    # Assumes at least console_error is registered once its module is imported.
    import retrace.detectors.console_error  # noqa: F401 (side-effect registration)
    names = [d.name for d in all_detectors()]
    assert "console_error" in names


def test_get_detector_returns_by_name():
    import retrace.detectors.console_error  # noqa: F401
    d = get_detector("console_error")
    assert d is not None
    assert d.name == "console_error"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_detectors/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: retrace.detectors.console_error` or `ImportError` on base.

- [ ] **Step 4: Implement `base.py` and `__init__.py`**

```python
# src/retrace/detectors/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class Signal:
    session_id: str
    detector: str
    timestamp_ms: int
    url: str
    details: dict[str, Any] = field(default_factory=dict)


class Detector(Protocol):
    name: str

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]: ...


_REGISTRY: dict[str, Detector] = {}


def register(detector: Detector) -> Detector:
    if detector.name in _REGISTRY:
        raise ValueError(f"detector {detector.name!r} already registered")
    _REGISTRY[detector.name] = detector
    return detector


def all_detectors() -> list[Detector]:
    return list(_REGISTRY.values())


def get_detector(name: str) -> Detector | None:
    return _REGISTRY.get(name)
```

```python
# src/retrace/detectors/__init__.py
from retrace.detectors.base import (
    Detector,
    Signal,
    all_detectors,
    get_detector,
    register,
)

__all__ = ["Detector", "Signal", "all_detectors", "get_detector", "register"]
```

Note: individual detector modules register themselves on import. Task 5 creates the first (`console_error.py`) — until then the registry test will skip via `# noqa: F401` but fail the `in names` assertion. So run the registry test only after Task 5.

Leave registry test marked `@pytest.mark.skip(reason="depends on Task 5")` for now to keep `pytest` green:

```python
# tests/test_detectors/test_registry.py
import pytest

pytest.skip("enabled after console_error detector lands in Task 5", allow_module_level=True)

# ... rest of file unchanged
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest -v`
Expected: previous tests PASS; registry tests SKIPPED.

- [ ] **Step 6: Commit**

```bash
git add src/retrace/detectors tests/fixtures tests/test_detectors
git commit -m "feat(detectors): base protocol and registry"
```

---

## Task 5: console_error Detector

**Files:**
- Create: `src/retrace/detectors/console_error.py`
- Test: `tests/test_detectors/test_console_error.py`
- Modify: `tests/test_detectors/test_registry.py` (remove skip)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_detectors/test_console_error.py
from tests.fixtures.events import console_event, meta


def test_console_error_detects_error_level():
    from retrace.detectors.console_error import detector

    events = [
        meta(ts=1000, href="https://example.com/page"),
        console_event(ts=1500, level="log", message="ok"),
        console_event(ts=2000, level="error", message="TypeError: x is undefined"),
    ]
    signals = detector.detect("sess-1", events)
    assert len(signals) == 1
    s = signals[0]
    assert s.detector == "console_error"
    assert s.session_id == "sess-1"
    assert s.timestamp_ms == 2000
    assert s.url == "https://example.com/page"
    assert "TypeError" in s.details["message"]


def test_console_error_ignores_non_error_levels():
    from retrace.detectors.console_error import detector

    events = [
        meta(ts=0),
        console_event(ts=100, level="warn", message="hmm"),
        console_event(ts=200, level="info", message="hi"),
    ]
    assert detector.detect("sess-1", events) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_detectors/test_console_error.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the detector**

```python
# src/retrace/detectors/console_error.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, register


_ERROR_LEVELS = {"error", "assert"}


def _current_url(events: list[dict[str, Any]], idx: int) -> str:
    # walk back to most recent Meta (type 4) event with href
    for i in range(idx, -1, -1):
        e = events[i]
        if e.get("type") == 4 and "href" in (e.get("data") or {}):
            return e["data"]["href"]
    return ""


@dataclass
class ConsoleErrorDetector:
    name: str = "console_error"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for idx, e in enumerate(events):
            if e.get("type") != 6:
                continue
            data = e.get("data") or {}
            if not str(data.get("plugin", "")).startswith("rrweb/console"):
                continue
            payload = data.get("payload") or {}
            level = payload.get("level")
            if level not in _ERROR_LEVELS:
                continue
            msg_parts = payload.get("payload") or []
            message = " ".join(str(p) for p in msg_parts)
            out.append(
                Signal(
                    session_id=session_id,
                    detector=self.name,
                    timestamp_ms=int(e.get("timestamp") or 0),
                    url=_current_url(events, idx),
                    details={"message": message, "level": level},
                )
            )
        return out


detector = register(ConsoleErrorDetector())
```

- [ ] **Step 4: Re-enable the registry test**

Delete the `pytest.skip(...)` line from `tests/test_detectors/test_registry.py`.

- [ ] **Step 5: Run all tests**

Run: `uv run pytest -v`
Expected: all PASS including the 2 new detector tests and the 2 registry tests.

- [ ] **Step 6: Commit**

```bash
git add src/retrace/detectors/console_error.py tests/test_detectors/test_console_error.py tests/test_detectors/test_registry.py
git commit -m "feat(detectors): console_error detector"
```

---

## Task 6: network_5xx Detector

**Files:**
- Create: `src/retrace/detectors/network_5xx.py`
- Test: `tests/test_detectors/test_network_5xx.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_detectors/test_network_5xx.py
from tests.fixtures.events import meta, network_event


def test_network_5xx_detects_server_errors():
    from retrace.detectors.network_5xx import detector

    events = [
        meta(ts=0, href="https://app.example.com/orders"),
        network_event(ts=500, url="https://api.example.com/orders", status=200),
        network_event(ts=1000, url="https://api.example.com/orders", status=503, method="POST"),
    ]
    signals = detector.detect("sess-1", events)
    assert len(signals) == 1
    s = signals[0]
    assert s.detector == "network_5xx"
    assert s.timestamp_ms == 1000
    assert s.url == "https://app.example.com/orders"
    assert s.details["status"] == 503
    assert s.details["request_url"].endswith("/orders")
    assert s.details["method"] == "POST"


def test_network_5xx_ignores_4xx_and_2xx():
    from retrace.detectors.network_5xx import detector

    events = [
        meta(ts=0),
        network_event(ts=100, url="https://api.example.com/x", status=404),
        network_event(ts=200, url="https://api.example.com/y", status=200),
    ]
    assert detector.detect("sess-1", events) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_detectors/test_network_5xx.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the detector**

```python
# src/retrace/detectors/network_5xx.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, register
from retrace.detectors.console_error import _current_url


@dataclass
class Network5xxDetector:
    name: str = "network_5xx"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        for idx, e in enumerate(events):
            if e.get("type") != 6:
                continue
            data = e.get("data") or {}
            plugin = str(data.get("plugin", ""))
            if "network" not in plugin:
                continue
            payload = data.get("payload") or {}
            status = payload.get("status_code") or payload.get("status")
            if not isinstance(status, int) or not 500 <= status < 600:
                continue
            out.append(
                Signal(
                    session_id=session_id,
                    detector=self.name,
                    timestamp_ms=int(e.get("timestamp") or 0),
                    url=_current_url(events, idx),
                    details={
                        "status": status,
                        "request_url": payload.get("url", ""),
                        "method": payload.get("method", "GET"),
                    },
                )
            )
        return out


detector = register(Network5xxDetector())
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_detectors/test_network_5xx.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrace/detectors/network_5xx.py tests/test_detectors/test_network_5xx.py
git commit -m "feat(detectors): network_5xx detector"
```

---

## Task 7: rage_click Detector

**Files:**
- Create: `src/retrace/detectors/rage_click.py`
- Test: `tests/test_detectors/test_rage_click.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_detectors/test_rage_click.py
from tests.fixtures.events import click_event, meta


def test_rage_click_fires_on_three_quick_clicks_same_target():
    from retrace.detectors.rage_click import detector

    events = [
        meta(ts=0, href="https://app.example.com/checkout"),
        click_event(ts=1000, x=10, y=20, target_id=7),
        click_event(ts=1200, x=10, y=20, target_id=7),
        click_event(ts=1400, x=10, y=20, target_id=7),
    ]
    signals = detector.detect("sess-1", events)
    assert len(signals) == 1
    s = signals[0]
    assert s.detector == "rage_click"
    assert s.url == "https://app.example.com/checkout"
    assert s.details["click_count"] == 3
    assert s.details["target_id"] == 7


def test_rage_click_ignores_slow_clicks():
    from retrace.detectors.rage_click import detector

    events = [
        meta(ts=0),
        click_event(ts=0, x=10, y=20),
        click_event(ts=2000, x=10, y=20),  # > 1s gap
        click_event(ts=4000, x=10, y=20),
    ]
    assert detector.detect("sess-1", events) == []


def test_rage_click_ignores_different_targets():
    from retrace.detectors.rage_click import detector

    events = [
        meta(ts=0),
        click_event(ts=100, x=10, y=20, target_id=1),
        click_event(ts=200, x=10, y=20, target_id=2),
        click_event(ts=300, x=10, y=20, target_id=3),
    ]
    assert detector.detect("sess-1", events) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_detectors/test_rage_click.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the detector**

```python
# src/retrace/detectors/rage_click.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from retrace.detectors.base import Signal, register
from retrace.detectors.console_error import _current_url


WINDOW_MS = 1000
MIN_CLICKS = 3


def _is_click(e: dict[str, Any]) -> bool:
    if e.get("type") != 3:
        return False
    data = e.get("data") or {}
    # source 2 = MouseInteraction; type 2 = Click in rrweb
    return data.get("source") == 2 and data.get("type") == 2


@dataclass
class RageClickDetector:
    name: str = "rage_click"

    def detect(self, session_id: str, events: list[dict[str, Any]]) -> list[Signal]:
        out: list[Signal] = []
        # Sliding window over clicks on same target_id within WINDOW_MS
        click_indices = [i for i, e in enumerate(events) if _is_click(e)]
        emitted_indices: set[int] = set()
        for i, idx in enumerate(click_indices):
            if idx in emitted_indices:
                continue
            window = [idx]
            base = events[idx]
            base_tid = (base["data"] or {}).get("id")
            base_ts = int(base.get("timestamp") or 0)
            for j in range(i + 1, len(click_indices)):
                jdx = click_indices[j]
                ev = events[jdx]
                tid = (ev["data"] or {}).get("id")
                ts = int(ev.get("timestamp") or 0)
                if tid != base_tid:
                    break
                if ts - base_ts > WINDOW_MS:
                    break
                window.append(jdx)
            if len(window) >= MIN_CLICKS:
                emitted_indices.update(window)
                out.append(
                    Signal(
                        session_id=session_id,
                        detector=self.name,
                        timestamp_ms=base_ts,
                        url=_current_url(events, idx),
                        details={
                            "click_count": len(window),
                            "target_id": base_tid,
                        },
                    )
                )
        return out


detector = register(RageClickDetector())
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_detectors/test_rage_click.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrace/detectors/rage_click.py tests/test_detectors/test_rage_click.py
git commit -m "feat(detectors): rage_click detector"
```

---

## Task 8: PostHog Ingester

**Files:**
- Create: `src/retrace/ingester.py`
- Test: `tests/test_ingester.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingester.py
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from retrace.config import PostHogConfig
from retrace.ingester import PostHogIngester
from retrace.storage import Storage


@pytest.fixture
def cfg() -> PostHogConfig:
    return PostHogConfig(host="https://us.i.posthog.com", project_id="42", api_key="phx_test")


def test_fetch_sessions_since_stores_metadata_and_snapshots(
    httpx_mock: HTTPXMock, tmp_path: Path, cfg: PostHogConfig
):
    since = datetime(2026, 4, 19, 10, 0, tzinfo=timezone.utc)

    httpx_mock.add_response(
        method="GET",
        url=(
            "https://us.i.posthog.com/api/projects/42/session_recordings"
            "?date_from=2026-04-19T10%3A00%3A00%2B00%3A00&limit=50"
        ),
        json={
            "results": [
                {
                    "id": "sess-1",
                    "start_time": "2026-04-19T11:00:00+00:00",
                    "recording_duration": 42,
                    "distinct_id": "user-1",
                    "click_count": 2,
                }
            ],
            "next": None,
        },
    )
    httpx_mock.add_response(
        method="GET",
        url="https://us.i.posthog.com/api/projects/42/session_recordings/sess-1/snapshots",
        json={"snapshots": [{"type": 4, "timestamp": 0, "data": {"href": "https://x.com/"}}]},
    )

    store = Storage(tmp_path / "retrace.db")
    store.init_schema()
    ingester = PostHogIngester(cfg, store, data_dir=tmp_path / "data")

    ids = ingester.fetch_since(since, max_sessions=50)

    assert ids == ["sess-1"]
    assert store.get_session("sess-1") is not None
    events_path = tmp_path / "data" / "sessions" / "sess-1.json"
    assert events_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ingester.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the ingester**

```python
# src/retrace/ingester.py
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from retrace.config import PostHogConfig
from retrace.storage import SessionMeta, Storage


class PostHogIngester:
    def __init__(self, cfg: PostHogConfig, store: Storage, data_dir: Path):
        self.cfg = cfg
        self.store = store
        self.data_dir = Path(data_dir)
        self.sessions_dir = self.data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_key}"}

    def fetch_since(self, since: datetime, max_sessions: int) -> list[str]:
        qs = urlencode({"date_from": since.isoformat(), "limit": max_sessions})
        list_url = f"{self.cfg.host}/api/projects/{self.cfg.project_id}/session_recordings?{qs}"
        with httpx.Client(timeout=60) as client:
            list_resp = client.get(list_url, headers=self._headers())
            list_resp.raise_for_status()
            recordings = list_resp.json().get("results", [])

            ids: list[str] = []
            for r in recordings[:max_sessions]:
                sid = r["id"]
                meta = SessionMeta(
                    id=sid,
                    project_id=self.cfg.project_id,
                    started_at=datetime.fromisoformat(r["start_time"]),
                    duration_ms=int(float(r.get("recording_duration", 0)) * 1000),
                    distinct_id=r.get("distinct_id"),
                    event_count=int(r.get("click_count", 0)),  # best available proxy
                )
                self.store.upsert_session(meta)

                snap_url = (
                    f"{self.cfg.host}/api/projects/{self.cfg.project_id}"
                    f"/session_recordings/{sid}/snapshots"
                )
                snap_resp = client.get(snap_url, headers=self._headers())
                snap_resp.raise_for_status()
                snapshots = snap_resp.json().get("snapshots", [])
                (self.sessions_dir / f"{sid}.json").write_text(json.dumps(snapshots))
                ids.append(sid)
            return ids

    def load_events(self, session_id: str) -> list[dict[str, Any]]:
        path = self.sessions_dir / f"{session_id}.json"
        return json.loads(path.read_text())
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ingester.py -v`
Expected: 1 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrace/ingester.py tests/test_ingester.py
git commit -m "feat(ingester): PostHog session-recordings fetcher"
```

---

## Task 9: LLM Client

**Files:**
- Create: `src/retrace/llm/__init__.py`
- Create: `src/retrace/llm/client.py`
- Test: `tests/test_llm_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_llm_client.py
from pytest_httpx import HTTPXMock

from retrace.config import LLMConfig
from retrace.llm.client import LLMClient


def test_chat_json_returns_parsed_dict(httpx_mock: HTTPXMock):
    cfg = LLMConfig(base_url="http://localhost:8080/v1", model="test", api_key=None)
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"title":"x","severity":"high"}',
                    }
                }
            ]
        },
    )
    client = LLMClient(cfg)
    out = client.chat_json(
        system="you are a QA analyst",
        user="analyze this",
    )
    assert out == {"title": "x", "severity": "high"}


def test_chat_json_strips_code_fences(httpx_mock: HTTPXMock):
    cfg = LLMConfig(base_url="http://localhost:8080/v1", model="test", api_key=None)
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={
            "choices": [
                {"message": {"role": "assistant", "content": '```json\n{"a":1}\n```'}}
            ]
        },
    )
    client = LLMClient(cfg)
    assert client.chat_json(system="s", user="u") == {"a": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_client.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the client**

```python
# src/retrace/llm/__init__.py
```

```python
# src/retrace/llm/client.py
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from retrace.config import LLMConfig


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=self.cfg.timeout_seconds) as client:
            resp = client.post(url, headers=self._headers(), json=body)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        return _parse_json(content)


def _parse_json(content: str) -> dict[str, Any]:
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = _FENCE_RE.search(content)
        if m:
            return json.loads(m.group(1))
        raise
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_llm_client.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrace/llm/__init__.py src/retrace/llm/client.py tests/test_llm_client.py
git commit -m "feat(llm): OpenAI-compatible chat_json client"
```

---

## Task 10: LLM Analyst (Finding Builder)

**Files:**
- Create: `src/retrace/llm/analyst.py`
- Create: `src/retrace/sinks/__init__.py`
- Create: `src/retrace/sinks/base.py`
- Test: `tests/test_llm_analyst.py`

The `Finding` type lives in `sinks/base.py` because it's the shared output shape between the analyst and any sink.

- [ ] **Step 1: Create `sinks/base.py` with the `Finding` dataclass**

```python
# src/retrace/sinks/__init__.py
```

```python
# src/retrace/sinks/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class Finding:
    session_id: str
    session_url: str
    title: str
    severity: str            # critical | high | medium | low
    category: str            # functional_error | visual_bug | performance | confusion
    what_happened: str
    likely_cause: str
    reproduction_steps: list[str] = field(default_factory=list)
    confidence: str = "medium"
    detector_signals: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    started_at: datetime
    finished_at: datetime
    sessions_scanned: int
    sessions_flagged: int


class Sink(Protocol):
    def write(self, summary: RunSummary, findings: list[Finding]) -> None: ...
```

- [ ] **Step 2: Write the failing analyst test**

```python
# tests/test_llm_analyst.py
from unittest.mock import MagicMock

from retrace.detectors.base import Signal
from retrace.llm.analyst import build_prompt, analyze_session


def test_build_prompt_includes_signals_and_action_trail():
    signals = [
        Signal(
            session_id="s1",
            detector="console_error",
            timestamp_ms=1000,
            url="https://x/checkout",
            details={"message": "TypeError: y is undefined", "level": "error"},
        )
    ]
    events = [
        {"type": 4, "timestamp": 0, "data": {"href": "https://x/checkout"}},
        {"type": 3, "timestamp": 500, "data": {"source": 2, "type": 2, "id": 7}},
    ]
    sys, usr = build_prompt("s1", events, signals)
    assert "QA analyst" in sys.lower() or "analyst" in sys.lower()
    assert "TypeError" in usr
    assert "console_error" in usr
    assert "https://x/checkout" in usr


def test_analyze_session_returns_finding_from_llm_json():
    signals = [
        Signal(
            session_id="s1",
            detector="console_error",
            timestamp_ms=1000,
            url="https://x/checkout",
            details={"message": "boom", "level": "error"},
        )
    ]
    llm = MagicMock()
    llm.chat_json.return_value = {
        "title": "Checkout crashes on empty cart",
        "severity": "high",
        "category": "functional_error",
        "what_happened": "User hit checkout and saw nothing.",
        "likely_cause": "Null reference in render.",
        "reproduction_steps": ["open /checkout", "observe blank page"],
        "confidence": "high",
    }
    finding = analyze_session(
        llm_client=llm,
        session_id="s1",
        session_url="https://posthog/replay/s1",
        events=[{"type": 4, "timestamp": 0, "data": {"href": "https://x/checkout"}}],
        signals=signals,
    )
    assert finding.title == "Checkout crashes on empty cart"
    assert finding.severity == "high"
    assert finding.session_url == "https://posthog/replay/s1"
    assert finding.detector_signals == ["console_error"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_llm_analyst.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement the analyst**

```python
# src/retrace/llm/analyst.py
from __future__ import annotations

import json
from typing import Any

from retrace.detectors.base import Signal
from retrace.llm.client import LLMClient
from retrace.sinks.base import Finding


SYSTEM_PROMPT = """You are a senior QA analyst. You review user session recordings
and explain bugs in plain English. Respond with a single JSON object matching the
requested schema. Do not add commentary outside the JSON."""


_USER_SCHEMA = """Return JSON with keys:
  title (short one-line summary of the bug)
  severity (one of: critical, high, medium, low)
  category (one of: functional_error, visual_bug, performance, confusion)
  what_happened (2-4 sentence plain-English narrative)
  likely_cause (1-2 sentences — your best guess)
  reproduction_steps (array of 2-6 short strings)
  confidence (one of: high, medium, low)
"""


def _summarize_actions(events: list[dict[str, Any]], limit: int = 30) -> list[str]:
    out: list[str] = []
    for e in events:
        t = e.get("type")
        d = e.get("data") or {}
        if t == 4 and "href" in d:
            out.append(f"navigate: {d['href']}")
        elif t == 3 and d.get("source") == 2 and d.get("type") == 2:
            out.append(f"click: id={d.get('id')}")
        elif t == 3 and d.get("source") == 5:
            out.append(f"input: id={d.get('id')}")
        if len(out) >= limit:
            break
    return out


def build_prompt(
    session_id: str, events: list[dict[str, Any]], signals: list[Signal]
) -> tuple[str, str]:
    signal_lines = [
        f"- [{s.detector} @ {s.timestamp_ms}ms] {s.url} :: {json.dumps(s.details)}"
        for s in signals
    ]
    actions = _summarize_actions(events)
    user = (
        f"Session: {session_id}\n\n"
        f"Signals detected by heuristics:\n" + "\n".join(signal_lines) + "\n\n"
        f"User actions leading up to and around the issue:\n"
        + "\n".join(f"  - {a}" for a in actions)
        + "\n\n"
        + _USER_SCHEMA
    )
    return SYSTEM_PROMPT, user


def analyze_session(
    *,
    llm_client: LLMClient,
    session_id: str,
    session_url: str,
    events: list[dict[str, Any]],
    signals: list[Signal],
) -> Finding:
    system, user = build_prompt(session_id, events, signals)
    result = llm_client.chat_json(system=system, user=user)
    return Finding(
        session_id=session_id,
        session_url=session_url,
        title=str(result.get("title", "Unclassified issue")),
        severity=str(result.get("severity", "medium")),
        category=str(result.get("category", "functional_error")),
        what_happened=str(result.get("what_happened", "")),
        likely_cause=str(result.get("likely_cause", "")),
        reproduction_steps=list(result.get("reproduction_steps", []) or []),
        confidence=str(result.get("confidence", "medium")),
        detector_signals=sorted({s.detector for s in signals}),
    )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_llm_analyst.py -v`
Expected: 2 PASS.

- [ ] **Step 6: Commit**

```bash
git add src/retrace/llm/analyst.py src/retrace/sinks/__init__.py src/retrace/sinks/base.py tests/test_llm_analyst.py
git commit -m "feat(llm): session analyst produces Finding from LLM"
```

---

## Task 11: Markdown Sink

**Files:**
- Create: `src/retrace/sinks/markdown.py`
- Test: `tests/test_markdown_sink.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_markdown_sink.py
from datetime import datetime, timezone
from pathlib import Path

from retrace.sinks.base import Finding, RunSummary
from retrace.sinks.markdown import MarkdownSink


def test_markdown_sink_writes_report_grouped_by_severity(tmp_path: Path):
    sink = MarkdownSink(output_dir=tmp_path)
    summary = RunSummary(
        started_at=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 19, 14, 3, tzinfo=timezone.utc),
        sessions_scanned=47,
        sessions_flagged=2,
    )
    findings = [
        Finding(
            session_id="s1",
            session_url="https://posthog/replay/s1",
            title="Checkout crashes on empty cart",
            severity="critical",
            category="functional_error",
            what_happened="User opened /checkout and saw a blank page.",
            likely_cause="Null reference in CartSummary.",
            reproduction_steps=["Open /checkout with no items", "Observe blank page"],
            confidence="high",
            detector_signals=["console_error"],
        ),
        Finding(
            session_id="s2",
            session_url="https://posthog/replay/s2",
            title="Submit button requires triple-click",
            severity="medium",
            category="confusion",
            what_happened="User clicked submit three times before any feedback.",
            likely_cause="Button disables only after network round-trip.",
            reproduction_steps=["Fill form", "Click submit"],
            confidence="medium",
            detector_signals=["rage_click"],
        ),
    ]

    sink.write(summary, findings)

    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "Scanned 47 sessions" in text
    assert "Flagged 2" in text
    # Critical appears before Medium
    crit_idx = text.index("Critical")
    med_idx = text.index("Medium")
    assert crit_idx < med_idx
    assert "Checkout crashes on empty cart" in text
    assert "https://posthog/replay/s1" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_markdown_sink.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the sink**

```python
# src/retrace/sinks/markdown.py
from __future__ import annotations

from pathlib import Path

from retrace.sinks.base import Finding, RunSummary, Sink


_SEVERITY_ORDER = ["critical", "high", "medium", "low"]
_SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}


def _render_finding(f: Finding) -> str:
    steps = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(f.reproduction_steps))
    signals = ", ".join(f.detector_signals) if f.detector_signals else "—"
    return (
        f"### {f.title}\n\n"
        f"- **Session:** [{f.session_id}]({f.session_url})\n"
        f"- **Category:** {f.category}\n"
        f"- **Confidence:** {f.confidence}\n"
        f"- **Signals:** {signals}\n\n"
        f"**What happened:** {f.what_happened}\n\n"
        f"**Likely cause:** {f.likely_cause}\n\n"
        f"**Reproduction:**\n{steps}\n"
    )


class MarkdownSink(Sink):
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, summary: RunSummary, findings: list[Finding]) -> None:
        name = summary.started_at.strftime("%Y-%m-%d-%H%M") + ".md"
        path = self.output_dir / name

        by_sev: dict[str, list[Finding]] = {sev: [] for sev in _SEVERITY_ORDER}
        for f in findings:
            by_sev.setdefault(f.severity, []).append(f)

        out: list[str] = []
        out.append(f"# Retrace report — {summary.started_at.strftime('%Y-%m-%d %H:%M')}\n")
        out.append(
            f"Scanned {summary.sessions_scanned} sessions.  "
            f"Flagged {summary.sessions_flagged}.\n"
        )

        for sev in _SEVERITY_ORDER:
            items = by_sev.get(sev, [])
            if not items:
                continue
            emoji = _SEVERITY_EMOJI.get(sev, "")
            out.append(f"## {emoji} {sev.capitalize()}\n")
            for f in items:
                out.append(_render_finding(f))

        path.write_text("\n".join(out))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_markdown_sink.py -v`
Expected: 1 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrace/sinks/markdown.py tests/test_markdown_sink.py
git commit -m "feat(sinks): markdown report writer"
```

---

## Task 12: Pipeline Orchestrator

**Files:**
- Create: `src/retrace/pipeline.py`
- Test: `tests/test_pipeline.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_pipeline.py
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from retrace.config import DetectorsConfig, LLMConfig, PostHogConfig, RetraceConfig, RunConfig
from retrace.pipeline import run_pipeline
from retrace.storage import Storage

# Triggers detector self-registration on import.
import retrace.detectors.console_error  # noqa: F401
import retrace.detectors.network_5xx  # noqa: F401
import retrace.detectors.rage_click  # noqa: F401


def test_run_pipeline_end_to_end_with_fake_llm_and_ingester(tmp_path: Path):
    cfg = RetraceConfig(
        posthog=PostHogConfig(host="https://ph", project_id="42", api_key="phx"),
        llm=LLMConfig(base_url="http://llm/v1", model="m", api_key=None),
        run=RunConfig(
            lookback_hours=6,
            max_sessions_per_run=10,
            output_dir=tmp_path / "reports",
            data_dir=tmp_path / "data",
        ),
        detectors=DetectorsConfig(console_error=True, network_5xx=True, rage_click=True),
    )

    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    # Stub ingester: returns one session id and pre-seeds an event file.
    (tmp_path / "data" / "sessions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "sessions" / "sess-1.json").write_text(
        '[{"type":4,"timestamp":0,"data":{"href":"https://x/checkout"}},'
        '{"type":6,"timestamp":500,"data":{"plugin":"rrweb/console@1",'
        '"payload":{"level":"error","payload":["TypeError boom"]}}}]'
    )

    ingester = MagicMock()
    ingester.fetch_since.return_value = ["sess-1"]
    ingester.load_events.return_value = __import__("json").loads(
        (tmp_path / "data" / "sessions" / "sess-1.json").read_text()
    )

    llm_client = MagicMock()
    llm_client.chat_json.return_value = {
        "title": "Checkout crashes",
        "severity": "critical",
        "category": "functional_error",
        "what_happened": "TypeError shown after open.",
        "likely_cause": "Null ref.",
        "reproduction_steps": ["open /checkout"],
        "confidence": "high",
    }

    summary = run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
    )

    assert summary.sessions_scanned == 1
    assert summary.sessions_flagged == 1

    reports = list((tmp_path / "reports").glob("*.md"))
    assert len(reports) == 1
    text = reports[0].read_text()
    assert "Checkout crashes" in text
    assert "Critical" in text


def test_run_pipeline_skips_sessions_with_no_signals(tmp_path: Path):
    cfg = RetraceConfig(
        posthog=PostHogConfig(host="https://ph", project_id="42", api_key="phx"),
        llm=LLMConfig(base_url="http://llm/v1", model="m", api_key=None),
        run=RunConfig(
            lookback_hours=6,
            max_sessions_per_run=10,
            output_dir=tmp_path / "reports",
            data_dir=tmp_path / "data",
        ),
        detectors=DetectorsConfig(console_error=True, network_5xx=True, rage_click=True),
    )

    store = Storage(tmp_path / "data" / "retrace.db")
    store.init_schema()

    ingester = MagicMock()
    ingester.fetch_since.return_value = ["sess-clean"]
    ingester.load_events.return_value = [
        {"type": 4, "timestamp": 0, "data": {"href": "https://x/home"}}
    ]

    llm_client = MagicMock()

    summary = run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime(2026, 4, 19, 14, 0, tzinfo=timezone.utc),
    )

    assert summary.sessions_scanned == 1
    assert summary.sessions_flagged == 0
    llm_client.chat_json.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the pipeline**

```python
# src/retrace/pipeline.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from retrace.config import RetraceConfig
from retrace.detectors import Signal, all_detectors
from retrace.ingester import PostHogIngester
from retrace.llm.analyst import analyze_session
from retrace.llm.client import LLMClient
from retrace.sinks.base import Finding, RunSummary
from retrace.sinks.markdown import MarkdownSink
from retrace.storage import Storage


def _enabled_detector_names(cfg: RetraceConfig) -> set[str]:
    d = cfg.detectors
    names = set()
    if d.console_error:
        names.add("console_error")
    if d.network_5xx:
        names.add("network_5xx")
    if d.rage_click:
        names.add("rage_click")
    return names


def _session_replay_url(cfg: RetraceConfig, session_id: str) -> str:
    return (
        f"{cfg.posthog.host.rstrip('/')}/project/{cfg.posthog.project_id}"
        f"/replay/{session_id}"
    )


def run_pipeline(
    *,
    cfg: RetraceConfig,
    store: Storage,
    ingester: PostHogIngester,
    llm_client: LLMClient,
    now: datetime,
) -> RunSummary:
    run_id = store.start_run()
    started_at = now

    cursor = store.get_last_run_cursor() or (now - timedelta(hours=cfg.run.lookback_hours))
    ids = ingester.fetch_since(cursor, max_sessions=cfg.run.max_sessions_per_run)

    enabled = _enabled_detector_names(cfg)
    detectors = [d for d in all_detectors() if d.name in enabled]

    findings: list[Finding] = []
    for sid in ids:
        events: list[dict[str, Any]] = ingester.load_events(sid)
        signals: list[Signal] = []
        for d in detectors:
            signals.extend(d.detect(sid, events))
        if not signals:
            continue
        finding = analyze_session(
            llm_client=llm_client,
            session_id=sid,
            session_url=_session_replay_url(cfg, sid),
            events=events,
            signals=signals,
        )
        findings.append(finding)

    finished_at = datetime.now(timezone.utc)
    summary = RunSummary(
        started_at=started_at,
        finished_at=finished_at,
        sessions_scanned=len(ids),
        sessions_flagged=len(findings),
    )

    sink = MarkdownSink(output_dir=cfg.run.output_dir)
    sink.write(summary, findings)

    store.set_last_run_cursor(now)
    store.finish_run(
        run_id,
        sessions_scanned=summary.sessions_scanned,
        findings_count=summary.sessions_flagged,
        status="ok",
    )
    return summary
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/retrace/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): orchestrate ingest → detect → analyze → write"
```

---

## Task 13: CLI Entrypoint

**Files:**
- Create: `src/retrace/cli.py`
- Create: `src/retrace/__main__.py`

- [ ] **Step 1: Implement the CLI**

```python
# src/retrace/__main__.py
from retrace.cli import main

if __name__ == "__main__":
    main()
```

```python
# src/retrace/cli.py
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

# Import detectors to trigger registration.
import retrace.detectors.console_error  # noqa: F401
import retrace.detectors.network_5xx  # noqa: F401
import retrace.detectors.rage_click  # noqa: F401

from retrace.config import load_config
from retrace.ingester import PostHogIngester
from retrace.llm.client import LLMClient
from retrace.pipeline import run_pipeline
from retrace.storage import Storage


@click.group()
def main() -> None:
    """Retrace — find the bugs your users hit."""


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
def run(config_path: Path) -> None:
    """Pull recent sessions, detect issues, write a report."""
    cfg = load_config(config_path)

    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()

    ingester = PostHogIngester(cfg.posthog, store, data_dir=cfg.run.data_dir)
    llm_client = LLMClient(cfg.llm)

    summary = run_pipeline(
        cfg=cfg,
        store=store,
        ingester=ingester,
        llm_client=llm_client,
        now=datetime.now(timezone.utc),
    )
    click.echo(
        f"Scanned {summary.sessions_scanned} sessions. "
        f"Flagged {summary.sessions_flagged}. "
        f"Report written to {cfg.run.output_dir}/"
    )
```

- [ ] **Step 2: Smoke-test CLI registration**

Run: `uv run retrace --help`
Expected: help output mentioning the `run` subcommand.

Run: `uv run retrace run --help`
Expected: help output mentioning `--config`.

- [ ] **Step 3: Commit**

```bash
git add src/retrace/cli.py src/retrace/__main__.py
git commit -m "feat(cli): retrace run command"
```

---

## Task 14: README & Final Sweep

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

```markdown
# Retrace

Your real users are your QA team. Retrace finds the bugs they hit.

## v0.1-alpha

This is a pre-release vertical slice. It:
- Pulls session recordings from PostHog
- Runs 3 detectors (`console_error`, `network_5xx`, `rage_click`) over rrweb events
- Sends flagged sessions to an OpenAI-compatible LLM (llama.cpp, ollama, OpenAI, etc.)
- Writes a markdown bug report to `./reports/YYYY-MM-DD-HHMM.md`

## Install

```bash
uv tool install retrace
```

## Setup

1. Copy config template:
   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```
2. Fill in PostHog host, project ID, and personal API key.
3. Point `llm.base_url` at a running OpenAI-compatible server. For llama.cpp:
   ```bash
   llama-server -m ./models/llama-3.1-8b-instruct.gguf --host 0.0.0.0 --port 8080
   ```
4. Run:
   ```bash
   retrace run
   ```

See `docs/superpowers/specs/2026-04-19-retrace-design.md` for the full design.
```

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -v`
Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: v0.1-alpha README"
```

---

## Self-Review Notes

**Spec coverage (v0.1-alpha scope):**

- ✅ PostHog ingester with cursor — Task 8
- ✅ 3 detectors (console_error, network_5xx, rage_click) — Tasks 5–7
- ✅ OpenAI-compatible LLM client — Task 9
- ✅ LLM analyst producing structured Finding — Task 10
- ✅ Markdown sink — Task 11
- ✅ Pipeline orchestrator — Task 12
- ✅ `retrace run` CLI — Task 13
- ✅ SQLite storage for sessions + runs + cursor — Task 3
- ✅ Config loader (yaml + .env) — Task 2
- ✅ Test fixtures + TDD per unit — throughout

**Explicitly deferred to Plan B:**
- Clustering (in Plan A, each flagged session → one finding)
- `retrace init` wizard (Plan A uses a hand-edited `config.yaml`)
- `retrace doctor`
- Docker Compose
- Remaining 5 detectors (`network_4xx`, `dead_click`, `error_toast`, `blank_render`, `session_abandon_on_error`)

**Placeholder scan:** clean — no TBDs, every step has concrete code or a concrete command.

**Type consistency:** `Signal`, `Finding`, `RunSummary`, `SessionMeta`, `RetraceConfig` and its sub-models are defined once and referenced consistently across tasks.
