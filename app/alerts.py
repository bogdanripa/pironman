"""Down / server-error alerting over Telegram.

A background loop (started in main.lifespan) checks every app on a short interval
and messages Telegram when something changes state: an app that was running goes
down, a downed app recovers, an app starts throwing new 5xx responses, or a
scale-to-zero app stays awake with nothing asking for it. State
lives in the alert_state table so only edges alert, not every tick.

Three bits of noise control:
  - a consecutive-failure debounce (DOWN_AFTER), so a rolling redeploy — where the
    container is briefly absent while Coolify swaps it — is not flagged as an
    outage;
  - apps enrolled in scale-to-zero are never reported down for having no
    container: being stopped while idle is the feature working. They stay covered
    two ways — their 5xx rate, which catches a failure to wake, and a check that
    the container EXISTS at all, because a deleted one can never wake and no
    amount of waiting fixes it; and
  - 5xx alerts fire on the *increase* in the day's 5xx count since the last check,
    thresholded, rather than on an absolute number.

Everything above reads the platform's own view of itself, and twice that view
was entirely green while the apps were unreachable. So two checks in probe.py
ask the user's question instead — does a request to the public URL come back —
and their results are alerted here: Traefik's router table every tick, and a
synthetic fetch once a day per app, in an hour derived from the app's id.

Unconfigured (no Telegram token/chat), check_once returns immediately.
"""
import re
from datetime import date, datetime, timezone

from . import autoupdate, notify, probe, stats
from .config import (ALERT_5XX_THRESHOLD, TRAEFIK_API_URL, app_url,
                      sablier_session_seconds)
from .db import pool

DOWN_AFTER = 2  # consecutive missed checks before an app counts as down

# How long a scale-to-zero app may sit awake with no traffic before that is a
# fault rather than a lull. Six sessions, floor 30 minutes: analytics ingests
# every 120s and this loop runs every 150s, so last_seen trails real traffic by
# a few minutes and the margin has to swallow that without crying wolf. Costs
# nothing in sensitivity — the case this was written for sat awake 21 hours.
STUCK_AWAKE_AFTER = max(6 * sablier_session_seconds(), 1800)


def _docker_time(raw: str | None) -> datetime | None:
    """Docker's RFC3339 timestamp -> datetime, or None.

    Docker writes nine fractional digits; fromisoformat accepts three or six and
    nothing else, so the tail is trimmed rather than parsed. A value that will
    not parse returns None, which the caller treats as "cannot tell" and stays
    quiet — the wrong direction to guess in is the one that pages someone.
    """
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    m = re.match(r"(.*\.\d{6})\d*([+-]\d\d:\d\d)$", text)
    if m:
        text = m.group(1) + m.group(2)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def missing_container_decision(
    aid: str, exists: bool, prev: dict | None, sleeps: bool,
) -> tuple[list[str], int, bool]:
    """Should a scale-to-zero app's ABSENT container alert? -> (messages, count, latch)

    Pure so it can be tested without Docker or a database: check_once supplies
    `exists` from the container lifecycle and `prev` from alert_state.

    Only for sleeping apps. A non-sleeping app with no container already reads as
    "not running" and alerts as down; saying it twice trains people to skim.

    This exists because the 5xx cover the scale-to-zero exemption relies on is
    traffic-dependent, and a low-traffic app produces no traffic to go wrong.
    smartbill-mcp sat with no container for ten hours across ten identical failed
    repairs: no requests, so no 5xx, so no alert, and nothing said a word until
    someone tried to use it. Absence is true whether or not anyone is looking.
    """
    missing_count = 0 if exists else ((prev.get("missing_count", 0) if prev else 0) + 1)
    alerted_missing = bool(prev.get("alerted_missing")) if prev else False
    messages: list[str] = []

    if sleeps and prev is not None:
        if not exists and missing_count >= DOWN_AFTER and not alerted_missing:
            messages.append(
                f"\U0001f6a8 <b>{aid}</b> has NO container — it is not asleep, "
                f"it cannot wake, and every request to it fails. Needs a "
                f"redeploy.\n{app_url(aid)}")
            alerted_missing = True
        elif exists and alerted_missing:
            messages.append(
                f"\U0001f7e2 <b>{aid}</b> has a container again\n{app_url(aid)}")
            alerted_missing = False
    elif not sleeps and alerted_missing:
        # Stops a stale flag muting a later genuine recovery, the same way
        # alerted_down is cleared when an app becomes sleeping.
        alerted_missing = False

    return messages, missing_count, alerted_missing


