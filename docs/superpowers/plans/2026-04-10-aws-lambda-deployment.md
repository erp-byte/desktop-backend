# AWS Lambda Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the FastAPI backend as a Docker container on AWS Lambda with a Function URL, backed by RDS via RDS Proxy.

**Architecture:** Mangum wraps the existing FastAPI ASGI app so Lambda can invoke it. The app is packaged as a Docker container image pushed to ECR, then deployed to Lambda with a Function URL. DB migrations are extracted to a standalone script run before each deploy.

**Tech Stack:** Python 3.12, FastAPI, Mangum, asyncpg, boto3, Docker, AWS Lambda, ECR, RDS Proxy

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `requirements.txt` | Modify | Add `mangum`, `boto3` |
| `app/config.py` | Modify | Add `S3_BUCKET`, `AWS_REGION` settings |
| `app/main.py` | Modify | Remove migrations + keep-alive; add Mangum handler |
| `scripts/migrate.py` | Create | Standalone DB migration runner |
| `Dockerfile` | Create | Lambda container image definition |
| `.dockerignore` | Create | Exclude venv, cache, local files from image |
| `tests/test_config.py` | Create | Verify new config fields load correctly |
| `tests/test_migrate.py` | Create | Verify migration script SQL ordering |
| `tests/test_handler.py` | Create | Smoke-test Mangum handler with mock Lambda event |

---

## Task 1: Add dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add mangum, boto3, and python-dotenv**

Open `requirements.txt` and add three lines after `uvicorn[standard]==0.42.0`:

```
mangum==0.19.0
boto3==1.38.0
python-dotenv==1.1.0
```

- [ ] **Step 2: Install and verify**

```bash
pip install mangum==0.19.0 boto3==1.38.0 python-dotenv==1.1.0
python -c "import mangum; import boto3; from dotenv import load_dotenv; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "deps: add mangum, boto3, python-dotenv for Lambda deployment"
```

---

## Task 2: Extend config with S3 fields

**Files:**
- Modify: `app/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
import os
import pytest
from unittest.mock import patch


def test_config_s3_defaults():
    """S3_BUCKET and AWS_REGION have sensible defaults and load from env."""
    env = {
        "DATABASE_URL": "postgresql://user:pass@localhost/db",
        "ANTHROPIC_API_KEY": "test-key",
    }
    with patch.dict(os.environ, env, clear=True):
        from app.config import Settings
        s = Settings()
        assert s.S3_BUCKET == ""
        assert s.AWS_REGION == "ap-south-1"


def test_config_s3_from_env():
    env = {
        "DATABASE_URL": "postgresql://user:pass@localhost/db",
        "S3_BUCKET": "my-bucket",
        "AWS_REGION": "us-east-1",
    }
    with patch.dict(os.environ, env, clear=True):
        from app.config import Settings
        s = Settings()
        assert s.S3_BUCKET == "my-bucket"
        assert s.AWS_REGION == "us-east-1"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_config.py -v
```

Expected: FAIL — `Settings` has no attribute `S3_BUCKET`

- [ ] **Step 3: Add fields to config**

