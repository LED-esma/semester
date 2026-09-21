#!/usr/bin/env python3
"""
Semester
========
Pulls assignments + discussions from Canvas via the REST API and builds a
friendly planner dashboard (dashboard.html) plus a machine-readable data file
(planner_data.json) used by the calendar feed and the Claude connector.

Features
  - Urgency grouping (Overdue / Today / This week / Later / No due date)
  - Suggested START dates (back-calculated from due date + estimated effort)
  - Workload warnings (days where several things pile up)
  - Submission status (so finished work drops off your radar)

Usage
  python3 canvas_planner.py            # fetch + build dashboard
  python3 canvas_planner.py --open     # also open the dashboard in your browser

Setup
  Copy config.example.json -> config.json and fill in:
    base_url : e.g. "https://dvc.instructure.com"  (your school's Canvas URL)
    token    : a Canvas personal access token
               (Account -> Settings -> "+ New Access Token")
"""

import json
import os
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.26"  # keep in sync with the latest GitHub release tag

# Windows opens text files (and its console) as cp1252, which cannot hold a bullet, an em dash,
# or an accented course name. Everything Semester writes is utf-8, stated explicitly.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def write_text(path, text):
    """Write a text file as utf-8. Every page and feed Semester saves goes through here."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def replace_file(tmp_path, path):
    """Swap a freshly written file into place. On Windows this fails outright while anything else
    has the file open — and the app's own web server may be reading the page at that moment — so
    give it a few tries before falling back to a plain copy."""
    for attempt in range(10):
        try:
            os.replace(tmp_path, path)
            return True
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    try:
        with open(tmp_path, encoding="utf-8") as src:
            write_text(path, src.read())
        os.remove(tmp_path)
        return True
    except OSError:
        return False


def _data_dir():
    """Where config + generated files live. Next to the script normally, but a
    user folder when bundled as a frozen app (the bundle dir is read-only)."""
    if getattr(sys, "frozen", False):
        d = os.path.join(os.path.expanduser("~"), ".semester")
        os.makedirs(d, exist_ok=True)
        return d
    return HERE


DATA_DIR = _data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
HTML_PATH = os.path.join(DATA_DIR, "dashboard.html")
DATA_PATH = os.path.join(DATA_DIR, "planner_data.json")
PAGE_DATA_PATH = os.path.join(DATA_DIR, "dashboard_data.json")
OVERRIDES_PATH = os.path.join(DATA_DIR, "overrides.json")  # {assignment_id: planner_override_id}
NOTES_PATH = os.path.join(DATA_DIR, "notes.json")          # {assignment_id: planner_note_id}


def _load_map(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def _save_map(path, m):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        json.dump(m, open(path, "w", encoding="utf-8"))
    except Exception:
        pass


def load_overrides():
    return _load_map(OVERRIDES_PATH)


def save_overrides(m):
    _save_map(OVERRIDES_PATH, m)


def load_notes():
    return _load_map(NOTES_PATH)


def save_notes(m):
    _save_map(NOTES_PATH, m)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def first_run_setup():
    """Interactively create config.json on first run (if we have a terminal)."""
    if not sys.stdin.isatty():
        sys.exit(
            "No config.json found.\n"
            "  1. cp config.example.json config.json\n"
            "  2. Add your Canvas base_url and a personal access token.\n"
            "     (Canvas -> Account -> Settings -> '+ New Access Token')\n"
            "Or run this script in a terminal to be guided through setup."
        )
    print("\nWelcome to Semester — let's set you up (takes ~2 min).\n")
    print("STEP 1 — Your school's Canvas web address.")
    print("  Look at the URL when you're logged into Canvas, e.g. https://myschool.instructure.com")
    base_url = input("  Canvas URL: ").strip().rstrip("/")
    if base_url and not base_url.startswith("http"):
        base_url = "https://" + base_url
    print("\nSTEP 2 — A personal access token.")
    print("  In Canvas: Account → Settings → scroll to 'Approved Integrations'")
    print("  → '+ New Access Token' → name it 'Semester' → Generate → copy it.")
    token = input("  Paste token: ").strip()
    if not base_url or not token:
        sys.exit("Setup cancelled — both the URL and token are required.")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"base_url": base_url, "token": token}, f, indent=2)
    print(f"\n✓ Saved {CONFIG_PATH} (kept private, never shared).\n")
    return {"base_url": base_url, "token": token}


def save_token(cfg, token):
    """Swap in a new access token, keeping every other setting as-is."""
    cfg = dict(cfg)
    cfg["token"] = token
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def reauth_prompt(cfg):
    """Canvas rejected the token — ask for a new one (terminal), validate, save."""
    base = cfg.get("base_url", "")
    print("\nYour Canvas access token expired or was revoked.")
    print(f"   Get a new one: {base}/profile/settings#access_tokens")
    print("   (Account → Settings → + New Access Token → Generate → copy)\n")
    if not sys.stdin.isatty():
        sys.exit("Re-run Semester in a terminal, or open the app, to paste a new token.")
    for _ in range(3):
        token = input("   Paste new token: ").strip()
        if not token:
            continue
        ok, info = validate_token(base, token)
        if ok:
            print(f"   ✓ Reconnected as {info}.\n")
            return save_token(cfg, token)
        print(f"   ✗ {info}")
    sys.exit("Couldn't validate a new token — try again later.")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return first_run_setup()
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("base_url") or not cfg.get("token") or "PASTE" in cfg["token"]:
        sys.exit("config.json is missing base_url or token. Edit it and try again.")
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    return cfg


# --------------------------------------------------------------------------- #
# Canvas API
# --------------------------------------------------------------------------- #
class TokenExpired(Exception):
    """Canvas returned 401 — the access token expired or was revoked.

    Raised instead of exiting so callers can recover: the app shows a
    'paste a new token' screen, the CLI prompts for one, and the MCP
    server returns a clear message. Never kill the process over this.
    """


class Canvas:
    def __init__(self, base_url, token):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self._cache = None

    def enable_cache(self):
        """Memoize GETs for this client's lifetime (one dashboard build)."""
        self._cache, self._lock = {}, threading.Lock()

    def prefetch(self, calls, workers=6):
        """Warm the cache by running independent fetches concurrently."""
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for f in [pool.submit(c) for c in calls]:
                try:
                    f.result()
                except Exception:
                    pass  # the error is cached; the real caller re-raises it where it always did

    def get(self, path, params=None):
        if self._cache is None:
            return self._get(path, params)
        key = (path, json.dumps(params, sort_keys=True, default=str))
        with self._lock:
            hit = self._cache.get(key)
        if hit is None:
            try:
                hit = ("ok", self._get(path, params))
            except Exception as e:
                hit = ("err", e)
            with self._lock:
                self._cache[key] = hit
        if hit[0] == "err":
            raise hit[1]
        return hit[1]

    def _get(self, path, params=None):
        """GET with automatic pagination (follows Link rel=next)."""
        url = f"{self.base_url}/api/v1{path}"
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out = []
        while url:
            r = self.session.get(url, params=params, timeout=30)
            if r.status_code == 401:
                raise TokenExpired("Canvas rejected the access token (401).")
            r.raise_for_status()
            data = r.json()
            out.extend(data if isinstance(data, list) else [data])
            url = r.links.get("next", {}).get("url")
            params = None  # next URL already carries query params
        return out

    def post(self, path, data):
        return self.session.post(f"{self.base_url}/api/v1{path}", json=data, timeout=30)

    def put(self, path, data):
        return self.session.put(f"{self.base_url}/api/v1{path}", json=data, timeout=30)

    def mark_done(self, assign_id, done=True):
        """Mark an assignment complete in Canvas's planner. We remember the override
        id we create (keyed by assignment) so toggling works even when Canvas stores
        the override under a different plannable type (e.g. graded discussions)."""
        m = load_overrides()
        oid = m.get(str(assign_id))
        if oid:
            return self.put(f"/planner/overrides/{oid}", {"marked_complete": done}).status_code in (200, 201)
        if not done:
            return True  # nothing to undo
        r = self.post("/planner/overrides",
                      {"plannable_type": "assignment", "plannable_id": assign_id, "marked_complete": True})
        if r.status_code in (200, 201):
            try:
                m[str(assign_id)] = r.json().get("id")
                save_overrides(m)
            except Exception:
                pass
            return True
        # Override already exists (e.g. marked before): reuse an assignment-type one.
        for o in self.get("/planner/overrides"):
            if o.get("plannable_type") == "assignment" and o.get("plannable_id") == assign_id:
                m[str(assign_id)] = o["id"]
                save_overrides(m)
                return self.put(f"/planner/overrides/{o['id']}", {"marked_complete": done}).status_code in (200, 201)
        return False

    def submit(self, course_id, assign_id, sub_type, content):
        """Submit an assignment (online_text_entry or online_url)."""
        body = {"submission": {"submission_type": sub_type}}
        body["submission"]["url" if sub_type == "online_url" else "body"] = content
        r = self.post(f"/courses/{course_id}/assignments/{assign_id}/submissions", body)
        if r.status_code in (200, 201):
            return True, ""
        try:
            return False, (r.json().get("errors") or r.text)
        except Exception:
            return False, f"HTTP {r.status_code}"

    def save_note(self, assign_id, text, title="Semester note", todo_date=None):
        """Create or update a Canvas planner note (syncs to Canvas's planner). Canvas
        blocks students from linking notes to an assignment, so we track the note id
        ourselves (keyed by assignment) for prefill + in-place edits."""
        m = load_notes()
        nid = m.get(str(assign_id))
        if nid:
            return self.put(f"/planner_notes/{nid}", {"details": text, "title": title}).status_code in (200, 201)
        data = {"title": title or "Semester note", "details": text,
                "todo_date": todo_date or datetime.now(timezone.utc).date().isoformat()}
        r = self.post("/planner_notes", data)
        if r.status_code in (200, 201):
            try:
                m[str(assign_id)] = r.json().get("id")
                save_notes(m)
            except Exception:
                pass
            return True
        return False

    def active_courses(self):
        courses = self.get(
            "/courses",
            {"enrollment_state": "active",
             "include[]": ["term", "syllabus_body", "public_description", "total_scores"]},
        )
        # Canvas sometimes returns access-restricted stubs without a name.
        return [c for c in courses if c.get("name")]

    def assignments(self, course_id):
        return self.get(
            f"/courses/{course_id}/assignments",
            {"include[]": "submission", "order_by": "due_at"},
        )

    def discussions(self, course_id):
        return self.get(f"/courses/{course_id}/discussion_topics")

    def assignment_groups(self, course_id):
        return self.get(f"/courses/{course_id}/assignment_groups", {"per_page": 100})

    def modules(self, course_id):
        return self.get(f"/courses/{course_id}/modules", {"include[]": "items", "per_page": 100})

    def quizzes(self, course_id):
        return self.get(f"/courses/{course_id}/quizzes", {"per_page": 100})

    def files(self, course_id):
        return self.get(f"/courses/{course_id}/files", {"per_page": 100})

    def pages(self, course_id):
        return self.get(f"/courses/{course_id}/pages", {"per_page": 100})

    def submissions(self, course_id):
        """Your submissions for a course, with feedback comments + rubric scores."""
        return self.get(
            f"/courses/{course_id}/students/submissions",
            {"student_ids[]": "self", "include[]": ["submission_comments", "rubric_assessment"]},
        )

    def announcements(self, course_id):
        return self.get(
            f"/courses/{course_id}/discussion_topics",
            {"only_announcements": "true"},
        )


