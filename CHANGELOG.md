# Changelog

## 0.1.0 — Unreleased

The first development release candidate includes:

- Consistent RabbitMQ Topology Analyzer branding, package metadata, runtime
  defaults, and deployment resource names.
- Offline static and ephemeral private-capture workspaces, using the published
  `rabbitmq-management-sdk==0.5.0` as the parsing and graph-analysis authority.
- Real-SDK integration regressions preserve rejection of invalid capture
  envelopes, safe atomic startup failure, and failed-replacement snapshot/result
  retention while accepting empty and older cluster exports.
- Safe topology exploration with React Flow, compound vhost layout in an ELK
  Web Worker, search, visibility filters, and keyboard-accessible details.
- Versioned JSON downloads of the full selected topology, including capture
  metadata, the snapshot revision, and resources hidden by graph filters.
- Owner-scoped, cancellable SCC and structural-cycle jobs with bounded worker
  admission, indexed results, paging, revision guards, and expiry.
- Structural-cycle searches include every cross-vhost cycle by default. A
  shared limit caps single-vhost cycles across the capture, with `0` for no limit.
- Plain-language analysis and upload guidance, more readable cycle paths, and
  consistent filter actions in the graph and results drawer. Keyboard users
  can return from results directly to the drawing.
- Mutually exclusive `none`, OIDC backend-for-frontend, and HTTP Basic modes;
  centrally enforced viewer/analyst capabilities and private-resource isolation.
- Browser-bound OIDC login, server-only provider credentials, stable ownership
  across refresh, pre-body CSRF/upload-role rejection, and exact-owner logout
  cleanup. Basic password verification has bounded, cancellation-safe capacity.
- Automated backend, frontend, and real-browser authentication acceptance.
- A top-level Helm chart for the combined application, with validated capacity
  settings, optional static captures/Ingress, and existing Basic/OIDC Secrets.
- Plain HTTP support for `none` mode on trusted private hostnames. Helm normalizes
  decimal ports when generating the OIDC callback URI.
- A refreshed Python runtime image and patched locked Python dependencies.
- Release tooling prepares application versions from a Git tag, including Helm
  `appVersion`.

State deliberately belongs to one replica and one ASGI worker. Restart loses
private captures, authentication sessions, jobs, and retained results. The
application never connects to or modifies a live RabbitMQ broker.

This is unreleased application metadata, not a publication announcement or an
SDK release. Final committed-candidate verification, dependency security
review, deployment acceptance, and publication approval remain separate gates.
