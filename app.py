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
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import canvas_planner as cp
import notify

APP_NAME = "Semester"
DEFAULT_ACCENT = "#6366f1"
GH_REPO = "LED-esma/semester"


def _vtuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")) or (0,)


def is_git_checkout():
    return (not getattr(sys, "frozen", False)) and os.path.isdir(os.path.join(cp.HERE, ".git"))


def latest_release():
    """(tag, html_url) of the newest GitHub release, or (None, None)."""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GH_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "semester"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.load(r)
        return d.get("tag_name"), d.get("html_url")
    except Exception:
        return None, None


# --------------------------------------------------------------------------- #
# Setup wizard (Apple Setup Assistant style: one focused step per screen)
# --------------------------------------------------------------------------- #
SETUP_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Welcome to Semester</title>
<link rel="icon" href='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="%230a0a0c"/><text x="50" y="54" font-family="Helvetica,Arial,sans-serif" font-size="74" font-weight="700" fill="white" text-anchor="middle" dominant-baseline="central">S</text></svg>'>
<style>
  :root { --accent: #6366f1; }
  * { box-sizing: border-box; }
  body { margin: 0; height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #fbfbfd; color: #1d1d1f; font-family: -apple-system, 'SF Pro Text', system-ui, sans-serif; }
  .wiz { width: min(540px, 90vw); text-align: center; position: relative; padding: 40px 0; }
  .back { position: absolute; top: 6px; left: 0; font-size: 30px; line-height: 1; background: none;
          border: none; color: #86868b; cursor: pointer; }
  .screen { display: none; }
  .screen.active { display: block; animation: fade .25s ease; }
  @keyframes fade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .glyph { font-size: 58px; margin-bottom: 16px; }
  h1 { font-size: 32px; font-weight: 600; letter-spacing: -.02em; margin: 0 0 12px; }
  p { font-size: 16px; line-height: 1.5; color: #6e6e73; margin: 0 auto 26px; max-width: 420px; }
  input[type=text], input[type=password] { width: 100%; max-width: 380px; font-size: 16px; padding: 13px 15px;
          border: 1px solid #d2d2d7; border-radius: 12px; outline: none; font-family: inherit; margin-bottom: 20px; }
  input[type=text]:focus, input[type=password]:focus { border-color: var(--accent); box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 18%, transparent); }
  .cont { display: inline-block; background: var(--accent); color: #fff; border: none; font-size: 16px;
          font-weight: 500; padding: 13px 42px; border-radius: 980px; cursor: pointer; font-family: inherit; }
  .cont:hover { filter: brightness(.95); }
  .ghost { background: #f0f0f3; color: var(--accent); border: none; padding: 11px 22px; border-radius: 980px;
          font-size: 15px; cursor: pointer; margin-bottom: 18px; font-family: inherit; }
  .ghost:hover { background: #e8e8ed; }
  .swatches { display: flex; gap: 12px; justify-content: center; margin-bottom: 18px; }
  .sw { width: 34px; height: 34px; border-radius: 50%; cursor: pointer; border: 3px solid #fff; box-shadow: 0 0 0 1px #d2d2d7; }
  .sw.sel { box-shadow: 0 0 0 2px var(--accent); }
  input[type=color] { width: 56px; height: 38px; border: 1px solid #d2d2d7; border-radius: 10px; background: #fff; padding: 3px; cursor: pointer; vertical-align: middle; }
  .dots { display: flex; gap: 8px; justify-content: center; margin-top: 36px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #d2d2d7; }
  .dot.on { background: var(--accent); }
  .spinner { width: 34px; height: 34px; border: 3px solid #e5e5ea; border-top-color: var(--accent);
          border-radius: 50%; margin: 4px auto 0; animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .msg { min-height: 18px; margin-top: 14px; font-size: 14px; color: #d23b2e; }
</style></head>
<body>
<div class="wiz">
  <button class="back" id="back" aria-label="Back">&#8249;</button>

  <section class="screen active" data-i="0">
    <div class="glyph">&#127891;</div>
    <h1>Welcome to Semester</h1>
    <p>Your whole semester &mdash; assignments, discussions, and deadlines &mdash; in one calm place. Setup takes a minute, and everything stays on this computer.</p>
    <button class="cont" data-next>Get started</button>
  </section>

  <section class="screen" data-i="1">
    <div class="glyph">&#127979;</div>
    <h1>Connect your school</h1>
    <p>What&rsquo;s your school&rsquo;s Canvas web address? It&rsquo;s in your browser&rsquo;s address bar when you&rsquo;re logged into Canvas.</p>
    <input id="url" type="text" placeholder="myschool.instructure.com" autocomplete="off">
    <br><button class="cont" data-next>Continue</button>
  </section>

  <section class="screen" data-i="2">
    <div class="glyph">&#128273;</div>
    <h1>Get your access token</h1>
    <p>This lets Semester read your courses. Open your token page, click &ldquo;+ New Access Token,&rdquo; generate one, and paste it below.</p>
    <button class="ghost" id="openCanvas" type="button">Open my Canvas token page &#8599;</button><br>
    <input id="token" type="password" placeholder="Paste your access token here" autocomplete="off">
    <br><button class="cont" data-next>Continue</button>
  </section>

  <section class="screen" data-i="3">
    <div class="glyph">&#127912;</div>
    <h1>Make it yours</h1>
    <p>Pick an accent color for your dashboard. You can change it anytime later.</p>
    <div class="swatches" id="swatches"></div>
    <input type="color" id="accent" value="#6366f1">
    <p style="margin-top:24px"></p>
    <button class="cont" id="finish">Set up Semester</button>
  </section>

  <section class="screen" data-i="4">
    <div class="glyph">&#10024;</div>
    <h1 id="finishTitle">Setting things up&hellip;</h1>
    <p id="finishMsg">Connecting to Canvas and building your dashboard.</p>
    <div class="spinner"></div>
  </section>

  <div class="dots" id="dots"></div>
  <div class="msg" id="msg"></div>
</div>
<script>
  const $ = id => document.getElementById(id);
  const screens = [...document.querySelectorAll('.screen')];
  const total = screens.length;
  let i = 0;
  const dotsBox = $('dots');
  screens.forEach(() => { const d = document.createElement('div'); d.className = 'dot'; dotsBox.appendChild(d); });

  function show(n) {
    i = Math.max(0, Math.min(total - 1, n));
    screens.forEach(s => s.classList.toggle('active', +s.dataset.i === i));
    [...dotsBox.children].forEach((d, k) => d.classList.toggle('on', k === i));
    $('back').style.visibility = (i === 0 || i === total - 1) ? 'hidden' : 'visible';
    const inp = screens[i].querySelector('input[type=text], input[type=password]');
    if (inp) setTimeout(() => inp.focus(), 60);
  }
  $('back').addEventListener('click', () => show(i - 1));

  const normUrl = u => { u = (u || '').trim().replace(/\\/+$/, ''); if (u && !/^https?:\\/\\//.test(u)) u = 'https://' + u; return u; };
  const setMsg = t => { $('msg').textContent = t || ''; };

  document.querySelectorAll('[data-next]').forEach(b => b.addEventListener('click', () => {
    setMsg('');
    if (i === 1 && !normUrl($('url').value)) { setMsg('Please enter your Canvas web address.'); return; }
    if (i === 2 && !$('token').value.trim()) { setMsg('Please paste your access token.'); return; }
    show(i + 1);
  }));

  const SW = ['#6366f1', '#8b5cf6', '#14b8a6', '#f97366', '#0067c0', '#22c55e', '#ec4899'];
  const setAccent = c => {
    document.documentElement.style.setProperty('--accent', c); $('accent').value = c;
    [...$('swatches').children].forEach(s => s.classList.toggle('sel', s.dataset.c === c));
  };
  SW.forEach(c => { const d = document.createElement('div'); d.className = 'sw'; d.dataset.c = c;
    d.style.background = c; d.title = c; d.addEventListener('click', () => setAccent(c)); $('swatches').appendChild(d); });
  $('accent').addEventListener('input', e => setAccent(e.target.value));
  setAccent('#6366f1');

  $('openCanvas').addEventListener('click', () => {
    const u = normUrl($('url').value);
    if (!u) { setMsg('Enter your Canvas web address on the previous step first.'); return; }
    window.open(u + '/profile/settings#access_tokens', '_blank');
  });

  $('finish').addEventListener('click', async () => {
    const base_url = normUrl($('url').value), token = $('token').value.trim(), accent = $('accent').value;
    if (!base_url || !token) { setMsg('Missing your Canvas address or token.'); show(base_url ? 2 : 1); return; }
    show(total - 1);
    try {
      const r = await fetch('/api/save', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url, token, accent }) });
      const data = await r.json();
      if (data.ok) {
        $('finishTitle').textContent = 'All set, ' + (data.name || '').split(' ')[0] + '!';
        $('finishMsg').textContent = 'Opening your dashboard\\u2026';
        setTimeout(() => { window.location = '/'; }, 900);
      } else { setMsg(data.error || 'Something went wrong.'); show(2); }
    } catch (e) { setMsg('Could not reach the app. Please try again.'); show(2); }
  });

  ['url', 'token'].forEach(id => $(id).addEventListener('keydown', e => {
    if (e.key === 'Enter') { const b = screens[i].querySelector('[data-next], #finish'); if (b) b.click(); }
  }));
  show(0);
</script>
</body></html>"""


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
def valid_config():
    if not os.path.exists(cp.CONFIG_PATH):
        return None
    try:
        with open(cp.CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        return None
    if not cfg.get("base_url") or not cfg.get("token") or "PASTE" in str(cfg.get("token")):
        return None
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    return cfg


BUILD = {"status": "idle", "error": None, "built": 0}  # idle | building | done | error


def ensure_build(cfg):
    if BUILD["status"] in ("building", "done"):
        return
    BUILD["status"], BUILD["error"] = "building", None

    def run():
        try:
            cp.build_dashboard(cfg)
            BUILD["status"], BUILD["built"] = "done", int(time.time())
        except Exception as e:
            BUILD["status"], BUILD["error"] = "error", str(e)
    threading.Thread(target=run, daemon=True).start()


def auto_refresh_loop():
    """Re-fetch Canvas on a schedule (default hourly) so open windows can offer a reload."""
    while True:
        cfg = valid_config()
        mins = int((cfg or {}).get("autorefresh_min", 60) or 60)
        time.sleep(max(15, mins) * 60)
        cfg = valid_config()
        if cfg and cfg.get("autorefresh", True):
            BUILD["status"], BUILD["error"] = "idle", None
            ensure_build(cfg)


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

    def do_GET(self):
        if self.path.startswith("/setup"):
            return self._send(200, SETUP_HTML)
        if self.path.startswith("/retry"):
            BUILD["status"], BUILD["error"] = "idle", None
            return self._redirect("/")
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
                "newer": newer, "mode": "git" if is_git_checkout() else "download",
            }), "application/json")
        if self.path.startswith("/api/canvas/"):
            return self._canvas_get()
        if self.path == "/" or self.path.startswith("/dashboard"):
            cfg = valid_config()
            if not cfg:
                return self._send(200, SETUP_HTML)
            ensure_build(cfg)
            if BUILD["status"] == "done":
                with open(cp.HTML_PATH, "rb") as f:
                    return self._send(200, f.read())
            if BUILD["status"] == "error":
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

    def do_POST(self):
        if self.path not in ("/api/save", "/api/prefs", "/api/notify", "/api/canvas/done",
                             "/api/canvas/note", "/api/canvas/submit",
                             "/api/canvas/reply", "/api/canvas/compose", "/api/update/run"):
            return self._send(404, "{}", "application/json")
        if self.path == "/api/update/run":
            if is_git_checkout():
                try:
                    out = subprocess.run(["git", "-C", cp.HERE, "pull", "--ff-only"],
                                         capture_output=True, text=True, timeout=60)
                    ok = out.returncode == 0
                    return self._send(200, json.dumps({"ok": ok, "mode": "git",
                        "output": (out.stdout + out.stderr).strip()[:400]}), "application/json")
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
            for k in ("aggressiveness", "autorefresh", "autorefresh_min"):
                if k in data:
                    cfg[k] = data[k]
            with open(cp.CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
            BUILD["status"], BUILD["error"] = "idle", None  # rebuild with new prefs
            return self._send(200, json.dumps({"ok": True}), "application/json")
        base_url, token = data.get("base_url", ""), data.get("token", "")
        accent = data.get("accent") or DEFAULT_ACCENT
        ok, info = cp.validate_token(base_url, token)
        if not ok:
            return self._send(200, json.dumps({"ok": False, "error": info}), "application/json")
        base_url = base_url.rstrip("/")
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        with open(cp.CONFIG_PATH, "w") as f:
            json.dump({"base_url": base_url, "token": token, "accent": accent}, f, indent=2)
        BUILD["status"], BUILD["error"] = "idle", None  # rebuild with new settings
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


def main():
    if "--notify" in sys.argv:  # one-shot background check invoked by the scheduler
        notify.run_once()
        return
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()

    # Preferred: render everything inside a native app window.
    try:
        import webview
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
