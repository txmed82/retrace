from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


API_SUITE_SCHEMA_VERSION = "api_suite.v1"


@dataclass
class APITestSuite:
    schema_version: str
    suite_id: str
    name: str
    source: str
    created_at: str
    updated_at: str
    spec_ids: list[str] = field(default_factory=list)
    filters: dict[str, str] = field(default_factory=dict)
    auth_profile: str = ""
    env_profile: str = ""
    import_summary: dict[str, Any] = field(default_factory=dict)
    operations: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return s or "api-suite"


def api_suites_dir_for_data_dir(data_dir: Path) -> Path:
    return data_dir / "api-tests" / "suites"


def save_api_suite(suites_dir: Path, suite: APITestSuite) -> None:
    validate_api_suite(suite)
    suites_dir.mkdir(parents=True, exist_ok=True)
    _suite_path(suites_dir, suite.suite_id).write_text(
        json.dumps(asdict(suite), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def create_api_suite(
    *,
    suites_dir: Path,
    name: str,
    source: str,
    spec_ids: list[str],
    filters: dict[str, str] | None = None,
    auth_profile: str = "",
    env_profile: str = "",
    import_summary: dict[str, Any] | None = None,
    operations: list[dict[str, Any]] | None = None,
    skipped: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> APITestSuite:
    created_at = now_iso()
    suite = APITestSuite(
        schema_version=API_SUITE_SCHEMA_VERSION,
        suite_id=f"api_suite_{slugify(name)}_{uuid.uuid4().hex[:8]}",
        name=name.strip() or "API Suite",
        source=source.strip() or "manual",
        created_at=created_at,
        updated_at=created_at,
        spec_ids=[str(item) for item in spec_ids if str(item).strip()],
        filters={str(k): str(v) for k, v in dict(filters or {}).items() if str(v).strip()},
        auth_profile=auth_profile.strip(),
        env_profile=env_profile.strip(),
        import_summary=dict(import_summary or {}),
        operations=[dict(item) for item in list(operations or [])],
        skipped=[str(item) for item in list(skipped or [])],
        metadata=dict(metadata or {}),
    )
    save_api_suite(suites_dir, suite)
    return suite


def load_api_suite(suites_dir: Path, suite_id: str) -> APITestSuite:
    path = _suite_path(suites_dir, suite_id)
    if not path.exists():
        raise FileNotFoundError(suite_id)
    return _coerce_suite(json.loads(path.read_text(encoding="utf-8")))


def list_api_suites(suites_dir: Path) -> list[APITestSuite]:
    if not suites_dir.exists():
        return []
    suites: list[APITestSuite] = []
    for path in sorted(suites_dir.glob("*.json")):
        try:
            suites.append(load_api_suite(suites_dir, path.stem))
        except Exception:
            continue
    return suites


def validate_api_suite(suite: APITestSuite) -> None:
    if suite.schema_version != API_SUITE_SCHEMA_VERSION:
        raise ValueError("unsupported API suite schema_version")
    if not suite.suite_id.strip():
        raise ValueError("suite_id is required")
    if not suite.name.strip():
        raise ValueError("name is required")
    if not isinstance(suite.spec_ids, list):
        raise ValueError("spec_ids must be a list")
    if not isinstance(suite.operations, list):
        raise ValueError("operations must be a list")
    if not isinstance(suite.skipped, list):
        raise ValueError("skipped must be a list")


def _suite_path(suites_dir: Path, suite_id: str) -> Path:
    if not suite_id or not re.match(r"^[a-zA-Z0-9_-]+$", suite_id):
        raise ValueError("Invalid API suite_id")
    candidate = (suites_dir / f"{suite_id}.json").resolve()
    candidate.relative_to(suites_dir.resolve())
    return candidate


def _coerce_suite(data: dict[str, Any]) -> APITestSuite:
    data = dict(data)
    data.setdefault("schema_version", API_SUITE_SCHEMA_VERSION)
    data.setdefault("spec_ids", [])
    data.setdefault("filters", {})
    data.setdefault("auth_profile", "")
    data.setdefault("env_profile", "")
    data.setdefault("import_summary", {})
    data.setdefault("operations", [])
    data.setdefault("skipped", [])
    data.setdefault("metadata", {})
    return APITestSuite(**data)