# --------------------------------------------------------------------------- #
# Planning logic
# --------------------------------------------------------------------------- #
def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


import re
import html as _html


import hashlib


def item_uid(url, course, title, kind):
    """Stable id for an item so drag placements persist across refreshes."""
    base = url or f"{kind}|{course}|{title}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def clean_html(s, base_url="", max_len=30000):
    """Light sanitize of Canvas description HTML for safe inline display:
    drop scripts/styles/iframes + inline event handlers, and make Canvas
    relative links/images absolute so they resolve from the local file."""
    if not s:
        return ""
    s = re.sub(r"<(script|style|iframe)\b[^>]*>.*?</\1>", "", s, flags=re.I | re.S)
    s = re.sub(r"\son\w+\s*=\s*\"[^\"]*\"", "", s, flags=re.I)
    s = re.sub(r"\son\w+\s*=\s*'[^']*'", "", s, flags=re.I)
    if base_url:
        s = re.sub(r'(href|src)\s*=\s*"(/[^"]*)"', rf'\1="{base_url}\2"', s)
        s = re.sub(r"(href|src)\s*=\s*'(/[^']*)'", rf"\1='{base_url}\2'", s)
    return s[:max_len]


from html.parser import HTMLParser as _HTMLParser

# An assignment description is a teacher's HTML, and the dashboard renders it as HTML so
# formatting survives. That makes it the one place Canvas's words become markup, and the page
# shares an origin with the local API, so a <script> in a description could drive it. Keep the
# formatting; allow nothing that executes.
_OK_TAGS = {"p", "br", "hr", "b", "strong", "i", "em", "u", "s", "sub", "sup", "small",
            "ul", "ol", "li", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6",
            "blockquote", "pre", "code", "span", "div", "a", "img",
            "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"}
