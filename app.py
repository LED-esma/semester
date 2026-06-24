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
        open(h, "w").write(
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
        open(bat, "w").write(
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
    open(h, "w").write(
        "#!/bin/bash\n" f'PID={pid}\nwhile kill -0 "$PID" 2>/dev/null; do sleep 0.4; done\n'
        f'BAK="{target}.bak"; cp -f "{target}" "$BAK"\n'
        f'if cp -f "{newbin}" "{target}"; then chmod +x "{target}"; rm -f "$BAK"; else cp -f "$BAK" "{target}"; fi\n'
        f'setsid "{target}" >/dev/null 2>&1 &\n')
    os.chmod(h, 0o755); _spawn_detached(["/bin/bash", h]); return "installing"


# --------------------------------------------------------------------------- #
# Setup wizard (Apple Setup Assistant style: one focused step per screen)
# --------------------------------------------------------------------------- #
SETUP_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Welcome to Semester</title>
<link rel="icon" href='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="%230a0a0c"/><text x="50" y="54" font-family="Helvetica,Arial,sans-serif" font-size="74" font-weight="700" fill="white" text-anchor="middle" dominant-baseline="central">S</text></svg>'>
<style>
  :root { --accent: #6366f1; --bg: #fbfbfd; --text: #1d1d1f; --dim: #6e6e73; --field: #fff; --line: #d2d2d7;
          --soft: #f0f0f3; --ok: #1d8a3a; --bad: #c0392b; }
  html.dark { --bg: #1c1c1e; --text: #f5f5f7; --dim: #98989d; --field: #2c2c2e; --line: #3a3a3c;
              --soft: #2c2c2e; --ok: #4cd964; --bad: #ff6b5e; }
  [hidden] { display: none !important; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: var(--bg); color: var(--text); transition: background .2s, color .2s;
         font-family: -apple-system, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif; }
  .wiz { width: min(520px, 90vw); text-align: center; position: relative; padding: 40px 0; }
  .back { position: absolute; top: 0; left: 0; font: inherit; font-size: 15px; background: none; border: none;
          color: var(--accent); cursor: pointer; padding: 6px 0; }
  .screen { display: none; }
  .screen.active { display: block; animation: fade .25s ease; }
  @keyframes fade { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .mark { width: 72px; height: 72px; border-radius: 17px; background: #0a0a0c; color: #fff; margin: 0 auto 22px;
          font: 700 50px/72px Helvetica, Arial, sans-serif; }
  html.dark .mark { box-shadow: 0 0 0 1px #3a3a3c; }
  h1 { font-size: 30px; font-weight: 600; letter-spacing: -.02em; margin: 0 0 10px; }
  p { font-size: 16px; line-height: 1.5; color: var(--dim); margin: 0 auto 24px; max-width: 420px; }
  .fine { font-size: 13px; color: var(--dim); margin-top: 14px; }
  input[type=text], input[type=password] { width: 100%; max-width: 380px; font-size: 16px; padding: 13px 15px;
         background: var(--field); color: var(--text); border: 1px solid var(--line); border-radius: 12px;
         outline: none; font-family: inherit; }
  input[type=text]:focus, input[type=password]:focus { border-color: var(--accent);
         box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 18%, transparent); }
  .cont { display: inline-block; background: var(--accent); color: #fff; border: none; font-size: 16px; font-weight: 500;
          padding: 13px 40px; border-radius: 980px; cursor: pointer; font-family: inherit; }
  .cont:hover { filter: brightness(.95); }
  .cont:disabled { opacity: .6; cursor: default; }
  .combo { position: relative; max-width: 380px; margin: 0 auto; }
  .suggest { position: absolute; left: 0; right: 0; top: calc(100% + 6px); z-index: 5; margin: 0; padding: 4px;
             list-style: none; text-align: left; background: var(--field); border: 1px solid var(--line);
             border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,.12); }
  .suggest li { padding: 9px 12px; border-radius: 8px; cursor: pointer; font-size: 15px; }
  .suggest li[aria-selected=true] { background: var(--soft); }
  .suggest .d { display: block; font-size: 12px; color: var(--dim); }
  .suggest .none { color: var(--dim); cursor: default; font-size: 14px; }
  .status { min-height: 22px; font-size: 14px; margin: 10px auto 16px; max-width: 420px; color: var(--dim); }
  .status.ok { color: var(--ok); } .status.bad { color: var(--bad); }
  .spin { display: inline-block; width: 12px; height: 12px; border: 2px solid var(--line); border-top-color: var(--accent);
          border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -1px; margin-right: 7px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .steps { text-align: left; max-width: 380px; margin: 0 auto 20px; padding: 0; list-style: none; counter-reset: s; }
  .steps li { counter-increment: s; position: relative; padding: 1px 0 12px 34px; font-size: 15px; line-height: 1.45; }
  .steps li::before { content: counter(s); position: absolute; left: 0; top: 0; width: 22px; height: 22px; border-radius: 50%;
          background: var(--soft); font-size: 12px; font-weight: 600; text-align: center; line-height: 22px; }
  .steps b { font-weight: 600; }
  .linkbtn { background: none; border: none; color: var(--accent); font: inherit; padding: 0; cursor: pointer; }
  .linkbtn:hover { text-decoration: underline; }
  .note { text-align: left; background: var(--soft); border-left: 3px solid #f59e0b; border-radius: 8px;
          padding: 11px 14px; font-size: 14px; line-height: 1.5; margin: 0 auto 20px; max-width: 420px; }
  .preview { max-width: 380px; margin: 0 auto 18px; background: var(--field); border: 1px solid var(--line);
             border-radius: 14px; padding: 14px 16px; text-align: left; }
  .pv-top, .pv-bot { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: var(--dim); }
  .pv-course { display: flex; align-items: center; gap: 6px; }
  .pv-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
  .pv-title { font-size: 15px; font-weight: 600; margin: 6px 0 10px; }
  .pv-btn { background: var(--accent); color: #fff; font-weight: 500; padding: 4px 12px; border-radius: 980px; }
  .rows { max-width: 380px; margin: 0 auto 6px; text-align: left; }
  .row { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 12px 0;
         border-top: 1px solid var(--line); font-size: 15px; }
  .row:first-child { border-top: none; }
  .rsub { display: block; font-size: 12px; color: var(--dim); margin-top: 2px; }
  .seg { display: inline-flex; background: var(--soft); border-radius: 9px; padding: 2px; flex: none; }
  .seg button { border: none; background: none; color: var(--text); font: inherit; font-size: 13px;
                padding: 5px 12px; border-radius: 7px; cursor: pointer; }
  .seg button[aria-pressed=true] { background: var(--field); box-shadow: 0 1px 3px rgba(0,0,0,.15); font-weight: 500; }
  .swatches { display: flex; gap: 8px; flex: none; }
  .sw { width: 22px; height: 22px; border-radius: 50%; border: none; cursor: pointer; padding: 0;
        box-shadow: 0 0 0 1px rgba(0,0,0,.08); }
  .sw[aria-pressed=true] { box-shadow: 0 0 0 2px var(--bg), 0 0 0 4px var(--accent); }
  .sw.custom { background: conic-gradient(#f43f5e, #f59e0b, #22c55e, #06b6d4, #6366f1, #d946ef, #f43f5e);
               position: relative; overflow: hidden; }
  .sw.custom input { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
  .switch { position: relative; width: 44px; height: 26px; flex: none; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch .track { position: absolute; inset: 0; background: var(--line); border-radius: 13px; cursor: pointer; transition: background .2s; }
  .switch .track::after { content: ""; position: absolute; top: 2px; left: 2px; width: 22px; height: 22px; background: #fff;
                          border-radius: 50%; transition: transform .2s; box-shadow: 0 1px 2px rgba(0,0,0,.2); }
  .switch input:checked + .track { background: var(--accent); }
  .switch input:checked + .track::after { transform: translateX(18px); }
  .dots { display: flex; gap: 8px; justify-content: center; margin-top: 32px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--line); }
  .dot.on { background: var(--accent); }
</style></head>
<body>
<div class="wiz">
  <button class="back" id="back">&lsaquo; Back</button>

  <section class="screen active" data-i="0">
    <div class="mark">S</div>
    <h1>Welcome to Semester</h1>
    <p>Your assignments, deadlines, and grades from Canvas, in one calm place. Setup takes about a minute.</p>
    <button class="cont" data-next>Get started</button>
    <div class="fine">Everything stays on this computer.</div>
  </section>

  <section class="screen" data-i="1">
    <h1>Find your school</h1>
    <p>Search for your school, or paste your Canvas address.</p>
    <div class="combo">
      <input id="url" type="text" placeholder="School name or Canvas address" autocomplete="off" spellcheck="false" value="__URL__"
             role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="schoolList">
      <ul class="suggest" id="schoolList" role="listbox" hidden></ul>
    </div>
    <div class="status" id="urlStatus"></div>
    <button class="cont" id="urlNext">Continue</button>
  </section>

  <section class="screen" data-i="2">
    <h1 id="tokenHead">Connect your account</h1>
    <div class="note" id="reauthNote" hidden>Your Canvas access expired, so Semester can&rsquo;t refresh. Reconnect below. Your settings and plans stay put.</div>
    <div id="signinBox" hidden>
      <p>Sign in with your school account. Semester sets up access for you.</p>
      <button class="cont" id="signin" type="button">Sign in to Canvas</button>
      <div class="status" id="signStatus"></div>
      <p class="fine" id="googleNote" hidden>Your school signs in with Google, which may not open inside Semester. If it doesn&rsquo;t, paste a token instead.</p>
      <button class="linkbtn" id="showPaste" type="button">Paste a token instead</button>
    </div>
    <div id="pasteBox">
      <p id="tokenLead">Canvas lets apps like Semester in with an access token. Making one takes a few seconds:</p>
      <ol class="steps">
        <li><button class="linkbtn" id="openCanvas" type="button">Open your Canvas settings &#8599;</button></li>
        <li>Click <b>+ New Access Token</b>, name it <b>Semester</b>, and leave the expiry date blank.</li>
        <li>Copy the token and paste it below. It connects automatically.</li>
      </ol>
      <input id="token" type="password" placeholder="Paste your token" autocomplete="off" spellcheck="false">
      <div class="status" id="tokStatus"></div>
    </div>
    <div class="fine" id="tokenFine">Your token is saved only on this computer.</div>
  </section>

  <section class="screen" data-i="3">
    <h1>Make it yours</h1>
    <p id="helloLead">You can change any of this later in Settings.</p>
    <div class="preview" aria-hidden="true">
      <div class="pv-top"><span class="pv-course"><span class="pv-dot"></span>Calculus III</span><span>10 pts</span></div>
      <div class="pv-title">Problem Set 4</div>
      <div class="pv-bot"><span>Due Thu 11:59 PM</span><span class="pv-btn">Mark done</span></div>
    </div>
    <div class="rows">
      <div class="row"><span>Appearance</span>
        <div class="seg" id="theme"><button data-v="system">Auto</button><button data-v="light">Light</button><button data-v="dark">Dark</button></div></div>
      <div class="row"><span>Accent color</span><div class="swatches" id="swatches"></div></div>
      <div class="row"><span>Reminders<span class="rsub">A heads-up before things are due, even when Semester is closed.</span></span>
        <label class="switch"><input type="checkbox" id="remind" checked aria-label="Reminders"><span class="track"></span></label></div>
    </div>
    <div class="status" id="buildStatus"></div>
    <button class="cont" id="finish">Open Semester</button>
  </section>

  <div class="dots" id="dots"></div>
</div>
<script>
  const $ = id => document.getElementById(id);
  const REAUTH = !!window.__REAUTH__;
  const SIGNIN = !!window.__SIGNIN__;
  const screens = [...document.querySelectorAll('.screen')];
  const dotsBox = $('dots');
  let i = 0;
  screens.forEach(() => { const d = document.createElement('div'); d.className = 'dot'; dotsBox.appendChild(d); });

  function show(n) {
    i = Math.max(0, Math.min(screens.length - 1, n));
    screens.forEach(s => s.classList.toggle('active', +s.dataset.i === i));
    [...dotsBox.children].forEach((d, k) => d.classList.toggle('on', k === i));
    $('back').style.visibility = i === 0 ? 'hidden' : 'visible';
    const inp = screens[i].querySelector('input[type=text], input[type=password]');
    if (inp) setTimeout(() => { if (!inp.offsetParent) return; inp.focus(); if (inp.value) inp.select(); }, 60);  // typing replaces a pre-filled value
  }
  $('back').addEventListener('click', () => show(i - 1));
  document.querySelectorAll('[data-next]').forEach(b => b.addEventListener('click', () => show(i + 1)));
  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || /INPUT|BUTTON/.test(e.target.tagName)) return;
    if (i === 0) show(1); else if (i === 3) $('finish').click();
  });

  const post = async (path, body) => (await fetch(path, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const setStatus = (el, html, kind) => { el.className = 'status' + (kind ? ' ' + kind : ''); el.innerHTML = html || ''; };
  const CHECKING = '<span class="spin"></span>Checking…';
  const OFFLINE = 'Couldn’t reach Semester. Try again.';

  // Look: starts from whatever the app already uses, so re-running setup keeps it.
  const SW = ['#6366f1', '#8b5cf6', '#ec4899', '#f97316', '#14b8a6', '#0a84ff', '#22c55e'];
  let accent = '#6366f1', theme = 'system';
  const mq = matchMedia('(prefers-color-scheme: dark)');
  const applyTheme = () => document.documentElement.classList.toggle('dark', theme === 'dark' || (theme === 'system' && mq.matches));
  mq.addEventListener('change', applyTheme);
  function setTheme(v) {
    theme = v; [...$('theme').children].forEach(b => b.setAttribute('aria-pressed', b.dataset.v === v)); applyTheme();
  }
  function setAccent(c) {
    accent = c; document.documentElement.style.setProperty('--accent', c);
    [...$('swatches').children].forEach(s => s.setAttribute('aria-pressed',
      s.classList.contains('custom') ? !SW.includes(c) : s.dataset.c === c));
  }
  [...$('theme').children].forEach(b => b.addEventListener('click', () => setTheme(b.dataset.v)));
  SW.forEach(c => {
    const b = document.createElement('button'); b.className = 'sw'; b.dataset.c = c; b.style.background = c;
    b.setAttribute('aria-label', 'Accent ' + c); b.addEventListener('click', () => setAccent(c)); $('swatches').appendChild(b);
  });
  const custom = document.createElement('label'); custom.className = 'sw custom'; custom.title = 'Custom color';
  custom.innerHTML = '<input type="color" aria-label="Custom accent color">'; $('swatches').appendChild(custom);
  custom.querySelector('input').addEventListener('input', e => setAccent(e.target.value));
  let saved = {};
  try { saved = { theme: localStorage.getItem('semester.theme'), accent: localStorage.getItem('semester.accent') }; } catch (e) {}
  setTheme(saved.theme || 'system'); setAccent(saved.accent || '#6366f1');

  // School: accepts a full Canvas link, a bare address, or just the school's Canvas name.
  let baseUrl = '', urlTimer = null, urlSeq = 0;
  async function checkUrl(value) {
    const raw = (typeof value === 'string' ? value : $('url').value).trim(), st = $('urlStatus');
    if (!raw) { setStatus(st, ''); return false; }
    const seq = ++urlSeq;
    setStatus(st, CHECKING);
    try {
      const d = await post('/api/check_url', { base_url: raw });
      if (seq !== urlSeq) return false;  // a newer check replaced this one
      if (!d.ok) { setStatus(st, esc(d.error), 'bad'); return false; }
      baseUrl = d.base_url;
      setStatus(st, 'Found Canvas at ' + esc(baseUrl.replace(/^https?:\/\//, '')), 'ok');
      return true;
    } catch (e) { setStatus(st, OFFLINE, 'bad'); return false; }
  }
  async function urlNext() {
    clearTimeout(urlTimer);
    if (baseUrl) return show(2);
    const v = $('url').value.trim();
    if (!looksLikeAddress(v) && schools.length) return chooseSchool(Math.max(pick, 0));  // a name: take the highlighted match
    if (await checkUrl($('url').dataset.domain || v)) show(2);
  }
  // Typing a name searches Instructure's school directory; a link or address is checked directly.
  const looksLikeAddress = v => /[./:]/.test(v);
  const list = $('schoolList');
  let schools = [], pick = -1, schoolSeq = 0;
  function renderSchools(msg) {
    list.innerHTML = '';
    if (msg) { const li = document.createElement('li'); li.className = 'none'; li.textContent = msg; list.appendChild(li); }
    schools.forEach((sc, k) => {
      const li = document.createElement('li'); li.setAttribute('role', 'option'); li.setAttribute('aria-selected', k === pick);
      li.innerHTML = '<span></span><span class="d"></span>';
      li.firstChild.textContent = sc.name; li.lastChild.textContent = sc.domain;
      li.addEventListener('mousedown', ev => { ev.preventDefault(); chooseSchool(k); });
      list.appendChild(li);
    });
    const open = !!(msg || schools.length);
    list.hidden = !open; $('url').setAttribute('aria-expanded', open);
  }
  const closeSchools = () => { schools = []; pick = -1; renderSchools(); };
  async function searchSchools() {
    const q = $('url').value.trim(), seq = ++schoolSeq;
    if (q.length < 2 || looksLikeAddress(q)) return closeSchools();
    let found = [];
    try { found = await (await fetch('/api/schools?q=' + encodeURIComponent(q))).json(); } catch (e) {}
    if (seq !== schoolSeq) return;  // a newer search replaced this one
    schools = found.slice(0, 6); pick = schools.length ? 0 : -1;
    renderSchools(schools.length ? '' : 'No schools found. Try the full name, or paste your Canvas address.');
  }
  async function chooseSchool(k) {
    const sc = schools[k]; closeSchools();
    $('url').value = sc.name; $('url').dataset.domain = sc.domain; baseUrl = '';
    $('googleNote').hidden = !/google/i.test(sc.auth || '');
    if (await checkUrl(sc.domain)) setTimeout(() => show(2), 500);
  }
  $('url').addEventListener('input', e => {
    baseUrl = ''; delete $('url').dataset.domain; $('googleNote').hidden = true;
    clearTimeout(urlTimer); setStatus($('urlStatus'), '');
    if (looksLikeAddress($('url').value.trim())) {  // a paste is checked at once; typing waits for a short pause
      closeSchools(); urlTimer = setTimeout(checkUrl, e.inputType === 'insertFromPaste' ? 0 : 450);
    } else urlTimer = setTimeout(searchSchools, 200);
  });
  $('url').addEventListener('keydown', e => {
    const open = !list.hidden && schools.length;
    if (open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault(); pick = (pick + (e.key === 'ArrowDown' ? 1 : schools.length - 1)) % schools.length; renderSchools();
    } else if (e.key === 'Escape') closeSchools();
    else if (e.key === 'Enter') { if (open && pick >= 0) chooseSchool(pick); else urlNext(); }
  });
  $('url').addEventListener('blur', () => setTimeout(closeSchools, 150));
  $('urlNext').addEventListener('click', urlNext);

  // Token: checked the moment it's pasted; success moves on by itself.
  $('openCanvas').addEventListener('click', () => {
    const u = (baseUrl || $('url').value.trim()).replace(/\/+$/, '');
    if (!u) { show(1); return; }
    window.open((/^https?:\/\//.test(u) ? u : 'https://' + u) + '/profile/settings#access_tokens', '_blank');
  });
  function connected(st, name) {
    setStatus(st, 'Connected' + (name ? ' as ' + esc(name) : '') + '.', 'ok');
    if (REAUTH) { setTimeout(() => { location.href = '/'; }, 700); return; }
    const first = (name || '').split(' ')[0];
    if (first) $('helloLead').textContent = 'Hi ' + first + '. You can change any of this later in Settings.';
    watchBuild();
    setTimeout(() => show(3), 700);
  }

  // Sign in on the school's real page, in its own window; Semester makes the token.
  const showPaste = () => { $('pasteBox').hidden = false; $('showPaste').hidden = true; };
  async function startSignin() {
    const st = $('signStatus'), btn = $('signin');
    btn.disabled = true;
    setStatus(st, '<span class="spin"></span>Finish signing in, in the window that just opened.');
    let d = {};
    try { d = await post('/api/signin/start', { base_url: baseUrl || $('url').value.trim() }); } catch (e) {}
    if (!d.ok) { btn.disabled = false; setStatus(st, esc(d.error || OFFLINE), 'bad'); showPaste(); return; }
    const poll = setInterval(async () => {
      let s = {};
      try { s = await (await fetch('/api/signin/status', { cache: 'no-store' })).json(); } catch (e) { return; }
      if (s.state === 'waiting') return;
      clearInterval(poll); btn.disabled = false;
      if (s.state === 'done') return connected(st, s.name);
      if (s.state === 'closed') return setStatus(st, 'The sign-in window was closed. Try again when you’re ready.');
      setStatus(st, esc(s.error || 'Sign-in didn’t work.'), 'bad');
      showPaste();  // offer the other way right away
    }, 1000);
  }
  $('signin').addEventListener('click', startSignin);
  $('showPaste').addEventListener('click', () => { showPaste(); $('token').focus(); });
  if (SIGNIN) {
    $('signinBox').hidden = false; $('pasteBox').hidden = true;
    $('tokenFine').textContent = 'Your password goes only to your school. Semester saves an access token on this computer.';
  }

  let tokTimer = null, lastTok = '';
  async function checkToken() {
    const t = $('token').value.trim(), st = $('tokStatus');
    if (t.length < 20 || t === lastTok) return;
    lastTok = t; setStatus(st, CHECKING);
    try {
      const d = REAUTH ? await post('/api/reauth', { token: t })
                       : await post('/api/save', { base_url: baseUrl, token: t, accent });
      if (!d.ok) { setStatus(st, esc(d.error || 'That token didn’t work. Copy it again.'), 'bad'); return; }
      connected(st, d.name);
    } catch (e) { lastTok = ''; setStatus(st, OFFLINE, 'bad'); }
  }
  $('token').addEventListener('input', e => { clearTimeout(tokTimer);
    tokTimer = setTimeout(checkToken, e.inputType === 'insertFromPaste' ? 0 : 350); });
  $('token').addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(tokTimer); lastTok = ''; checkToken(); } });

  // Courses load in the background while they pick a look.
  let ready = false;
  async function watchBuild() {
    const st = $('buildStatus');
    setStatus(st, '<span class="spin"></span>Loading your courses…');
    for (;;) {
      let s = {};
      try { s = await (await fetch('/api/status', { cache: 'no-store' })).json(); } catch (e) {}
      if (s.status === 'done') { ready = true; setStatus(st, 'Your courses are ready.', 'ok'); return; }
      if (s.status === 'error' || s.status === 'expired') { ready = true; setStatus(st, ''); return; }  // the dashboard explains it
      await new Promise(r => setTimeout(r, 1000));
    }
  }
  $('finish').addEventListener('click', async () => {
    const btn = $('finish'), remind = $('remind').checked;
    try {
      localStorage.setItem('semester.theme', theme); localStorage.setItem('semester.accent', accent);
      localStorage.setItem('semester.bgNotify', remind ? 'on' : 'off');
    } catch (e) {}
    btn.disabled = true; btn.textContent = ready ? 'Opening…' : 'Finishing up…';
    try { await post('/api/prefs', { accent }); } catch (e) {}
    if (remind) { try { await post('/api/notify', { enabled: true, interval: 60 }); } catch (e) {} }
    const t0 = Date.now();
    while (!ready && Date.now() - t0 < 20000) await new Promise(r => setTimeout(r, 300));
    location.href = '/';
  });

  if (REAUTH) {
    $('reauthNote').hidden = false; $('tokenLead').hidden = true; $('tokenHead').textContent = 'Reconnect to Canvas';
    $('back').hidden = true; dotsBox.hidden = true;
    show(2);
  } else {
    show(0);
    if ($('url').value.trim()) checkUrl();  // re-running setup: verify the saved school up front
  }
</script>
</body></html>"""


def with_banner(html, message, label, href):
    """Saved dashboard + a slim status bar, so their data stays usable when a refresh fails."""
    try:
        ts = time.strftime("%a %b %-d at %-I:%M %p", time.localtime(os.path.getmtime(cp.HTML_PATH)))
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
    html = SETUP_HTML.replace("__URL__", url)
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
        with open(cp.CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


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
        cfg = valid_config()
        if cfg and cfg.get("autorefresh", True) and BUILD["status"] != "expired":
            refresh_now(cfg, force=time.time() - BUILD["built"] >= FULL_EVERY)


# --------------------------------------------------------------------------- #
# "Sign in to Canvas": the user logs in on the school's real page in a second window,
# then Semester creates an access token for them (named "Semester") through Canvas's API.
# The password only ever goes to the school's page.
# --------------------------------------------------------------------------- #
SIGNIN = {"state": "idle", "name": None, "error": None}  # idle | waiting | done | closed | error

CREATE_TOKEN_JS = r"""(function () {
  window.__semesterResult = '';
  var m = document.cookie.match(/(?:^|;\s*)_csrf_token=([^;]+)/);
  fetch('/api/v1/users/self/tokens', { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json',
               'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' },
    body: JSON.stringify({ token: { purpose: 'Semester' } }) })
  .then(function (r) { return r.text().then(function (t) {
    var j = {}; try { j = JSON.parse(t.replace(/^while\(1\);/, '')); } catch (e) {}
    window.__semesterResult = JSON.stringify({ status: r.status, token: j.visible_token || j.token || null });
  }); })
  .catch(function () { window.__semesterResult = JSON.stringify({ status: 0, token: null }); });
  return 'started';
})()"""


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
    host = urllib.parse.urlparse(base_url).netloc
    try:
        win = _open_signin_window(base_url + "/login")
    except Exception:
        SIGNIN.update(state="error", error="Couldn't open the sign-in window. Paste a token instead.")
        return
    closed = threading.Event()
    win.events.closed += lambda *a: closed.set()
    asked_at, deadline = 0.0, time.time() + 15 * 60
    try:
        while not closed.is_set() and time.time() < deadline:
            time.sleep(0.8)
            try:
                here = urllib.parse.urlparse(win.get_current_url() or "")
            except Exception:
                continue
            if here.netloc != host or here.path.startswith("/login"):
                continue  # still on the school's sign-in pages
            if time.time() - asked_at > 10:  # ask (again, if the page moved on mid-request)
                win.evaluate_js(CREATE_TOKEN_JS)
                asked_at = time.time()
                continue
            raw = win.evaluate_js("window.__semesterResult || ''")
            if not raw:
                continue
            res = json.loads(raw)
            if res.get("token"):
                ok, info = cp.validate_token(base_url, res["token"])
                if ok:
                    with open(cp.CONFIG_PATH, "w") as f:
                        json.dump({**raw_config(), "base_url": base_url, "token": res["token"]}, f, indent=2)
                    ACCOUNT["name"] = info
                    BUILD["status"], BUILD["error"] = "idle", None
                    ensure_build(valid_config())
                    SIGNIN.update(state="done", name=info, error=None)
                    return
            SIGNIN.update(state="error", error=(
                "Your school doesn't let students create access tokens this way. "
                "Paste a token instead, or ask your school's Canvas admin."
                if res.get("status") in (401, 403) else "Canvas couldn't create a token. Paste one instead."))
            return
        if closed.is_set():
            SIGNIN.update(state="closed", error=None)
        else:
            SIGNIN.update(state="error", error="Sign-in timed out. Try again.")
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

    def do_GET(self):
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
        cp.save_token(cfg, token)
        BUILD["status"], BUILD["error"] = "idle", None
        ACCOUNT["name"] = info
        return self._send(200, json.dumps({"ok": True, "name": info}), "application/json")

    def do_POST(self):
        if self.path == "/api/reauth":
            return self._reauth()
        if self.path not in ("/api/save", "/api/logout", "/api/signin/start", "/api/check", "/api/check_url", "/api/prefs", "/api/notify", "/api/canvas/done",
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
            cfg.pop("token", None)  # keep the school and preferences so logging back in is quick
            with open(cp.CONFIG_PATH, "w") as f:
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
            changed = [k for k in ("aggressiveness", "autorefresh", "autorefresh_min", "show_all_courses", "accent")
                       if k in data]
            for k in changed:
                cfg[k] = data[k]
            with open(cp.CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
            if any(k != "accent" for k in changed):  # the accent applies live; everything else needs fresh data
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
        merged = {**(valid_config() or {}), "base_url": base_url, "token": token, "accent": accent}
        with open(cp.CONFIG_PATH, "w") as f:
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
