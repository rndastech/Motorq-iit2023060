# Vehicle Enrollment Workflow Manager - Implementation Plan

## Context

Motorq integrates with multiple OEMs to monitor vehicles in real-time. Need a reliable multi-step enrollment system where each OEM can have different sequences of steps (sync and async). Current setup: fresh Django project with SQLite, no existing code.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Workflow Manager API                        │
│  start(vin, brand) → current() → next() → retry() → cancel()   │
└─────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   ┌─────────┐           ┌──────────┐         ┌──────────┐
   │ State DB│           │Workflow DB│         │Sequence DB│
   │(per VIN)│           │(per OEM) │         │ (DAG edges)│
   └─────────┘           └──────────┘         └──────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │ Step Executor   │
                    │  (sync/async)   │
                    └─────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │   Pub/Sub       │
                    │ (async handler) │
                    └─────────────────┘
```

---

## Database Schema

### 1. State DB (VehicleEnrollmentState)
Tracks each vehicle's enrollment progress:
- `vin` - Vehicle identifier (PK)
- `brand` - OEM brand (e.g., "maruti", "toyota")
- `current_step` - Current step name in workflow
- `status` - enum: `pending`, `in_progress`, `awaiting_async`, `completed`, `failed`, `cancelled`
- `tries` - Retry count for current step
- `step_data` - JSON blob for step-specific data (e.g., capabilities, enrollment results)
- `created_at`, `updated_at`

### 2. Workflow DB (WorkflowDefinition)
Defines workflow per OEM:
- `id` (PK)
- `brand` - OEM identifier
- `steps` - JSON array of step definitions, each with:
  - `name` - Step identifier
  - `type` - "sync" or "async"
  - `api_endpoint` - URL/path for the step
  - `capabilities` - Optional list of required capabilities
  - `products` - Optional list of products for enrollment

### 3. Sequence DB (WorkflowSequence)
DAG edge definitions:
- `id` (PK)
- `brand` - Foreign key to WorkflowDefinition
- `from_step` - Source step name
- `to_step` - Target step name
- `condition` - Optional: "always", "on_success", "on_failure"

---

## Core Components

### 1. Models (`enrollment/models.py`)
```python
class WorkflowDefinition(models.Model):
    brand = CharField(unique=True)
    steps = JSONField()  # [{name, type, api_endpoint, capabilities?, products?}]

class WorkflowSequence(models.Model):
    workflow = ForeignKey(WorkflowDefinition)
    from_step = CharField()
    to_step = CharField()
    condition = CharField(default="always")

class VehicleEnrollmentState(models.Model):
    vin = CharField(primary_key=True)
    brand = ForeignKey(WorkflowDefinition)
    current_step = CharField()
    status = CharField(choices=STATUS_CHOICES)
    tries = IntegerField(default=0)
    step_data = JSONField(default=dict)
    created_at, updated_at
```

### 2. Workflow Manager (`enrollment/workflow_manager.py`)
```python
class WorkflowManager:
    def start(vin, brand) -> State
    def current(vin) -> State
    def next(vin) -> State | None  # advances DAG
    def retry(vin) -> State
    def cancel(vin) -> State
```

### 3. Step Executor (`enrollment/step_executor.py`)
- Sync steps: execute immediately, follow DAG
- Async steps: execute API, pause DAG on acknowledgment, wait for pub/sub

### 4. Async Completion Handler (`enrollment/async_handler.py`)
- Database polling approach (simpler than Redis pub/sub)
- Background task checks for async completion status
- On success: call `workflow_manager.next(vin)`
- On failure: update state with error, allow retry

### 5. API Endpoints (`enrollment/views.py` + `urls.py`)
- **Authentication**: API Key in header (`X-API-Key`)
```
POST /api/enroll/start/          - Start enrollment (vin, brand)
GET  /api/enroll/state/<vin>/    - Get current state
POST /api/enroll/retry/<vin>/    - Retry current step
POST /api/enroll/cancel/<vin>/   - Cancel enrollment
GET  /api/enroll/history/<vin>/  - Get enrollment history
```

### 6. Management Commands
```
python manage.py setup_maruti_workflow   # Seed Maruti workflow
python manage.py listen_async           # Start pub/sub listener
```

---

## Example: Maruti Workflow

```
validate_vin (sync) → check_capabilities (sync) → submit_enrollment (async) → register_ready (sync)
```

```python
# steps JSON
[
    {"name": "validate_vin", "type": "sync", "api_endpoint": "/api/oem/maruti/validate-vin"},
    {"name": "check_capabilities", "type": "sync", "api_endpoint": "/api/oem/maruti/capabilities"},
    {"name": "submit_enrollment", "type": "async", "api_endpoint": "/api/oem/maruti/enroll", "products": ["tracking", "diagnostics"]},
    {"name": "register_ready", "type": "sync", "api_endpoint": "/api/oem/maruti/register-ready"}
]

# Sequence edges
(validate_vin, check_capabilities, always)
(check_capabilities, submit_enrollment, always)
(submit_enrollment, register_ready, on_success)
```

---

## Async Flow Example

1. `POST /api/enroll/start/` with `{"vin": "ABC123", "brand": "maruti"}`
2. Manager creates State row, status="in_progress", current_step="validate_vin"
3. `next()` executes sync steps until "submit_enrollment"
4. Async step called, response is acknowledged, DAG paused
5. Pub/sub listener receives enrollment success event for VIN "ABC123"
6. Listener calls `workflow_manager.next("ABC123")`
7. DAG resumes, executes "register_ready"
8. State updated to "completed"

---

## Files to Create

| File | Purpose |
|------|---------|
| `manage.py` | Django management script |
| `config/__init__.py` | Django project package |
| `config/settings.py` | Django settings (SQLite, apps) |
| `config/urls.py` | Root URL configuration |
| `enrollment/__init__.py` | App package |
| `enrollment/models.py` | Database models |
| `enrollment/workflow_manager.py` | Core workflow logic |
| `enrollment/step_executor.py` | API execution (sync/async) |
| `enrollment/async_handler.py` | Polling-based async completion handler |
| `enrollment/views.py` | REST API endpoints |
| `enrollment/urls.py` | App URL routes |
| `enrollment/serializers.py` | DRF serializers |
| `management/commands/setup_workflow.py` | Seed Maruti workflow |
| `management/commands/listen_async.py` | Start pub/sub listener |
| `tests/test_workflow.py` | Unit tests |

---

## Dependencies

- Django 4.2+
- djangorestframework (for API)
- redis (for pub/sub, via redis-py)
- requests (for OEM API calls)

---

## Implementation Order

1. **Django project setup** - config/, manage.py, settings
2. **Models** - All 3 DB tables
3. **Workflow Manager** - Core logic (start, next, retry, cancel)
4. **Step Executor** - API calls with sync/async handling
5. **Pub/Sub Handler** - Redis-based async continuation
6. **API Views** - REST endpoints
7. **Management Commands** - Setup + listener
8. **Tests** - Core functionality coverage

---

## Configuration

- SQLite DB: `db.sqlite3` in project root
- Redis: `redis://localhost:6379/0` (configurable via env)
- OEM API base URL: configurable per brand