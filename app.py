#!/usr/bin/env python3
"""
Semester — desktop app entry point.

Runs entirely inside its own window (via pywebview): the setup wizard, the
loading/error states, and your dashboard all render in-app — no browser needed.
A tiny local web server backs the UI. Everything stays on your computer; the
only network calls go to your own school's Canvas site.

If pywebview (or its system webview) isn't available, it gracefully falls back
to opening in your default browser, so the app always works.
"""

import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import canvas_planner as cp
import notify

# The bundled Mac app has no system CA certificates, so every urllib HTTPS call failed
# (update checks quietly said "up to date"). certifi ships with requests; use it everywhere.
try:
    import certifi
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))))
except ImportError:
    pass

APP_NAME = "Semester"
DEFAULT_ACCENT = "#6366f1"
GH_REPO = "LED-esma/semester"
# Updaters from v1.5 and earlier look for "Semester-macOS.zip" and unpack it incorrectly;
# a different name makes them open the download page instead of breaking the app.
MAC_ASSET = "Semester-mac.zip"


def _vtuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")) or (0,)


def is_git_checkout():
    return (not getattr(sys, "frozen", False)) and os.path.isdir(os.path.join(cp.HERE, ".git"))


def _release_json():
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GH_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "semester"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def latest_release():
    """(tag, html_url) of the newest GitHub release, or (None, None)."""
    try:
        d = _release_json()
        return d.get("tag_name"), d.get("html_url")
    except Exception:
        return None, None


def update_mode():
    if is_git_checkout():
        return "git"
    if getattr(sys, "frozen", False):   # macOS / Windows / Linux bundled app
        return "selfupdate"
    return "download"


def _app_bundle_path():
    """Path to the running Semester.app (…/Semester.app/Contents/MacOS/Semester → …/Semester.app)."""
    p = sys.executable
    for _ in range(3):
        p = os.path.dirname(p)
    return p if p.endswith(".app") and os.path.isdir(p) else None


def _asset_url(d, name):
    return next((a.get("browser_download_url") for a in d.get("assets", []) if a.get("name") == name), None)


def _spawn_detached(cmd):
    if sys.platform.startswith("win"):
        subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_GROUP
    else:
        subprocess.Popen(cmd, start_new_session=True)


def self_update():
    """Download the latest build for THIS OS, swap it in via a detached helper, relaunch.
    The helper backs up the old app first and restores it if the swap fails. User data
    lives in ~/.semester + the browser, so nothing is lost. Returns a status string."""
    d = _release_json()
    pid = os.getpid()
    tmp = tempfile.mkdtemp(prefix="semester-upd-")
    fallback = d.get("html_url") or f"https://github.com/{GH_REPO}/releases"

    if sys.platform == "darwin":
        url, app_path = _asset_url(d, MAC_ASSET), _app_bundle_path()
        if not url or not app_path:
            webbrowser.open(fallback); return "opened"
        zp = os.path.join(tmp, "new.zip"); urllib.request.urlretrieve(url, zp)
        # ditto keeps the executable bit, symlinks, and signature; Python's zipfile drops all three.
        unpacked = subprocess.run(["ditto", "-x", "-k", zp, tmp]).returncode == 0
        newapp = os.path.join(tmp, "Semester.app")
        verified = unpacked and subprocess.run(["codesign", "--verify", "--deep", "--strict", newapp],
                                               capture_output=True).returncode == 0
        if not verified:  # never swap in an app that won't launch
            webbrowser.open(fallback); return "opened"
        h = os.path.join(tmp, "swap.sh")
        open(h, "w", encoding="utf-8").write(
            "#!/bin/bash\n" f'PID={pid}\nwhile kill -0 "$PID" 2>/dev/null; do sleep 0.4; done\n'
            f'BAK="{app_path}.bak"; rm -rf "$BAK"; mv "{app_path}" "$BAK" || exit 1\n'
            f'if ditto "{newapp}" "{app_path}"; then rm -rf "$BAK"; else rm -rf "{app_path}"; mv "$BAK" "{app_path}"; fi\n'
            f'open "{app_path}"\n')
        os.chmod(h, 0o755); _spawn_detached(["/bin/bash", h]); return "installing"

    if sys.platform.startswith("win"):
        url, target = _asset_url(d, "Semester-Windows.exe"), sys.executable
        if not url or not target:
            webbrowser.open(fallback); return "opened"
        newexe = os.path.join(tmp, "Semester-new.exe"); urllib.request.urlretrieve(url, newexe)
        bat = os.path.join(tmp, "swap.bat")
        open(bat, "w", encoding="utf-8").write(
            "@echo off\r\n"
            f':loop\r\ntasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
            "if not errorlevel 1 ( timeout /t 1 /nobreak >nul & goto loop )\r\n"
            f'move /y "{target}" "{target}.bak" >nul\r\n'
            f'move /y "{newexe}" "{target}" >nul && (del "{target}.bak" >nul 2>&1) || move /y "{target}.bak" "{target}" >nul\r\n'
            f'start "" "{target}"\r\n')
        _spawn_detached(["cmd", "/c", bat]); return "installing"

    # Linux
    url, target = _asset_url(d, "Semester-Linux"), sys.executable
    if not url or not target:
        webbrowser.open(fallback); return "opened"
    newbin = os.path.join(tmp, "Semester-Linux"); urllib.request.urlretrieve(url, newbin); os.chmod(newbin, 0o755)
    h = os.path.join(tmp, "swap.sh")
    open(h, "w", encoding="utf-8").write(
        "#!/bin/bash\n" f'PID={pid}\nwhile kill -0 "$PID" 2>/dev/null; do sleep 0.4; done\n'
        f'BAK="{target}.bak"; cp -f "{target}" "$BAK"\n'
        f'if cp -f "{newbin}" "{target}"; then chmod +x "{target}"; rm -f "$BAK"; else cp -f "$BAK" "{target}"; fi\n'
        f'setsid "{target}" >/dev/null 2>&1 &\n')
    os.chmod(h, 0o755); _spawn_detached(["/bin/bash", h]); return "installing"


