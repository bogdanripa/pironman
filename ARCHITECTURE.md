# Pironman — Raspberry Pi PaaS architecture

This document describes the self-hosted platform that `paas-api` (this repo, aka
**pironman** / the "Prionman" MCP server) controls. It doubles as memory for
future work and as a replication guide for anyone wanting to build the same thing
on their own hardware.

The short version: a **Raspberry Pi 5 at home runs [Coolify](https://coolify.io)
as a mini-Heroku**. Each app is a Docker container with an automatic HTTPS URL.
`paas-api` is a thin FastAPI control plane on top of Coolify that also exposes
every operation as an **MCP server**, so an LLM (Claude, via a claude.ai
connector) can deploy and operate the whole box conversationally. Traffic enters
through Cloudflare → Traefik, apps scale to zero when idle (Sablier), deploys are
secretless (CI pushes an image; the box notices and redeploys itself), and
traffic analytics are harvested automatically from the shared proxy log.

---

## 1. Hardware & host

- **Raspberry Pi 5**, `linux/arm64`. This is the single most important
  constraint: **every image must be built for arm64** or it pulls fine and then
  dies with an exec-format error.
- Raspberry Pi OS. One disk (SD/SSD) — disk is the real capacity ceiling, not
  RAM or CPU.
- Docker Engine, with the Docker socket (`/var/run/docker.sock`) as the universal
  control surface — Coolify, Traefik, Sablier and `paas-api` all drive Docker
  through it.

> **Gotcha — `docker stats` shows 0 RAM.** Raspberry Pi OS ships with the kernel
> **memory cgroup controller disabled**, so Docker cannot report per-container
> memory (CPU still works). Fix once: add `cgroup_enable=memory cgroup_memory=1`
> to the single line in `/boot/firmware/cmdline.txt` and reboot.

---

## 2. The stack, top to bottom

```mermaid
flowchart TD
    U[User / bot] -->|HTTPS| CF[Cloudflare edge]
    CF -->|HTTP, real IP in CF-Connecting-Ip| TR[Traefik v3.6 · coolify-proxy]
    TR -->|Sablier plugin: wake if asleep| SB[Sablier]
    SB -.->|start container on demand| APP
    TR -->|route by Host| APP[App container :80]
    subgraph Coolify control
      CO[coolify] --- CDB[(coolify-db · Postgres)]
      CO --- CR[(coolify-redis)]
      CO --- RT[coolify-realtime]
      CO --- SEN[coolify-sentinel]
    end
    CO -->|creates/deploys containers, writes Traefik labels| APP
    API[paas-api · pironman MCP] -->|REST| CO
    API -->|docker.sock| TR
    API -->|docker exec| DBs[(per-app Postgres / Mongo)]
    APP --> DBs
    CLAUDE[Claude connector] -->|MCP /mcp| API
```

**Request path:** Cloudflare terminates TLS and forwards HTTP to Traefik
(`coolify-proxy`). Because we're behind Cloudflare, the **real client IP is in the
`CF-Connecting-Ip` header**, not the socket. Traefik routes by `Host` to the app
container on **port 80**. If the app is asleep, the Sablier Traefik plugin starts
it first and shows a brief "starting" page.

**Control path:** Coolify owns the lifecycle (create/deploy/delete containers,
generate Traefik labels, provision databases). `paas-api` is a facade over
Coolify's REST API plus direct Docker/DB access via the mounted socket.

---

## 3. Containers on the box

| Container | Role |
|---|---|
| `coolify` | The Coolify app (orchestrator + UI/API) |
| `coolify-db` | Coolify's own Postgres |
| `coolify-redis` | Coolify queue/cache |
| `coolify-realtime` | Websockets for the UI |
| `coolify-sentinel` | Coolify host metrics agent |
| `coolify-proxy` | **Traefik v3.6** — the edge proxy, with the Sablier plugin |
| `sablier` | `sablierapp/sablier` — scale-to-zero controller (:10000) |
| `<uuid>-<timestamp>` | An **app** container. The name is the Coolify resource uuid + a deploy timestamp, so **it changes on every deploy** |
| `api` (`paas-api`) | **This control plane.** Also an app, but self-managing |

App container names look like `khmhpu3k4rd7a6vwzfq3t922-151340268225`. The stable
identity is the **uuid prefix** (`coolify_uuid` in the `apps` table); the suffix
changes each redeploy — a fact that matters for anything that references a
container by name (see the Sablier gotcha in §9).

---

## 4. Networking & domains

- Every app is published at **`https://<app-id>-coolify.bogdanripa.com`**
  (`DOMAIN_SUFFIX = -coolify.bogdanripa.com`). Wildcard DNS + Cloudflare, so
  creating an app needs no DNS/cert/proxy step.
- **App ids become hostnames**: lowercase, alphanumeric + hyphens, one flat
  label, fixed at creation.
- Coolify stores app origins as `http://…` (not https): Cloudflare terminates
  TLS at the edge, and https here would make Traefik redirect-loop.
- **Apps must listen on port 80 on BOTH IP families** — not 3000/8080 (→ 502).
  Two clients connect from opposite directions, so each wrong bind fails
  differently:
  - **IPv4-only (`0.0.0.0`)** — the in-container healthcheck hits `localhost`,
    which resolves to `::1` first, is refused, and Coolify rolls the deploy back
    even though the app serves fine externally.
  - **IPv6-only** — the healthcheck passes and the container reports *healthy*,
    but Traefik connects to the container's IPv4 address, is refused, and every
    request 502s. The nastier failure: healthy container, dead site.

  Node's `listen(80, '::')` is dual-stack. **Python's is not** — asyncio sets
  `IPV6_V6ONLY`, so `uvicorn --host ::` listens on IPv6 only. Bind it explicitly:
  `socket.create_server(("::", 80), family=AF_INET6, dualstack_ipv6=True)` and
  pass the fd to uvicorn (see `web/run.py`). Port 80 is privileged → run as root.
  Verify from *outside* the container; `localhost` will lie to you.

---

## 5. The control plane (`paas-api` / pironman)

A FastAPI app (this repo) that:

1. **Wraps Coolify** (`app/coolify.py`) — create/delete/get app, set image, set
   env, deploy, set healthcheck.
2. **Exposes everything as MCP** (`fastapi-mcp`, mounted at `/mcp`) so the
   claude.ai connector can call each operation as a tool. Tool name = FastAPI
   `operation_id`; description = summary + docstring.
3. **Talks to Docker directly** via the mounted socket for things Coolify doesn't
   expose — container logs/stats, image digest checks, `docker exec` into
   database containers.
4. Runs **background loops**: hourly auto-update sweep, ~2 min analytics
   ingestion, ~2.5 min alert check.

It runs *as one of its own apps* (`api`), which is why it can redeploy itself
(§8) — the recursive part of the design.

### MCP transport gotchas (hard-won)

`fastapi-mcp` 0.4.0's defaults don't work with the claude.ai connector proxy.
`app/main.py` patches the SDK session manager to force:

- **`stateless=True`** — the control plane redeploys *itself* constantly, and MCP
  sessions live in memory; any stateful session dies on redeploy and every later
  call fails with "Invalid content from server". Stateless makes each tool call
  self-contained.
- **`json_response=False`** — answer as SSE (`text/event-stream`), which the
  connector proxy expects.
- The middleware that promotes `?key=` → `Authorization` header is **pure ASGI**,
  not Starlette `BaseHTTPMiddleware`, because the latter buffers/mangles the SSE
  stream and the proxy then rejects it.
- Server `version` is a real semver and the long description lives in
  `instructions` — the connector chokes if the whole description blob lands in
  `serverInfo.version`.
- Tools are annotated read-only / write / destructive (`ToolAnnotations`) so the
  connector UI groups them instead of dumping everything in "Other tools".

### Auth

`api_keys` table (sha256 only). Two kinds:

- **Admin keys** (`app_id` NULL) — full access; the bootstrap key and the
  connector `?key=` are these.
- **Deploy keys** (`app_id` set) — scoped to one app, may only
  `PUT /apps/<id>/code`. Handed out freely as an app's CI secret; a leak can only
  redeploy that one app to an already-published image.

---

## 6. Databases

- **`_paas` registry** — a Postgres database (created out-of-band with
  `pdb create _paas`) holding `paas-api`'s own tables: `apps`, `api_keys`,
  `shared_env`, `app_env`, `crons`, and the analytics/alerts rollups. The Postgres
  **host is resolved live** (`pdb host --engine postgres`) because Coolify
  recreates DB containers and the name changes.
- **Per-app databases** — provisioned on request via the host **`pdb`** script
  (mounted read-only into `paas-api` and invoked over the Docker socket). One
  shared Postgres container and one shared Mongo container back all app DBs; each
  app gets its own database + user. The connection string is injected as
  **`DATABASE_URL`** into the app on every deploy (recomposed each time — never
  hardcode it). `db_run_script` / `db_size` reach an app DB by
  `docker exec`-ing `psql`/`mongosh` into the shared engine container.

> `api` shows no DB size in `apps_stats` because it has **no attached app
> database** — it uses `_paas`, which lives outside the per-app attach model, so
> its `apps` row has no `db_engine`.

### Self-migrating schema

`app/db.py` holds a `_SCHEMA` of `CREATE TABLE IF NOT EXISTS` /
`ALTER TABLE … ADD COLUMN IF NOT EXISTS` run at every startup — so a new feature
(env vars, analytics, alerts) needs **no manual SQL on the Pi**.

---

## 7. Environment variables

Two scopes, both stored in `_paas` and injected on deploy:

- **Shared** (`shared_env`, tools `env_*`) — account-wide, applied to every app.
  Single-owner box, so cross-cutting secrets (e.g. an OpenAI key) live here.
- **App-specific** (`app_env`, tools `apps_env_*`) — one app; overrides a shared
  var of the same name.

Effective env = shared, overlaid by app-specific, plus platform-managed
`DATABASE_URL`. Setting/removing a var redeploys the affected app(s). Values are
**write-only** — listings show a masked preview only.

---

## 8. Deploy pipeline (secretless)

Apps deploy with **no repository secret**:

```mermaid
flowchart LR
    DEV[git push to main] --> GH[GitHub Actions]
    GH -->|build linux/arm64| IMG[image]
    IMG -->|push :latest + :sha| GHCR[ghcr.io]
    GH -->|curl POST /apps/ID/refresh · unauthenticated| API[paas-api]
    API -->|docker pull + inspect digest| GHCR
    API -->|digest changed → set_image + deploy| CO[Coolify]
    CO --> APP[new container]
```

1. CI builds an **arm64** image, pushes to `ghcr.io` tagged `:latest` (+ `:sha`).
2. CI calls the app's **unauthenticated** `POST /apps/<id>/refresh`.
3. `paas-api` pulls the watched tag through the host Docker daemon, and **only if
   the image digest actually moved** re-points Coolify at the new image and
   redeploys. Also runs **hourly** regardless.

`/refresh` is safe to leave unauthenticated: it takes no caller-supplied image, so
the worst a caller can do is trigger a digest check. `apps_deploy_workflow`
returns the exact GitHub Actions YAML + Dockerfile rules to wire this up. A
private-registry digest check needs one server-side credential
(`GHCR_USER`/`GHCR_TOKEN`) — one platform credential, not a per-app secret.

`api` itself is the exception: it deploys via the **authenticated**
`apps_update_code` path with a scoped deploy key, and is opted out of
auto-update, so a self-deploy is deliberate.

---

## 9. Scale-to-zero (Sablier)

Idle apps are stopped and started on demand by **Sablier**, integrated as a
**Traefik plugin** (`experimental.plugins.sablier` in the proxy's static config)
plus the `sablier` container (Docker provider, watching `/var/run/docker.sock`).
A request for a sleeping app hits the Sablier middleware, which starts the
container (using its Coolify healthcheck to tell "started" from "ready") and
serves a brief waiting page.

**Enrollment is the fragile part, and the cause of a real outage-of-absence we
hit:** an app is enrolled by attaching the Sablier middleware to its Traefik
router. If that enrollment lives anywhere Coolify regenerates — the default
router labels, or a hand-edited file under `/data/coolify/proxy/dynamic/` —
then **the next deploy wipes it and the app silently stops sleeping** (stays
"Up" forever with zero traffic). Because container names carry a deploy
timestamp, enrolling by container **name** breaks on every redeploy for the same
reason.

**Durable design (implemented in `app/sablier.py`):** `paas-api` reads the app
container's **current labels straight off the running container** (Coolify's
complete generated set — nothing to reconstruct), adds `sablier.enable=true` +
`sablier.group=<app-id>` and a Sablier plugin middleware keyed on the **stable
app id** (not the volatile container name), prepends that middleware to every
Traefik router chain, and writes the whole set back as Coolify **read-only custom
labels** (`is_container_label_readonly_enabled`) so Coolify stops regenerating
them and the enrollment survives every redeploy.

- **Default: every app sleeps** when idle (`apps.sleep_when_idle`, default true).
- **`api` is hard-excluded** (`SABLIER_EXCLUDE`) — it runs the ingester,
  auto-update sweep and alert loop and must never sleep.
- **`apps_sablier`** toggles it per app (reads base labels → enroll/unenroll →
  redeploy). Requires the app to have deployed once (so its labels exist).
- **`SABLIER_AUTO_ENROLL`** (default off) makes new/updated apps enroll
  automatically after deploy, once verified. Config: `SABLIER_URL` (how Traefik
  reaches Sablier), `SABLIER_SESSION_DURATION`, `SABLIER_STRATEGY`.

> Verify enrollment on one app (`apps_sablier <app> true`) — confirm it sleeps
> when idle and wakes on request — **before** flipping `SABLIER_AUTO_ENROLL` on,
> since a wrong `SABLIER_URL` would break routing for every enrolled app.

---

## 9b. Frontends (static bundles + shared host)

An app can be a **backend** (docker image), a **static frontend** (a zip of built
assets), or **both** — they share one hostname.

Frontends are not containers. A CI job builds the assets, zips them and `PUT`s
them to `/apps/<id>/frontend`; `paas-api` unpacks the zip into a shared volume
(atomic staging + directory swap, so no half-written site) and the **shared static
host** (`web/`, an always-warm app, Sablier-excluded) serves them. A frontend
deploy is ~1 second and restarts nothing.

**Request resolution is static-first, with no path convention required:**

1. a path the app explicitly declared as backend-owned → backend
2. a real file in the bundle → serve it (hashed assets `immutable`, `index.html`
   `no-cache`)
3. **GET + `Accept: text/html`** (browser navigation) → `index.html`
4. anything else → backend (its own status codes preserved)

Step 3 makes SPA deep links work with zero config, and deliberately does **not**
consult the backend — so a page load never wakes a sleeping backend or pays a cold
start. Step 4 is where `fetch`/XHR goes, so an API 404 stays a 404 instead of
becoming `index.html`.

Step 1 is the escape hatch (`apps_backend_routes`, stored in
`apps.backend_routes`, normally empty) for the cases the heuristic can't know:
**OAuth callbacks, download links, server-rendered pages** — browser navigations
that the backend must answer. `/api` is *suggested* for new apps, never enforced.

Because it is same-origin, the frontend calls its API with a relative path: no
CORS, no API base URL, no cookie-domain juggling.

**Routing.** A frontend-only app has no container, so nothing would claim its
hostname. `app/routing.py` derives one Traefik router per frontend app from the
database and writes them onto the **static host's** Coolify custom labels, so the
app's Host resolves to the shared static host. It re-syncs after every frontend
deploy and after an app is deleted, and is a no-op when unchanged (a frontend
deploy must stay a one-second file swap, not a restart of the shared host).

> Coolify here rejects the readonly-labels flag (`422 … field is not allowed`),
> so the routers are written **unprotected** — a Coolify label regeneration can
> drop them. They come back on the next frontend deploy, since the label set is
> derived from the database. Symptom to recognise: a frontend that suddenly 404s;
> re-publish it to restore the route.

*Verified end to end:* frontend-only app created with no image → files published
→ route created automatically → served over HTTPS.

**Waking a sleeping backend:** the static host proxies **back through Traefik**
with `Host: <app-id>.internal` (an internal-only router carrying the Sablier
middleware) rather than straight to the container — a direct container connection
would bypass Sablier and fail against a sleeping app. Using the public host would
loop, since that host's catch-all points at the static host.

**CDN caching** is defended in layers, because a mis-cached API response is the
one failure that really hurts:
- **whitelist** at Cloudflare — cache only immutable asset paths, bypass
  everything else (a blacklist fails open; a whitelist fails closed);
- the static host sends **`Cache-Control: no-store, private`** on every proxied
  response, so a bad rule still can't cache an API reply;
- non-GET methods and `Set-Cookie` responses are never cached anyway;
- never enable Cloudflare **"Cache Everything"** — it overrides origin headers and
  is the classic cause of leaked API responses.

Verify with `CF-Cache-Status`: `DYNAMIC`/`BYPASS` on API paths, `HIT` on assets.

**Auth asymmetry, on purpose:** the backend's `/refresh` hook is unauthenticated
because it takes no caller content (it only makes the box pull an image the
registry controls). A frontend upload *is* caller-supplied content served on the
app's domain, so it requires the app's **scoped deploy key** (`PAAS_KEY`) — which
can only touch that one app.

## 10. Analytics & observability

All cross-app analytics come from the **one place every app's traffic already
flows through: the Traefik access log** — nothing is installed per app.

- Traefik is configured to write a **JSON access log** and keep the
  `User-Agent` and `Cf-Connecting-Ip` headers (one-time proxy flags).
- `paas-api` tails the `coolify-proxy` container log over the Docker socket on a
  ~2 min loop (`app/analytics.py`), turning each request into a **cookieless
  visitor** = `sha256(salt | ip | user-agent)` — the app id is deliberately *not*
  in the hash, so one person across two apps is one visitor. Ingestion is
  idempotent via a `StartUTC` cursor.
- Rollups in `_paas`: `analytics_visits` (per app/visitor/day, with an `is_bot`
  flag), `analytics_first_seen` (cohorts), `analytics_perf` (requests / 4xx /
  5xx / summed latency), `analytics_latency` (additive histogram → p50/p95
  without storing raw samples), `analytics_agents` (top raw user-agent strings).
- **Read-only MCP tools:** `analytics_overview` (uniques, hits, DAU/WAU/MAU,
  humans-vs-bots, per-app breakdown), `analytics_timeseries`, `analytics_cohorts`,
  `analytics_agents`, `analytics_recent` (live tail of raw requests), and
  `apps_stats` (per-app running state, live CPU/RAM, disk, DB size, error rate,
  p50/p95, plus host CPU/RAM/disk headroom).
- A human dashboard is served by the control plane at
  **`/analytics/dashboard?key=<PAAS_KEY>`** — self-contained HTML, inline SVG
  charts, no external assets.

---

## 11. Alerting

`app/alerts.py` runs a background loop that messages **Telegram** when an app goes
**down**, **recovers**, or throws **new 5xx** errors. A consecutive-failure
debounce keeps rolling redeploys from looking like outages. Configure with
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`; unset, the loop is a no-op.
`alerts_test` confirms delivery.

---

## 12. Hard-won lessons (checklist)

- **arm64 only** — an amd64 image pulls then dies with exec-format error.
- **Listen on `:80` bound to `::`**, run as root — anything else 502s or fails
  the localhost healthcheck and gets rolled back.
- **Real client IP is `CF-Connecting-Ip`** — behind Cloudflare the socket IP is
  the edge, not the user.
- **Enable the memory cgroup** or `docker stats` reports 0 RAM.
- **Anything Coolify generates, Coolify regenerates** — Sablier enrollment (and
  any custom route label) must live in Coolify `custom_labels`, keyed on the
  stable app id, not in a hand-edited file or a container-name reference.
- **MCP must be stateless + SSE** for the claude.ai connector, and the key-promote
  middleware must be pure ASGI.
- **Deploys go through CI, never hand-built images** — hand builds won't be arm64
  and won't reproduce; the box watches the registry itself.
- **The `api` control plane is special** — no auto-update, no scale-to-zero,
  authenticated self-deploy only.

---

## 13. Replicating this from scratch

1. **Hardware:** Raspberry Pi 5, arm64 OS, Docker. Enable the memory cgroup
   (`/boot/firmware/cmdline.txt`).
2. **Coolify:** install it; create one project + server + destination. Note the
   project/server/destination uuids.
3. **DNS/TLS:** point a wildcard `*-coolify.<your-domain>` at the box through
   Cloudflare (proxied). Coolify's Traefik gets certs via Let's Encrypt / CF.
4. **Edge proxy:** the Traefik `coolify-proxy`. Turn on the JSON **access log**
   keeping `User-Agent` + `Cf-Connecting-Ip` (for analytics), and load the
   **Sablier plugin** for scale-to-zero.
5. **Sablier:** run `sablierapp/sablier` with the Docker provider.
6. **Databases:** a shared Postgres and (optionally) Mongo container; a host
   `pdb` script to create/drop/host per-app databases. Create the `_paas`
   registry DB.
7. **Control plane:** deploy this repo as an app (`api`), mounting the Docker
   socket and `pdb`. Set the env in `.env.example` (Coolify uuids, `PAAS_DB_*`,
   `DOMAIN_SUFFIX`, optional `GHCR_*`, `GITHUB_TOKEN`, `TELEGRAM_*`,
   `ANALYTICS_*`). Bootstrap one admin key.
8. **Connect Claude:** add a claude.ai custom connector pointing at
   `https://api-<suffix>/mcp?key=<admin-key>`. Set read-only tools to
   always-allow.
9. **Ship apps:** for each new app, `apps_create`, then drop the
   `apps_deploy_workflow` output into its repo. Push to main → CI builds arm64,
   pushes to ghcr, calls `/refresh`, the box redeploys itself. No secrets.

---

*Kept as living memory. When the platform's behavior changes, update this file in
the same PR.*
