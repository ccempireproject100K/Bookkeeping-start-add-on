# API image with the tax engine compiled in. Build from the REPO ROOT:
#   docker build -t bookkeeping-api .
#   docker run -p 8000:8000 --env-file backend/.env bookkeeping-api
#
# backend/Dockerfile (build context backend/) cannot see tax-engine/, so its
# image answers 503 "engine_not_built" for tax estimates. Use this one.

# --- 1. compile the TypeScript tax engine --------------------------------
FROM node:22-bookworm-slim AS taxengine
WORKDIR /src/tax-engine
COPY tax-engine/package.json tax-engine/package-lock.json ./
RUN npm ci --include=dev
COPY tax-engine/ ./
RUN npm run build

# --- 2. Python API, with the node runtime the engine bridge shells out to --
FROM python:3.12-slim-bookworm
COPY --from=taxengine /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
# lib/tax_engine_driver.mjs resolves ../../tax-engine/dist from backend/lib
COPY --from=taxengine /src/tax-engine/dist tax-engine/dist
COPY --from=taxengine /src/tax-engine/package.json tax-engine/package.json
# Render/Fly inject $PORT; default 8000 for local docker runs
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir /app/backend
