from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from retrace.config import PostHogConfig
from retrace.storage import SessionMeta, Storage


log = logging.getLogger(__name__)


class PostHogIngester:
    def __init__(self, cfg: PostHogConfig, store: Storage, data_dir: Path):
        self.cfg = cfg
        self.store = store
        self.data_dir = Path(data_dir)
        self.sessions_dir = self.data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_key}"}

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        """Write JSON durably: temp file in same dir, fsync, os.replace."""
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def fetch_since(self, since: datetime, max_sessions: int) -> list[str]:
        host = self.cfg.host.rstrip("/")
        qs = urlencode({"date_from": since.isoformat(), "limit": max_sessions})
        next_url: str | None = (
            f"{host}/api/projects/{self.cfg.project_id}/session_recordings?{qs}"
        )
        ids: list[str] = []
        with httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
        ) as client:
            while next_url and len(ids) < max_sessions:
                list_resp = client.get(next_url, headers=self._headers())
                list_resp.raise_for_status()
                body = list_resp.json()
                recordings = body.get("results", [])
                for r in recordings:
                    if len(ids) >= max_sessions:
                        break
                    sid = r["id"]
                    try:
                        snap_url = (
                            f"{host}/api/projects/{self.cfg.project_id}"
                            f"/session_recordings/{sid}/snapshots"
                        )
                        snap_resp = client.get(snap_url, headers=self._headers())
                        snap_resp.raise_for_status()
                        snapshots = snap_resp.json().get("snapshots", [])
                        self._atomic_write_json(
                            self.sessions_dir / f"{sid}.json", snapshots
                        )
                        duration_seconds = r.get("recording_duration") or 0
                        meta = SessionMeta(
                            id=sid,
                            project_id=self.cfg.project_id,
                            started_at=datetime.fromisoformat(
                                str(r["start_time"]).replace("Z", "+00:00")
                            ),
                            duration_ms=int(float(duration_seconds) * 1000),
                            distinct_id=r.get("distinct_id"),
                            event_count=int(
                                r.get("event_count") or r.get("click_count") or 0
                            ),
                        )
                        self.store.upsert_session(meta)
                        ids.append(sid)
                    except Exception as exc:
                        log.warning("failed to ingest session %s: %s", sid, exc)
                        continue
                next_url = body.get("next")
        return ids

    def load_events(self, session_id: str) -> list[dict[str, Any]]:
        path = self.sessions_dir / f"{session_id}.json"
        return json.loads(path.read_text())
