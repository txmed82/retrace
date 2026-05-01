from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from retrace.config import LLMConfig


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_ANTHROPIC_VERSION = "2023-06-01"


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
        return _build_headers(provider=self.cfg.provider, api_key=self.cfg.api_key)

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        body = {
            "system": system,
            "user": user,
            "temperature": temperature,
            "response_json": True,
            "max_tokens": None,
        }
        url, headers, req_body = build_llm_http_request(
            provider=self.cfg.provider,
            base_url=self.cfg.base_url,
            model=self.cfg.model,
            api_key=self.cfg.api_key,
            **body,
        )

        last_exc: Exception | None = None
        for attempt in range(3):  # initial + 2 retries
            try:
                resp = self._client.post(url, headers=headers, json=req_body)
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
                content = extract_llm_text_content(
                    provider=self.cfg.provider, payload=payload
                )
            except LLMError as exc:
                raise LLMError(f"unexpected LLM response shape: {payload!r}") from exc
            if not isinstance(content, str):
                raise LLMError(f"LLM content not a string: {content!r}")
            return _parse_json(content)

        assert last_exc is not None
        raise LLMError("LLM request failed after 3 attempts") from last_exc

    def chat_visual_json(
        self,
        *,
        system: str,
        user: str,
        image_path: str,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Send a multimodal chat request that includes a screenshot.

        Used by the visual CUA execution mode (RET-22).  The image is read
        from `image_path` and encoded inline as base64.  Both Anthropic and
        OpenAI-compatible providers accept this shape; gateways that strip
        non-text content will fail to parse the model's response.
        """
        url, headers, req_body = build_llm_http_request(
            provider=self.cfg.provider,
            base_url=self.cfg.base_url,
            model=self.cfg.model,
            api_key=self.cfg.api_key,
            system=system,
            user=user,
            temperature=temperature,
            response_json=True,
            max_tokens=None,
            image_path=image_path,
        )

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._client.post(url, headers=headers, json=req_body)
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
                content = extract_llm_text_content(
                    provider=self.cfg.provider, payload=payload
                )
            except LLMError as exc:
                raise LLMError(f"unexpected LLM response shape: {payload!r}") from exc
            if not isinstance(content, str):
                raise LLMError(f"LLM content not a string: {content!r}")
            return _parse_json(content)

        assert last_exc is not None
        raise LLMError("LLM visual request failed after 3 attempts") from last_exc

    @staticmethod
    def _backoff(attempt: int) -> None:
        # 0.5s, 1s (no sleep after final attempt since loop exits)
        if attempt < 2:
            time.sleep(0.5 * (2**attempt))


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
            raise LLMError(
                f"LLM fenced content not valid JSON: {content[:200]!r}"
            ) from exc
    if not isinstance(result, dict):
        raise LLMError(f"LLM returned non-object JSON: {result!r}")
    return result


def build_llm_http_request(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: Optional[str],
    system: str,
    user: str,
    temperature: float,
    response_json: bool,
    max_tokens: Optional[int],
    image_path: Optional[str] = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    p = provider or "openai_compatible"
    headers = _build_headers(provider=p, api_key=api_key)
    image_data, image_mime = _load_image_inline(image_path)

    if p == "anthropic":
        url = f"{base_url.rstrip('/')}/messages"
        anth_user = user
        if response_json:
            anth_user = f"{user}\n\nReturn only a valid JSON object."
        if image_data:
            user_content: list[dict[str, Any]] = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_mime,
                        "data": image_data,
                    },
                },
                {"type": "text", "text": anth_user},
            ]
        else:
            user_content = anth_user  # type: ignore[assignment]
        body: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            "temperature": temperature,
            "max_tokens": int(max_tokens or 512),
        }
        return url, headers, body

    url = f"{base_url.rstrip('/')}/chat/completions"
    if image_data:
        user_message: Any = [
            {"type": "text", "text": user},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image_mime};base64,{image_data}"},
            },
        ]
    else:
        user_message = user
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
    }
    if response_json:
        body["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)
    return url, headers, body


def _load_image_inline(image_path: Optional[str]) -> tuple[Optional[str], str]:
    """Read an image off disk and return (base64_data, mime).

    Returns (None, "") when no image was requested or the file is missing —
    the caller falls back to the text-only request shape so a missing
    screenshot doesn't take the whole run down.
    """
    if not image_path:
        return None, ""
    p = Path(image_path)
    if not p.is_file():
        return None, ""
    mime, _ = mimetypes.guess_type(p.name)
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    try:
        encoded = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError:
        return None, ""
    return encoded, mime


def extract_llm_text_content(*, provider: str, payload: dict[str, Any]) -> str:
    p = provider or "openai_compatible"
    if p == "anthropic":
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            raise LLMError("anthropic response missing content list")
        texts: list[str] = []
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                texts.append(block["text"])
        if not texts:
            raise LLMError("anthropic response had no text content")
        return "\n".join(texts)

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            "openai-compatible response missing choices[0].message.content"
        ) from exc
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        if texts:
            return "\n".join(texts)
    if not isinstance(content, str):
        raise LLMError("openai-compatible content is not a string")
    return content


def _build_headers(*, provider: str, api_key: Optional[str]) -> dict[str, str]:
    p = provider or "openai_compatible"
    h = {"Content-Type": "application/json"}
    if p == "anthropic":
        h["anthropic-version"] = _ANTHROPIC_VERSION
        if api_key:
            h["x-api-key"] = api_key
        return h
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def fetch_llm_models(
    *,
    provider: str,
    base_url: str,
    api_key: Optional[str],
    timeout_seconds: int = 10,
) -> list[str]:
    """Best-effort model discovery for onboarding.

    Returns a list of model IDs or an empty list if none could be parsed.
    Raises on transport/auth/http errors so callers can decide UX fallback.
    """
    p = provider or "openai_compatible"
    headers = _build_headers(provider=p, api_key=api_key)

    url = f"{base_url.rstrip('/')}/models"
    with httpx.Client(timeout=timeout_seconds) as c:
        resp = c.get(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    return _extract_model_ids(payload)


def _extract_model_ids(payload: Any) -> list[str]:
    ids: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value not in ids:
            ids.append(value)

    if isinstance(payload, dict):
        # Common shape: {"data":[{"id":"..."}, ...]}
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    add(item.get("id"))
                    add(item.get("name"))
                else:
                    add(item)

        # Fallback shapes sometimes seen in proxies.
        models = payload.get("models")
        if isinstance(models, list):
            for item in models:
                if isinstance(item, dict):
                    add(item.get("id"))
                    add(item.get("name"))
                else:
                    add(item)
        elif isinstance(models, dict):
            for key in models.keys():
                add(key)

    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                add(item.get("id"))
                add(item.get("name"))
            else:
                add(item)

    return ids