_VOID_TAGS = {"br", "hr", "img"}
_OK_ATTRS = {"a": {"href", "title"}, "img": {"src", "alt", "title", "width", "height"},
             "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan", "scope"}}
_DROP_WHOLE = {"script", "style", "noscript", "template", "iframe", "frame", "frameset",
               "object", "embed", "applet", "form", "input", "button", "select", "textarea",
               "svg", "math", "link", "meta", "base"}


def _safe_url(u):
    """http, https, mailto, or a relative link. Blocks javascript: and data:, including the
    versions padded with control characters or newlines that browsers still follow."""
    v = "".join(c for c in str(u or "") if c > " " and c != chr(127))
    head = v.split("/")[0].split("?")[0].split("#")[0].lower()
    if ":" in head:
        return v if head.startswith(("http:", "https:", "mailto:")) else ""
    return v


def _safe_links(value):
    """Run every URL in the payload through _safe_url, wherever it sits.

    A link from Canvas goes straight into an href without meeting the HTML sanitizer, which
    only ever sees assignment descriptions. Course names, module items, files and announcements
    are teacher-editable fields on a school's own site, so this is the difference between
    "unlikely" and "cannot". Keyed on the field name rather than a list of paths, so a URL added
    to the payload later is covered the day it appears instead of the day someone remembers it.
    """
    if isinstance(value, dict):
        return {k: (_safe_url(v) if k.lower().endswith("url") and isinstance(v, str)
                    else _safe_links(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_links(v) for v in value]
    return value


class _Sanitizer(_HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.muted = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_WHOLE:
            self.muted += 1
            return
        if self.muted or tag not in _OK_TAGS:
            return            # unknown tag: drop the tag, keep what's inside it
        keep = []
        for k, v in attrs:
            k = (k or "").lower()
            if k not in _OK_ATTRS.get(tag, set()):
                continue      # this drops every on* handler and style=
            if k in ("href", "src"):
                v = _safe_url(v)
                if not v:
                    continue
            keep.append(f' {k}="{_html.escape(str(v or ""), quote=True)}"')
        self.out.append(f"<{tag}{''.join(keep)}>")
        if tag not in _VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in _OK_TAGS and tag not in _VOID_TAGS and self.stack and self.stack[-1] == tag:
            self.stack.pop()
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag):
        if tag in _DROP_WHOLE:
            self.muted = max(0, self.muted - 1)
            return
        if self.muted or tag not in _OK_TAGS or tag in _VOID_TAGS:
            return
        if tag in self.stack:
            while self.stack:                      # close anything left open inside it
                open_tag = self.stack.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data):
        if not self.muted:
            self.out.append(_html.escape(data))

    def result(self):
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.stack))


def sanitize_html(s):
    """Canvas HTML with everything executable removed. Safe to put in innerHTML."""
    if not s:
        return ""
    p = _Sanitizer()
    try:
        p.feed(str(s))
        p.close()
    except Exception:
        return _html.escape(html_to_text(s))       # unparseable: show it as plain text
    return p.result()


