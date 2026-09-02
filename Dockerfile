# syntax=docker/dockerfile:1.7

FROM node:20-alpine AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/home/polytrade/.local/bin:${PATH}"

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 polytrade \
    && useradd --uid 1000 --gid 1000 --create-home polytrade

COPY --chown=polytrade:polytrade backend/ backend/
COPY --chown=polytrade:polytrade docs/ docs/
COPY --from=frontend-build --chown=polytrade:polytrade /build/frontend/dist/ frontend/dist/

USER polytrade
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"]

# --proxy-headers makes request.client.host the real caller rather than the
# Caddy container, which is what every per-client rate limit is keyed on and
# what the access log records. The app port is only `expose`d on the internal
# Docker network -- Caddy is the sole possible peer -- so trusting the hop is
# safe here; FORWARDED_ALLOW_IPS can narrow it further.
# uvicorn reads FORWARDED_ALLOW_IPS from the environment, so this stays
# overridable without giving up the exec form (no shell, signals reach uvicorn).
ENV FORWARDED_ALLOW_IPS="*"
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--proxy-headers"]
