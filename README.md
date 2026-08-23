# paas-api

Control plane for the Pironman box. An **app** = one container + zero-or-one
database + a public URL + a set of cron jobs. Facade over Coolify: Coolify
still owns image pulls, the Traefik proxy and deploys.

## Layout

```
app/config.py      env + hostname helpers
app/db.py          asyncpg pool against _paas
app/auth.py        bearer key -> sha256 -> api_keys; per-app deploy keys
app/coolify.py     facade client (VERIFIED / UNVERIFIED marked per call)
app/provision.py   wraps the host `pdb` script
app/hostexec.py    host_run_script — a root shell on the Pi itself, via nsenter
app/cronmatch.py   dependency-free 5-field cron matcher
app/envs.py        shared + per-app env vars: desired-set + Coolify sync
app/autoupdate.py  watch an app's tag, redeploy when the image digest changes
app/cli.py         python -m app.cli create <label>
app/routers/       apps, crons, query, host, scaffold, env, refresh
paas-cron-dispatch host-side dispatcher, runs every minute from crontab
paas-watchdog      host-side liveness watchdog, runs every 5 minutes
docker-destroy-log host-side recorder of container destroy events (+ .service, .logrotate)
app/heartbeat.py   background-loop liveness (task_heartbeat)
```

`db.py` creates the tables it owns (`shared_env`, `app_env`) on startup with
`CREATE TABLE IF NOT EXISTS`, so adding these needs no manual SQL on the Pi —
the next deploy of paas-api brings them into being.

## Bootstrap (manual, once)

The API cannot create itself, and you cannot mint the first key through an
endpoint that requires a key.

1. Build and push for **arm64**:

   ```
   docker buildx build --platform linux/arm64 \
     -t ghcr.io/bogdanripa/paas-api:latest --push .
   ```

2. Create the app **by hand** in Coolify:
   - image `ghcr.io/bogdanripa/paas-api:latest`
   - domain `http://api-coolify.bogdanripa.com`   (http, not https)
   - Ports Exposes `80`
   - Custom Docker Options:
     `-v /var/run/docker.sock:/var/run/docker.sock -v /usr/local/bin/pdb:/usr/local/bin/pdb:ro`
   - environment variables from `.env.example`, filled in from
     `pdb create _paas` and a Coolify API token

3. Mint the first key:

   ```
   docker exec -it <api-container> python -m app.cli create bootstrap
   ```

4. From then on it manages its own redeploys — see **Self-management** below.

## Self-management (recursive deploys)

paas-api is created by hand in Coolify during bootstrap, so it has no row in its
own `apps` registry and cannot redeploy itself until it *adopts* itself once.
After that, a normal deploy workflow in this repo redeploys the control plane
through its own `apps_update_code` endpoint — paas-api deploying paas-api.

The app's registry id is **`api`** (it serves `api-coolify.bogdanripa.com`, and
`api` + the `-coolify.bogdanripa.com` suffix is that host). The repo is
`pironman` and the image is `ghcr.io/bogdanripa/paas-api`, so all three names
differ — the workflow in `.github/workflows/deploy.yml` spells them out
explicitly rather than deriving them from the repo name.

Unlike a normally-scaffolded app, this workflow logs in to ghcr with a **PAT**
(`GHCR_PAT` secret) rather than the built-in `GITHUB_TOKEN`. The `paas-api`
package was first pushed by hand during bootstrap, so it exists owned by the
account with no link to the repo, and `GITHUB_TOKEN` gets `403 Forbidden` on
push. A fresh app whose package is created by its first CI run does not need
this — see the note that `apps_deploy_workflow` returns.

One-time setup, in order:

1. Get the current code (this feature) running once by hand — build/push the
   image and hit **Redeploy** in Coolify — so `apps_adopt` exists on the live
   instance.
2. Add two repository secrets on `bogdanripa/pironman`:
   - `PAAS_KEY` — this app's scoped deploy key. Issue it with the `apps_deploy_key`
     tool (`POST /apps/api/deploy-key`); it can only redeploy `api`, nothing else.
     (Admin keys — for the connector or bootstrap — still come from
     `python -m app.cli create`, which mints unscoped keys.)
   - `GHCR_PAT` — a classic PAT with `write:packages`, so the workflow can push
     to the hand-created `paas-api` package (see above).

3. Adopt paas-api into its own registry (once):

   ```
   PUT /apps/api/adopt
   {"coolify_uuid":"<uuid from the Coolify app URL>",
    "image":"ghcr.io/bogdanripa/paas-api:latest"}
   ```

   or the `apps_adopt` MCP tool. It writes a registry row only — it does not
   touch the running container. `db_engine` is null: paas-api reaches the `_paas`
   database through `PAAS_DB_*`, not a platform-injected `DATABASE_URL`.

