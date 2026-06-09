# Frontend API Documentation

This document describes the current frontend contract for the FastAPI multi-agent workflow backend.

It is written for frontend engineers building:

- session creation flows
- workflow status screens
- approval/reject/retry/cancel actions
- recent-session lists
- workflow graph UIs
- polling or WebSocket-based live updates

## Base URL

Local development:

```text
http://127.0.0.1:8000
```

Interactive docs:

```text
GET /docs
GET /openapi.json
```

## High-Level Workflow

The workflow is a gated multi-agent pipeline:

1. User creates a session with a URL.
2. `atlas` and `audit` start in parallel.
3. User reviews both and approves or rejects them.
4. When both `atlas` and `audit` are approved, `media_planner` starts automatically.
5. User reviews `media_planner` and approves or rejects it.
6. When `media_planner` is approved, `geo_fence` and `meta` start automatically in parallel.
7. User reviews `geo_fence` and `meta`.

Important:

- `approve` means user accepts the output and allows the workflow to continue.
- `reject` means regenerate the same step with user feedback.
- `cancel` cancels the whole workflow.
- `retry` is only for failed steps.

## Authentication

The frontend does not send DaisyNova credentials directly.

The backend talks to DaisyNova using:

```text
DAISYNOVA_API_TOKEN
```

If DaisyNova calls fail and `ALLOW_AGENT_MOCK_FALLBACK=true`, the backend may return mock outputs clearly marked with:

```json
{
  "is_mock": true
}
```

Frontend should surface that clearly.

## Important Concepts

### `session_id`

This is the backend workflow session ID.

Use it for all workflow APIs:

- `GET /sessions/{session_id}`
- `POST /sessions/{session_id}/steps/{step_id}/approve`
- `POST /sessions/{session_id}/steps/{step_id}/reject`
- `POST /sessions/{session_id}/steps/{step_id}/retry`
- `POST /sessions/{session_id}/cancel`
- `WS /ws/sessions/{session_id}`

### `agent_session_id`

This is the upstream DaisyNova agent-side session/thread ID returned by the external agent.

Frontend does not need to send it manually.

It is exposed in step state for transparency/debugging.

### Step IDs

Valid step IDs:

- `atlas`
- `audit`
- `media_planner`
- `geo_fence`
- `meta`

### Step Statuses

- `PENDING`
- `RUNNING`
- `WAITING_FOR_APPROVAL`
- `APPROVED`
- `REJECTED`
- `CANCELLED`
- `FAILED`
- `SKIPPED`

### Workflow Statuses

- `RUNNING`
- `WAITING_FOR_APPROVAL`
- `COMPLETED`
- `FAILED`
- `CANCELLED`

### Current Stages

- `INITIAL_ANALYSIS`
- `MEDIA_PLANNING`
- `ACTIVATION`
- `COMPLETED`
- `CANCELLED`
- `FAILED`

## Core Endpoints

### `GET /health`

Simple health check.

Response:

```json
{
  "status": "ok"
}
```

### `GET /agents`

Returns the configured DaisyNova agents.

Response shape:

```json
{
  "agents": [
    {
      "name": "Atlas Agent",
      "step_id": "atlas",
      "agent_id": 39,
      "transport": "run",
      "enabled": true,
      "endpoint": "https://aiagents.daisynova.com/api/agents/39/run"
    }
  ]
}
```

Current expected agent IDs:

- `atlas`: `39`
- `audit`: `14`
- `media_planner`: `43`
- `geo_fence`: `74`
- `meta`: `70`

### `POST /sessions`

Creates a workflow session and immediately starts `atlas` and `audit` in parallel.

Request:

```json
{
  "url": "https://example.com",
  "user_id": "user_123"
}
```

Notes:

- `url` must be a valid `http` or `https` URL.
- `user_id` is optional.
- if omitted, backend defaults to `AGENT_USER_ID`.

Response:

- full `WorkflowStateResponse`

Status code:

- `201 Created`

### `GET /sessions/{session_id}`

Returns the full workflow state for one session.

This is the main polling endpoint for the frontend.

Response:

- full `WorkflowStateResponse`

### `GET /sessions/recent?limit=6`

