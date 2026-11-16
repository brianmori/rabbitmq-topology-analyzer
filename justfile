# List all available recipes.
default:
    @just --list

# Start both development servers; Ctrl+C stops both.
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    repo_dir={{quote(justfile_directory())}}

    for command in uv node pnpm; do
        if ! command -v "$command" >/dev/null 2>&1; then
            printf 'Required command is missing: %s\n' "$command" >&2
            exit 1
        fi
    done

    # Use the committed dependency versions without rewriting either lockfile.
    (cd "$repo_dir/backend" && uv sync --locked)
    (cd "$repo_dir/frontend" && pnpm install --frozen-lockfile)

    # Give each server its own process group, including reload workers, so
    # cleanup stops every process started by this recipe (Bash 3+).
    set -m
    backend_pid=''
    frontend_pid=''

    cleanup() {
        trap - EXIT INT TERM
        for server_pid in "$backend_pid" "$frontend_pid"; do
            if [[ -n "$server_pid" ]]; then
                kill -TERM -- "-$server_pid" 2>/dev/null || true
            fi
        done
        wait 2>/dev/null || true
    }

    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    (
        cd "$repo_dir/backend"
        exec uv run --locked --no-sync uvicorn main:app --app-dir src --reload --port 8000
    ) &
    backend_pid=$!

    (
        cd "$repo_dir/frontend"
        exec pnpm dev --port 5173 --strictPort
    ) &
    frontend_pid=$!

    printf '\nFrontend: http://localhost:5173\nAPI docs: http://localhost:8000/docs\nPress Ctrl+C to stop both servers.\n\n'

    # Bash 3 (the macOS default) does not provide wait -n. If either server
    # exits, preserve its status and let the EXIT trap stop the other one.
    while kill -0 "$backend_pid" 2>/dev/null && kill -0 "$frontend_pid" 2>/dev/null; do
        sleep 1
    done

    exit_status=0
    if ! kill -0 "$backend_pid" 2>/dev/null; then
        wait "$backend_pid" || exit_status=$?
    else
        wait "$frontend_pid" || exit_status=$?
    fi
    exit "$exit_status"

# Run repository quality checks, including rendered Helm configurations.
check: backend-lock-check backend-check frontend-check chart-check

# Run lint checks for both applications.
lint: backend-lint frontend-lint

# Check Python formatting. The frontend does not have a formatter configured yet.
fmt: backend-fmt

# Apply safe backend lint and formatting fixes.
fix: backend-fix

# Run all configured unit and component tests.
test: backend-test frontend-test

# Run the checks intended for continuous integration, including the pinned
# Chromium production-build acceptance journeys.
ci: check test e2e

# Run the production-build browser acceptance suite.
e2e:
    cd frontend && pnpm test:e2e

# Check the shared dependency lock before any check can install dependencies.
backend-lock-check:
    cd backend && uv lock --check --offline

# Validate application, installed package, OpenAPI, and Helm appVersion metadata.
release-check tag:
    uv run --project backend --locked python -m tools.release {{quote(tag)}}

# Build locally with OCI metadata. No image is pushed by this recipe.
image-build image="rabbitmq-topology-analyzer:release":
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    version=$(uv run --directory backend --locked python -c 'import tomllib; from pathlib import Path; print(tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"])')
    revision=$(git rev-parse HEAD)
    if [[ -n "$(git status --porcelain)" ]]; then revision="${revision}-dirty"; fi
    created=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    docker build --build-arg VERSION="$version" --build-arg REVISION="$revision" --build-arg CREATED="$created" --tag "{{image}}" .

