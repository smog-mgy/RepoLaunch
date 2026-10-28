"""Bundled Docker smoke check. Uses Python standard library only."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok", "service": "repolaunch-docker-check"}).encode()
            content_type = "application/json; charset=utf-8"
        elif self.path == "/":
            body = b"<!doctype html><meta charset=utf-8><title>RepoLaunch Docker check</title><h1>RepoLaunch: Docker is running</h1><p>This page is served by a real Docker container.</p><p><a href=/health>Check health JSON</a></p>"
            content_type = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"RepoLaunch Docker check listening on 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
