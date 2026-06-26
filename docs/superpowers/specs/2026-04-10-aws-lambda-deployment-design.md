# AWS Lambda Deployment Design

**Date:** 2026-04-10
**Scope:** FastAPI backend only (`app/main.py`). MCP server stays on Render.

---

## Architecture

```
Android App
     │
     │ HTTPS
     ▼
Lambda Function URL (AUTH_TYPE=NONE)
     │
     │ ASGI via Mangum
     ▼
FastAPI app (Docker container image in ECR)
     │
     ├── asyncpg → RDS Proxy → RDS PostgreSQL
     │
     └── PDF storage → S3 (replaces ./so_pdfs local dir)
```

---

## Components

### Lambda + Function URL
- Runtime: container image (no 250MB zip limit)
- Entry point: `app.main.handler` (Mangum)
- Auth: `NONE` — CORS already handled by FastAPI middleware
- Memory: 1024MB (tunable)
- Timeout: 15 minutes (max) — covers AI planner calls

### ECR
- Repository: `candor-consumption-backend`
- Image tagged by git SHA on each deploy

### RDS Proxy
- Sits between Lambda and RDS PostgreSQL
- Prevents connection exhaustion when multiple Lambda instances spin up simultaneously
- Lambda connects to Proxy endpoint instead of RDS directly; no asyncpg changes needed

### S3
- Bucket: `candor-consumption-pdfs` (or existing bucket)
- Used when `STORAGE_BACKEND=s3`
- Replaces ephemeral `./so_pdfs` local directory

### Migration Script (`scripts/migrate.py`)
- Standalone Python script
- Reads all SQL files from `app/db/` in order and executes against `DATABASE_URL`
- Run manually or in CI before deploying a new Lambda image
- Removes the risk of concurrent migration races on Lambda cold start

---

## Code Changes

### 1. `app/main.py`
- Remove all migration SQL execution from the lifespan handler
- Remove the keep-alive background poller (no persistent background tasks on Lambda)
- Add `handler = Mangum(app, lifespan="on")` at the bottom

### 2. `app/config.py`
- Add `S3_BUCKET: str = ""`
- Add `AWS_REGION: str = "ap-south-1"`
- `STORAGE_BACKEND` already exists; set to `"s3"` in Lambda environment

### 3. `requirements.txt`
- Add `mangum`
- Add `boto3` (for S3 reads/writes)

### 4. `Dockerfile` (new)
- Base image: `public.ecr.aws/lambda/python:3.12`
- Copies app code, installs requirements
- Sets `CMD ["app.main.handler"]`

### 5. `scripts/migrate.py` (new)
- Reads `DATABASE_URL` from env
- Runs all SQL files from `app/db/` in the same order as the current lifespan
- Idempotent (SQL files already use `IF NOT EXISTS`)

---

## Environment Variables (Lambda)

| Variable | Value |
|---|---|
| `DATABASE_URL` | RDS Proxy endpoint connection string |
| `ANTHROPIC_API_KEY` | From Secrets Manager or SSM |
| `STORAGE_BACKEND` | `s3` |
| `S3_BUCKET` | `candor-consumption-pdfs` |
| `AWS_REGION` | `ap-south-1` |
| `STORAGE_LOCAL_BASE_DIR` | Not needed (backend is s3) |

---

## Deployment Flow

1. Run `python scripts/migrate.py` — applies any pending schema changes
2. Build Docker image: `docker build -t candor-consumption-backend .`
3. Tag and push to ECR
4. Update Lambda function to use new image URI
5. Update frontend `BASE_URL` to Lambda Function URL

---

## What Does NOT Change

- All routers, services, schemas
- Auth middleware
- asyncpg usage patterns
- MCP server (stays on Render)
- SQL files

---

## Open Questions / Post-Launch

- **Custom domain:** Lambda Function URL can be fronted by CloudFront for a stable custom domain (e.g. `api.candorfoods.com`)
- **Cold start latency:** Container images have ~1-3s cold starts. If this is unacceptable, provisioned concurrency can keep one instance warm (replaces the old Render keep-alive poller)
- **S3 PDF upload:** Services that currently write to `./so_pdfs` will need their file I/O updated to use boto3. This is a follow-on task scoped separately.