# Exercise the installed production wheel offline, without mounting app source.
image-smoke image="rabbitmq-topology-analyzer:release":
    #!/usr/bin/env bash
    set -euo pipefail
    root="{{justfile_directory()}}"
    container=""
    cleanup() {
        if [[ -n "$container" ]]; then docker stop --timeout 10 "$container" >/dev/null; fi
    }
    trap cleanup EXIT
    for capture_mode in upload static; do
        settings=(--env RABBITMQ_AUTH_MODE=none)
        if [[ "$capture_mode" == static ]]; then
            settings+=(--env RABBITMQ_FULL_EXPORT_DEFINITIONS_DIR=/verification/definitions)
        fi
        container=$(docker run --detach --rm --network none --read-only \
            --cap-drop ALL --security-opt no-new-privileges \
            --tmpfs /tmp:rw,noexec,nosuid,size=128m \
            --env RABBITMQ_DEPLOYMENT_MODE=production \
            --env RABBITMQ_ANALYSIS_WORKERS=1 \
            --env RABBITMQ_ANALYSIS_RESULTS_MAX_BYTES=1000000 \
            --env RABBITMQ_ANALYSIS_RESULTS_DIR=/tmp/results \
            --mount "type=bind,src=$root/backend/benchmarks/smoke_image.py,dst=/verification/smoke_image.py,readonly" \
            --mount "type=bind,src=$root/backend/pyproject.toml,dst=/verification/pyproject.toml,readonly" \
            --mount "type=bind,src=$root/backend/uv.lock,dst=/verification/uv.lock,readonly" \
            --mount "type=bind,src=$root/frontend/e2e/fixtures/static,dst=/verification/definitions,readonly" \
            "${settings[@]}" "{{image}}")
        if ! docker exec "$container" python /verification/smoke_image.py "$capture_mode"; then
            docker logs "$container"
            exit 1
        fi
        cleanup
        container=""
    done

# Generate and validate the canonical 50-vhost SDK fixture.
representative-fixture:
    cd backend && uv run python -m benchmarks.representative_capture

# Measure and enforce the backend Milestone 5 performance budgets.
benchmark-backend:
    cd backend && uv run python -m benchmarks.benchmark_backend

# Export the real-SDK graph and measure production Chromium ELK layouts.
benchmark-layout:
    cd backend && uv run python -m benchmarks.export_representative_topology ../benchmarks/.artifacts/representative-topology.json
    cd frontend && pnpm benchmark:layout

# Run the pinned browser benchmark with the canonical 4-CPU/8-GiB limits.
benchmark-layout-container:
    cd backend && uv run python -m benchmarks.export_representative_topology ../benchmarks/.artifacts/representative-topology.json
    docker build --file benchmarks/browser.Dockerfile --tag rabbitmq-topology-layout-benchmark:local .
    docker run --rm --cpus 4 --memory 8g --shm-size 2g --mount type=bind,src={{justfile_directory()}}/benchmarks/.artifacts,dst=/workspace/benchmarks/.artifacts rabbitmq-topology-layout-benchmark:local

# Run every reproducible Milestone 5 performance budget.
benchmark: benchmark-backend benchmark-layout

# Lint Helm values/templates and verify rendered deployment configurations.
chart-check:
    helm lint --strict charts/rabbitmq-topology-analyzer
    uv run --project backend --locked pytest -q -c backend/pyproject.toml tests/test_helm_chart.py

# Compatibility alias for the former standalone manifest check.
deployment-check: chart-check

# Run backend lint, formatting, and type checks.
backend-check: backend-lint backend-fmt backend-typecheck

# Lint the FastAPI backend.
backend-lint:
    cd backend && uv run ruff check --config pyproject.toml src benchmarks ../tools ../tests

# Check backend formatting.
backend-fmt:
    cd backend && uv run ruff format --check --config pyproject.toml src benchmarks ../tools ../tests

# Apply backend lint and formatting fixes.
backend-fix:
    cd backend && uv run ruff check --fix --show-fixes --config pyproject.toml src benchmarks ../tools ../tests
    cd backend && uv run ruff format --config pyproject.toml src benchmarks ../tools ../tests

# Type-check the FastAPI backend.
backend-typecheck:
    cd backend && uv run mypy src

# Run backend tests.
backend-test:
    cd backend && uv run pytest

# Type-check and run frontend unit/component tests.
frontend-test:
    cd frontend && pnpm test

# Run the frontend production build and lint checks.
frontend-check: frontend-build frontend-lint

# Type-check and build the React application for production.
frontend-build:
    cd frontend && pnpm build

# Type-check the React application without producing a Vite build.
frontend-typecheck:
    cd frontend && pnpm exec tsc -b

# Lint the React application with Oxlint.
frontend-lint:
    cd frontend && pnpm lint
