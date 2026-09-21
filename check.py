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
              "due": (now + timedelta(days=2)).isoformat(), "submitted": False, "url": "#"}]
    with tempfile.TemporaryDirectory() as d:
        path, cp.DATA_PATH = cp.DATA_PATH, os.path.join(d, "planner_data.json")
        try:
            with open(cp.DATA_PATH, "w") as f:
                json.dump({"items": items}, f)
            ics = cp.build_ics()
        finally:
            cp.DATA_PATH = path
    assert ics and ics.startswith("BEGIN:VCALENDAR"), "no feed produced"
    assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT") == 1, "event count is wrong"
    assert "UID:a1@semester" in ics, "events need a stable id or calendars duplicate them"
    assert ics.endswith("END:VCALENDAR\r\n") and "\r\n" in ics, "iCalendar needs CRLF line endings"
    return "1 event, stable ids"


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


@check("the page's data describes the whole dashboard")
def _page_data_endpoint():
    """The page no longer ships pre-drawn panels: it ships this payload and views.js draws
    from it, and /api/data hands the same object to anything that is not the page. So the
    payload IS the dashboard, and a missing piece here is a blank tab rather than a stack
    trace. Check what the page promises the browser it will find."""
    now = datetime.now(timezone.utc)
    items = cp.demo_items(now)
    names = sorted({i["course"] for i in items})
    colors = {n: cp.COURSE_PALETTE[k % len(cp.COURSE_PALETTE)] for k, n in enumerate(names)}
    crs = [{"name": n, "url": "#", "syllabus_url": "#", "syllabus": "", "pending": 1} for n in names]
    ann = [{"course": names[0], "title": "Welcome", "url": "#", "posted": now.isoformat(), "preview": "Hi."}]

    out = {}
    page = cp.render_html(items, cp.workload_warnings(items, now, threshold=2), crs, ann, colors, now,
                          data_out=out)

    for key in ("generated", "built", "updated", "version", "accent", "buckets", "counts",
                "items", "kanban", "grades", "classes", "courses", "announcements",
                "courseCards", "warnings"):
        assert key in out, f"the payload is missing {key!r}"

    # The page carries the endpoint's object verbatim — not a second copy built another way.
    m = re.search(r'<script type="application/json" id="pagedata">(.*?)</script>', page, re.S)
    assert m, "the page no longer embeds its data"
    assert json.loads(m.group(1)) == out, "the page and the endpoint disagree"

    # Nothing left half-substituted, or the browser gets a literal __JS__ where a script goes.
    for hole in ("__CSS__", "__JS__", "__DATA__", "__ACCENT__", "__PAGE_BUILT__", "__COURSES__"):
        assert hole not in page, f"{hole} was never filled in"
    for must in ('id="pagedata"', "function renderViews", "function startApp(",
                 'id="todo"', 'id="disc"', 'id="ann"', 'id="crs"'):
        assert must in page, f"the page is missing {must!r}"

    # Every card the board draws must point at an item that is really there.
    for k in out["kanban"]:
        assert 0 <= k["did"] < len(out["items"]), f"{k['title']!r} points past the end of the list"
        assert out["items"][k["did"]]["uid"] == k["uid"], f"{k['title']!r} points at the wrong item"

    buckets = {b[0] for b in out["buckets"]}
    for it in out["items"]:
        assert it["bucket"] in buckets, f"{it['title']!r} is in unknown group {it['bucket']!r}"
        assert it["bucket"] != "overdue", f"{it['title']!r} is overdue and should not be here"

    c = out["counts"]
    assert c["todo"] == sum(1 for i in out["items"] if not i["submitted"]), "the to-do count is wrong"
    assert c["disc"] == sum(1 for i in out["items"]
                            if i["graded"] and i["type"] == "discussion" and not i["submitted"]), \
        "the discussions count is wrong"
    assert c["ann"] == len(out["announcements"]), "the announcements count is wrong"
    assert c["crs"] == len(out["courseCards"]), "the courses count is wrong"

    # The unit is added where the number is shown, so the field must not already carry one.
    for it in out["items"]:
        assert " pts" not in it["points"], f"{it['title']!r} carries its unit into the payload"

    assert out["items"], "no items reached the page"
    return f"{len(out['items'])} items, {len(out['kanban'])} on the board, 15 keys, counts agree"


