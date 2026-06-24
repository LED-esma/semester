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
