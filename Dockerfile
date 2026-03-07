ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy lockfile and source for deterministic, reproducible builds.
COPY requirements.lock pyproject.toml ./
COPY src/ src/

RUN python -m pip install --upgrade pip && \
    python -m pip install --prefix=/install -r requirements.lock && \
    python -m pip install --prefix=/install --no-deps .


FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="ACI Platform"
LABEL org.opencontainers.image.description="AI Compute Intelligence Platform"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

# Keep source in image for stack traces and mounted static assets.
COPY pyproject.toml ./
COPY src/ src/
COPY frontend/ frontend/

RUN useradd --create-home --shell /bin/bash --uid 1000 --user-group aci && \
    mkdir -p /tmp && \
    chown -R aci:aci /app /tmp
USER aci

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["sh", "-c", "uvicorn aci.api.app:app --host 0.0.0.0 --port 8000 --workers ${ACI_UVICORN_WORKERS:-1}"]
