"""Run the coordinator-UI staff page against the REAL deployed API (tt-portal-api), until there is a hosted web app.

  python infra/portal_ui.py --email divya.shah7999@gmail.com      then open http://127.0.0.1:8788/staff

Serves coordinator-frontend/staff.html unchanged. That page normally talks to the local dev server; here:
  /api/*            -> forwarded to the deployed HTTP API with your Cognito ID token (refreshed before it expires)
  /dev/identities   -> just you, taken from the token, so the page's sign-in picker shows one user
  /dev/reset, other /dev/* and the inspector -> refused (there is no local data to reset; this is real AWS)

Everything is real: Aurora, EventBridge schedules, and "Send now" sends a WhatsApp message to the participant.
Sign-in happens once in this terminal (temporary password, then a new one, on the first run).
"""
import argparse
import base64
import json
import pathlib
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import staff_token

API_URL = "https://ca0jle1s2g.execute-api.us-east-1.amazonaws.com"
UI = pathlib.Path(__file__).resolve().parents[2] / "coordinator-frontend"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}


class Session:
    def __init__(self, email):
        self.lock = threading.Lock()
        self._use(staff_token.sign_in(email))

    def _use(self, result):
        self.id_token = result["IdToken"]
        self.refresh_token = result.get("RefreshToken", getattr(self, "refresh_token", None))
        self.claims = json.loads(base64.urlsafe_b64decode(self.id_token.split(".")[1] + "=="))

    def token(self):
        with self.lock:
            if self.claims["exp"] - time.time() < 120:
                self._use(staff_token.refresh(self.refresh_token))
            return self.id_token


class Handler(BaseHTTPRequestHandler):
    session: Session

    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PATCH = do_DELETE = lambda self: self._route()

    def _route(self):
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        if path in ("/", "/staff") and self.command == "GET":
            return self._send(200, (UI / "staff.html").read_bytes(), TYPES[".html"])
        if path.startswith("/ui/") and self.command == "GET":
            f = UI / path.removeprefix("/ui/")
            if f.parent == UI and f.suffix in TYPES and f.exists():
                return self._send(200, f.read_bytes(), TYPES[f.suffix])
        if path.startswith("/api/staff/"):
            return self._api(path.removeprefix("/api") + (f"?{url.query}" if url.query else ""), body)
        if path == "/dev/identities":
            c = self.session.claims
            return self._send(200, {"staff": [{"username": c["email"], "name": c.get("name"),
                                               "groups": [c.get("role", "")], "site_ids": c.get("site_ids")}],
                                    "participant": []})
        self._send(404, {"error": "Not available here: this page is connected to real AWS"})

    def _api(self, path, body):
        headers = {"Authorization": f"Bearer {self.session.token()}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(API_URL + path, data=body, method=self.command, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=35) as r:
                status, out = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, out = e.code, e.read()
        print(f"  {self.command} {path} -> {status}")
        if path.endswith("/send-now") and status == 200:
            # The page expects {sent, reason}; tt-reminder answers {result, why}.
            r = json.loads(out)
            out = json.dumps({**r, "sent": r.get("result") == "sent", "reason": r.get("why")}).encode()
        self._send(status, out)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--port", type=int, default=8788)
    args = ap.parse_args()
    Handler.session = Session(args.email)
    print(f"Signed in as {Handler.session.claims['email']} ({Handler.session.claims.get('role')}, "
          f"sites {Handler.session.claims.get('site_ids')}).")
    print(f"Open http://127.0.0.1:{args.port}/staff  (real AWS: Send now messages the participant). Ctrl+C stops.")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
