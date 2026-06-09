import pytest

from app.agent_client import AgentClient, AgentClientError
from app.config import AgentSettings, settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


class MockResponse:
    def __init__(self, json_data=None, text="", status_code=200):
        self._json_data = json_data
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://example.com")
            raise httpx.HTTPStatusError("boom", request=request, response=self)

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


class MockAsyncClient:
    def __init__(self, *, timeout, recorder):
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, json=None, headers=None):
        self._recorder["post"] = {"url": url, "json": json, "headers": headers or {}}
        return MockResponse(json_data={"result": {"summary": "ok"}, "session_id": "agent-session-1"})


@pytest.mark.anyio
async def test_run_transport_sends_task_session_id_wait_and_authorization(monkeypatch):
    recorder = {}
    monkeypatch.setattr(settings, "daisynova_api_token", "token-123")
    client = AgentClient(client_factory=lambda **kwargs: MockAsyncClient(recorder=recorder, **kwargs))
    agent = AgentSettings(
        name="Atlas Agent",
        step_id="atlas",
        agent_id=39,
        transport="run",
        url="https://aiagents.daisynova.com/api/agents/39/run",
    )

    result = await client.run_agent(agent=agent, task="analyze", user_id="user_123", agent_session_id="sess-1")

    assert recorder["post"]["json"] == {"task": "analyze", "session_id": "sess-1", "wait": True}
    assert recorder["post"]["headers"]["Authorization"] == "Bearer token-123"
    assert recorder["post"]["headers"]["Content-Type"] == "application/json"
    assert result["content"] == {"summary": "ok"}
    assert result["agent_session_id"] == "agent-session-1"


@pytest.mark.anyio
async def test_missing_token_fails_clearly(monkeypatch):
    monkeypatch.setattr(settings, "daisynova_api_token", None)
    client = AgentClient(client_factory=lambda **kwargs: MockAsyncClient(recorder={}, **kwargs))
    agent = AgentSettings(
        name="Audit Agent",
        step_id="audit",
        agent_id=14,
        transport="run",
        url="https://aiagents.daisynova.com/api/agents/14/run",
    )

    with pytest.raises(AgentClientError, match="DaisyNova API token is missing."):
        await client.run_agent(agent=agent, task="audit task", user_id="user_123")


def test_duplicate_result_is_collapsed_to_single_main_output():
    client = AgentClient()
    normalized = client._parse_json_response(
        MockResponse(
            json_data={
                "result": "{\"summary\":\"same\"}",
                "session_id": "agent-session-1",
                "usage": {"prompt_tokens": 10},
                "logs": [{"message": "done"}],
            }
        )
    )

    assert normalized["content"] == "{\"summary\":\"same\"}"
    assert normalized["text"] is None
    assert "result" not in normalized["raw"]
    assert normalized["raw"]["usage"] == {"prompt_tokens": 10}
    assert normalized["agent_session_id"] == "agent-session-1"


@pytest.mark.anyio
def test_empty_payload_with_error_logs_gets_readable_summary():
    client = AgentClient()
    normalized = client._normalize_payload(
        {
            "result": {},
            "logs": [
                {"message": "Native tool call"},
                {"result": "[Error] Meta campaign creation failed because special_ad_categories is required."},
            ],
        }
    ).model_dump()
    normalized = client._apply_error_summary_if_empty(normalized)

    assert "special_ad_categories is required" in normalized["content"]
    assert normalized["error_summary"] == normalized["content"]