From then on every push to `main` builds an arm64 image and redeploys the
control plane through itself. The first workflow run before steps 2–3 are done
still builds and pushes the image; only its redeploy curl fails (401/404) until
the key and the registry row are in place.

> A bad image can take paas-api down, and then its own API can't fix it. Recover
> from the Coolify UI directly — **Rollback** to a previous deployment, or point
> the tag back at a known-good `sha-…` and **Redeploy**.

## Host dispatcher

```
sudo cp paas-cron-dispatch /usr/local/bin/
sudo mkdir -p /opt/paas/app && sudo cp app/cronmatch.py /opt/paas/app/
sudo touch /opt/paas/app/__init__.py
( sudo crontab -l 2>/dev/null; echo '* * * * * /usr/local/bin/paas-cron-dispatch >> /var/log/paas-cron.log 2>&1' ) | sudo crontab -
```

## Watchdog

```
sudo cp paas-watchdog /usr/local/bin/
sudo mkdir -p /var/lib/paas-watchdog
( sudo crontab -l 2>/dev/null; echo '*/5 * * * * /usr/local/bin/paas-watchdog --restart >> /var/log/paas-watchdog.log 2>&1' ) | sudo crontab -
paas-watchdog --dry-run          # see what it would say, send nothing
```

It answers one question nothing else on the box can: **is the platform's
background work still happening?** Containers that die are already covered —
Coolify sets `restart: unless-stopped`, so Docker brings them back. What has
actually bitten this platform is the opposite: a loop that keeps running while
achieving nothing. An analytics cursor frozen for 23 hours. A sweep whose first
hour-long sleep outlives every redeploy, so it never runs at all. A dispatcher
that stopped firing jobs. Every one of those leaves a **healthy container and a
silent log**, because a loop with nothing to do and a loop that is dead produce
identical output — nothing.

So each loop records when it last *completed* and how long it may go without
completing (`task_heartbeat`, written by `app/heartbeat.py` and by the cron
dispatcher). Staleness is then a plain comparison, readable by anything with
database access. `platform_tasks_health` exposes the same view over MCP.

This runs on the **host**, not in a container, for one reason: `app/alerts.py`
already notifies Telegram about apps going down, but it runs *inside* paas-api
and so can never report that paas-api itself is wedged. Anything watching a
process from inside that process shares its fate. The watchdog therefore depends
on nothing but the stdlib, `docker` and `psql`, and reads Telegram credentials
out of the api container's config with `docker inspect` — which works on a
*stopped* container, which is exactly when it has something to say. Override with
`/etc/paas-watchdog.env` if the container may not exist at all.

Alerts are edge-triggered against `/var/lib/paas-watchdog/state.json`: one
message when a fault appears, one when it clears, nothing in between. A monitor
that repeats itself every five minutes is one people mute.

`--restart` is opt-in and narrow on purpose: only apps with
`sleep_when_idle = false`, after two consecutive failed runs. An app that sleeps
is *supposed* to be stopped, and a watchdog that "fixed" that would quietly
destroy scale-to-zero while reporting success.

Sleeping apps are still **checked**, just for the right thing. Their run state
says nothing — being stopped is the feature — but whether a container still
**exists** says everything: a deleted one can never be woken by any request and
502s until something redeploys it. So they are checked with `docker ps -a` and
reported as `<app> sleeps but has no container at all`, and never restarted (there
is nothing to `docker start`; only a redeploy recreates it). Before this, sleeping
apps were skipped outright, so the box's only alerting path had nothing to say
while an app sat unreachable for nine hours.

It also reports an app that has **more than one running container**
(`<app> has 2 running containers …`). Both carry the same Coolify-generated
Traefik router, so Traefik round-robins between them and the older one answers
with a stale image — while both containers stay healthy and every other signal
says the app is fine. Stop the leftover: `docker stop <name>`. Traefik's Docker
provider ignores non-running containers, so the router drops to one server
immediately, and `docker start` puts it back if you picked wrong.

## Destroy log

```
sudo cp docker-destroy-log /usr/local/bin/ && sudo chmod +x /usr/local/bin/docker-destroy-log
sudo cp docker-destroy-log.service /etc/systemd/system/
sudo cp docker-destroy-log.logrotate /etc/logrotate.d/docker-destroy-log
sudo systemctl daemon-reload && sudo systemctl enable --now docker-destroy-log
tail /var/log/docker-destroy.log
```

Docker keeps **no event history** — the stream is in-memory and rolls over in
minutes — so a container that vanishes overnight leaves nothing to read the next
morning. That gap turned one vanished container into a multi-hour reconstruction
from inference; this makes the next one a single `grep`. Every destroy is appended
with a UTC timestamp, container name and image; logrotate keeps 8 weeks.

The shell lives in a **file** rather than inline in the unit's `ExecStart` because
systemd expands `$VAR` itself — inline, it ate both the timestamp subshell and the
loop variable and logged garbage.

