"""Entrypoint that binds a genuinely dual-stack socket.

`uvicorn --host ::` is NOT dual-stack on Python: asyncio sets IPV6_V6ONLY on an
AF_INET6 listening socket, so the server accepts IPv6 only. Inside a container
that failure is silent and confusing — the healthcheck hits `localhost`, which
resolves to ::1 and succeeds, so the container reports healthy, while Traefik
connects to the container's IPv4 address and gets 'connection refused' → the app
serves 502 while looking perfectly fine from the inside.

Node's `listen('::')` defaults to dual-stack, which is why the platform's usual
advice works there and not here. Binding the socket ourselves with
dualstack_ipv6=True fixes it for both families.
"""
import socket

import uvicorn

if __name__ == "__main__":
    sock = socket.create_server(
        ("::", 80), family=socket.AF_INET6, dualstack_ipv6=True, backlog=2048)
    uvicorn.run("server:app", fd=sock.fileno())
