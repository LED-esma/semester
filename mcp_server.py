#!/usr/bin/env python3
"""
Semester MCP server — lets Claude query your Canvas coursework directly.

Tools: refresh data, list assignments (by urgency/course), what's due soon,
grades, announcements, full assignment details, and mark-done (syncs to Canvas).

Reads the same planner_data.json the Semester app builds; call refresh_canvas
to pull fresh data from Canvas first when freshness matters.

Register (user scope, works in every session):
  claude mcp add --scope user semester -- python3 /path/to/mcp_server.py
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import canvas_planner as cp

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("semester")


def _data():
    try:
        with open(cp.DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"items": [], "grades": [], "announcements": [], "generated": None}


def _cfg():
    with open(cp.CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _pending(d):
    return [i for i in d["items"] if not i["submitted"]]


def _fmt(i):
    due = i["due"][:16].replace("T", " ") if i["due"] else "no due date"
    pts = f'{i["points"]:g} pts' if i.get("points") else "ungraded"
    flag = " (due date parsed from title — verify)" if i.get("due_approx") else ""
    return {"title": i["title"], "course": cp.clean_course_name(i["course"]),
            "type": i["type"], "due": due + flag, "points": pts,
            "status": i["bucket"], "url": i.get("url")}


@mcp.tool()
def refresh_canvas() -> str:
    """Fetch the latest assignments, grades, and announcements from Canvas.
    Takes ~15-30s. Call this first if the user wants current data."""
    try:
        return json.dumps(cp.build_dashboard(_cfg()))
    except cp.TokenExpired:
        return json.dumps({"error": "canvas_token_expired",
                           "message": "Your Canvas access token expired or was revoked. "
                                      "Open the Semester app — it will prompt for a new token — "
                                      "then try again. Cached data still works for read-only questions."})


@mcp.tool()
def data_age() -> str:
    """When the local Canvas data was last refreshed (so you know if it's stale)."""
    g = _data().get("generated")
    if not g:
        return "No data yet — call refresh_canvas."
    age = datetime.now(timezone.utc) - datetime.fromisoformat(g)
    return f"Data is {int(age.total_seconds() // 60)} minutes old (fetched {g[:16]})."


@mcp.tool()
def list_assignments(status: str = "pending", course: str = "") -> str:
    """List coursework. status: 'pending' (default, everything unfinished incl.
    overdue), 'overdue', 'upcoming', 'week' (due within 7 days), or 'all'.
    course: optional substring filter, e.g. 'python' or 'critical'."""
    d = _data()
    items = d["items"] if status == "all" else _pending(d)
    if status == "overdue":
        items = [i for i in items if i["bucket"] == "overdue"]
    elif status == "week":
        items = [i for i in items if i["bucket"] in ("today", "week")]
    elif status == "upcoming":
        items = [i for i in items if i["bucket"] in ("today", "week", "later")]
    if course:
        items = [i for i in items if course.lower() in i["course"].lower()]
    items.sort(key=lambda x: (x["due"] is None, x["due"] or "~"))
    return json.dumps([_fmt(i) for i in items], ensure_ascii=False)


@mcp.tool()
def whats_due(days: int = 7) -> str:
    """Everything unfinished due within the next N days (default 7), soonest first."""
    d = _data()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    out = []
    for i in _pending(d):
        if not i["due"]:
            continue
        due = datetime.fromisoformat(i["due"])
        if now <= due <= horizon:
            out.append(i)
    out.sort(key=lambda x: x["due"])
    return json.dumps([_fmt(i) for i in out], ensure_ascii=False)


@mcp.tool()
def get_assignment(title_search: str) -> str:
    """Full detail for one assignment matched by title substring: description,
    due date, points, submission types, feedback/rubric if graded."""
    d = _data()
    matches = [i for i in d["items"] if title_search.lower() in i["title"].lower()]
    if not matches:
        return json.dumps({"error": f"No assignment matching '{title_search}'."})
    out = []
    for i in matches[:3]:
        e = _fmt(i)
        e["submitted"] = i["submitted"]
        e["description_text"] = cp.html_to_text(i.get("description"), limit=4000)
        e["submission_types"] = i.get("submission_types", [])
        if i.get("score") is not None:
            e["score"] = i["score"]
        if i.get("comments"):
            e["instructor_comments"] = i["comments"]
        if i.get("rubric"):
            e["rubric"] = i["rubric"]
        out.append(e)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def get_grades() -> str:
    """Current grade per course, plus each course's graded items with scores."""
    d = _data()
    out = []
    for g in d.get("grades", []):
        out.append({"course": cp.clean_course_name(g["name"]),
                    "score_percent": g.get("score"), "letter": g.get("grade"),
                    "graded_items": [{"title": i["title"], "score": i["score"],
                                      "points": i["points"]}
                                     for i in g.get("items", []) if i.get("graded")]})
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def get_announcements() -> str:
    """Recent course announcements (last 30 days), newest first."""
    d = _data()
    return json.dumps([{"course": cp.clean_course_name(a["course"]), "title": a["title"],
                        "posted": (a.get("posted") or "")[:16], "preview": a.get("preview")}
                       for a in d.get("announcements", [])], ensure_ascii=False)


def _twin_submitted(d, disc):
    """A graded discussion appears twice (assignment + discussion). The assignment
    twin carries the real submission state — use it when available."""
    for i in d["items"]:
        if i["type"] == "assignment" and i["title"] == disc["title"] and i["course"] == disc["course"]:
            return i["submitted"]
    return disc["submitted"]


@mcp.tool()
def list_discussions(only_needing_reply: bool = False, course: str = "") -> str:
    """List discussions with whether you've replied. only_needing_reply=True shows
    just the ones you still need to post to. course: optional substring filter."""
    d = _data()
    out = []
    for i in d["items"]:
        if i["type"] != "discussion":
            continue
        if course and course.lower() not in i["course"].lower():
            continue
        replied = _twin_submitted(d, i)
        if only_needing_reply and replied:
            continue
        e = _fmt(i)
        e["replied"] = replied
        e["needs_reply"] = not replied
        out.append(e)
    out.sort(key=lambda x: (x["replied"], x["due"]))
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def get_discussion(title_search: str) -> str:
    """Live-fetch one discussion from Canvas: the full prompt, whether YOU have
    posted (checked against your real entries), your posts, and recent classmate
    posts (useful for 'reply to a classmate' requirements). ~5-10s."""
    import re as _re
    d = _data()
    matches = [i for i in d["items"] if i["type"] == "discussion"
               and title_search.lower() in i["title"].lower()]
    if not matches:
        return json.dumps({"error": f"No discussion matching '{title_search}'."})
    disc = matches[0]
    m = _re.search(r"/courses/(\d+)/discussion_topics/(\d+)", disc.get("url") or "")
    if not m:
        return json.dumps({"error": "Could not resolve the discussion's Canvas ids."})
    cid, tid = m.group(1), m.group(2)
    cfg = _cfg()
    canvas = cp.Canvas(cfg["base_url"], cfg["token"])
    me = canvas.get("/users/self")[0]["id"]
    topic = canvas.get(f"/courses/{cid}/discussion_topics/{tid}")[0]
    try:
        entries = canvas.get(f"/courses/{cid}/discussion_topics/{tid}/entries")
    except Exception:
        entries = []  # require_initial_post topics hide entries until you post
    mine, classmates = [], []
    for e in entries:
        row = {"author": e.get("user_name"), "posted": (e.get("created_at") or "")[:16],
               "text": cp.html_to_text(e.get("message"), limit=900)}
        (mine if e.get("user_id") == me else classmates).append(row)
        for r in (e.get("recent_replies") or []):
            rr = {"author": r.get("user_name"), "posted": (r.get("created_at") or "")[:16],
                  "text": cp.html_to_text(r.get("message"), limit=900)}
            (mine if r.get("user_id") == me else classmates).append(rr)
    return json.dumps({
        "title": disc["title"], "course": cp.clean_course_name(disc["course"]),
        "due": disc["due"], "points": disc.get("points"),
        "prompt": cp.html_to_text(topic.get("message"), limit=5000),
        "requires_initial_post_before_seeing_others": bool(topic.get("require_initial_post")),
        "you_have_posted": bool(mine),
        "your_posts": mine,
        "classmate_posts_recent": classmates[-8:],
        "url": disc.get("url"),
    }, ensure_ascii=False)


@mcp.tool()
def mark_done(title_search: str, done: bool = True) -> str:
    """Mark an assignment done (or not done) — syncs to the user's Canvas planner.
    Matches by title substring; fails if it matches more than one assignment."""
    d = _data()
    matches = [i for i in d["items"] if title_search.lower() in i["title"].lower()
               and i.get("assign_id")]
    if not matches:
        return json.dumps({"ok": False, "error": "No matching assignment."})
    if len(matches) > 1:
        return json.dumps({"ok": False, "error": "Ambiguous — matches: "
                           + "; ".join(m["title"] for m in matches[:5])})
    canvas = cp.Canvas(_cfg()["base_url"], _cfg()["token"])
    ok = canvas.mark_done(matches[0]["assign_id"], done)
    return json.dumps({"ok": ok, "title": matches[0]["title"], "done": done})


if __name__ == "__main__":
    mcp.run()