# --------------------------------------------------------------------------- #
# Setup wizard (Apple Setup Assistant style: one focused step per screen)
# --------------------------------------------------------------------------- #
# The setup wizard markup now lives in static/setup.html, so it can be linted and
# formatted like the dashboard. Loaded per call, the same way render_html loads app.js.


def with_banner(html, message, label, href):
    """Saved dashboard + a slim status bar, so their data stays usable when a refresh fails."""
    try:
        ts = cp.strf(datetime.fromtimestamp(os.path.getmtime(cp.HTML_PATH)), "%a %b %-d at %-I:%M %p")
    except Exception:
        ts = "your last refresh"
    bar = ('<style>body{display:flex;flex-direction:column;height:100vh}'
           '.app{flex:1;height:auto!important;min-height:0}</style>'
           '<div style="display:flex;align-items:center;justify-content:center;gap:14px;padding:9px 16px;'
           'background:#fff4e5;color:#7a4a00;border-bottom:1px solid #f0c98a;'
           'font:14px -apple-system,system-ui,sans-serif">'
           f'<span>{message} Showing data from {ts}.</span>'
           f'<a href="{href}" style="background:#7a4a00;color:#fff;text-decoration:none;'
           f'padding:6px 14px;border-radius:8px;font-weight:600">{label}</a></div>')
    return html.replace("<body>", "<body>" + bar, 1)


def check_canvas_url(raw):
    """Turn whatever they pasted (a full Canvas link, a bare address, or just 'myschool')
    into a base URL, and confirm Canvas answers there. Returns (ok, base_url_or_error)."""
    raw = (raw or "").strip()
    if not raw:
        return False, "Enter your school's Canvas address."
    host = urllib.parse.urlparse(raw if "://" in raw else "https://" + raw).netloc.lower()
    if host and "." not in host:
        host += ".instructure.com"
    try:
        r = requests.get(f"https://{host}/api/v1/users/self", timeout=8)
        body = r.json() if r.status_code == 401 else {}
    except ValueError:
        body = {}
    except Exception:
        return False, "Couldn't reach that address. Check the spelling and your internet connection."
    if not (body.get("errors") or body.get("status") == "unauthenticated"):
        return False, "That doesn't look like Canvas. Copy the address from your browser while you're signed in."
    final = urllib.parse.urlparse(r.url)  # follow redirects to the school's real Canvas address
    return True, f"{final.scheme}://{final.netloc}"


SCHOOL_CACHE = {}


def search_schools(q):
    """Instructure's school directory (the same search the Canvas mobile app uses). [] on any trouble,
    so setup quietly falls back to typing or pasting an address."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    key = q.lower()
    if key not in SCHOOL_CACHE:
        try:
            r = requests.get("https://canvas.instructure.com/api/v1/accounts/search",
                             params={"search_term": q, "per_page": 8}, timeout=6)
            SCHOOL_CACHE[key] = [{"name": x.get("name"), "domain": x.get("domain"),
                                  "auth": x.get("authentication_provider")} for x in r.json() if x.get("domain")]
        except Exception:
            return []
    return SCHOOL_CACHE[key]


def setup_html(reauth=False, base_url=""):
    """The setup wizard. In reauth mode it jumps straight to the token step,
    explains that the old token expired, and pre-fills the school URL."""
    url = (base_url or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    html = cp._load_static("setup.html").replace("__URL__", url)
    if reauth:
        html = html.replace("<script>", "<script>window.__REAUTH__ = true;", 1)
    if signin_available():
        html = html.replace("<script>", "<script>window.__SIGNIN__ = true;", 1)
    return html


def page(title, msg, accent, actions=""):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{APP_NAME}</title>
<link rel="icon" href='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="%230a0a0c"/><text x="50" y="54" font-family="Helvetica,Arial,sans-serif" font-size="74" font-weight="700" fill="white" text-anchor="middle" dominant-baseline="central">S</text></svg>'>
<style>
  body {{ font-family: -apple-system, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif; background: #fbfbfd; color: #1d1d1f;
         display: flex; height: 100vh; margin: 0; align-items: center; justify-content: center; }}
  .c {{ text-align: center; max-width: 460px; padding: 0 24px; }}
  .c h2 {{ font-weight: 600; font-size: 24px; margin: 0 0 8px; letter-spacing: -.02em; }}
  .c p {{ font-size: 15px; color: #6e6e73; line-height: 1.5; }}
  .dot {{ width: 36px; height: 36px; border: 3px solid #e5e5ea; border-top-color: {accent};
         border-radius: 50%; margin: 0 auto 16px; animation: spin 0.8s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  a.btn {{ display: inline-block; margin: 18px 6px 0; background: {accent}; color: #fff; text-decoration: none;
          padding: 11px 26px; border-radius: 980px; font-size: 15px; font-weight: 500; }}
  a.btn.ghost {{ background: #f0f0f3; color: #1d1d1f; }}
</style></head><body><div class="c"><h2>{title}</h2><p>{msg}</p>{actions}</div></body></html>"""