## A shell on the host (`host_run_script`)

`POST /host/run {"script":"df -h /","timeout":60}` runs a shell script **on the
Pi**, as root, in the host's own filesystem and network — not inside an app
container. It is the escape hatch for what has no tool: disk pressure and
cleanup, OS/kernel config, a container the platform did not create, `systemctl`,
`crontab -l`. Admin keys only; a scoped deploy key cannot reach it (`auth.py`
allows deploy keys nothing but their own three deploy routes).

The control plane is itself a container, so this is not a plain `subprocess`.
`app/hostexec.py` spends the one surface it has — the mounted Docker socket — on
a throwaway `--privileged --pid=host` container that `nsenter`s into PID 1's
namespaces:

```
docker run --rm -i --privileged --pid=host <image> \
  nsenter -t 1 -m -u -i -n -p -- timeout -k 5 <t> env -i PATH=… /bin/sh -c 'cd / && exec /bin/sh'
```

Four details are load-bearing:

- **The image is only a carrier for `nsenter`.** Everything the script runs comes
  from the host, so the default is paas-api's own image — already on the box, so
  no pull — and `HOST_EXEC_IMAGE` overrides it if that base ever drops
  util-linux.
- **`env -i`**, because nsenter passes the *caller's* environment through: without
  it a script's `env` would print this control plane's `COOLIFY_TOKEN` and
  `PAAS_DB_PASSWORD`, and inherit a PATH with no host sbin dirs.
- **`cd /` inside the shell**, not `nsenter --wd=/`: nsenter opens that directory
  *before* entering the mount namespace, so `--wd=/` would leave the script
  sitting in the container's root.
- **The script goes in on stdin** — nothing to quote, no argument-length limit —
  so the shell is `exec`'d rather than handed a `-c` command.

Two timeouts guard it: the host-side `timeout` bounds the script and lets the
helper exit cleanly, and an outer wait force-removes the container if it does not
(killing the `docker run` client alone would leave it running). Output is
combined stdout/stderr, truncated past 100k characters; the response carries the
script's `exit_code` and is a 200 even when that is non-zero.

There are no guardrails, and this is the machine everything else runs on. The
tool's description tells the model to read the script back before anything that
writes. It is **not** tagged destructive: that annotation makes the connector
prompt before every call, which stalls an unattended Routine indefinitely (see
"Running a routine unattended"). Interactively the description is what asks; in a
scheduled run nothing does.

## Running a routine unattended

A Routine fires on a schedule with nobody watching, so any tool that prompts
stops the run dead. The connector's approval gate keys on the MCP annotations in
`app/main.py`, so **the annotation set is the autonomy policy**:

| Annotation | In a Routine |
|---|---|
| `readOnlyHint=True` | runs |
| `readOnlyHint=False` (the `else` bucket) | untested — see below |
| `destructiveHint=True` | **prompts, and the run blocks until a human answers** |

`host_run_script` and `db_run_script` are kept out of `_DESTRUCTIVE` for exactly
this reason. The cost is real and worth stating plainly: a root shell on the box
and arbitrary SQL now execute in a scheduled run with no interactive
confirmation. What replaces the gate is the routine's own prompt — its
MUST-NOT-without-asking list is, as that prompt says, the only guardrail left.
Write those lists as if nothing else will stop the model, because nothing will.

**Whether the middle row prompts is not established.** On 2026-08-03 the audit
was blocked by a destructive tool and, once approved, every later call ran — but
that approval may have covered the session, so it proves nothing about the
`else` bucket. The test is simply whether the next unattended run completes on
its own; until one does, treat "makes-changes tools are fine" as unproven.

**Symptom of getting this wrong:** the run produces no output at all. A routine
that reports by exception is silent when healthy *and* silent when it never
started, so check that it ran before reading silence as an all-clear. The
2026-08-03 audit lost five and a half hours to precisely this.

## Test sequence — do not skip step 1

1. `POST /apps {"id":"t1","image":"traefik/whoami:latest"}` — no database.
   Check `https://t1-coolify.bogdanripa.com` answers. This proves the facade
   end to end and is the only step that can invalidate the whole design.
2. Same with `"db_engine":"postgres"` — check `DATABASE_URL` landed in the
   container env.
3. `POST /apps/t2/db/query {"script":"SELECT 1"}`
4. Add a cron for `* * * * *`, watch `/var/log/paas-cron.log`.
5. `DELETE` both; confirm Coolify app, database and registry row are all gone.

## Auto-update

Apps deploy with **one repository secret**, `PAAS_KEY` — the app's scoped deploy
key, which the platform installs itself (`apps_create` returns it,
`github_secret_set` writes it), so wiring an app up still needs no human step.
CI builds and pushes `:latest`; the box watches that tag and redeploys when the
image digest changes — **hourly** (an in-process loop, first run an hour after
start) and **immediately** when CI POSTs `/apps/<id>/refresh`. Digest resolution
goes through the host Docker daemon (`docker pull` + `inspect`), reusing its
existing pull credentials, and a redeploy fires only when the digest actually
moved.

