# paas-api

Control plane for the Pironman box. An **app** = one container + zero-or-one
database + a public URL + a set of cron jobs. Facade over Coolify: Coolify
still owns image pulls, the Traefik proxy and deploys.

## Layout

```
app/config.py      env + hostname helpers
app/db.py          asyncpg pool against _paas
app/auth.py        bearer key -> sha256 -> api_keys
app/coolify.py     facade client (VERIFIED / UNVERIFIED marked per call)
app/provision.py   wraps the host `pdb` script
app/cronmatch.py   dependency-free 5-field cron matcher
app/envs.py        shared + per-app env vars: desired-set + Coolify sync
app/cli.py         python -m app.cli create <label>
app/routers/       apps, crons, query, scaffold, env
paas-cron-dispatch host-side dispatcher, runs every minute from crontab
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
   - `PAAS_KEY` — a dedicated platform key for the redeploy call, minted with:

     ```
     docker exec -it <api-container> python -m app.cli create ci-paas-api
     ```

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

## Test sequence — do not skip step 1

1. `POST /apps {"id":"t1","image":"traefik/whoami:latest"}` — no database.
   Check `https://t1-coolify.bogdanripa.com` answers. This proves the facade
   end to end and is the only step that can invalidate the whole design.
2. Same with `"db_engine":"postgres"` — check `DATABASE_URL` landed in the
   container env.
3. `POST /apps/t2/db/query {"script":"SELECT 1"}`
4. Add a cron for `* * * * *`, watch `/var/log/paas-cron.log`.
5. `DELETE` both; confirm Coolify app, database and registry row are all gone.

## Environment variables

Two scopes, both stored in `_paas` and injected into the container on deploy:

- **shared** (`shared_env`, tools `env_*`) — account-wide, applied to every app.
  This is a single-owner box, so cross-cutting secrets like an OpenAI API key
  live here rather than being pasted into each app.
- **app-specific** (`app_env`, tools `apps_env_*`) — one app only, and overrides
  a shared variable of the same name.

An app's effective environment is *shared, overlaid by app-specific*, plus the
platform-managed `DATABASE_URL` (reserved — it cannot be set as a variable).
Setting or removing a variable pushes it to Coolify and redeploys the affected
app(s) so it reaches the running container; a shared change redeploys every app.
Pass `redeploy=false` to stage a batch and let the next code deploy apply it.

Values are **write-only**: they are pushed to Coolify but never read back in
plaintext through the API. Listings (`env_list`, `apps_env_list`, `get_app`)
show a masked preview only, so a secret never lands in a tool result.

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