Returns recent sessions ordered by latest `updated_at`.

Query params:

- `limit`
  - default: `6`
  - minimum: `1`
  - maximum: `20`

Response:

```json
{
  "sessions": [
    {
      "session_id": "abc",
      "url": "https://example.com",
      "workflow_status": "WAITING_FOR_APPROVAL",
      "current_stage": "MEDIA_PLANNING",
      "updated_at": "2026-06-08T13:35:40.216375Z",
      "progress": {
        "total_steps": 5,
        "completed_steps": 2,
        "waiting_for_approval_steps": ["media_planner"],
        "running_steps": [],
        "failed_steps": []
      }
    }
  ]
}
```

### `POST /sessions/{session_id}/steps/{step_id}/approve`

Approves a step and may trigger downstream agents automatically.

Request body:

```json
{}
```

Optional advanced override:

```json
{
  "approved_output": {
    "custom": "override"
  }
}
```

Default behavior:

- if `approved_output` is omitted, backend automatically derives it from the step’s existing `raw_output`

Rules:

- only allowed when step status is `WAITING_FOR_APPROVAL`
- once approved, the step is locked

Auto-trigger behavior:

- approving both `atlas` and `audit` starts `media_planner`
- approving `media_planner` starts `geo_fence` and `meta`

### `POST /sessions/{session_id}/steps/{step_id}/reject`

Rejects the current output and regenerates the same step.

Request:

```json
{
  "reason": "Please make this more detailed."
}
```

Behavior:

- only allowed when step status is `WAITING_FOR_APPROVAL`
- appends the reason to `user_feedback_history`
- increments `revision_count`
- re-runs the same agent
- does not advance the workflow
- returns the step to `WAITING_FOR_APPROVAL` after regeneration completes

### `POST /sessions/{session_id}/steps/{step_id}/retry`

Retries a failed step.

Request body:

- no body required

Behavior:

- only allowed when step status is `FAILED`
- re-runs the same agent using the original `input_task`
- does not automatically trigger downstream steps until the step is later approved

### `POST /sessions/{session_id}/cancel`

Cancels the workflow.

Request:

```json
{
  "reason": "User cancelled workflow"
}
```

Behavior:

- marks workflow as `CANCELLED`
- no further agent execution should continue
- pending/running steps may become `CANCELLED`

## WorkflowStateResponse

Main response model returned by:

- `POST /sessions`
- `GET /sessions/{session_id}`
- step action endpoints
- `cancel`

Shape:

```json
{
  "session": {
    "session_id": "abc",
    "url": "https://example.com",
    "user_id": "user_123",
    "steps": {
      "atlas": {},
      "audit": {},
      "media_planner": {},
      "geo_fence": {},
      "meta": {}
    },
    "workflow_status": "WAITING_FOR_APPROVAL",
    "updated_at": "2026-06-08T13:35:40.216375Z"
  },
  "current_stage": "MEDIA_PLANNING",
  "progress": {},
  "frontend_cards": [],
  "workflow_graph": {}
}
```

## Session Shape

```json
{
  "session_id": "abc",
  "url": "https://example.com",
  "user_id": "user_123",
  "steps": {
    "atlas": {
      "session_id": "abc",
      "step_id": "atlas",
      "status": "WAITING_FOR_APPROVAL",
      "agent_session_id": "upstream-agent-session-id-or-null",
      "input_task": "Analyze this brand URL for strategic brand intelligence: https://example.com/",
      "mapped_input_preview": null,
      "raw_output": {},
      "approved_output": null,
      "user_feedback_history": [],
      "rejection_reason": null,
      "revision_count": 0,
      "error": null,
      "available_actions": ["approve", "reject"],
      "updated_at": "2026-06-08T13:35:40.216375Z"
    }
  },
  "workflow_status": "WAITING_FOR_APPROVAL",
  "updated_at": "2026-06-08T13:35:40.216375Z"
}
```

### Step Fields

#### `status`

Current step status.

#### `agent_session_id`

The DaisyNova agent-side session/thread ID returned by the upstream agent.

This is informational for the frontend.

#### `input_task`

The actual prompt/task string sent to the DaisyNova agent.

This is often verbose and mainly useful for debugging.