def loading_page(accent):
    return page("Loading your dashboard…", "Fetching the latest from Canvas.", accent,
                '<div class="dot" style="margin-top:18px"></div>'
                '<script>setTimeout(()=>location.reload(),1200)</script>')


def error_page(msg, accent):
    return page("Couldn't load your dashboard", msg, accent,
                '<a class="btn" href="/retry">Try again</a>'
                '<a class="btn ghost" href="/setup">Re-run setup</a>')


# --------------------------------------------------------------------------- #
# Server + build state
# --------------------------------------------------------------------------- #
def raw_config():
    """The saved config even without a token: after logging out it still holds the school and preferences."""
    try:
        with open(cp.CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def valid_config():
    if not os.path.exists(cp.CONFIG_PATH):
        return None
    try:
        with open(cp.CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    if not cfg.get("base_url") or not cfg.get("token") or "PASTE" in str(cfg.get("token")):
        return None
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    return cfg


BUILD = {"status": "idle", "error": None, "built": 0}  # idle | building | done | error | expired
ACCOUNT = {}  # the connected Canvas user's name, looked up once


def ensure_build(cfg):
    if BUILD["status"] in ("building", "done", "error", "expired"):
        return  # failures wait for /retry, a new token, or the hourly refresh
    BUILD["status"], BUILD["error"] = "building", None

    def run():
        try:
            try:
                CHANGE_SIG["sig"] = canvas_signature(cfg)  # baseline, so later checks rebuild only on real changes
            except Exception:
                pass
            cp.build_dashboard(cfg)
            BUILD["status"], BUILD["built"] = "done", int(time.time())
            threading.Thread(target=notify.check, daemon=True).start()  # anything new, or anything now due
        except cp.TokenExpired:
            BUILD["status"], BUILD["error"] = "expired", None
        except Exception as e:
            BUILD["status"], BUILD["error"] = "error", str(e)
    threading.Thread(target=run, daemon=True).start()


# Measured on a real account: a full refresh is 39 requests (~8 Canvas rate-limit units of ~700),
# while the change check below is 3 tiny requests (~0.25 units). So check often, rebuild only on change.
CHECK_EVERY, FULL_EVERY = 5 * 60, 30 * 60
CHANGE_SIG = {"sig": None}
LAST_CHECK = {"t": 0.0}
_check_lock = threading.Lock()


def canvas_signature(cfg):
    """Three tiny requests whose answers change whenever something new lands in Canvas."""
    c = cp.Canvas(cfg["base_url"], cfg["token"])
    return json.dumps([c._get(p) for p in ("/users/self/todo_item_count",
                                           "/users/self/activity_stream/summary",
                                           "/conversations/unread_count")], sort_keys=True)


def refresh_now(cfg, force=False):
    """Rebuild if Canvas changed since the last build (or when forced). Returns True if a rebuild started."""
    if not _check_lock.acquire(blocking=False):
        return False  # a check is already running
    try:
        LAST_CHECK["t"] = time.time()
        try:
            changed = canvas_signature(cfg) != CHANGE_SIG["sig"]
        except cp.TokenExpired:
            BUILD["status"] = "expired"
            return False
        except Exception:
            return False  # offline or Canvas hiccup: try again next cycle
        if (changed or force) and BUILD["status"] != "building":
            BUILD["status"], BUILD["error"] = "idle", None
            ensure_build(cfg)
            return True
        return False
    finally:
        _check_lock.release()


def auto_refresh_loop():
    """Every few minutes, a near-free change check; a full refresh only when something changed,
    and at least every half hour to catch edits the check can't see (like a moved due date)."""
    while True:
        time.sleep(CHECK_EVERY)
        renew_token_if_needed()
        try:
            notify.check()  # time-based reminders between refreshes
        except Exception:
            pass
        cfg = valid_config()
        if cfg and cfg.get("autorefresh", True) and BUILD["status"] != "expired":
            refresh_now(cfg, force=time.time() - BUILD["built"] >= FULL_EVERY)


# --------------------------------------------------------------------------- #
# "Sign in to Canvas": the user logs in on the school's real page in a second window,
# then Semester creates an access token for them (named "Semester") through Canvas's API.
# The password only ever goes to the school's page.
# --------------------------------------------------------------------------- #
SIGNIN = {"state": "idle", "name": None, "error": None}  # idle | waiting | done | closed | error
SIGNIN_NUDGE = threading.Event()  # set by "Already signed in? Continue" in setup

# The token-creation script the sign-in window runs: static/create_token.js


# Tokens Semester makes itself carry an id and an expiry, so it can renew them before they run out.
TOKEN_META_KEYS = ("token_id", "token_expires", "token_days")
RENEW_WITHIN_DAYS = 14
NOTIFY_KEYS = ("notify_due", "notify_due_when", "notify_morning_hour", "notify_new", "notify_new_assignments",
               "notify_new_announcements", "notify_new_grades", "notify_start", "notify_quiet",
               "notify_quiet_from", "notify_quiet_to", "notify_mute_courses")


def token_meta(token_id, expires_at):
    """What Semester needs to renew a token it made: its id, when it expires, and how long the school allows."""
    meta = {"token_id": token_id, "token_expires": expires_at, "token_days": None}
    if expires_at:
        left = cp.parse_dt(expires_at) - datetime.now(timezone.utc)
        meta["token_days"] = max(1, round(left.total_seconds() / 86400))
    return meta


def _create_token(base_url, token, days):
    """Make a new token using the current one. Returns Canvas's token object, or None."""
    exp = (datetime.now(timezone.utc) + timedelta(days=days) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.post(f"{base_url}/api/v1/users/self/tokens", timeout=20,
                      headers={"Authorization": f"Bearer {token}"},
                      json={"token": {"purpose": "Semester", "expires_at": exp}})
    lower = re.search(r"more than (\d+) days", r.text) if r.status_code == 400 else None
    if lower and int(lower.group(1)) < days:  # the school shortened its limit since the last renewal
        return _create_token(base_url, token, int(lower.group(1)))
    return r.json() if r.status_code in (200, 201) else None


def renew_token_if_needed():
    """A token Semester made itself (via Sign in) renews two weeks before it expires, so nobody has to
    sign in again. Pasted tokens can't be renewed this way; when one expires, Semester asks to reconnect."""
    cfg = valid_config()
    if not cfg or not cfg.get("token_expires") or not cfg.get("token_days"):
        return False
    if cp.parse_dt(cfg["token_expires"]) - datetime.now(timezone.utc) > timedelta(days=RENEW_WITHIN_DAYS):
        return False
    try:
        new = _create_token(cfg["base_url"], cfg["token"], int(cfg["token_days"]))
        ok = bool(new and new.get("visible_token")) and cp.validate_token(cfg["base_url"], new["visible_token"])[0]
    except Exception:
        return False
    if not ok:
        return False
    old_id = cfg.get("token_id")
    cfg.update({"token": new["visible_token"], **token_meta(new.get("id"), new.get("expires_at"))})
    with open(cp.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if old_id:
        try:
            requests.delete(f"{cfg['base_url']}/api/v1/users/self/tokens/{old_id}", timeout=20,
                            headers={"Authorization": f"Bearer {cfg['token']}"})
        except Exception:
            pass  # the old one simply expires on its own
    return True


def signin_available():
    """Sign-in needs the app window (pywebview); the browser fallback can only paste a token."""
    try:
        import webview
        return bool(webview.windows)
    except Exception:
        return False


def _open_signin_window(url):
    import webview
    return webview.create_window("Sign in to Canvas", url, width=520, height=720)


def _signin_flow(base_url):
    """Open the school's sign-in page and wait for the window to land back on Canvas with a session, then
    have Canvas make the token. Schools bounce through their own login (CAS, SAML, a portal), so this
    watches for a working Canvas session instead of for any particular address."""
    host = urllib.parse.urlparse(base_url).netloc
    try:
        win = _open_signin_window(base_url + "/login")
    except Exception:
        SIGNIN.update(state="error", error="Couldn't open the sign-in window. Paste a token instead.")
        return
    closed = threading.Event()
    win.events.closed += lambda *a: closed.set()
    asked_at = nudged_at = 0.0
    deadline = time.time() + 10 * 60
    try:
        while not closed.is_set() and time.time() < deadline:
            time.sleep(0.6)
            try:
                here = urllib.parse.urlparse(win.get_current_url() or "")
            except Exception:
                continue
            if here.netloc != host:
                if SIGNIN_NUDGE.is_set():  # they pressed "Already signed in? Continue"
                    SIGNIN_NUDGE.clear()
                    try:
                        win.load_url(base_url)
                    except Exception:
                        pass
                    continue
                # Some schools finish on their own "Login successful" page (Berkeley's CalNet does).
                # That page never returns to Canvas, so send the window back ourselves.
                if time.time() - nudged_at > 5:
                    nudged_at = time.time()
                    try:
                        seen = win.evaluate_js("(document.title || '') + ' ' + "
                                               "(document.body ? document.body.innerText.slice(0, 400) : '')") or ""
                    except Exception:
                        seen = ""
                    # Google refuses to sign anyone in inside an app window, and always will.
                    if "accounts.google.com" in here.netloc or re.search(r"disallowed_useragent|browser or app may not be secure", seen, re.I):
                        SIGNIN.update(state="error", error="Your school signs in with Google, which won't open inside "
                                                          "apps. Paste a token instead.")
                        return
                    if re.search(r"log(?:in|ged)[ -]?in success|sign(?:ed|-in)? in success|authentication success"
                                 r"|success(?:ful|fully) (?:logged|signed) in|you are (?:now )?(?:logged|signed) in"
                                 r"|already (?:logged|signed) in", seen, re.I):
                        try:
                            win.load_url(base_url)
                        except Exception:
                            pass
                continue
            SIGNIN_NUDGE.clear()
            if time.time() - asked_at > 3:  # on Canvas: ask for a token; "not signed in yet" just means wait
                win.evaluate_js(cp._load_static("create_token.js"))
                asked_at = time.time()
                continue
            raw = win.evaluate_js("window.__semesterResult || ''")
            if not raw:
                continue
            res = json.loads(raw)
            if res.get("token"):
                ok, info = cp.validate_token(base_url, res["token"])
                if ok:
                    cfg = {**raw_config(), "base_url": base_url, "token": res["token"],
                           **token_meta(res.get("id"), res.get("expires_at"))}
                    with open(cp.CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, indent=2)
                    ACCOUNT["name"] = info
                    BUILD["status"], BUILD["error"] = "idle", None
                    ensure_build(valid_config())
                    SIGNIN.update(state="done", name=info, error=None)
                    return
            # Only a flat "you may not create tokens" ends sign-in. Anything else (no session yet, a
            # half-finished login, a hiccup) just means keep waiting until the deadline.
            if res.get("blocked"):
                SIGNIN.update(state="error", error="Your school doesn't let students create access tokens this way. "
                                                   "Paste a token instead, or ask your school's Canvas admin.")
                return
        if closed.is_set():
            SIGNIN.update(state="closed", error=None)
        else:
            SIGNIN.update(state="error", error="Sign-in took too long. Try again, or paste a token instead.")
    finally:
        if not closed.is_set():
            try:
                win.destroy()
            except Exception:
                pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, loc):
        self.send_response(302)
        self.send_header("Location", loc)
        self.end_headers()

    LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

    def _host_ok(self):
        """Only answer to a local Host. This is what stops DNS rebinding: a page on evil.com
        whose name has been re-pointed at 127.0.0.1 counts as same-origin with itself, so the
        browser sends no Origin and would let the page read the reply — but the request still
        arrives with Host: evil.com."""
        host = (self.headers.get("Host") or "").strip()
        if not host:                                  # HTTP/1.0 clients may send none
            return True
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host.split("]")[0]
        return name.strip("[]").lower() in self.LOCAL_HOSTS

    def _origin_ok(self):
        """Reject a write asked for by another site. A browser always sets Origin on a cross-site
        POST, so its absence means a non-browser caller (the CLI, the MCP connector) rather than
        a way in; Sec-Fetch-Site catches the rest."""
        origin = self.headers.get("Origin")
        if origin is None:
            return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")
        u = urllib.parse.urlparse(origin)
        return (u.scheme == "http" and (u.hostname or "").lower() in self.LOCAL_HOSTS
                and u.port == self.server.server_address[1])

    def _deny(self):
        return self._send(403, json.dumps({"ok": False, "error": "Blocked: that request came from another site."}),
                          "application/json")

    def do_GET(self):
        if not self._host_ok():
            return self._deny()
        if self.path.startswith("/setup"):
            existing = raw_config()
            return self._send(200, setup_html(reauth="reauth=1" in self.path,
                                              base_url=existing.get("base_url", "")))
        if self.path.startswith("/retry"):
            BUILD["status"], BUILD["error"] = "idle", None
            return self._redirect("/")
        if self.path.startswith("/api/schools"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
            return self._send(200, json.dumps(search_schools(q)), "application/json")
        if self.path.startswith("/api/signin/status"):
            return self._send(200, json.dumps({k: SIGNIN[k] for k in ("state", "name", "error")}), "application/json")
        if self.path.startswith("/api/account"):
            cfg = valid_config()
            if not cfg:
                return self._send(200, json.dumps({"ok": False}), "application/json")
            if "name" not in ACCOUNT:
                ok, info = cp.validate_token(cfg["base_url"], cfg["token"])
                ACCOUNT["name"] = info if ok else None
            return self._send(200, json.dumps({"ok": True, "name": ACCOUNT["name"],
                              "school": urllib.parse.urlparse(cfg["base_url"]).netloc}), "application/json")
        if self.path.startswith("/api/status"):
            return self._send(200, json.dumps({"built": BUILD.get("built", 0),
                                               "status": BUILD["status"]}), "application/json")
        if self.path.startswith("/api/update"):
            tag, url = latest_release()
            latest = (tag or "").lstrip("v")
            newer = bool(tag) and _vtuple(latest) > _vtuple(cp.VERSION)
            return self._send(200, json.dumps({
                "current": cp.VERSION, "latest": latest or cp.VERSION,
                "url": url or f"https://github.com/{GH_REPO}/releases",
                "newer": newer, "mode": update_mode(), "checked": bool(tag),
            }), "application/json")
        if self.path.startswith("/api/data"):
            # Everything the dashboard draws itself from, as one document. The page still gets
            # this embedded at build time; this is the same data for anything that is not the page.
            try:
                with open(cp.PAGE_DATA_PATH, encoding="utf-8") as f:
                    return self._send(200, f.read(), "application/json")
            except FileNotFoundError:
                return self._send(404, json.dumps({"ok": False, "error": "No data yet. Open Semester once so it can fetch from Canvas."}),
                                  "application/json")
        if self.path.startswith("/api/canvas/"):
            return self._canvas_get()
        if self.path.startswith("/calendar.ics"):
            ics = cp.build_ics()
            if ics is None:
                return self._send(404, "Calendar not ready — open Semester once so it can fetch your data.",
                                  "text/plain; charset=utf-8")
            return self._send(200, ics, "text/calendar; charset=utf-8")
        if self.path == "/" or self.path.startswith("/dashboard"):
            cfg = valid_config()
            if not cfg:
                return self._send(200, setup_html(base_url=raw_config().get("base_url", "")))
            ensure_build(cfg)
            cached = os.path.exists(cp.HTML_PATH)
            status = BUILD["status"]
            if status == "done" or (status == "building" and cached):
                # result first: show the saved dashboard now; the page swaps itself when fresh data lands
                with open(cp.HTML_PATH, "rb") as f:
                    return self._send(200, f.read())
            if status == "expired":
                if cached:
                    with open(cp.HTML_PATH, encoding="utf-8") as f:
                        return self._send(200, with_banner(f.read(), "Canvas disconnected &mdash; your access token expired.",
                                                           "Reconnect", "/setup?reauth=1"))
                return self._send(200, setup_html(reauth=True, base_url=cfg.get("base_url", "")))
            if status == "error":
                if cached:
                    with open(cp.HTML_PATH, encoding="utf-8") as f:
                        return self._send(200, with_banner(f.read(), "Couldn&rsquo;t refresh from Canvas.", "Try again", "/retry"))
                return self._send(200, error_page(BUILD["error"], cfg.get("accent") or DEFAULT_ACCENT))
            return self._send(200, loading_page(cfg.get("accent") or DEFAULT_ACCENT))
        return self._send(404, "Not found")

    def _canvas_get(self):
        """Live read-only Canvas proxies for the Inbox (token stays server-side)."""
        cfg = valid_config()
        if not cfg:
            return self._send(200, json.dumps({"ok": False, "error": "Not configured."}), "application/json")
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        canvas = cp.Canvas(cfg["base_url"], cfg["token"])
        try:
            if u.path == "/api/canvas/conversations":
                out = [{"id": c.get("id"), "subject": c.get("subject") or "(no subject)",
                        "preview": c.get("last_message"), "date": c.get("last_message_at"),
                        "unread": c.get("workflow_state") == "unread",
                        "with": ", ".join(p.get("name", "") for p in (c.get("participants") or [])[:3])}
                       for c in canvas.get("/conversations", {"per_page": 50})]
                return self._send(200, json.dumps({"ok": True, "items": out}), "application/json")
            if u.path == "/api/canvas/conversation":
                conv = canvas.get(f"/conversations/{int(q['id'][0])}")
                conv = conv[0] if isinstance(conv, list) else conv
                who = {p["id"]: p.get("name", "?") for p in (conv.get("participants") or [])}
                msgs = [{"author": who.get(m.get("author_id"), "?"), "body": m.get("body"),
                         "date": m.get("created_at")} for m in (conv.get("messages") or [])]
                return self._send(200, json.dumps({"ok": True, "subject": conv.get("subject"),
                                                   "messages": msgs}), "application/json")
            if u.path == "/api/canvas/teachers":
                t = [{"id": p.get("id"), "name": p.get("name")}
                     for p in canvas.get(f"/courses/{int(q['course_id'][0])}/users",
                                         {"enrollment_type[]": "teacher", "per_page": 50})]
                return self._send(200, json.dumps({"ok": True, "teachers": t}), "application/json")
        except Exception as e:
            return self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")
        return self._send(404, json.dumps({"ok": False, "error": "unknown"}), "application/json")

    def _reauth(self):
        """Swap in a fresh Canvas token; every other setting stays put."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or "{}")
        except Exception:
            data = {}
        cfg = valid_config() or {}
        token = (data.get("token") or "").strip()
        ok, info = cp.validate_token(cfg.get("base_url", ""), token)
        if not ok:
            return self._send(200, json.dumps({"ok": False, "error": info}), "application/json")
        cp.save_token({k: v for k, v in cfg.items() if k not in TOKEN_META_KEYS}, token)  # a pasted token isn't renewable
        BUILD["status"], BUILD["error"] = "idle", None
        ACCOUNT["name"] = info
        return self._send(200, json.dumps({"ok": True, "name": info}), "application/json")

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._deny()
        if self.path == "/api/reauth":
            return self._reauth()
        if self.path not in ("/api/save", "/api/logout", "/api/signin/start", "/api/signin/continue", "/api/notify/test", "/api/check", "/api/check_url", "/api/prefs", "/api/notify", "/api/canvas/done",
                             "/api/canvas/note", "/api/canvas/submit",
                             "/api/canvas/reply", "/api/canvas/compose", "/api/update/run"):
            return self._send(404, "{}", "application/json")
        if self.path == "/api/update/run":
            mode = update_mode()
            if mode == "git":
                try:
                    out = subprocess.run(["git", "-C", cp.HERE, "pull", "--ff-only"],
                                         capture_output=True, text=True, timeout=60)
                    return self._send(200, json.dumps({"ok": out.returncode == 0, "mode": "git",
                        "output": (out.stdout + out.stderr).strip()[:400]}), "application/json")
                except Exception as e:
                    return self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")
            if mode == "selfupdate":
                try:
                    result = self_update()
                    if result == "installing":
                        threading.Timer(1.2, lambda: os._exit(0)).start()  # quit so the helper can swap
                    return self._send(200, json.dumps({"ok": result in ("installing", "opened"),
                        "mode": "selfupdate", "result": result}), "application/json")
                except Exception as e:
                    return self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")
            _, url = latest_release()
            webbrowser.open(url or f"https://github.com/{GH_REPO}/releases")
            return self._send(200, json.dumps({"ok": True, "mode": "download"}), "application/json")
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or "{}")
        except Exception:
            data = {}
        if self.path == "/api/canvas/done":
            cfg = valid_config()
            if not cfg:
                return self._send(200, json.dumps({"ok": False, "error": "Not configured."}), "application/json")
            try:
                canvas = cp.Canvas(cfg["base_url"], cfg["token"])
                ok = canvas.mark_done(int(data.get("assign_id")), bool(data.get("done", True)))
                return self._send(200, json.dumps({"ok": ok}), "application/json")
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")
        if self.path == "/api/canvas/note":
            cfg = valid_config()
            if not cfg:
                return self._send(200, json.dumps({"ok": False, "error": "Not configured."}), "application/json")
            try:
                canvas = cp.Canvas(cfg["base_url"], cfg["token"])
                ok = canvas.save_note(int(data.get("assign_id")), data.get("text", ""),
                                      title=(data.get("title") or "Semester note")[:120])
                return self._send(200, json.dumps({"ok": ok}), "application/json")
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")
        if self.path in ("/api/canvas/reply", "/api/canvas/compose"):
            cfg = valid_config()
            if not cfg:
                return self._send(200, json.dumps({"ok": False, "error": "Not configured."}), "application/json")
            body = (data.get("body") or "").strip()
            if not body:
                return self._send(200, json.dumps({"ok": False, "error": "Message is empty."}), "application/json")
            try:
                canvas = cp.Canvas(cfg["base_url"], cfg["token"])
                if self.path == "/api/canvas/reply":
                    r = canvas.post(f"/conversations/{int(data['id'])}/add_message", {"body": body})
                else:
                    payload = {"recipients[]": data.get("recipients") or [], "body": body,
                               "subject": data.get("subject") or "(no subject)"}
                    if data.get("course_id"):
                        payload["context_code"] = f"course_{data['course_id']}"
                    r = canvas.post("/conversations", payload)
                ok = r.status_code in (200, 201)
                return self._send(200, json.dumps({"ok": ok, "error": "" if ok else r.text[:200]}), "application/json")
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")
        if self.path == "/api/canvas/submit":
            cfg = valid_config()
            if not cfg:
                return self._send(200, json.dumps({"ok": False, "error": "Not configured."}), "application/json")
            content = (data.get("content") or "").strip()
            if not content:
                return self._send(200, json.dumps({"ok": False, "error": "Nothing to submit."}), "application/json")
            try:
                canvas = cp.Canvas(cfg["base_url"], cfg["token"])
                ok, err = canvas.submit(data.get("course_id"), int(data.get("assign_id")),
                                        data.get("type", "online_text_entry"), content)
                return self._send(200, json.dumps({"ok": ok, "error": str(err)}), "application/json")
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}), "application/json")
        if self.path == "/api/signin/continue":  # "Already signed in? Continue"
            if SIGNIN["state"] == "waiting":
                SIGNIN_NUDGE.set()
            return self._send(200, json.dumps({"ok": SIGNIN["state"] == "waiting"}), "application/json")
        if self.path == "/api/signin/start":
            if not signin_available():
                return self._send(200, json.dumps({"ok": False, "error": "Sign-in works in the Semester app window. Paste a token instead."}), "application/json")
            base = (data.get("base_url") or raw_config().get("base_url") or "").strip().rstrip("/")
            if not base:
                return self._send(200, json.dumps({"ok": False, "error": "Enter your school's Canvas address first."}),
                                  "application/json")
            if "://" not in base:
                base = "https://" + base
            if SIGNIN["state"] != "waiting":
                SIGNIN.update(state="waiting", name=None, error=None)
                threading.Thread(target=_signin_flow, args=(base,), daemon=True).start()
            return self._send(200, json.dumps({"ok": True}), "application/json")
        if self.path == "/api/logout":
            for _ in range(40):  # let a running refresh finish so it can't write data back afterwards
                if BUILD["status"] != "building":
                    break
                time.sleep(0.25)
            cfg = raw_config()
            for k in ("token", *TOKEN_META_KEYS):  # keep the school and preferences so logging back in is quick
                cfg.pop(k, None)
            with open(cp.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            for p in (cp.HTML_PATH, cp.DATA_PATH, cp.OVERRIDES_PATH, cp.NOTES_PATH):
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                notify.uninstall()  # reminders need an account
            except Exception:
                pass
            BUILD.update(status="idle", error=None, built=0)
            CHANGE_SIG["sig"] = None
            ACCOUNT.clear()
            return self._send(200, json.dumps({"ok": True}), "application/json")
        if self.path == "/api/check":  # the window came back into focus
            cfg = valid_config()
            if cfg and cfg.get("autorefresh", True) and time.time() - LAST_CHECK["t"] > 60:
                threading.Thread(target=refresh_now, args=(cfg,), daemon=True).start()
            return self._send(200, json.dumps({"ok": True}), "application/json")
        if self.path == "/api/check_url":
            ok, info = check_canvas_url(data.get("base_url", ""))
            return self._send(200, json.dumps({"ok": True, "base_url": info} if ok else {"ok": False, "error": info}),
                              "application/json")
        if self.path == "/api/notify/test":
            notify._notify("Semester", "Notifications are working.")
            return self._send(200, json.dumps({"ok": True}), "application/json")
        if self.path == "/api/notify":
            if data.get("enabled"):
                ok, msg = notify.install(int(data.get("interval", 60) or 60))
            else:
                ok, msg = notify.uninstall()
            return self._send(200, json.dumps({"ok": ok, "msg": msg}), "application/json")
        if self.path == "/api/prefs":
            cfg = valid_config()
            if not cfg:
                return self._send(200, json.dumps({"ok": False}), "application/json")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(data.get("accent", "#000000"))):
                data.pop("accent")
            for k in ("notify_due", "notify_new", "notify_start", "notify_quiet",
                      "notify_new_assignments", "notify_new_announcements", "notify_new_grades"):
                if k in data:
                    data[k] = bool(data[k])
            for k in ("notify_morning_hour", "notify_quiet_from", "notify_quiet_to"):
                if k in data:
                    try:
                        data[k] = min(23, max(0, int(data[k])))
                    except (TypeError, ValueError):
                        data.pop(k)
            if "notify_due_when" in data:
                w = data["notify_due_when"] if isinstance(data["notify_due_when"], list) else []
                ok = lambda x: x in ("day_before", "morning", "hours3") or (
                    isinstance(x, str) and x.startswith("minutes:") and x[8:].isdigit() and 0 < int(x[8:]) <= 20160)
                data["notify_due_when"] = [x for x in w if ok(x)]
            if "notify_mute_courses" in data:
                m = data["notify_mute_courses"]
                data["notify_mute_courses"] = [str(x)[:120] for x in m][:50] if isinstance(m, list) else []
            changed = [k for k in ("aggressiveness", "autorefresh", "show_all_courses", "accent", *NOTIFY_KEYS)
                       if k in data]
            for k in changed:
                cfg[k] = data[k]
            with open(cp.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            if any(k not in ("accent", *NOTIFY_KEYS) for k in changed):  # the accent applies live; everything else needs fresh data
                BUILD["status"], BUILD["error"] = "idle", None
            return self._send(200, json.dumps({"ok": True}), "application/json")
        base_url, token = data.get("base_url", ""), data.get("token", "")
        accent = data.get("accent") or DEFAULT_ACCENT
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(accent)):
            accent = DEFAULT_ACCENT
        ok, info = cp.validate_token(base_url, token)
        if not ok:
            return self._send(200, json.dumps({"ok": False, "error": info}), "application/json")
        base_url = base_url.rstrip("/")
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        merged = {k: v for k, v in (valid_config() or {}).items() if k not in TOKEN_META_KEYS}  # a pasted token isn't renewable
        merged.update({"base_url": base_url, "token": token, "accent": accent})
        with open(cp.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        BUILD["status"], BUILD["error"] = "idle", None  # rebuild with new settings
        ensure_build(valid_config() or merged)  # start loading courses while they finish setup
        ACCOUNT["name"] = info
        return self._send(200, json.dumps({"ok": True, "name": info}), "application/json")


def free_port():
    for p in (8765, 8766, 8780, 8800, 0):
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    return 8765


def selfcheck():
    """Render a dashboard from sample data and quit. CI runs the built app this way on each OS,
    so a missing bundled file or a date format one platform rejects fails the build instead of
    somebody's download. No window, no network, no account."""
    try:
        now = datetime.now(timezone.utc)
        items = cp.demo_items(now)
        names = sorted({i["course"] for i in items})
        colors = {n: cp.COURSE_PALETTE[i % len(cp.COURSE_PALETTE)] for i, n in enumerate(names)}
        courses = [{"name": n, "url": "#", "syllabus_url": "#", "syllabus": "", "pending": 1} for n in names]
        html = cp.render_html(items, cp.workload_warnings(items, now, threshold=2), courses, [], colors, now)
        assert "<html" in html and "Week board" in html and len(html) > 20_000, "page came out wrong"
        cp.write_text("semester-selfcheck.html", html)   # the app's own writer, not a tidier one

        # The setup screen and the sign-in script are bundled files too, and setup is the first
        # thing a new user sees. Draw them here so a bundling miss fails CI, not an install.
        setup = setup_html()
        assert '<html' in setup and 'id="token"' in setup and len(setup) > 10_000, "setup page came out wrong"
        assert 'users/self/tokens' in cp._load_static("create_token.js"), "sign-in script came out wrong"

        print(f"selfcheck ok — {len(html) // 1024} KB page, {len(setup) // 1024} KB setup")
        return 0
    except Exception as e:
        print(f"selfcheck FAILED: {type(e).__name__}: {e}")
        return 1


def main():
    if "--selfcheck" in sys.argv:  # CI smoke test of the built app
        sys.exit(selfcheck())
    if "--notify" in sys.argv:  # one-shot background check invoked by the scheduler
        notify.run_once()
        return
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    threading.Thread(target=renew_token_if_needed, daemon=True).start()

    # Preferred: render everything inside a native app window.
    try:
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True  # so "Download .ics" saves a file
        webview.create_window(APP_NAME, url, width=1180, height=800, min_size=(900, 600))
        webview.start()
    except Exception:
        # Fallback: open in the default browser and stay alive.
        print(f"{APP_NAME} is running at {url}\nLeave this window open while you use it; close it to quit.")
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    server.shutdown()


if __name__ == "__main__":
    main()
