"""Checks to run before publishing: `python3 check.py`.

CI runs this on macOS, Windows and Linux, so a bug that only shows up on one of them
fails the build instead of somebody's download. The Windows date check is here because
"%-d" (a day with no leading zero) works on Mac and Linux and raises on Windows, which
once shipped a version whose dashboard couldn't render there at all.

Nothing here touches a real Canvas account, a real token, or the network.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import canvas_planner as cp   # noqa: E402

CHECKS = []


def check(name):
    def wrap(fn):
        CHECKS.append((name, fn))
        return fn
    return wrap


def demo_page():
    """Build the dashboard from sample data, the same way `--demo` does."""
    now = datetime.now(timezone.utc)
    items = cp.demo_items(now)
    names = sorted({i["course"] for i in items})
    colors = {n: cp.COURSE_PALETTE[i % len(cp.COURSE_PALETTE)] for i, n in enumerate(names)}
    ann = [{"course": names[0], "title": "Welcome", "url": "#", "posted": now.isoformat(), "preview": "Hi."}]
    crs = [{"name": n, "url": "#", "syllabus_url": "#", "syllabus": "", "pending": 1} for n in names]
    return cp.render_html(items, cp.workload_warnings(items, now, threshold=2), crs, ann, colors, now)


# ---------------------------------------------------------------- the checks

@check("dashboard builds")
def _build():
    html = demo_page()
    for must in ("<html", "Week board", "id=\"globalSearch\"", "</html>"):
        assert must in html, f"missing {must!r}"
    assert "%-" not in html, "a date format leaked into the page"
    return f"{len(html) // 1024} KB"


@check("dates work on Windows")
def _windows_dates():
    """Windows rejects "%-d"/"%-I"; pretend we're there so the same code has to pass here.

    datetime.strftime is C code we can't patch, so feed the page a datetime subclass that
    refuses those codes — anything formatting a date the Windows-unsafe way raises."""
    class Strict(datetime):
        def strftime(self, fmt):
            if re.search(r"%[-#]", fmt):
                raise ValueError("Invalid format string")    # the exact Windows error
            return super().strftime(fmt)

    def as_strict(dt):
        return Strict(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond, dt.tzinfo)

    real_dt, real_parse, real_time = cp.datetime, cp.parse_dt, time.strftime
    cp.datetime = Strict
    cp.parse_dt = lambda iso: (lambda d: as_strict(d) if d else d)(real_parse(iso))
    time.strftime = lambda fmt, *a: (_ for _ in ()).throw(ValueError("Invalid format string")) \
        if re.search(r"%[-#]", fmt) else real_time(fmt, *a)
    try:
        demo_page()
        # and the pieces that format dates outside the page
        cp.fmt_due("2026-01-05T00:07:00Z"), cp.fmt_start("2026-01-05T00:07:00Z"), cp.fmt_posted("2026-01-05T00:07:00Z")
    finally:
        cp.datetime, cp.parse_dt, time.strftime = real_dt, real_parse, real_time
    return "no Windows-only date codes reach strftime"


@check("date helper is correct")
def _strf():
    cases = [((2026, 9, 5, 0, 7), "%a %b %-d, %-I:%M %p", "Sat Sep 5, 12:07 AM"),    # midnight is 12, not 0
             ((2026, 9, 5, 12, 0), "%a %b %-d, %-I:%M %p", "Sat Sep 5, 12:00 PM"),   # noon is 12, not 0
             ((2026, 9, 5, 13, 5), "%a %b %-d, %-I:%M %p", "Sat Sep 5, 1:05 PM"),
             ((2026, 12, 25, 23, 59), "%A, %B %-d at %-I:%M %p", "Friday, December 25 at 11:59 PM")]
    for args, fmt, want in cases:
        got = cp.strf(datetime(*args), fmt)
        assert got == want, f"{fmt} gave {got!r}, wanted {want!r}"
    return f"{len(cases)} formats"


@check("page scripts parse")
def _scripts():
    """A syntax error in a page script doesn't crash anything — it silently kills every button below it.
    Parse each one with Node (present on the build machines) so that fails here instead."""
    import shutil
    node = shutil.which("node")
    if not node:
        return "skipped (no Node here; CI has it)"
    names = sorted(f for f in os.listdir(os.path.join(HERE, "static")) if f.endswith(".js"))
    for n in names:
        r = subprocess.run([node, "--check", os.path.join(HERE, "static", n)], capture_output=True, text=True)
        assert r.returncode == 0, f"{n}: " + (r.stderr.strip().splitlines() or ["syntax error"])[-1]
    return ", ".join(names)


@check("every module imports")
def _imports():
    names = ["app", "notify", "canvas_planner"]
    for n in names:
        subprocess.run([sys.executable, "-c", f"import {n}"], cwd=HERE, check=True,
                       capture_output=True, timeout=120)
    return ", ".join(names)


@check("calendar feed is valid")
def _ics():
    """A malformed feed silently stops syncing in Apple and Google Calendar, so check the shape."""
    now = datetime.now(timezone.utc)
    items = [{"uid": "a1", "course": "MATH 292", "title": "Lab 4", "type": "assignment",
              "due": (now + timedelta(days=2)).isoformat(), "submitted": False, "url": "#"},
             # missing work stays on the calendar however old it is, labelled as missing
             {"uid": "a2", "course": "CHEM 120", "title": "Lab Report 3", "type": "assignment",
              "due": (now - timedelta(days=30)).isoformat(), "submitted": False, "missing": True, "url": "#"},
             # an old deadline you already dealt with should not clutter it
             {"uid": "a3", "course": "CHEM 120", "title": "Ancient quiz", "type": "assignment",
              "due": (now - timedelta(days=30)).isoformat(), "submitted": False, "url": "#"}]
    with tempfile.TemporaryDirectory() as d:
        path, cp.DATA_PATH = cp.DATA_PATH, os.path.join(d, "planner_data.json")
        try:
            with open(cp.DATA_PATH, "w") as f:
                json.dump({"items": items}, f)
            ics = cp.build_ics()
        finally:
            cp.DATA_PATH = path
    assert ics and ics.startswith("BEGIN:VCALENDAR"), "no feed produced"
    assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT") == 2, "event count is wrong"
    assert "Missing: Lab Report 3" in ics, "missing work fell off the calendar"
    assert "Ancient quiz" not in ics, "an old deadline that isn't missing should drop off"
    assert "UID:a1@semester" in ics, "events need a stable id or calendars duplicate them"
    assert ics.endswith("END:VCALENDAR\r\n") and "\r\n" in ics, "iCalendar needs CRLF line endings"
    return "2 events, stable ids, missing work kept"


@check("files are written as UTF-8")
def _encodings():
    """Windows opens text files as cp1252, which can't hold a bullet, an em dash, or an accented
    course name — 1.23 died there with "charmap codec can't encode character". Force that same
    default here and run the real save path, so a forgotten encoding= fails on this machine.

    This is deliberately NOT the app's own selfcheck: that one passed on Windows in CI because the
    test wrote the file more carefully than the app did."""
    import builtins
    import notify
    real_open = builtins.open

    def windows_open(file, mode="r", *a, **kw):
        if "b" not in mode and "encoding" not in kw and not a[2:3]:
            kw["encoding"] = "cp1252"   # what Windows picks when nobody says otherwise
        return real_open(file, mode, *a, **kw)

    html = demo_page()
    assert any(ord(c) > 255 for c in html), "sample page has no characters cp1252 would choke on"
    builtins.open = windows_open
    with tempfile.TemporaryDirectory() as d:
        paths = cp.HTML_PATH, cp.CONFIG_PATH, cp.DATA_PATH, notify.STATE
        cp.HTML_PATH = os.path.join(d, "dashboard.html")
        cp.CONFIG_PATH = os.path.join(d, "config.json")
        cp.DATA_PATH = os.path.join(d, "planner_data.json")
        notify.STATE = os.path.join(d, "notified.json")
        try:
            cp.write_text(cp.HTML_PATH, html)                     # the page itself
            cp.save_token({"base_url": "https://x.edu"}, "9999~t") # config, with settings in it
            cp.write_text(cp.DATA_PATH, json.dumps({"items": [], "note": "café ●"}))
            notify._save_state({"sent": ["café ●"], "seen": None})
            back = real_open(cp.HTML_PATH, encoding="utf-8").read()
            assert back == html, "the page didn't survive the round trip"
        finally:
            builtins.open = real_open
            cp.HTML_PATH, cp.CONFIG_PATH, cp.DATA_PATH, notify.STATE = paths
    return "page, config, data and reminder state"


@check("page swap survives a locked file")
def _swap():
    """Windows refuses to replace a file that anything else has open, and Semester's own web server
    may be reading the page at the moment a refresh rewrites it. Simulate that refusal."""
    import builtins
    real_replace, calls = os.replace, {"n": 0}

    def busy_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:                     # the file is "open elsewhere" for the first tries
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_replace(src, dst)

    with tempfile.TemporaryDirectory() as d:
        page, tmp = os.path.join(d, "dashboard.html"), os.path.join(d, "dashboard.html.tmp")
        cp.write_text(tmp, "hello ● café")
        cp.os.replace = busy_replace
        try:
            assert cp.replace_file(tmp, page), "gave up on a locked file"
        finally:
            cp.os.replace = real_replace
        assert builtins.open(page, encoding="utf-8").read() == "hello ● café", "page came out wrong"
    return f"retried {calls['n'] - 1}x, then swapped"


@check("Windows reminders use a built-in toast")
def _win_notify():
    """Reminders on Windows used to call a PowerShell module (BurntToast) that nobody has installed,
    so they were silently dropped. They must use Windows' own notification API instead."""
    import notify
    seen = {}
    real_system, real_run = notify.platform.system, notify.subprocess.run
    notify.platform.system = lambda: "Windows"
    notify.subprocess.run = lambda cmd, **kw: seen.setdefault("cmd", cmd)
    try:
        notify._notify("Lab 4 due", "today at 11:59 PM — café ●")
    finally:
        notify.platform.system, notify.subprocess.run = real_system, real_run
    cmd = " ".join(seen.get("cmd") or [])
    assert cmd, "nothing was sent on Windows"
    assert "BurntToast" not in cmd, "still depends on a module nobody installs"
    assert "ToastNotificationManager" in cmd, "not using Windows' own notification API"
    assert "café" in cmd, "the text got mangled on the way out"
    return "Windows' own toast API"


