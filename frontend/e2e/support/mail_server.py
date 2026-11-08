"""A local mail-capture service for the browser tests. Nothing it receives ever leaves the machine.

SMTP on 127.0.0.1:E2E_SMTP_PORT (default 2525) accepts every message and keeps it in memory. A small JSON API on
127.0.0.1:E2E_MAIL_API_PORT (default 8026) lets the tests read what arrived:

    GET    /messages?to=<address>   -> [{"to": [...], "subject": "...", "text": "...", "received": <unix seconds>}]
    DELETE /messages                -> forget everything

It binds to loopback only and needs no credentials, so it must never run anywhere but a test machine or CI runner.
"""

import json
import os
import signal
import threading
import time
from email import message_from_bytes, policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from aiosmtpd.controller import Controller

SMTP_PORT = int(os.environ.get("E2E_SMTP_PORT", "2525"))
API_PORT = int(os.environ.get("E2E_MAIL_API_PORT", "8026"))
MESSAGES: list[dict] = []
LOCK = threading.Lock()


class Capture:
    async def handle_DATA(self, server, session, envelope):
        parsed = message_from_bytes(envelope.content, policy=policy.default)
        body = parsed.get_body(preferencelist=("plain",))
        with LOCK:
            MESSAGES.append({
                "to": [a.lower() for a in envelope.rcpt_tos], "from": envelope.mail_from,
                "subject": str(parsed["Subject"] or ""), "text": body.get_content() if body else "",
                "received": time.time()})
        return "250 Message accepted"


class Api(BaseHTTPRequestHandler):
    def _send(self, status: int, payload) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self._send(200, {"status": "ok"})
        if url.path != "/messages":
            return self._send(404, {"detail": "not found"})
        wanted = (parse_qs(url.query).get("to") or [None])[0]
        with LOCK:
            found = [m for m in MESSAGES if wanted is None or wanted.lower() in m["to"]]
        self._send(200, found)

    def do_DELETE(self):
        with LOCK:
            MESSAGES.clear()
        self._send(200, {"deleted": True})

    def log_message(self, *args):  # silent: the tests print enough
        pass


def main() -> None:
    smtp = Controller(Capture(), hostname="127.0.0.1", port=SMTP_PORT, server_hostname="localhost")
    smtp.start()
    api = ThreadingHTTPServer(("127.0.0.1", API_PORT), Api)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    threading.Thread(target=api.serve_forever, daemon=True).start()
    stop.wait()
    api.shutdown()
    smtp.stop()


if __name__ == "__main__":
    main()
