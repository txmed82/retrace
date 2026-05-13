"""Replay blob storage abstraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Protocol

class ReplayBlobStore(Protocol):
    backend: str

    def write_events(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: str,
        sequence: int,
        events: list[dict[str, object]],
    ) -> str:
        ...

    def read_events(self, key: str) -> list[dict[str, Any]]:
        ...


class LocalReplayBlobStore:
    backend = "local_filesystem"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_part(value: object) -> str:
        raw = str(value or "").strip()
        safe = "".join(c if c.isalnum() or c in {"-", "_", "."} else "_" for c in raw)
        return safe or "default"

    def write_events(
        self,
        *,
        project_id: str,
        environment_id: str,
        session_id: str,
        sequence: int,
        events: list[dict[str, object]],
    ) -> str:
        key = "/".join(
            [
                self._safe_part(project_id),
                self._safe_part(environment_id),
                self._safe_part(session_id),
                f"{int(sequence):012d}.json",
            ]
        )
        path = (self.root / key).resolve()
        root = self.root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("replay blob key escaped storage root") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(events, separators=(",", ":")) + "\n", encoding="utf-8")
        return key

    def read_events(self, key: str) -> list[dict[str, Any]]:
        path = (self.root / key).resolve()
        root = self.root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("replay blob key escaped storage root") from exc
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

