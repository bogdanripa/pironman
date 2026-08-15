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
| `api` is covered by "apps that must not sleep" | Its row says `sleep_when_idle = true` |
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
| README documents the five `--accesslog` flags as one-time setup, so the proxy has them | It has **four**. `X-Pironman-Backend=keep` was documented on 2026-08-05 and was still not applied on 2026-08-15, ten days later — documenting a host step does not perform it. Every fronted sleeping app therefore still carries one phantom 5xx per wake |
| A container's IP is the one `docker inspect` prints, so filtering the proxy log on it finds that container's own requests | The `coolify` network is **dual-stack**, and `web` talks to the proxy over **IPv6**. `{{.NetworkSettings.Networks}}.IPAddress` returns only `10.0.1.14`; every line `web` actually produced logged `ClientHost` `fddf:6e69:23a7::e`. Filtering on the v4 matched **nothing**, which does not degrade the answer, it inverts it: all 14 phantom 5xx reclassified as client-visible and a healthy night read as an incident. Take `.GlobalIPv6Address` too (ARCHITECTURE §12) |

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
