"""Entrypoint binding a genuinely dual-stack socket -- same reason as web/run.py.

`uvicorn --host ::` is IPv6-only on Python: asyncio sets IPV6_V6ONLY, so the
container's healthcheck (localhost -> ::1) passes while Traefik's IPv4
connection is refused, and the app 502s while reporting perfectly healthy.
"""
import socket

import uvicorn

if __name__ == "__main__":
    sock = socket.create_server(
        ("::", 80), family=socket.AF_INET6, dualstack_ipv6=True, backlog=2048)
    uvicorn.run("rag.server:app", fd=sock.fileno())
