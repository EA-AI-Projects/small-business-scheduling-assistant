# Small Business Scheduling Assistant

An SMS-first scheduling assistant for a small home-cleaning business. Clients request, reschedule, and cancel visits by text; the owner approves new requests by SMS. A shared scheduling system is the source of truth for availability and appointment status.

## Project documents

- [Product Requirements Document (PRD)](doc/PRD.md)
- [High-Level Design (HLD)](doc/HLD.md)
- [Technical Architecture](doc/ARCHITECTURE.md)
- [Scheduling service contracts](doc/SCHEDULING_CONTRACTS.md)
- [Human–agent team agreement](doc/TEAM_AGREEMENT.md)

## Current MVP direction

- Flexible, one-off appointment requests (recurring schedules deferred).
- Owner approval required before new appointments are confirmed.
- Configurable pending-request hold, defaulting to 24 hours.
- Booking horizon of 14 days.
- Monday–Friday, 8:00 a.m.–5:00 p.m. in `America/Los_Angeles`; observed US federal holidays are closed, with owner-editable exceptions.
- Configurable small/medium/large visit defaults of 1/2/3 hours, currently capped at 3 hours, with an owner override per appointment.
- Configurable 15-minute start-time increments and one crew for the pilot.
- Configurable 30-minute travel buffer between visits, with none before the first or after the last.
- SMS-first owner and client workflows, with a minimal owner calendar/admin view.
- Free client cancellations; cancelled time becomes available for future requests.

## Status

The backend exposes a health endpoint and read-only calendar and availability APIs backed by a synthetic local adapter. The owner-approved pilot policy is encoded as a default for local tests; the DynamoDB adapter requires that policy to be persisted before booking. Hold creation and appointment transitions use revision-guarded atomic writes. Provider-independent outbox dispatch and delivery logic includes a scheduled Lambda dispatcher entry point; queue resources and the SMS sender remain under development. Authenticated booking routes and external SMS delivery remain under development. No production scheduling service has been deployed.

## Local backend

Python 3.12+ is required locally. From the repository root:

```sh
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e './backend[dev]'
backend/.venv/bin/python -m uvicorn scheduling.api:app --app-dir backend --reload
```

Then open `http://127.0.0.1:8000/docs` or request `GET /health`. `GET /v1/businesses/pilot/calendar` requires timezone-aware `start_at` and `end_at` query parameters. `GET /v1/businesses/pilot/availability` accepts `day` and `duration_minutes`, and returns UTC starts under the owner-approved pilot policy. The in-memory calendar contains no customer data and resets on restart. These read endpoints are a local harness, not authenticated production routes.

Focused checks:

```sh
backend/.venv/bin/python -m pytest backend/tests
backend/.venv/bin/python -m ruff check backend
backend/.venv/bin/python -m mypy backend/scheduling
```
