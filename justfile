# List all available recipes.
default:
    @just --list

# Run every configured static check.
check: backend-check frontend-check

# Run lint checks for both applications.
lint: backend-lint frontend-lint

# Check Python formatting. The frontend does not have a formatter configured yet.
fmt: backend-fmt

# Apply safe backend lint and formatting fixes.
fix: backend-fix

# Run the currently configured test suite. A frontend test runner has not been added yet.
test: backend-test

# Run the checks intended for continuous integration.
ci: check test

# Run backend lint, formatting, and type checks.
backend-check: backend-lint backend-fmt backend-typecheck

# Lint the FastAPI backend.
backend-lint:
    cd backend && uv run ruff check src

# Check backend formatting.
backend-fmt:
    cd backend && uv run ruff format --check src

# Apply backend lint and formatting fixes.
backend-fix:
    cd backend && uv run ruff check --fix --show-fixes src
    cd backend && uv run ruff format src

# Type-check the FastAPI backend.
backend-typecheck:
    cd backend && uv run mypy src

# Run backend tests.
backend-test:
    cd backend && uv run pytest

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
