# Mirrorgate

Mirror container images from public registries into a private destination registry.

Mirrorgate is a small HTTP service that wraps `skopeo` to copy images from public
registries (Docker Hub, ghcr.io, quay.io, gcr.io, mcr.microsoft.com, registry.k8s.io,
us-docker.pkg.dev) into a single private registry of your choice. It is built for
environments where workload nodes cannot reach the public internet directly and
must pull from an internal registry, and where pulls to the public internet have
to go through a corporate proxy.

It exposes:

- a JSON API at `/api/*` (optional `X-API-Key` auth) for CI pipelines, and
- an htmx UI at `/ui/*` for humans.

Copies run on an in-memory queue with N worker threads. Jobs can be listed,
streamed, cancelled, and retried.

## How it works

```
   ┌────────────────────┐     POST /api/copy        ┌─────────────────────┐
   │  CI pipeline       │  ────────────────────────▶│  Mirrorgate         │
   │  (Jenkins/GitLab)  │   {"src":"...","dest":...}│   - queue           │
   └────────────────────┘                           │   - N workers       │
                                                    │   - skopeo copy     │
                                                    └─────────┬───────────┘
                                                              │
                              pull via CORPORATE_PROXY        │      push direct
                       ┌──────────────────────────────────────┴──────────────┐
                       ▼                                                     ▼
            ┌─────────────────────┐                          ┌─────────────────────┐
            │ Public registry     │                          │ Private registry    │
            │ docker.io / ghcr.io │                          │ ($NEXUS_REGISTRY)   │
            │ quay.io / ...       │                          │                     │
            └─────────────────────┘                          └─────────────────────┘
```

The destination is always the private registry configured via `NEXUS_REGISTRY`.
Only the path and tag come from the request.

## API

All `/api/*` endpoints require `X-API-Key: <API_KEY>` when `API_KEY` is set on the
server. `/health` and `/api/registries` are open. The UI under `/ui/*` is intended
to sit behind an authenticating reverse proxy (e.g. oauth-proxy on OpenShift).

### POST `/api/copy`

Queue a copy. Returns `202` with a `jobId`.

```bash
curl -X POST http://localhost:8080/api/copy \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <api-key>' \
  -d '{
    "src":  "docker.io/library/nginx:1.27",
    "dest": "library/nginx:1.27"
  }'
```

Request body:

| Field       | Required | Description                                                            |
|-------------|----------|------------------------------------------------------------------------|
| `src`       | yes      | Full source ref: `[registry/]image[:tag]`. Default registry `docker.io`, default tag `latest`. |
| `dest`      | no       | `image[:tag]` on the private registry. Defaults to mirror of `src`.    |
| `src_user`  | no       | Username for source registry (for authenticated pulls).                |
| `src_token` | no       | Token / password for source registry.                                  |

Response:

```json
{ "jobId": "...", "state": "queued", "dest": "registry.example.com/library/nginx:1.27" }
```

### Other endpoints

| Method | Path                          | Description                                       |
|--------|-------------------------------|---------------------------------------------------|
| GET    | `/health`                     | Liveness/version probe.                           |
| GET    | `/api/jobs`                   | List recent jobs.                                 |
| GET    | `/api/jobs/<id>`              | Job state and metadata.                           |
| GET    | `/api/jobs/<id>/log`          | Plain-text job log.                               |
| POST   | `/api/jobs/<id>/cancel`       | Cancel a queued or in-flight job.                 |
| POST   | `/api/jobs/<id>/retry`        | Retry a failed or cancelled job.                  |
| GET    | `/api/registries`             | Health snapshot of upstream registries.           |
| GET    | `/ui/`                        | htmx UI.                                          |

### Supported source registries

`docker.io`, `ghcr.io`, `quay.io`, `gcr.io`, `mcr.microsoft.com`,
`registry.k8s.io`, `us-docker.pkg.dev`. Anything else is rejected at parse time.

## Configuration

All configuration is environment-driven. The Helm chart sets these for you; the
table below is the canonical source of truth.