`/refresh` authenticates with the app's scoped deploy key, as does the frontend
upload — one secret covers both halves. Because that key can only touch one app,
the call also carries the image CI just built, and on an app's **first** deploy
that image is what creates its container: registration writes a bare id, and the
pipeline is the only thing that knows what the app runs.

**`POST /apps/<id>/refresh` answers `202 Accepted`** with a deploy id, and the
work happens off-request. It used to block until `verify_deploy` finished, which
is bounded at 150s — but Cloudflare fronts this API and cuts a request at ~100s,
so the deploys worth hearing about were exactly the ones CI could not hear
about. Two `POST /apps/gepetel/refresh` calls on 2026-08-23 ran 125.0s at the
edge and both ended `499`.

Be precise about what those were, because the obvious reading argues for the
wrong fix. They were not slow successes: Coolify had **failed and rolled both
back inside 20s** (deployment queue rows 390/393 — healthcheck unhealthy,
`wget: not found`, the app crashing on a missing key). `verify_deploy` cannot
see Coolify's verdict, so it polls the container to its own deadline, and the
524 arrived before the `502` this hook was about to return correctly. The 150s
budget is therefore not too short and must not be raised — Coolify decides in
8-49s here, so a longer one would only delay honest failures.

`GET /apps/<id>/refresh?deploy=<id>` returns that verdict (`state` is
`succeeded`, `failed`, `running` or `unknown`), read from the `deploys` table so
it survives the control plane's own redeploys. **CI must gate on it**, and the
generated workflow does: it is the only check that can catch a rollback, because
a rollback leaves the previous container serving and the app's health endpoint
answers perfectly well with the old code in place. A failure also raises a
Telegram alert, as the hourly sweep's does.

An app's environment can be set **before it has a container**. `apps_env_set` on
a registered-but-never-deployed app stages the value (`staged: true`) and
`apply_image` syncs it into Coolify before the first deploy, so the container
boots configured. That is not a nicety: an app that reads config at import — the
OpenAI SDK throws on a missing key — cannot survive the boot that would give it
a container to configure, so the old "deploy it once, then retry" 400 was a
deadlock, and it is what failed gepetel's first four deploys.

New apps have auto-update on, watching the tag they were created with;
`apps_autoupdate` toggles it (off to hold a manual rollback). `app/autoupdate.py`
holds the routine. `apps_update_code` plus a scoped deploy key remain the
*authenticated* path for explicit deploys/rollbacks — the control plane itself
(`api`) still deploys that way and is opted out of auto-update.

## The dashboard (dogfooding the frontend feature)

The analytics dashboard used to be ~300 lines of HTML inside `dashboard.py`,
served by the control plane. It is now a **frontend-only app on this platform**:
`dashboard/` in this repo, published by `.github/workflows/deploy-dashboard.yml`
to `https://dashboard-coolify.bogdanripa.com`. No image, no container — the
shared static host serves it and the CDN caches it.

It was deliberately *not* put on the control plane's own hostname. Publishing a
frontend for `api` would route every request to it — the MCP connector, CI's
`/refresh`, every tool call — through the static host, so `web` breaking would
take the control plane with it, and the control plane is what you would use to
fix `web`. A viewer is not worth that coupling.

Being a separate origin, its `fetch` calls are cross-origin, so the control plane
allows the dashboard's origin on the read-only `/analytics/*` and `/stats/*`
paths (`app/cors.py`, `DASHBOARD_ORIGIN`). That middleware is narrow and pure
ASGI on purpose: no write endpoint becomes browser-reachable from another origin,
and it never touches `/mcp`, whose SSE stream is broken by response-rewriting
middleware. Auth is unchanged — every one of those paths still needs the key.

`GET /analytics/dashboard` on the API 308-redirects to the new app, so old links
keep working.

Setup, once:

```bash
# frontend-only app (no image)
curl -sS -X POST https://api-coolify.bogdanripa.com/apps -H "Authorization: Bearer $K" \
  -H 'Content-Type: application/json' -d '{"id":"dashboard"}'
```

Then install the returned `paas_key` as this repo's **DASHBOARD_PAAS_KEY** secret
and push — CI publishes the bundle.

**The key never stays in the URL.** Open the dashboard at `/` and it asks for an
API key, keeping it in `localStorage`. A key passed as `?key=` is stored and then
stripped by replacing the URL with `/`, so it does not remain in the address bar,
in history, or in a referrer header. A rejected key (401/403) is discarded and
the prompt returns, and "forget key" clears it.

