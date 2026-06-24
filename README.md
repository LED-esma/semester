# 🎓 Semester

Your whole semester in one place — a local desktop dashboard for Canvas. Everything runs on your computer; your token never leaves it.

## Features

- 🗓️ **Week board** — drag assignments onto the day you'll work on them
- ⚡ **Instant launch** — opens on your last dashboard and syncs with Canvas in a few seconds
- ⏭️ **Up next** — the board opens with your next deadline and how much lands on each day
- ✔️ **Quick done** — hover any card and check it off in Canvas without opening it
- 🤖 **Auto-schedule** — plan the week in one click (balanced, deadline-first, front-load, or just-in-time)
- 🌡️ **Day-load meter** — flags days you've over-stacked
- 📋 **To-Do** — grouped by urgency, with suggested start dates and crunch-day warnings
- 🏫 **Classes** — Google Classroom–style: class cards → Stream, Classwork, Grades, Materials
- 📊 **Grades** — current score per course
- 🎯 **"What do I need?"** — type a target and see the score you need
- 💬 **Discussions** — graded discussions in one place
- 📣 **Announcements** — recent posts with previews
- 📨 **Inbox** — read, reply, and message instructors
- ✅ **Mark done** — syncs to your Canvas planner
- 📤 **Submit** — text or URL assignments from the app
- 🗒️ **Feedback + rubric** — your score, rubric, and instructor comments
- 📝 **Notes** — per-assignment notes that sync to Canvas
- ⏱️ **Focus timer** — 25-minute Pomodoro that logs time
- 📆 **Live calendar feed** — subscribe your calendar app to `/calendar.ics` once and deadlines stay in sync (one-time `.ics` download still available)
- 🧠 **AI export** — download your coursework as clean JSON (pick sections) to hand to any AI
- 🤝 **Claude connector** — an MCP server so Claude can query your assignments directly
- 🔔 **Reminders** — notifications for work due soon
- 🔄 **Auto-update** — checks GitHub and installs new versions in one click
- 🎨 **Themes** — light/dark and any accent color

## Get started

1. Download your platform from [**Releases**](https://github.com/LED-esma/semester/releases):
   - **Mac** — `Semester-mac.zip` (signed & notarized — just open it)
   - **Windows** — `Semester-Windows.exe` (SmartScreen: More info → Run anyway, once)
   - **Linux** — `Semester-Linux` (`chmod +x Semester-Linux && ./Semester-Linux`)
2. Open it. The setup wizard asks for:
   - your school's Canvas web address (the URL when you're logged in, e.g. `https://myschool.instructure.com`)
   - an access token — the wizard has a button that opens your Canvas token page
     (Canvas → Account → Settings → **+ New Access Token** → Generate → copy)
   - an accent color
3. That's it. Your dashboard builds and opens in the app window.

## Using it day to day

- Open the app — it fetches the latest from Canvas and lands on the **Week board**.
- Drag cards from the tray onto days, or hit **Auto-schedule → Plan my week**.
- Click any card for its description, feedback, notes, a focus timer, and **Mark done** / **Submit**.
- **Classes** shows each course like Google Classroom; **Settings** has themes, sections,
  calendar/AI export, reminders, and updates.
- When a new version is released, a banner appears — one click updates in place.
  Your settings, plan, and notes survive every update.
- Canvas tokens expire. When yours does, Semester keeps showing your last data with a **Reconnect**
  bar — paste a new token and it picks right back up, settings intact.
- On a Mac, if a one-click update ever left Semester unable to open, download it again from
  Releases. Updates from version 1.5 and earlier could unpack it incorrectly; 1.6 fixes this.

## Use with Claude (MCP connector)

Let Claude answer "what's due this week?", check what discussions you still need to reply to,
pull an assignment's full description, or mark things done — straight from your Canvas data.

```bash
pip install mcp
claude mcp add --scope user semester -- python3 /path/to/semester/mcp_server.py
```

Then in any Claude Code session, ask naturally:

| You say | Tool used |
|---|---|
| "what's due this week?" | `whats_due` |
| "list my overdue work" | `list_assignments` |
| "which discussions do I still need to reply to?" | `list_discussions` |
| "open the Feed Essay discussion — what did classmates say?" | `get_discussion` |
| "what are my grades?" | `get_grades` |
| "mark Lab 4 done" | `mark_done` |
| "refresh from Canvas first" | `refresh_canvas` |

The connector reads the same local data as the app (instant) and can re-fetch live when asked.
Requires the app (or `canvas_planner.py`) to be set up first, since it uses the same config.

## Run from source

```bash
git clone https://github.com/LED-esma/semester.git
cd semester
bash setup.sh        # installs deps + launches the app
```

- `python3 canvas_planner.py --open` — build the dashboard and open it in a browser (no app window)
- `python3 canvas_planner.py --demo` — preview with sample data, no token needed
- `bash build.sh` — build a double-click app for your OS (outputs to `dist/`)
- `bash sign_mac.sh` — macOS build, signed + notarized (needs an Apple Developer ID)

Publishing a GitHub Release builds and attaches apps for all three OSes automatically via CI.

## License

MIT — see [LICENSE](LICENSE).
