#!/usr/bin/env python3
"""Tiny test service for the bridge e2e test. Binds 127.0.0.1:$PORT (default
8765) and echoes back a JSON body with the request path + the X-Forwarded-*
headers that nginx adds, so we can prove a public request actually reached this
loopback port on the Mac (i.e. the whole reverse-tunnel chain works)."""
import http.server
import json
import os


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self):
        body = json.dumps({
            "hello": "from-mac-%s" % self.server.server_address[1],
            "method": self.command,
            "path": self.path,
            "host": self.headers.get("Host", ""),
            "x_forwarded_proto": self.headers.get("X-Forwarded-Proto", ""),
            "x_forwarded_port": self.headers.get("X-Forwarded-Port", ""),
            "x_forwarded_for": self.headers.get("X-Forwarded-For", ""),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_HEAD = do_POST = _reply

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("mac test service on 127.0.0.1:%d" % port, flush=True)
    srv.serve_forever()