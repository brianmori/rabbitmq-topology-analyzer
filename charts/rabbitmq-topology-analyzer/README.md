# RabbitMQ Topology Analyzer Helm chart

This chart installs the combined frontend/API image as one Deployment and one
ClusterIP Service. Ingress and a read-only static-capture PVC are optional.
It creates no Secrets, identity provider, RabbitMQ server, or persistent
application state. Helm 4.3.1 is the version verified in this repository.

## Install

Run from the repository root after the desired image has been published:

```bash
helm lint --strict charts/rabbitmq-topology-analyzer
helm template topology charts/rabbitmq-topology-analyzer -f /path/to/values.local.yaml
helm upgrade --install topology charts/rabbitmq-topology-analyzer \
  --namespace topology --create-namespace -f /path/to/values.local.yaml
```

The default image is `ghcr.io/brianmori/rabbitmq-topology-analyzer`, tagged with
the `appVersion` from [Chart.yaml](Chart.yaml).
Repository metadata does not mean this image has been published. Override
`image.tag` for another release or set `image.digest: sha256:...` to pin an
exact image; digest takes precedence. Use `imagePullSecrets: [{name: registry-auth}]`
for an existing private-registry pull Secret in the release namespace.

The default `auth.mode: none` grants anonymous analyst access. Use it only
inside a trusted network boundary. Defaults expose only the ClusterIP Service;
Ingress requires an explicit host and controller configuration.

## Authentication

Keep credentials out of values files and Git. Create/manage provider Secrets
outside Helm in the release namespace. Only the selected provider's keys are
projected into a read-only volume, readable by UID/GID 10001. See the
[application guide](../../README.md#run-with-docker) for the users-file and
role-mapping JSON formats, Argon2 requirements, and provider behavior.

Basic values reference an existing Secret containing the `users.json` key:

```yaml
auth:
  mode: basic
  publicOrigin: https://topology.example.com
  existingSecret: topology-basic
```

OIDC values reference a Secret with `client-secret` and `role-mapping.json`:

```yaml
auth:
  mode: oidc
  publicOrigin: https://topology.example.com
  existingSecret: topology-oidc
  oidc:
    issuer: https://identity.example.com/realms/topology
    clientId: topology
    roleClaim: roles
```

Register `https://topology.example.com/api/auth/oidc/callback` at the provider.
The chart derives that URI from `auth.publicOrigin`, lowercasing the host and
removing the default `:443` port. The origin must have no path or trailing slash.
TLS must terminate at the Ingress or another trusted reverse proxy. Preserve
`Origin`, cookies, and the application's CSRF header. OIDC uses secure cookies,
the default `openid offline_access` scopes, and RS256; configure
`auth.oidc.scopes`, `acceptedAlgorithms`, or `discoveryUrl` for your provider.
The application validates provider files/configuration before readiness.

Override `auth.basic.usersKey`, `auth.oidc.clientSecretKey`, and
`auth.oidc.roleMappingKey` if the existing Secret uses different key names.
The chart never reads Secret contents during rendering. After rotating a Secret,
restart the Deployment with `kubectl rollout restart deployment/NAME -n topology`;
provider files are loaded at process startup. ConfigMap changes automatically
change the pod-template checksum and trigger replacement.

## Static captures and Ingress

Without `captures.existingClaim`, the image starts with an empty static catalogue
and accepts session uploads. To add static captures, set that value to an
existing PVC containing `manifest.json` and its referenced capture files at the
volume root. The claim and mount are read-only at `/captures`; UID/GID 10001
must be able to read the files. The chart does not create or populate the PVC.
See the [manifest format](../../README.md#read-only-static-captures).

Ingress is controller-neutral and disabled by default:

```yaml
ingress:
  enabled: true
  className: your-ingress-class
  host: topology.example.com
  tlsSecretName: topology-tls
  annotations: {} # Controller-specific body/concurrency/buffering settings.
```

Manage TLS outside the chart. Match the externally visible host to
`auth.publicOrigin`. Configure the selected proxy/controller's aggregate upload
body limit above `captures.maxUploadBytes` to allow multipart overhead, bound
concurrent requests, and avoid buffering large uploads ahead of the application's
authentication boundary. The chart does not install or choose an ingress
controller; empty annotations do not enforce these outer limits.

## Capacity and lifecycle

One replica, one ASGI worker, and `Recreate` are fixed. Additional compute
children use `analysis.workers`; there is no replica/autoscaling/command/env
override. Sessions, uploads, jobs, and results belong to one process. Pod
replacement, upgrades, and restarts discard that state and cause downtime;
static files remain on the externally managed PVC. Do not scale this workload
outside Helm.

The [annotated values](values.yaml) list all settings; [the schema](values.schema.json)
rejects unknown settings and invalid types/ranges. The main capacity groups are:

| Values | Purpose/default |
|---|---|
| `analysis.*` | Two compute children, two queued jobs, 120-second heartbeat, four-hour result TTL |
| `captures.*` | 50 MiB aggregate file upload limit; 5 uploads per owner, 25 globally; eight-hour topology TTL |
| `storage.*` | Integer byte quantities for the application quota, two disk-backed `emptyDir` volumes, and container ephemeral-storage request/limit |
| `resources.requests` / `resources.limits` | CPU/memory requests `2`/`2Gi` and limits `4`/`8Gi` |
| `service.port` | ClusterIP HTTP port, default `80`; application listens on `8000` |

Defaults reserve a 4 GiB results volume with a 3,000,000,000-byte application
quota, a 512 MiB temporary volume, and a 5 GiB/8 GiB ephemeral-storage
request/limit. Rendering requires the quota below the results-volume size,
both volumes together below the container limit, and request no greater than
limit. Leave headroom for logs, upload spooling, and filesystem overhead. Resource
requests/limits and admission policies still need review for the target cluster.

## Maintainer verification

`just chart-check` runs strict Helm lint and real-render tests for the defaults,
Basic/OIDC Secret references, static captures, Ingress, image digest overrides,
release isolation, and rejected scale/auth/storage values. It is included in
`just check` and CI; `just deployment-check` remains an alias. No cluster is
contacted by these checks. Validate the rendered resources against your target
cluster's admission policies before installation.

Run `just release-prepare TAG` from the repository root to set the chart's
`appVersion` alongside the other application versions. Chart `version` is
independent and changes when shipping a chart release. The image workflow
publishes container images only.
