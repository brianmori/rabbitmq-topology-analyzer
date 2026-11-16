FROM node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS node-toolchain

FROM mcr.microsoft.com/playwright:v1.65.0-noble

# Use the same Node 24 toolchain as production; the browser image ships Node 26.
COPY --from=node-toolchain /usr/local/bin/node /usr/local/bin/node

WORKDIR /workspace

RUN npm install --global pnpm@12.18.0

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml frontend/
RUN cd frontend && pnpm install --frozen-lockfile

COPY frontend frontend
COPY benchmarks/.artifacts/representative-topology.json \
    benchmarks/.artifacts/representative-topology.json

WORKDIR /workspace/frontend

CMD ["pnpm", "benchmark:layout"]
