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