# Working on this repo

## Verify. Do not assume.

There is a shell on the box (`host_run_script`), read-only MCP tools for almost
every fact about it, and the source in `/opt/paas`. **So there is nearly always a
way to check, and the rule is to check rather than reason from what the code
implies.**

Before deciding on a change, confirm the diagnosis **twice, by two independent
routes** — not the same query run again. A second look that shares an assumption
with the first is not a second look. Read the code *and* the live state. Check
the database *and* the container. Read the log *and* reproduce the command.
Only once both agree, make the change.

An assumption is only harmless if being wrong about it changes nothing. "Probably
the default" is not harmless when a default is exactly what drifts.

### This is not theoretical — it is what went wrong

Every one of these was stated confidently from reading the code, and every one
was false:

| Assumed | Actually |
|---|---|
| ping-pong was never enrolled in Sablier (`SABLIER_AUTO_ENROLL` defaults off) | `sablier_enrolled` was **true**, labels fully intact |
| The running `api` was not built from this repo (`git log -S` found nothing) | It was; those commits had not reached `origin/main` yet |
| Two Postgres containers, `docker ps` picked the wrong one | **One** container, and it has `_paas` |
| `docker logs --since` returned nothing | It returns lines normally |
| The watchdog would report tasks as "never completed" | It **crashed** on the missing table |
| `api` could be found by its `pironman.app` label | `api` has no such label; only `web` does |
| `api` is covered by "apps that must not sleep" | Its row said `sleep_when_idle = true` — so the watchdog's `ALWAYS_ON` set, not the column, is what keeps it checked. (The row reads `false` as of 2026-08-18, and the container carries no Sablier labels; the lesson stands because the column is what drifted) |
| A repeated `analytics: nothing counted, cursor N minutes behind` WARNING means ingestion is stalled | Ingestion was **exact** — `analytics_perf` matched the proxy log request-for-request. All 43 such warnings were caught-up passes (tally `app == old`) |
| The Sablier controller is the container named `sablier` | **No container has that name.** It is a Coolify app, so its container is `<uuid>-<timestamp>`; `sablier` is a Docker **network alias**. Find it by image, `sablierapp/sablier` |
| `analytics_last_seen` lists only live apps (ARCHITECTURE.md said so) | It holds deleted apps too — `pingpong` and `analytics` are in it with no `apps` row. Only the `apps` table answers "does this app exist" |
| An app's uuid prefix identifies exactly **one** container, so `docker ps ... \| head -1` is safe | `wa-gateway` ran **two** at once — `<uuid>` and `<uuid>-<timestamp>` — both healthy, both carrying the same Traefik router, splitting its traffic across two image builds. `head -1` picked the right one and hid the other |
| Every app container is named `<uuid>-<timestamp>` | Not with `is_consistent_container_name_enabled` — then it is the bare `<uuid>`. Match the prefix, never the suffix |
| A sleeping app showing a ~10% 5xx rate is failing to wake | Those are the wake handshake's own internal `503`s (§9c), logged and counted like real errors. The client got `200` every time. Count the wakes before reading the rate as a fault |
| A ~10s cold wake is the app, or the healthcheck, or Traefik picking the container up | **Sablier's blocking call, alone** — `probe 0.05s, sablier 10.02s, retry 0.27s`. The app answered on its own IP at 0.76s. Four confident explanations were built by timing this from the outside and all four died on the next measurement; the fifth came from one log line inside `_proxy`. When a number is suspiciously *constant*, stop modelling accumulation and go find the fixed timer |
| "Connector tools run without permission prompts during a Routine" (the routine's own prompt said so) | A `destructiveHint=True` tool prompts anyway and **blocks the run**. The 2026-08-03 audit sat idle from ~02:00 to 07:35 on its first call. The prompt asserting something does not make the connector do it |
| The MCP `ToolAnnotations` set is cosmetic — it groups tools in the connector UI | It is the **autonomy policy**: the approval gate keys on it, so the set decides which tools a scheduled run can reach at all (`app/main.py`) |
| A routine that sent no Telegram ran and found nothing | It may never have started. "Reports by exception" makes *blocked* and *healthy* produce identical silence — confirm it ran before reading silence as an all-clear |
| README documents the five `--accesslog` flags as one-time setup, so the proxy has them | It has **four**. `X-Pironman-Backend=keep` was documented on 2026-08-05 and was still absent thirteen days later on 2026-08-18. Documenting a host step does not perform it. Note a plain `docker restart` will not apply it either — `coolify-proxy` restarted on 2026-08-18 and came back with the same four flags, because a restart reuses the container's existing `Args`; the container has to be **recreated**. Every fronted sleeping app therefore still carries one phantom 5xx per wake |
| `/var/log/paas-cron.log` last written three weeks ago, and its tail is a Python traceback — the dispatcher is dead | It ran a minute ago. The dispatcher **prints nothing when no job is due**, and with the `crons` table empty (as it was on 2026-08-22; it holds 2 enabled `gepetel` rows again as of 2026-08-25, and the log is back to 2 lines an hour) it had nothing to print since 2026-07-31 — so the file's mtime measures *cron activity*, not liveness, and the traceback frozen at its tail is a 2026-07-31 event that `tail` will keep surfacing as if it were the newest. Liveness lives in `task_heartbeat` (`platform_tasks_health`), which the dispatcher writes every minute regardless |
| To tell a sleeping app's phantom 5xx from a real one, match `ClientHost` against the static host's container IP — ARCHITECTURE §11 says so | It said so in the **wrong IP family**. The `coolify` network is dual-stack and `web`'s forwards arrive over IPv6, but `docker inspect --format '{{.IPAddress}}'` hands you the IPv4. Keyed on `10.0.1.13` the documented recipe matched **0 of 147** `gepetel` 5xx and declared every one client-visible; keyed on `fddf:6e69:23a7::d`, 121 were internal and **zero** were client-visible after 11:10. The repo already knew this box is as much `fddf:6e69:23a7::/48` as `10.0.1.0/24` — rag's SSRF guard checks both families — so one half of the codebase had it right and the audit recipe did not. Recipe corrected 2026-08-23 |
| The bare trailing `?` is the redeploy-proof discriminator, so a nightly audit can key phantom-vs-real 5xx on it | It is **one-directional**, and two apps here trip that every night: `bt-gateway` (`/api/v1/orders?statuses=OPEN`) and `gepetel` (its `/cron/` calls) route with query strings, so their forwards keep that query instead of a bare `?` and read as client legs. On 2026-08-26 that mis-binned **124 forwards** and reported **71** client-visible 5xx; the truth was **5**, all `bt-gateway` relaying its third-party upstream's `HTTP 400` (§9c). Key on the **client** leg instead — `ClientHost == fddf:6e69:23a7::1`, the `coolify` network's gateway, which is a network property (`docker network inspect`) and so drifts with nothing. Two independent partitions agreed on all 369 client rows. The `?` confirms a leg is internal; it never proves one is not |
| An app with two hourly crons showing 5–8 `/cron/` hits per hour in the proxy log means the dispatcher is over-firing | It fires **exactly twice**. `/var/log/paas-cron.log` held 24 lines for 12h × 2 crons, all `200`, and exactly 24 proxy lines carried `ClientHost 10.0.1.1` (the host, i.e. the dispatcher calling `127.0.0.1`). The surplus is the wake handshake plus the static host's second hop — a fronted app logs every request at least twice (§11). Count fires from the dispatcher's own log, never by grepping the proxy log for the path |
| 54 `WRN`/`ERR` lines in the Sablier log — `Group not found`, `No such container: <app>` — mean Sablier is broken or an enrolment has drifted | All 54 fell inside **ten minutes**, and none since. They are what a **failing first deploy** looks like from Sablier: the labels exist, so it is asked to start a group whose container Coolify keeps rolling back. Confirmed against `application_deployment_queues` — rows 390/393/395/397 all `failed` across exactly that window, quiet from the moment row 398 finished. Bound the timestamps before reading a Sablier error burst as live |
| `wake <id>: still failing … passing the backend's 502 through` is a wake failure | It is usually the opposite: the backend was **already up** and answering `502` on its own account. `_proxy` cannot tell that from a sleeping app, so it guesses and retries on the short budget. **Read the `sablier` stage** — ~0.02s means nothing was started (a real cold start is 0.77–1.78s here). On 2026-08-18 the two such warnings were `bt-gateway` relaying its third-party upstream's failure, `200` on the same path 3s later (§9c) |
| A second container on `ghcr.io/bogdanripa/paas-api:sha-<x>`, `Up Less than a second (health: starting)` — a duplicate deploy, or the control plane crash-looping | It is **`host_run_script` itself**. The nsenter helper defaults to paas-api's own image (§5b), so every call you make appears in the `docker ps` that call is running — named `pironman-host-<12 hex>`, a fresh name each time, `--rm --privileged --pid=host`, no labels. Verified 2026-08-24 two ways: the name changed between consecutive calls, and `app/hostexec.py:64` mints it. The real api is the `<uuid>-<timestamp>` running `uvicorn`. A duplicate check keyed on **image** is fooled; one keyed on the uuid prefix is not |
| `md5sum /usr/local/bin/paas-watchdog` equals `/opt/paas/paas-watchdog`, so the host scripts are current | It only means the copy step was run since the last `git pull` **of that clone** — and nobody has to pull it. On 2026-08-24 `/opt/paas` sat at `ef5c5dc` while `origin/main` was at `3f5648a`; equal md5s would have looked identically clean had a script changed in between. Diff the installed scripts against a fresh checkout of `main` instead. Also: root cannot run *any* git there — `admin:admin`, no `safe.directory`, so even `git -C /opt/paas log` dies on "dubious ownership"; use `su -s /bin/sh -c '…' admin` |
| `docker inspect web` gives the static host's `ClientHost`, so the phantom-5xx reconciliation can key the whole day on it | It gives the address `web` holds **since its last redeploy**, and a redeploy inside the window leaves the older one all over the log. `web` was recreated 2026-08-25 at `13:24:05`; `fddf:6e69:23a7::d` forwards ran to `13:24:08`, `::f` began at `13:24:15`, and **215 of that day's 272** `gepetel` forwards carried the address `docker inspect` no longer reports. Keyed on `::f` alone the recipe called 128 of 132 5xx client-visible; only **1** was. Read `.State.StartedAt` first — inside the window, resolve the older address from the log. The redeploy-proof discriminator is a **bare trailing `?`** (243/243 forwards, 0/143 client and dispatcher legs), but one-directional: a forward of a request that already had a query string keeps that query, so absence of the `?` proves nothing (§11) |
| The Pironman connector failing to connect (`502`) is a connector-side problem, so a scheduled audit simply cannot run and there is nothing to report | It can equally mean **the box is gone**, and one `curl` tells the two apart. On 2026-08-28 the connector 502'd; direct HTTPS from the session to `api-coolify`, `web-coolify`, `gepetel-coolify`, `bt-gateway-coolify` **and the bare `bogdanripa.com` apex** all returned `HTTP 530` / `error code: 1033` — Cloudflare's origin-unreachable error — while DNS still resolved to Cloudflare IPs. A non-app host in the zone is the discriminator: an app-level fault cannot take it down, so identical `1033` across it and the app hosts is box- or origin-link-wide, not one container. Treating the 502 as mere tooling noise would have filed a total outage as a quiet non-run. It stayed down **three consecutive nights** (2026-08-28/29/30), same signature each time plus `server: cloudflare` + a `cf-ray` on each `530` (so Cloudflare's edge is answering, not something synthesising the error) and `530` on plain **HTTP :80** too, so it is not TLS. **Resolved 2026-08-31**: the owner reports a power outage, which the box corroborates — `dmesg` shows `EXT4-fs orphan cleanup` on the recovery boot. A box that is gone does not come back on its own: re-report it each night rather than treating it as already filed, and expect unclean-shutdown damage on the way back (ARCHITECTURE §12, zero-length files) |
| The bare `bogdanripa.com` apex is always the host to test, and a `502` reaching it is the same `502` as the connector's | The session's own **egress proxy** refused exactly that host — `curl: (56) CONNECT tunnel failed, response 502`, no Cloudflare headers, `remote_ip 127.0.0.1` — while tunnelling `www.bogdanripa.com` fine seconds later on 2026-08-29. So an unreachable apex proves nothing, and its `502` is not Cloudflare's `530`. Use **`www`** (same zone, not a platform app) as the stand-in, and make the second route a **positive control**: `www.cloudflare.com` and `api.github.com` both returned `200` through that same egress, which is what turns "all our hosts are `1033`" into "the failure is this origin, not my network" |
| `paas-watchdog --dry-run` saying `ok — all always-on apps running` covers the always-on containers the audit lists (§1e: api, web, coolify-proxy, sablier, postgres, coolify itself) | It covers **two** of them. `ALWAYS_ON` is `{API_APP, STATIC_HOST_APP}` = `api` and `web`, and the `unhealthy` branch only runs inside the loop over `apps` rows — so every Coolify container and the Sablier controller, none of which have an `apps` row, are invisible to it. On 2026-08-31 `coolify` was `unhealthy` with `FailingStreak 74` and the watchdog still printed `ok`; `platform_tasks_health` was clean too, because it watches the api's own loops rather than other containers. Neither was lying — nothing on this box alarms on infrastructure containers, so the nightly audit's own `docker ps` is the only thing that checks them |
| The audit alerts by Telegram, so a run that finds a fault can report it | Only for faults that leave the box up. Both credentials live **only** in the `api` container's environment — the audit's own recipe `docker inspect`s them out of it, and every consumer sources them the same way (`app/config.py`, `app/notify.py`, `paas-watchdog:118`); nothing off-box holds them. So the alert path runs *through* the thing being monitored, and fails exactly when the fault is total: on 2026-08-30 the box was unreachable, `host_run_script` was gone with it, and there was no way to send the Telegram the routine's §3 mandates. Verified two ways — no `TELEGRAM_*` in the session environment, and every code path reading them from that container. The channel that survives is the routine's own notification (`PushNotification`), which does not touch the box: send the outage there and do not treat an unsendable Telegram as a reason to stay quiet |
| `curl -sS -o out -w '%{http_code}'` in a loop reports what each host returned | Only for transfers that **happened**. A failed one writes nothing, so a reused `-o` filename leaves the **previous** host's body in place and `head`ing it attributes that body to the host that failed. On 2026-08-29 the apex printed `HTTP 000` beside a stale `error code: 1033` and read as a fifth confirmation when curl had in fact never connected. Read `%{http_code}` first — `000` means no answer, and any body beside it is a leftover |
| The proxy writes a JSON log, so `docker logs coolify-proxy \| grep '"level":"error"'` counts its errors | It counts **zero**, always. `--accesslog.format=json` sets the *access* log's format; there is no `--log.format=json` in the proxy's `Args`, so Traefik's own lines are ANSI-coloured plain text — `\x1b[31mERR\x1b[0m`. On 2026-08-31 that grep returned `0` for a window holding 8 real `ERR` lines, which were the whole explanation for the day's 41 client-visible 5xx (Traefik came up without the Docker socket, the Sablier plugin never loaded, and every `sablier-<app>` middleware was invalid — ARCHITECTURE §12). Strip ANSI, match ` ERR `/` WRN `, and skip lines containing `"StartUTC"`. Note both halves of that check were wrong at once: the pattern missed the format *and* the input was a truncated `--since` read |
| `MAX_LINES = 2000` is generous for the analytics ingest — `_read_since`'s own docstring says so ("it needs to exceed the traffic between two passes, and the whole log is currently 2333 lines") | The second clause measures the wrong thing and the first is no longer true. What matters is **arrivals between passes**, and on 2026-08-25T02:03:24Z one 120s window carried **3,654** lines — 1.8× the ceiling — from a single scanner (3,652 hits from `87.120.104.29`, forged Googlebot UA, against the bare IP `5.12.126.43`). A `--tail 2000` read of a 3,654-line batch silently drops the oldest 1,654, which are *newer than the cursor*, and the cursor then advances past them. Nothing catches it: every truncation diagnostic sits in the `else` of `if seen:` and so runs only when a pass counted **nothing**, while an overrun pass counts plenty and logs `counted N lines` at INFO. Whether app data was actually lost is **unverified** — those lines belong to no hosted app, and `_internal_leg`'s partial fallback rules out an exact rollup-vs-log reconciliation. Busiest 120s on 2026-08-31 was 1,041, so there is headroom today. **Raised to 5000 on 2026-09-01** — but `--tail` has a ceiling too, and it is the same gap-seeking bug as the row below: 4000/5000/6000 each returned exactly what was asked, while `--tail 7000` returned **4001** lines (fewer than 6000 did, and the newest 4000 differ by md5) because 7000 is where the read first reaches past the 73.4h outage gap. That cliff sits at "lines available since the most recent gap", so it moves — re-measure the ladder before raising it again |
| `docker logs --since 24h coolify-proxy` returns the last 24h of the access log, so a nightly count can be keyed on it | Not after an outage. On 2026-08-31 it returned **899** JSON lines whose earliest was *exactly* the current log file's first line (`11:28:35.875Z`), silently dropping the **2,990** in-window lines that sat in `-json.log.1` — 77% of the day, and precisely the part containing the recovery. Keyed on it, the §11 client-leg partition reported **0** client-visible 5xx; reading the rotated files directly over the same window it was **41**. `--since 24h`, `48h` and `72h` all returned the identical 899, while `--since 168h` returned 14,178 and a bare `docker logs` 16,216 — so it is not a general `--since` failure and rotation-crossing works normally (a `--since` before the gap read `.log.1` fine). Every broken value falls at or after the log's 73.4h outage gap; the mechanism is **unproven**. Two further traps in the same command: a bare `--since` timestamp is read in the host's **local** time (UTC+3 here — `--since 2026-08-26T00:00:00` first returned `2026-08-25T21:00:01Z`), and the count is silent about what it dropped. Count from the container's `/var/lib/docker/containers/<id>/*-json.log*` files instead, filtering on the inner `StartUTC` |

The pattern is the same every time: the code said what *should* be true, the box
said what *was* true, and they differed. Three of these produced confident wrong
answers to the user before evidence corrected them. Two shipped in commit
messages that had to be rewritten.

### How to check, per kind of claim

- **"This app is/isn't running"** — `apps_stats`, or `docker ps` on the host. Not
  the `apps` table, which records intent.
- **"This flag is set"** — read the row (`host_run_script` → `psql`). Defaults in
  `config.py` describe a fresh box, not this one.
- **"The deployed code does X"** — check the running image tag against git.
  `/opt/paas` is a clone and may be ahead of, behind, or unrelated to what is
  deployed; `paas-api` runs from a **built image**, not from that directory.
- **"This label/route/enrolment exists"** — `docker inspect` the container.
  Coolify regenerates labels, so the database's belief is a claim, not a fact.
- **"Nothing in history has this string"** — `git fetch` first. Someone else may
  be working in the same repo; `origin/main` moves under you.
- **"This code path behaves like so"** — run it. Every fix in this session that
  was tested before shipping held up; the ones reasoned about did not.
- **"The connector is down"** — `curl` the hostnames from the session before
  concluding anything. `502` from the connector alone is ambiguous; add the app
  hosts *and* a non-app host in the zone (`www`; the bare apex is not reliably
  reachable from a session — see the table). All `530`/`1033` **with a `cf-ray`
  header** = the origin is unreachable (a real outage, report it); the non-app
  host answering while the connector 502s = the connector path, not the box.
  Prove it is not your own egress with a positive control in the same run
  (`www.cloudflare.com`, `api.github.com`), and read `%{http_code}` — a `502`
  from the egress proxy or a `000` is not Cloudflare's `530`.
- **"N requests / N errors happened in the last 24h"** — count from the
  container's json-file logs on disk
  (`/var/lib/docker/containers/<id>/*-json.log*`), filtering on the inner
  `StartUTC`. Never from `docker logs --since` (see the table): it can return a
  clean, plausible, *silently truncated* subset, and a truncated log reads as a
  quiet one. Sanity-check any such count against a second route — `analytics_perf`
  for today, or the dispatcher's own `/var/log/paas-cron.log` — and treat a
  suspiciously round agreement with the current file's first line as the tell.

### Say what you actually verified

When reporting, separate what was observed from what was inferred, and name the
gap. "I could not check X, so this rests on Y" is a useful answer. A confident
answer that turns out to rest on an unchecked default is not — it costs more to
undo than the check would have cost.

If a claim already given to the user turns out to be wrong, correct it plainly
and fix any commit message that carries it. Do not let a wrong explanation sit in
the permanent record because the code happens to be right.

## Close your own loops — set a timer, do not ask for a ping

A change to `paas-api` is not finished when it is pushed. It has to build
(emulated arm64, minutes) and redeploy before anything on the box changes, so
"it should work now" is a prediction, not a result — and predictions made at
this point in the loop have been wrong repeatedly here.

**So schedule the verification instead of delegating it.** `send_later` puts a
message back into this session; use it after any push that has to reach the box,
and again if the build has not landed yet. Keep going until the thing is
confirmed working or genuinely blocked. Never end a turn with "ping me when it
deploys" — the user should not have to carry the loop.

The check itself is the same discipline as everything else: read the running
image tag first, because a stale image explains every other symptom and
explaining a symptom that has not changed yet wastes the round.

## Where to push — standing permission

**Push to `main` directly. This is the repo owner's standing, explicit
permission, and it applies to every session and every Routine here.**

It is written down because a cloud session is handed an auto-generated working
branch (`claude/<something>`) together with a rule against pushing anywhere else
*without explicit permission*. There is no setting that changes that branch name;
this paragraph is the permission the rule asks for. So do not open a feature
branch for a fix or a docs update, and do not leave work parked on one waiting to
be merged — a branch nobody merges is how a verified fact fails to reach the next
run, which is the whole point of §"Write down what you learned".

**The stop hook will call that push unpushed. It is wrong.** It compares the
local branch against a remote of the *same name*, and `claude/<something>` has
none, so `git push origin HEAD:main` trips it every time even though the commit
is on `origin/main`. Confirm with `git log origin/main..HEAD` (empty) and
`git ls-remote --heads origin <branch>` (empty), then ignore it — pushing the
branch to silence the hook creates precisely the stray nobody merges.

Unchanged by this: never force-push, never rewrite published history, and
anything on a routine's MUST-NOT list still needs asking. Permission to choose
the branch is not permission to skip the verification that earns the commit.

## Running unattended (Routines)

A scheduled run has nobody to answer a question, so anything that would pause is
a stall, not a delay. Three rules follow.

**The annotation set is the autonomy policy.** The connector's approval gate
keys on `ToolAnnotations` in `app/main.py`: a `destructiveHint=True` tool prompts
before every call and blocks a Routine outright. So moving a tool into
`_DESTRUCTIVE` silently removes it from every scheduled run, and `_READONLY` must
list every GET-backed tool — derive that from the route methods, never from what
a name suggests. `host_run_script` and `db_run_script` are deliberately excluded
from `_DESTRUCTIVE` so audits can run; README "Running a routine unattended" has
the tradeoff.

**Which means the prompt is the only guardrail left.** A root shell and arbitrary
SQL now execute unprompted in a scheduled run. Nothing downstream will stop a
destructive call, so a routine's MUST-NOT-without-asking list has to be written
as if it is the last line of defence, because it is. When editing one, assume no
human reads the output before the commands run.

**Silence is a claim, not a result.** A routine that reports by exception is
silent when healthy and equally silent when it never started, was denied, or died
on its first call. Before treating "no alert" as an all-clear, confirm the run
actually executed. The 2026-08-03 audit lost five and a half hours to exactly
this and would have lost the whole night unnoticed.

## Facts about this box that are easy to get wrong

- **DNS is a wildcard.** `*.bogdanripa.com` via Cloudflare, so there is never a
  per-app DNS record to create, and never one to clean up after a deletion.
  ARCHITECTURE §4 already said this; suggesting a DNS tidy-up after removing an
  app contradicted the repo's own docs.
- **Every app this platform creates is `<app-id>-coolify.bogdanripa.com`** —
  the `-coolify` infix is not optional and is not derived from anything. So a
  container answering on a *bare* host like `whoami.bogdanripa.com` was not
  created here, whatever else it looks like. That is a reliable first
  discriminator when deciding whether something is ours.
- **Coolify-managed is not the same as platform-managed.** An app can be in
  Coolify (`coolify.managed=true`, an `applications` row) and absent from the
  `apps` table. Removing its container with `docker rm` leaves Coolify owning a
  resource whose container is gone; the removal belongs in Coolify, or via
  `apps_adopt` + `apps_delete`. Check both registries before concluding
  something is stray.

## Destructive actions

`host_run_script` is root on the machine the whole platform runs on, including
this API. There are no guardrails. Read any writing, removing, installing or
restarting script back to the user and get agreement first. Read-only commands
need no ceremony — run them freely, and run them often enough that guessing is
never the cheaper option.

## The design rule everything else follows from

**Never let a failure be answered with something that looks like success.** It is
the theme of every hard bug here (ARCHITECTURE §12) and it applies to diagnosis
too: an unverified answer that sounds right is the same failure mode, one level
up.