@check("missing work is flagged, not hidden")
def _missing():
    """Past-due work you never handed in is the thing a student most needs to see — Google
    Classroom calls it Missing. It used to be dropped from the board entirely."""
    now = datetime.now(timezone.utc)
    past, old_past, future = now - timedelta(days=2), now - timedelta(days=90), now + timedelta(days=2)
    up = ["online_upload"]
    cases = [
        ("past due, nothing handed in", ({}, past, up, False, False), True),
        ("Canvas flagged it missing", ({"missing": True}, past, up, False, False), True),
        ("handed in", ({}, past, up, True, False), False),
        ("graded", ({}, past, up, False, True), False),
        ("not due yet", ({}, future, up, False, False), False),
        ("no due date", ({}, None, up, False, False), False),
        ("paper assignment", ({}, past, ["on_paper"], False, False), False),
        ("nothing to submit", ({}, past, ["none"], False, False), False),
        ("from months ago", ({}, old_past, up, False, False), False),
    ]
    for name, args, want in cases:
        got = cp.is_missing(args[0], args[1], now, args[2], args[3], args[4])
        assert got == want, f"{name}: got {got}, wanted {want}"

    # and it has to survive into the page rather than being filtered out on the way
    items = cp.demo_items(now)
    flagged = [i for i in items if i.get("missing")]
    assert flagged, "demo data has nothing missing to render"
    page = demo_page()
    assert ">Missing<" in page, "the Missing flag never reached the page"
    assert flagged[0]["title"] in page, "missing work was dropped from the board"
    return f"{len(cases)} rules, {len(flagged)} flagged in the sample"


