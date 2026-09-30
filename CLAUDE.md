# Small Business Scheduling Assistant

SMS-first scheduling assistant for a small home-cleaning business. Backend: Python 3.12 FastAPI on AWS SAM (`backend/`, `template.yaml`). Owner app: static Next.js + TypeScript (`frontend/`). Work is governed by [doc/TEAM_AGREEMENT.md](doc/TEAM_AGREEMENT.md); accepted decisions live in `doc/PRD.md`, `doc/HLD.md`, `doc/ARCHITECTURE.md`, and `doc/SCHEDULING_CONTRACTS.md`.

## Hard boundaries

- Synthetic data only. Never use real names, phone numbers, addresses, access codes, or customer messages in code, tests, fixtures, logs, PRs, or chat.
- Never send live SMS, deploy, spend money, or publish without Enrique's separate explicit authorization for that specific action.
- Never read, print, or commit `.env` contents or other secrets.
- Do not invent business policy. An unclear business rule goes to Enrique as a question on the issue.

## Checks

Run from the repository root. GitHub Actions `CI` runs the same checks plus DynamoDB Local race tests; local checks are not CI.

```sh
backend/.venv/bin/python -m pytest backend/tests
backend/.venv/bin/python -m ruff check backend
backend/.venv/bin/python -m mypy backend/scheduling
```

If an owner API route or schema changes, regenerate both contracts and commit them:

```sh
backend/.venv/bin/python -m scheduling.owner_openapi > frontend/openapi/owner.json
(cd frontend && npm run generate:api)
```

Frontend (from `frontend/`): `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`, `npm run check:export`.

## Conventions

- Branch from `origin/main`; name `claude/issue-<n>-<slug>`. One issue per PR, small enough to review.
- Commit subjects are plain sentences ending in `(#<issue>)`. Bodies explain why.
- PR bodies start with `Refs #<n>.` and use the sections: what changes for users, safety boundary (when relevant), docs, validation, known limits.
- A behavior change that alters a documented decision updates the owning document in the same PR.
