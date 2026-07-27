"""Minimal 5-field cron matcher — no third-party dependency, so the host
dispatcher runs on a bare Raspberry Pi OS Lite with nothing installed.

Supports: * , - / and plain numbers. Names (MON, JAN) and @macros are not
supported; the API validates on write so bad expressions never reach the
dispatcher.
"""
from datetime import datetime

RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]


def _field(spec: str, lo: int, hi: int) -> set[int] | None:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            if not s.isdigit() or int(s) == 0:
                return None
            step = int(s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return None
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            return None
        if start < lo or end > hi or start > end:
            return None
        out.update(range(start, end + 1, step))
    return out or None


def parse(expr: str) -> list[set[int]] | None:
    parts = expr.split()
    if len(parts) != 5:
        return None
    fields = []
    for spec, (lo, hi) in zip(parts, RANGES):
        f = _field(spec, lo, hi)
        if f is None:
            return None
        fields.append(f)
    return fields


def validate(expr: str) -> bool:
    return parse(expr) is not None


def due(expr: str, now: datetime) -> bool:
    f = parse(expr)
    if not f:
        return False
    dom_restricted = expr.split()[2] != "*"
    dow_restricted = expr.split()[4] != "*"
    dom_ok = now.day in f[2]
    dow_ok = (now.weekday() + 1) % 7 in f[4]   # python Mon=0 -> cron Sun=0

    # cron semantics: if both day-of-month and day-of-week are restricted,
    # either matching is enough.
    if dom_restricted and dow_restricted:
        day_ok = dom_ok or dow_ok
    else:
        day_ok = dom_ok and dow_ok

    return now.minute in f[0] and now.hour in f[1] and day_ok and now.month in f[3]