That key is an **admin** key — the platform has no read-only scope, so a key that
reads analytics can also delete apps. This is an accepted trade on a single-owner
box: the key stays in browser storage, is never in a URL, and CORS only exposes
the read-only paths to the browser anyway. A read-only key scope would be the
stricter answer if this ever had more than one user.

## CDN caching and invalidation

The static host sets the cache policy per file: hashed filenames (and anything
under `/assets/` or `/static/`) get `immutable, max-age=1y`, entry files get
`no-cache`, other static files 5 minutes, and anything proxied to a backend
`no-store, private`. Cloudflare sits in front and respects `no-cache` on the
entry file — verified — so **a deploy is never hidden behind a stale
index.html**.

Two caveats worth knowing:

- Cloudflare's zone **Browser Cache TTL** (default 4 hours) *overrides* the
  origin's `max-age` on cacheable assets, so our 5-minute value becomes 4 hours
  in the browser. Set it to **"Respect Existing Headers"** (Caching →
  Configuration) to hand that decision back to the code that knows which file is
  which.
- With **content-hashed** filenames none of this matters — a new build produces
  new URLs and the `no-cache` entry file points at them immediately. Hashing is
  the real answer; purging is for bundles that ship `app.js` unhashed.

**Invalidation on publish:** a frontend deploy purges that app's cached
responses (`app/cdn.py`), never the whole zone — a zone-wide purge would evict every
other app's assets and send the whole box's traffic back to origin to fix one
app. It purges by **hostname** where the plan allows it (one call, and it also clears
files a deploy removed) and falls back to the exact URLs published, which every
Cloudflare plan supports — hostname, prefix and tag purging are Enterprise-only.
Which applies is discovered on the first attempt and remembered. In the fallback,
`/` is purged alongside `index.html`, since they are separate cache entries and
`/` is what browsers actually request. Set `CLOUDFLARE_API_TOKEN` (Zone.Cache Purge on this zone) and
`CLOUDFLARE_ZONE_ID` to enable it; without them the platform works unchanged and
unhashed assets simply wait out their TTL. A failed purge never fails a deploy —
the files are published and correct either way — and the result is reported as
`cdn` in the publish response.

## Container names

Coolify names containers `<resource-uuid>-<deploy-timestamp>`, which is
unreadable and changes on every deploy — so never reference a container by name
(all the code here matches on the uuid prefix instead). Two things make them
resolvable rather than renaming them, which Coolify would undo and which would
break its own bookkeeping:

- `apps_create` sets the Coolify application **name** to the app id, so the
  Coolify UI and the labels it derives show `space-invaders` rather than the
  image reference.
- Every custom-label write stamps **`pironman.app=<app-id>`** on the container,
  so it self-identifies regardless of what Coolify does with names.

`tools/papps` lists containers by app name, state and restart policy:

```bash
sudo cp tools/papps /usr/local/bin/ && sudo chmod +x /usr/local/bin/papps
papps        # running
papps -a     # include stopped (asleep) containers
```

## Reboots and scale-to-zero

