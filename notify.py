#!/usr/bin/env python3
"""
Semester background reminders.

Posts native OS notifications for work that's due soon or whose suggested start
day is today — even when the app window is closed. An OS scheduler (launchd on
macOS) runs this every hour; see install()/uninstall().

Usage:
  python3 notify.py            # check once and notify (what the scheduler runs)
  python3 notify.py --test     # post one sample notification (verify it works)
  python3 notify.py --install  # schedule hourly background checks (macOS)
  python3 notify.py --uninstall
"""

import json
import os
import platform
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import canvas_planner as cp

STATE = os.path.join(cp.DATA_DIR, "notified.json")
LABEL = "com.semester.notify"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _notify(title, body):
    """Best-effort native notification, per OS."""
    system = platform.system()
    q = lambda s: json.dumps(s, ensure_ascii=False)  # literal Unicode; AppleScript chokes on \uXXXX
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 f"display notification {q(body)} with title {q(title)}"],
                check=False)
        elif system == "Linux":
            subprocess.run(["notify-send", title, body], check=False)
        elif system == "Windows":
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"New-BurntToastNotification -Text {json.dumps(title)},{json.dumps(body)}"],
                           check=False)
    except Exception:
        pass


def _config():
    try:
        cfg = json.load(open(cp.CONFIG_PATH))
        if cfg.get("base_url") and cfg.get("token"):
            cfg["base_url"] = cfg["base_url"].rstrip("/")
            return cfg
    except Exception:
        pass
    return None


def _load_state():
    """What's been sent and what's been seen. (Older versions saved just a list of sent keys.)"""
    try:
        s = json.load(open(STATE))
    except Exception:
        return {"sent": [], "seen": None}
    return {"sent": s, "seen": None} if isinstance(s, list) else s


def _save_state(s):
    try:
        os.makedirs(cp.DATA_DIR, exist_ok=True)
        s["sent"] = sorted(s.get("sent", []))[-3000:]
        json.dump(s, open(STATE, "w"))
    except Exception:
        pass


WHEN_CHOICES = ("day_before", "morning", "hours3")
MORNING_HOUR = 8


def _valid_when(w):
    return w in WHEN_CHOICES or (isinstance(w, str) and w.startswith("minutes:") and w[8:].isdigit())


def prefs(cfg):
    """Notification settings, all set in Settings > Notifications. Defaults: reminders the day before and
    the morning of, every kind of new thing, start days, no quiet hours, nothing muted."""
    when = cfg.get("notify_due_when")
    both = cfg.get("notify_new", True)  # older versions had one switch for all three
    return {"due": cfg.get("notify_due", True),
            "when": [w for w in (when if isinstance(when, list) else ["day_before", "morning"]) if _valid_when(w)],
            "morning_hour": int(cfg.get("notify_morning_hour", MORNING_HOUR)),
            "new_assignments": cfg.get("notify_new_assignments", both),
            "new_announcements": cfg.get("notify_new_announcements", both),
            "new_grades": cfg.get("notify_new_grades", both),
            "start": cfg.get("notify_start", True),
            "quiet": cfg.get("notify_quiet", False),
            "quiet_from": int(cfg.get("notify_quiet_from", 22)),
            "quiet_to": int(cfg.get("notify_quiet_to", 8)),
            "mute": [str(c) for c in (cfg.get("notify_mute_courses") or [])]}


def _in_quiet(p, local):
    if not p["quiet"] or p["quiet_from"] == p["quiet_to"]:
        return False
    a, b, h = p["quiet_from"] % 24, p["quiet_to"] % 24, local.hour
    return (h >= a or h < b) if a > b else (a <= h < b)


def _due_phrase(due, now):
    d = due.astimezone()
    days, t = (d.date() - now.astimezone().date()).days, d.strftime("%-I:%M %p")
    return f"today at {t}" if days == 0 else f"tomorrow at {t}" if days == 1 else d.strftime("%a at ") + t


