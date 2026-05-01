import pytest
from pytest_httpx import HTTPXMock

from retrace.config import LLMConfig
from retrace.llm.client import LLMClient, LLMError


def _cfg(
    api_key: str | None = None,
    provider: str = "openai_compatible",
    base_url: str = "http://localhost:8080/v1",
) -> LLMConfig:
    return LLMConfig(
        provider=provider, base_url=base_url, model="test", api_key=api_key
    )


def test_chat_json_returns_parsed_dict(httpx_mock: HTTPXMock):
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
    with LLMClient(_cfg()) as client:
        out = client.chat_json(system="you are a QA analyst", user="analyze this")
    assert out == {"title": "x", "severity": "high"}


def test_chat_json_strips_code_fences(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={
            "choices": [
                {"message": {"role": "assistant", "content": '```json\n{"a":1}\n```'}}
            ]
        },
    )
    with LLMClient(_cfg()) as client:
        assert client.chat_json(system="s", user="u") == {"a": 1}


def test_chat_json_retries_on_500_then_succeeds(httpx_mock: HTTPXMock, monkeypatch):
    monkeypatch.setattr("retrace.llm.client.time.sleep", lambda _s: None)

    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        status_code=500,
        json={"error": "boom"},
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"content": '{"ok":true}'}}]},
    )
    with LLMClient(_cfg()) as client:
        assert client.chat_json(system="s", user="u") == {"ok": True}


def test_chat_json_raises_after_three_5xx(httpx_mock: HTTPXMock, monkeypatch):
    monkeypatch.setattr("retrace.llm.client.time.sleep", lambda _s: None)
    for _ in range(3):
        httpx_mock.add_response(
            method="POST",
            url="http://localhost:8080/v1/chat/completions",
            status_code=500,
            json={"error": "boom"},
        )
    with LLMClient(_cfg()) as client, pytest.raises(LLMError):
        client.chat_json(system="s", user="u")


def test_chat_json_raises_on_non_object_json(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"content": "[1,2,3]"}}]},
    )
    with LLMClient(_cfg()) as client, pytest.raises(LLMError):
        client.chat_json(system="s", user="u")


def test_chat_json_sends_no_auth_header_when_api_key_none(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"content": "{}"}}]},
    )
    with LLMClient(_cfg(api_key=None)) as client:
        client.chat_json(system="s", user="u")
    req = httpx_mock.get_requests()[-1]
    assert "authorization" not in {k.lower() for k in req.headers}


def test_chat_json_anthropic_uses_messages_endpoint_and_parses_content(
    httpx_mock: HTTPXMock,
):
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": '{"ok": true}'}]},
    )
    with LLMClient(
        _cfg(
            provider="anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key="sk-ant-test",
        )
    ) as client:
        assert client.chat_json(system="s", user="u") == {"ok": True}
    req = httpx_mock.get_requests()[-1]
    assert req.headers["x-api-key"] == "sk-ant-test"
    assert "anthropic-version" in {k.lower() for k in req.headers}


def test_chat_json_anthropic_raises_on_missing_text_content(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "tool_use", "name": "x"}]},
    )
    with (
        LLMClient(
            _cfg(
                provider="anthropic",
                base_url="https://api.anthropic.com/v1",
                api_key="sk-ant-test",
            )
        ) as client,
        pytest.raises(LLMError),
    ):
        client.chat_json(system="s", user="u")


def test_chat_visual_json_inlines_image_for_openai_compatible(
    tmp_path, httpx_mock: HTTPXMock
):
    """RET-22: visual mode sends a base64-encoded screenshot as image_url."""
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG-fake-bytes")
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"content": '{"tool":"finish","args":{"status":"success"}}'}}]},
    )
    with LLMClient(_cfg(api_key="sk-fake")) as client:
        out = client.chat_visual_json(
            system="s", user="u", image_path=str(img)
        )
    assert out == {"tool": "finish", "args": {"status": "success"}}
    req = httpx_mock.get_requests()[-1]
    body = req.read()
    # The user content list contains the screenshot inline as a data URL.
    assert b"image_url" in body
    assert b"data:image/png;base64," in body


def test_chat_visual_json_inlines_image_for_anthropic(
    tmp_path, httpx_mock: HTTPXMock
):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG-fake-bytes")
    httpx_mock.add_response(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": '{"tool":"finish","args":{"status":"success"}}'}]},
    )
    with LLMClient(
        _cfg(
            provider="anthropic",
            base_url="https://api.anthropic.com/v1",
            api_key="sk-ant-test",
        )
    ) as client:
        out = client.chat_visual_json(
            system="s", user="u", image_path=str(img)
        )
    assert out == {"tool": "finish", "args": {"status": "success"}}
    body = httpx_mock.get_requests()[-1].read()
    # Anthropic uses {"type":"image","source":{"type":"base64",...}}
    assert b'"type":"image"' in body
    assert b'"media_type":"image/png"' in body


def test_chat_visual_json_falls_back_to_text_when_image_missing(
    tmp_path, httpx_mock: HTTPXMock
):
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={"choices": [{"message": {"content": '{"ok":true}'}}]},
    )
    with LLMClient(_cfg(api_key="sk-fake")) as client:
        # image_path doesn't exist - the request should still go through with
        # only text content, so a flaky screenshot doesn't take the run down.
        out = client.chat_visual_json(
            system="s", user="u", image_path=str(tmp_path / "missing.png")
        )
    assert out == {"ok": True}
    body = httpx_mock.get_requests()[-1].read()
    assert b"image_url" not in body