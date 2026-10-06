"""Local callback receiver for integration checks. Not included in the public demo."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

events = {}
lock = threading.Lock()


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        with lock:
            body = json.dumps(events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with lock:
            history = events.setdefault(self.path, [])
            history.append({"at": time.monotonic(), "payload": payload})
            attempt = len(history)
        if self.path.startswith("/fail/") or self.path.startswith("/flaky/") and attempt < 3:
            code = 503
        else:
            code = 204
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Receiver).serve_forever()
