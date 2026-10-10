"""Allow-list egress proxy for the agents container: ``python -m shutterstorm.egress_proxy``.

The agents container sits on an internal Docker network with no gateway, so it has no route
to Immich, the host or the internet. Its only way out is this proxy (``HTTPS_PROXY``), which
accepts nothing but ``CONNECT`` tunnels to the allowed host:port pairs (by default
``api.mistral.ai:443``). TLS stays end to end; the proxy never sees the API key or content.
"""
from __future__ import annotations

import argparse
import select
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IDLE_TIMEOUT_S = 120


def make_server(allowed: set[str], host: str = "0.0.0.0", port: int = 3128, log=print) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _refuse(self, code: int, why: str) -> None:
            log(f"refused {self.command} {self.path}: {why}")
            self.send_error(code, why)

        def do_CONNECT(self) -> None:
            target = self.path.lower()
            if target not in allowed:
                return self._refuse(403, f"{target} is not on the egress allow-list")
            name, port_s = target.rsplit(":", 1)
            try:
                upstream = socket.create_connection((name, int(port_s)), timeout=15)
            except OSError as exc:
                return self._refuse(502, f"cannot reach {target}: {exc.__class__.__name__}")
            self.send_response(200, "Connection established")
            self.end_headers()
            log(f"tunnel to {target}")
            conns = [self.connection, upstream]
            try:
                while True:
                    ready, _, broken = select.select(conns, [], conns, IDLE_TIMEOUT_S)
                    if broken or not ready:
                        break
                    for src in ready:
                        data = src.recv(65536)
                        if not data:
                            return
                        (upstream if src is self.connection else self.connection).sendall(data)
            except OSError:
                pass
            finally:
                upstream.close()

        def do_GET(self) -> None:
            self._refuse(403, "only CONNECT tunnels to allow-listed hosts")

        do_POST = do_PUT = do_DELETE = do_HEAD = do_GET

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shutterstorm.egress_proxy")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3128)
    parser.add_argument("--allow", action="append", default=[], help="host:port (repeatable)")
    args = parser.parse_args(argv)
    allowed = {a.lower() for a in (args.allow or ["api.mistral.ai:443"])}
    server = make_server(allowed, args.host, args.port, lambda m: print(m, flush=True))
    print(f"egress proxy on {args.host}:{args.port}, allowed: {', '.join(sorted(allowed))}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
