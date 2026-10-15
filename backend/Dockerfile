FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Render/Fly inject $PORT; default 8000 for local docker runs
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir /app