def check(cfg=None, now=None, send=None):
    """Send whatever's due, from the data Semester already downloaded (no network). The app calls this
    after every refresh and every few minutes; the background job calls it too. Each notification goes
    out once: the state file remembers what was sent."""
    cfg = cfg or _config()
    if not cfg:
        return []
    try:
        with open(cp.DATA_PATH) as f:
            data = json.load(f)
    except Exception:
        return []
    now, send, p, st = now or datetime.now(timezone.utc), send or _notify, prefs(cfg), _load_state()
    local = now.astimezone()
    if _in_quiet(p, local):
        return []  # held, not lost: nothing is marked seen or sent, so it all goes out when quiet hours end
    sent, out = set(st.get("sent", [])), []
    queued = {"due": [], "start": [], "new": []}

    def fire(key, title, body, kind):
        if key not in sent:
            sent.add(key)
            queued[kind].append((title, body))

    all_items = [i for i in data.get("items", []) if i.get("uid")]
    all_anns = data.get("announcements", [])
    ann_key = lambda a: a.get("url") or (a.get("course", "") + "|" + a.get("title", ""))
    # Remember everything, including muted classes, so unmuting later doesn't dump their whole backlog.
    now_seen = {"items": {i["uid"] for i in all_items}, "ann": {ann_key(a) for a in all_anns},
                "graded": {i["uid"] for i in all_items if i.get("score") is not None}}
    muted = lambda course: cp.clean_course_name(course or "") in p["mute"]
    items = [i for i in all_items if not muted(i.get("course"))]
    anns = [a for a in all_anns if not muted(a.get("course"))]
    seen = st.get("seen")
    # New things. The first check only takes stock, so setting Semester up doesn't set off a flood.
    if seen is not None:
        old = {k: set(v) for k, v in seen.items()}
        if p["new_assignments"]:
            for i in items:
                if i["uid"] not in old.get("items", set()) and not i.get("submitted") and i.get("bucket") != "overdue":
                    when = (", due " + _due_phrase(cp.parse_dt(i["due"]), now)) if i.get("due") else ""
                    fire("new:" + i["uid"], "New in " + cp.clean_course_name(i["course"]), i["title"] + when, "new")
        if p["new_announcements"]:
            for a in anns:
                if ann_key(a) not in old.get("ann", set()):
                    fire("ann:" + ann_key(a), cp.clean_course_name(a.get("course", "")), a.get("title") or "New announcement", "new")
        if p["new_grades"]:
            for i in items:
                if i["uid"] in now_seen["graded"] and i["uid"] not in old.get("graded", set()):
                    pts = f" / {i['points']:g}" if i.get("points") else ""
                    fire("graded:" + i["uid"], "Graded: " + i["title"], f"{i['score']:g}{pts}", "new")
    st["seen"] = {k: sorted(v | set((seen or {}).get(k, []))) for k, v in now_seen.items()}

    # Due reminders at the chosen times. If Semester wasn't open at one of them, only the latest
    # still-useful one goes out, so reminders never stack up.
    if p["due"]:
        for i in items:
            if i.get("submitted") or not i.get("due"):
                continue
            due = cp.parse_dt(i["due"])
            if now >= due:
                continue
            morning = due.astimezone().replace(hour=p["morning_hour"], minute=0, second=0, microsecond=0)
            times = {"day_before": due - timedelta(days=1), "hours3": due - timedelta(hours=3), "morning": morning}
            for w in p["when"]:
                if w.startswith("minutes:"):
                    times[w] = due - timedelta(minutes=int(w[8:]))
            ready = sorted((times[w], w) for w in p["when"] if times[w] <= now and not (w == "morning" and morning >= due))
            if ready:
                for _, w in ready[:-1]:
                    sent.add(f"due:{w}:{i['uid']}")  # superseded by the later reminder; never send it late
                fire(f"due:{ready[-1][1]}:{i['uid']}", "Due " + _due_phrase(due, now) + ": " + i["title"],
                     cp.clean_course_name(i["course"]), "due")
    if p["start"]:
        for i in items:
            if (i.get("start") and not i.get("submitted") and local.hour >= p["morning_hour"]
                    and cp.parse_dt(i["start"]).astimezone().date() == local.date()):
                fire("start:" + i["uid"] + ":" + str(local.date()), "Time to start: " + i["title"],
                     cp.clean_course_name(i["course"]) + ", suggested start day", "start")
    # One or two of a kind arrive separately; three or more become a single summary, never a flood.
    summary = {"due": "{} things due soon", "start": "Time to start {} things", "new": "{} new things in Canvas"}
    for kind, msgs in queued.items():
        if len(msgs) <= 2:
            for title, body in msgs:
                out.append((title, body))
                send(title, body)
        elif msgs:
            names = [(t.split(": ", 1)[1] if ": " in t else b.split(", due ")[0]) for t, b in msgs]
            names = [n if len(n) <= 40 else n[:39].rstrip() + "…" for n in names]
            body = ", ".join(names[:3]) + (f", and {len(msgs) - 3} more" if len(msgs) > 3 else "")
            out.append((summary[kind].format(len(msgs)), body))
            send(summary[kind].format(len(msgs)), body)
    st["sent"] = list(sent)
    _save_state(st)
    return out


