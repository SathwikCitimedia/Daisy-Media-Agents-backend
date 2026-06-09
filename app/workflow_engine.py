from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import HTTPException, status

from app.agent_client import AgentClient, AgentClientError
from app.config import AgentSettings, settings
from app.models import (
    CurrentStage,
    CreateSessionRequest,
    FrontendCard,
    RecentSessionSummary,
    RecentSessionsResponse,
    StepId,
    StepAction,
    StepStatus,
    WorkflowGraph,
    WorkflowGraphEdge,
    WorkflowGraphNode,
    WorkflowProgress,
    WorkflowStateResponse,
    WorkflowSession,
    WorkflowStatus,
    WorkflowStep,
    build_default_steps,
    new_session_id,
)
from app.output_mapper import extract_useful_content, map_for_geo_fence, map_for_media_planner, map_for_meta
from app.websocket_manager import WebSocketManager
from repositories.base import BaseSessionRepository, SessionNotFoundError


class WorkflowEngine:
    def __init__(
        self,
        repository: BaseSessionRepository,
        agent_client: AgentClient,
        websocket_manager: WebSocketManager | None = None,
    ) -> None:
        self._repository = repository
        self._agent_client = agent_client
        self._websocket_manager = websocket_manager
        self._tasks: set[asyncio.Task[Any]] = set()

    async def create_session(self, request: CreateSessionRequest) -> WorkflowSession:
        session_id = new_session_id()
        session = WorkflowSession(
            session_id=session_id,
            url=request.url,
            user_id=request.user_id or settings.agent_user_id,
            steps=build_default_steps(session_id),
        )

        session.steps["meta"] = self._build_default_step(session_id, "meta")

        created = await self._repository.create_session(session)
        await self._mark_steps_running(created.session_id, ["atlas", "audit"])
        self._schedule_parallel_runs(created.session_id, ["atlas", "audit"])
        return await self.get_session(created.session_id)

    async def get_session(self, session_id: str) -> WorkflowSession:
        try:
            session = await self._repository.get_session(session_id)
            return self._decorate_session(session)
        except SessionNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Session '{session_id}' was not found.",
            ) from exc

    async def list_recent_sessions(self, limit: int = 6) -> RecentSessionsResponse:
        sessions = await self._repository.list_sessions(limit=limit)
        items = []
        for session in sessions:
            decorated = self._decorate_session(session)
            items.append(
                RecentSessionSummary(
                    session_id=decorated.session_id,
                    url=decorated.url,
                    workflow_status=decorated.workflow_status,
                    current_stage=self.get_current_stage(decorated),
                    updated_at=decorated.updated_at,
                    progress=self.build_progress(decorated),
                )
            )
        return RecentSessionsResponse(sessions=items)

    async def approve_step(
        self,
        session_id: str,
        step_id: StepId,
        approved_output: Any | None,
    ) -> WorkflowSession:
        session = await self.get_session(session_id)
        if session.workflow_status == WorkflowStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Workflow is cancelled and cannot be changed.",
            )
        step = session.steps[step_id]

        if step.status != StepStatus.WAITING_FOR_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Step '{step_id}' is not waiting for approval.",
            )

        resolved_approved_output = approved_output
        if resolved_approved_output is None:
            resolved_approved_output = extract_useful_content(step.raw_output)

        async def updater(working: WorkflowSession) -> WorkflowSession:
            working_step = working.steps[step_id]
            working_step.status = StepStatus.APPROVED
            working_step.approved_output = resolved_approved_output
            working_step.error = None
            return working

        updated = await self._repository.update_session(session_id, updater)
        await self._emit_step_event("STEP_APPROVED", updated, step_id)
        self._trigger_dependents(updated, step_id)
        await self._emit_terminal_workflow_event(updated)
        return await self.get_session(session_id)

    async def reject_step(
        self,
        session_id: str,
        step_id: StepId,
        reason: str,
    ) -> WorkflowSession:
        session = await self.get_session(session_id)
        if session.workflow_status == WorkflowStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Workflow is cancelled and cannot be changed.",
            )
        step = session.steps[step_id]

        if step.status != StepStatus.WAITING_FOR_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Step '{step_id}' is not waiting for approval.",
            )

        session = await self._queue_regeneration(session_id, step_id, reason)
        return await self.get_session(session.session_id)

    async def retry_step(self, session_id: str, step_id: StepId) -> WorkflowSession:
        session = await self.get_session(session_id)
        if session.workflow_status == WorkflowStatus.CANCELLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Workflow is cancelled and cannot be changed.",
            )

        step = session.steps[step_id]
        if step.status != StepStatus.FAILED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Step '{step_id}' is not failed and cannot be retried.",
            )
        if step.input_task is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Step '{step_id}' does not have an original task to retry.",
            )

        async def updater(working: WorkflowSession) -> WorkflowSession:
            working_step = working.steps[step_id]
            working_step.status = StepStatus.RUNNING
            working_step.error = None
            working_step.rejection_reason = None
            return working

        updated = await self._repository.update_session(session_id, updater)
        await self._emit_step_event("STEP_RETRIED", updated, step_id)
        self._schedule_run(session_id, step_id, step.input_task)
        return await self.get_session(session_id)

    async def cancel_workflow(self, session_id: str, reason: str) -> WorkflowSession:
        await self.get_session(session_id)

        async def updater(working: WorkflowSession) -> WorkflowSession:
            for step in working.steps.values():
                if step.status in {StepStatus.PENDING, StepStatus.RUNNING}:
                    step.status = StepStatus.CANCELLED
                    step.error = reason
            return working

        updated = await self._repository.update_session(session_id, updater)
        await self._emit_terminal_workflow_event(updated)
        return await self.get_session(updated.session_id)

    def build_workflow_response(self, session: WorkflowSession) -> WorkflowStateResponse:
        session = self._decorate_session(session)
        return WorkflowStateResponse(
            session=session,
            current_stage=self.get_current_stage(session),
            progress=self.build_progress(session),
            frontend_cards=self.build_frontend_cards(session),
            workflow_graph=self.build_workflow_graph(session),
        )

    def get_current_stage(self, session: WorkflowSession) -> CurrentStage:
        if session.workflow_status.value == "COMPLETED":
            return CurrentStage.COMPLETED
        if session.workflow_status.value == "CANCELLED":
            return CurrentStage.CANCELLED
        if session.workflow_status.value == "FAILED":
            return CurrentStage.FAILED

        media_status = session.steps["media_planner"].status
        activation_steps = [session.steps["geo_fence"].status, session.steps["meta"].status]
        if media_status in {StepStatus.APPROVED, StepStatus.SKIPPED} or any(
            step not in {StepStatus.PENDING, StepStatus.SKIPPED} for step in activation_steps
        ):
            return CurrentStage.ACTIVATION
        if media_status != StepStatus.PENDING:
            return CurrentStage.MEDIA_PLANNING
        return CurrentStage.INITIAL_ANALYSIS

    def get_available_actions(self, step: WorkflowStep) -> list[StepAction]:
        if step.status == StepStatus.WAITING_FOR_APPROVAL:
            return ["approve", "reject"]
        return []

    def build_progress(self, session: WorkflowSession) -> WorkflowProgress:
        steps = session.steps
        completed_statuses = {StepStatus.APPROVED, StepStatus.SKIPPED}
        return WorkflowProgress(
            total_steps=len(steps),
            completed_steps=sum(1 for step in steps.values() if step.status in completed_statuses),
            waiting_for_approval_steps=[
                step_id for step_id, step in steps.items() if step.status == StepStatus.WAITING_FOR_APPROVAL
            ],
            running_steps=[step_id for step_id, step in steps.items() if step.status == StepStatus.RUNNING],
            failed_steps=[
                step_id
                for step_id, step in steps.items()
                if step.status in {StepStatus.FAILED, StepStatus.REJECTED, StepStatus.CANCELLED}
            ],
        )

    def build_frontend_cards(self, session: WorkflowSession) -> list[FrontendCard]:
        cards: list[FrontendCard] = []
        for step_id in ("atlas", "audit", "media_planner", "geo_fence", "meta"):
            step = session.steps[step_id]
            output = step.approved_output if step.approved_output is not None else step.raw_output
            cards.append(
                FrontendCard(
                    step_id=step_id,
                    title=self._get_agent_settings(step_id).name,
                    status=step.status,
                    summary=self._build_step_summary(step),
                    output=output if output is not None else {},
                    mapped_input_preview=step.mapped_input_preview,
                    available_actions=self.get_available_actions(step),
                )
            )
        return cards

    def build_workflow_graph(self, session: WorkflowSession) -> WorkflowGraph:
        return WorkflowGraph(
            nodes=[
                WorkflowGraphNode(id="atlas", label="Atlas", status=session.steps["atlas"].status),
                WorkflowGraphNode(id="audit", label="Audit", status=session.steps["audit"].status),
                WorkflowGraphNode(
                    id="media_planner",
                    label="Media Planner",
                    status=session.steps["media_planner"].status,
                ),
                WorkflowGraphNode(id="geo_fence", label="Geo Fence", status=session.steps["geo_fence"].status),
                WorkflowGraphNode(id="meta", label="Meta", status=session.steps["meta"].status),
            ],
            edges=[
                WorkflowGraphEdge(**{"from": "atlas", "to": "media_planner"}),
                WorkflowGraphEdge(**{"from": "audit", "to": "media_planner"}),
                WorkflowGraphEdge(**{"from": "media_planner", "to": "geo_fence"}),
                WorkflowGraphEdge(**{"from": "media_planner", "to": "meta"}),
            ],
        )

    def _trigger_dependents(self, session: WorkflowSession, approved_step_id: StepId) -> None:
        if approved_step_id in {"atlas", "audit"}:
            if self._are_steps_approved(session, ["atlas", "audit"]):
                media_step = session.steps["media_planner"]
                if media_step.status == StepStatus.PENDING:
                    self._schedule_run(
                        session.session_id,
                        "media_planner",
                        self._build_media_planner_task(session),
                    )
            return

        if approved_step_id == "media_planner":
            geo_step = session.steps["geo_fence"]
            meta_step = session.steps["meta"]
            steps_to_launch: list[StepId] = []
            if geo_step.status == StepStatus.PENDING:
                steps_to_launch.append("geo_fence")
            if meta_step.status == StepStatus.PENDING:
                steps_to_launch.append("meta")

            if steps_to_launch:
                self._schedule_parallel_runs(session.session_id, steps_to_launch)

    def _schedule_parallel_runs(self, session_id: str, step_ids: list[StepId]) -> None:
        task = asyncio.create_task(self._execute_parallel_steps(session_id, step_ids))
        self._track_task(task)

    def _schedule_run(self, session_id: str, step_id: StepId, task_override: str | None) -> None:
        task = asyncio.create_task(self._execute_step(session_id, step_id, task_override))
        self._track_task(task)

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _execute_parallel_steps(self, session_id: str, step_ids: list[StepId]) -> None:
        await self._mark_steps_running(session_id, step_ids)
        coroutines = []
        for step_id in step_ids:
            if step_id == "meta" and (settings.meta.agent_id is None or not settings.meta.enabled):
                coroutines.append(self._mark_meta_skipped(session_id))
            else:
                coroutines.append(self._execute_step(session_id, step_id, None))
        await asyncio.gather(*coroutines)

    async def _execute_step(
        self,
        session_id: str,
        step_id: StepId,
        task_override: str | None,
    ) -> None:
        try:
            session = await self.get_session(session_id)
            if session.workflow_status == WorkflowStatus.CANCELLED:
                return
            agent = self._get_agent_settings(step_id)
            step = session.steps[step_id]
            task_text = task_override or self._build_step_task(session, step_id)

            async def mark_running(working: WorkflowSession) -> WorkflowSession:
                step = working.steps[step_id]
                step.input_task = task_text
                step.mapped_input_preview = self._get_mapped_input_preview(session, step_id)
                step.status = StepStatus.RUNNING
                step.error = None
                step.rejection_reason = None
                return working

            running_session = await self._repository.update_session(session_id, mark_running)
            await self._emit_step_event("STEP_STARTED", running_session, step_id)

            output = await self._agent_client.run_agent(
                agent=agent,
                task=task_text,
                user_id=session.user_id,
                agent_session_id=step.agent_session_id,
            )
            latest_session = await self.get_session(session_id)
            if latest_session.workflow_status == WorkflowStatus.CANCELLED:
                return

            next_agent_session_id: str | None = None
            if isinstance(output, dict):
                next_agent_session_id = output.get("agent_session_id")
                output = {key: value for key, value in output.items() if key != "agent_session_id"}

            async def mark_waiting(working: WorkflowSession) -> WorkflowSession:
                step = working.steps[step_id]
                step.raw_output = output
                step.status = StepStatus.WAITING_FOR_APPROVAL
                step.error = None
                step.rejection_reason = None
                if next_agent_session_id:
                    step.agent_session_id = next_agent_session_id
                return working

            waiting_session = await self._repository.update_session(session_id, mark_waiting)
            await self._emit_step_event("STEP_COMPLETED", waiting_session, step_id)
            await self._emit_step_event("STEP_WAITING_APPROVAL", waiting_session, step_id)
            await self._emit_terminal_workflow_event(waiting_session)
        except HTTPException:
            raise
        except (AgentClientError, SessionNotFoundError) as exc:
            if settings.allow_agent_mock_fallback:
                await self._mark_step_mock_fallback(session_id, step_id, str(exc))
            else:
                await self._mark_step_failed(session_id, step_id, str(exc))
        except Exception as exc:  # pragma: no cover - defensive path
            error_message = f"Unexpected error: {exc}"
            if settings.allow_agent_mock_fallback:
                await self._mark_step_mock_fallback(session_id, step_id, error_message)
            else:
                await self._mark_step_failed(session_id, step_id, error_message)

    async def _mark_meta_skipped(self, session_id: str) -> None:
        async def updater(working: WorkflowSession) -> WorkflowSession:
            meta_step = working.steps["meta"]
            meta_step.status = StepStatus.SKIPPED
            meta_step.error = "Meta Agent is not configured yet."
            return working

        try:
            updated = await self._repository.update_session(session_id, updater)
            await self._emit_terminal_workflow_event(updated)
        except SessionNotFoundError:
            return

    async def _mark_steps_running(self, session_id: str, step_ids: list[StepId]) -> None:
        async def updater(working: WorkflowSession) -> WorkflowSession:
            for step_id in step_ids:
                step = working.steps[step_id]
                if step.status == StepStatus.PENDING:
                    step.status = StepStatus.RUNNING
                    step.error = None
            return working

        try:
            await self._repository.update_session(session_id, updater)
        except SessionNotFoundError:
            return

    async def _mark_step_failed(self, session_id: str, step_id: StepId, error_message: str) -> None:
        async def updater(working: WorkflowSession) -> WorkflowSession:
            step = working.steps[step_id]
            step.status = StepStatus.FAILED
            step.error = error_message
            return working

        try:
            updated = await self._repository.update_session(session_id, updater)
            await self._emit_step_event("STEP_FAILED", updated, step_id, {"error": error_message})
            await self._emit_terminal_workflow_event(updated)
        except SessionNotFoundError:
            return

    async def _mark_step_mock_fallback(self, session_id: str, step_id: StepId, error_message: str) -> None:
        mock_output = {
            "content": f"Mock fallback output for {step_id}. Real agent call failed: {error_message}",
            "text": f"Mock fallback output for {step_id}. Real agent call failed: {error_message}",
            "is_mock": True,
            "original_error": error_message,
        }

        async def updater(working: WorkflowSession) -> WorkflowSession:
            step = working.steps[step_id]
            step.raw_output = mock_output
            step.status = StepStatus.WAITING_FOR_APPROVAL
            step.error = error_message
            return working

        try:
            updated = await self._repository.update_session(session_id, updater)
            await self._emit_step_event(
                "STEP_MOCK_FALLBACK",
                updated,
                step_id,
                {"error": error_message, "is_mock": True},
            )
            await self._emit_step_event("STEP_WAITING_APPROVAL", updated, step_id, {"is_mock": True})
        except SessionNotFoundError:
            return

    async def _queue_regeneration(
        self,
        session_id: str,
        step_id: StepId,
        reason: str,
    ) -> WorkflowSession:
        session = await self.get_session(session_id)
        agent = self._get_agent_settings(step_id)
        if agent.agent_id is None or not agent.enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Step '{step_id}' is not configured for execution.",
            )
        current = session.steps[step_id]
        if current.status == StepStatus.RUNNING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Step '{step_id}' cannot be regenerated while it is running.",
            )
        if current.status != StepStatus.WAITING_FOR_APPROVAL:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Step '{step_id}' is not waiting for approval.",
            )
        if current.input_task is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Step '{step_id}' has not run yet, so it cannot be regenerated.",
            )
        revised_task = self._build_regeneration_task(
            original_task=current.input_task or "",
            previous_output=current.raw_output,
            reason=reason,
        )

        async def updater(working: WorkflowSession) -> WorkflowSession:
            step = working.steps[step_id]
            step.user_feedback_history.append(reason)
            step.input_task = revised_task
            step.status = StepStatus.RUNNING
            step.error = None
            step.approved_output = None
            step.rejection_reason = None
            step.revision_count += 1
            return working

        updated = await self._repository.update_session(session_id, updater)
        await self._emit_step_event("STEP_REJECTED", updated, step_id, {"reason": reason})
        self._schedule_run(session_id, step_id, revised_task)
        return updated

    def _build_step_task(self, session: WorkflowSession, step_id: StepId) -> str:
        if step_id == "atlas":
            return f"Analyze this brand URL for strategic brand intelligence: {session.url}"
        if step_id == "audit":
            return f"Perform a detailed brand audit for this URL: {session.url}"
        if step_id == "media_planner":
            return self._build_media_planner_task(session)
        if step_id == "geo_fence":
            return self._build_geo_fence_task(session)
        if step_id == "meta":
            return self._build_meta_task(session)
        raise ValueError(f"Unsupported step id: {step_id}")

    def _build_media_planner_task(self, session: WorkflowSession) -> str:
        mapped_input = map_for_media_planner(session)
        return (
            "Create a media plan using the approved Atlas and Audit outputs. "
            f"Mapped input: {self._render_output(mapped_input)}"
        )

    def _build_geo_fence_task(self, session: WorkflowSession) -> str:
        mapped_input = map_for_geo_fence(session)
        return (
            "Create a geo-fencing strategy using this structured media plan input. "
            "Use only the provided fields. If geofence_zones are missing, infer reasonable zones from target_locations. "
            f"Mapped input: {self._render_output(mapped_input)}"
        )

    def _build_meta_task(self, session: WorkflowSession) -> str:
        mapped_input = map_for_meta(session)
        return (
            "Create a Meta ads campaign plan using this structured media plan input. "
            "Include special_ad_categories. If no special category applies, use an empty array. "
            f"Mapped input: {self._render_output(mapped_input)}"
        )

    def _build_regeneration_task(self, original_task: str, previous_output: Any, reason: str) -> str:
        previous_output_text = self._render_output(self._summarize_previous_output_for_regeneration(previous_output))
        return (
            "The user did not approve the previous output and requested regeneration.\n"
            f"Original task: {original_task}\n"
            f"Previous output: {previous_output_text}\n"
            f"Reason: {reason}\n"
            "Generate the revised output."
        )

    def _summarize_previous_output_for_regeneration(self, previous_output: Any) -> Any:
        if previous_output is None:
            return None
        if isinstance(previous_output, str):
            return previous_output
        if not isinstance(previous_output, dict):
            return extract_useful_content(previous_output)

        summary: dict[str, Any] = {}
        useful_content = extract_useful_content(previous_output)
        if useful_content not in (None, {}, "{}", ""):
            summary["content"] = useful_content

        text = previous_output.get("text")
        if isinstance(text, str) and text.strip():
            rendered_useful_content = self._render_output(useful_content) if useful_content is not None else None
            rendered_text = text.strip()
            if rendered_useful_content != rendered_text:
                summary["text"] = text

        for key in ("error_summary", "original_error", "is_mock"):
            value = previous_output.get(key)
            if value not in (None, "", {}, []):
                summary[key] = value

        if not summary:
            return useful_content
        return summary

    def _render_approved_output(self, approved_output: Any) -> str:
        if approved_output is None:
            return "No approved output provided."
        return self._render_output(approved_output)

    def _render_output(self, output: Any) -> str:
        if isinstance(output, str):
            return output
        return json.dumps(output, ensure_ascii=True, separators=(",", ":"))

    def _are_steps_approved(self, session: WorkflowSession, step_ids: list[StepId]) -> bool:
        return all(session.steps[step_id].status == StepStatus.APPROVED for step_id in step_ids)

    def _decorate_session(self, session: WorkflowSession) -> WorkflowSession:
        decorated = session.model_copy(deep=True)
        for step in decorated.steps.values():
            step.available_actions = self.get_available_actions(step)
        return decorated

    def _build_step_summary(self, step: WorkflowStep) -> str:
        source = step.approved_output if step.approved_output is not None else step.raw_output
        if source is None:
            if step.rejection_reason:
                return step.rejection_reason
            if step.error:
                return step.error
            return ""
        if isinstance(source, dict):
            if isinstance(source.get("text"), str):
                return source["text"]
            content = source.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, dict):
                for key in ("summary", "text", "message"):
                    value = content.get(key)
                    if isinstance(value, str):
                        return value
            return self._render_output(source)[:240]
        if isinstance(source, str):
            return source
        return self._render_output(source)[:240]

    async def _emit_step_event(
        self,
        event_type: str,
        session: WorkflowSession,
        step_id: StepId,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self._websocket_manager is None:
            return
        await self._websocket_manager.broadcast(
            session.session_id,
            {
                "type": event_type,
                "session_id": session.session_id,
                "step_id": step_id,
                "status": session.steps[step_id].status.value,
                "workflow_status": session.workflow_status.value,
                "payload": payload or {},
            },
        )

    async def _emit_terminal_workflow_event(self, session: WorkflowSession) -> None:
        if self._websocket_manager is None:
            return
        event_map = {
            WorkflowStatus.COMPLETED: "WORKFLOW_COMPLETED",
            WorkflowStatus.CANCELLED: "WORKFLOW_CANCELLED",
            WorkflowStatus.FAILED: "WORKFLOW_FAILED",
        }
        event_type = event_map.get(session.workflow_status)
        if event_type is None:
            return
        await self._websocket_manager.broadcast(
            session.session_id,
            {
                "type": event_type,
                "session_id": session.session_id,
                "step_id": None,
                "status": None,
                "workflow_status": session.workflow_status.value,
                "payload": {},
            },
        )

    def _get_agent_settings(self, step_id: StepId) -> AgentSettings:
        mapping = {
            "atlas": settings.atlas,
            "audit": settings.audit,
            "media_planner": settings.media_planner,
            "geo_fence": settings.geo_fence,
            "meta": settings.meta,
        }
        return mapping[step_id]

    def _build_default_step(self, session_id: str, step_id: StepId) -> WorkflowStep:
        return WorkflowStep(
            session_id=session_id,
            step_id=step_id,
            status=StepStatus.PENDING,
        )

    def _get_mapped_input_preview(self, session: WorkflowSession, step_id: StepId) -> Any | None:
        if not settings.debug_workflow_payloads:
            return None
        if step_id == "media_planner":
            return map_for_media_planner(session)
        if step_id == "geo_fence":
            return map_for_geo_fence(session)
        if step_id == "meta":
            return map_for_meta(session)
        return None
