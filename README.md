# Multi-Agent Workflow Orchestration Backend

FastAPI backend for orchestrating a staged multi-agent workflow with session-based state, approval gates, and agent re-runs when outputs are rejected.

## Features

- Session-based workflow state for every request
- Parallel execution of Atlas and Audit
- Approval-driven execution of Media Planner, then Geo Fence and Meta
- In-memory repository abstraction that can later be replaced with PostgreSQL
- Async external agent calls using `httpx`
- DaisyNova agents use Bearer-authenticated `/run` requests
- Exponential backoff retries and per-agent timeouts for external calls
- Structured logging for workflow and step transitions
- Pydantic request and response models
- CORS enabled for frontend integration

## Project Structure

```text
app/
  __init__.py
  main.py
  models.py
  storage.py
  agent_client.py
  workflow_engine.py
  config.py
repositories/
  base.py
  memory_repository.py
  postgres_repository.py
```

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

## Docker

Build the image:

```bash
docker build -t daisy-media-agents-backend .
```

Run the container:

```bash
docker run --env-file .env -p 8000:8000 daisy-media-agents-backend
```

For production, prefer `STORAGE_BACKEND=postgres` and provide a valid `DATABASE_URL`.

## Local Commands

```bash
pip install -r requirements.txt
python -m pytest
uvicorn app.main:app --reload
```

## Storage Backend Configuration

Set `STORAGE_BACKEND=memory` to use the in-memory repository or `STORAGE_BACKEND=postgres` to use PostgreSQL.

When using PostgreSQL, set `DATABASE_URL` to your SQLAlchemy async connection string, for example:

```bash
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/workflows
```

Set `DAISYNOVA_API_TOKEN` to the DaisyNova bearer token used for all agent `/run` requests.

Set `ALLOW_AGENT_MOCK_FALLBACK=true` only for development if you want the workflow to continue with clearly marked mock outputs when real agent calls fail.

## Workflow Summary

1. `POST /sessions` creates a new `session_id`, stores the URL, and starts `atlas` and `audit` in parallel.
2. `GET /sessions/{session_id}` returns the full workflow state for polling.
3. `GET /agents` returns current configured agents, transport type, and whether each is enabled.
4. `POST /sessions/{session_id}/steps/{step_id}/approve` stores an approved result and triggers downstream agents when dependencies are satisfied.
5. `POST /sessions/{session_id}/steps/{step_id}/reject` regenerates the selected agent output without advancing the workflow.
6. `POST /sessions/{session_id}/steps/{step_id}/retry` retries a failed step with its original task.
7. `POST /sessions/{session_id}/cancel` cancels the workflow.

## Agent Inputs

- `atlas`
  - Receives the original session URL.
  - Task pattern: `Analyze this brand URL for strategic brand intelligence: {url}`
- `audit`
  - Receives the original session URL.
  - Task pattern: `Perform a detailed brand audit for this URL: {url}`
- `media_planner`
  - Starts only after both `atlas` and `audit` are approved.
  - Receives a mapped payload built from approved `atlas` and `audit` outputs:
  - `atlas` approved output is mapped into `brand_intelligence`, which may include:
    `brand_name`, `industry`, `primary_geo`, `business_model`, `report_type`, `tier`, `overall_score`, `grade`, `summary`, `headline`, `top_priorities`, `strategic_gaps`, `paid_media_status`, `geo_insight`, `geofit_score`, `competitive_summary`, `competitors`, `measurement_maturity`, `recommended_actions`, `locations`
  - `audit` approved output is mapped into `audit_findings`, which may include:
    `audit_id`, `summary`, `findings`, `strengths`, `weaknesses`, `recommendations`, `priority_actions`, `seo`, `local_seo`, `conversion`, `performance`, `technical_issues`, `compliance`, `competitors_mentioned`, `cities_mentioned`
```json
{
  "url": "https://example.com",
  "brand_intelligence": {
    "brand_name": "...",
    "industry": "...",
    "summary": "...",
    "top_priorities": [],
    "strategic_gaps": [],
    "competitors": [],
    "recommended_actions": [],
    "locations": []
  },
  "audit_findings": {
    "summary": "...",
    "findings": [],
    "recommendations": [],
    "technical_issues": [],
    "cities_mentioned": []
  }
}
```
- `geo_fence`
  - Starts only after `media_planner` is approved.
  - Receives a compact mapped payload built from the approved `media_planner` output:
```json
{
  "url": "https://example.com",
  "brand_name": "...",
  "primary_location": "...",
  "country": "...",
  "target_locations": [],
  "geofence_zones": [],
  "audience_segments": [],
  "campaign_objective": "...",
  "budget": 0,
  "duration": "...",
  "recommended_channels": []
}
```
- `meta`
  - Starts only after `media_planner` is approved.
  - Receives a compact mapped payload built from the approved `media_planner` output:
```json
{
  "url": "https://example.com",
  "brand_name": "...",
  "campaign_name": "...",
  "campaign_objective": "...",
  "target_audience": "...",
  "locations": [],
  "budget": 0,
  "daily_budget": 0,
  "duration": "...",
  "ad_sets": [],
  "ad_creatives": [],
  "placements": [],
  "special_ad_categories": [],
  "country": "..."
}
```

The exact mapped inputs are also exposed in the session response as `mapped_input_preview` for `media_planner`, `geo_fence`, and `meta` when workflow payload debugging is enabled.

## Step Statuses

- `PENDING`
- `RUNNING`
- `WAITING_FOR_APPROVAL`
- `APPROVED`
- `FAILED`
- `SKIPPED`

## Notes

- All DaisyNova agents use `POST /api/agents/{agent_id}/run` with:
  - `Content-Type: application/json`
  - `Authorization: Bearer <DAISYNOVA_API_TOKEN>`
- Agent payloads use:
  - `task`
  - optional `session_id`
  - `wait: true`
- If `ALLOW_AGENT_MOCK_FALLBACK=true`, failed agent calls are converted into clearly marked mock fallback outputs instead of failing the workflow. This is for development only.
- `meta` is enabled with agent ID `70` and runs after `media_planner` approval alongside `geo_fence`.
- Downstream prompts are built from approved outputs only.
- Because execution is async and started in the background, clients should poll `GET /sessions/{session_id}` for updated step results.
- Sessions and steps expose `updated_at` timestamps, and sessions also expose a workflow-level status of `RUNNING`, `WAITING_FOR_APPROVAL`, `COMPLETED`, or `FAILED`.

## Current Storage Behavior

- Sessions are stored in memory in `repositories/memory_repository.py` when `STORAGE_BACKEND=memory`.
- Data is lost whenever the server restarts.
- This setup is only intended for development.
- PostgreSQL should be used for production deployments.
- `session_id` is the key used to fetch the full workflow state.

## Frontend Integration Contract

`POST /sessions`

- Request:
```json
{
  "url": "https://example.com",
  "user_id": "user_123"
}
```
- `url` must be a valid `http` or `https` URL.
- `user_id` is optional and defaults to `AGENT_USER_ID`.

`GET /sessions/{session_id}`

- Returns the full session state, plus derived frontend fields:
  - `current_stage`
  - `progress`
  - `frontend_cards`
  - `workflow_graph`

`POST /sessions/{session_id}/steps/{step_id}/approve`

- Request:
```json
{}
```
- By default, the backend copies `approved_output` from the step's existing `raw_output` using its content extractor.
- Frontend clients should usually send an empty body.
- Only send `approved_output` manually if you intentionally want to override the extracted value.

`POST /sessions/{session_id}/steps/{step_id}/reject`

- Request:
```json
{
  "reason": "User did not approve. Regenerate with better details."
}
```
- Empty rejection reason is rejected.
- Regenerates the same agent and returns the step to `WAITING_FOR_APPROVAL`.

`POST /sessions/{session_id}/steps/{step_id}/retry`

- Retries a failed step using its original `input_task`.
- Only allowed when the step status is `FAILED`.
- Does not trigger downstream agents until the step is approved.

`POST /sessions/{session_id}/cancel`

- Request:
```json
{
  "reason": "User cancelled workflow"
}
```

WebSocket: `GET/WS /ws/sessions/{session_id}`

- Event shape:
```json
{
  "type": "STEP_WAITING_APPROVAL",
  "session_id": "session_123",
  "step_id": "atlas",
  "status": "WAITING_FOR_APPROVAL",
  "workflow_status": "WAITING_FOR_APPROVAL",
  "payload": {}
}
```

## DaisyNova Setup

- Add `DAISYNOVA_API_TOKEN` to your environment before starting the API.
- The backend sends the token in `Authorization: Bearer <DAISYNOVA_API_TOKEN>` for every DaisyNova agent request.
- If the token is missing and `ALLOW_AGENT_MOCK_FALLBACK=false`, startup fails with a clear configuration error.
- If `ALLOW_AGENT_MOCK_FALLBACK=true`, startup continues, but failed DaisyNova calls fall back to clearly marked mock outputs for development only.

## Agent Transports

- `atlas`: `run` transport, `POST https://aiagents.daisynova.com/api/agents/39/run`
- `audit`: `run` transport, `POST https://aiagents.daisynova.com/api/agents/14/run`
- `media_planner`: `run` transport, `POST https://aiagents.daisynova.com/api/agents/43/run`
- `geo_fence`: `run` transport, `POST https://aiagents.daisynova.com/api/agents/74/run`
- `meta`: `run` transport, `POST https://aiagents.daisynova.com/api/agents/70/run`
