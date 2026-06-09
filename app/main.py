import logging
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent_client import AgentClient
from app.config import settings
from app.models import (
    AgentsResponse,
    AgentSummary,
    ApproveStepRequest,
    CancelWorkflowRequest,
    CreateSessionRequest,
    RecentSessionsResponse,
    RejectStepRequest,
    StepId,
    WorkflowStateResponse,
)
from app.websocket_manager import WebSocketManager
from app.workflow_engine import WorkflowEngine
from repositories.base import BaseSessionRepository
from repositories.memory_repository import InMemorySessionRepository


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("app.config").info(
        "DAISYNOVA_API_TOKEN configured: %s",
        "yes" if bool(settings.daisynova_api_token) else "no",
    )
    logging.getLogger("app.config").info(
        "ALLOW_AGENT_MOCK_FALLBACK enabled: %s",
        "yes" if settings.allow_agent_mock_fallback else "no",
    )


def validate_runtime_configuration() -> None:
    enabled_agents = [
        agent.name
        for agent in (
            settings.atlas,
            settings.audit,
            settings.media_planner,
            settings.geo_fence,
            settings.meta,
        )
        if agent.enabled
    ]
    if enabled_agents and not settings.daisynova_api_token:
        if settings.allow_agent_mock_fallback:
            logging.getLogger("app.config").warning(
                "DaisyNova API token is missing, but startup is continuing because "
                "ALLOW_AGENT_MOCK_FALLBACK=true."
            )
            return
        raise RuntimeError("DaisyNova API token is missing.")


def create_repository() -> BaseSessionRepository:
    backend = settings.storage_backend.lower()
    if backend == "memory":
        return InMemorySessionRepository()
    if backend == "postgres":
        if not settings.database_url:
            raise ValueError("DATABASE_URL must be set when STORAGE_BACKEND=postgres.")
        from repositories.postgres_repository import PostgresSessionRepository

        return PostgresSessionRepository(settings.database_url)
    raise ValueError("STORAGE_BACKEND must be either 'memory' or 'postgres'.")


def create_app(
    repository: BaseSessionRepository | None = None,
    agent_client: AgentClient | None = None,
) -> FastAPI:
    configure_logging()
    validate_runtime_configuration()
    repository = repository or create_repository()
    agent_client = agent_client or AgentClient()
    websocket_manager = WebSocketManager()
    workflow_engine = WorkflowEngine(
        repository=repository,
        agent_client=agent_client,
        websocket_manager=websocket_manager,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await repository.initialize()
        try:
            yield
        finally:
            await repository.close()

    app = FastAPI(
        title="Multi-Agent Workflow Orchestration API",
        version="1.2.0",
        lifespan=lifespan,
    )
    app.state.repository = repository
    app.state.agent_client = agent_client
    app.state.workflow_engine = workflow_engine
    app.state.websocket_manager = websocket_manager

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=settings.cors_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def healthcheck() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/agents", response_model=AgentsResponse)
    async def list_agents() -> AgentsResponse:
        agents = [
            AgentSummary(
                name=agent.name,
                step_id=agent.step_id,
                agent_id=agent.agent_id,
                transport=agent.transport,
                enabled=agent.enabled,
                endpoint=agent.endpoint,
            )
            for agent in (
                settings.atlas,
                settings.audit,
                settings.media_planner,
                settings.geo_fence,
                settings.meta,
            )
        ]
        return AgentsResponse(agents=agents)

    @app.post(
        "/sessions",
        response_model=WorkflowStateResponse,
        status_code=201,
        summary="Create a workflow session",
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "default": {
                                "summary": "Create session",
                                "value": {"url": "https://example.com", "user_id": "user_123"},
                            }
                        }
                    }
                }
            }
        },
    )
    async def create_session(request: CreateSessionRequest) -> WorkflowStateResponse:
        session = await workflow_engine.create_session(request)
        return workflow_engine.build_workflow_response(session)

    @app.get(
        "/sessions/recent",
        response_model=RecentSessionsResponse,
        summary="List recent workflow sessions",
    )
    async def list_recent_sessions(limit: int = 6) -> RecentSessionsResponse:
        safe_limit = min(max(limit, 1), 20)
        return await workflow_engine.list_recent_sessions(limit=safe_limit)

    @app.get("/sessions/{session_id}", response_model=WorkflowStateResponse, summary="Get workflow session state")
    async def get_session(session_id: str) -> WorkflowStateResponse:
        session = await workflow_engine.get_session(session_id)
        return workflow_engine.build_workflow_response(session)

    @app.post(
        "/sessions/{session_id}/steps/{step_id}/approve",
        response_model=WorkflowStateResponse,
        summary="Approve a workflow step",
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "default": {
                                "summary": "Approve step",
                                "value": {},
                            }
                        }
                    }
                }
            }
        },
    )
    async def approve_step(
        session_id: str,
        step_id: StepId,
        request: ApproveStepRequest = Body(default_factory=ApproveStepRequest),
    ) -> WorkflowStateResponse:
        session = await workflow_engine.approve_step(
            session_id,
            step_id=step_id,
            approved_output=request.approved_output,
        )
        return workflow_engine.build_workflow_response(session)

    @app.post(
        "/sessions/{session_id}/steps/{step_id}/reject",
        response_model=WorkflowStateResponse,
        summary="Reject a workflow step",
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "default": {
                                "summary": "Reject step",
                                "value": {"reason": "User rejected this output"},
                            }
                        }
                    }
                }
            }
        },
    )
    async def reject_step(
        session_id: str,
        step_id: StepId,
        request: RejectStepRequest,
    ) -> WorkflowStateResponse:
        session = await workflow_engine.reject_step(session_id, step_id=step_id, reason=request.reason)
        return workflow_engine.build_workflow_response(session)

    @app.post(
        "/sessions/{session_id}/steps/{step_id}/retry",
        response_model=WorkflowStateResponse,
        summary="Retry a failed workflow step",
    )
    async def retry_step(
        session_id: str,
        step_id: StepId,
    ) -> WorkflowStateResponse:
        session = await workflow_engine.retry_step(session_id, step_id=step_id)
        return workflow_engine.build_workflow_response(session)

    @app.post(
        "/sessions/{session_id}/cancel",
        response_model=WorkflowStateResponse,
        summary="Cancel a workflow session",
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "default": {
                                "summary": "Cancel workflow",
                                "value": {"reason": "User cancelled workflow"},
                            }
                        }
                    }
                }
            }
        },
    )
    async def cancel_workflow(
        session_id: str,
        request: CancelWorkflowRequest,
    ) -> WorkflowStateResponse:
        session = await workflow_engine.cancel_workflow(session_id, reason=request.reason)
        return workflow_engine.build_workflow_response(session)

    @app.websocket("/ws/sessions/{session_id}")
    async def session_websocket(session_id: str, websocket: WebSocket) -> None:
        await websocket_manager.connect(session_id, websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            websocket_manager.disconnect(session_id, websocket)
        except Exception:
            websocket_manager.disconnect(session_id, websocket)

    return app


app = create_app()
