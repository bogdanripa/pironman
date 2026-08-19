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
    through their 5xx rate, which catches a failure to wake; and
  - 5xx alerts fire on the *increase* in the day's 5xx count since the last check,
    thresholded, rather than on an absolute number.

Unconfigured (no Telegram token/chat), check_once returns immediately.
"""
import re
from datetime import date, datetime, timezone

from . import notify, stats
from .config import ALERT_5XX_THRESHOLD, app_url, sablier_session_seconds
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

            if prev is not None and not sleeps:
                if not running and fail_count == DOWN_AFTER and not alerted_down:
                    messages.append(f"🔴 <b>{aid}</b> is down\n{app_url(aid)}")
                    alerted_down = True
                elif running and alerted_down:
                    messages.append(f"🟢 <b>{aid}</b> recovered\n{app_url(aid)}")
                    alerted_down = False
            elif sleeps and alerted_down:
                # It was alerted down before being enrolled; clear that state
                # rather than leaving a stale "down" flag that suppresses a later
                # genuine recovery message.
                alerted_down = False

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
            cname = next((n for n in lifecycle if uuid and uuid in n), None)
            awake_since = _docker_time((lifecycle.get(cname) or {}).get("since")
                                       if cname else None)
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
                " err_day, err_server, updated_at) "
                "VALUES ($1, $2, $3, $4, CURRENT_DATE, $5, now()) "
                "ON CONFLICT (app_id) DO UPDATE SET "
                "fail_count = EXCLUDED.fail_count, "
                "alerted_down = EXCLUDED.alerted_down, "
                "alerted_stuck = EXCLUDED.alerted_stuck, "
                "err_day = EXCLUDED.err_day, err_server = EXCLUDED.err_server, "
                "updated_at = now()",
                aid, fail_count, alerted_down, alerted_stuck, err)

    return {"apps_checked": len(apps), "alerts_sent": sent}
