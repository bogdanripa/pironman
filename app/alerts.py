"""Down / server-error alerting over Telegram.

A background loop (started in main.lifespan) checks every app on a short interval
and messages Telegram when something changes state: an app that was running goes
down, a downed app recovers, or an app starts throwing new 5xx responses. State
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
from datetime import date

from . import notify, stats
from .config import ALERT_5XX_THRESHOLD, app_url
from .db import pool

DOWN_AFTER = 2  # consecutive missed checks before an app counts as down


async def check_once() -> dict:
    if not notify.configured():
        return {"skipped": "telegram not configured"}

    async with pool().acquire() as conn:
        # coolify_uuid IS NOT NULL: frontend-only apps have no container of their
        # own, so "not running" is their normal state — alerting on it would fire
        # forever. Their traffic is served by the shared static host, which is
        # itself an app and is monitored like any other.
        apps = await conn.fetch(
            "SELECT id, coolify_uuid, sleep_when_idle FROM apps "
            "WHERE coolify_uuid IS NOT NULL ORDER BY id")
        perf = await conn.fetch(
            "SELECT app_id, err_server FROM analytics_perf WHERE day = CURRENT_DATE")
        err_today = {r["app_id"]: r["err_server"] for r in perf}
        state = {r["app_id"]: dict(r) for r in
                 await conn.fetch("SELECT * FROM alert_state")}

        running_names = list((await stats._container_stats()).keys())
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

                # 5xx: baseline is the prior count only within the same day (the
                # counter resets each day, so a day rollover baselines from 0).
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
                "(app_id, fail_count, alerted_down, err_day, err_server, updated_at) "
                "VALUES ($1, $2, $3, CURRENT_DATE, $4, now()) "
                "ON CONFLICT (app_id) DO UPDATE SET "
                "fail_count = EXCLUDED.fail_count, "
                "alerted_down = EXCLUDED.alerted_down, "
                "err_day = EXCLUDED.err_day, err_server = EXCLUDED.err_server, "
                "updated_at = now()",
                aid, fail_count, alerted_down, err)

    return {"apps_checked": len(apps), "alerts_sent": sent}