Coolify already creates app containers with **`restart: unless-stopped`**
(verified on the box — only Coolify's own infrastructure uses `always`), so a
container Sablier stopped stays stopped when the daemon comes back, and one that
was running restarts. Scale-to-zero apps therefore come back from a reboot
**asleep** and start on their first request, with nothing for this codebase to
do. `api` and `web` are excluded from sleeping and always start.

Check it with:

```bash
docker ps -a --format '{{.Names}}' | while read c; do
  printf '%-45s %s\n' "$c" "$(docker inspect "$c" --format '{{.HostConfig.RestartPolicy.Name}}')"
done
```

`apps_get` also reports the live policy in `backend.runtime.restart_policy`.

**A stopped container is fine; a deleted one is fatal.** Sablier finds an app by
the `sablier.group` label, and that label lives on the container — so a slept
container is always findable, but a *deleted* one leaves the group empty and no
request can ever wake it (`Group not found`, then `No such container` on the name
fallback). Only a deploy recreates it. Coolify's forced Docker cleanup was
deleting them nightly, because `docker container prune`'s `label!=` filters are
ANDed rather than ORed and so excluded nothing; **`force_docker_cleanup` must stay
false**. `sablier.reconcile` now recreates a missing container at paas-api startup
and hourly, `paas-watchdog` reports one within 5 minutes, and
`/var/log/docker-destroy.log` records what removed it. See ARCHITECTURE.md §9.

**New deploys start asleep.** Sablier only stops instances it holds a session for,
and sessions come from requests through its middleware, never from a deploy — so a
deployed-but-never-called app used to stay up for ever with `sleep_when_idle: true`.
`autoupdate.sleep_after_deploy` stops it once the deploy is verified, so the flag
means what it says from the moment it lands.

### Is the first request after a quiet night slow?

Two flags describe sleeping and neither answers that on its own.
`sleep_when_idle` is what the app asked for; `sablier_enrolled` is whether the
middleware is actually stamped on its container. Auto-enrolment is gated
(`SABLIER_AUTO_ENROLL`, off by default but **`true` on this box**), so
`sleep_when_idle: true` with `sablier_enrolled: false` is a normal, indefinite
state on a box where it is off — an app that is *marked* to sleep and never does.
It is also why `sleep_after_deploy` refuses to stop an unenrolled app: nothing
would be able to start it again.

So `apps_get` reports a derived `backend.sleeps` (both flags true) and a plain
`backend.first_request_after_idle` alongside the raw pair, and `apps_create`
answers the same question in its `idle_behaviour` field — at create time, before
there is any app to inspect, which is when anyone handing a URL to friends
actually asks it.

## What an app is made of

`apps_create` takes no image and no repository. It registers an id, hands it a
hostname and optionally provisions a database — nothing runs. The app becomes a
**frontend** the first time CI uploads a bundle, a **backend** the first time CI
deploys an image (`PUT /apps/<id>/code`, which creates the container on that
first call), or **both**.

Information flows from GitHub to the platform, never the other way: the pipeline
is the only thing that knows what it built, so it reports it. That is why
`apps_update_code` takes an image and `apps_create` does not. `apps_adopt` also
takes one, since it describes an app that already exists in Coolify.

### One health path

`health_path` is recorded at create time, applied to the app's Coolify
configuration when the container is eventually created, and changeable
afterwards with `apps_update` (which rewrites the Coolify config immediately; the
running container keeps the check it started with until the next deploy).
`apps_get` echoes it back under `backend.health_path`, so it can be confirmed
rather than guessed at.

It is the **only** health path, and three things read it:

- the container healthcheck Coolify runs, which is what a silent rollback is
  decided on;
- the `HEALTHCHECK` line in the app's Dockerfile, which should name the same
  path — that instruction is a fallback for the case where the Coolify
  configuration did not land, not a second, independent setting. Two different
  paths is the one combination to avoid, because whichever check actually runs is
  then testing a route nobody meant;
- the deploy workflow's *wait for healthy* step, which requests it and requires a
  2xx.

For an app that ships a **frontend as well as a backend**, leaving it at `/` makes
that last check meaningless: `/` is answered by the static bundle straight from
the CDN, with no container in the path, so the step goes green with a dead API
behind it. Give such an app a path its backend owns — `/api/health` — and the
check tests something.

## Deploys are verified, not assumed

Coolify's deploy call is asynchronous and its rollback is **silent**: if the new
container fails to start or fails its healthcheck, Coolify removes it and the
previous container keeps serving. Everything then looks fine — the deploy
returned 2xx, a container exists, `apps_logs` reports `running (healthy)` — while
the code that was deployed is nowhere. That is exactly how a broken static-host
image went unnoticed while three separate signals said success.

So a deploy is now checked rather than assumed (`autoupdate.verify_deploy`): it
waits for the app's container to have a start time *later* than the one observed
before the deploy — proving it was genuinely replaced, not merely survived — and
to be running without an unhealthy healthcheck.

- **`POST /apps/<id>/refresh`** answers 202 and verifies off-request; the
  verdict is read back from **`GET /apps/<id>/refresh?deploy=<id>`**, which the
  generated workflow polls, so CI goes red instead of green. It cannot be
  returned inline — that wait is longer than Cloudflare's request ceiling (see
  "Auto-update"). A failure also alerts on Telegram, because nobody is watching
  a background deploy either.
- **`PUT /apps/<id>/code`** does the same, except for the control plane itself
  (`CONTROL_PLANE_APP`, default `api`), which cannot observe its own replacement
  — that request is served by the container being replaced.
- The **hourly sweep** verifies too and sends a Telegram alert on a rollback,
  since nobody is watching a background sweep.

## Deploy keys

Two kinds of API key, both stored only as sha256 in `api_keys`:

- **Admin keys** (`python -m app.cli create`, `app_id` NULL) — full access. The
  bootstrap key and the connector `?key=` are these.
- **Deploy keys** (`app_id` set) — scoped to one app: they may only trigger that
  app's deploy (`POST /apps/<id>/refresh`), redeploy it explicitly
  (`PUT /apps/<id>/code`) or upload its frontend (`PUT /apps/<id>/frontend`),
  enforced centrally in `auth.require_key`.
  `apps_create` returns one as `paas_key` the moment an app is created, and
  `apps_deploy_key` (re)issues one (revoking the previous). This is the app's
  `PAAS_KEY` CI secret — safe to hand out, since a leak can only redeploy that
  one app to an image tag that already exists in the registry.

## Environment variables

Two scopes, both stored in `_paas` and injected into the container on deploy:

- **shared** (`shared_env`, tools `env_*`) — account-wide, applied to every app.
  This is a single-owner box, so cross-cutting secrets like an OpenAI API key
  live here rather than being pasted into each app.
