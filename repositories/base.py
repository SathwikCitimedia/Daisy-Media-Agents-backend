from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from app.models import StepId, WorkflowSession, WorkflowStep, derive_workflow_status, utc_now


logger = logging.getLogger("app.workflow")


class SessionNotFoundError(KeyError):
    """Raised when a session does not exist."""


class BaseSessionRepository(ABC):
    @abstractmethod
    async def create_session(self, session: WorkflowSession) -> WorkflowSession:
        raise NotImplementedError

    @abstractmethod
    async def get_session(self, session_id: str) -> WorkflowSession:
        raise NotImplementedError

    @abstractmethod
    async def list_sessions(self, limit: int = 6) -> list[WorkflowSession]:
        raise NotImplementedError

    @abstractmethod
    async def update_session(
        self,
        session_id: str,
        updater: Callable[[WorkflowSession], WorkflowSession | Awaitable[WorkflowSession]],
    ) -> WorkflowSession:
        raise NotImplementedError

    @abstractmethod
    async def get_step(self, session_id: str, step_id: StepId) -> WorkflowStep:
        raise NotImplementedError

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def prepare_new_session(self, session: WorkflowSession) -> WorkflowSession:
        session.workflow_status = derive_workflow_status(session.steps)
        session.updated_at = utc_now()
        self._log_session_created(session)
        return session

    def finalize_updated_session(
        self,
        previous: WorkflowSession,
        updated: WorkflowSession,
    ) -> WorkflowSession:
        now = utc_now()
        for step_id, step in updated.steps.items():
            previous_step = previous.steps[step_id]
            if step.model_dump() != previous_step.model_dump():
                step.updated_at = now
        updated.workflow_status = derive_workflow_status(updated.steps)
        if updated.model_dump() != previous.model_dump():
            updated.updated_at = now
        else:
            updated.updated_at = previous.updated_at
        self._log_transitions(previous, updated)
        return updated

    def _log_session_created(self, session: WorkflowSession) -> None:
        logger.info(
            json.dumps(
                {
                    "event": "session_created",
                    "session_id": session.session_id,
                    "workflow_status": session.workflow_status.value,
                    "updated_at": session.updated_at.isoformat(),
                }
            )
        )

    def _log_transitions(self, previous: WorkflowSession, updated: WorkflowSession) -> None:
        if previous.workflow_status != updated.workflow_status:
            logger.info(
                json.dumps(
                    {
                        "event": "workflow_status_transition",
                        "session_id": updated.session_id,
                        "from_status": previous.workflow_status.value,
                        "to_status": updated.workflow_status.value,
                        "updated_at": updated.updated_at.isoformat(),
                    }
                )
            )

        for step_id, updated_step in updated.steps.items():
            previous_step = previous.steps[step_id]
            if previous_step.status != updated_step.status:
                logger.info(
                    json.dumps(
                        {
                            "event": "step_status_transition",
                            "session_id": updated.session_id,
                            "step_id": step_id,
                            "from_status": previous_step.status.value,
                            "to_status": updated_step.status.value,
                            "updated_at": updated_step.updated_at.isoformat(),
                            "error": updated_step.error,
                        }
                    )
                )
