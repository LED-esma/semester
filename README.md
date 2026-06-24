# Semester

A desktop dashboard for Canvas. It shows what's due, when to start, and where your grades stand.
It runs on your computer, and your Canvas token never leaves it.

## What it does

- **Week board.** Everything due this week, by day. Drag work onto the day you'll do it, or let it plan the week.
- **To-Do.** Grouped by urgency, with suggested start dates and a warning when too much lands on one day.
- **Classes.** Each course's announcements, modules, grades, and files.
- **Grades.** Your current score in each class, what-if scores, and the average you need to hit a target.
- **Discussions, announcements, and inbox** in one place.
- **Mark done, notes, and submit.** Check work off, keep notes, and turn in text or link assignments without opening Canvas.
- **Calendar feed** for Apple or Google Calendar.
- **Reminders** before things are due.
- **Stays current.** Checks Canvas for changes every few minutes.
- **Updates itself** in one click.

## Install

Download from [Releases](https://github.com/LED-esma/semester/releases/latest):

- **Mac:** `Semester-mac.dmg`. Open it and drag Semester into Applications.
- **Windows:** `Semester-Windows.exe`. If Windows says it doesn't recognize the app, click More info, then Run anyway.
- **Linux:** `Semester-Linux`. Run `chmod +x Semester-Linux && ./Semester-Linux`.

## Setup

Open Semester, search for your school, and sign in with your school account. Semester sets up access for you.
If your school doesn't allow that, paste a Canvas access token instead; Semester opens the page where you make one.
It takes about a minute.

## Good to know

- Canvas tokens can expire. When yours does, Semester keeps your last data on screen and asks for a new one.
- Log out from Settings > Account. It removes your token and course data from this computer.
- Updates keep your settings, plan, and notes.
- On a Mac running 1.7 or earlier, download the current version once. Those builds can't update themselves.

## Claude connector (MCP)

Ask Claude about your coursework: "what's due this week?", "what are my grades?", "mark Lab 4 done".

```bash
pip install mcp
claude mcp add --scope user semester -- python3 /path/to/semester/mcp_server.py
```

Tools: `whats_due`, `list_assignments`, `list_discussions`, `get_discussion`, `get_grades`, `mark_done`, `refresh_canvas`.
The connector uses the config from a source setup (below), so set that up first.

## Run from source

```bash
git clone https://github.com/LED-esma/semester.git
cd semester
bash setup.sh
```

- `python3 canvas_planner.py --demo`: preview with sample data, no token needed.
- `python3 canvas_planner.py --open`: build the dashboard and open it in a browser.
- `bash build.sh`: build the app for your OS into `dist/`.
- `bash sign_mac.sh`: a signed, notarized Mac build (needs an Apple Developer ID).

Publishing a GitHub release builds all three apps in CI.

## License

MIT. See [LICENSE](LICENSE). Not affiliated with Instructure; Canvas is a trademark of Instructure, Inc.
