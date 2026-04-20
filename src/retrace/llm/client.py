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