#### `mapped_input_preview`

Structured downstream input preview for:

- `media_planner`
- `geo_fence`
- `meta`

Use this for frontend inspection/debug views.

It is cleaner than `input_task`.

#### `raw_output`

The current unapproved output from the agent.

After recent normalization cleanup:

- `content` is the main canonical output
- `text` is only present when it adds something different from `content`
- `raw` keeps upstream metadata like:
  - `exec_id`
  - `usage`
  - `logs`
  - upstream `session_id`

#### `approved_output`

The user-approved output.

This is what downstream mapping uses.

#### `user_feedback_history`

List of rejection reasons/regeneration feedbacks.

#### `revision_count`

How many times the step has been regenerated.

#### `error`

Current step error message, if any.

May still be present even when fallback output exists.

#### `available_actions`

Current UI actions allowed for this step.

Rules:

- when `WAITING_FOR_APPROVAL`:
  - `["approve", "reject"]`
- when `RUNNING`, `PENDING`, `APPROVED`, `FAILED`, `CANCELLED`, `SKIPPED`:
  - `[]`

## Progress Object

```json
{
  "total_steps": 5,
  "completed_steps": 3,
  "waiting_for_approval_steps": ["geo_fence", "meta"],
  "running_steps": [],
  "failed_steps": []
}
```

Use this for:

- progress bars
- stage badges
- “currently running” UI
- “needs review” counts

## Frontend Cards

`frontend_cards` is a UI-focused projection of each step.

Shape:

```json
{
  "step_id": "geo_fence",
  "title": "Geo Fence Agent",
  "status": "RUNNING",
  "summary": "",
  "output": {},
  "mapped_input_preview": {
    "url": "https://example.com",
    "brand_name": "Brand",
    "target_locations": ["Mumbai"]
  },
  "available_actions": []
}
```

Important behavior:

- while a step is `RUNNING`, `output` is often `{}` because no final output exists yet
- `mapped_input_preview` is the best field to show what a downstream step is running with
- `summary` is derived from approved output first, then raw output

## Workflow Graph

Shape:

```json
{
  "nodes": [
    {"id": "atlas", "label": "Atlas", "status": "APPROVED"},
    {"id": "audit", "label": "Audit", "status": "APPROVED"},
    {"id": "media_planner", "label": "Media Planner", "status": "APPROVED"},
    {"id": "geo_fence", "label": "Geo Fence", "status": "WAITING_FOR_APPROVAL"},
    {"id": "meta", "label": "Meta", "status": "WAITING_FOR_APPROVAL"}
  ],
  "edges": [
    {"from": "atlas", "to": "media_planner"},
    {"from": "audit", "to": "media_planner"},
    {"from": "media_planner", "to": "geo_fence"},
    {"from": "media_planner", "to": "meta"}
  ]
}
```

Use this for a DAG or pipeline visualization.

## WebSocket

Endpoint:

```text
WS /ws/sessions/{session_id}
```

Behavior:

- optional enhancement over polling
- backend still works fully without any WebSocket client
- backend broadcasts workflow and step events to connected clients for that session

### Event Shape

```json
{
  "type": "STEP_WAITING_APPROVAL",
  "session_id": "abc",
  "step_id": "atlas",
  "status": "WAITING_FOR_APPROVAL",
  "workflow_status": "WAITING_FOR_APPROVAL",
  "payload": {}
}
```

### Common Event Types

- `STEP_STARTED`
- `STEP_COMPLETED`
- `STEP_WAITING_APPROVAL`
- `STEP_APPROVED`
- `STEP_REJECTED`
- `STEP_RETRIED`
- `STEP_FAILED`
- `STEP_MOCK_FALLBACK`
- `WORKFLOW_COMPLETED`
- `WORKFLOW_CANCELLED`
- `WORKFLOW_FAILED`

### Frontend Recommendation

Use WebSocket for instant updates, but still keep polling as a fallback.

## Downstream Mapping

Frontend usually does not need to build downstream prompts manually, but it helps to know what is happening.

### Media Planner Input

Built internally from approved `atlas` and `audit` outputs.

Shape:

```json
{
  "url": "https://example.com",
  "brand_intelligence": {...},
  "audit_findings": {...}
}
```

