from __future__ import annotations

import json
import logging
import os
import tempfile
import time
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

    def _get_with_retry(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, str] | None = None,
        max_attempts: int = 5,
    ) -> httpx.Response:
        for attempt in range(max_attempts):
            resp = client.get(url, headers=self._headers(), params=params)
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp
            if attempt >= max_attempts - 1:
                resp.raise_for_status()
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep_s = float(retry_after)
            else:
                sleep_s = min(8.0, 0.5 * (2 ** attempt))
            time.sleep(sleep_s)
        raise RuntimeError("unreachable retry loop")

    @staticmethod
    def _parse_concatenated_json(text: str) -> list[Any]:
        """Parse JSON values concatenated in one stream (PostHog blob_v2/jsonl)."""
        out: list[Any] = []
        decoder = json.JSONDecoder()
        i = 0
        n = len(text)
        while i < n:
            while i < n and text[i].isspace():
                i += 1
            if i >= n:
                break
            value, j = decoder.raw_decode(text, i)
            out.append(value)
            i = j
        return out

    def _fetch_snapshots(self, client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
        host = self.cfg.host.rstrip("/")
        snap_url = (
            f"{host}/api/projects/{self.cfg.project_id}"
            f"/session_recordings/{session_id}/snapshots"
        )
        snap_resp = self._get_with_retry(client, snap_url)
        body = snap_resp.json()

        # Legacy shape: {"snapshots": [...]}
        if isinstance(body, dict) and isinstance(body.get("snapshots"), list):
            return body.get("snapshots", [])

        # New shape: {"sources":[{"source":"blob_v2","blob_key":"0",...}, ...]}
        sources = body.get("sources", []) if isinstance(body, dict) else []
        if not isinstance(sources, list) or not sources:
            return []

        snapshots: list[dict[str, Any]] = []
        for src in sources:
            if not isinstance(src, dict):
                continue
            source_name = src.get("source")
            blob_key = src.get("blob_key")
            if not source_name or blob_key is None:
                continue

            blob_resp = self._get_with_retry(
                client,
                snap_url,
                params={
                    "source": str(source_name),
                    "start_blob_key": str(blob_key),
                    "end_blob_key": str(blob_key),
                },
            )
            for item in self._parse_concatenated_json(blob_resp.text):
                if isinstance(item, list) and len(item) >= 2 and isinstance(item[1], dict):
                    snapshots.append(item[1])
                elif isinstance(item, dict):
                    snapshots.append(item)
        return snapshots

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
                list_resp = self._get_with_retry(client, next_url)
                body = list_resp.json()
                recordings = body.get("results", [])
                for r in recordings:
                    if len(ids) >= max_sessions:
                        break
                    sid = r["id"]
                    try:
                        snapshots = self._fetch_snapshots(client, sid)
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