| Variable                | Required | Default | Description                                                       |
|-------------------------|----------|---------|-------------------------------------------------------------------|
| `NEXUS_REGISTRY`        | yes      | —       | Destination registry host, e.g. `registry.example.com`. The app exits if unset. |
| `NEXUS_USER`            | no       | —       | Username for the destination registry.                            |
| `NEXUS_PASS`            | no       | —       | Password / token for the destination registry. Inject via Secret. |
| `API_KEY`               | no       | —       | If set, `/api/*` requires `X-API-Key: <value>`. Inject via Secret.|
| `CORPORATE_PROXY`       | no       | —       | `http://host:port` proxy used for pulls. Pushes bypass it.        |
| `NO_PROXY_EXTRA`        | no       | —       | Comma-separated extra hosts to add to `NO_PROXY`.                 |
| `PORT`                  | no       | `8080`  | Listen port.                                                      |
| `WORKERS`               | no       | `3`     | Number of worker threads.                                         |
| `HISTORY_SIZE`          | no       | `500`   | Job history retained in memory.                                   |
| `HEALTH_CHECK_INTERVAL` | no       | `30`    | Seconds between upstream-registry health probes.                  |

Secrets (`NEXUS_PASS`, `API_KEY`) should be supplied via a Kubernetes `Secret`
that you create out of band. See `chart/values.yaml` for the references.

## Deploying with Helm

The chart lives under `chart/`. It does not ship any cluster-internal defaults:
`image.repository`, `image.tag`, and `nexus.host` are required and the chart will
refuse to render if they are missing.

### 1. Create the secret

```bash
kubectl create secret generic mirrorgate-secrets \
  --from-literal=NEXUS_PASS='<password>' \
  --from-literal=API_KEY="$(openssl rand -hex 32)"
```

### 2. Install the chart

Minimal `values.yaml`:

```yaml
image:
  repository: registry.example.com/devops/mirrorgate
  tag: "0.1.0"

nexus:
  host: registry.example.com
  user: mirrorgate
  passwordSecret:
    name: mirrorgate-secrets
    key: NEXUS_PASS

apiKeySecret:
  name: mirrorgate-secrets
  key: API_KEY

corporateProxy: "http://proxy.example.com:8080"
```

```bash
helm install mirrorgate ./chart -f values.yaml
```

### 3. Expose it (optional)

On OpenShift, enable the route:

```yaml
openshift:
  route:
    enabled: true
    host: mirrorgate.apps.example.com
    tls:
      termination: edge
      insecureEdgeTerminationPolicy: Redirect
```

On vanilla Kubernetes, wire your own Ingress to the `Service` named after the
release (`{release}-mirrorgate` by default).

See `chart/values.yaml` for the full set of knobs (`resources`, `nodeSelector`,
`tolerations`, `affinity`, `serviceAccount`, `podSecurityContext`,
`securityContext`, `extraEnv`, `imagePullSecrets`).

## CI integration

Templates are provided under `ci-templates/`:

- `ci-templates/gitlab-ci.yml` — a `.mirrorgate-copy` job template driven by
  `MIRRORGATE_URL`, `DEST_REGISTRY`, optional `MIRRORGATE_API_KEY`.
- `ci-templates/Jenkinsfile` — a `syncImage(...)` helper that reads the same env
  vars.

Both POST to `/api/copy` with the `{src, dest}` shape documented above.

## Building from source

```bash
docker build -t mirrorgate:dev .
docker run --rm -p 8080:8080 \
  -e NEXUS_REGISTRY=registry.example.com \
  mirrorgate:dev
```

## Operational notes

- **OCI vs Docker manifests.** `skopeo copy` handles format conversion. If your
  destination registry rejects OCI, the failure classifier reports
  `NexusRejectedManifest`.
- **Multi-arch.** Mirrorgate copies the full image (all platforms) by default.
- **Single-flight.** Two requests for the same `dest:tag` collapse into one job.
- **Restricted SCC.** The container does not require root and runs cleanly under
  OpenShift's `restricted-v2` SCC.

## License

MIT. See `LICENSE`.
