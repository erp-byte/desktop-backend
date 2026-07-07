# Deploying the backend to AWS App Runner

The `Dockerfile` in this folder builds a container that serves the FastAPI app
with uvicorn (mirrors `../../backend`). App Runner runs Docker images from ECR,
so the flow is: **build → push to ECR → create App Runner service from that
image**. (App Runner's *source-code* mode uses managed runtimes + `apprunner.yaml`
and does **not** use a Dockerfile — so we use the image path.)

> `Dockerfile.lambda` is the previous AWS Lambda packaging (Mangum handler),
> preserved unchanged. The default `Dockerfile` is now the App Runner one.

## 1. Build & push to ECR

```bash
AWS_REGION=ap-south-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REPO=candor-backend
ECR=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

aws ecr create-repository --repository-name $REPO --region $AWS_REGION 2>/dev/null || true
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR

# Build for App Runner (linux/amd64 — important if you build on Apple Silicon)
docker build --platform linux/amd64 -t $REPO .
docker tag  $REPO:latest $ECR/$REPO:latest
docker push $ECR/$REPO:latest
```

## 2. Create the App Runner service

Console → App Runner → Create service:
- **Source:** Container registry → Amazon ECR → the image above. Enable
  *automatic deployments* if you want redeploy-on-push.
- **Port:** `8000` (matches the Dockerfile's `EXPOSE`/CMD default).
- **Health check:** HTTP, path `/health` (returns `{"status":"ok"}`). Give a
  generous unhealthy threshold / interval — the app's startup lifespan connects
  to the DB and runs master ingest before it starts serving.
- **CPU/Memory:** 1 vCPU / 2 GB is a safe starting point.
- **Instance role:** attach an IAM role with S3 + Secrets Manager access **only
  if** you use those features (vendor uploads / secrets).
- **Environment variables:** see below.

Or via CLI: `aws apprunner create-service ...` with `ImageRepository`,
`Port=8000`, `HealthCheckConfiguration{Protocol=HTTP,Path=/health}`, and the
env vars as `RuntimeEnvironmentVariables`.

## 3. Required environment variables

These come from `app/config.py` (Settings). Set them in the App Runner service
config — **never bake secrets into the image** (`.env` is git/Docker-ignored):

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | **Yes** | Postgres URL. Missing → Settings validation fails → container won't boot. |
| `APP_ENV` | Recommended | `prod` / `staging` / `dev`. Non-dev **requires** `JWT_SECRET`. |
| `JWT_SECRET` | Yes (non-dev) | `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `AUTH_ENCRYPTION_KEY` | Yes | Fernet key (auth). |
| `PACKING_TOKEN_KEY` | Yes (for packing QR) | AES-256-GCM key: `python -c "import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
| `ANTHROPIC_API_KEY` | If AI used | |
| `AWS_*`, `VENDOR_S3_BUCKET` | If S3 used | Prefer an instance role over static keys. |
| `WHATSAPP_*`, `SMTP_*` | If notifications used | |
| `INTERNAL_WEBHOOK_TOKEN`, `WS_TOKEN_SECRET` | If used | |

## 4. After deploy — the HTTPS URL

App Runner gives the service an HTTPS URL like
`https://<id>.<region>.awsapprunner.com`. That URL is exactly what the packing
QR / Wix integration needs:

- Set the Wix Velo `API_BASE` (see `../../web_replica/docs/wix-packing-scan-page.md`)
  to this App Runner URL — the public `/api/v1/packing-details/public/scan`
  endpoint is then reachable over HTTPS from the Wix page.
- Optionally point the frontend's `next.config.ts` API proxy target and
  `PUBLIC_BACKEND_URL` at it too.

## 5. Database migrations

The container only *serves* the app; it does not run migrations. Apply schema
changes (incl. `069_packing_details.sql`) separately against the DB:

```bash
# from a machine with DATABASE_URL set
python scripts/migrate.py
```

(Idempotent — safe to re-run. Run once; all envs share the same DB.)

## Local sanity check

```bash
docker compose up --build     # serves on http://localhost:8000
curl localhost:8000/health    # -> {"status":"ok"}
```
