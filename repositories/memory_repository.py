from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.models import StepId, WorkflowSession, WorkflowStep
from repositories.base import BaseSessionRepository, SessionNotFoundError


class InMemorySessionRepository(BaseSessionRepository):
    # Current storage is temporary and in-memory only: sessions live in this
    # process dictionary, disappear on server restart, and should be moved to
    # PostgreSQL before production use.

    def __init__(self) -> None:
        self._sessions: dict[str, WorkflowSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def create_session(self, session: WorkflowSession) -> WorkflowSession:
        async with self._global_lock:
            prepared = self.prepare_new_session(session)
            self._sessions[prepared.session_id] = prepared
            self._locks[prepared.session_id] = asyncio.Lock()
        return prepared.model_copy(deep=True)

    async def get_session(self, session_id: str) -> WorkflowSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        return session.model_copy(deep=True)

    async def list_sessions(self, limit: int = 6) -> list[WorkflowSession]:
        sessions = sorted(
            self._sessions.values(),
            key=lambda session: session.updated_at,
            reverse=True,
        )
        return [session.model_copy(deep=True) for session in sessions[:limit]]

    async def update_session(
        self,
        session_id: str,
        updater: Callable[[WorkflowSession], WorkflowSession | Awaitable[WorkflowSession]],
    ) -> WorkflowSession:
        lock = self._locks.get(session_id)
        if lock is None:
            raise SessionNotFoundError(session_id)

        async with lock:
            current = self._sessions.get(session_id)
            if current is None:
                raise SessionNotFoundError(session_id)

            previous = current.model_copy(deep=True)
            working_copy = current.model_copy(deep=True)
            updated = updater(working_copy)
            if asyncio.iscoroutine(updated):
                updated = await updated
            finalized = self.finalize_updated_session(previous, updated)
            self._sessions[session_id] = finalized
            return finalized.model_copy(deep=True)

    async def get_step(self, session_id: str, step_id: StepId) -> WorkflowStep:
        session = await self.get_session(session_id)
        return session.steps[step_id]
