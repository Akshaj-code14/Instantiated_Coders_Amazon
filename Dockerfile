# FIX (deploy-ready, item 1): same app, same behavior, judge-friendly container.
# One uvicorn process serves the single-file UI and the JSON API.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Dependencies first so rebuilds reuse the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Only the code the server needs (see .dockerignore for what stays out).
COPY backend ./backend
COPY frontend ./frontend

# uvicorn runs from backend/ because the app imports `server`, `agent`,
# `services` and `integration` as top-level packages.
WORKDIR /app/backend

EXPOSE 8000

# The host sets $PORT (Render/Railway do); defaults to 8000 locally.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