Replace the contents of `app/config.py`:

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    ANTHROPIC_API_KEY: str = ""
    STORAGE_BACKEND: str = "local"
    STORAGE_LOCAL_BASE_DIR: str = "./so_pdfs"
    S3_BUCKET: str = ""
    AWS_REGION: str = "ap-south-1"
    QUEUE_BACKEND: str = "memory"
    POPPLER_PATH: str | None = None
    SYSTEM_USER_ID: int = 0
    MAX_PDF_SIZE_MB: int = 20
    EXTRACTION_MAX_RETRIES: int = 3
    CLAUDE_MODEL: str = "claude-sonnet-4-20250514"

    model_config = {"env_file": ".env", "extra": "ignore"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_config.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: add S3_BUCKET and AWS_REGION to Settings"
```

---

## Task 3: Create migration script

**Files:**
- Create: `scripts/migrate.py`
- Create: `tests/test_migrate.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_migrate.py`:

```python
import importlib.util
import sys
from pathlib import Path


def test_migrate_script_importable():
    """migrate.py must be importable without side effects at import time."""
    spec = importlib.util.spec_from_file_location(
        "migrate", Path(__file__).parent.parent / "scripts" / "migrate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Should not raise — main() must be guarded by if __name__ == "__main__"
    spec.loader.exec_module(mod)
    assert hasattr(mod, "main"), "migrate.py must define a main() function"


def test_migrate_sql_order():
    """SQL files are applied in the correct dependency order."""
    spec = importlib.util.spec_from_file_location(
        "migrate", Path(__file__).parent.parent / "scripts" / "migrate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ordered = mod.SQL_FILES
    names = [Path(f).name for f in ordered]

    # schema must come before migrate for the same module
    assert names.index("schema.sql") < names.index("migrate.sql")
    assert names.index("po_schema.sql") < names.index("po_migrate.sql")
    assert names.index("production_schema.sql") < names.index("production_migrate.sql")
    assert "seed_test_data.sql" in names
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_migrate.py -v
```

Expected: FAIL — `scripts/migrate.py` does not exist

- [ ] **Step 3: Create the migration script**

Create `scripts/migrate.py`:

```python
"""Standalone DB migration runner.

Run before each Lambda deploy to apply schema changes:

    python scripts/migrate.py

Reads DATABASE_URL from environment or .env file.
All SQL files are idempotent (IF NOT EXISTS / ON CONFLICT).
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DB_DIR = Path(__file__).parent.parent / "app" / "db"

# Applied in dependency order — schemas before their migrations
SQL_FILES = [
    DB_DIR / "schema.sql",
    DB_DIR / "migrate.sql",
    DB_DIR / "po_schema.sql",
    DB_DIR / "po_migrate.sql",
    DB_DIR / "production_schema.sql",
    DB_DIR / "production_migrate.sql",
    DB_DIR / "auth_schema.sql",
    DB_DIR / "ims_new_schema.sql",
    DB_DIR / "sap_mm_align.sql",
    DB_DIR / "001_job_card_chain.sql",
    DB_DIR / "seed_test_data.sql",
]


async def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable not set")
        sys.exit(1)

    logger.info("Connecting to database...")
    conn = await asyncpg.connect(database_url)
    try:
        for sql_file in SQL_FILES:
            if not sql_file.exists():
                logger.warning("Skipping missing file: %s", sql_file.name)
                continue
            logger.info("Applying %s ...", sql_file.name)
            sql = sql_file.read_text(encoding="utf-8")
            await conn.execute(sql)
            logger.info("  OK")
        logger.info("All migrations applied successfully.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
```

> **Note:** This uses `python-dotenv` which is not yet in requirements.txt — add it in the next step.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_migrate.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate.py tests/test_migrate.py
git commit -m "feat: add standalone DB migration script"
```

---

## Task 4: Add Mangum handler to main.py

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_handler.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_handler.py`:

```python
def test_handler_is_callable():
    """Mangum handler must be importable and callable (not a FastAPI app)."""
    # We only test the object exists and is callable — not a full Lambda invocation
    # since that would require a live DB. Integration tested manually post-deploy.
    from app.main import handler
    assert callable(handler), "handler must be a callable (Mangum instance)"


def test_app_is_fastapi():
    from app.main import app
    from fastapi import FastAPI
    assert isinstance(app, FastAPI)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_handler.py::test_handler_is_callable -v
```

Expected: FAIL — `cannot import name 'handler' from 'app.main'`

- [ ] **Step 3: Add Mangum import and handler to main.py**

Add `mangum` to the imports at the top of `app/main.py` (after the existing imports):

```python
from mangum import Mangum
```

Add at the very bottom of `app/main.py` (after the `/health` route):

```python
# AWS Lambda entry point
handler = Mangum(app, lifespan="on")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_handler.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_handler.py
git commit -m "feat: add Mangum handler for Lambda compatibility"
```

---

## Task 5: Remove migrations from lifespan

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Remove migration block from lifespan**

In `app/main.py`, find the lifespan function. Remove lines 33–47 (the block that reads and executes SQL files):

**Remove this entire block:**

```python
    db_dir = Path(__file__).parent / "db"
    async with pool.acquire() as conn:
        await conn.execute((db_dir / "schema.sql").read_text())
        await conn.execute((db_dir / "migrate.sql").read_text(encoding="utf-8"))
        await conn.execute((db_dir / "po_schema.sql").read_text(encoding="utf-8"))
        await conn.execute((db_dir / "po_migrate.sql").read_text(encoding="utf-8"))
        await conn.execute((db_dir / "production_schema.sql").read_text(encoding="utf-8"))
        await conn.execute((db_dir / "production_migrate.sql").read_text(encoding="utf-8"))
        await conn.execute((db_dir / "auth_schema.sql").read_text(encoding="utf-8"))
        await conn.execute((db_dir / "ims_new_schema.sql").read_text(encoding="utf-8"))
        await conn.execute((db_dir / "sap_mm_align.sql").read_text(encoding="utf-8"))
        # Seed test data (idempotent — ON CONFLICT DO NOTHING)
        seed_file = db_dir / "seed_test_data.sql"
        if seed_file.exists():
            await conn.execute(seed_file.read_text(encoding="utf-8"))
    logger.info("Database schema ensured")
```

Also remove the now-unused `Path` import if it appears — check the top of the file. Keep the import only if `data_dir` logic below still uses it (it does — `Path(__file__).parent / "data"`) so **keep the `Path` import**.

The lifespan after removal should read:

```python
@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    settings = Settings()
    fastapi_app.state.settings = settings

    pool = await create_pool(settings)
    fastapi_app.state.db_pool = pool

    master_items = await load_master_items(pool)
    fastapi_app.state.master_items = master_items

    data_dir = Path(__file__).parent / "data"
    if not data_dir.exists():
        data_dir = Path(__file__).parent.parent / "data"
    await run_master_ingest(pool, data_dir, master_items)

    yield

    await close_pool(pool)
    logger.info("Shutdown complete")
```

- [ ] **Step 2: Run existing tests to confirm nothing broke**

```bash
pytest tests/ -v
```

Expected: All previously passing tests still pass

- [ ] **Step 3: Commit**

```bash
git add app/main.py
git commit -m "refactor: remove DB migrations from Lambda lifespan"
```

---

## Task 6: Remove keep-alive poller from lifespan

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Remove keep-alive code**

In `app/main.py`, remove the entire keep-alive block (the `keep_alive_task`, `keep_alive_urls`, `_keep_alive` async function, and the task start/cancel logic). Also remove the `os` import and `asyncio` import if they are no longer used after removal.

After this task, the lifespan from Task 5 stays exactly the same — no additions needed.

Check imports at the top: `asyncio` is not used elsewhere in main.py after removal — remove it. `os` is not used elsewhere — remove it. `httpx` is not used elsewhere — remove it.

The import block at the top of `app/main.py` should become:

```python
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from app.config import Settings
from app.db.connection import create_pool, close_pool
from app.modules.auth.router import router as auth_router
from app.modules.so.router import router as so_router
from app.modules.purchase.router import router as purchase_router
from app.modules.production.router import router as production_router
from app.modules.amendment_router import router as amendment_router
from app.modules.so.services.item_matcher import load_master_items
from app.modules.production.services.master_ingest import run_master_ingest
```

- [ ] **Step 2: Run existing tests to confirm nothing broke**

```bash
pytest tests/ -v
```

Expected: All tests still pass

- [ ] **Step 3: Commit**

```bash
git add app/main.py
git commit -m "refactor: remove keep-alive poller (not needed on Lambda)"
```

---

## Task 7: Create Dockerfile

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

- [ ] **Step 1: Create .dockerignore**

Create `.dockerignore`:

```
.venv/
__pycache__/
*.pyc
*.pyo
.git/
.env
.env.*
tests/
docs/
*.md
.vscode/
.claude/
scripts/
planning_structure.json
so_pdfs/
data/
```

- [ ] **Step 2: Create Dockerfile**

Create `Dockerfile` at the project root:

```dockerfile
FROM public.ecr.aws/lambda/python:3.12

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ${LAMBDA_TASK_ROOT}/app/

# Lambda handler entry point: app/main.py -> handler variable
CMD ["app.main.handler"]
```

- [ ] **Step 3: Build the image locally to verify it works**

```bash
docker build -t candor-backend-test .
```

Expected: Build completes with no errors. Final line should be something like:
```
=> exporting to image
=> => naming to docker.io/library/candor-backend-test
```

- [ ] **Step 4: Verify the handler is importable inside the container**

```bash
docker run --rm candor-backend-test python -c "from app.main import handler; print(type(handler))"
```

Expected: `<class 'mangum.handlers.lambda_handler.LambdaHandler'>` (or similar Mangum class name)

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "feat: add Dockerfile for Lambda container image"
```

---

## Task 8: Push image to ECR and deploy Lambda

> **Prerequisites:** AWS CLI configured (`aws configure`), ECR repository created, RDS Proxy endpoint available.

**Files:** No code changes — this is AWS infra steps.

- [ ] **Step 1: Create ECR repository (one-time)**

```bash
aws ecr create-repository \
  --repository-name candor-consumption-backend \
  --region ap-south-1
```

Note the `repositoryUri` from the output — looks like:
`123456789.dkr.ecr.ap-south-1.amazonaws.com/candor-consumption-backend`

- [ ] **Step 2: Run migrations before first deploy**

```bash
DATABASE_URL="postgresql://user:password@your-rds-host:5432/dbname" python scripts/migrate.py
```

Expected: Each SQL file logged as `OK`. Final line: `All migrations applied successfully.`

- [ ] **Step 3: Authenticate Docker to ECR**

```bash
aws ecr get-login-password --region ap-south-1 \
  | docker login --username AWS --password-stdin \
    123456789.dkr.ecr.ap-south-1.amazonaws.com
```

Expected: `Login Succeeded`

- [ ] **Step 4: Build, tag, and push image**

```bash
# Build for Linux/amd64 (Lambda runs on x86_64)
docker build --platform linux/amd64 -t candor-consumption-backend .

# Tag with ECR URI
docker tag candor-consumption-backend:latest \
  123456789.dkr.ecr.ap-south-1.amazonaws.com/candor-consumption-backend:latest

# Push
docker push 123456789.dkr.ecr.ap-south-1.amazonaws.com/candor-consumption-backend:latest
```

- [ ] **Step 5: Create Lambda function**

```bash
aws lambda create-function \
  --function-name candor-consumption-backend \
  --package-type Image \
  --code ImageUri=123456789.dkr.ecr.ap-south-1.amazonaws.com/candor-consumption-backend:latest \
  --role arn:aws:iam::123456789:role/lambda-execution-role \
  --timeout 900 \
  --memory-size 1024 \
  --region ap-south-1
```

> `lambda-execution-role` must have: `AWSLambdaBasicExecutionRole` + `AmazonRDSDataFullAccess` (or VPC access if RDS Proxy is in a VPC) + `AmazonS3FullAccess`

- [ ] **Step 6: Set environment variables on Lambda**

```bash
aws lambda update-function-configuration \
  --function-name candor-consumption-backend \
  --environment "Variables={
    DATABASE_URL=postgresql://user:password@your-rds-proxy-endpoint:5432/dbname,
    ANTHROPIC_API_KEY=sk-ant-...,
    STORAGE_BACKEND=s3,
    S3_BUCKET=candor-consumption-pdfs,
    AWS_REGION=ap-south-1
  }" \
  --region ap-south-1
```

- [ ] **Step 7: Create Function URL**

```bash
aws lambda create-function-url-config \
  --function-name candor-consumption-backend \
  --auth-type NONE \
  --cors '{
    "AllowOrigins": ["*"],
    "AllowMethods": ["*"],
    "AllowHeaders": ["*"]
  }' \
  --region ap-south-1
```

Note the `FunctionUrl` from the output — this is your new backend URL.

- [ ] **Step 8: Add permission for Function URL to be publicly invoked**

```bash
aws lambda add-permission \
  --function-name candor-consumption-backend \
  --statement-id FunctionURLAllowPublicAccess \
  --action lambda:InvokeFunctionUrl \
  --principal "*" \
  --function-url-auth-type NONE \
  --region ap-south-1
```

- [ ] **Step 9: Smoke test the deployed Lambda**

```bash
curl https://<your-function-url>.lambda-url.ap-south-1.on.aws/health
```

Expected: `{"status":"ok"}`

---

## Task 9: Update frontend BASE_URL

**Files:** Android app config (outside this repo)

- [ ] **Step 1: Replace the Render URL with the Lambda Function URL**

In the Android app, find where `BASE_URL` or the API host is configured and replace:

```
https://desktop-backend-vhf0.onrender.com
```

with:

```
https://<your-function-url>.lambda-url.ap-south-1.on.aws
```

- [ ] **Step 2: Test a real API call from the app**

Run the Android app and perform a login or any authenticated request. Verify it returns a successful response.

---

## Re-deploy Workflow (after this initial setup)

For every subsequent deploy:

```bash
# 1. Run migrations if schema changed
DATABASE_URL="..." python scripts/migrate.py

# 2. Build and push new image
docker build --platform linux/amd64 -t candor-consumption-backend .
docker tag candor-consumption-backend:latest 123456789.dkr.ecr.ap-south-1.amazonaws.com/candor-consumption-backend:latest
docker push 123456789.dkr.ecr.ap-south-1.amazonaws.com/candor-consumption-backend:latest

# 3. Update Lambda to use new image
aws lambda update-function-code \
  --function-name candor-consumption-backend \
  --image-uri 123456789.dkr.ecr.ap-south-1.amazonaws.com/candor-consumption-backend:latest \
  --region ap-south-1
```
