import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent_client import AgentClientError
from app.main import create_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SuccessAgentClient:
    def __init__(self):
        self.calls: list[dict[str, str | None]] = []

    async def run_agent(self, agent, task, user_id, agent_session_id=None):
        self.calls.append(
            {"step_id": agent.step_id, "task": task, "user_id": user_id, "agent_session_id": agent_session_id}
        )
        await asyncio.sleep(0.01)
        return {
            "content": {
                "agent": agent.step_id,
                "task": task,
                "user_id": user_id,
            },
            "text": f"{agent.step_id} complete",
            "raw": {"ok": True},
        }


class MappingAwareAgentClient:
    def __init__(self):
        self.calls: list[dict[str, str | None]] = []

    async def run_agent(self, agent, task, user_id, agent_session_id=None):
        self.calls.append(
            {"step_id": agent.step_id, "task": task, "user_id": user_id, "agent_session_id": agent_session_id}
        )
        await asyncio.sleep(0.01)
        return {
            "content": {
                "agent": agent.step_id,
                "task": task,
                "user_id": user_id,
            },
            "text": f"{agent.step_id} complete",
            "raw": {"ok": True},
        }


class FailingAgentClient:
    async def run_agent(self, agent, task, user_id, agent_session_id=None):
        raise AgentClientError(f"{agent.name} failed in test")


class FlakyAgentClient:
    def __init__(self):
        self.calls: list[dict[str, str | None]] = []
        self._failed_steps: set[str] = set()

    async def run_agent(self, agent, task, user_id, agent_session_id=None):
        self.calls.append(
            {"step_id": agent.step_id, "task": task, "user_id": user_id, "agent_session_id": agent_session_id}
        )
        await asyncio.sleep(0.01)
        if agent.step_id not in self._failed_steps:
            self._failed_steps.add(agent.step_id)
            raise AgentClientError(f"{agent.name} failed once in test")
        return {
            "content": {
                "agent": agent.step_id,
                "task": task,
                "user_id": user_id,
            },
            "text": f"{agent.step_id} complete",
            "raw": {"ok": True},
        }


class AuditSessionClient(SuccessAgentClient):
    async def run_agent(self, agent, task, user_id, agent_session_id=None):
        self.calls.append(
            {"step_id": agent.step_id, "task": task, "user_id": user_id, "agent_session_id": agent_session_id}
        )
        await asyncio.sleep(0.01)
        result = {
            "content": {
                "agent": agent.step_id,
                "task": task,
                "user_id": user_id,
            },
            "text": f"{agent.step_id} complete",
            "raw": {"ok": True},
        }
        if agent.step_id == "audit":
            result["agent_session_id"] = agent_session_id or "audit-session-123"
        return result


class VerboseMetaClient(SuccessAgentClient):
    async def run_agent(self, agent, task, user_id, agent_session_id=None):
        self.calls.append(
            {"step_id": agent.step_id, "task": task, "user_id": user_id, "agent_session_id": agent_session_id}
        )
        await asyncio.sleep(0.01)
        if agent.step_id == "meta":
            return {
                "content": "Campaign created",
                "text": "Campaign created",
                "raw": {
                    "exec_id": 1234,
                    "usage": {"prompt_tokens": 999},
                    "logs": [{"message": "very noisy internal log"}],
                },
            }
        return {
            "content": {
                "agent": agent.step_id,
                "task": task,
                "user_id": user_id,
            },
            "text": f"{agent.step_id} complete",
            "raw": {"ok": True},
        }


@pytest.mark.anyio
async def test_invalid_approval_returns_conflict():
    app = create_app(agent_client=SuccessAgentClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]

        response = await client.post(
            f"/sessions/{session_id}/steps/media_planner/approve",
            json={"approved_output": {"plan": "approved"}},
        )

    assert response.status_code == 409
    assert "not waiting for approval" in response.json()["detail"]


