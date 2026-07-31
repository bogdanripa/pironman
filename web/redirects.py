"""Redirect rules: pattern -> target, applied by the static host.

Patterns follow the conventions people already expect from Netlify/Vercel-style
`_redirects` files, so a rule can usually be copied across unchanged:

    /old-page            /new-page                 exact path
    /blog/*              /news/:splat              wildcard, tail captured
    /posts/:id           /articles/:id             named placeholder
    /docs/:section/*     /help/:section/:splat     both together
    /old                 https://example.com/new   external target

`*` matches the rest of the path (including slashes) and is available in the
target as `:splat`. `:name` matches one path segment and is available as `:name`.
A pattern with no wildcard or placeholder must match the whole path exactly.

The query string is carried over unless the target sets its own, so
`/old?a=1` -> `/new?a=1` without having to say so.
"""
import re
from urllib.parse import urlsplit, urlunsplit

# Only real redirect statuses. 301/308 are permanent (308 keeps the method and
# body, which matters for a redirected POST); 302/307 are temporary.
STATUSES = (301, 302, 307, 308)
DEFAULT_STATUS = 301

_PLACEHOLDER = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")


def compile_rule(pattern: str) -> re.Pattern:
    """Turn a redirect pattern into an anchored regex.

    Everything outside a placeholder or `*` is escaped, so a pattern containing
    regex metacharacters (`/prices(old)`) matches literally rather than blowing
    up or matching something unintended.
    """
    out, i = [], 0
    for m in re.finditer(r"\*|:([A-Za-z_][A-Za-z0-9_]*)", pattern):
        out.append(re.escape(pattern[i:m.start()]))
        if m.group(0) == "*":
            out.append(r"(?P<splat>.*)")
        else:
            out.append(rf"(?P<{m.group(1)}>[^/]+)")
        i = m.end()
    out.append(re.escape(pattern[i:]))
    return re.compile("^" + "".join(out) + "$")


def _expand(target: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), target)


def match(rules: list[dict], path: str, query: str = "") -> tuple[str, int] | None:
    """First matching rule wins, so order is significant — specific rules should
    come before catch-alls. Returns (location, status) or None.

    Rules are stored pre-validated, but a malformed one is skipped rather than
    breaking every request for the app.
    """
    for rule in rules:
        if not isinstance(rule, dict):
            continue  # never let one bad entry break every request for the app
        pattern, target = rule.get("from"), rule.get("to")
        if not pattern or not target:
            continue
        try:
            m = compile_rule(pattern).match(path)
        except re.error:
            continue
        if not m:
            continue

        location = _expand(target, {k: v for k, v in m.groupdict().items() if v is not None})
        # Carry the incoming query string over, unless the target has its own.
        if query:
            parts = urlsplit(location)
            if not parts.query:
                location = urlunsplit(parts._replace(query=query))
        status = rule.get("status") or DEFAULT_STATUS
        return location, (status if status in STATUSES else DEFAULT_STATUS)
    return None
