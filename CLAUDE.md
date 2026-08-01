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