- **app-specific** (`app_env`, tools `apps_env_*`) — one app only, and overrides
  a shared variable of the same name.

An app's effective environment is *shared, overlaid by app-specific*, plus the
platform-managed `DATABASE_URL` (reserved — it cannot be set as a variable).

`DATABASE_URL` — and the identical `db_url` returned by `apps_create`,
`apps_attach_db` and `apps_get` — names an **internal Docker container**, not a
routable host. It resolves from inside the app's own network and nowhere else:
not from a laptop, not from CI, not from a local dev server. There is no exposed
port and no tunnel, and nothing about the string itself says so, which is exactly
why it reads like a URL you could point `psql` at. To touch the database from
outside, use `db_run_script`; to develop locally, run your own database and let
`DATABASE_URL` differ between environments. Each of those tools now returns the
caveat alongside the URL.
Setting or removing a variable pushes it to Coolify and redeploys the affected
app(s) so it reaches the running container; a shared change redeploys every app.
Pass `redeploy=false` to stage a batch and let the next code deploy apply it.

Values are **write-only**: they are pushed to Coolify but never read back in
plaintext through the API. Listings (`env_list`, `apps_env_list`, `get_app`)
show a masked preview only, so a secret never lands in a tool result.

## Analytics

Cross-app traffic stats with **nothing to instrument per app**. paas-api tails
the shared Traefik access log (the `coolify-proxy` container, over the mounted
docker socket) on a 120s loop, turns each request into a cookieless visitor id —
`sha256(salt | ip | user-agent)`, no cookie, no raw IP stored — and rolls it up
into `analytics_visits` / `analytics_first_seen`. The app_id is deliberately
*not* in the hash, so one person hitting two apps is one visitor: cross-app
unique counts and global cohorts are meaningful. Ingestion is idempotent via a
`StartUTC` cursor in `analytics_state`, so overlapping log windows never
double-count. Code is in `app/analytics.py`; tuning knobs are `ANALYTICS_PROXY`
and `ANALYTICS_SALT` (keep the salt stable — changing it resets identity).

Read-only MCP tools, each `app_id`-optional (omit for the whole platform):
`analytics_overview` (unique visitors, hits, DAU/WAU/MAU, humans-vs-bots split,
per-app breakdown when global), `analytics_timeseries` (daily visitors/hits),
`analytics_cohorts` (weekly retention), `analytics_agents` (top user-agents with
bot flag), `analytics_recent` (live tail of recent HTTP requests). A human-facing
dashboard of all of it runs as its own frontend-only app — see below.

The ingester also fills request-health rollups (`analytics_perf`, per-app 4xx/5xx
and summed latency), a latency histogram (`analytics_latency`) and each app's
last request time (`analytics_last_seen`, to the second — the day-keyed rollups
cannot answer "is anyone still using this?") from the same log, which feed the
resource view below.

An app fronted by the static host appears in the proxy log **twice** per request
— the client's leg and the static host's forward to the backend. Only the client's
leg is counted, so the numbers are what a visitor experienced, wake included;
`analytics_recent` still shows both and marks the forward `internal: true`. See
ARCHITECTURE §12 for how the two are told apart and for the one phantom `503` a
cold wake still leaves behind.

### Live resources (`apps_stats`)

`apps_stats` returns, per app in one call: running state, live CPU/RAM (from a
single `docker stats`), writable-layer disk, database size (Postgres
`pg_database_size` / Mongo `storageSize` via `docker exec`), 7-day request
health (volume, error rate, p50/p95 latency), and when it was **last accessed**
(all-time, not windowed). It also returns host totals — CPU
count, RAM, and disk used/free — for headroom. Same numbers power the dashboard's
Resources panel.

> **RAM shows 0?** Docker can only report per-container memory if the kernel
> memory cgroup controller is enabled. On Raspberry Pi OS it is off by default —
> add `cgroup_enable=memory cgroup_memory=1` to the single line in
> `/boot/firmware/cmdline.txt` and reboot. CPU works without it.