def _app_running():
    """The open app sends its own notifications, so the background job steps aside while it runs."""
    for port in (8765, 8766, 8780, 8800):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as r:
                if "status" in json.load(r):
                    return True
        except Exception:
            continue
    return False


def _signature(cfg):
    """Three tiny requests that change whenever something new lands in Canvas (same check the app uses)."""
    c = cp.Canvas(cfg["base_url"], cfg["token"])
    return json.dumps([c.get(p) for p in ("/users/self/todo_item_count", "/users/self/activity_stream/summary",
                                          "/conversations/unread_count")], sort_keys=True)


def run_once():
    """What the background job runs (about hourly, while Semester is closed): refresh only if Canvas
    changed or the saved data is over three hours old, then send whatever is due."""
    cfg = _config()
    if not cfg or _app_running():
        return
    try:
        sig = _signature(cfg)
        try:
            with open(cp.DATA_PATH) as f:
                age = datetime.now(timezone.utc) - cp.parse_dt(json.load(f)["generated"])
        except Exception:
            age = timedelta(days=1)
        if sig != _load_state().get("sig") or age > timedelta(hours=3):
            cp.build_dashboard(cfg)
            st = _load_state()
            st["sig"] = sig
            _save_state(st)
    except Exception:
        pass  # offline or signed out: reminders still go out from the last data
    check(cfg)


def _launch_cmd():
    """How the scheduler should invoke a one-shot check (works frozen or source)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--notify"]
    here = os.path.dirname(os.path.abspath(__file__))
    return [sys.executable, os.path.join(here, "app.py"), "--notify"]


def install(interval_min=60):
    if platform.system() != "Darwin":
        return False, "Auto-scheduling is macOS-only for now; see README for Linux/Windows."
    args = "".join(f"<string>{c}</string>" for c in _launch_cmd())
    plist = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
             '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
             '<plist version="1.0"><dict>'
             f'<key>Label</key><string>{LABEL}</string>'
             f'<key>ProgramArguments</key><array>{args}</array>'
             f'<key>StartInterval</key><integer>{max(15, interval_min) * 60}</integer>'
             '<key>RunAtLoad</key><true/></dict></plist>')
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "w") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", PLIST], check=False)
    subprocess.run(["launchctl", "load", PLIST], check=False)
    return True, "Background reminders enabled."


def uninstall():
    if os.path.exists(PLIST):
        subprocess.run(["launchctl", "unload", PLIST], check=False)
        try:
            os.remove(PLIST)
        except OSError:
            pass
    return True, "Background reminders disabled."


if __name__ == "__main__":
    if "--install" in sys.argv:
        print(install()[1])
    elif "--uninstall" in sys.argv:
        print(uninstall()[1])
    elif "--test" in sys.argv:
        _notify("Semester", "Background reminders are working.")
    else:
        run_once()
