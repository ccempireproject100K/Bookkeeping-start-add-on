# Backend-only image (build context: backend/). It does NOT include the tax
# engine, so tax estimates answer 503 "engine_not_built". For a full API image,
# build the root Dockerfile instead:  docker build -t bookkeeping-api .
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Render/Fly inject $PORT; default 8000 for local docker runs
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir /app
