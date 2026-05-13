from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class PostHogClient:
    """Shared HTTP client for PostHog API calls with auth, retry, and host normalization.

    Used by both the ingester (session/snapshot fetching) and the enrichment
    engine (HogQL queries).
    """

    def __init__(
        self,
        *,
        host: str,
        project_id: str,
        api_key: str,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_retries: int = 5,
    ) -> None:
        self._host = host.rstrip("/")
        self._project_id = project_id
        self._api_key = api_key
        self._connect_timeout = max(0.5, float(connect_timeout))
        self._read_timeout = max(1.0, float(read_timeout))
        self._max_retries = max(1, int(max_retries))

    # -- host helpers ---------------------------------------------------------

    @property
    def query_host(self) -> str:
        """Normalize ingest host (e.g. us.i.posthog.com) → query host (us.posthog.com)."""
        h = self._host
        if "://us.i.posthog.com" in h:
            return h.replace("://us.i.posthog.com", "://us.posthog.com")
        if "://eu.i.posthog.com" in h:
            return h.replace("://eu.i.posthog.com", "://eu.posthog.com")
        return h

    @property
    def ingest_base(self) -> str:
        """Base URL for the ingest (capture / session) API."""
        return f"{self._host}/api/projects/{self._project_id}"

    @property
    def query_base(self) -> str:
        """Base URL for the query (HogQL) API."""
        return f"{self.query_host.rstrip('/')}/api/projects/{self._project_id}/query/"

    # -- auth -----------------------------------------------------------------

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    # -- HTTP helpers ---------------------------------------------------------

    def _build_client(self, *, read_timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(
                connect=self._connect_timeout,
                read=read_timeout or self._read_timeout,
                write=10.0,
                pool=10.0,
            )
        )

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        read_timeout: float | None = None,
    ) -> httpx.Response:
        """GET with exponential-backoff retry on 429 (rate-limit) only."""
        with self._build_client(read_timeout=read_timeout) as client:
            for attempt in range(self._max_retries):
                resp = client.get(
                    url, headers=self._auth_headers, params=params
                )
                if resp.status_code == 429:
                    if attempt < self._max_retries - 1:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            sleep_s = float(retry_after)
                        else:
                            sleep_s = min(8.0, 0.5 * (2**attempt))
                        time.sleep(sleep_s)
                        continue
                resp.raise_for_status()
                return resp

        raise RuntimeError("unreachable: retry loop exhausted without exception")

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        read_timeout: float | None = None,
    ) -> httpx.Response:
        """POST with exponential-backoff retry on 429 (rate-limit) only."""
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        with self._build_client(read_timeout=read_timeout) as client:
            for attempt in range(self._max_retries):
                resp = client.post(url, headers=headers, json=json)
                if resp.status_code == 429:
                    if attempt < self._max_retries - 1:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            sleep_s = float(retry_after)
                        else:
                            sleep_s = min(8.0, 0.5 * (2**attempt))
                        time.sleep(sleep_s)
                        continue
                resp.raise_for_status()
                return resp

        raise RuntimeError("unreachable: retry loop exhausted without exception")
