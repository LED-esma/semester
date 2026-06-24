#!/usr/bin/env python3
"""
Semester
========
Pulls assignments + discussions from Canvas via the REST API and builds a
friendly planner dashboard (dashboard.html) plus a machine-readable data file
(planner_data.json) used for Google Calendar sync.

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
import webbrowser
from datetime import datetime, timezone, timedelta

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.2"  # keep in sync with the latest GitHub release tag


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
OVERRIDES_PATH = os.path.join(DATA_DIR, "overrides.json")  # {assignment_id: planner_override_id}
NOTES_PATH = os.path.join(DATA_DIR, "notes.json")          # {assignment_id: planner_note_id}


def _load_map(path):
    try:
        return json.load(open(path))
    except Exception:
        return {}


def _save_map(path, m):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        json.dump(m, open(path, "w"))
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
    print("\n👋 Welcome to Semester — let's set you up (takes ~2 min).\n")
    print("STEP 1 — Your school's Canvas web address.")
    print("  Look at the URL when you're logged into Canvas, e.g. https://myschool.instructure.com")
    base_url = input("  Canvas URL: ").strip().rstrip("/")
    if base_url and not base_url.startswith("http"):
        base_url = "https://" + base_url
    print("\nSTEP 2 — A personal access token.")
    print("  In Canvas: Account → Settings → scroll to 'Approved Integrations'")
    print("  → '+ New Access Token' → purpose 'planner' → Generate → copy it.")
    token = input("  Paste token: ").strip()
    if not base_url or not token:
        sys.exit("Setup cancelled — both the URL and token are required.")
    with open(CONFIG_PATH, "w") as f:
        json.dump({"base_url": base_url, "token": token}, f, indent=2)
    print(f"\n✓ Saved {CONFIG_PATH} (kept private, never shared).\n")
    return {"base_url": base_url, "token": token}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return first_run_setup()
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if not cfg.get("base_url") or not cfg.get("token") or "PASTE" in cfg["token"]:
        sys.exit("config.json is missing base_url or token. Edit it and try again.")
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    return cfg


# --------------------------------------------------------------------------- #
# Canvas API
# --------------------------------------------------------------------------- #
class Canvas:
    def __init__(self, base_url, token):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def get(self, path, params=None):
        """GET with automatic pagination (follows Link rel=next)."""
        url = f"{self.base_url}/api/v1{path}"
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out = []
        while url:
            r = self.session.get(url, params=params, timeout=30)
            if r.status_code == 401:
                sys.exit("Canvas rejected the token (401). Check the token in config.json.")
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
    days = (local_due.date() - now.date()).days
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
        if 0 <= (d - now.date()).days <= 14:
            counts[d] = counts.get(d, 0) + 1
    return sorted((str(d), n) for d, n in counts.items() if n >= threshold)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
# Overdue items are intentionally excluded from the planner (see render_html).
BUCKET_META = [
    ("today", "🔥 Due Today", "#f97316"),
    ("week", "📅 This Week", "#eab308"),
    ("later", "🗓️ Later", "#3b82f6"),
    ("none", "📋 No Due Date", "#6b7280"),
]


def fmt_due(iso):
    if not iso:
        return "—"
    dt = parse_dt(iso).astimezone()
    return dt.strftime("%a %b %-d, %-I:%M %p")


def fmt_start(iso):
    if not iso:
        return ""
    return parse_dt(iso).astimezone().strftime("%a %b %-d")


def fmt_posted(iso):
    if not iso:
        return ""
    return parse_dt(iso).astimezone().strftime("%a %b %-d, %-I:%M %p")


def _esc(s):
    return _html.escape(str(s or ""))


def render_html(items, warnings, courses_info, announcements, colors, now, accent="#6366f1", grades=None):
    # Overdue items are removed from the planner entirely.
    pending = [i for i in items if not i["submitted"] and i["bucket"] != "overdue"]
    done = sum(1 for i in items if i["submitted"])

    cards_by_bucket = {b: [] for b, _, _ in BUCKET_META}
    for it in sorted(pending, key=lambda x: (x["due"] is None, x["due"] or "")):
        cards_by_bucket[it["bucket"]].append(it)

    def dot(course):
        return f'<span class="dot" style="background:{colors.get(course, "#888")}"></span>'

    # Detail registry: each card gets an id; JS opens a popup with these fields.
    details = []

    def reg(it):
        details.append({
            "title": it["title"],
            "course": clean_course_name(it["course"]),
            "type": it["type"],
            "due": fmt_due(it.get("due")),
            "start": fmt_start(it.get("start")),
            "points": (f'{it["points"]:g} pts' if it.get("points") else ""),
            "url": it.get("url") or "#",
            "desc": it.get("description") or "",
            "score": it.get("score"),
            "comments": it.get("comments") or [],
            "rubric": [r for r in (it.get("rubric") or []) if r.get("points") is not None or r.get("comment")],
            "subTypes": it.get("submission_types") or [],
            "courseId": it.get("course_id"),
            "assignId": it.get("assign_id"),
            "submitted": it.get("submitted", False),
            "uid": it.get("uid"),
            "note": it.get("note", ""),
        })
        return len(details) - 1

    # ---- Tab 1: To-Do ----
    warn_html = ""
    if warnings:
        rows = "".join(
            f"<li><b>{datetime.fromisoformat(d).strftime('%a %b %-d')}</b> — {n} things due</li>"
            for d, n in warnings
        )
        warn_html = f'<div class="warn"><h3>🚨 Heavy days ahead — start early</h3><ul>{rows}</ul></div>'

    todo = ""
    for bucket, label, color in BUCKET_META:
        cards = cards_by_bucket[bucket]
        if not cards:
            continue
        cell = ""
        for it in cards:
            pts = f'<span class="pts">{it["points"]:g} pts</span>' if it.get("points") else ""
            start = fmt_start(it.get("start"))
            start_html = f'<span class="start">▶ start {start}</span>' if start and bucket in ("week", "later") else ""
            badge = "💬" if it["type"] == "discussion" else "📝"
            cell += f"""
            <div class="card" data-id="{reg(it)}" style="border-left-color:{colors.get(it['course'],'#555')}">
              <div class="card-top"><span class="course">{dot(it['course'])}{badge} {_esc(clean_course_name(it['course']))}</span>{pts}</div>
              <div class="title">{_esc(it['title'])}</div>
              <div class="card-bot"><span class="due">⏰ {fmt_due(it.get('due'))}</span>{start_html}</div>
            </div>"""
        todo += f'<section><h2 style="color:{color}">{label} <span class="count">{len(cards)}</span></h2><div class="grid">{cell}</div></section>'
    todo = todo or '<p class="empty">🎉 Nothing pending. Enjoy the break!</p>'

    # ---- Tab 2: Graded Discussions (excluding overdue) ----
    graded = [i for i in items if i["type"] == "discussion"
              and i.get("points") is not None and i["bucket"] != "overdue"]
    graded.sort(key=lambda x: (x["submitted"], x["due"] is None, x["due"] or ""))
    disc = ""
    for it in graded:
        pts = f'<span class="pts">{it["points"]:g} pts</span>' if it.get("points") else ""
        check = '<span class="start">✓ done</span>' if it["submitted"] else ""
        disc += f"""
        <div class="card" data-id="{reg(it)}" style="border-left-color:{colors.get(it['course'],'#555')}">
          <div class="card-top"><span class="course">{dot(it['course'])}💬 {_esc(clean_course_name(it['course']))}</span>{pts}</div>
          <div class="title">{_esc(it['title'])}</div>
          <div class="card-bot"><span class="due">⏰ {fmt_due(it.get('due'))}</span>{check}</div>
        </div>"""
    disc = f'<div class="grid">{disc}</div>' if disc else '<p class="empty">No graded discussions.</p>'
    n_disc_todo = sum(1 for i in graded if not i["submitted"])

    # ---- Tab 3: Announcements ----
    ann = ""
    for a in announcements:
        prev = _esc(a["preview"]).replace("\n", "<br>")
        ann += f"""
        <a class="ann" href="{_esc(a.get('url') or '#')}" target="_blank" style="border-left-color:{colors.get(a['course'],'#555')}">
          <div class="card-top"><span class="course">{dot(a['course'])}{_esc(clean_course_name(a['course']))}</span><span class="date">{fmt_posted(a.get('posted'))}</span></div>
          <div class="title">{_esc(a['title'])}</div>
          <div class="prev">{prev}</div>
        </a>"""
    ann = ann or '<p class="empty">No recent announcements.</p>'

    # ---- Tab 3: Courses / Syllabus / Modules / Files / Pages ----
    def _linklist(rows):
        return "".join(f'<li><a href="{_esc(u)}" target="_blank">{_esc(t or "(untitled)")}</a></li>' for t, u in rows if u)

    crs = ""
    for c in courses_info:
        col = colors.get(c["name"], "#888")
        syl = _esc(c["syllabus"]).replace("\n", "<br>") if c["syllabus"] else "<i>No syllabus text posted — open the course to view.</i>"
        mod_html = ""
        for m in c.get("modules", []):
            items_html = _linklist((it.get("title"), it.get("url")) for it in m.get("items", []))
            mod_html += f'<li class="modname">{_esc(m.get("name") or "Module")}</li>{items_html}'
        files_html = _linklist((f.get("name"), f.get("url")) for f in c.get("files", []))
        pages_html = _linklist((p.get("title"), p.get("url")) for p in c.get("pages", []))
        extra = ""
        if mod_html:
            extra += f'<details><summary>📦 Modules ({len(c.get("modules", []))})</summary><ul class="clist">{mod_html}</ul></details>'
        if files_html:
            extra += f'<details><summary>📄 Files ({len(c.get("files", []))})</summary><ul class="clist">{files_html}</ul></details>'
        if pages_html:
            extra += f'<details><summary>📃 Pages ({len(c.get("pages", []))})</summary><ul class="clist">{pages_html}</ul></details>'
        crs += f"""
        <div class="course-card" style="border-top:3px solid {col}">
          <div class="cc-top"><h3>{dot(c['name'])}{_esc(clean_course_name(c['name']))}</h3>
            <span class="pill">{c['pending']} to do</span></div>
          <div class="links">
            <a href="{_esc(c['url'])}" target="_blank">Open course ↗</a>
            <a href="{_esc(c['syllabus_url'])}" target="_blank">Full syllabus ↗</a>
          </div>
          <details><summary>📖 Syllabus preview</summary><div class="syl">{syl}</div></details>
          {extra}
        </div>"""
    crs = crs or '<p class="empty">No courses found.</p>'

    # ---- Grades (rendered client-side for the live what-if projector) ----
    grades_json = json.dumps([{**g, "color": colors.get(g["name"], "#888"),
                               "cleanName": clean_course_name(g["name"])} for g in (grades or [])],
                             ensure_ascii=False).replace("</", "<\\/")

    # ---- Week board (kanban) data: every pending, non-overdue item ----
    kanban = []
    for it in items:
        if it["submitted"] or it["bucket"] == "overdue":
            continue
        kanban.append({
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
        })
    kanban_json = json.dumps(kanban, ensure_ascii=False).replace("</", "<\\/")

    courses_compact = json.dumps([{"id": c.get("id"), "name": clean_course_name(c["name"])}
                                  for c in courses_info], ensure_ascii=False).replace("</", "<\\/")

    # Embed per-card details for the popup (escape </ so it can't break out of the tag).
    data_json = json.dumps(details, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Semester</title>
<link rel="icon" href='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="%230a0a0c"/><text x="50" y="54" font-family="Helvetica,Arial,sans-serif" font-size="74" font-weight="700" fill="white" text-anchor="middle" dominant-baseline="central">S</text></svg>'>
<style>
  :root {{
    --accent: {accent}; color-scheme: light;
    --bg: #e9e9e9; --rail: #f6f6f6; --surface: #ffffff; --surface-2: #f2f2f2;
    --text: #1b1b1b; --dim: #5d5d5d; --border: #e3e3e3; --hover: #ededed;
    --sel: #e6e6e6; --pill: #ececec; --pilltext: #444; --shadow: 0 1px 2px rgba(0,0,0,.05);
    --warnbg: rgba(239,68,68,.07); --warnh: #c4271b;
  }}
  html.dark {{
    color-scheme: dark;
    --bg: #0f1115; --rail: #15181e; --surface: #1a1d24; --surface-2: #14161c;
    --text: #e7e9ee; --dim: #9aa0ad; --border: #262a33; --hover: #21252e;
    --sel: #21252e; --pill: #2a2e38; --pilltext: #cbd2e0; --shadow: none;
    --warnbg: #2a1416; --warnh: #ff8a8a;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI','Segoe UI Variable',system-ui,-apple-system,sans-serif;
         margin: 0; background: var(--bg); color: var(--text); }}
  .app {{ display: flex; height: 100vh; }}
  .rail {{ width: 248px; min-width: 248px; background: var(--rail); border-right: 1px solid var(--border);
          display: flex; flex-direction: column; }}
  .brand {{ font-size: 18px; font-weight: 500; padding: 20px 18px 14px; }}
  .navlist {{ flex: 1; overflow: auto; padding: 4px 0; }}
  .nav {{ display: flex; align-items: center; gap: 12px; width: 100%; text-align: left; border: 0;
         background: none; font: inherit; font-size: 14px; color: var(--text); padding: 10px 16px;
         cursor: pointer; border-left: 3px solid transparent; }}
  .nav span {{ flex: 1; }}
  .nav:hover {{ background: var(--hover); }}
  .nav.active {{ background: var(--sel); border-left-color: var(--accent); font-weight: 600; }}
  .badge {{ background: var(--pill); color: var(--pilltext); font-size: 12px; border-radius: 10px; padding: 1px 8px; font-weight: 500; }}
  .railfoot {{ border-top: 1px solid var(--border); padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }}
  .accentrow {{ font-size: 13px; color: var(--dim); display: flex; align-items: center; justify-content: space-between; }}
  input[type=color] {{ width: 44px; height: 26px; border: 1px solid var(--border); background: var(--surface); padding: 2px; cursor: pointer; border-radius: 4px; }}
  .themebtn {{ font: inherit; font-size: 13px; color: var(--text); background: var(--surface);
          border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px; cursor: pointer; text-align: left; }}
  .themebtn:hover {{ background: var(--hover); }}
  .content {{ flex: 1; overflow: auto; padding: 22px 30px 50px; }}
  .pagehead {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
  h1 {{ margin: 0; font-size: 26px; font-weight: 500; }}
  .updated {{ font-size: 13px; color: var(--dim); }}
  .stats {{ display: flex; gap: 12px; margin: 16px 0 4px; flex-wrap: wrap; }}
  .stat {{ background: var(--surface-2); border-radius: 10px; padding: 11px 18px; min-width: 92px; }}
  .stat b {{ font-size: 22px; display: block; }}
  .stat span {{ color: var(--dim); font-size: 12px; }}
  .panel {{ display: none; }} .panel.active {{ display: block; margin-top: 8px; }}
  section {{ margin-top: 22px; }}
  h2 {{ font-size: 17px; margin: 0 0 12px; display: flex; align-items: center; gap: 8px; font-weight: 600; }}
  .count, .pill {{ background: var(--pill); color: var(--pilltext); font-size: 12px; border-radius: 999px; padding: 2px 9px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 12px; }}
  .card, .ann {{ background: var(--surface); border: 1px solid var(--border); border-left: 4px solid #555; border-radius: 8px;
          padding: 14px 16px; text-decoration: none; color: inherit; display: block; box-shadow: var(--shadow);
          transition: background .12s ease; }}
  .card {{ cursor: pointer; }}
  .card:hover, .ann:hover {{ background: var(--hover); }}
  .modal {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5);
           z-index: 50; padding: 40px 16px; overflow: auto; }}
  .modal.open {{ display: block; }}
  .modal-box {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
           max-width: 720px; margin: 0 auto; padding: 24px 26px; position: relative; box-shadow: 0 8px 30px rgba(0,0,0,.18); }}
  .modal-x {{ position: absolute; top: 14px; right: 16px; background: none; border: none;
           color: var(--dim); font-size: 20px; cursor: pointer; }}
  .modal-x:hover {{ color: var(--text); }}
  .modal-course {{ font-size: 13px; color: var(--dim); display: flex; align-items: center; gap: 7px; }}
  .modal-box h2 {{ margin: 6px 0 10px; font-size: 21px; line-height: 1.3; }}
  .modal-meta {{ display: flex; gap: 10px; flex-wrap: wrap; font-size: 13px; color: var(--text); margin-bottom: 14px; }}
  .modal-meta span {{ background: var(--surface-2); border-radius: 7px; padding: 4px 10px; }}
  .modal-desc {{ color: var(--text); font-size: 14px; line-height: 1.65; border-top: 1px solid var(--border); padding-top: 14px; }}
  .modal-desc img {{ max-width: 100%; height: auto; }}
  .modal-desc a {{ color: var(--accent); }}
  .modal-desc table {{ border-collapse: collapse; }} .modal-desc td, .modal-desc th {{ border: 1px solid var(--border); padding: 4px 8px; }}
  .modal-open {{ display: inline-block; margin-top: 18px; background: var(--accent); color: #fff;
           text-decoration: none; padding: 9px 16px; border-radius: 8px; font-weight: 600; font-size: 14px; }}
  .modal-open:hover {{ filter: brightness(.9); }}
  .modal-fb {{ margin-top: 16px; }}
  .fb-h {{ font-weight: 600; font-size: 14px; margin-bottom: 8px; }}
  .fb-score {{ font-size: 14px; color: var(--accent); font-weight: 600; margin-bottom: 8px; }}
  .fb-rub {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 8px; }}
  .fb-rub td {{ padding: 6px 4px; border-top: 1px solid var(--border); vertical-align: top; }}
  .fb-rub td.gs {{ text-align: right; white-space: nowrap; font-weight: 500; }}
  .fb-c {{ color: var(--dim); font-size: 12px; }}
  .fb-cm {{ border-top: 1px solid var(--border); padding-top: 8px; }}
  .fb-cmrow {{ font-size: 13px; color: var(--text); margin-bottom: 6px; line-height: 1.5; }}
  .modal-actions {{ margin-top: 16px; display: flex; gap: 10px; flex-wrap: wrap; }}
  .modal-submit {{ margin-top: 16px; }}
  .sub-h {{ font-weight: 600; font-size: 14px; margin-bottom: 6px; }}
  .modal-submit select {{ background: var(--surface); color: var(--text); border: 1px solid var(--border);
          border-radius: 8px; padding: 6px 9px; font: inherit; font-size: 13px; margin-bottom: 8px; }}
  .modal-submit textarea, .modal-submit input[type=url] {{ width: 100%; background: var(--surface-2); color: var(--text);
          border: 1px solid var(--border); border-radius: 8px; padding: 9px 11px; font: inherit; font-size: 13px; resize: vertical; }}
  .sub-warn {{ font-size: 12px; color: var(--warnh); }}
  .sub-note {{ font-size: 13px; color: var(--dim); background: var(--surface-2); border: 1px solid var(--border);
          border-radius: 8px; padding: 10px 12px; }}
  .modal-notes {{ margin-top: 16px; }}
  .notes-h {{ font-weight: 600; font-size: 14px; margin-bottom: 6px; }}
  .notes-sub {{ color: var(--dim); font-weight: 400; font-size: 12px; }}
  .modal-notes textarea {{ width: 100%; background: var(--surface-2); color: var(--text);
          border: 1px solid var(--border); border-radius: 8px; padding: 9px 11px; font: inherit; font-size: 13px; resize: vertical; }}
  .notes-row {{ display: flex; gap: 10px; align-items: center; margin-top: 8px; flex-wrap: wrap; }}
  .logged {{ font-size: 12px; color: var(--dim); }}
  .focusbar {{ position: fixed; left: 18px; bottom: 18px; background: var(--surface); color: var(--text);
          border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: 10px;
          padding: 10px 14px; font-size: 14px; box-shadow: 0 6px 24px rgba(0,0,0,.18); z-index: 60;
          display: none; align-items: center; gap: 10px; }}
  .focusbar.show {{ display: flex; }}
  .focusbar .ft {{ font-weight: 600; font-variant-numeric: tabular-nums; }}
  .focusbar button {{ background: none; border: 1px solid var(--border); border-radius: 6px;
          color: var(--text); cursor: pointer; padding: 2px 8px; font-size: 13px; }}
  .ann {{ margin-bottom: 12px; }}
  .card-top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
  .course {{ font-size: 12px; color: var(--dim); display: flex; align-items: center; gap: 6px; }}
  .dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}
  .pts {{ font-size: 11px; background: var(--pill); border-radius: 6px; padding: 2px 7px; color: var(--pilltext); }}
  .title {{ font-weight: 600; margin: 7px 0 8px; line-height: 1.3; }}
  .card-bot {{ display: flex; justify-content: space-between; align-items: center; font-size: 12px; }}
  .due {{ color: var(--dim); }} .start {{ color: #2e9b54; font-weight: 600; }}
  .date {{ color: var(--dim); font-size: 12px; }}
  .prev {{ color: var(--dim); font-size: 13px; line-height: 1.5; }}
  .warn {{ background: var(--warnbg); border: 1px solid #ef4444; border-radius: 10px; padding: 14px 18px; margin: 6px 0 4px; }}
  .warn h3 {{ margin: 0 0 8px; color: var(--warnh); }} .warn ul {{ margin: 0; padding-left: 20px; }}
  .course-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; margin-bottom: 14px; box-shadow: var(--shadow); }}
  .cc-top {{ display: flex; justify-content: space-between; align-items: center; }}
  .cc-top h3 {{ margin: 0; display: flex; align-items: center; gap: 8px; font-size: 17px; font-weight: 600; }}
  .links {{ display: flex; gap: 16px; margin: 10px 0; }}
  .links a {{ color: var(--accent); font-size: 13px; text-decoration: none; }}
  .links a:hover {{ text-decoration: underline; }}
  details {{ margin-top: 6px; }} summary {{ cursor: pointer; color: var(--dim); font-size: 14px; }}
  .syl {{ color: var(--dim); font-size: 13px; line-height: 1.6; margin-top: 10px;
         max-height: 360px; overflow: auto; padding-right: 8px; }}
  .clist {{ list-style: none; margin: 8px 0 4px; padding: 0; max-height: 320px; overflow: auto; }}
  .clist li {{ font-size: 13px; padding: 3px 0; }}
  .clist li.modname {{ font-weight: 600; color: var(--text); margin-top: 8px; }}
  .clist a {{ color: var(--accent); text-decoration: none; }}
  .clist a:hover {{ text-decoration: underline; }}
  .empty {{ color: var(--dim); }}
  /* Week board (kanban) */
  .controls {{ display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin: 0 0 14px; }}
  .chips {{ display: flex; gap: 7px; flex-wrap: wrap; }}
  .chip {{ display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text);
          background: var(--surface); border: 1px solid var(--border); border-radius: 999px;
          padding: 5px 11px; cursor: pointer; user-select: none; }}
  .chip .cdot {{ width: 9px; height: 9px; border-radius: 50%; }}
  .chip.off {{ color: var(--dim); opacity: .6; }}
  .chip.off .cdot {{ opacity: .25; }}
  .sortbox {{ font-size: 12px; color: var(--dim); display: flex; align-items: center; gap: 7px; margin-left: auto; }}
  .sortbox select {{ background: var(--surface); color: var(--text); border: 1px solid var(--border);
          border-radius: 8px; padding: 5px 9px; font: inherit; font-size: 12px; cursor: pointer; }}
  .board {{ display: grid; grid-template-columns: repeat(7, minmax(105px, 1fr)); gap: 10px;
           overflow-x: auto; padding-bottom: 4px; }}
  .col {{ background: var(--surface-2); border: 1px solid var(--border); border-radius: 12px; padding: 10px; min-height: 200px; transition: background .12s ease; }}
  .col.today {{ box-shadow: inset 0 0 0 2px var(--accent); }}
  .col.drop, .tray.drop {{ background: var(--hover); }}
  .col h4 {{ margin: 0 0 10px; font-size: 12px; color: var(--dim); font-weight: 500; display: flex; align-items: center; gap: 5px; }}
  .col h4 .d {{ color: var(--text); }}
  .col h4 .load {{ margin-left: auto; background: var(--pill); color: var(--pilltext); border-radius: 8px; padding: 0 6px; font-size: 11px; }}
  .col.load-warn h4 .load {{ background: #fde68a; color: #92400e; }}
  .col.load-heavy {{ box-shadow: inset 0 0 0 2px #ef4444; }}
  .col.load-heavy h4 .load {{ background: #fecaca; color: #991b1b; }}
  .tray-label {{ margin: 20px 0 8px; font-size: 13px; color: var(--dim); font-weight: 500; }}
  .tray-label b {{ color: var(--accent); }}
  .tray {{ background: var(--surface-2); border: 1px dashed var(--border); border-radius: 12px; padding: 12px;
          min-height: 96px; display: flex; flex-wrap: wrap; gap: 8px; align-content: flex-start;
          transition: background .12s ease; }}
  .kcard {{ border-radius: 10px; padding: 10px 11px; margin-bottom: 8px; cursor: grab; }}
  .tray .kcard {{ width: 188px; margin-bottom: 0; }}
  .kcard:active {{ cursor: grabbing; }}
  .kcard.dragging {{ opacity: .45; }}
  .kcard .kt {{ color: var(--text); font-weight: 500; font-size: 13px; line-height: 1.3; }}
  .kcard .km {{ font-size: 11px; margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }}
  .kcard .due {{ color: var(--dim); background: var(--pill); border-radius: 6px; padding: 1px 6px; }}
  .empty-col {{ color: var(--dim); opacity: .5; font-size: 18px; text-align: center; padding: 10px 0; user-select: none; }}
  .tray-empty {{ color: var(--dim); font-size: 13px; padding: 8px 4px; }}
  /* Grades */
  .bigscore {{ font-size: 20px; font-weight: 600; color: var(--accent); }}
  .gtable {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }}
  .gtable td {{ padding: 7px 4px; border-top: 1px solid var(--border); }}
  .gtable td.gs {{ text-align: right; font-weight: 500; white-space: nowrap; }}
  .gtable .gp {{ color: var(--dim); font-weight: 400; }}
  .gtable input {{ width: 64px; background: var(--surface); color: var(--text); border: 1px solid var(--border);
          border-radius: 6px; padding: 4px 6px; font: inherit; font-size: 13px; text-align: right; }}
  .proj {{ display: flex; gap: 18px; align-items: center; flex-wrap: wrap; margin: 10px 0 4px; }}
  .proj .pv {{ font-size: 18px; font-weight: 600; }}
  .needline {{ font-size: 13px; color: var(--dim); margin-top: 6px; }}
  .needline b {{ color: var(--accent); }}
  .target-in {{ width: 60px; background: var(--surface); color: var(--text); border: 1px solid var(--border);
          border-radius: 6px; padding: 4px 6px; font: inherit; font-size: 13px; }}
  /* Inbox */
  .inbox-top {{ display: flex; gap: 10px; margin-bottom: 14px; }}
  .conv {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px;
          margin-bottom: 8px; cursor: pointer; box-shadow: var(--shadow); }}
  .conv:hover {{ background: var(--hover); }}
  .conv .cs {{ font-weight: 600; font-size: 14px; }}
  .conv.unread .cs::before {{ content: "● "; color: var(--accent); }}
  .conv .cw {{ font-size: 12px; color: var(--dim); }}
  .conv .cp {{ font-size: 13px; color: var(--dim); margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .thread .msg {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 11px 14px; margin-bottom: 8px; }}
  .thread .ma {{ font-weight: 600; font-size: 13px; }} .thread .md {{ font-size: 12px; color: var(--dim); }}
  .thread .mb {{ font-size: 14px; line-height: 1.6; margin-top: 5px; white-space: pre-wrap; }}
  .composer textarea, .composer input, .composer select {{ width: 100%; background: var(--surface-2); color: var(--text);
          border: 1px solid var(--border); border-radius: 8px; padding: 9px 11px; font: inherit; font-size: 13px; margin-bottom: 8px; }}
  .backlink {{ color: var(--accent); cursor: pointer; font-size: 13px; display: inline-block; margin-bottom: 10px; }}
  /* Update banner */
  .updbar {{ position: fixed; top: 0; left: 0; right: 0; background: var(--accent); color: #fff;
          padding: 9px 14px; font-size: 13px; text-align: center; z-index: 70; display: none; }}
  .updbar.show {{ display: block; }}
  .updbar button {{ background: rgba(255,255,255,.22); border: 1px solid rgba(255,255,255,.5);
          color: #fff; border-radius: 6px; padding: 3px 10px; font: inherit; font-size: 13px; cursor: pointer; margin-left: 10px; }}
  .updbar button.x {{ background: none; border: none; }}
  /* Settings */
  .railbottom {{ border-top: 1px solid var(--border); padding: 6px 0; }}
  .setgroup {{ max-width: 620px; margin-top: 18px; }}
  .setrow {{ display: flex; align-items: center; justify-content: space-between; gap: 16px;
          background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
          padding: 12px 16px; margin-bottom: 8px; box-shadow: var(--shadow); }}
  .st {{ font-size: 14px; font-weight: 500; }}
  .sd {{ font-size: 12px; color: var(--dim); margin-top: 2px; }}
  .switch {{ position: relative; display: inline-block; width: 40px; height: 20px; flex: none; }}
  .switch input {{ opacity: 0; width: 0; height: 0; }}
  .track {{ position: absolute; inset: 0; background: var(--pill); border: 1px solid var(--border);
          border-radius: 999px; transition: .15s; cursor: pointer; }}
  .track:before {{ content: ""; position: absolute; width: 12px; height: 12px; left: 3px; top: 3px;
          background: var(--dim); border-radius: 50%; transition: .15s; }}
  .switch input:checked + .track {{ background: var(--accent); border-color: var(--accent); }}
  .switch input:checked + .track:before {{ transform: translateX(20px); background: #fff; }}
  .setrow select, .setrow input[type=number] {{ background: var(--surface); color: var(--text);
          border: 1px solid var(--border); border-radius: 8px; padding: 6px 9px; font: inherit; font-size: 13px; cursor: pointer; }}
  .setrow input[type=number] {{ width: 74px; }}
  .dlbtn {{ background: var(--accent); color: #fff; border: none; border-radius: 8px; padding: 8px 14px;
          font: inherit; font-size: 13px; font-weight: 500; cursor: pointer; }}
  .dlbtn:hover {{ filter: brightness(.93); }}
  .ghostbtn {{ background: var(--surface); color: var(--text); border: 1px solid var(--border);
          border-radius: 8px; padding: 8px 12px; font: inherit; font-size: 13px; cursor: pointer; }}
  .ghostbtn:hover {{ background: var(--hover); }}
  .autolabel {{ font-size: 12px; color: var(--dim); }}
  .controls select {{ background: var(--surface); color: var(--text); border: 1px solid var(--border);
          border-radius: 8px; padding: 6px 9px; font: inherit; font-size: 12px; cursor: pointer; }}
  .toast {{ position: fixed; right: 18px; bottom: 18px; background: var(--surface); color: var(--text);
          border: 1px solid var(--border); border-left: 3px solid var(--accent); border-radius: 10px;
          padding: 12px 16px; font-size: 13px; box-shadow: 0 6px 24px rgba(0,0,0,.18); cursor: pointer;
          display: none; z-index: 60; max-width: 280px; }}
  .toast.show {{ display: block; }}
  .foot {{ color: var(--dim); font-size: 12px; padding: 30px 0 10px; }}
</style>
<script>try{{var t=localStorage.getItem('semester.theme')||'light';var dark=t==='dark'||(t==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);if(dark)document.documentElement.classList.add('dark');var a=localStorage.getItem('semester.accent');if(a)document.documentElement.style.setProperty('--accent',a);}}catch(e){{}}</script>
</head>
<body>
<div class="app">
  <aside class="rail">
    <div class="brand">Semester</div>
    <nav class="navlist">
      <button class="nav active" data-p="week"><span>Week board</span></button>
      <button class="nav" data-p="todo"><span>To-Do</span><b class="badge">{len(pending)}</b></button>
      <button class="nav" data-p="disc"><span>Discussions</span><b class="badge">{n_disc_todo}</b></button>
      <button class="nav" data-p="ann"><span>Announcements</span><b class="badge">{len(announcements)}</b></button>
      <button class="nav" data-p="crs"><span>Courses</span><b class="badge">{len(courses_info)}</b></button>
      <button class="nav" data-p="grades"><span>Grades</span></button>
      <button class="nav" data-p="inbox"><span>Inbox</span></button>
    </nav>
    <div class="railbottom">
      <button class="nav" data-p="settings"><span>Settings</span></button>
    </div>
  </aside>
  <main class="content">
    <div class="pagehead"><h1 id="pageTitle">Week board</h1>
      <div class="updated">Updated {now.astimezone().strftime('%A, %B %-d at %-I:%M %p')}</div></div>
    <div class="stats">
      <div class="stat"><b>{len(pending)}</b><span>to do</span></div>
      <div class="stat"><b>{len(cards_by_bucket['today']) + len(cards_by_bucket['week'])}</b><span>due this week</span></div>
      <div class="stat"><b>{n_disc_todo}</b><span>discussions</span></div>
      <div class="stat"><b>{done}</b><span>done ✓</span></div>
    </div>
    <div class="panel active" id="week">
      <p class="empty" style="margin:4px 0 12px">Drag cards from the tray <b>up onto a day</b>, or let <b>Auto-schedule</b> plan the week for you. The "due" tag always stays put. Saved in this browser.</p>
      <div class="controls">
        <span class="autolabel">Auto-schedule</span>
        <select id="autoAlgo">
          <option value="balanced">Balanced load</option>
          <option value="deadline">Deadline-first</option>
          <option value="front">Front-load</option>
          <option value="jit">Just-in-time</option>
        </select>
        <button class="dlbtn" id="autoBtn">Plan my week</button>
        <button class="ghostbtn" id="clearPlan">Clear</button>
      </div>
      <div class="controls">
        <div class="chips" id="courseChips"></div>
        <label class="sortbox">Sort by
          <select id="traySort">
            <option value="due">Due date</option>
            <option value="points">Points (high→low)</option>
            <option value="course">Course</option>
            <option value="title">Name (A→Z)</option>
          </select>
        </label>
      </div>
      <div class="board" id="weekboard"></div>
      <div class="tray-label">📥 Unscheduled — <b>drag up onto a day</b></div>
      <div class="tray" id="weektray"></div>
    </div>
    <div class="panel" id="todo">{warn_html}{todo}</div>
    <div class="panel" id="disc">{disc}</div>
    <div class="panel" id="ann">{ann}</div>
    <div class="panel" id="crs">{crs}</div>
    <div class="panel" id="grades"><p class="empty" style="margin:4px 0 14px">Type a hypothetical score into any ungraded row to project your grade, or set a target to see what you need.</p><div id="gradesBox"></div></div>
    <div class="panel" id="inbox">
      <div class="inbox-top"><button class="dlbtn" id="composeBtn">New message</button>
        <button class="ghostbtn" id="inboxRefresh">Refresh</button></div>
      <div id="inboxBox"><p class="empty">Loading messages…</p></div>
    </div>
    <div class="panel" id="settings">
      <div class="setgroup">
        <h2>Appearance</h2>
        <div class="setrow">
          <div><div class="st">Theme</div><div class="sd">Light, dark, or match your system.</div></div>
          <select id="themeSel"><option value="light">Light</option><option value="dark">Dark</option><option value="system">Match system</option></select>
        </div>
        <div class="setrow">
          <div><div class="st">Accent color</div><div class="sd">Buttons, highlights, and the selected tab.</div></div>
          <input type="color" id="accentInput" value="{accent}">
        </div>
      </div>
      <div class="setgroup">
        <h2>Week board</h2>
        <div class="setrow">
          <div><div class="st">Board view</div><div class="sd">A rolling next-7-days, or the current calendar week.</div></div>
          <select id="boardView"><option value="rolling">Next 7 days</option><option value="week">This week</option></select>
        </div>
        <div class="setrow">
          <div><div class="st">Week starts on</div><div class="sd">Used when board view is "This week."</div></div>
          <select id="weekStart"><option value="0">Sunday</option><option value="1">Monday</option></select>
        </div>
        <div class="setrow"><div class="st">Show weekends</div><label class="switch"><input type="checkbox" id="weekends"><span class="track"></span></label></div>
        <div class="setrow">
          <div><div class="st">Heavy-day threshold</div><div class="sd">Flag a day once this many tasks land on it.</div></div>
          <input type="number" id="dayThreshold" min="2" max="12">
        </div>
      </div>
      <div class="setgroup">
        <h2>Planning</h2>
        <div class="setrow">
          <div><div class="st">Start-early aggressiveness</div><div class="sd">How far ahead suggested start dates land. Applies in the installed app.</div></div>
          <select id="aggr"><option value="relaxed">Relaxed</option><option value="balanced">Balanced</option><option value="aggressive">Aggressive</option></select>
        </div>
        <div class="setrow">
          <div><div class="st">Default tab</div><div class="sd">Which section opens when you launch.</div></div>
          <select id="defaultTab"><option value="week">Week board</option><option value="todo">To-Do</option><option value="disc">Discussions</option><option value="ann">Announcements</option><option value="crs">Courses</option><option value="grades">Grades</option></select>
        </div>
      </div>
      <div class="setgroup">
        <h2>Updates</h2>
        <div class="setrow"><div><div class="st">Auto-refresh</div><div class="sd">Re-check Canvas in the background. Applies in the installed app.</div></div>
          <label class="switch"><input type="checkbox" id="autoOn"><span class="track"></span></label></div>
        <div class="setrow"><div><div class="st">Refresh every</div><div class="sd">Minutes between background checks.</div></div>
          <input type="number" id="autoMin" min="15" max="720" step="5"></div>
        <div class="setrow"><div><div class="st">Background reminders</div><div class="sd">Notify me of due-soon work and start days even when the app is closed (macOS).</div></div>
          <label class="switch"><input type="checkbox" id="bgNotify"><span class="track"></span></label></div>
      </div>
      <div class="setgroup">
        <h2>Sections</h2>
        <p class="sd" style="margin:0 0 10px">Choose which tabs appear in the sidebar. Week board is always shown.</p>
        <div class="setrow"><div class="st">To-Do</div><label class="switch"><input type="checkbox" data-tab="todo"><span class="track"></span></label></div>
        <div class="setrow"><div class="st">Discussions</div><label class="switch"><input type="checkbox" data-tab="disc"><span class="track"></span></label></div>
        <div class="setrow"><div class="st">Announcements</div><label class="switch"><input type="checkbox" data-tab="ann"><span class="track"></span></label></div>
        <div class="setrow"><div class="st">Courses</div><label class="switch"><input type="checkbox" data-tab="crs"><span class="track"></span></label></div>
        <div class="setrow"><div class="st">Grades</div><label class="switch"><input type="checkbox" data-tab="grades"><span class="track"></span></label></div>
        <div class="setrow"><div class="st">Inbox</div><label class="switch"><input type="checkbox" data-tab="inbox"><span class="track"></span></label></div>
      </div>
      <div class="setgroup">
        <h2>Calendar</h2>
        <div class="setrow"><div class="st">Include deadlines</div><label class="switch"><input type="checkbox" id="icsDue" checked><span class="track"></span></label></div>
        <div class="setrow"><div class="st">Include planned work-days</div><label class="switch"><input type="checkbox" id="icsPlan" checked><span class="track"></span></label></div>
        <div class="setrow"><div><div class="st">Export calendar</div><div class="sd">Download an .ics file to import into Google or Apple Calendar.</div></div>
          <button class="dlbtn" id="icsExport">Download .ics</button></div>
      </div>
      <div class="setgroup">
        <h2>About</h2>
        <div class="setrow"><div><div class="st">Check for updates on launch</div><div class="sd">Notify me when a newer version is released.</div></div>
          <label class="switch"><input type="checkbox" id="autoUpdate"><span class="track"></span></label></div>
        <div class="setrow"><div><div class="st">Semester {VERSION}</div><div class="sd">Updates keep your settings, plan, and notes — they live outside the app.</div></div>
          <button class="ghostbtn" id="checkUpdate">Check now</button></div>
      </div>
    </div>
    <div class="foot">Green ▶ dates are suggested START days so you're not cramming. Colored dots group by course. Click any card to read its description.</div>
  </main>
</div>

<div class="modal" id="modal">
  <div class="modal-box">
    <button class="modal-x" id="modalX">✕</button>
    <div class="modal-course" id="mCourse"></div>
    <h2 id="mTitle"></h2>
    <div class="modal-meta" id="mMeta"></div>
    <div class="modal-desc" id="mDesc"></div>
    <div class="modal-fb" id="mFb"></div>
    <div class="modal-submit" id="mSubmit"></div>
    <div class="modal-notes" id="mNotes"></div>
    <div class="modal-actions" id="mActions"></div>
    <a class="modal-open" id="mLink" target="_blank">Open in Canvas ↗</a>
  </div>
</div>

<div class="toast" id="toast"></div>
<div class="focusbar" id="focusBar"></div>
<div class="updbar" id="updBar"></div>

<script type="application/json" id="itemdata">{data_json}</script>
<script type="application/json" id="kanbandata">{kanban_json}</script>
<script type="application/json" id="gradesdata">{grades_json}</script>
<script>
  const TITLES = {{week:'Week board', todo:'To-Do', disc:'Discussions', ann:'Announcements', crs:'Courses', grades:'Grades', inbox:'Inbox', settings:'Settings'}};
  const COURSES = {courses_compact};
  document.querySelectorAll('.nav').forEach(t => t.addEventListener('click', () => {{
    document.querySelectorAll('.nav').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById(t.dataset.p).classList.add('active');
    document.getElementById('pageTitle').textContent = TITLES[t.dataset.p] || '';
  }}));

  // ---- Settings (saved per browser, except a few app-side prefs noted below) ----
  const LS = localStorage;
  const $id = id => document.getElementById(id);

  // Toast helper (used by refresh nudge + saved-pref hints)
  const toast = $id('toast');
  let toastAction = null;
  function showToast(msg, action) {{ toast.textContent = msg; toastAction = action || null; toast.classList.add('show');
    clearTimeout(toast._t); if (!action) toast._t = setTimeout(() => toast.classList.remove('show'), 4000); }}
  toast.addEventListener('click', () => {{ if (toastAction) toastAction(); toast.classList.remove('show'); }});

  // Theme (light / dark / match system)
  const themeSel = $id('themeSel');
  const resolveDark = v => v === 'dark' || (v === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  themeSel.value = LS.getItem('semester.theme') || 'light';
  themeSel.addEventListener('change', () => {{
    LS.setItem('semester.theme', themeSel.value);
    document.documentElement.classList.toggle('dark', resolveDark(themeSel.value));
  }});

  // Accent
  const accentInput = $id('accentInput');
  const savedAccent = LS.getItem('semester.accent'); if (savedAccent) accentInput.value = savedAccent;
  accentInput.addEventListener('input', () => {{
    document.documentElement.style.setProperty('--accent', accentInput.value);
    LS.setItem('semester.accent', accentInput.value);
  }});

  // Generic binders for client-side board settings (re-render on change)
  const bindSel = (id, key, def, after) => {{ const el = $id(id); el.value = LS.getItem(key) || def;
    el.addEventListener('change', () => {{ LS.setItem(key, el.value); if (after) after(); }}); }};
  const bindNum = (id, key, def, after) => {{ const el = $id(id); el.value = LS.getItem(key) || def;
    el.addEventListener('change', () => {{ LS.setItem(key, el.value); if (after) after(); }}); }};
  const bindTog = (id, key, defOn, after) => {{ const el = $id(id); el.checked = (LS.getItem(key) || (defOn ? 'on' : 'off')) === 'on';
    el.addEventListener('change', () => {{ LS.setItem(key, el.checked ? 'on' : 'off'); if (after) after(); }}); }};

  bindSel('boardView', 'semester.boardView', 'rolling', () => renderBoard());
  bindSel('weekStart', 'semester.weekStart', '0', () => renderBoard());
  bindTog('weekends', 'semester.weekends', true, () => renderBoard());
  bindNum('dayThreshold', 'semester.dayThreshold', '4', () => renderBoard());
  bindSel('defaultTab', 'semester.defaultTab', 'week', null);

  // App-side prefs: persist to config.json via the server, then offer a reload.
  async function savePref(obj) {{ try {{ await fetch('/api/prefs', {{ method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(obj) }}); }} catch (e) {{}} }}
  const aggr = $id('aggr'); aggr.value = LS.getItem('semester.aggr') || 'balanced';
  aggr.addEventListener('change', () => {{ LS.setItem('semester.aggr', aggr.value);
    savePref({{ aggressiveness: aggr.value }}); showToast('Saved — reload to recalculate start dates.', () => location.reload()); }});
  const autoOn = $id('autoOn'), autoMin = $id('autoMin');
  autoOn.checked = (LS.getItem('semester.autoOn') || 'on') === 'on';
  autoMin.value = LS.getItem('semester.autoMin') || '60';
  const saveAuto = () => {{ LS.setItem('semester.autoOn', autoOn.checked ? 'on' : 'off'); LS.setItem('semester.autoMin', autoMin.value);
    savePref({{ autorefresh: autoOn.checked, autorefresh_min: parseInt(autoMin.value, 10) || 60 }}); }};
  autoOn.addEventListener('change', saveAuto); autoMin.addEventListener('change', saveAuto);
  const bgNotify = $id('bgNotify');
  bgNotify.checked = (LS.getItem('semester.bgNotify') || 'off') === 'on';
  bgNotify.addEventListener('change', async () => {{
    LS.setItem('semester.bgNotify', bgNotify.checked ? 'on' : 'off');
    try {{
      const r = await fetch('/api/notify', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ enabled: bgNotify.checked, interval: parseInt(autoMin.value, 10) || 60 }}) }});
      const d = await r.json(); showToast(d.msg || 'Updated.');
    }} catch (e) {{ showToast('Background reminders need the installed app.'); }}
  }});

  // Section show/hide
  const loadTabs = () => {{ try {{ return JSON.parse(LS.getItem('semester.tabs')) || {{}}; }} catch (e) {{ return {{}}; }} }};
  function applyTabs() {{
    const prefs = loadTabs();
    document.querySelectorAll('.nav[data-p]').forEach(n => {{
      const p = n.dataset.p;
      if (p === 'week' || p === 'settings') return;
      const on = prefs[p] !== false;
      n.style.display = on ? '' : 'none';
      if (!on && n.classList.contains('active')) document.querySelector('.nav[data-p="week"]').click();
    }});
  }}
  document.querySelectorAll('input[data-tab]').forEach(cb => {{
    cb.checked = loadTabs()[cb.dataset.tab] !== false;
    cb.addEventListener('change', () => {{
      const p = loadTabs(); p[cb.dataset.tab] = cb.checked;
      LS.setItem('semester.tabs', JSON.stringify(p)); applyTabs();
    }});
  }});
  applyTabs();

  // Open the user's default tab on launch
  const defTab = LS.getItem('semester.defaultTab');
  if (defTab && defTab !== 'week') {{ const b = document.querySelector('.nav[data-p="' + defTab + '"]');
    if (b && b.style.display !== 'none') b.click(); }}

  const DATA = JSON.parse(document.getElementById('itemdata').textContent);
  const modal = document.getElementById('modal');
  function openModal(id) {{
    const d = DATA[id]; if (!d) return;
    document.getElementById('mCourse').textContent = (d.type === 'discussion' ? '💬 ' : '📝 ') + d.course;
    document.getElementById('mTitle').textContent = d.title;
    const meta = [];
    if (d.due) meta.push('⏰ Due ' + d.due);
    if (d.start) meta.push('▶ Start ' + d.start);
    if (d.points) meta.push('🏆 ' + d.points);
    document.getElementById('mMeta').innerHTML = meta.map(m => '<span>' + m + '</span>').join('');
    document.getElementById('mDesc').innerHTML = d.desc || '<i>No description provided in Canvas.</i>';
    document.getElementById('mLink').href = d.url;
    // Feedback (score, rubric, comments)
    let fb = '';
    if (d.score != null || (d.comments && d.comments.length) || (d.rubric && d.rubric.length)) {{
      fb += '<div class="fb-h">Feedback</div>';
      if (d.score != null) fb += '<div class="fb-score">Score: ' + d.score + (d.points ? (' / ' + d.points.replace(' pts', '')) : '') + '</div>';
      if (d.rubric && d.rubric.length) {{
        fb += '<table class="fb-rub">';
        d.rubric.forEach(r => {{ fb += '<tr><td>' + esc(r.desc || '') + '</td><td class="gs">' + (r.points != null ? r.points : '—') + (r.max != null ? (' / ' + r.max) : '') + '</td></tr>' + (r.comment ? '<tr><td colspan="2" class="fb-c">' + esc(r.comment) + '</td></tr>' : ''); }});
        fb += '</table>';
      }}
      if (d.comments && d.comments.length) {{
        fb += '<div class="fb-cm">';
        d.comments.forEach(c => {{ fb += '<div class="fb-cmrow"><b>' + esc(c.author) + '</b> ' + esc(c.text) + '</div>'; }});
        fb += '</div>';
      }}
    }}
    document.getElementById('mFb').innerHTML = fb;
    // Actions
    let act = '';
    if (d.assignId) act += '<button class="dlbtn" id="mDone">' + (d.submitted ? 'Done ✓ — undo' : 'Mark done') + '</button>';
    document.getElementById('mActions').innerHTML = act;
    const doneBtn = document.getElementById('mDone');
    if (doneBtn) doneBtn.addEventListener('click', () => markDone(d));
    // Notes (synced to Canvas) + focus timer
    let nh = '';
    if (d.assignId) {{
      nh = '<div class="notes-h">Notes <span class="notes-sub">— synced to your Canvas planner</span></div>'
        + '<textarea id="mNote" rows="3" placeholder="Private notes for this assignment..."></textarea>'
        + '<div class="notes-row"><button class="ghostbtn" id="mNoteSave">Save note</button>'
        + '<button class="ghostbtn" id="mFocus">Focus 25 min</button><span class="logged" id="mLogged"></span></div>';
    }}
    document.getElementById('mNotes').innerHTML = nh;
    if (d.assignId) {{
      document.getElementById('mNote').value = d.note || '';
      document.getElementById('mNoteSave').addEventListener('click', () => saveNote(d));
      document.getElementById('mFocus').addEventListener('click', () => startFocus(d));
      const lg = getLogged(d.uid); document.getElementById('mLogged').textContent = lg ? ('⏱ ' + lg + 'm logged') : '';
    }}
    // Submit (text / URL)
    const canText = (d.subTypes || []).includes('online_text_entry');
    const canUrl = (d.subTypes || []).includes('online_url');
    let sub = '';
    if (d.assignId && !d.submitted && (canText || canUrl)) {{
      const opts = (canText ? '<option value="online_text_entry">Text entry</option>' : '')
        + (canUrl ? '<option value="online_url">Website URL</option>' : '');
      sub = '<div class="sub-h">Submit</div><select id="subType">' + opts + '</select>'
        + '<textarea id="subText" rows="4" placeholder="Type your submission..."></textarea>'
        + '<input id="subUrl" type="url" placeholder="https://..." style="display:none">'
        + '<div class="notes-row"><button class="dlbtn" id="subBtn">Submit to Canvas</button>'
        + '<span class="sub-warn">This submits for real.</span></div>';
    }} else if (d.assignId && !d.submitted) {{
      sub = '<div class="sub-note">Submitted through Canvas (e.g. zyBooks or a quiz) — use “Open in Canvas”.</div>';
    }}
    document.getElementById('mSubmit').innerHTML = sub;
    if (document.getElementById('subType')) {{
      const st = document.getElementById('subType'), tx = document.getElementById('subText'), ur = document.getElementById('subUrl');
      const upd = () => {{ const u = st.value === 'online_url'; ur.style.display = u ? '' : 'none'; tx.style.display = u ? 'none' : ''; }};
      st.addEventListener('change', upd); upd();
      document.getElementById('subBtn').addEventListener('click', () => submitWork(d));
    }}
    modal.classList.add('open');
  }}
  async function submitWork(d) {{
    const type = document.getElementById('subType').value;
    const content = type === 'online_url' ? document.getElementById('subUrl').value.trim() : document.getElementById('subText').value;
    if (!content) {{ showToast('Nothing to submit yet.'); return; }}
    if (!confirm('Submit this to Canvas now? This counts as a real submission.')) return;
    try {{
      const r = await fetch('/api/canvas/submit', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ course_id: d.courseId, assign_id: d.assignId, type, content }}) }});
      const j = await r.json();
      if (j.ok) {{ showToast('Submitted to Canvas ✓ — reload to update.', () => location.reload()); closeModal(); }}
      else showToast('Submit failed: ' + (j.error || 'unknown'));
    }} catch (e) {{ showToast('Submit needs the installed app.'); }}
  }}
  function closeModal() {{ modal.classList.remove('open'); }}
  async function markDone(d) {{
    const makeDone = !d.submitted;
    try {{
      const r = await fetch('/api/canvas/done', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ assign_id: d.assignId, done: makeDone }}) }});
      const j = await r.json();
      if (j.ok) {{ showToast(makeDone ? 'Marked done in Canvas — reload to update.' : 'Marked not done — reload to update.', () => location.reload()); closeModal(); }}
      else showToast(j.error || 'Could not update (installed app only).');
    }} catch (e) {{ showToast('Mark-done needs the installed app.'); }}
  }}
  document.querySelectorAll('[data-id]').forEach(c =>
    c.addEventListener('click', () => openModal(+c.dataset.id)));
  document.getElementById('modalX').addEventListener('click', closeModal);
  modal.addEventListener('click', e => {{ if (e.target === modal) closeModal(); }});
  document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});

  // ---- Week board (drag-to-reschedule, saved in localStorage) ----
  const KANBAN = JSON.parse(document.getElementById('kanbandata').textContent);
  const PLAN_KEY = 'canvasPlannerPlan';
  const HIDE_KEY = 'canvasPlannerHidden';
  const SORT_KEY = 'canvasPlannerSort';
  const loadHidden = () => {{ try {{ return new Set(JSON.parse(localStorage.getItem(HIDE_KEY)) || []); }} catch (e) {{ return new Set(); }} }};
  const saveHidden = s => localStorage.setItem(HIDE_KEY, JSON.stringify([...s]));
  let traySort = localStorage.getItem(SORT_KEY) || 'due';

  function sortItems(arr) {{
    const a = arr.slice();
    if (traySort === 'points') a.sort((x, y) => (Number(y.points) || 0) - (Number(x.points) || 0));
    else if (traySort === 'course') a.sort((x, y) => x.course.localeCompare(y.course) || (x.due || '~').localeCompare(y.due || '~'));
    else if (traySort === 'title') a.sort((x, y) => x.title.localeCompare(y.title));
    else a.sort((x, y) => (x.due || '~').localeCompare(y.due || '~'));
    return a;
  }}

  function renderChips() {{
    const hidden = loadHidden();
    const seen = {{}}; const courses = [];
    KANBAN.forEach(it => {{ if (!(it.course in seen)) {{ seen[it.course] = it.color; courses.push(it.course); }} }});
    const box = document.getElementById('courseChips');
    box.innerHTML = '';
    courses.forEach(c => {{
      const chip = document.createElement('span');
      chip.className = 'chip' + (hidden.has(c) ? ' off' : '');
      chip.innerHTML = '<span class="cdot" style="background:' + seen[c] + '"></span>' + esc(c);
      chip.addEventListener('click', () => {{
        const h = loadHidden();
        if (h.has(c)) h.delete(c); else h.add(c);
        saveHidden(h); renderChips(); renderBoard();
      }});
      box.appendChild(chip);
    }});
  }}
  const DAY = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const loadPlan = () => {{ try {{ return JSON.parse(localStorage.getItem(PLAN_KEY)) || {{}}; }} catch (e) {{ return {{}}; }} }};
  const savePlan = p => localStorage.setItem(PLAN_KEY, JSON.stringify(p));
  const ymd = d => d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
  const dueYmd = iso => iso ? ymd(new Date(iso)) : null;
  const esc = s => {{ const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }};

  function makeCard(it) {{
    const card = document.createElement('div'); card.className = 'kcard'; card.draggable = true;
    card.dataset.uid = it.uid; card.dataset.did = it.did;
    card.style.background = it.color + '24'; card.style.border = '0.5px solid ' + it.color + '59';
    card.innerHTML = '<div class="kt">' + esc(it.title) + '</div><div class="km"><span style="color:' + it.color + '">' + esc(it.course) + '</span>' + (it.due ? '<span class="due">' + (it.approx ? '~due ' : 'due ') + esc(it.dueLabel) + '</span>' : '') + '</div>';
    card.addEventListener('dragstart', e => {{ e.dataTransfer.setData('text/uid', it.uid); card.classList.add('dragging'); }});
    card.addEventListener('dragend', () => card.classList.remove('dragging'));
    card.addEventListener('click', () => openModal(+card.dataset.did));
    return card;
  }}

  function weekCols() {{
    const today = new Date(); today.setHours(0,0,0,0);
    const view = localStorage.getItem('semester.boardView') || 'rolling';
    const weekStart = parseInt(localStorage.getItem('semester.weekStart') || '0', 10);
    const weekends = (localStorage.getItem('semester.weekends') || 'on') === 'on';
    let start = new Date(today);
    if (view === 'week') {{ const diff = (today.getDay() - weekStart + 7) % 7; start.setDate(today.getDate() - diff); }}
    const cols = [];
    for (let i = 0; cols.length < 7 && i < 16; i++) {{
      const d = new Date(start); d.setDate(start.getDate() + i);
      const wd = d.getDay(); if (!weekends && (wd === 0 || wd === 6)) continue;
      cols.push({{key: ymd(d), date: d}});
    }}
    return {{cols, todayKey: ymd(today)}};
  }}

  function renderBoard() {{
    const plan = loadPlan();
    const board = document.getElementById('weekboard');
    const tray = document.getElementById('weektray');
    board.innerHTML = ''; tray.innerHTML = '';
    const {{cols, todayKey}} = weekCols();
    const windowKeys = new Set(cols.map(c => c.key));
    const targets = {{}};
    cols.forEach(c => {{
      const isToday = c.key === todayKey;
      const el = document.createElement('div'); el.className = 'col' + (isToday ? ' today' : ''); el.dataset.key = c.key;
      el.innerHTML = '<h4>' + (isToday ? 'Today' : DAY[c.date.getDay()]) + ' <span class="d">' + MON[c.date.getMonth()] + ' ' + c.date.getDate() + '</span></h4>';
      board.appendChild(el); targets[c.key] = el;
    }});
    tray.dataset.key = 'tray'; targets['tray'] = tray;

    const hidden = loadHidden();
    const visible = sortItems(KANBAN.filter(it => !hidden.has(it.course)));
    visible.forEach(it => {{
      const pd = plan[it.uid];
      const dueK = it.due ? dueYmd(it.due) : null;
      // Only honor a placement that's a visible day AND not after the due date.
      const ok = pd && windowKeys.has(pd) && (!dueK || pd <= dueK);
      targets[ok ? pd : 'tray'].appendChild(makeCard(it));
    }});

    const thr = parseInt(localStorage.getItem('semester.dayThreshold') || '4', 10);
    cols.forEach(c => {{ const el = targets[c.key];
      const n = el.querySelectorAll('.kcard').length;
      el.classList.toggle('load-warn', n >= thr && n < thr + 2);
      el.classList.toggle('load-heavy', n >= thr + 2);
      if (n) el.querySelector('h4').insertAdjacentHTML('beforeend', '<span class="load">' + n + '</span>');
      else {{ const p = document.createElement('div'); p.className = 'empty-col'; p.textContent = '·'; el.appendChild(p); }}
    }});
    if (!tray.querySelector('.kcard')) {{ const p = document.createElement('div'); p.className = 'tray-empty'; p.textContent = 'All scheduled. 🎉'; tray.appendChild(p); }}

    Object.values(targets).forEach(el => {{
      el.addEventListener('dragover', e => {{ e.preventDefault(); el.classList.add('drop'); }});
      el.addEventListener('dragleave', () => el.classList.remove('drop'));
      el.addEventListener('drop', e => {{
        e.preventDefault(); el.classList.remove('drop');
        const uid = e.dataTransfer.getData('text/uid'); if (!uid) return;
        const key = el.dataset.key;
        if (key !== 'tray') {{
          const it = KANBAN.find(x => x.uid === uid);
          if (it && it.due && key > dueYmd(it.due)) {{ showToast('Cannot schedule after the due date.'); return; }}
        }}
        const p = loadPlan();
        if (key === 'tray') delete p[uid]; else p[uid] = key;
        savePlan(p); renderBoard();
      }});
    }});
  }}

  const sortSel = document.getElementById('traySort');
  sortSel.value = traySort;
  sortSel.addEventListener('change', () => {{ traySort = sortSel.value; localStorage.setItem(SORT_KEY, traySort); renderBoard(); }});
  renderChips();
  renderBoard();

  // ---- Auto-schedule: fill the tray across the week by strategy ----
  function autoSchedule(algo) {{
    const {{cols}} = weekCols();
    const dayKeys = cols.map(c => c.key);
    if (!dayKeys.length) return;
    const cap = parseInt(localStorage.getItem('semester.dayThreshold') || '4', 10);
    const plan = loadPlan();
    const hidden = loadHidden();
    const load = {{}}; dayKeys.forEach(k => load[k] = 0);
    // Seed each day's load from cards already placed by hand (we keep those).
    KANBAN.forEach(it => {{ if (hidden.has(it.course)) return; const d = plan[it.uid]; if (d && load[d] !== undefined) load[d]++; }});
    // Candidates: visible items still in the tray (unscheduled), most urgent first.
    const tray = KANBAN.filter(it => it.due && !hidden.has(it.course) && !(plan[it.uid] && dayKeys.includes(plan[it.uid])));
    tray.sort((a, b) => (a.due || '~').localeCompare(b.due || '~'));
    const idxOf = k => dayKeys.indexOf(k);
    for (const it of tray) {{
      const dk = it.due ? dueYmd(it.due) : null;
      let allowed = dayKeys.filter(k => !dk || k <= dk);   // never past the due date
      if (!allowed.length) allowed = [dayKeys[0]];          // due before the window -> earliest day
      const open = allowed.filter(k => load[k] < cap);      // respect the daily cap
      if (!open.length) continue;                            // no room -> leave it in the tray
      let pick;
      if (algo === 'front') pick = open[0];
      else if (algo === 'jit') pick = open[open.length - 1];
      else if (algo === 'balanced') pick = open.reduce((b, k) => load[k] < load[b] ? k : b, open[0]);
      else {{  // deadline-first: open day nearest the suggested start day
        const sk = it.start ? dueYmd(it.start) : null;
        let ti = 0;
        if (sk) {{ const ix = idxOf(sk); ti = ix >= 0 ? ix : Math.max(0, dayKeys.filter(k => k <= sk).length - 1); }}
        pick = open.reduce((b, k) => Math.abs(idxOf(k) - ti) < Math.abs(idxOf(b) - ti) ? k : b, open[0]);
      }}
      plan[it.uid] = pick; load[pick]++;
    }}
    savePlan(plan); renderBoard();
  }}
  document.getElementById('autoBtn').addEventListener('click', () => {{
    const sel = document.getElementById('autoAlgo');
    autoSchedule(sel.value);
    showToast('Week planned · ' + sel.options[sel.selectedIndex].text);
  }});
  document.getElementById('clearPlan').addEventListener('click', () => {{
    const p = loadPlan(); KANBAN.forEach(it => delete p[it.uid]); savePlan(p); renderBoard();
    showToast('Cleared — everything is back in the tray.');
  }});

  // ---- Calendar export (.ics) ----
  const pad = n => String(n).padStart(2, '0');
  const icsDate = d => d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate());
  function buildICS() {{
    const inclDue = $id('icsDue').checked, inclPlan = $id('icsPlan').checked, plan = loadPlan();
    const L = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Semester//EN', 'CALSCALE:GREGORIAN'];
    const stamp = icsDate(new Date()) + 'T000000Z';
    const ev = (date, summary, uid) => {{ const d = new Date(date), e = new Date(d); e.setDate(d.getDate() + 1);
      L.push('BEGIN:VEVENT', 'UID:' + uid + '@semester', 'DTSTAMP:' + stamp,
             'DTSTART;VALUE=DATE:' + icsDate(d), 'DTEND;VALUE=DATE:' + icsDate(e),
             'SUMMARY:' + summary.replace(/[,;\\\\]/g, ' '), 'END:VEVENT'); }};
    KANBAN.forEach(it => {{
      if (inclDue && it.due) ev(new Date(it.due), 'Due: ' + it.title + ' (' + it.course + ')', 'due-' + it.uid);
      if (inclPlan && plan[it.uid]) ev(plan[it.uid] + 'T12:00:00', 'Work on: ' + it.title + ' (' + it.course + ')', 'plan-' + it.uid);
    }});
    L.push('END:VCALENDAR'); return L.join('\\r\\n');
  }}
  $id('icsExport').addEventListener('click', () => {{
    const blob = new Blob([buildICS()], {{ type: 'text/calendar' }}), url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'semester.ics'; a.click(); URL.revokeObjectURL(url);
  }});

  // ---- Auto-refresh nudge (installed app only; static file just no-ops) ----
  const PAGE_BUILT = {int(now.timestamp())};
  async function checkUpdates() {{
    if ((LS.getItem('semester.autoOn') || 'on') !== 'on') return;
    try {{ const r = await fetch('/api/status', {{ cache: 'no-store' }}); const s = await r.json();
      if (s.built && s.built > PAGE_BUILT) showToast('Fresh data from Canvas — click to reload.', () => location.reload());
    }} catch (e) {{}}
  }}
  setInterval(checkUpdates, 5 * 60 * 1000);

  // ---- Notifications while the app is open ----
  function notifyDueSoon() {{
    if (!('Notification' in window)) return;
    if (Notification.permission === 'default') Notification.requestPermission();
    if (Notification.permission !== 'granted') return;
    let done; try {{ done = new Set(JSON.parse(LS.getItem('semester.notified')) || []); }} catch (e) {{ done = new Set(); }}
    const now2 = new Date(), todayK = ymd(now2), plan = loadPlan();
    KANBAN.forEach(it => {{
      if (it.due) {{ const hrs = (new Date(it.due) - now2) / 36e5, k = 'due:' + it.uid;
        if (hrs > 0 && hrs <= 12 && !done.has(k)) {{ new Notification('Due soon: ' + it.title, {{ body: it.course + ' — due ' + it.dueLabel }}); done.add(k); }} }}
      if (it.start) {{ const sk = 'start:' + it.uid + ':' + todayK;
        if (ymd(new Date(it.start)) === todayK && !done.has(sk)) {{ new Notification('Time to start: ' + it.title, {{ body: it.course + ' — suggested start day' }}); done.add(sk); }} }}
    }});
    LS.setItem('semester.notified', JSON.stringify([...done]));
  }}
  setTimeout(notifyDueSoon, 2500); setInterval(notifyDueSoon, 30 * 60 * 1000);

  // ---- Grades: live what-if / what-do-I-need projector ----
  const GRADES = JSON.parse(document.getElementById('gradesdata').textContent);
  function projected(course, hypo) {{
    if (course.weighted) {{
      const groups = {{}};
      course.items.forEach((it, i) => {{ const v = (hypo[i] != null) ? hypo[i] : (it.graded ? it.score : null);
        if (v == null || !it.points) return; const g = groups[it.group] || (groups[it.group] = {{ w: it.weight || 0, e: 0, p: 0 }}); g.e += v; g.p += it.points; }});
      let ws = 0, acc = 0; Object.values(groups).forEach(g => {{ if (g.p > 0 && g.w > 0) {{ acc += g.w * (g.e / g.p); ws += g.w; }} }});
      return ws > 0 ? acc / ws * 100 : null;
    }}
    let e = 0, p = 0; course.items.forEach((it, i) => {{ const v = (hypo[i] != null) ? hypo[i] : (it.graded ? it.score : null); if (v == null || !it.points) return; e += v; p += it.points; }});
    return p > 0 ? e / p * 100 : null;
  }}
  function neededFor(course, target) {{
    const rem = course.items.map((it, i) => i).filter(i => !course.items[i].graded && course.items[i].points);
    if (!rem.length) return null;
    let lo = 0, hi = 100;
    for (let k = 0; k < 40; k++) {{ const mid = (lo + hi) / 2; const hypo = {{}}; rem.forEach(i => hypo[i] = course.items[i].points * mid / 100);
      const pr = projected(course, hypo); if (pr == null) return null; if (pr < target) lo = mid; else hi = mid; }}
    return (lo + hi) / 2;
  }}
  function renderGrades() {{
    const box = document.getElementById('gradesBox');
    if (!GRADES.length) {{ box.innerHTML = '<p class="empty">No grades available.</p>'; return; }}
    box.innerHTML = '';
    GRADES.forEach(course => {{
      const hypo = {{}};
      const card = document.createElement('div'); card.className = 'course-card'; card.style.borderTop = '3px solid ' + course.color;
      const cur = course.score != null ? (course.score.toFixed(1) + '%' + (course.grade ? (' · ' + course.grade) : '')) : '—';
      let rows = '';
      course.items.forEach((it, i) => {{
        const val = it.graded && it.score != null ? it.score : '';
        rows += '<tr><td>' + esc(it.title) + '</td><td class="gs"><input type="number" data-i="' + i + '" value="' + val + '"' + (it.graded ? '' : ' placeholder="—"') + '> <span class="gp">/ ' + (it.points != null ? (+it.points).toFixed(0) : '?') + '</span></td></tr>';
      }});
      const p0 = projected(course, {{}});
      card.innerHTML = '<div class="cc-top"><h3><span class="dot" style="background:' + course.color + '"></span>' + esc(course.cleanName) + '</h3><span class="bigscore">' + cur + '</span></div>'
        + '<div class="proj">Projected <span class="pv" data-pv>' + (p0 != null ? p0.toFixed(1) + '%' : '—') + '</span>'
        + '<span class="needline">Target <input type="number" class="target-in" data-target value="90">% → <span data-need></span></span></div>'
        + '<table class="gtable">' + rows + '</table>';
      box.appendChild(card);
      const pv = card.querySelector('[data-pv]'), needEl = card.querySelector('[data-need]'), tin = card.querySelector('[data-target]');
      const recompute = () => {{
        const pr = projected(course, hypo); pv.textContent = pr != null ? pr.toFixed(1) + '%' : '—';
        const tgt = parseFloat(tin.value); const need = (!isNaN(tgt)) ? neededFor(course, tgt) : null;
        needEl.innerHTML = need == null ? 'all graded' : ('need <b>' + need.toFixed(1) + '%</b> avg on remaining');
      }};
      card.querySelectorAll('input[data-i]').forEach(inp => inp.addEventListener('input', () => {{
        const i = +inp.dataset.i, v = parseFloat(inp.value); if (inp.value === '' || isNaN(v)) delete hypo[i]; else hypo[i] = v; recompute();
      }}));
      tin.addEventListener('input', recompute); recompute();
    }});
  }}
  renderGrades();

  // ---- Notes (synced) + Focus timer ----
  const timeLog = () => {{ try {{ return JSON.parse(LS.getItem('semester.time')) || {{}}; }} catch (e) {{ return {{}}; }} }};
  const getLogged = uid => Math.round(timeLog()[uid] || 0);
  async function saveNote(d) {{
    const text = document.getElementById('mNote').value;
    try {{
      const r = await fetch('/api/canvas/note', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ assign_id: d.assignId, text, title: d.title }}) }});
      const j = await r.json();
      showToast(j.ok ? 'Note saved to your Canvas planner.' : 'Could not save note (installed app only).');
    }} catch (e) {{ showToast('Notes need the installed app.'); }}
  }}
  let focusTimer = null, focusRemain = 0, focusUid = null, focusPaused = false;
  const focusBar = document.getElementById('focusBar');
  const fmtT = s => Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  function startFocus(d) {{
    focusUid = d.uid; focusRemain = 25 * 60; focusPaused = false;
    focusBar.innerHTML = '<b>Focus</b> <span class="ft">25:00</span> <button data-p aria-label="pause">⏸</button><button data-s aria-label="stop">■</button>';
    focusBar.classList.add('show');
    focusBar.querySelector('[data-p]').onclick = () => {{ focusPaused = !focusPaused; focusBar.querySelector('[data-p]').textContent = focusPaused ? '▶' : '⏸'; }};
    focusBar.querySelector('[data-s]').onclick = () => stopFocus(false);
    clearInterval(focusTimer);
    focusTimer = setInterval(() => {{
      if (focusPaused) return;
      if (--focusRemain <= 0) {{ stopFocus(true); return; }}
      focusBar.querySelector('.ft').textContent = fmtT(focusRemain);
    }}, 1000);
    closeModal();
  }}
  function stopFocus(completed) {{
    clearInterval(focusTimer); focusBar.classList.remove('show');
    const mins = completed ? 25 : Math.round((25 * 60 - focusRemain) / 60);
    if (focusUid && mins > 0) {{ const t = timeLog(); t[focusUid] = (t[focusUid] || 0) + mins; LS.setItem('semester.time', JSON.stringify(t)); }}
    if (completed) {{ showToast('Focus session done — 25 min logged.');
      if ('Notification' in window && Notification.permission === 'granted') new Notification('Focus session complete', {{ body: 'Nice work — take a break.' }}); }}
  }}

  // ---- Inbox (read + reply + compose) ----
  let inboxLoaded = false;
  const inboxBox = document.getElementById('inboxBox');
  const jget = async url => (await fetch(url)).json();
  const jpost = async (url, b) => (await fetch(url, {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(b) }})).json();
  const fmtDate = s => {{ try {{ return new Date(s).toLocaleString([], {{ month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }}); }} catch (e) {{ return ''; }} }};
  async function loadInbox() {{
    inboxLoaded = true; inboxBox.innerHTML = '<p class="empty">Loading messages…</p>';
    const j = await jget('/api/canvas/conversations');
    if (!j.ok) {{ inboxBox.innerHTML = '<p class="empty">' + esc(j.error || 'Inbox needs the installed app.') + '</p>'; return; }}
    if (!j.items.length) {{ inboxBox.innerHTML = '<p class="empty">No messages.</p>'; return; }}
    inboxBox.innerHTML = j.items.map(c => '<div class="conv' + (c.unread ? ' unread' : '') + '" data-id="' + c.id + '"><div class="cs">' + esc(c.subject) + '</div><div class="cw">' + esc(c.with || '') + ' · ' + fmtDate(c.date) + '</div><div class="cp">' + esc(c.preview || '') + '</div></div>').join('');
    inboxBox.querySelectorAll('.conv').forEach(el => el.addEventListener('click', () => openThread(+el.dataset.id)));
  }}
  async function openThread(id) {{
    inboxBox.innerHTML = '<p class="empty">Loading…</p>';
    const j = await jget('/api/canvas/conversation?id=' + id);
    if (!j.ok) {{ inboxBox.innerHTML = '<p class="empty">' + esc(j.error || 'Error') + '</p>'; return; }}
    let h = '<span class="backlink" id="ibBack">← Inbox</span><h2 style="font-size:18px;margin:0 0 12px">' + esc(j.subject || '') + '</h2><div class="thread">';
    h += j.messages.map(m => '<div class="msg"><span class="ma">' + esc(m.author) + '</span> <span class="md">' + fmtDate(m.date) + '</span><div class="mb">' + esc(m.body || '') + '</div></div>').join('');
    h += '</div><div class="composer"><textarea id="replyBody" rows="3" placeholder="Reply…"></textarea><button class="dlbtn" id="replyBtn">Send reply</button></div>';
    inboxBox.innerHTML = h;
    document.getElementById('ibBack').onclick = () => loadInbox();
    document.getElementById('replyBtn').onclick = async () => {{
      const body = document.getElementById('replyBody').value.trim(); if (!body) {{ showToast('Reply is empty.'); return; }}
      const r = await jpost('/api/canvas/reply', {{ id, body }});
      if (r.ok) {{ showToast('Reply sent.'); openThread(id); }} else showToast('Could not send: ' + (r.error || ''));
    }};
  }}
  function composeView() {{
    const opts = COURSES.map(c => '<option value="' + c.id + '">' + esc(c.name) + '</option>').join('');
    inboxBox.innerHTML = '<span class="backlink" id="ibBack">← Inbox</span><div class="composer">'
      + '<select id="cmpCourse"><option value="">Choose a course…</option>' + opts + '</select>'
      + '<div id="cmpTeachers" class="empty" style="font-size:13px;margin-bottom:8px"></div>'
      + '<input id="cmpSubject" placeholder="Subject"><textarea id="cmpBody" rows="4" placeholder="Message…"></textarea>'
      + '<button class="dlbtn" id="cmpSend">Send</button></div>';
    document.getElementById('ibBack').onclick = () => loadInbox();
    let recipients = [];
    document.getElementById('cmpCourse').addEventListener('change', async e => {{
      recipients = []; const cid = e.target.value; const box = document.getElementById('cmpTeachers');
      if (!cid) {{ box.textContent = ''; return; }}
      box.textContent = 'Loading instructors…';
      const j = await jget('/api/canvas/teachers?course_id=' + cid);
      if (!j.ok || !j.teachers.length) {{ box.textContent = 'No instructors found.'; return; }}
      recipients = j.teachers.map(t => t.id);
      box.innerHTML = 'To: ' + j.teachers.map(t => esc(t.name)).join(', ');
    }});
    document.getElementById('cmpSend').onclick = async () => {{
      const subject = document.getElementById('cmpSubject').value.trim(), body = document.getElementById('cmpBody').value.trim();
      if (!recipients.length) {{ showToast('Pick a course first.'); return; }}
      if (!body) {{ showToast('Message is empty.'); return; }}
      const r = await jpost('/api/canvas/compose', {{ course_id: document.getElementById('cmpCourse').value, recipients, subject, body }});
      if (r.ok) {{ showToast('Message sent.'); loadInbox(); }} else showToast('Could not send: ' + (r.error || ''));
    }};
  }}
  document.querySelector('.nav[data-p="inbox"]').addEventListener('click', () => {{ if (!inboxLoaded) loadInbox(); }});
  document.getElementById('inboxRefresh').addEventListener('click', loadInbox);
  document.getElementById('composeBtn').addEventListener('click', composeView);

  // ---- Auto-update: check GitHub for a newer release ----
  const LABEL = {{ git: 'Update & restart', selfupdate: 'Download & install', download: 'Download' }};
  async function applyUpdate(mode) {{
    if (mode === 'selfupdate') showToast('Downloading & installing — Semester will relaunch…');
    else showToast('Updating…');
    try {{
      const j = await (await fetch('/api/update/run', {{ method: 'POST' }})).json();
      if (j.mode === 'git') showToast(j.ok ? 'Pulled latest — restart Semester to apply.' : ('Update failed: ' + (j.output || j.error || '')));
      else if (j.mode === 'selfupdate') showToast(j.result === 'installing' ? 'Installing… the app will reopen on the new version.' : 'Opening the download page…');
      else showToast('Opening the download page…');
    }} catch (e) {{ /* app may be quitting to swap itself — that's expected */ }}
  }}
  async function checkUpdate(manual) {{
    try {{
      const j = await (await fetch('/api/update')).json();
      if (j.newer) {{
        const bar = document.getElementById('updBar');
        bar.innerHTML = '🔔 Semester v' + j.latest + ' is available. <button id="updGo">' + (LABEL[j.mode] || 'Download') + '</button><button class="x" id="updX">✕</button>';
        bar.classList.add('show');
        document.getElementById('updGo').onclick = () => applyUpdate(j.mode);
        document.getElementById('updX').onclick = () => bar.classList.remove('show');
      }} else if (manual) {{ showToast("You're up to date (v" + j.current + ")."); }}
    }} catch (e) {{ if (manual) showToast('Update check failed.'); }}
  }}
  document.getElementById('checkUpdate').addEventListener('click', () => checkUpdate(true));
  const autoUp = document.getElementById('autoUpdate');
  if (autoUp) {{
    autoUp.checked = LS.getItem('semester.autoupdate') !== 'off';
    autoUp.addEventListener('change', () => LS.setItem('semester.autoupdate', autoUp.checked ? 'on' : 'off'));
  }}
  if (LS.getItem('semester.autoupdate') !== 'off') setTimeout(() => checkUpdate(false), 3000);
</script>
</body></html>"""


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
        return False, f"Couldn't reach {base_url} — check the web address. ({e})"
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

    log("Fetching courses…")
    courses = canvas.active_courses()
    colors = course_colors(courses)

    log("Fetching assignments + discussions…")
    items = build_items(canvas, courses, now, cfg.get("aggressiveness", "balanced"))
    warnings = workload_warnings(items, now)

    log("Fetching announcements + syllabus…")
    announcements = build_announcements(canvas, courses, now)
    courses_info = build_courses(canvas, courses, items)
    grades = build_grades(canvas, courses, items)

    with open(DATA_PATH, "w") as f:
        json.dump({"generated": now.isoformat(), "items": items, "warnings": warnings,
                   "announcements": announcements, "courses": courses_info, "grades": grades}, f, indent=2)

    accent = cfg.get("accent") or "#6366f1"
    html = render_html(items, warnings, courses_info, announcements, colors, now, accent, grades)
    with open(HTML_PATH, "w") as f:
        f.write(html)

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
        html = render_html(items, warnings, crs, ann, colors, now)
        with open(HTML_PATH, "w") as f:
            f.write(html)
        print(f"Demo dashboard -> {HTML_PATH}")
        if "--open" in sys.argv:
            webbrowser.open(f"file://{HTML_PATH}")
        return

    cfg = load_config()
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
