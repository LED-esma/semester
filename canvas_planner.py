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
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.19"  # keep in sync with the latest GitHub release tag


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
    with open(CONFIG_PATH, "w") as f:
        json.dump({"base_url": base_url, "token": token}, f, indent=2)
    print(f"\n✓ Saved {CONFIG_PATH} (kept private, never shared).\n")
    return {"base_url": base_url, "token": token}


def save_token(cfg, token):
    """Swap in a new access token, keeping every other setting as-is."""
    cfg = dict(cfg)
    cfg["token"] = token
    with open(CONFIG_PATH, "w") as f:
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
    with open(CONFIG_PATH) as f:
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
        with open(DATA_PATH) as f:
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
# Overdue items are intentionally excluded from the planner (see render_html).
BUCKET_META = [
    ("today", "Due Today", "#f97316"),
    ("week", "This Week", "#eab308"),
    ("later", "Later", "#3b82f6"),
    ("none", "No Due Date", "#6b7280"),
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


def _load_static(name):
    """Read a bundled static asset (works from source and PyInstaller bundles)."""
    base = getattr(sys, "_MEIPASS", HERE)
    with open(os.path.join(base, "static", name), encoding="utf-8") as f:
        return f.read()


def render_html(items, warnings, courses_info, announcements, colors, now, accent="#6366f1", grades=None, classes=None):
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
            "dueIso": it.get("due"),          # for filtering by due date
            "color": colors.get(it["course"], "#6366f1"),
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
        warn_html = f'<div class="warn"><h3>Heavy days ahead — start early</h3><ul>{rows}</ul></div>'

    todo = ""
    for bucket, label, color in BUCKET_META:
        cards = cards_by_bucket[bucket]
        if not cards:
            continue
        cell = ""
        for it in cards:
            pts = f'<span class="pts">{it["points"]:g} pts</span>' if it.get("points") else ""
            start = fmt_start(it.get("start"))
            start_html = f'<span class="start">start {start}</span>' if start and bucket in ("week", "later") else ""
            cell += f"""
            <div class="card" data-id="{reg(it)}" style="border-left-color:{colors.get(it['course'],'#555')}">
              <div class="card-top"><span class="course">{dot(it['course'])}{_esc(clean_course_name(it['course']))}</span>{pts}</div>
              <div class="title">{_esc(it['title'])}</div>
              <div class="card-bot"><span class="due">{fmt_due(it.get('due'))}</span>{start_html}</div>
            </div>"""
        todo += f'<section><h2 style="color:{color}">{label} <span class="count">{len(cards)}</span></h2><div class="grid">{cell}</div></section>'
    todo = todo or '<p class="empty">Nothing pending.</p>'

    # ---- Tab 2: Graded Discussions (excluding overdue) ----
    graded = [i for i in items if i["type"] == "discussion"
              and i.get("points") is not None and i["bucket"] != "overdue"]
    graded.sort(key=lambda x: (x["submitted"], x["due"] is None, x["due"] or ""))
    disc = ""
    for it in graded:
        pts = f'<span class="pts">{it["points"]:g} pts</span>' if it.get("points") else ""
        check = '<span class="start">Done</span>' if it["submitted"] else ""
        disc += f"""
        <div class="card" data-id="{reg(it)}" style="border-left-color:{colors.get(it['course'],'#555')}">
          <div class="card-top"><span class="course">{dot(it['course'])}{_esc(clean_course_name(it['course']))}</span>{pts}</div>
          <div class="title">{_esc(it['title'])}</div>
          <div class="card-bot"><span class="due">{fmt_due(it.get('due'))}</span>{check}</div>
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
            extra += f'<details><summary>Modules ({len(c.get("modules", []))})</summary><ul class="clist">{mod_html}</ul></details>'
        if files_html:
            extra += f'<details><summary>Files ({len(c.get("files", []))})</summary><ul class="clist">{files_html}</ul></details>'
        if pages_html:
            extra += f'<details><summary>Pages ({len(c.get("pages", []))})</summary><ul class="clist">{pages_html}</ul></details>'
        crs += f"""
        <div class="course-card" style="border-top:3px solid {col}">
          <div class="cc-top"><h3>{dot(c['name'])}{_esc(clean_course_name(c['name']))}</h3>
            <span class="pill">{c['pending']} to do</span></div>
          <div class="links">
            <a href="{_esc(c['url'])}" target="_blank">Open course ↗</a>
            <a href="{_esc(c['syllabus_url'])}" target="_blank">Full syllabus ↗</a>
          </div>
          <details><summary>Syllabus preview</summary><div class="syl">{syl}</div></details>
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
    classes_json = json.dumps(classes or [], ensure_ascii=False).replace("</", "<\\/")

    courses_compact = json.dumps([{"id": c.get("id"), "name": clean_course_name(c["name"])}
                                  for c in courses_info], ensure_ascii=False).replace("</", "<\\/")


    # Embed per-card details for the popup (escape </ so it can't break out of the tag).
    data_json = json.dumps(details, ensure_ascii=False).replace("</", "<\\/")

    css = _load_static("style.css").replace("__ACCENT__", accent)
    js = (_load_static("app.js")
          .replace("__PAGE_BUILT__", str(int(now.timestamp())))
          .replace("__COURSES__", courses_compact))

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Semester</title>
<link rel="icon" href='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="%230a0a0c"/><text x="50" y="54" font-family="Helvetica,Arial,sans-serif" font-size="74" font-weight="700" fill="white" text-anchor="middle" dominant-baseline="central">S</text></svg>'>
<style>
{css}
</style>
<script>try{{var t=localStorage.getItem('semester.theme')||'system';var dark=t==='dark'||(t==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);if(dark)document.documentElement.classList.add('dark');var a=localStorage.getItem('semester.accent');if(a)document.documentElement.style.setProperty('--accent',a);}}catch(e){{}}</script>
</head>
<body>
<div class="app">
  <aside class="rail">
    <div class="brand">Semester</div>
    <nav class="navlist">
      <button class="nav active" data-p="week"><span>Week board</span></button>
      <button class="nav" data-p="classes"><span>Classes</span><b class="badge">{len(classes or [])}</b></button>
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
      <div class="searchwrap"><input id="globalSearch" type="search" placeholder="Search everything…" autocomplete="off" role="combobox" aria-autocomplete="list" aria-controls="searchSuggest" aria-expanded="false"><ul class="suggest" id="searchSuggest" role="listbox" hidden></ul></div>
      <div class="updated">Updated {now.astimezone().strftime('%A, %B %-d at %-I:%M %p')}</div></div>
    <div class="stats">
      <div class="stat"><b>{len(pending)}</b><span>to do</span></div>
      <div class="stat"><b>{len(cards_by_bucket['today']) + len(cards_by_bucket['week'])}</b><span>due this week</span></div>
      <div class="stat"><b>{n_disc_todo}</b><span>discussions</span></div>
      <div class="stat"><b>{done}</b><span>done</span></div>
    </div>
    <div class="filterbar" id="filterBar">
      <div class="chips" id="courseChips"></div>
      <select id="fType" aria-label="Type"><option value="all">All types</option><option value="assignment">Assignments</option><option value="quiz">Quizzes</option><option value="discussion">Discussions</option></select>
      <select id="fDue" aria-label="Due date"><option value="any">Any due date</option><option value="today">Due today</option><option value="week">Due in 7 days</option><option value="2weeks">Due in 14 days</option><option value="nodate">No due date</option></select>
      <select id="fStatus" aria-label="Status"><option value="all">To do and done</option><option value="todo">To do</option><option value="done">Done</option></select>
      <button class="ghostbtn" id="fClear" hidden>Clear filters</button>
    </div>
    <div class="panel active" id="week">
      <div class="upnext" id="upnext"></div>
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
      <div class="tray-label">Unscheduled</div>
      <div class="tray" id="weektray"></div>
    </div>
    <div class="panel" id="classes"><div id="classView"></div></div>
  <div class="panel" id="todo">{warn_html}{todo}</div>
    <div class="panel" id="disc">{disc}</div>
    <div class="panel" id="ann">{ann}</div>
    <div class="panel" id="crs">{crs}</div>
    <div class="panel" id="grades"><div id="gradesBox"></div></div>
    <div class="panel" id="inbox">
      <div class="inbox-top"><button class="dlbtn" id="composeBtn">New message</button>
        <button class="ghostbtn" id="inboxRefresh">Refresh</button></div>
      <div id="inboxBox"><p class="empty">Loading messages…</p></div>
    </div>
    <div class="panel" id="settings">
      <div class="setgroup" id="accountGroup" style="display:none">
        <h2>Account</h2>
        <div class="setrow"><div><div class="st" id="acctName">Canvas account</div><div class="sd" id="acctSchool"></div></div>
          <button class="ghostbtn" id="logoutBtn">Log out</button></div>
      </div>
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
        <div class="setrow">
          <div><div class="st">Show badge and training courses</div><div class="sd">Orientation badges and compliance trainings are hidden by default. Applies in the installed app.</div></div>
          <label class="switch"><input type="checkbox" id="showAllCourses"><span class="track"></span></label>
        </div>
        <div class="setrow">
          <div><div class="st">Show tips</div><div class="sd">Short hints that appear once, the first time you use a feature.</div></div>
          <label class="switch"><input type="checkbox" id="showTips"><span class="track"></span></label>
        </div>
      </div>
      <div class="setgroup">
        <h2>Updates</h2>
        <div class="setrow"><div><div class="st">Auto-refresh</div><div class="sd">Checks Canvas for changes every few minutes and whenever you come back, then updates your dashboard. Uses very little data.</div></div>
          <label class="switch"><input type="checkbox" id="autoOn"><span class="track"></span></label></div>
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
        <div class="setrow" id="icsFeedRow" style="display:none"><div><div class="st">Live calendar feed</div><div class="sd">Subscribe once and your deadlines stay in sync while Semester is running.<span class="feedurl" id="icsFeedUrl"></span></div></div>
          <button class="ghostbtn" id="icsCopy">Copy URL</button></div>
        <div class="setrow"><div><div class="st">Export calendar</div><div class="sd">Or download a one-time .ics file to import into Google or Apple Calendar.</div></div>
          <button class="dlbtn" id="icsExport">Download .ics</button></div>
      </div>
      <div class="setgroup">
        <h2>About</h2>
        <div class="setrow"><div><div class="st">Check for updates on launch</div><div class="sd">Notify me when a newer version is released.</div></div>
          <label class="switch"><input type="checkbox" id="autoUpdate"><span class="track"></span></label></div>
        <div class="setrow"><div><div class="st">Semester {VERSION}</div><div class="sd">Updates keep your settings, plan, and notes.
          <a href="https://github.com/LED-esma/semester/releases/tag/v{VERSION}" target="_blank">What's new</a> ·
          <a href="https://github.com/LED-esma/semester" target="_blank">Source code</a></div></div>
          <button class="ghostbtn" id="checkUpdate">Check now</button></div>
        <p class="sd" style="margin:10px 0 0">Not affiliated with Instructure. Canvas is a trademark of Instructure, Inc.</p>
      </div>
    </div>
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
<script type="application/json" id="classesdata">{classes_json}</script>
<script>
{js}
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

    with open(DATA_PATH, "w") as f:
        json.dump({"generated": now.isoformat(), "items": items, "warnings": warnings,
                   "announcements": announcements, "courses": courses_info, "grades": grades}, f, indent=2)

    accent = cfg.get("accent") or "#6366f1"
    html = render_html(items, warnings, courses_info, announcements, colors, now, accent, grades, classes)
    tmp_path = HTML_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(html)
    os.replace(tmp_path, HTML_PATH)  # atomic: the app may be serving the previous file right now

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