@check("this version explains itself")
def _whats_new():
    """Every release shows the user what changed, in the app, once. A version with no notes would
    update silently, so the release stops here until the notes are written."""
    items = cp.WHATS_NEW.get(cp.VERSION)
    assert items, f"no what's-new notes for {cp.VERSION} — add them to WHATS_NEW in canvas_planner.py"
    jargon = ("API", "endpoint", "UTF-8", "cp1252", "PowerShell", "universal2", "strftime", "CSS", "localStorage")
    for t in items:
        assert 20 <= len(t) <= 160, f"too short or too long for a one-liner: {t!r}"
        assert t[0].isupper() and t.endswith("."), f"write it as a sentence: {t!r}"
        bad = [w for w in jargon if w.lower() in t.lower()]
        assert not bad, f"say it in plain words instead of {bad}: {t!r}"
    return f"{len(items)} notes for {cp.VERSION}"


@check("Mac reminders come from Semester")
def _mac_notify_path():
    """Reminders should post through the system API, so they carry Semester's name and icon and
    open the app when clicked. AppleScript is the fallback only — it shows up as Script Editor.
    The danger is a silent drop: posting "succeeds" while macOS shows nothing because permission
    was refused, so the system path must report failure and let the fallback run."""
    import notify
    calls, real_system, real_run, real_mac = [], notify.platform.system, notify.subprocess.run, notify._mac_notify
    notify.platform.system = lambda: "Darwin"
    notify.subprocess.run = lambda cmd, **kw: calls.append(cmd[0])
    try:
        notify._mac_notify = lambda t, b: True            # system API took it
        notify._notify("Lab 4 due", "today at 11:59 PM")
        assert not calls, "fell back to AppleScript even though the system API worked"
        notify._mac_notify = lambda t, b: False           # refused or unavailable
        notify._notify("Lab 4 due", "today at 11:59 PM")
        assert calls == ["osascript"], f"no fallback when the system API couldn't post: {calls}"
    finally:
        notify.platform.system, notify.subprocess.run, notify._mac_notify = real_system, real_run, real_mac
    assert real_mac("x", "y") is False, "from a plain interpreter this must decline — a bare Python posts as \"Python\""
    return "system API first, AppleScript only as fallback"