@check("Canvas links can't run scripts")
def _links_guarded():
    """A link from Canvas becomes an href directly. The HTML sanitizer never sees one — it only
    handles assignment descriptions — so nothing else stands between a teacher-editable field and
    the page. Feed every route a URL takes into the payload a javascript: URL, including the class
    pages, whose data is passed straight through from build_classes."""
    now = datetime.now(timezone.utc)
    evil = "javascript:alert(1)"
    items = cp.demo_items(now)
    for it in items:
        it["url"] = evil
    names = sorted({i["course"] for i in items})
    colors = {n: cp.COURSE_PALETTE[k % len(cp.COURSE_PALETTE)] for k, n in enumerate(names)}
    good = "https://dvc.instructure.com/courses/1/discussion_topics/2?x=1#reply"
    ann = [{"course": names[0], "title": "Welcome", "url": good,
            "posted": now.isoformat(), "preview": "Hi."}]
    crs = [{"name": n, "url": evil, "syllabus_url": evil, "syllabus": "", "pending": 1,
            "modules": [{"name": "Week 1", "items": [{"title": "Reading", "url": evil}]}],
            "files": [{"name": "syllabus.pdf", "url": evil}],
            "pages": [{"title": "Notes", "url": evil}]} for n in names]
    classes = [{"id": 1, "name": names[0], "url": evil,
                "stream": [{"title": "Post", "posted": now.isoformat(), "preview": "x", "url": evil}],
                "modules": [{"name": "M", "items": [{"title": "t", "type": "Page", "url": evil}]}],
                "quizzes": [{"title": "Q", "points": 5, "url": evil}],
                "pages": [{"title": "P", "url": evil}],
                "files": [{"name": "f.pdf", "url": evil, "size": 10}],
                "grades": []}]

    out = {}
    page = cp.render_html(items, [], crs, ann, colors, now, grades=[], classes=classes, data_out=out)

    def links(v, path="payload"):
        if isinstance(v, dict):
            for k, x in v.items():
                if k.lower().endswith("url") and isinstance(x, str):
                    yield f"{path}.{k}", x
                else:
                    yield from links(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                yield from links(x, f"{path}[{i}]")

    found = list(links(out))
    assert found, "no link fields found — this check would pass on an empty payload"
    bad = [f"{p}={u!r}" for p, u in found
           if u.lower().startswith(("javascript:", "data:", "vbscript:"))]
    assert not bad, "a script URL survived -> " + "; ".join(bad)
    assert "javascript:" not in page, "a script URL reached the page some other way"

    # ...and a real Canvas link still works, query string, fragment and all. A guard that
    # blocks everything would pass every assertion above and ship a dashboard of dead links.
    assert out["announcements"][0]["url"] == good,         f"a normal link was mangled -> {out['announcements'][0]['url']!r}"
    return f"{len(found)} link fields guarded, real links intact"


@check("the setup screen renders from its file")
def _setup_page():
    """setup.html and create_token.js used to be Python string constants that could not fail.
    They are bundled files now, so a packaging miss would break the first screen a new user
    sees. This draws every variant the app can serve."""
    import app as appmod
    variants = {
        "first run":  appmod.setup_html(),
        "reauth":     appmod.setup_html(reauth=True),
        "pre-filled": appmod.setup_html(base_url="https://4cd.instructure.com"),
    }
    for name, html in variants.items():
        assert html.startswith("<!DOCTYPE html>"), f"{name}: not a page"
        assert 'id="token"' in html, f"{name}: no token field"
        assert "</html>" in html, f"{name}: truncated"
        assert len(html) > 10_000, f"{name}: suspiciously small ({len(html)} bytes)"
    arm = "<script>window.__REAUTH__ = true;"   # the injection, not the page's own reference to it
    assert arm in variants["reauth"], "reauth mode did not arm"
    assert arm not in variants["first run"], "reauth armed when it should not"
    assert "4cd.instructure.com" in variants["pre-filled"], "the school URL was not pre-filled"

    # a quoted school URL must not break out of the attribute it lands in
    nasty = appmod.setup_html(base_url='" onload="alert(1)')
    assert 'onload="alert(1)"' not in nasty, "the pre-filled URL escaped its attribute"

    assert "users/self/tokens" in cp._load_static("create_token.js"), "sign-in script missing"
    return f"{len(variants)} variants, {len(variants['first run']) // 1024} KB"


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


@check("the page's own files are committed")
def _static_tracked():
    """static/dashboard.html was invisible to git for a minute: .gitignore said "dashboard.html",
    meaning the generated page, and quietly matched the template it is generated from too. Nothing
    fails locally when that happens — the file is right there — and the release is broken instead.
    Everything the page is built from has to be a file a fresh clone gets."""
    tracked = set(subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True, text=True).stdout.split())
    if not tracked:
        return "not a git checkout, skipped"
    needed = ["static/" + n for n in os.listdir(os.path.join(HERE, "static"))]
    missing = [rel for rel in needed if rel not in tracked]
    assert not missing, "bundled but not committed -> " + ", ".join(missing)
    return f"{len(needed)} bundled files, all tracked"

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
