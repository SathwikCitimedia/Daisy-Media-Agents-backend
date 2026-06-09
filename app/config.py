import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field


load_dotenv()


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_text(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    # dotenv/env files often contain double-escaped regex backslashes.
    return value.replace("\\\\", "\\")


class AgentSettings(BaseModel):
    name: str
    step_id: str
    agent_id: int | None
    transport: str = "run"
    url: str | None = None
    enabled: bool = True
    timeout_seconds: float = 60.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.5

    @property
    def endpoint(self) -> str | None:
        return self.url


class Settings(BaseModel):
    storage_backend: str = Field(default_factory=lambda: os.getenv("STORAGE_BACKEND", "memory"))
    database_url: str | None = Field(default_factory=lambda: os.getenv("DATABASE_URL"))
    agent_user_id: str = Field(default_factory=lambda: os.getenv("AGENT_USER_ID", "user_123"))
    daisynova_api_token: str | None = Field(default_factory=lambda: os.getenv("DAISYNOVA_API_TOKEN"))
    allow_agent_mock_fallback: bool = Field(default_factory=lambda: env_flag("ALLOW_AGENT_MOCK_FALLBACK", False))
    debug_workflow_payloads: bool = Field(default_factory=lambda: env_flag("DEBUG_WORKFLOW_PAYLOADS", False))
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS",
                "http://localhost:3000,http://localhost:5173,http://localhost:5175,http://localhost:4173",
            ).split(",")
            if origin.strip()
        ]
    )
    cors_origin_regex: str = Field(
        default_factory=lambda: env_text(
            "CORS_ORIGIN_REGEX",
            r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^https://.*\.ngrok-free\.app$",
        )
    )
    atlas: AgentSettings = Field(
        default_factory=lambda: AgentSettings(
            name="Atlas Agent",
            step_id="atlas",
            agent_id=39,
            transport="run",
            url="https://aiagents.daisynova.com/api/agents/39/run",
            timeout_seconds=180.0,
        )
    )
    audit: AgentSettings = Field(
        default_factory=lambda: AgentSettings(
            name="Audit Agent",
            step_id="audit",
            agent_id=14,
            transport="run",
            url="https://aiagents.daisynova.com/api/agents/14/run",
            timeout_seconds=180.0,
        )
    )
    media_planner: AgentSettings = Field(
        default_factory=lambda: AgentSettings(
            name="Media Planner Agent",
            step_id="media_planner",
            agent_id=43,
            transport="run",
            url="https://aiagents.daisynova.com/api/agents/43/run",
            timeout_seconds=180.0,
        )
    )
    geo_fence: AgentSettings = Field(
        default_factory=lambda: AgentSettings(
            name="Geo Fence Agent",
            step_id="geo_fence",
            agent_id=74,
            transport="run",
            url="https://aiagents.daisynova.com/api/agents/74/run",
            timeout_seconds=180.0,
        )
    )
    meta: AgentSettings = Field(
        default_factory=lambda: AgentSettings(
            name="Meta Agent",
            step_id="meta",
            agent_id=70,
            transport="run",
            url="https://aiagents.daisynova.com/api/agents/70/run",
            enabled=True,
            timeout_seconds=180.0,
        )
    )


settings = Settings()