@check("Canvas text can't run PowerShell")
def _win_notify_injection():
    """Notification titles are Canvas's words, not ours. Windows builds a PowerShell command out
    of them, and a double-quoted PowerShell string interpolates: an assignment named
    "Lab 4 $(...)" ran the subexpression as the user. Text has to be single-quoted."""
    import notify
    payload = "Lab 4 $(1+1) `whoami` it's due"
    seen = {}
    real_system, real_run = notify.platform.system, notify.subprocess.run
    notify.platform.system = lambda: "Windows"
    notify.subprocess.run = lambda cmd, **kw: seen.setdefault("cmd", cmd)
    try:
        notify._notify(payload, payload)
    finally:
        notify.platform.system, notify.subprocess.run = real_system, real_run

    cmd = " ".join(seen.get("cmd") or [])
    assert cmd, "nothing was sent on Windows"
    assert 'CreateTextNode("' not in cmd, "notification text is double-quoted; PowerShell would interpolate it"
    assert "$(1+1)" in cmd, "the title never reached the command"
    assert "it''s due" in cmd, "an apostrophe in the title isn't escaped for PowerShell"

    if sys.platform == "win32":   # on the platform that matters, prove it stays inert
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-Command", f"Write-Output {notify._ps_literal(payload)}"],
                             capture_output=True, text=True).stdout.strip()
        assert out == " ".join(payload.split()), f"PowerShell altered the text: {out!r}"
        return "stays literal; ran it to confirm"
    return "text is single-quoted"


@check("the local API refuses other sites")
def _local_api_guards():
    """The app's own server holds a Canvas token, so a page on any site could once POST to it
    (submit work, send a message, log you out), and a rebound DNS name could read the replies.
    Two header checks close both; this pins them so they can't quietly come off."""
    import app as appmod
    H = appmod.Handler

    class Fake:
        LOCAL_HOSTS = H.LOCAL_HOSTS
        def __init__(self, headers, port=8765):
            self.headers = headers
            self.server = type("S", (), {"server_address": ("127.0.0.1", port)})()

    host = lambda h, port=8765: H._host_ok(Fake(h, port))
    origin = lambda h, port=8765: H._origin_ok(Fake(h, port))

    # DNS rebinding: the browser calls it same-origin, only the Host header gives it away.
    assert not host({"Host": "attacker.example"}), "answers to a foreign Host"
    assert not host({"Host": "evil.example:8765"}), "answers to a foreign Host with a port"
    assert host({"Host": "127.0.0.1:8765"}), "refuses its own address"
    assert host({"Host": "localhost:8765"}), "refuses localhost"
    assert host({}), "refuses a client that sends no Host"

    # Cross-site writes.
    assert not origin({"Origin": "https://evil.example"}), "accepts a write from another site"
    assert not origin({"Origin": "null"}), "accepts a write from a null origin"
    assert not origin({"Origin": "http://127.0.0.1:9999"}), "accepts a write from another local app"
    assert not origin({"Sec-Fetch-Site": "cross-site"}), "accepts a cross-site fetch"
    assert origin({"Origin": "http://127.0.0.1:8765"}), "refuses its own page"
    assert origin({"Origin": "http://localhost:8765"}), "refuses its own page as localhost"
    assert origin({}), "refuses the CLI and the MCP connector, which send no Origin"

    return "cross-site writes and foreign Hosts blocked"


