from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from app.models import StepId, WorkflowSession, WorkflowStep
from repositories.base import BaseSessionRepository, SessionNotFoundError


class Base(DeclarativeBase):
    pass


class WorkflowSessionORM(Base):
    __tablename__ = "workflow_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    workflow_status: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    steps: Mapped[list["WorkflowStepORM"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class WorkflowStepORM(Base):
    __tablename__ = "workflow_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    mapped_input_preview: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    raw_output: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    approved_output: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    user_feedback_history: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[WorkflowSessionORM] = relationship(back_populates="steps")


class PostgresSessionRepository(BaseSessionRepository):
    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, future=True)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create_session(self, session: WorkflowSession) -> WorkflowSession:
        prepared = self.prepare_new_session(session)
        async with self._global_lock:
            self._locks.setdefault(prepared.session_id, asyncio.Lock())
        async with self._session_factory() as db:
            db.add(self._to_session_orm(prepared))
            await db.commit()
        return prepared.model_copy(deep=True)

    async def get_session(self, session_id: str) -> WorkflowSession:
        async with self._session_factory() as db:
            session_orm = await self._get_session_orm(db, session_id)
            if session_orm is None:
                raise SessionNotFoundError(session_id)
            return self._from_session_orm(session_orm)

    async def list_sessions(self, limit: int = 6) -> list[WorkflowSession]:
        async with self._session_factory() as db:
            stmt = (
                select(WorkflowSessionORM)
                .options(selectinload(WorkflowSessionORM.steps))
                .order_by(WorkflowSessionORM.updated_at.desc())
                .limit(limit)
            )
            result = await db.execute(stmt)
            sessions = result.scalars().all()
            return [self._from_session_orm(session_orm) for session_orm in sessions]

    async def update_session(
        self,
        session_id: str,
        updater: Callable[[WorkflowSession], WorkflowSession | Awaitable[WorkflowSession]],
    ) -> WorkflowSession:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            async with self._session_factory() as db:
                session_orm = await self._get_session_orm(db, session_id, with_for_update=True)
                if session_orm is None:
                    raise SessionNotFoundError(session_id)

                current = self._from_session_orm(session_orm)
                previous = current.model_copy(deep=True)
                working_copy = current.model_copy(deep=True)
                updated = updater(working_copy)
                if asyncio.iscoroutine(updated):
                    updated = await updated
                finalized = self.finalize_updated_session(previous, updated)
                self._apply_to_orm(session_orm, finalized)
                await db.commit()
                return finalized.model_copy(deep=True)

    async def get_step(self, session_id: str, step_id: StepId) -> WorkflowStep:
        session = await self.get_session(session_id)
        return session.steps[step_id]

    async def _get_session_orm(
        self,
        db: AsyncSession,
        session_id: str,
        with_for_update: bool = False,
    ) -> WorkflowSessionORM | None:
        stmt = (
            select(WorkflowSessionORM)
            .options(selectinload(WorkflowSessionORM.steps))
            .where(WorkflowSessionORM.session_id == session_id)
        )
        if with_for_update:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    def _to_session_orm(self, session: WorkflowSession) -> WorkflowSessionORM:
        return WorkflowSessionORM(
            session_id=session.session_id,
            url=str(session.url),
            user_id=session.user_id,
            workflow_status=session.workflow_status.value,
            updated_at=session.updated_at,
            steps=[self._to_step_orm(step) for step in session.steps.values()],
        )

    def _to_step_orm(self, step: WorkflowStep) -> WorkflowStepORM:
        return WorkflowStepORM(
            session_id=step.session_id,
            step_id=step.step_id,
            status=step.status.value,
            agent_session_id=step.agent_session_id,
            input_task=step.input_task,
            mapped_input_preview=step.mapped_input_preview,
            raw_output=step.raw_output,
            approved_output=step.approved_output,
            user_feedback_history=step.user_feedback_history,
            rejection_reason=step.rejection_reason,
            revision_count=step.revision_count,
            error=step.error,
            updated_at=step.updated_at,
        )

    def _from_session_orm(self, session_orm: WorkflowSessionORM) -> WorkflowSession:
        steps = {
            step_orm.step_id: WorkflowStep(
                session_id=step_orm.session_id,
                step_id=step_orm.step_id,  # type: ignore[arg-type]
                status=step_orm.status,  # type: ignore[arg-type]
                agent_session_id=step_orm.agent_session_id,
                input_task=step_orm.input_task,
                mapped_input_preview=step_orm.mapped_input_preview,
                raw_output=step_orm.raw_output,
                approved_output=step_orm.approved_output,
                user_feedback_history=step_orm.user_feedback_history or [],
                rejection_reason=step_orm.rejection_reason,
                revision_count=step_orm.revision_count,
                error=step_orm.error,
                updated_at=step_orm.updated_at,
            )
            for step_orm in session_orm.steps
        }
        return WorkflowSession(
            session_id=session_orm.session_id,
            url=session_orm.url,
            user_id=session_orm.user_id,
            steps=steps,  # type: ignore[arg-type]
            workflow_status=session_orm.workflow_status,  # type: ignore[arg-type]
            updated_at=session_orm.updated_at,
        )

    def _apply_to_orm(self, session_orm: WorkflowSessionORM, session: WorkflowSession) -> None:
        session_orm.url = str(session.url)
        session_orm.user_id = session.user_id
        session_orm.workflow_status = session.workflow_status.value
        session_orm.updated_at = session.updated_at

        existing_steps = {step.step_id: step for step in session_orm.steps}
        for step_id, step in session.steps.items():
            orm_step = existing_steps.get(step_id)
            if orm_step is None:
                session_orm.steps.append(self._to_step_orm(step))
                continue
            orm_step.status = step.status.value
            orm_step.agent_session_id = step.agent_session_id
            orm_step.input_task = step.input_task
            orm_step.mapped_input_preview = step.mapped_input_preview
            orm_step.raw_output = step.raw_output
            orm_step.approved_output = step.approved_output
            orm_step.user_feedback_history = step.user_feedback_history
            orm_step.rejection_reason = step.rejection_reason
            orm_step.revision_count = step.revision_count
            orm_step.error = step.error
            orm_step.updated_at = step.updated_at
