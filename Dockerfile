FROM public.ecr.aws/lambda/python:3.12

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ${LAMBDA_TASK_ROOT}/app/

# Lambda handler entry point: app/main.py -> handler variable
CMD ["app.main.handler"]
