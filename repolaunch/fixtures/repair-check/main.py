"""Intentionally undeclared requests import for the controlled repair acceptance test."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests  # Deliberately absent in the first test image.

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data=json.dumps({'status':'ok','service':'repolaunch-repair-check'}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)

if __name__=='__main__':
    HTTPServer(('0.0.0.0',int(os.environ.get('PORT','8000'))),Handler).serve_forever()
