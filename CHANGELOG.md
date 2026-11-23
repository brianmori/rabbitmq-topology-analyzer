# Changelog

## 0.1.0 — 2026-11-23

The first release of **RabbitMQ Topology Analyzer** lets you visualize and
analyze captured RabbitMQ configurations offline.

### Added

#### Topology and analysis

- **Captures** — Open a preconfigured capture or upload a private capture for
  your session. You can supply queue and exchange observation exports as a pair.
- **Interactive graph** — Explore resources grouped by vhost, search by name,
  and filter the drawing. Layout runs in a browser worker. Use the keyboard to
  inspect resources and relationships or move between the graph and results.
- **Components and cycles** — Find groups of resources that can reach one
  another and trace individual round trips. Runs are cancellable, cycle results
  are paginated, and searches include all cross-vhost cycles by default. A shared
  limit controls the total single-vhost cycles across the capture.
- **JSON export** — Download the full selected topology as versioned JSON,
  including capture metadata, its snapshot revision, and resources hidden by
  graph filters.
- **Capture validation** — `rabbitmq-management-sdk` 0.5.0 validates captures
  and builds their topology. Invalid replacement uploads leave the existing
  capture and its results available.

#### Authentication and access

- **Authentication modes** — Choose unauthenticated access (`none`), HTTP
  Basic, or OIDC. Basic password verification has a concurrency limit. OIDC
  binds login to the initiating browser and keeps provider credentials on the
  server.
- **Permissions** — Viewers can inspect topologies and run analyses. Analysts
  can also manage their own uploads. Private captures and results are scoped to
  the caller's session.

#### Deployment and operations

- **Helm chart** — Deploy the application with optional Ingress and static
  captures. Reference existing Kubernetes Secrets for Basic or OIDC. The `none`
  mode supports plain HTTP on trusted private hosts.
- **Capacity controls** — Configure analysis worker, queue, and result storage
  limits. Retained analysis results expire automatically.
- **Release tooling** — Prepare application versions from a Git tag, including
  Helm `appVersion`.

> [!NOTE]
> Run one replica with one ASGI worker. Restarting clears private uploads,
> application sessions, jobs, and retained results. Configured static captures
> load again at startup.
