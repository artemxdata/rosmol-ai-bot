ARG PYTHON_BASE_IMAGE=python:3.11.15-slim-trixie@sha256:00af38ae2ed311628970782e8a2d7f014d8909dbc63cb97bc0a158187f4db045
FROM ${PYTHON_BASE_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements/runtime.lock requirements/ml.lock \
     requirements/sdist-build-tools.lock requirements/sdist-bootstrap.lock \
     ./requirements/
ARG INSTALL_ML=false
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -c "from importlib.metadata import version; assert version('pip') == '24.0'; assert version('setuptools') == '79.0.1'" \
    && /opt/venv/bin/python -m pip install --require-hashes --only-binary=:all: \
        -r requirements/sdist-build-tools.lock \
    && /opt/venv/bin/python -c "from importlib.metadata import version; assert version('pip') == '26.1.2'; assert version('setuptools') == '83.0.0'" \
    && /opt/venv/bin/python -m pip install --require-hashes --no-deps \
        --no-build-isolation --no-binary=docopt \
        -r requirements/sdist-bootstrap.lock \
    && if [ "$INSTALL_ML" = "true" ]; then \
        /opt/venv/bin/python -m pip install --require-hashes --only-binary=:all: \
            -r requirements/ml.lock; \
    else \
        /opt/venv/bin/python -m pip install --require-hashes --only-binary=:all: \
            -r requirements/runtime.lock; \
    fi

FROM ${PYTHON_BASE_IMAGE} AS runtime

ARG RELEASE_GIT_SHA
ARG OCI_SOURCE=https://github.com/artemxdata/rosmol-ai-bot
RUN python -c "import re,sys; value=sys.argv[1]; sys.exit(0 if re.fullmatch(r'[0-9a-f]{40}', value) else 'RELEASE_GIT_SHA must be a full lowercase Git SHA')" "${RELEASE_GIT_SHA}"
LABEL org.opencontainers.image.title="rosmol-ai-bot" \
      org.opencontainers.image.source="${OCI_SOURCE}" \
      org.opencontainers.image.revision="${RELEASE_GIT_SHA}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app \
    && install -d -o app -g app /home/app/.cache /home/app/.cache/huggingface \
        /home/app/.cache/torch \
        /opt/models

COPY --from=builder /opt/venv /opt/venv
# Runtime allowlist: application code, migration/runtime entrypoints, frozen
# public seed metadata, reviewed eval cases, and the aggregate admin report.
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY scripts/init_qdrant.py scripts/index_kb.py scripts/check_ml_runtime.py \
     scripts/sync_yonote_kb.py scripts/build_yonote_kb_seed.py \
     scripts/prefetch_huggingface_models.py scripts/hde_transport_admin.py \
     scripts/purge_old_memory.py ./scripts/
COPY data/knowledge_base_seed.json data/forums_registry.json \
     data/kb_source_corrections.json data/response_contract_v1.json ./data/
COPY eval/cases/ ./eval/cases/
COPY deploy/huggingface_models.lock.json ./deploy/huggingface_models.lock.json
COPY reports/presentation_quality/presentation_quality_report.json \
    /app/reports/presentation_quality/presentation_quality_report.json
RUN chown -R app:app /app

EXPOSE 8000

USER app

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
