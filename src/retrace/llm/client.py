from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from retrace.config import LLMConfig


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = httpx.Client(timeout=cfg.timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

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

        last_exc: Exception | None = None
        for attempt in range(3):  # initial + 2 retries
            try:
                resp = self._client.post(url, headers=self._headers(), json=body)
            except httpx.TimeoutException as exc:
                last_exc = exc
                self._backoff(attempt)
                continue
            except httpx.TransportError as exc:
                last_exc = exc
                self._backoff(attempt)
                continue

            if 500 <= resp.status_code < 600:
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} from LLM", request=resp.request, response=resp
                )
                self._backoff(attempt)
                continue

            resp.raise_for_status()
            payload = resp.json()
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"unexpected LLM response shape: {payload!r}") from exc
            if not isinstance(content, str):
                raise LLMError(f"LLM content not a string: {content!r}")
            return _parse_json(content)

        assert last_exc is not None
        raise LLMError("LLM request failed after 3 attempts") from last_exc

    @staticmethod
    def _backoff(attempt: int) -> None:
        # 0.5s, 1s (no sleep after final attempt since loop exits)
        if attempt < 2:
            time.sleep(0.5 * (2 ** attempt))


def _parse_json(content: str) -> dict[str, Any]:
    content = content.strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        m = _FENCE_RE.search(content)
        if not m:
            raise LLMError(f"LLM returned non-JSON content: {content[:200]!r}")
        try:
            result = json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM fenced content not valid JSON: {content[:200]!r}") from exc
    if not isinstance(result, dict):
        raise LLMError(f"LLM returned non-object JSON: {result!r}")
    return result