@pytest.mark.anyio
async def test_failed_agent_call_marks_step_and_workflow_failed():
    app = create_app(agent_client=FailingAgentClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await asyncio.sleep(0.05)

        response = await client.get(f"/sessions/{session_id}")

    assert response.status_code == 200
    session = response.json()["session"]
    assert session["workflow_status"] == "FAILED"
    assert session["steps"]["atlas"]["status"] == "FAILED"
    assert "failed in test" in session["steps"]["atlas"]["error"]


@pytest.mark.anyio
async def test_retry_failed_step_reruns_same_agent_without_triggering_downstream():
    agent_client = FlakyAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await asyncio.sleep(0.05)

        failed_session = (await client.get(f"/sessions/{session_id}")).json()["session"]
        assert failed_session["workflow_status"] == "FAILED"
        assert failed_session["steps"]["atlas"]["status"] == "FAILED"

        retry_response = await client.post(f"/sessions/{session_id}/steps/atlas/retry")
        assert retry_response.status_code == 200
        retry_session = retry_response.json()["session"]
        assert retry_session["steps"]["atlas"]["status"] == "RUNNING"

        session = await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

    assert session["workflow_status"] == "WAITING_FOR_APPROVAL"
    assert session["steps"]["media_planner"]["status"] == "PENDING"
    assert [call["step_id"] for call in agent_client.calls].count("atlas") == 2


async def wait_for_step_status(client: AsyncClient, session_id: str, step_id: str, expected_status: str):
    for _ in range(30):
        response = await client.get(f"/sessions/{session_id}")
        session = response.json()["session"]
        if session["steps"][step_id]["status"] == expected_status:
            return session
        await asyncio.sleep(0.02)
    raise AssertionError(f"Step {step_id} did not reach status {expected_status}")


@pytest.mark.anyio
async def test_reject_waiting_step_regenerates_same_agent():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

        response = await client.post(
            f"/sessions/{session_id}/steps/atlas/reject",
            json={"reason": "User rejected this output"},
        )
        assert response.status_code == 200
        running_session = response.json()["session"]
        assert running_session["steps"]["atlas"]["status"] == "RUNNING"
        session = await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

    assert session["workflow_status"] == "WAITING_FOR_APPROVAL"
    assert session["steps"]["atlas"]["status"] == "WAITING_FOR_APPROVAL"
    assert session["steps"]["atlas"]["user_feedback_history"][-1] == "User rejected this output"
    assert [call["step_id"] for call in agent_client.calls].count("atlas") == 2


@pytest.mark.anyio
async def test_reject_increments_revision_count():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

        response = await client.post(
            f"/sessions/{session_id}/steps/atlas/reject",
            json={"reason": "Needs more detail"},
        )
        assert response.status_code == 200
        session = await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

    assert session["steps"]["atlas"]["revision_count"] == 1


@pytest.mark.anyio
async def test_after_reject_regeneration_step_returns_to_waiting_for_approval():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

        await client.post(
            f"/sessions/{session_id}/steps/atlas/reject",
            json={"reason": "Needs more detail"},
        )
        session = await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

    assert session["steps"]["atlas"]["status"] == "WAITING_FOR_APPROVAL"


@pytest.mark.anyio
async def test_reject_does_not_cancel_workflow():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

        await client.post(
            f"/sessions/{session_id}/steps/atlas/reject",
            json={"reason": "Needs more detail"},
        )
        session = await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

    assert session["workflow_status"] != "CANCELLED"
    assert session["workflow_status"] == "WAITING_FOR_APPROVAL"


@pytest.mark.anyio
async def test_reject_does_not_trigger_downstream_agents():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

        await client.post(
            f"/sessions/{session_id}/steps/atlas/reject",
            json={"reason": "Needs more detail"},
        )
        session = await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

    assert session["steps"]["media_planner"]["status"] == "PENDING"
    assert [call["step_id"] for call in agent_client.calls].count("media_planner") == 0


@pytest.mark.anyio
async def test_reject_approved_step_returns_error():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

        approve_response = await client.post(
            f"/sessions/{session_id}/steps/atlas/approve",
            json={},
        )
        assert approve_response.status_code == 200

        response = await client.post(
            f"/sessions/{session_id}/steps/atlas/reject",
            json={"reason": "Too late"},
        )

    assert response.status_code == 409
    assert "not waiting for approval" in response.json()["detail"]


@pytest.mark.anyio
async def test_cancel_endpoint_marks_workflow_cancelled():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

        response = await client.post(
            f"/sessions/{session_id}/cancel",
            json={"reason": "User cancelled workflow"},
        )

    assert response.status_code == 200
    session = response.json()["session"]
    assert session["workflow_status"] == "CANCELLED"


@pytest.mark.anyio
async def test_downstream_agent_starts_only_after_approval():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")
        await wait_for_step_status(client, session_id, "audit", "WAITING_FOR_APPROVAL")

        first_session = (await client.get(f"/sessions/{session_id}")).json()["session"]
        assert first_session["steps"]["media_planner"]["status"] == "PENDING"

        await client.post(
            f"/sessions/{session_id}/steps/atlas/approve",
            json={},
        )
        middle_session = (await client.get(f"/sessions/{session_id}")).json()["session"]
        assert middle_session["steps"]["media_planner"]["status"] == "PENDING"

        await client.post(
            f"/sessions/{session_id}/steps/audit/approve",
            json={},
        )
        final_session = await wait_for_step_status(client, session_id, "media_planner", "WAITING_FOR_APPROVAL")

    assert final_session["steps"]["media_planner"]["status"] == "WAITING_FOR_APPROVAL"
    assert [call["step_id"] for call in agent_client.calls].count("media_planner") == 1


@pytest.mark.anyio
async def test_available_actions_only_show_for_waiting_steps():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        session = await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

    assert session["steps"]["atlas"]["available_actions"] == ["approve", "reject"]
    assert session["steps"]["media_planner"]["available_actions"] == []


@pytest.mark.anyio
async def test_user_cannot_go_back_and_regenerate_after_approval():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

        approve_response = await client.post(
            f"/sessions/{session_id}/steps/atlas/approve",
            json={},
        )
        assert approve_response.status_code == 200

        reject_response = await client.post(
            f"/sessions/{session_id}/steps/atlas/reject",
            json={"reason": "Try again"},
        )

    assert reject_response.status_code == 409
    assert "not waiting for approval" in reject_response.json()["detail"]


@pytest.mark.anyio
async def test_agents_endpoint_shows_meta_enabled_and_transport_details():
    app = create_app(agent_client=SuccessAgentClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/agents")

    assert response.status_code == 200
    agents = {agent["step_id"]: agent for agent in response.json()["agents"]}
    assert all(agent["transport"] == "run" for agent in agents.values())
    assert agents["audit"]["transport"] == "run"
    assert agents["audit"]["endpoint"] == "https://aiagents.daisynova.com/api/agents/14/run"
    assert agents["meta"]["enabled"] is True
    assert agents["meta"]["agent_id"] == 70
    assert agents["geo_fence"]["agent_id"] == 74
    assert "requires_cookie" not in agents["audit"]


@pytest.mark.anyio
async def test_recent_sessions_returns_latest_six_sessions():
    app = create_app(agent_client=SuccessAgentClient())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        created_session_ids = []
        for index in range(7):
            response = await client.post(
                "/sessions",
                json={"url": f"https://example{index}.com", "user_id": "user_123"},
            )
            created_session_ids.append(response.json()["session"]["session_id"])
            await asyncio.sleep(0.02)

        recent_response = await client.get("/sessions/recent")

    assert recent_response.status_code == 200
    sessions = recent_response.json()["sessions"]
    assert len(sessions) == 6
    returned_ids = [session["session_id"] for session in sessions]
    assert created_session_ids[-1] in returned_ids
    assert created_session_ids[0] not in returned_ids
    updated_at_values = [session["updated_at"] for session in sessions]
    assert updated_at_values == sorted(updated_at_values, reverse=True)


@pytest.mark.anyio
async def test_reject_audit_reuses_agent_session_id():
    agent_client = AuditSessionClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        session = await wait_for_step_status(client, session_id, "audit", "WAITING_FOR_APPROVAL")
        assert session["steps"]["audit"]["agent_session_id"] == "audit-session-123"

        response = await client.post(
            f"/sessions/{session_id}/steps/audit/reject",
            json={"reason": "Please regenerate the audit"},
        )
        assert response.status_code == 200
        session = await wait_for_step_status(client, session_id, "audit", "WAITING_FOR_APPROVAL")

    assert session["steps"]["audit"]["agent_session_id"] == "audit-session-123"
    audit_calls = [call for call in agent_client.calls if call["step_id"] == "audit"]
    assert len(audit_calls) == 2
    assert audit_calls[1]["agent_session_id"] == "audit-session-123"


@pytest.mark.anyio
async def test_reject_regeneration_uses_compact_previous_output_summary():
    agent_client = VerboseMetaClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")
        await wait_for_step_status(client, session_id, "audit", "WAITING_FOR_APPROVAL")

        await client.post(f"/sessions/{session_id}/steps/atlas/approve", json={})
        await client.post(f"/sessions/{session_id}/steps/audit/approve", json={})
        await wait_for_step_status(client, session_id, "media_planner", "WAITING_FOR_APPROVAL")

        await client.post(f"/sessions/{session_id}/steps/media_planner/approve", json={})
        await wait_for_step_status(client, session_id, "meta", "WAITING_FOR_APPROVAL")

        await client.post(
            f"/sessions/{session_id}/steps/meta/reject",
            json={"reason": "Please revise this"},
        )
        await wait_for_step_status(client, session_id, "meta", "WAITING_FOR_APPROVAL")

    meta_calls = [call for call in agent_client.calls if call["step_id"] == "meta"]
    assert len(meta_calls) == 2
    regeneration_task = meta_calls[1]["task"]
    assert "Campaign created" in regeneration_task
    assert "very noisy internal log" not in regeneration_task
    assert '"exec_id":1234' not in regeneration_task
    assert '"usage"' not in regeneration_task
    assert '"logs"' not in regeneration_task


@pytest.mark.anyio
async def test_frontend_cards_and_progress_are_included():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        response = await client.get(f"/sessions/{session_id}")
        payload = response.json()

    assert response.status_code == 200
    assert payload["current_stage"] == "INITIAL_ANALYSIS"
    assert payload["progress"]["total_steps"] == 5
    assert set(payload["progress"]["running_steps"] + payload["progress"]["waiting_for_approval_steps"]) >= {
        "atlas",
        "audit",
    }
    cards = {card["step_id"]: card for card in payload["frontend_cards"]}
    assert len(cards) == 5
    assert cards["atlas"]["title"] == "Atlas Agent"
    assert "available_actions" in cards["atlas"]
    assert "summary" in cards["atlas"]
    assert "mapped_input_preview" in cards["atlas"]
    graph = payload["workflow_graph"]
    assert len(graph["nodes"]) == 5
    assert {"from": "atlas", "to": "media_planner"} in graph["edges"]
    atlas_node = next(node for node in graph["nodes"] if node["id"] == "atlas")
    assert atlas_node["label"] == "Atlas"


@pytest.mark.anyio
async def test_approving_with_empty_body_stores_extracted_raw_output():
    agent_client = SuccessAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")

        response = await client.post(
            f"/sessions/{session_id}/steps/atlas/approve",
            json={},
        )

    assert response.status_code == 200
    session = response.json()["session"]
    assert session["steps"]["atlas"]["approved_output"] == {
        "agent": "atlas",
        "task": "Analyze this brand URL for strategic brand intelligence: https://example.com/",
        "user_id": "user_123",
    }


@pytest.mark.anyio
async def test_media_planner_receives_mapped_atlas_and_audit_content():
    agent_client = MappingAwareAgentClient()
    app = create_app(agent_client=agent_client)

    atlas_approved = {
        "content": {"summary": "Brand positioning summary"},
        "raw": {"sensitive": "atlas raw should not pass"},
    }
    audit_approved = {
        "result": {"summary": "Audit insights"},
        "raw": {"sensitive": "audit raw should not pass"},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")
        await wait_for_step_status(client, session_id, "audit", "WAITING_FOR_APPROVAL")

        await client.post(
            f"/sessions/{session_id}/steps/atlas/approve",
            json={"approved_output": atlas_approved},
        )
        await client.post(
            f"/sessions/{session_id}/steps/audit/approve",
            json={"approved_output": audit_approved},
        )
        await wait_for_step_status(client, session_id, "media_planner", "WAITING_FOR_APPROVAL")

    media_call = next(call for call in agent_client.calls if call["step_id"] == "media_planner")
    assert "Brand positioning summary" in media_call["task"]
    assert "Audit insights" in media_call["task"]
    assert "atlas raw should not pass" not in media_call["task"]
    assert "audit raw should not pass" not in media_call["task"]
    assert '"brand_intelligence":{"summary":"Brand positioning summary"}' in media_call["task"]
    assert '"audit_findings":{"summary":"Audit insights"}' in media_call["task"]


@pytest.mark.anyio
async def test_media_planner_starts_after_empty_body_approvals_and_uses_mapped_content():
    agent_client = MappingAwareAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")
        await wait_for_step_status(client, session_id, "audit", "WAITING_FOR_APPROVAL")

        await client.post(f"/sessions/{session_id}/steps/atlas/approve", json={})
        await client.post(f"/sessions/{session_id}/steps/audit/approve", json={})
        session = await wait_for_step_status(client, session_id, "media_planner", "WAITING_FOR_APPROVAL")

    assert session["steps"]["media_planner"]["input_task"] is not None
    assert '"brand_intelligence":{' in session["steps"]["media_planner"]["input_task"]
    assert '"audit_findings":{' in session["steps"]["media_planner"]["input_task"]
    assert [call["step_id"] for call in agent_client.calls].count("media_planner") == 1


@pytest.mark.anyio
async def test_geo_fence_receives_only_mapped_media_planner_content():
    agent_client = MappingAwareAgentClient()
    app = create_app(agent_client=agent_client)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/sessions",
            json={"url": "https://example.com", "user_id": "user_123"},
        )
        session_id = create_response.json()["session"]["session_id"]
        await wait_for_step_status(client, session_id, "atlas", "WAITING_FOR_APPROVAL")
        await wait_for_step_status(client, session_id, "audit", "WAITING_FOR_APPROVAL")

        await client.post(
            f"/sessions/{session_id}/steps/atlas/approve",
            json={"approved_output": {"content": {"summary": "Atlas approved"}}},
        )
        await client.post(
            f"/sessions/{session_id}/steps/audit/approve",
            json={"approved_output": {"content": {"summary": "Audit approved"}}},
        )
        await wait_for_step_status(client, session_id, "media_planner", "WAITING_FOR_APPROVAL")

        media_plan_output = {
            "data": {"summary": "Mapped media plan"},
            "raw": {"internal_notes": "should not be passed downstream"},
        }
        await client.post(
            f"/sessions/{session_id}/steps/media_planner/approve",
            json={"approved_output": media_plan_output},
        )
        await wait_for_step_status(client, session_id, "geo_fence", "WAITING_FOR_APPROVAL")

    geo_call = next(call for call in agent_client.calls if call["step_id"] == "geo_fence")
    assert "Mapped media plan" in geo_call["task"]
    assert "should not be passed downstream" not in geo_call["task"]
    assert '"media_plan":{"summary":"Mapped media plan"}' in geo_call["task"]