@check("assignment HTML can't run scripts")
def _desc_sanitized():
    """A description is a teacher's HTML and the dashboard renders it as HTML, so it is the one
    place Canvas's words become markup. The page shares an origin with the local API, so script
    here could drive it. Formatting has to survive; nothing executable may."""
    tab = chr(9)
    attacks = ["<script>alert(1)</script>",
               "<img src=x onerror=alert(1)>",
               '<a href="javascript:alert(1)">click</a>',
               '<a href="JaVaScRiPt:alert(1)">case</a>',
               '<a href="java' + tab + 'script:alert(1)">padded</a>',
               "<svg/onload=alert(1)>",
               '<iframe src="javascript:alert(1)"></iframe>',
               "<body onload=alert(1)>",
               '<div style="background:url(javascript:alert(1))">x</div>',
               '<a href="data:text/html;base64,PHNjcmlwdD4=">data</a>',
               '<form action="/api/logout"><button>x</button></form>',
               "<input onfocus=alert(1) autofocus>",
               "<template><script>alert(1)</script></template>",
               "<math><mtext><script>alert(1)</script></mtext></math>"]
    banned = ("<script", "onerror", "onload", "onfocus", "javascript:", "data:text/html",
              "<iframe", "<svg", "<object", "<form", "<input", "<style", "<math", "style=")
    for a in attacks:
        out = cp.sanitize_html(a).lower()
        hit = [b for b in banned if b in out]
        assert not hit, f"{a!r} survived as {out!r} ({', '.join(hit)})"

    # ...and the formatting teachers actually use is still there.
    keep = cp.sanitize_html('<p>Read <b>ch 4</b>.</p><ul><li>A</li></ul>'
                            '<a href="https://example.edu/r.pdf">Rubric</a>'
                            '<img src="https://example.edu/d.png" alt="D">')
    for must in ("<p>", "<b>ch 4</b>", "<ul>", "<li>A</li>",
                 'href="https://example.edu/r.pdf"', 'alt="D"'):
        assert must in keep, f"sanitizing removed {must!r}"

    # ...and it is actually wired into the render path, not merely available to call.
    now = datetime.now(timezone.utc)
    items = cp.demo_items(now)
    for it in items:                       # every item: the board drops some of them
        it["description"] = "<p>ok</p><script>alert(1)</script><img src=x onerror=alert(2)>"
    names = sorted({i["course"] for i in items})
    colors = {n: cp.COURSE_PALETTE[k % len(cp.COURSE_PALETTE)] for k, n in enumerate(names)}
    crs = [{"name": n, "url": "#", "syllabus_url": "#", "syllabus": "", "pending": 1} for n in names]
    page = cp.render_html(items, cp.workload_warnings(items, now, threshold=2), crs, [], colors, now)
    assert "<script>alert" not in page, "a raw <script> from a description reached the dashboard"
    assert "onerror" not in page.lower(), "a raw event handler from a description reached the dashboard"
    return f"{len(attacks)} payloads blocked, formatting kept, wired into the page"


@check("setup shows one of their own classes")
def _preview():
    """The sample card on the setup screen should be their own next assignment, not a made-up one."""
    import app
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import fake_canvas
    srv, base = fake_canvas.serve()
    try:
        card = app.setup_preview({"base_url": base, "token": "9999~" + "t" * 64})
        empty = app.setup_preview({"base_url": base, "token": "wrong"})   # Canvas says no: keep the sample
    finally:
        srv.shutdown()
    assert card.get("course") == "Intro to Programming", f"course came out as {card.get('course')!r}"
    assert card.get("title") == "Lab 4: Loops" and card.get("points") == "20 pts", card
    assert card.get("due", "").startswith("Due "), card
    assert empty == {}, "a failed lookup should leave the sample card alone"
    return f"{card['course']} · {card['title']} · {card['due']}"


