FROM python:3.14-slim AS base

LABEL org.opencontainers.image.title="ACI Platform"
LABEL org.opencontainers.image.description="AI Compute Intelligence Platform"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system dependencies.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Copy source and build metadata together so the build backend
# can find the package during install.
COPY pyproject.toml .
COPY src/ src/

# Install the application and runtime server.
RUN pip install --no-cache-dir . && \
    pip install --no-cache-dir uvicorn[standard]

# Create non-root user.
RUN useradd --create-home --shell /bin/bash aci && \
    chown -R aci:aci /app
USER aci

# Health check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "aci.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