### Telegram alerts (optional)

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` and the box messages you when an
app goes down, recovers, or throws new 5xx errors (`app/alerts.py`, a background
loop). A short consecutive-failure debounce keeps rolling redeploys from looking
like outages. `alerts_test` sends a test message to confirm delivery. Unset, the
loop is a no-op.

**One-time host setup** — Traefik must actually write the access log, and keep
three headers: the two the visitor id needs (real client IP comes from
Cloudflare in `Cf-Connecting-Ip`; `User-Agent` sharpens the hash), and the
static host's forwarding marker, which is the only thing that tells one of its
internal hops from a client's request. Add these flags to the `coolify-proxy`
command (Coolify → Server → Proxy → Configuration, then restart the proxy — a
few seconds of edge downtime):

```
--accesslog=true
--accesslog.format=json
--accesslog.fields.headers.names.User-Agent=keep
--accesslog.fields.headers.names.Cf-Connecting-Ip=keep
--accesslog.fields.headers.names.X-Pironman-Backend=keep
```

Until that is enabled the tools simply report zeros — the rollup tables and loop
are already live, so numbers start accruing the moment the proxy logs.

The third flag is the newest and only affects fronted apps. Without it the
ingester falls back to the router name, which catches most of the static host's
internal hops but not the ones a *sleeping* app's wake produces — those are
counted against the app as 5xx it never returned, and are what makes a cold wake
look like an outage (ARCHITECTURE §12).

## Frontends (the `web` static host)

An app can ship a backend (docker image), a static frontend (a zip of built
assets), or both on one hostname. Frontends are served by **one shared container**
built from `web/` in this repo — see ARCHITECTURE.md §9b for the request
resolution rules and the caching model.

`web/` has its own workflow (`.github/workflows/deploy-web.yml`, path-filtered so
it doesn't rebuild `paas-api`) publishing `ghcr.io/bogdanripa/pironman-web`.

**One-time setup on the box** — the static host and `paas-api` must share the
bundle directory, which is the only part Coolify has to be told about:

1. Create the app: image `ghcr.io/bogdanripa/pironman-web:latest`, id `web`,
   health path **`/_health`** (its `/` returns 404 for an unrecognised Host, so a
   `/` healthcheck fails and the deploy is rolled back).
2. Mount the **same host directory** at `/srv/frontends` in **both** `web` and
   `api` (Coolify → app → Storages): `/data/pironman/frontends → /srv/frontends`.
   `paas-api` unpacks uploads there; the static host serves from there. The two
   SOURCE paths must match **exactly** — mounting `/data/pironman` on one and
   `/data/pironman/frontends` on the other puts the bundle one level too deep and
   every site answers `{"error":"no frontend for this host"}`. Verify with:
   `docker inspect <container> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'`
3. `web` is **hard-excluded** from scale-to-zero in code — it fronts every app
   that sleeps and is what wakes them, so it sleeping would strand all of them
   with nothing able to start anything.
4. Give `web` `SABLIER_URL` (default `http://sablier:10000`) and make sure it can
   reach that container — it calls Sablier's blocking API to start a sleeping
   backend, because Traefik cannot route to a stopped one. Routing itself is
   automatic: `app/routing.py` writes a router per fronted app onto `web`'s
   labels and adds the `X-Pironman-Backend` marker condition to each linked
   backend's own router.

Until that exists, `apps_frontend_deploy` still accepts and stores bundles — they
simply aren't served yet.

## Checking the live box

`web/tests/test_server.py` covers the static host in isolation (a woken backend,
a dead one, SPA fallback, compressed passthrough, SSE streaming) and runs in CI
before its image is built.

`.github/workflows/verify.yml` is the other half: run it by hand
(`workflow_dispatch`, optionally naming an app with both a frontend and a
backend) to assert the same invariants against the **running** platform, from
outside, as a real client sees them. Reach for it after any routing, proxy or
Sablier change — the failures that have cost real time here all returned a
perfectly good status code while serving the wrong thing.

## Redirects

Any app can carry an ordered list of redirect rules (`apps_redirects_list` /
`apps_redirects_set`, stored in `apps.redirects`). They are applied by the shared
static host **before** files and before the backend, so a rule wins even where a
file or route still exists, and they take effect immediately with no redeploy.

Patterns follow the conventions from Netlify-style `_redirects`, so rules usually
port across unchanged:

```
/old-page          /new-page                  exact
/blog/*            /news/:splat               wildcard, tail captured
/posts/:id         /articles/:id              named segment
/docs/:section/*   /help/:section/:splat      both
/gone              https://example.com/new    external target
```

First match wins, so put specific rules before catch-alls. Status may be 301
(default), 302, 307 or 308; the incoming query string is carried over unless the
target sets its own. `apps_redirects_set` replaces the whole list — list, edit,
send back.

An app with redirects but no frontend is routed through the static host too, so
this works for backend-only apps. Validation rejects the mistakes that would
otherwise fail silently: a target placeholder the pattern never captures, a
duplicate source, or a rule pointing at itself.

## Known-unverified

`create_app`, `delete_app`, `set_image`, `deploy` and `get_app` in `coolify.py`
have all now run against the live instance — `set_image`/`deploy` via the
recursive self-deploy and `get_app` via `apps_adopt`. Still unexercised:
`set_env`, `list_envs` and `delete_env`. The self-deploy did **not** cover
`set_env`, because paas-api has no env vars and no database, so the first real
`env_set`/`apps_env_set` is where those env shapes get confirmed. If env
injection misbehaves, read `https://coolify.bogdanripa.com/docs` in a browser (it
is session-authenticated; a bearer token will not fetch it) and correct the body
shapes there first.