@check("token creation handles school limits")
def _tokens():
    """The paths that broke before: a school that demands an expiry date, one that caps how far
    out it can be, and one that forbids student tokens outright."""
    import app
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import fake_canvas
    out = []
    for mode, expect in (({"require_expiry": True}, "makes a token"), ({"block": True}, "refuses cleanly")):
        srv, base = fake_canvas.serve(**mode)
        try:
            existing = "9999~" + "t" * 64      # the fake's pre-issued token
            made = app._create_token(base, existing, 365)   # ask for longer than the 90-day cap
            if mode.get("block"):
                assert made is None, "a school that forbids tokens should return nothing, not raise"
            else:
                assert made and made.get("visible_token"), "no token came back"
                left = cp.parse_dt(made["expires_at"]) - datetime.now(timezone.utc)
                assert left < timedelta(days=91), f"expiry ignored the school's cap: {left.days} days"
                ok, _ = cp.validate_token(base, made["visible_token"])
                assert ok, "the new token doesn't work"
            out.append(expect)
        finally:
            srv.shutdown()
    return "; ".join(out)


@check("reminders respect quiet hours")
def _notify():
    import notify
    now = datetime.now().astimezone()
    due = (now + timedelta(hours=3)).astimezone(timezone.utc)
    items = [{"uid": f"u{i}", "course": "MATH 292", "title": f"Lab {i}", "type": "assignment",
              "due": due.isoformat(), "submitted": False, "url": "#"} for i in range(4)]
    sent = []
    with tempfile.TemporaryDirectory() as d:
        # Point both the data and the "already sent" file at throwaway copies, so this never reads
        # or writes the real ones — otherwise the check passes once and fails every run after.
        path, cp.DATA_PATH = cp.DATA_PATH, os.path.join(d, "planner_data.json")
        state, notify.STATE = notify.STATE, os.path.join(d, "notified.json")
        try:
            with open(cp.DATA_PATH, "w") as f:
                json.dump({"items": items}, f)
            quiet = {"notify_due": True, "notify_due_when": ["hours3"], "notify_quiet": True,
                     "notify_quiet_from": (now.hour - 1) % 24, "notify_quiet_to": (now.hour + 1) % 24,
                     "seen": {}}
            notify.check(quiet, now, send=lambda t, m: sent.append((t, m)))
            assert not sent, f"sent {len(sent)} during quiet hours"
            loud = dict(quiet, notify_quiet=False, seen={})
            notify.check(loud, now, send=lambda t, m: sent.append((t, m)))
        finally:
            cp.DATA_PATH, notify.STATE = path, state
    assert sent, "nothing sent outside quiet hours"
    assert len(sent) == 1, f"4 deadlines should bundle into 1 notification, got {len(sent)}"
    return "quiet hours hold; 4 deadlines bundle into 1"


@check("no secrets in tracked files")
def _secrets():
    tracked = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True, text=True).stdout.split()
    # Any Canvas-shaped token, plus two that leaked before. Those two are spelled in pieces so this
    # file doesn't match itself once it's committed.
    old = ("T6D" + "fvrGek", "rP6" + "RwJx")
    pat = re.compile(r"\b\d{4,5}~[A-Za-z0-9]{40,}|" + "|".join(old))
    hits = []
    for rel in tracked:
        p = os.path.join(HERE, rel)
        if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
            continue
        try:
            text = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for m in pat.finditer(text):
            if "9999~" in m.group(0) or "1234~" in m.group(0):
                continue                      # the fake Canvas's made-up tokens
            hits.append(f"{rel}: {m.group(0)[:12]}…")
    assert not hits, "possible token committed -> " + ", ".join(hits)
    return f"{len(tracked)} files clean"


# ---------------------------------------------------------------- plumbing



def main():
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    failed = []
    print(f"Semester {cp.VERSION} · {sys.platform} · Python {sys.version.split()[0]}\n")
    for name, fn in CHECKS:
        if only and not any(o.lower() in name.lower() for o in only):
            continue
        start = time.time()
        try:
            note = fn() or ""
            print(f"  ok    {name:34s} {note}  ({time.time() - start:.1f}s)")
        except Exception as e:
            failed.append(name)
            first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            print(f"  FAIL  {name:34s} {first}")
    print()
    if failed:
        print(f"{len(failed)} failed: {', '.join(failed)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
