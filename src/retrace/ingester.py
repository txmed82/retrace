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
                    event_count=int(r.get("click_count", 0)),
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
