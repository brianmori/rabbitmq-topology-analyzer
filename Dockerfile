FROM node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS frontend-builder

WORKDIR /workspace/frontend
RUN corepack enable && corepack prepare pnpm@12.18.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/index.html frontend/tsconfig.app.json frontend/vite.config.ts ./
COPY frontend/src/ src/
COPY frontend/public/favicon.svg frontend/public/icons.svg public/
# Compile only the shipped application; repository CI also checks test tooling.
RUN pnpm exec tsc -p tsconfig.app.json && pnpm exec vite build

FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

FROM python:3.14.5-slim-bookworm@sha256:a9bee15510a364124aa24692899d269835683b883de42f7ebec8c293cf679ccb AS backend-builder

COPY --from=uv /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
WORKDIR /workspace/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --group build --no-install-project --no-build
COPY backend/src/ src/
# Never package operator-provided static data, even if it exists in the checkout.
RUN mkdir -p static/definitions \
    && printf '{"schemaVersion":1,"analyses":[]}\n' > static/definitions/manifest.json \
    && uv build --wheel --no-build-isolation --python /opt/venv/bin/python --out-dir /wheels \
    && uv sync --locked --no-dev --no-install-project --no-build \
    && uv pip install --python /opt/venv/bin/python --no-deps /wheels/rabbitmq_topology_analyzer_backend-*.whl \
    && uv pip check --python /opt/venv/bin/python

FROM python:3.14.5-slim-bookworm@sha256:a9bee15510a364124aa24692899d269835683b883de42f7ebec8c293cf679ccb

ARG VERSION=0.1.0
ARG REVISION=unknown
ARG CREATED
LABEL org.opencontainers.image.title="RabbitMQ Topology Analyzer" \
    org.opencontainers.image.description="Offline RabbitMQ capture visualization and SDK topology analysis" \
    org.opencontainers.image.source="https://github.com/brianmori/rabbitmq-topology-analyzer" \
    org.opencontainers.image.version="${VERSION}" \
    org.opencontainers.image.revision="${REVISION}" \
    org.opencontainers.image.created="${CREATED}"

COPY --from=backend-builder /opt/venv /opt/venv
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=frontend-builder /workspace/frontend/dist /app/frontend
RUN /opt/venv/bin/python -c 'import os; from importlib.metadata import version; assert version("rabbitmq-topology-analyzer-backend") == os.environ["VERSION"], "VERSION must match installed package metadata"'

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RABBITMQ_FRONTEND_DIST_DIR=/app/frontend

USER 10001:10001
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
