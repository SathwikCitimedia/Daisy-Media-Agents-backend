from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator


class StepStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class WorkflowStatus(str, Enum):
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CurrentStage(str, Enum):
    INITIAL_ANALYSIS = "INITIAL_ANALYSIS"
    MEDIA_PLANNING = "MEDIA_PLANNING"
    ACTIVATION = "ACTIVATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


StepId = Literal["atlas", "audit", "media_planner", "geo_fence", "meta"]
StepAction = Literal["approve", "reject"]


class AgentResponse(BaseModel):
    content: Any
    text: str | None = None
    raw: Any


class WorkflowStep(BaseModel):
    session_id: str
    step_id: StepId
    status: StepStatus
    agent_session_id: str | None = None
    input_task: str | None = None
    mapped_input_preview: Any | None = None
    raw_output: Any | None = None
    approved_output: Any | None = None
    user_feedback_history: list[str] = Field(default_factory=list)
    rejection_reason: str | None = None
    revision_count: int = 0
    error: str | None = None
    available_actions: list[StepAction] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: utc_now())


class WorkflowSession(BaseModel):
    session_id: str
    url: HttpUrl
    user_id: str
    steps: dict[StepId, WorkflowStep]
    workflow_status: WorkflowStatus = WorkflowStatus.RUNNING
    updated_at: datetime = Field(default_factory=lambda: utc_now())


class CreateSessionRequest(BaseModel):
    url: HttpUrl
    user_id: str | None = None

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("user_id cannot be empty.")
        return stripped


class ApproveStepRequest(BaseModel):
    approved_output: Any | None = None


class RejectStepRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason cannot be empty.")
        return stripped


class CancelWorkflowRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason cannot be empty.")
        return stripped


class WorkflowStateResponse(BaseModel):
    session: WorkflowSession
    current_stage: CurrentStage
    progress: "WorkflowProgress"
    frontend_cards: list["FrontendCard"]
    workflow_graph: "WorkflowGraph"


class RecentSessionSummary(BaseModel):
    session_id: str
    url: HttpUrl
    workflow_status: WorkflowStatus
    current_stage: CurrentStage
    updated_at: datetime
    progress: "WorkflowProgress"


class RecentSessionsResponse(BaseModel):
    sessions: list[RecentSessionSummary]


class AgentSummary(BaseModel):
    name: str
    step_id: StepId
    agent_id: int | None
    transport: str
    enabled: bool
    endpoint: str | None


class AgentsResponse(BaseModel):
    agents: list[AgentSummary]


class WorkflowProgress(BaseModel):
    total_steps: int
    completed_steps: int
    waiting_for_approval_steps: list[StepId]
    running_steps: list[StepId]
    failed_steps: list[StepId]


class FrontendCard(BaseModel):
    step_id: StepId
    title: str
    status: StepStatus
    summary: str
    output: Any
    mapped_input_preview: Any | None = None
    available_actions: list[StepAction]


class WorkflowGraphNode(BaseModel):
    id: StepId
    label: str
    status: StepStatus


class WorkflowGraphEdge(BaseModel):
    from_: StepId = Field(alias="from")
    to: StepId


class WorkflowGraph(BaseModel):
    nodes: list[WorkflowGraphNode]
    edges: list[WorkflowGraphEdge]


def build_default_steps(session_id: str) -> dict[StepId, WorkflowStep]:
    return {
        "atlas": WorkflowStep(
            session_id=session_id,
            step_id="atlas",
            status=StepStatus.PENDING,
        ),
        "audit": WorkflowStep(
            session_id=session_id,
            step_id="audit",
            status=StepStatus.PENDING,
        ),
        "media_planner": WorkflowStep(
            session_id=session_id,
            step_id="media_planner",
            status=StepStatus.PENDING,
        ),
        "geo_fence": WorkflowStep(
            session_id=session_id,
            step_id="geo_fence",
            status=StepStatus.PENDING,
        ),
        "meta": WorkflowStep(
            session_id=session_id,
            step_id="meta",
            status=StepStatus.PENDING,
        ),
    }


def new_session_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def derive_workflow_status(steps: dict[StepId, WorkflowStep]) -> WorkflowStatus:
    statuses = [step.status for step in steps.values()]
    if any(status in {StepStatus.REJECTED, StepStatus.CANCELLED} for status in statuses):
        return WorkflowStatus.CANCELLED
    if any(status == StepStatus.FAILED for status in statuses):
        return WorkflowStatus.FAILED
    if any(status == StepStatus.WAITING_FOR_APPROVAL for status in statuses):
        return WorkflowStatus.WAITING_FOR_APPROVAL

    final_statuses = {StepStatus.APPROVED, StepStatus.SKIPPED}
    if all(status in final_statuses for status in statuses):
        return WorkflowStatus.COMPLETED

    return WorkflowStatus.RUNNING
