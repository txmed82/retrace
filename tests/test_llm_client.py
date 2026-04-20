from pytest_httpx import HTTPXMock

from retrace.config import LLMConfig
from retrace.llm.client import LLMClient


def test_chat_json_returns_parsed_dict(httpx_mock: HTTPXMock):
    cfg = LLMConfig(base_url="http://localhost:8080/v1", model="test", api_key=None)
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
    client = LLMClient(cfg)
    out = client.chat_json(
        system="you are a QA analyst",
        user="analyze this",
    )
    assert out == {"title": "x", "severity": "high"}


def test_chat_json_strips_code_fences(httpx_mock: HTTPXMock):
    cfg = LLMConfig(base_url="http://localhost:8080/v1", model="test", api_key=None)
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:8080/v1/chat/completions",
        json={
            "choices": [
                {"message": {"role": "assistant", "content": '```json\n{"a":1}\n```'}}
            ]
        },
    )
    client = LLMClient(cfg)
    assert client.chat_json(system="s", user="u") == {"a": 1}