def html_to_text(s, limit=None):
    """Crude HTML -> plain text for previews (no extra deps)."""
    if not s:
        return ""
    s = re.sub(r"<(br|/p|/div|/li)\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


# A stable color per course so the eye can group at a glance.
COURSE_PALETTE = ["#6366f1", "#10b981", "#f59e0b", "#ec4899", "#06b6d4", "#8b5cf6"]


def course_colors(courses):
    return {c["name"]: COURSE_PALETTE[i % len(COURSE_PALETTE)]
            for i, c in enumerate(courses)}


NON_ACADEMIC = re.compile(r"\b(badge|training|orientation|onboarding)\b", re.I)


def is_non_academic(course):
    """Badge, orientation, and compliance-training shells — not real classes."""
    return bool(NON_ACADEMIC.search(course.get("name") or ""))


def clean_course_name(name):
    """'5070 - Introduction to Programming' -> 'Introduction to Programming'."""
    return re.sub(r"^\d+\s*-\s*", "", name or "").strip()


def lead_days(points, name, kind):
    """How many days BEFORE the due date you should start."""
    name = (name or "").lower()
    big_words = ("project", "essay", "paper", "lab report", "presentation",
                 "research", "portfolio", "exam", "midterm", "final")
    if any(w in name for w in big_words):
        return 5
    p = points or 0
    if p >= 50:
        return 5
    if p >= 20:
        return 3
    if p >= 5:
        return 2
    return 1


_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6, "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2,
             "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_title_due(title, now):
    """Some instructors put the deadline in the title ('(due Thu 6/25 10:00 PM)',
    'Due Friday') and leave Canvas's due_at empty. Recover it. Returns (datetime, approx)."""
    if not title or "due" not in title.lower():
        return None, False
    local_now = now.astimezone()
    tz = local_now.tzinfo
    t = title.lower()
    hour, minute = 23, 59
    tm = re.search(r'(\d{1,2})(?::(\d{2}))?\s*([ap]m)', t)
    if tm:
        h = int(tm.group(1)) % 12
        if tm.group(3) == "pm":
            h += 12
        hour, minute = h, int(tm.group(2) or 0)
    md = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', t)
    if md:
        mo, da, yr = int(md.group(1)), int(md.group(2)), md.group(3)
        year = (int(yr) + 2000 if yr and int(yr) < 100 else int(yr)) if yr else local_now.year
        try:
            dt = datetime(year, mo, da, hour, minute, tzinfo=tz)
        except ValueError:
            return None, False
        if not yr and (local_now - dt).days > 60:   # title had no year; pick the sensible one
            dt = dt.replace(year=year + 1)
        return dt.astimezone(timezone.utc), True
    after = t.split("due", 1)[1]
    for word, wd in _WEEKDAYS.items():
        if re.search(r'\b' + word + r'\b', after):
            ahead = (wd - local_now.weekday()) % 7
            dt = (local_now + timedelta(days=ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            return dt.astimezone(timezone.utc), True
    return None, False


def urgency_bucket(due, now):
    if due is None:
        return "none"
    local_due = due.astimezone()
    days = (local_due.date() - now.astimezone().date()).days
    if days < 0:
        return "overdue"
    if days == 0:
        return "today"
    if days <= 7:
        return "week"
    return "later"


# Start-early aggressiveness → multiplier on the suggested lead time.
AGGRESSIVENESS = {"relaxed": 0.6, "balanced": 1.0, "aggressive": 1.6}


def build_items(canvas, courses, now, aggressiveness="balanced"):
    factor = AGGRESSIVENESS.get(aggressiveness, 1.0)
    lead = lambda d: max(1, round(d * factor))
    # An assignment is 'done' if the override we created for it is marked complete.
    omap = load_overrides()
    try:
        complete = {o.get("id") for o in canvas.get("/planner/overrides") if o.get("marked_complete")}
        done_ids = {int(aid) for aid, oid in omap.items() if oid in complete}
    except Exception:
        done_ids = set()
    nmap = load_notes()  # {assignment_id: planner_note_id}
    try:
        notes_by_id = {n["id"]: n.get("details") or "" for n in canvas.get("/planner_notes")}
        notes_by_assign = {int(aid): notes_by_id.get(nid, "") for aid, nid in nmap.items() if nid in notes_by_id}
    except Exception:
        notes_by_assign = {}
    items = []
    for c in courses:
        cid = c["id"]
        cname = c["name"]
        subs = {}
        try:
            subs = {s.get("assignment_id"): s for s in canvas.submissions(cid)}
        except Exception:
            pass  # feedback is best-effort; never block the build
        # Assignments
        for a in canvas.assignments(cid):
            sub = a.get("submission") or {}
            sf = subs.get(a.get("id")) or {}
            comments = [{"author": cm.get("author_name") or "Instructor", "text": cm.get("comment")}
                        for cm in (sf.get("submission_comments") or []) if cm.get("comment")]
            ra = sf.get("rubric_assessment") or {}
            rubric = []
            for crit in (a.get("rubric") or []):
                got = ra.get(crit.get("id")) or {}
                rubric.append({"desc": crit.get("description"), "max": crit.get("points"),
                               "points": got.get("points"), "comment": got.get("comments")})
            submitted = (bool(sub.get("submitted_at")) or sub.get("workflow_state") == "graded"
                         or a.get("id") in done_ids)
            due = parse_dt(a.get("due_at"))
            approx = False
            if due is None:
                due, approx = parse_title_due(a.get("name"), now)
            ld = lead(lead_days(a.get("points_possible"), a.get("name"), "assignment"))
            start = (due - timedelta(days=ld)) if due else None
            items.append({
                "type": "assignment",
                "course": cname,
                "title": a.get("name") or "(untitled)",
                "url": a.get("html_url"),
                "points": a.get("points_possible"),
                "score": sub.get("score"),
                "graded": sub.get("workflow_state") == "graded",
                "due": due.isoformat() if due else None,
                "start": start.isoformat() if start else None,
                "submitted": submitted,
                "bucket": urgency_bucket(due, now),
                "due_approx": approx,
                "description": clean_html(a.get("description"), canvas.base_url),
                "uid": item_uid(a.get("html_url"), cname, a.get("name") or "", "assignment"),
                "course_id": cid,
                "assign_id": a.get("id"),
                "group_id": a.get("assignment_group_id"),
                "note": notes_by_assign.get(a.get("id"), ""),
                "submission_types": a.get("submission_types") or [],
                "comments": comments,
                "rubric": rubric,
            })
        # Discussions (only those with a due date are actionable; keep others under "none")
        for d in canvas.discussions(cid):
            asg = d.get("assignment") or {}
            due = parse_dt(asg.get("due_at"))
            if d.get("locked") or d.get("locked_for_user"):
                continue
            approx = False
            if due is None:
                due, approx = parse_title_due(d.get("title"), now)
            start = (due - timedelta(days=lead(2))) if due else None
            items.append({
                "type": "discussion",
                "course": cname,
                "title": d.get("title") or "(untitled discussion)",
                "url": d.get("html_url"),
                "points": asg.get("points_possible"),
                "score": None,
                "graded": False,
                "due": due.isoformat() if due else None,
                "start": start.isoformat() if start else None,
                "submitted": bool(d.get("user_can_see_posts") and d.get("read_state") == "read" and asg == {}),
                "bucket": urgency_bucket(due, now),
                "due_approx": approx,
                "description": clean_html(d.get("message"), canvas.base_url),
                "uid": item_uid(d.get("html_url"), cname, d.get("title") or "", "discussion"),
            })
    return items


def merge_quizzes(canvas, courses, items, now):
    """Pull classic quizzes into the planner. Most quizzes already appear via the
    assignments API; this only adds ones that don't (deduped by course + title)."""
    existing = {(i["course"], i["title"].strip().lower()) for i in items}
    for c in courses:
        try:
            quizzes = canvas.quizzes(c["id"])
        except Exception:
            continue
        for q in quizzes:
            title = (q.get("title") or "").strip()
            if not title or (c["name"], title.lower()) in existing:
                continue
            due = parse_dt(q.get("due_at"))
            start = (due - timedelta(days=2)) if due else None
            items.append({
                "type": "quiz", "course": c["name"], "title": title,
                "url": q.get("html_url"), "points": q.get("points_possible"),
                "score": None, "graded": False,
                "due": due.isoformat() if due else None,
                "start": start.isoformat() if start else None,
                "submitted": bool(q.get("locked_for_user")) and due is not None and due < now,
                "bucket": urgency_bucket(due, now),
                "due_approx": False, "description": clean_html(q.get("description"), canvas.base_url),
                "uid": item_uid(q.get("html_url"), c["name"], title, "quiz"),
                "course_id": c["id"], "assign_id": None, "group_id": None,
                "note": "", "submission_types": [], "comments": [], "rubric": [],
            })


def build_grades(canvas, courses, items):
    """Per-course current grade + every gradeable assignment (with group + weight),
    so the dashboard can run a live 'what-if / what do I need' projection."""
    by_course = {}
    for it in items:
        if it["type"] == "assignment" and it.get("points"):
            by_course.setdefault(it["course"], []).append(it)
    out = []
    for c in courses:
        try:
            groups = {g["id"]: g for g in canvas.assignment_groups(c["id"])}
        except Exception:
            groups = {}
        enr = (c.get("enrollments") or [{}])[0]
        gitems = []
        for it in sorted(by_course.get(c["name"], []), key=lambda x: x["due"] or "~"):
            g = groups.get(it.get("group_id")) or {}
            gitems.append({
                "title": clean_course_name(it["title"]) if False else it["title"],
                "score": it.get("score"), "points": it.get("points"),
                "graded": bool(it.get("graded")),
                "group": g.get("name") or "", "weight": g.get("group_weight") or 0,
            })
        out.append({
            "name": c["name"],
            "score": enr.get("computed_current_score"),
            "grade": enr.get("computed_current_grade"),
            "weighted": bool(c.get("apply_assignment_group_weights")),
            "items": gitems,
        })
    return out


def build_classes(canvas, courses, items, announcements, grades):
    """Per-course bundle for the Google-Classroom-style class pages:
    stream (announcements), classwork (modules + quizzes), grades, materials (files + pages)."""
    by_assign = {it["assign_id"]: it for it in items if it.get("assign_id")}
    ann_by_course, grade_by_course = {}, {g["name"]: g for g in (grades or [])}
    for a in announcements:
        ann_by_course.setdefault(a["course"], []).append(a)
    out = []
    for c in courses:
        cid, cname = c["id"], c["name"]

        mods = []
        try:
            for m in canvas.modules(cid):
                mi_out = []
                for mi in (m.get("items") or []):
                    e = {"title": mi.get("title"), "type": mi.get("type"), "url": mi.get("html_url")}
                    a = by_assign.get(mi.get("content_id")) if mi.get("type") == "Assignment" else None
                    if a:
                        e.update({"due_local": fmt_due(a["due"]) if a["due"] else None,
                                  "status": "done" if a["submitted"] else a["bucket"], "points": a.get("points")})
                    mi_out.append(e)
                mods.append({"name": m.get("name"), "items": mi_out})
        except Exception:
            pass

        qz = []
        try:
            for q in canvas.quizzes(cid):
                due = parse_dt(q.get("due_at"))
                qz.append({"title": q.get("title"), "points": q.get("points_possible"), "url": q.get("html_url"),
                           "due": due.isoformat() if due else None, "due_local": fmt_due(due.isoformat()) if due else None})
        except Exception:
            pass

        fls = []
        try:
            for f in canvas.files(cid)[:60]:
                fls.append({"name": f.get("display_name"), "size": f.get("size"),
                            "url": f"{canvas.base_url}/courses/{cid}/files/{f.get('id')}"})
        except Exception:
            pass

        pgs = []
        try:
            for p in canvas.pages(cid)[:60]:
                pgs.append({"title": p.get("title"), "url": p.get("html_url")})
        except Exception:
            pass

        g = grade_by_course.get(cname, {})
        out.append({
            "id": cid, "name": clean_course_name(cname), "url": f"{canvas.base_url}/courses/{cid}",
            "score": g.get("score"), "grade": g.get("grade"),
            "pending": sum(1 for it in items if it["course"] == cname and not it["submitted"]
                           and it["due"] and it["bucket"] != "overdue"),
            "stream": [{"title": a["title"], "posted": a.get("posted"), "preview": a.get("preview"), "url": a.get("url")}
                       for a in ann_by_course.get(cname, [])],
            "modules": mods, "quizzes": qz, "files": fls, "pages": pgs,
            "grades": [{"title": i["title"], "score": i["score"], "points": i["points"]}
                       for i in g.get("items", []) if i.get("graded")],
        })
    return out


def build_announcements(canvas, courses, now, days_back=30):
    """Recent announcements across all courses, newest first."""
    out = []
    cutoff = now - timedelta(days=days_back)
    for c in courses:
        for a in canvas.announcements(c["id"]):
            posted = parse_dt(a.get("posted_at") or a.get("created_at"))
            if posted and posted < cutoff:
                continue
            out.append({
                "course": c["name"],
                "title": a.get("title") or "(untitled)",
                "url": a.get("html_url"),
                "posted": posted.isoformat() if posted else None,
                "preview": html_to_text(a.get("message"), limit=320),
            })
    out.sort(key=lambda x: x["posted"] or "", reverse=True)
    return out


def build_courses(canvas, courses, items):
    """Per-course summary: syllabus, link, and pending count."""
    pending_by_course = {}
    for it in items:
        if not it["submitted"] and it["due"] and it["bucket"] != "overdue":
            pending_by_course[it["course"]] = pending_by_course.get(it["course"], 0) + 1
    base = canvas.base_url
    out = []
    for c in courses:
        cid = c["id"]
        try:
            modules = [{"name": m.get("name"),
                        "items": [{"title": it.get("title"), "url": it.get("html_url") or it.get("url")}
                                  for it in (m.get("items") or [])]}
                       for m in canvas.modules(cid)]
        except Exception:
            modules = []
        try:
            files = [{"name": f.get("display_name") or f.get("filename"),
                      "url": f"{base}/courses/{cid}/files/{f.get('id')}"} for f in canvas.files(cid)]
        except Exception:
            files = []
        try:
            pages = [{"title": p.get("title"), "url": p.get("html_url")} for p in canvas.pages(cid)]
        except Exception:
            pages = []
        out.append({
            "name": c["name"],
            "id": cid,
            "url": f"{base}/courses/{cid}",
            "syllabus_url": f"{base}/courses/{cid}/assignments/syllabus",
            "syllabus": html_to_text(c.get("syllabus_body"), limit=1500),
            "pending": pending_by_course.get(c["name"], 0),
            "modules": modules, "files": files, "pages": pages,
        })
    return out


def workload_warnings(items, now, threshold=3):
    """Flag days (within the next 2 weeks) with >= threshold things due."""
    counts = {}
    for it in items:
        if it["submitted"] or not it["due"]:
            continue
        d = parse_dt(it["due"]).astimezone().date()
        if 0 <= (d - now.astimezone().date()).days <= 14:
            counts[d] = counts.get(d, 0) + 1
    return sorted((str(d), n) for d, n in counts.items() if n >= threshold)


def _ics_escape(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line):
    """RFC 5545 line folding: continuation lines start with a space."""
    out = []
    while len(line) > 73:
        out.append(line[:73])
        line = " " + line[73:]
    out.append(line)
    return "\r\n".join(out)


def build_ics(days_back=7):
    """Live .ics of pending deadlines, read from the last planner_data.json.
    Served by the app at /calendar.ics so calendar apps can subscribe once and
    stay in sync (instead of re-downloading a file after every change)."""
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            items = json.load(f).get("items", [])
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Semester//EN", "CALSCALE:GREGORIAN",
         "X-WR-CALNAME:Semester deadlines", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    seen = set()
    # Sort assignments first so the assignment copy of a graded discussion wins the dedupe.
    for it in sorted(items, key=lambda x: x.get("type", "")):
        due = parse_dt(it.get("due"))
        if not due or it.get("submitted") or (now - due).days > days_back:
            continue
        key = (it.get("course"), (it.get("title") or "").strip().lower(), it.get("due"))
        if key in seen:  # graded discussions appear twice (assignment + discussion)
            continue
        seen.add(key)
        summary = f"Due: {it.get('title') or '(untitled)'} ({clean_course_name(it.get('course'))})"
        L += ["BEGIN:VEVENT",
              f"UID:{it.get('uid') or abs(hash(key))}@semester",
              f"DTSTAMP:{stamp}",
              f"DTSTART:{(due - timedelta(minutes=30)).strftime('%Y%m%dT%H%M%SZ')}",
              f"DTEND:{due.strftime('%Y%m%dT%H%M%SZ')}",
              _ics_fold("SUMMARY:" + _ics_escape(summary)),
              _ics_fold("DESCRIPTION:" + _ics_escape(it.get("url") or "")),
              "TRANSP:TRANSPARENT",
              "END:VEVENT"]
    L.append("END:VCALENDAR")
    return "\r\n".join(L) + "\r\n"


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
# Overdue items are intentionally excluded from the planner (see _page_payload).
BUCKET_META = [
    ("today", "Due Today", "#f97316"),
    ("week", "This Week", "#eab308"),
    ("later", "Later", "#3b82f6"),
    ("none", "No Due Date", "#6b7280"),
]


def strf(dt, fmt):
    """strftime that works everywhere. "%-d"/"%-I" (drop the leading zero) are a Linux and Mac
    extension; on Windows they raise "Invalid format string", so fill those in ourselves first."""
    for code, value in (("%-d", dt.day), ("%-I", (dt.hour - 1) % 12 + 1),
                        ("%-m", dt.month), ("%-H", dt.hour), ("%-M", dt.minute), ("%-S", dt.second)):
        if code in fmt:
            fmt = fmt.replace(code, str(value))
    return dt.strftime(fmt)


def fmt_due(iso):
    if not iso:
        return "—"
    dt = parse_dt(iso).astimezone()
    return strf(dt, "%a %b %-d, %-I:%M %p")


def fmt_start(iso):
    if not iso:
        return ""
    return strf(parse_dt(iso).astimezone(), "%a %b %-d")


def fmt_posted(iso):
    if not iso:
        return ""
    return strf(parse_dt(iso).astimezone(), "%a %b %-d, %-I:%M %p")


def _esc(s):
    return _html.escape(str(s or ""))


def _load_static(name):
    """Read a bundled static asset (works from source and PyInstaller bundles)."""
    base = getattr(sys, "_MEIPASS", HERE)
    with open(os.path.join(base, "static", name), encoding="utf-8") as f:
        return f.read()


def _page_payload(items, warnings, courses_info, announcements, colors, now,
                  accent="#6366f1", grades=None, classes=None):
    """Everything a dashboard is, as one document.

    The page embeds this and /api/data serves it, so there is one description of a semester
    rather than one for the screen and another for everything else. Dates are formatted here,
    because strf() is the only place that knows how to drop a leading zero on Windows too.
    """
    # Overdue items are left out of the planner entirely.
    pending = [i for i in items if not i["submitted"] and i["bucket"] != "overdue"]
    graded = [i for i in items if i["type"] == "discussion"
              and i.get("points") is not None and i["bucket"] != "overdue"]

    # One entry per item, not per card: work that shows up on two screens is described once,
    # and everything else points at it by index.
    seen, registry = {}, []

    def reg(it):
        if id(it) not in seen:
            seen[id(it)] = len(registry)
            registry.append({
                "title": it["title"],
                "course": clean_course_name(it["course"]),
                "type": it["type"],
                "due": fmt_due(it.get("due")),
                "dueIso": it.get("due"),          # for filtering by due date
                "color": colors.get(it["course"], "#6366f1"),
                "start": fmt_start(it.get("start")),
                "points": (f'{it["points"]:g}' if it.get("points") else ""),   # bare; each screen adds the unit
                "url": it.get("url") or "#",
                "desc": sanitize_html(it.get("description")),
                "score": it.get("score"),
                "comments": it.get("comments") or [],
                "rubric": [r for r in (it.get("rubric") or []) if r.get("points") is not None or r.get("comment")],
                "subTypes": it.get("submission_types") or [],
                "courseId": it.get("course_id"),
                "assignId": it.get("assign_id"),
                "submitted": it.get("submitted", False),
                "uid": it.get("uid"),
                "note": it.get("note", ""),
                "bucket": it["bucket"],
                "graded": it.get("points") is not None,   # a graded discussion belongs on that tab
            })
        return seen[id(it)]

    # Registered in reading order: the To-Do tab first, then anything only Discussions shows.
    by_bucket = {b: [] for b, _, _ in BUCKET_META}
    for it in sorted(pending, key=lambda x: (x["due"] is None, x["due"] or "")):
        by_bucket[it["bucket"]].append(it)
    for bucket, _, _ in BUCKET_META:
        for it in by_bucket[bucket]:
            reg(it)
    for it in graded:
        reg(it)

    kanban = [{
        "uid": it["uid"],
        "did": reg(it),
        "title": it["title"],
        "course": clean_course_name(it["course"]),
        "color": colors.get(it["course"], "#6366f1"),
        "due": it["due"],
        "start": it["start"],
        "approx": it.get("due_approx", False),
        "dueLabel": fmt_start(it["due"]),
        "points": (f'{it["points"]:g}' if it.get("points") else ""),
        "type": it["type"],
    } for it in items if not it["submitted"] and it["bucket"] != "overdue"]

    return _safe_links({
        "generated": now.isoformat(),
        "built": int(now.timestamp()),
        "updated": strf(now.astimezone(), "%A, %B %-d at %-I:%M %p"),
        "version": VERSION,
        "accent": accent,
        "buckets": [list(b) for b in BUCKET_META],
        "counts": {
            "todo": len(pending),
            "week": len(by_bucket["today"]) + len(by_bucket["week"]),
            "disc": sum(1 for i in graded if not i["submitted"]),
            "done": sum(1 for i in items if i["submitted"]),
            "ann": len(announcements),
            "crs": len(courses_info),
            "classes": len(classes or []),
        },
        "items": registry,
        "kanban": kanban,
        "grades": [{**g, "color": colors.get(g["name"], "#888"),
                    "cleanName": clean_course_name(g["name"])} for g in (grades or [])],
        "classes": classes or [],
        "courses": [{"id": c.get("id"), "name": clean_course_name(c["name"])} for c in courses_info],
        "announcements": [{
            "course": clean_course_name(a["course"]),
            "color": colors.get(a["course"], "#555"),
            "title": a["title"],
            "url": a.get("url") or "#",
            "preview": a["preview"],
            "postedLabel": fmt_posted(a.get("posted")),
        } for a in announcements],
        "courseCards": [{
            "name": clean_course_name(c["name"]),
            "color": colors.get(c["name"], "#888"),
            "url": c["url"],
            "syllabusUrl": c["syllabus_url"],
            "syllabus": c["syllabus"],
            "pending": c["pending"],
            "modules": [{"name": m.get("name"),
                         "items": [{"title": i.get("title"), "url": i.get("url")}
                                   for i in m.get("items", [])]} for m in c.get("modules", [])],
            "files": [{"title": f.get("name"), "url": f.get("url")} for f in c.get("files", [])],
            "pages": [{"title": p.get("title"), "url": p.get("url")} for p in c.get("pages", [])],
        } for c in courses_info],
        "warnings": [{"label": strf(datetime.fromisoformat(d), "%a %b %-d"), "n": n}
                     for d, n in warnings],
    })


def render_html(items, warnings, courses_info, announcements, colors, now, accent="#6366f1", grades=None, classes=None, data_out=None):
    """The dashboard: the static shell from static/dashboard.html, this build's data, and the
    scripts that draw one from the other. The markup itself lives in static/, not in here."""
    payload = _page_payload(items, warnings, courses_info, announcements, colors, now,
                            accent, grades, classes)
    if data_out is not None:
        data_out.update(payload)

    # Data goes in last: a course named "__JS__" is a course name, not a placeholder.
    # Escaping "</" keeps a title containing "</script>" from ending the tag early.
    return (_load_static("dashboard.html")
            .replace("__CSS__", _load_static("style.css").replace("__ACCENT__", accent))
            .replace("__JS__", _load_static("views.js") + "\n" + _load_static("app.js"))
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", r"<\/")))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def demo_items(now):
    """Fake data so you can preview the dashboard without a Canvas token."""
    def mk(t, course, title, kind, pts, due_days, due_hour, submitted=False):
        due = (now + timedelta(days=due_days)).astimezone().replace(
            hour=due_hour, minute=0, second=0, microsecond=0) if due_days is not None else None
        ld = lead_days(pts, title, kind)
        start = (due - timedelta(days=ld)) if due else None
        return {
            "type": kind, "course": course, "title": title, "url": "#",
            "points": pts,
            "due": due.astimezone(timezone.utc).isoformat() if due else None,
            "start": start.astimezone(timezone.utc).isoformat() if start else None,
            "submitted": submitted, "bucket": urgency_bucket(due, now),
            "uid": item_uid(None, course, title, kind), "description": "",
        }
    return [
        mk(1, "CHEM-120", "Lab Report 3: Titration", "assignment", 50, -1, 23),
        mk(1, "MATH-292 Calc III", "WebAssign 7.4 Problem Set", "assignment", 20, 0, 23),
        mk(1, "ENGL-122", "Discussion: Rhetorical Analysis", "discussion", 15, 0, 23),
        mk(1, "MATH-292 Calc III", "Midterm 2 (study)", "assignment", 100, 4, 9),
        mk(1, "CHEM-120", "Pre-lab Quiz 4", "assignment", 10, 3, 8),
        mk(1, "ENGL-122", "Essay 2: Research Paper", "assignment", 100, 11, 23),
        mk(1, "MATH-292 Calc III", "WebAssign 8.1", "assignment", 20, 6, 23),
        mk(1, "CHEM-120", "Lab Report 2: Stoichiometry", "assignment", 50, -6, 23, submitted=True),
    ]


def validate_token(base_url, token):
    """Check a base_url + token against Canvas. Returns (ok, name_or_error)."""
    base_url = (base_url or "").rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    try:
        r = requests.get(f"{base_url}/api/v1/users/self",
                         headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except Exception as e:
        return False, "Couldn't reach Canvas. Check your internet connection and try again."
    if r.status_code == 401:
        return False, "Canvas rejected that token. Double-check you copied the whole thing."
    if r.status_code != 200:
        return False, f"Canvas returned an error ({r.status_code}). Check the web address."
    return True, r.json().get("name", "there")


def build_dashboard(cfg, now=None, log=lambda *a: None):
    """Fetch everything from Canvas and write dashboard.html + planner_data.json.
    Returns a summary dict. Shared by the CLI and the app."""
    now = now or datetime.now(timezone.utc)
    canvas = Canvas(cfg["base_url"], cfg["token"])
    canvas.enable_cache()

    log("Fetching courses…")
    courses = canvas.active_courses()
    if not cfg.get("show_all_courses"):
        courses = [c for c in courses if not is_non_academic(c)]
    colors = course_colors(courses)
    log("Fetching course data in parallel…")
    per_course = ("submissions", "assignments", "discussions", "quizzes", "assignment_groups",
                  "announcements", "modules", "files", "pages")
    canvas.prefetch([lambda: canvas.get("/planner/overrides"), lambda: canvas.get("/planner_notes")]
                    + [lambda m=m, cid=c["id"]: getattr(canvas, m)(cid) for c in courses for m in per_course])

    log("Fetching assignments + discussions…")
    items = build_items(canvas, courses, now, cfg.get("aggressiveness", "balanced"))
    merge_quizzes(canvas, courses, items, now)
    warnings = workload_warnings(items, now)

    log("Fetching announcements + syllabus…")
    announcements = build_announcements(canvas, courses, now)
    courses_info = build_courses(canvas, courses, items)
    grades = build_grades(canvas, courses, items)
    log("Fetching modules, quizzes, files, pages…")
    classes = build_classes(canvas, courses, items, announcements, grades)

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump({"generated": now.isoformat(), "items": items, "warnings": warnings,
                   "announcements": announcements, "courses": courses_info, "grades": grades}, f, indent=2)

    accent = cfg.get("accent") or "#6366f1"
    page_data = {}
    html = render_html(items, warnings, courses_info, announcements, colors, now, accent, grades, classes,
                       data_out=page_data)
    write_text(PAGE_DATA_PATH + ".tmp", json.dumps(page_data, ensure_ascii=False, indent=2))
    replace_file(PAGE_DATA_PATH + ".tmp", PAGE_DATA_PATH)
    tmp_path = HTML_PATH + ".tmp"
    write_text(tmp_path, html)
    replace_file(tmp_path, HTML_PATH)  # atomic: the app may be serving the previous file right now

    return {
        "pending": sum(1 for i in items if not i["submitted"] and i["bucket"] != "overdue"),
        "overdue": sum(1 for i in items if not i["submitted"] and i["bucket"] == "overdue"),
        "announcements": len(announcements),
        "courses": len(courses_info),
        "warnings": warnings,
    }


def main():
    now = datetime.now(timezone.utc)

    if "--demo" in sys.argv:
        print("Building DEMO dashboard with sample data…")
        items = demo_items(now)
        warnings = workload_warnings(items, now, threshold=2)
        names = sorted({i["course"] for i in items})
        colors = {n: COURSE_PALETTE[i % len(COURSE_PALETTE)] for i, n in enumerate(names)}
        ann = [{"course": names[0], "title": "Welcome to the course!",
                "url": "#", "posted": now.isoformat(),
                "preview": "Office hours are Tue/Thu 2-4pm. The first lab is due Friday — read chapter 1 before then."}]
        crs = [{"name": n, "url": "#", "syllabus_url": "#",
                "syllabus": "Grading: 40% labs, 30% exams, 30% participation. Late work loses 10%/day.",
                "pending": sum(1 for i in items if i["course"] == n and not i["submitted"] and i["due"])}
               for n in names]
        page_data = {}
        html = render_html(items, warnings, crs, ann, colors, now, data_out=page_data)
        write_text(HTML_PATH, html)
        write_text(PAGE_DATA_PATH, json.dumps(page_data, ensure_ascii=False, indent=2))
        print(f"Demo dashboard -> {HTML_PATH}")
        if "--open" in sys.argv:
            webbrowser.open(f"file://{HTML_PATH}")
        return

    cfg = load_config()
    try:
        summary = build_dashboard(cfg, now, log=print)
    except TokenExpired:
        cfg = reauth_prompt(cfg)          # ask for a fresh token, then retry once
        summary = build_dashboard(cfg, now, log=print)
    print(f"\nDone. {summary['pending']} pending ({summary['overdue']} overdue hidden) · "
          f"{summary['announcements']} announcement(s) · {summary['courses']} course(s).")
    print(f"Dashboard -> {HTML_PATH}")
    if summary["warnings"]:
        print("Heavy days:", ", ".join(f"{d} ({n})" for d, n in summary["warnings"]))

    if "--open" in sys.argv:
        webbrowser.open(f"file://{HTML_PATH}")


if __name__ == "__main__":
    main()
