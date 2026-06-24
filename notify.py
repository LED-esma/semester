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
from datetime import datetime, timezone

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
    try:
        return set(json.load(open(STATE)))
    except Exception:
        return set()


def _save_state(s):
    try:
        os.makedirs(cp.DATA_DIR, exist_ok=True)
        json.dump(sorted(s), open(STATE, "w"))
    except Exception:
        pass


def run_once(window_hours=16):
    """Notify about not-yet-submitted work due within window_hours, or whose
    suggested start day is today. De-duplicates via a small state file."""
    cfg = _config()
    if not cfg:
        return
    now = datetime.now(timezone.utc)
    canvas = cp.Canvas(cfg["base_url"], cfg["token"])
    items = cp.build_items(canvas, canvas.active_courses(), now,
                           cfg.get("aggressiveness", "balanced"))
    done = _load_state()
    today = now.astimezone().date()
    for it in items:
        if it["submitted"]:
            continue
        if it["due"]:
            due = cp.parse_dt(it["due"])
            hrs = (due - now).total_seconds() / 3600
            key = "due:" + it["uid"]
            if 0 < hrs <= window_hours and key not in done:
                _notify("Due soon: " + it["title"],
                        cp.clean_course_name(it["course"]) + " — due "
                        + due.astimezone().strftime("%a %-I:%M %p"))
                done.add(key)
        if it["start"]:
            sd = cp.parse_dt(it["start"]).astimezone().date()
            key = "start:" + it["uid"] + ":" + str(today)
            if sd == today and key not in done:
                _notify("Time to start: " + it["title"],
                        cp.clean_course_name(it["course"]) + " — suggested start day")
                done.add(key)
    _save_state(done)


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
        _notify("Semester", "Background reminders are working. 🎉")
    else:
        run_once()
