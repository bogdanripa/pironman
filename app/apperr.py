"""Shared wording for the one mistake every container-only endpoint can hit:
the app has no container.

Two very different situations both leave `coolify_uuid` NULL, and they want
opposite fixes:

  - a genuinely **frontend-only** app (`has_frontend`, no image) never has a
    container, and the answer is to put the setting in the bundle or give the
    app a backend;
  - an app **registered but not yet deployed** — the shape every app has
    between `apps_create` and its first CI run — will have one shortly, and the
    answer is simply to deploy it once.

Telling someone the first when the truth is the second sends them looking for a
mistake they did not make. That happened: a backend app was created, and every
`apps_env_set` on it answered "this is a frontend-only app", which reads as
"you created it wrong" rather than "not yet". Deciding on `has_frontend` costs
one column and removes the ambiguity.

Note what this is NOT for. `apps_env_set` no longer raises it: telling someone
to "deploy it once, then retry" is a deadlock when the missing variable is the
reason the deploy fails, so setting a variable on a container-less app now
stages it instead (see routers/env.py). Reach for this only where the operation
genuinely needs a container to act on and no amount of deferring helps — sleep
enrollment has nothing to enroll.
"""


def no_container(app_id: str, row, what: str, fix: str = "") -> str:
    """Detail string for a 400 when `row` has no container.

    `what` completes "…it has no container, so <what>". `fix` is the extra
    sentence for the frontend-only case, where the remedy is app-specific.
    """
    if row["has_frontend"]:
        tail = f" {fix}" if fix else ""
        return (f"this is a frontend-only app: it has no container, so {what}. "
                f"Static files are served as they are.{tail}")
    return (f"{app_id} has no container yet: it is registered but has never "
            f"deployed, so {what}. Push to its deploy branch so CI builds and "
            "deploys it once, then retry — this is not a frontend-only app.")