### Geo Fence Input

Built internally from approved `media_planner` output.

Shape:

```json
{
  "url": "https://example.com",
  "brand_name": "...",
  "primary_location": "...",
  "country": "...",
  "target_locations": [...],
  "geofence_zones": [
    {
      "zone_name": "...",
      "city": "...",
      "country": "...",
      "latitude": ...,
      "longitude": ...,
      "radius": ...,
      "type": "..."
    }
  ],
  "audience_segments": [...],
  "campaign_objective": "...",
  "budget": ...,
  "duration": "...",
  "recommended_channels": [...]
}
```

### Meta Input

Built internally from approved `media_planner` output.

Shape:

```json
{
  "url": "https://example.com",
  "brand_name": "...",
  "campaign_name": "...",
  "campaign_objective": "...",
  "target_audience": "...",
  "locations": [...],
  "budget": ...,
  "daily_budget": ...,
  "duration": "...",
  "ad_sets": [...],
  "ad_creatives": [...],
  "placements": [...],
  "special_ad_categories": [],
  "country": "..."
}
```

## Error and Edge Cases

### Mock Fallback Output

When `ALLOW_AGENT_MOCK_FALLBACK=true` and a real agent call fails, the step may return:

```json
{
  "content": "Mock fallback output for atlas. Real agent call failed: ...",
  "is_mock": true,
  "original_error": "..."
}
```

Frontend should clearly indicate that this is mock/fallback output.

### Running Steps

When a step is still running:

- `raw_output` may be `null`
- `frontend_cards[].output` may be `{}`
- `summary` may be empty
- `mapped_input_preview` is the best thing to show for downstream steps

### Conflict Errors

Typical `409 Conflict` situations:

- approving a step that is not `WAITING_FOR_APPROVAL`
- rejecting a step that is not `WAITING_FOR_APPROVAL`
- retrying a step that is not `FAILED`
- changing a cancelled workflow

Typical error response:

```json
{
  "detail": "Step 'atlas' is not waiting for approval."
}
```

### Validation Errors

Examples:

- invalid URL
- empty rejection reason
- empty cancel reason
- empty `user_id` when explicitly provided

These return standard FastAPI validation responses.

## Frontend Implementation Recommendations

### For polling

Recommended flow:

1. `POST /sessions`
2. store returned `session_id`
3. poll `GET /sessions/{session_id}`
4. stop or slow polling when:
   - `workflow_status = COMPLETED`
   - `workflow_status = FAILED`
   - `workflow_status = CANCELLED`

### For rendering step actions

Use `available_actions` directly rather than re-implementing state rules in the frontend.

### For cards

Recommended priority:

1. show `status`
2. show `summary`
3. show `mapped_input_preview` for downstream/running steps
4. show `output`

### For debugging views

Use:

- `input_task`
- `mapped_input_preview`
- `agent_session_id`
- `raw_output.raw.logs`
- `raw_output.raw.usage`

### For recent sessions list

Use `GET /sessions/recent?limit=6` for dashboard-style recent history.

## Example End-to-End UI Flow

1. User submits URL using `POST /sessions`.
2. Frontend navigates to a session detail page using returned `session_id`.
3. Frontend polls `GET /sessions/{session_id}` or subscribes to WebSocket.
4. Atlas/Audit cards move from `RUNNING` to `WAITING_FOR_APPROVAL`.
5. User clicks approve or reject.
6. After both Atlas and Audit are approved, Media Planner appears as `RUNNING`.
7. After Media Planner approval, Geo Fence and Meta appear as `RUNNING`.
8. Frontend shows `mapped_input_preview` for Geo/Meta while waiting for results.
9. User reviews final outputs.

## Current DaisyNova Agent Endpoints

- `atlas`: `POST https://aiagents.daisynova.com/api/agents/39/run`
- `audit`: `POST https://aiagents.daisynova.com/api/agents/14/run`
- `media_planner`: `POST https://aiagents.daisynova.com/api/agents/43/run`
- `geo_fence`: `POST https://aiagents.daisynova.com/api/agents/74/run`
- `meta`: `POST https://aiagents.daisynova.com/api/agents/70/run`

All use bearer auth server-side.
