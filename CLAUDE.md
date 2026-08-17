# Vehicle Enrollment Workflow Manager

Django-based system for managing multi-step vehicle enrollment workflows across different OEM brands (Maruti, Tata, etc.).

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         REST API (Django + DRF)                         │
│  POST /api/enroll/start/  GET /api/enroll/state/<vin>/                  │
│  POST /api/enroll/complete-async/  GET /api/enroll/pending/             │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         WorkflowManager                                  │
│  start(vin, brand) → auto-completes sync steps → stops at async         │
│  handle_async_complete() → continues workflow after pub/sub message     │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
               State DB      Workflow DB      Sequence DB
               (per VIN)      (per OEM)        (DAG edges)
```

## Database Models

| Model | Purpose |
|-------|---------|
| `WorkflowDefinition` | Defines workflow for an OEM brand |
| `WorkflowStep` | Individual step (sync/async) with API endpoint |
| `WorkflowSequence` | DAG edges (step transitions with conditions) |
| `VehicleEnrollmentState` | Per-VIN enrollment progress |
| `AsyncEnrollmentRequest` | Tracks pending async requests |
| `EnrollmentHistory` | Audit log of all state changes |

## Supported Brands & Workflows

### Maruti
```
validate_vin (sync) → check_capabilities (sync) → submit_enrollment (async) → register_ready (sync)
                                                                               ↑
                                                         waits for error_code=6700
```

### Tata
```
validate_vin (sync) → check_capabilities (sync) → user_verification (async) → submit_enrollment (async) → register_ready (sync)
                                                      ↑                              ↑
                                              status=verified              error_code=6700
```

## Key Files

| File | Purpose |
|------|---------|
| `enrollment/models.py` | Database models |
| `enrollment/workflow_manager.py` | Core workflow logic |
| `enrollment/step_executor.py` | Makes HTTP calls to OEM APIs |
| `enrollment/pubsub.py` | Redis pub/sub listener |
| `enrollment/mock_executor.py` | Mock executor for testing |
| `mock_oem_server.py` | Mock Maruti OEM API server |
| `run_test.py` | Standalone test (no Redis needed) |

## Quick Start

### 1. Install & Setup
```bash
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py setup_workflow          # Maruti
python manage.py setup_tata_workflow     # Tata
```

### 2. Run Tests (No Redis Required)
```bash
python run_test.py
```

### 3. Run with Mock OEM API
```bash
# Terminal 1: Django API
python manage.py runserver 0.0.0.0:8000

# Terminal 2: Mock Maruti API (uses fakeredis)
python mock_oem_server.py

# Terminal 3: Pub/Sub Listener (optional)
python manage.py listen_async
```

### 4. Test API
```bash
# Start enrollment (real OEM API)
curl -X POST http://localhost:8000/api/enroll/start/ ^
  -H "X-API-Key: dev-api-key-change-in-production" ^
  -H "Content-Type: application/json" ^
  -d "{\"vin\": \"TEST123\", \"brand\": \"maruti\"}"

# Start enrollment (with mock executor)
curl -X POST http://localhost:8000/api/enroll/start/ ^
  -H "X-API-Key: dev-api-key-change-in-production" ^
  -H "Content-Type: application/json" ^
  -d "{\"vin\": \"TEST123\", \"brand\": \"maruti\", \"use_mock\": true}"

# Check status
curl http://localhost:8000/api/enroll/state/TEST123/ ^
  -H "X-API-Key: dev-api-key-change-in-production"

# Simulate async completion (instead of pub/sub)
curl -X POST http://localhost:8000/api/enroll/complete-async/ ^
  -H "X-API-Key: dev-api-key-change-in-production" ^
  -H "Content-Type: application/json" ^
  -d "{\"vin\": \"TEST123\", \"success\": true, \"error_code\": \"6700\"}"

# List pending async enrollments
curl http://localhost:8000/api/enroll/pending/ ^
  -H "X-API-Key: dev-api-key-change-in-production"

# Health check
curl http://localhost:8000/api/enroll/health/
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/enroll/health/` | Health check (no auth) |
| POST | `/api/enroll/start/` | Start enrollment |
| GET | `/api/enroll/state/<vin>/` | Get enrollment state |
| POST | `/api/enroll/retry/<vin>/` | Retry failed step |
| POST | `/api/enroll/cancel/<vin>/` | Cancel enrollment |
| GET | `/api/enroll/history/<vin>/` | Get enrollment history |
| GET | `/api/enroll/workflows/` | List all workflows |
| POST | `/api/enroll/complete-async/` | Manually complete async step |
| GET | `/api/enroll/pending/` | List pending enrollments |

## Authentication

API Key authentication via `X-API-Key` header.
Default key: `dev-api-key-change-in-production`

## Configuration

Environment variables in `config/settings.py`:
```bash
DJANGO_SECRET_KEY=...
DJANGO_DEBUG=True
API_KEY=dev-api-key-change-in-production
REDIS_URL=redis://localhost:6379/0
MARUTI_API_URL=http://localhost:8001
TATA_API_URL=http://localhost:8003
USE_FAKE_REDIS=true  # Use fakeredis (no Redis server needed)
```

## Pub/Sub Message Format

```json
{
  "request_id": "12345",
  "product_id": "PROD-001",  // For enrollment
  "vin": "ABC123",
  "status": "success",  // or "verified", "failed"
  "error_code": "6700"  // 6700 = success
}
```

## Adding New OEM

1. Create `setup_<brand>_workflow.py` management command
2. Define steps and sequences
3. Add to `OEM_API_BASE_URL` in settings
4. Run `python manage.py setup_<brand>_workflow`
5. Listener auto-subscribes to `enrollment:<brand>`

## Running Tests

```bash
python manage.py test tests
```

## Production Deployment

```bash
# Install gunicorn
pip install gunicorn

# Run Django
gunicorn config.wsgi:application --bind 0.0.0.0:8000

# Run pub/sub listener (use supervisor/systemd in production)
python manage.py listen_async
```