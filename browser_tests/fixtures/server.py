"""Test-owned backend launched by the real app-engine process lifecycle."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            body, media_type = b'{"ok":true}', 'application/json'
        else:
            body = Path(__file__).with_name('index.html').read_bytes()
            media_type = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', media_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler)
Path(__file__).with_name('backend.json').write_text(json.dumps({
    'pid': os.getpid(), 'port': server.server_port,
}))
server.serve_forever()
