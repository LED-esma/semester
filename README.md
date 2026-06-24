# 🎓 Semester

A friendly desktop dashboard for your Canvas (LMS) coursework — see your whole
semester in one place and *space your work out* instead of cramming.

Pulls your assignments, discussions, announcements, and syllabi straight from
Canvas into a clean, drag-and-drop app. The setup and the dashboard all run
**inside the app window** — no browser, no server to manage, no data leaves your machine.

> Built by a student, for students. Works with any school that uses Canvas.

---

## ✨ Features

- **🗓️ Week board** — a drag-to-schedule kanban. Seven day-columns up top, and a
  tray of everything unscheduled at the bottom. Drag a card onto the day you plan
  to *work on* it; the real due date stays tagged on the card. **Filter by course**
  and **sort** (due date / points / course / name) to focus while you plan. Your
  layout saves automatically in your browser.
- **📋 To-Do** — grouped by urgency (Today / This Week / Later), color-coded per
  course, with **suggested start dates** and **workload warnings** for crunch days.
  Overdue clutter is hidden so you only see what's still actionable.
- **💬 Discussions** — your graded discussions in one place.
- **📣 Announcements** — recent course announcements with previews.
- **📚 Courses** — each class with a collapsible syllabus, **modules, files, and pages** (links into Canvas).
- **📨 Inbox** — read your Canvas conversations, reply, and message instructors without leaving the app.
- **Click any card** for its full description, plus:
  - **✅ Mark done** — syncs to your Canvas planner (and your phone).
  - **🗒️ Submission feedback + rubric** — see your score, rubric, and instructor comments.
  - **📤 Submit** — text-entry or website-URL assignments straight from the popup (zyBooks/quizzes link out).
  - **📝 Notes** — private per-assignment notes that sync to your Canvas planner.
  - **⏱️ Focus timer** — a 25-minute Pomodoro that logs time per assignment.
- **🎯 "What do I need?" grades** — type hypothetical scores or a target and see your projected grade.
- **🎨 Custom accent + light/dark** — a Windows 10 Settings–style interface with a
  left nav rail. Pick any accent color (full color wheel) and flip between light and
  dark from the rail; both save automatically.
- **🍎 Guided setup** — a friendly, Apple-style step-by-step wizard walks you through
  connecting Canvas the first time. No config files to edit.
- **📊 Grades** — current score and letter per course, with a breakdown of graded work.
- **🤖 Auto-schedule** — plan your whole week in one click with four strategies (Balanced load,
  Deadline-first, Front-load, Just-in-time). Never schedules past a due date, respects your
  daily cap and weekends, and keeps cards you placed by hand.
- **🌡️ Day-load meter** — the week board flags days you've over-stacked, so you can spread out.
- **📆 Calendar export** — download an `.ics` of your deadlines and planned work-days
  for Google/Apple Calendar.
- **🔄 Auto-refresh** — re-checks Canvas hourly in the background and offers a one-click reload.
- **🔔 Reminders** — notifications for work due soon and your suggested start days, in-app and
  (on macOS) even when the app is closed. Linux/Windows: run `python3 notify.py` on a schedule.
- **🧩 Settings** — theme (light/dark/system), accent, board view & week start, weekends,
  heavy-day threshold, start-early aggressiveness, default tab, and show/hide any section.

Everything runs locally inside the app. Your Canvas token never leaves your computer.

---

## 🚀 Download & run (no coding needed)

1. Go to the [**Releases**](https://github.com/YOUR_USERNAME/semester/releases) page.
2. Download the file for your computer:
   - **Windows** → `Semester-Windows.exe`
   - **Mac** → `Semester-macOS.zip` (unzip, then open *Semester*)
   - **Linux** → `Semester-Linux`
3. **Open it.** The Semester window opens with a setup screen — enter your school's
   Canvas address, paste an access token (see below), pick an accent color, and you're in. 🎉

Everything runs locally on your computer. Nothing is uploaded anywhere; the only
internet connection is to your own school's Canvas site.

> **First-open security prompt** (because the app isn't code-signed — that costs
> money the project doesn't have):
> - **Mac**: right-click the app → **Open** → **Open**. You only do this once.
> - **Windows**: if you see "Windows protected your PC", click **More info →
>   Run anyway**.

To **refresh** your planner later, just open the app again — it re-fetches from
Canvas and shows the latest.

---

## 🔑 Getting your Canvas access token

The setup screen has an **"Open my Canvas token page"** button that takes you
right here, but the manual steps are:

1. Log into Canvas → **Account → Settings**
2. Scroll to **Approved Integrations → "+ New Access Token"**
3. Purpose: `planner`, leave the expiry blank → **Generate Token**
4. Copy it and paste it into the setup screen

Your **Canvas URL** is just the address bar when you're logged in, e.g.
`https://myschool.instructure.com`.

> 🔒 The token is like a password to your Canvas account. It's stored only in a
> `config.json` file on your computer and is never shared or uploaded. If you
> ever leak it, delete it from Canvas → Settings and generate a new one.

---

## 🧑‍💻 Run from source (developers)

```bash
git clone https://github.com/YOUR_USERNAME/semester.git
cd semester
bash setup.sh          # installs deps, opens the setup screen
```

Or, to build your own double-click app for the OS you're on:

```bash
bash build.sh          # outputs to dist/
```

Pushing a GitHub **Release** auto-builds apps for all three OSes via
[GitHub Actions](.github/workflows/build.yml) and attaches them to the release.

Power users can skip the app entirely:
```bash
python3 canvas_planner.py --open    # terminal mode
python3 canvas_planner.py --demo     # preview with fake data, no token
```

---

## 🎛️ Customizing

- **How early it says to start** — edit `lead_days()` in `canvas_planner.py`
  (default: big projects 5 days out, ≥20 pts 3 days, small stuff 1–2).
- **Theme colors** — tweak the `<style>` block in `render_html()`.
- **Preview without a token** — `python3 canvas_planner.py --demo` builds the
  dashboard with sample data so you can see the layout first.

---

## 📤 The data file

Each run also writes `planner_data.json` — a clean list of your items, due dates,
suggested start dates, and workload warnings. Handy if you want to wire up your
own calendar sync, notifications, or other integrations.

---

## 🤝 Contributing

Issues and pull requests welcome! This is a small, friendly project. Ideas:
calendar (`.ics`) export, a "mark done" toggle on cards, grade tracking,
light theme, mobile layout.

## 📄 License

MIT — see [LICENSE](LICENSE). Do whatever you like; no warranty.
