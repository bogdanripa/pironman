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
app/cli.py         python -m app.cli create <label>
app/routers/       apps, crons, query
paas-cron-dispatch host-side dispatcher, runs every minute from crontab
```

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

4. From then on it manages its own redeploys via `PUT /apps/paas-api/code`.

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

## Known-unverified

`set_image`, `set_env` and `deploy` in `coolify.py` were not exercised against
the live instance — only `create_app` and `delete_app` were. If a redeploy or
env injection misbehaves, read `https://coolify.bogdanripa.com/docs` in a
browser (it is session-authenticated; a bearer token will not fetch it) and
correct the body shapes there first.