async def check_once() -> dict:
    if not notify.configured():
        return {"skipped": "telegram not configured"}

    async with pool().acquire() as conn:
        # coolify_uuid IS NOT NULL: frontend-only apps have no container of their
        # own, so "not running" is their normal state — alerting on it would fire
        # forever. Their traffic is served by the shared static host, which is
        # itself an app and is monitored like any other.
        apps = await conn.fetch(
            "SELECT id, coolify_uuid, sleep_when_idle, sablier_enrolled FROM apps "
            "WHERE coolify_uuid IS NOT NULL ORDER BY id")
        perf = await conn.fetch(
            "SELECT app_id, err_server FROM analytics_perf WHERE day = CURRENT_DATE")
        err_today = {r["app_id"]: r["err_server"] for r in perf}
        state = {r["app_id"]: dict(r) for r in
                 await conn.fetch("SELECT * FROM alert_state")}
        # Edge traffic per app. Holds rows for deleted apps too, so it is read
        # as a lookup against the apps list rather than iterated.
        last_seen = {r["app_id"]: r["last_seen"] for r in await conn.fetch(
            "SELECT app_id, last_seen FROM analytics_last_seen")}
        now = datetime.now(timezone.utc)
        messages_global: list[str] = []

        # Traefik's own account of which routers it will serve. None means the
        # API could not be read — reported as "cannot tell" rather than silently
        # passing, because a check that quietly stops checking is how both of
        # the outages this guards against lasted as long as they did.
        routers = await probe.fetch_routers(TRAEFIK_API_URL) if TRAEFIK_API_URL else None
        broken_routers = probe.router_problems(routers) if routers is not None else {}
        prev_traefik = state.get("__traefik__") or {}
        traefik_alerted = bool(prev_traefik.get("alerted_unreachable"))
        if broken_routers and not traefik_alerted:
            # One message for the lot. These fail in groups — a plugin that did
            # not load takes out every router using it — so a line per app would
            # be a dozen notifications for a single fault.
            detail = "\n".join(f"• {n}: {why}"
                                for n, why in sorted(broken_routers.items())[:8])
            more = len(broken_routers) - 8
            if more > 0:
                detail += f"\n• …and {more} more"
            messages_global.append(
                f"\U0001f6a8 Traefik has {len(broken_routers)} router(s) it will "
                f"not serve — the apps behind them are unreachable however "
                f"healthy their containers look:\n{detail}")
            traefik_alerted = True
        elif routers is not None and not broken_routers and traefik_alerted:
            messages_global.append("\U0001f7e2 Traefik is serving every router again")
            traefik_alerted = False

        # Skip the day's probes only when Traefik is KNOWN to be broken — the
        # answer is a foregone conclusion and waking every app to confirm it
        # turns one fault into a dozen notifications. An unreadable API is "do
        # not know", which must NOT suppress probing: the probe is the check
        # that does not depend on anything internal being readable, and making
        # it conditional on one would recreate the blind spot it exists to fill.
        traefik_known_broken = bool(broken_routers)

        # The Sablier plugin, and healing it. Checked directly rather than
        # through the router API, because the API needs a proxy config change
        # that Coolify reverts — and because an empty plugin source directory is
        # a fact, with no log wording or API shape to track.
        plugin = await probe.plugin_loaded(autoupdate._docker)
        if plugin is False:
            traefik_known_broken = True          # every sablier router is dead
            last_heal = prev_traefik.get("healed_at")
            if probe.should_heal_plugin(plugin, now.timestamp(), last_heal):
                code, _ = await autoupdate._docker("restart", "coolify-proxy", timeout=90)
                healed = code == 0
                messages_global.append(
                    f"\U0001f6a8 Traefik started without its Sablier plugin, so "
                    f"every scale-to-zero app was unreachable. "
                    + ("Restarted the proxy to reload it — verifying on the next "
                       "check." if healed else
                       "The automatic restart FAILED and this needs hands."))
                heal_ts = now.timestamp()
            else:
                heal_ts = last_heal
        else:
            heal_ts = None
            if plugin is None:
                # Cannot tell. Reported, never assumed well.
                pass


        running_names = list((await stats._container_stats()).keys())
        # state + since (StartedAt while running) for every container, needed to
        # tell "up for hours with no traffic" from "just woken".
        lifecycle = await stats._lifecycle()
        sent = 0

        for app in apps:
            aid = app["id"]
            uuid = app["coolify_uuid"] or ""
            running = any(uuid and uuid in n for n in running_names)
            err = int(err_today.get(aid, 0))
            prev = state.get(aid)

            # An app enrolled in scale-to-zero is SUPPOSED to be stopped when idle,
            # so a missing container is not an outage — alerting on it would page
            # every time Sablier did its job, and that noise is how people learn to
            # ignore alerts. Such an app is still covered: if it fails to wake or
            # crashes on start, requests to it produce 5xx, which alerts below.
            sleeps = bool(app["sleep_when_idle"])
            fail_count = 0 if (running or sleeps) else (
                (prev["fail_count"] if prev else 0) + 1)
            alerted_down = bool(prev["alerted_down"]) if prev else False
            alerted_stuck = bool(prev.get("alerted_stuck")) if prev else False
            messages = []

            # lifecycle covers stopped containers too, which is the whole
            # point: "stopped" and "deleted" look identical to every other check
            # here and could not be further apart. Sablier discovers an instance
            # through a label on the container, so a deleted one has no group to
            # wake and answers 5xx for ever until a deploy recreates it.
            container = next((n for n in lifecycle if uuid and uuid in n), None)
            missing_msgs, missing_count, alerted_missing = missing_container_decision(
                aid, container is not None, prev, sleeps)
            messages.extend(missing_msgs)

            # The inverse of the exemption above. A scale-to-zero app whose
            # container is up long after anything last asked for it is not
            # sleeping when it should be. Sablier drops an instance's expiry
            # timer occasionally — 2 of 69 dispatches over one 48h window — and
            # the container then sits awake until some later request happens to
            # re-arm it. Nothing else reports this, because nothing else is
            # wrong: apps_stats says "running" and the task watchdog says ok,
            # and both are telling the truth. bt-gateway sat awake 21 hours.
            #
            # Needs sablier_enrolled, not just sleep_when_idle: an app that
            # wants to sleep but was never enrolled has no expiry to lose, and
            # flagging it here would report a known configuration gap as a
            # recurring fault. A NULL last_seen (never any traffic) is left
            # alone — there is no age to measure, and the alternative is
            # alerting on every freshly created app.
            # Measured from the LATER of the last request and the container's
            # own start. Measuring from last_seen alone fired on every wake: a
            # just-woken app has an ancient last_seen by definition, and the
            # ingest that would refresh it runs up to two minutes behind, so
            # bt-gateway and revolut-mcp were both paged within a minute of
            # waking at 2026-08-19T06:06:26 after a legitimate 21.7h asleep. The
            # margin could never have covered that: the staleness is the whole
            # sleep, not the lag. A container up for three minutes cannot have
            # been stuck awake for hours, whatever last_seen says.
            seen = last_seen.get(aid)
            awake_since = _docker_time((lifecycle.get(container) or {}).get("since")
                                       if container else None)
            base = max([t for t in (seen, awake_since) if t is not None],
                       default=None)
            idle = (now - base).total_seconds() if base else None
            stuck = bool(sleeps and running and app["sablier_enrolled"]
                         and seen is not None and awake_since is not None
                         and idle is not None and idle > STUCK_AWAKE_AFTER)
            if stuck and not alerted_stuck:
                messages.append(
                    f"\U0001f634 <b>{aid}</b> is awake but has served nothing "
                    f"for {idle / 3600:.1f}h — scale-to-zero is not "
                    f"happening\n{app_url(aid)}")
                alerted_stuck = True
            elif alerted_stuck and not stuck:
                # Cleared without a message. This fault self-corrects the moment
                # any request re-arms the timer, so a recovery note would fire
                # about as often as the alert and train the reader to skim past
                # both.
                alerted_stuck = False

            # Once a day, in this app's own hour, actually fetch its public
            # URL. The only cause-independent check here: it cannot say why, only
            # that a real request did not come back — which is precisely what
            # both outages did, while every internal signal stayed green. Skipped
            # when Traefik is already known to be broken, because the answer is
            # a foregone conclusion and waking every app to confirm it would turn
            # one fault into a dozen notifications.
            probed_on = prev.get("probed_on") if prev else None
            alerted_unreachable = bool(prev.get("alerted_unreachable")) if prev else False
            if (prev is not None and not traefik_known_broken
                    and probe.due_for_probe(aid, now, probed_on)):
                serving, status = await probe.probe(app_url(aid))
                probe_msgs, alerted_unreachable = probe.reachability_decision(
                    aid, serving, status, app_url(aid), prev)
                messages.extend(probe_msgs)
                probed_on = now.date()

            # 5xx: baseline is the prior count only within the same day (the
            # counter resets each day, so a day rollover baselines from 0). This
            # runs for EVERY app, and must: it is the only cover a sleeping app
            # has — it is never reported down, so if it fails to wake or crashes
            # on start, this is what says so. It spent its whole life nested in
            # the branch above, which meant it fired only for an app that both
            # sleeps and had previously been alerted down: in practice, never.
            if prev is not None:
                baseline = prev["err_server"] if prev["err_day"] == date.today() else 0
                delta = err - baseline
                if delta >= ALERT_5XX_THRESHOLD:
                    messages.append(
                        f"⚠️ <b>{aid}</b> returned {delta} new 5xx "
                        f"response(s)\n{app_url(aid)}")

            for m in messages:
                if await notify.send(m):
                    sent += 1

            await conn.execute(
                "INSERT INTO alert_state "
                "(app_id, fail_count, alerted_down, alerted_stuck, "
                " missing_count, alerted_missing, probed_on, alerted_unreachable, "
                " err_day, err_server, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, CURRENT_DATE, $9, now()) "
                "ON CONFLICT (app_id) DO UPDATE SET "
                "fail_count = EXCLUDED.fail_count, "
                "alerted_down = EXCLUDED.alerted_down, "
                "alerted_stuck = EXCLUDED.alerted_stuck, "
                "missing_count = EXCLUDED.missing_count, "
                "alerted_missing = EXCLUDED.alerted_missing, "
                "probed_on = EXCLUDED.probed_on, "
                "alerted_unreachable = EXCLUDED.alerted_unreachable, "
                "err_day = EXCLUDED.err_day, err_server = EXCLUDED.err_server, "
                "updated_at = now()",
                aid, fail_count, alerted_down, alerted_stuck,
                missing_count, alerted_missing, probed_on, alerted_unreachable, err)

        for m in messages_global:
            if await notify.send(m):
                sent += 1
        # The traefik latch rides in alert_state under a reserved key rather than
        # a table of its own: same latch semantics, same place to look.
        await conn.execute(
            "INSERT INTO alert_state (app_id, alerted_unreachable, healed_at, updated_at) "
            "VALUES ('__traefik__', $1, $2, now()) "
            "ON CONFLICT (app_id) DO UPDATE SET "
            "alerted_unreachable = EXCLUDED.alerted_unreachable, "
            "healed_at = EXCLUDED.healed_at, updated_at = now()",
            traefik_alerted, heal_ts)

    return {"apps_checked": len(apps), "alerts_sent": sent,
            # "cannot tell" is reported, never mistaken for health.
            "traefik": "unreadable" if routers is None else
                       (f"{len(broken_routers)} broken router(s)" if broken_routers else "ok"),
            "sablier_plugin": {True: "loaded", False: "MISSING",
                               None: "unknown"}[plugin]}
