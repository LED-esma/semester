"""A tiny stand-in for a school's Canvas, for testing "Sign in to Canvas" and token renewal
without real credentials. Modes:
  --block           refuse token creation (a school that locks tokens down)
  --require-expiry  like 4cd: tokens need an expiry date, at most 90 days out
/login sets a CSRF cookie and "signs in" by itself after a moment (like SSO bouncing back).
GET /__tokens lists the tokens that are currently valid (for test assertions).
"""
import itertools
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CSRF = "abc+/=="            # sent URL-encoded in the cookie, like Canvas does
# Modes live in one dict so check.py can start this in a thread (serve() below) instead of
# spawning a process — one less thing to go wrong on a build machine.
MODE = {"block": "--block" in sys.argv,
        "cas": "--cas" in sys.argv,          # a school login that dead-ends on "Login Successful"
        "portal": "--portal" in sys.argv,    # a school portal that dead-ends with no telltale wording
        "google": "--google" in sys.argv,    # Google refusing to sign in inside an app window
        "sso": next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--sso=")), None),
        "require_expiry": "--require-expiry" in sys.argv}
CAP_DAYS = 90
ids = itertools.count(101)
TOKENS = {"9999~" + "t" * 64: {"id": 100, "expires_at": None}}   # a pre-issued token for tests that start signed in


def issue(expires_at):
    tid = next(ids)
    tok = f"9999~{tid}" + "x" * 60
    TOKENS[tok] = {"id": tid, "expires_at": expires_at}
    return tok, tid


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", headers=()):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _bearer(self):
        a = self.headers.get("Authorization") or ""
        return a[7:] if a.startswith("Bearer ") and a[7:] in TOKENS else None

    def do_GET(self):
        if MODE["google"]:
            return self._send(403, "<title>Sign in - Google Accounts</title><h1>Couldn't sign you in</h1>"
                              "<p>This browser or app may not be secure. Error 403: disallowed_useragent</p>", "text/html")
        if MODE["portal"]:  # a campus portal that just sits there after login
            return self._send(200, "<title>Student Portal</title><h1>Welcome to the student portal</h1>"
                              "<p>Choose an application below.</p>", "text/html")
        if MODE["cas"]:  # a stand-in for CalNet: says you are signed in, and goes nowhere
            return self._send(200, "<title>CAS - Central Authentication Service</title>"
                              "<h1>Login Successful</h1><p>You have successfully logged in.</p>", "text/html")
        if self.path.startswith("/__tokens"):
            return self._send(200, json.dumps(sorted(v["id"] for v in TOKENS.values())))
        if self.path.startswith("/login"):
            if MODE["sso"]:  # hand off to the school's own login, like a real Canvas does (no session yet)
                return self._send(302, "", "text/html", [("Location", MODE["sso"])])
            return self._send(200, "<h1>School sign-in (test)</h1><p>Signing you in…</p>"
                              "<script>setTimeout(() => location.href = '/?login_success=1', 1500)</script>",
                              "text/html", [("Set-Cookie", "_csrf_token=abc%2B%2F%3D%3D; Path=/")])
        if self.path.startswith("/api/v1/users/self") and not self.path.startswith("/api/v1/users/self/tokens"):
            if not self._bearer():
                return self._send(401, '{"errors":[{"message":"Invalid access token."}]}')
            return self._send(200, json.dumps({"id": 1, "name": "Test Student"}))
        if self.path.startswith("/api/v1/"):
            return self._send(200, "[]")
        return self._send(200, "<h1>Dashboard (test)</h1>", "text/html",
                          [("Set-Cookie", "_csrf_token=abc%2B%2F%3D%3D; Path=/")])

    def do_POST(self):
        if MODE["cas"] or MODE["portal"] or MODE["google"]:
            return self._send(401, '{"status":"unauthenticated","errors":[{"message":"user authorization required"}]}')
        if self.path != "/api/v1/users/self/tokens":
            return self._send(404, "{}")
        session_ok = self.headers.get("X-CSRF-Token") == CSRF and "_csrf_token=" in (self.headers.get("Cookie") or "")
        if not (session_ok or self._bearer()):
            return self._send(422, '{"errors":[{"message":"Invalid CSRF token"}]}')
        if MODE["block"]:
            return self._send(401, '{"status":"unauthorized","errors":[{"message":"user not authorized to perform that action"}]}')
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
        exp = (body.get("token") or {}).get("expires_at")
        if MODE["require_expiry"] and not exp:
            return self._send(400, '[{"message":"Expiration date is required"}]')
        if MODE["require_expiry"] and datetime.fromisoformat(exp.replace("Z", "+00:00")) > datetime.now(timezone.utc) + timedelta(days=CAP_DAYS):
            return self._send(400, f'[{{"message":"Expiration date cannot be more than {CAP_DAYS} days in the future"}}]')
        tok, tid = issue(exp)
        prefix = "" if self._bearer() else "while(1);"   # session JSON is prefixed, like Canvas
        return self._send(200, prefix + json.dumps({"id": tid, "visible_token": tok, "expires_at": exp,
                                                    "purpose": (body.get("token") or {}).get("purpose")}))

    def do_DELETE(self):
        if self.path.startswith("/api/v1/users/self/tokens/") and self._bearer():
            tid = int(self.path.rsplit("/", 1)[1])
            for t in [t for t, v in TOKENS.items() if v["id"] == tid]:
                del TOKENS[t]
            return self._send(200, "{}")
        return self._send(404, "{}")


def serve(**mode):
    """Start on a free port in a background thread and return (server, base_url).
    Call server.shutdown() when done. Used by check.py; the command line below is for
    driving a real sign-in window by hand."""
    MODE.update(dict.fromkeys(MODE, False) | {"sso": None} | mode)
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8796
    mode = ("blocking token creation" if MODE["block"] else
            "expiry required, max 90 days" if MODE["require_expiry"] else "open")
    print(f"fake Canvas on {port} ({mode})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
