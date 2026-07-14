FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
ARG INSTALL_ML=false
ARG TORCH_CPU_VERSION=2.6.0+cpu
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && if [ "$INSTALL_ML" = "true" ]; then \
        /opt/venv/bin/pip install --extra-index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_CPU_VERSION}" \
        && /opt/venv/bin/pip install ".[ml]"; \
    else \
        /opt/venv/bin/pip install "."; \
    fi

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
COPY . .
# .dockerignore excludes every local report except this reviewed aggregate.
# Keep the explicit COPY so the admin quality artifact remains an intentional
# part of the runtime image instead of an accidental consequence of COPY . .
COPY reports/presentation_quality/presentation_quality_report.json \
    /app/reports/presentation_quality/presentation_quality_report.json
RUN chown -R app:app /app

EXPOSE 8000

USER app

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
