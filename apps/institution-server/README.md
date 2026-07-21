# PredixaLearn Institution Server

This optional FastAPI service adds institution-controlled archive search,
teacher collaboration, curriculum and rubric versioning, answer-script review,
accessibility descriptions, verified datasets, OCR workers, and LTI 1.3
integration. It does not replace the standalone Windows OCR studio and it does
not automatically upload local History.

## Development

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\packages\institution-protocol -e ".\apps\institution-server[test]"
$env:PREDIXALEARN_INSTITUTION_ENV = "test"
$env:PREDIXALEARN_DATABASE_URL = "sqlite:///./apps/institution-server/dev.sqlite3"
$env:PREDIXALEARN_AUTH_MODE = "test"
$env:PREDIXALEARN_TEST_JWT_SECRET = "replace-for-local-development"
.\.venv\Scripts\uvicorn.exe institution_server.main:app --app-dir .\apps\institution-server --port 8100
```

SQLite and test authentication are development-only. Production validation
requires PostgreSQL, Redis, OIDC, HTTPS, Ed25519 worker signing, webhook
signing, separate learner-identity encryption, and S3-compatible or Azure Blob
storage.

Production Python dependencies, including both object-storage adapters, are
pinned with SHA-256 hashes in `requirements.lock`. Regenerate it only after
tests and dependency review:

```powershell
pip-compile pyproject.toml --extra s3 --extra azure --generate-hashes --output-file requirements.lock
```

## Production configuration

Use `deploy/institution.env.example` as the non-secret configuration template.
Supply these values through a protected secret store:

- `PREDIXALEARN_DATABASE_URL`
- `PREDIXALEARN_REDIS_URL`
- `PREDIXALEARN_OIDC_CLIENT_SECRET`
- `PREDIXALEARN_SESSION_SECRET`
- `PREDIXALEARN_WORKER_SIGNING_KEY`
- `PREDIXALEARN_WEBHOOK_SIGNING_SECRET`
- `PREDIXALEARN_API_TOKEN_SECRET`
- `PREDIXALEARN_IDENTITY_ENCRYPTION_KEY`
- object-storage credentials
- `PREDIXALEARN_LTI_PRIVATE_KEY` and `PREDIXALEARN_LTI_KEY_ID` when LTI services are enabled

`PREDIXALEARN_FEATURES` controls reversible rollout rings. Available flags are
`archive_review`, `curriculum`, `answer_scripts`, `accessibility`, `datasets`,
`workers`, `integrations`, and `lti`.

## Deployment

Docker Compose provides PostgreSQL with pgvector, Redis, MinIO, migrations,
and the API. Copy the example environment file, create the three secret files
under `deploy/secrets/`, then run:

```powershell
docker compose -f apps/institution-server/deploy/docker-compose.yml up --build
```

The Helm chart at `deploy/helm/predixalearn-institution` uses a pre-install and
pre-upgrade Alembic migration Job, rolling updates, a disruption budget,
read-only containers, dropped Linux capabilities, health probes, and a
NetworkPolicy. Database, Redis, storage, TLS, OIDC, backup, and encryption-key
operations remain institution responsibilities.

## Safety invariants

- PostgreSQL row-level security and application guards enforce tenant scope.
- Evidence versions, dataset releases, and published curriculum/rubric versions
  are immutable.
- Private resources are visible only to their creator.
- Learner identifiers are AES-GCM encrypted in a separate table; searchable
  resources contain pseudonyms only.
- Worker jobs are Ed25519 signed. Local synchronization also requires a fresh
  Ed25519 device signature plus its device bearer credential.
- Worker enrollment pins the server signing public key on the device. Every
  worker API request also carries a fresh Ed25519 device signature over its
  timestamp, method, and exact route; the bearer credential alone is
  insufficient. Sources
  are staged with `POST /api/v2/worker-jobs/sources`; workers can only stream a
  job after claiming it and can only upload artifact kinds approved by that
  job. Source and output hashes, sizes, tenant prefixes, ownership, and job
  state are checked before acceptance. The staged source is removed when the
  job reaches a terminal state.
- Original source uploads are excluded from synchronization by default.
- AI, semantic indexing, grade return, and dataset release require explicit
  policy or human approval.
- Operational logs, SSE messages, metrics, and webhooks contain identifiers and
  state only, never OCR document text.
- Institution-system integrations use tenant-scoped OAuth2 client credentials.
  Plaintext client secrets are returned once and only scrypt hashes are stored.
  Issued access tokens last 10 minutes and contain only approved permissions,
  rollout flags, and course/academic-unit scopes.
- Workers can be drained without interrupting a claimed job. Draining workers
  receive no new work; irreversible revocation invalidates the credential and
  requeues a claimed job without retaining its lease metadata.
- Saved archive filters are private, tenant-scoped, and validated against the
  full search schema. Review assignments, deadlines, and moderation flags are
  first-class fields, and reassignment requires the current ETag.

After enrolling an OCR Studio device, start its outbound-only worker from the
repository root with:

```powershell
Set-Location apps/ocr-studio
../../.venv/Scripts/python.exe run.py --mode institution-worker --poll-interval 5
```

Use `--once` for an operational smoke test. No worker is started implicitly by
enrollment, and processing uses a temporary isolated History/output tree.

For an institution-system connector, an Integration Admin provisions a client
with `POST /api/v2/api-clients`, stores the one-time secret in the institution's
secret manager, and exchanges it at `POST /api/v2/oauth/token` using the OAuth2
`client_credentials` grant. Rotate with
`POST /api/v2/api-clients/{client_id}/rotate` and the current `If-Match` ETag;
revoke with `DELETE /api/v2/api-clients/{client_id}`. Never embed these secrets
in the Windows OCR client or LMS browser content.

## Verification

The local institutional gate is:

```powershell
../../.venv/Scripts/python.exe -m pytest tests `
  --cov=institution_server --cov-branch --cov-fail-under=85
```

The current Windows workspace result is 43 passing tests at 86% branch-aware
coverage. Ruff, browser type checking, two Playwright/axe/mobile portal checks,
the hashed Python dependency audit, npm audit, Alembic migration round-trip,
and Docker Compose configuration validation also pass. Docker image execution,
Helm lint, real OIDC/LMS conformance, storage-provider integration tests, load
tests, backup restoration, and disaster-recovery drills remain deployment gates
that require institution infrastructure.

Alembic migration `0002_api_clients_worker_lifecycle` adds the RLS-protected
client registry and versioned worker state. Its SQLite upgrade -> downgrade ->
upgrade smoke passes locally; CI repeats migration checks against PostgreSQL
with pgvector.

See `../ocr-studio/README.md` for the complete product architecture, local
enrollment flow, API inventory, privacy rules, tests, limitations, and rollback
guidance.
