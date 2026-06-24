  // Launch sync runs first and stands alone, so an error anywhere below can't block fresh data.
  (function syncOnLaunch() {
    const BUILT = __PAGE_BUILT__;
    let touched = false;
    ['pointerdown', 'keydown', 'wheel'].forEach(ev => addEventListener(ev, () => { touched = true; }, { once: true, passive: true }));
    const status = async () => (await fetch('/api/status', { cache: 'no-store' })).json();
    status().then(s => {
      if (s.status !== 'building') return;
      const pill = document.createElement('div'); pill.className = 'syncpill'; pill.textContent = 'Syncing with Canvas…';
      document.body.appendChild(pill);
      const poll = setInterval(async () => {
        try { s = await status(); } catch (e) { return; }
        if (s.status === 'building') return;
        clearInterval(poll);
        if (s.status === 'done' && s.built <= BUILT) { pill.remove(); return; }
        if (s.status === 'done' && touched) {
          pill.textContent = 'Updated from Canvas — click to refresh'; pill.classList.add('ready');
          pill.onclick = () => location.reload(); return;
        }
        location.reload();
      }, 1500);
    }).catch(() => {});
  })();

  const TITLES = {week:'Week board', classes:'Classes', todo:'To-Do', disc:'Discussions', ann:'Announcements', crs:'Courses', grades:'Grades', inbox:'Inbox', settings:'Settings'};
  const COURSES = __COURSES__;
  document.querySelectorAll('.nav').forEach(t => t.addEventListener('click', () => {
    document.querySelectorAll('.nav').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.getElementById(t.dataset.p).classList.add('active');
    document.getElementById('pageTitle').textContent = TITLES[t.dataset.p] || '';
  }));

  // ---- Settings (saved per browser, except a few app-side prefs noted below) ----
  const LS = localStorage;
  const $id = id => document.getElementById(id);

  // Toast helper (used by refresh nudge + saved-pref hints)
  const toast = $id('toast');
  let toastAction = null;
  function showToast(msg, action) { toast.textContent = msg; toastAction = action || null; toast.classList.add('show');
    clearTimeout(toast._t); if (!action) toast._t = setTimeout(() => toast.classList.remove('show'), 4000); }
  toast.addEventListener('click', () => { if (toastAction) toastAction(); toast.classList.remove('show'); });

  // Theme (light / dark / match system)
  const themeSel = $id('themeSel');
  const resolveDark = v => v === 'dark' || (v === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  themeSel.value = LS.getItem('semester.theme') || 'system';
  themeSel.addEventListener('change', () => {
    LS.setItem('semester.theme', themeSel.value);
    document.documentElement.classList.toggle('dark', resolveDark(themeSel.value));
  });

  // Accent
  const accentInput = $id('accentInput');
  const savedAccent = LS.getItem('semester.accent'); if (savedAccent) accentInput.value = savedAccent;
  accentInput.addEventListener('input', () => {
    document.documentElement.style.setProperty('--accent', accentInput.value);
    LS.setItem('semester.accent', accentInput.value);
  });

  // Generic binders for client-side board settings (re-render on change)
  const bindSel = (id, key, def, after) => { const el = $id(id); el.value = LS.getItem(key) || def;
    el.addEventListener('change', () => { LS.setItem(key, el.value); if (after) after(); }); };
  const bindNum = (id, key, def, after) => { const el = $id(id); el.value = LS.getItem(key) || def;
    el.addEventListener('change', () => { LS.setItem(key, el.value); if (after) after(); }); };
  const bindTog = (id, key, defOn, after) => { const el = $id(id); el.checked = (LS.getItem(key) || (defOn ? 'on' : 'off')) === 'on';
    el.addEventListener('change', () => { LS.setItem(key, el.checked ? 'on' : 'off'); if (after) after(); }); };

  bindSel('boardView', 'semester.boardView', 'rolling', () => renderBoard());
  bindSel('weekStart', 'semester.weekStart', '0', () => renderBoard());
  bindTog('weekends', 'semester.weekends', true, () => renderBoard());
  bindNum('dayThreshold', 'semester.dayThreshold', '4', () => renderBoard());
  bindSel('defaultTab', 'semester.defaultTab', 'week', null);

  // App-side prefs: persist to config.json via the server, then offer a reload.
  async function savePref(obj) { try { await fetch('/api/prefs', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(obj) }); } catch (e) {} }
  const aggr = $id('aggr'); aggr.value = LS.getItem('semester.aggr') || 'balanced';
  aggr.addEventListener('change', () => { LS.setItem('semester.aggr', aggr.value);
    savePref({ aggressiveness: aggr.value }); showToast('Saved — reload to recalculate start dates.', () => location.reload()); });
  const showAll = $id('showAllCourses');
  if (showAll) {
    showAll.checked = LS.getItem('semester.showAll') === 'on';
    showAll.addEventListener('change', () => { LS.setItem('semester.showAll', showAll.checked ? 'on' : 'off');
      savePref({ show_all_courses: showAll.checked }); showToast('Saved — click to refresh your courses.', () => location.reload()); });
  }
  const autoOn = $id('autoOn'), autoMin = $id('autoMin');
  autoOn.checked = (LS.getItem('semester.autoOn') || 'on') === 'on';
  autoMin.value = LS.getItem('semester.autoMin') || '60';
  const saveAuto = () => { LS.setItem('semester.autoOn', autoOn.checked ? 'on' : 'off'); LS.setItem('semester.autoMin', autoMin.value);
    savePref({ autorefresh: autoOn.checked, autorefresh_min: parseInt(autoMin.value, 10) || 60 }); };
  autoOn.addEventListener('change', saveAuto); autoMin.addEventListener('change', saveAuto);
  const bgNotify = $id('bgNotify');
  bgNotify.checked = (LS.getItem('semester.bgNotify') || 'off') === 'on';
  bgNotify.addEventListener('change', async () => {
    LS.setItem('semester.bgNotify', bgNotify.checked ? 'on' : 'off');
    try {
      const r = await fetch('/api/notify', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: bgNotify.checked, interval: parseInt(autoMin.value, 10) || 60 }) });
      const d = await r.json(); showToast(d.msg || 'Updated.');
    } catch (e) { showToast('Background reminders need the installed app.'); }
  });

  // Section show/hide
  const loadTabs = () => { try { return JSON.parse(LS.getItem('semester.tabs')) || {}; } catch (e) { return {}; } };
  function applyTabs() {
    const prefs = loadTabs();
    document.querySelectorAll('.nav[data-p]').forEach(n => {
      const p = n.dataset.p;
      if (p === 'week' || p === 'settings') return;
      const on = prefs[p] !== false;
      n.style.display = on ? '' : 'none';
      if (!on && n.classList.contains('active')) document.querySelector('.nav[data-p="week"]').click();
    });
  }
  document.querySelectorAll('input[data-tab]').forEach(cb => {
    cb.checked = loadTabs()[cb.dataset.tab] !== false;
    cb.addEventListener('change', () => {
      const p = loadTabs(); p[cb.dataset.tab] = cb.checked;
      LS.setItem('semester.tabs', JSON.stringify(p)); applyTabs();
    });
  });
  applyTabs();

  // Open the user's default tab on launch
  const defTab = LS.getItem('semester.defaultTab');
  if (defTab && defTab !== 'week') { const b = document.querySelector('.nav[data-p="' + defTab + '"]');
    if (b && b.style.display !== 'none') b.click(); }

  const DATA = JSON.parse(document.getElementById('itemdata').textContent);
  const modal = document.getElementById('modal');
  function openModal(id) {
    const d = DATA[id]; if (!d) return;
    document.getElementById('mCourse').textContent = d.course;
    document.getElementById('mTitle').textContent = d.title;
    const meta = [];
    if (d.due) meta.push('Due ' + d.due);
    if (d.start) meta.push('Start ' + d.start);
    if (d.points) meta.push(d.points + ' pts');
    document.getElementById('mMeta').innerHTML = meta.map(m => '<span>' + m + '</span>').join('');
    document.getElementById('mDesc').innerHTML = d.desc || '<i>No description provided in Canvas.</i>';
    document.getElementById('mLink').href = d.url;
    // Feedback (score, rubric, comments)
    let fb = '';
    if (d.score != null || (d.comments && d.comments.length) || (d.rubric && d.rubric.length)) {
      fb += '<div class="fb-h">Feedback</div>';
      if (d.score != null) fb += '<div class="fb-score">Score: ' + d.score + (d.points ? (' / ' + d.points.replace(' pts', '')) : '') + '</div>';
      if (d.rubric && d.rubric.length) {
        fb += '<table class="fb-rub">';
        d.rubric.forEach(r => { fb += '<tr><td>' + esc(r.desc || '') + '</td><td class="gs">' + (r.points != null ? r.points : '—') + (r.max != null ? (' / ' + r.max) : '') + '</td></tr>' + (r.comment ? '<tr><td colspan="2" class="fb-c">' + esc(r.comment) + '</td></tr>' : ''); });
        fb += '</table>';
      }
      if (d.comments && d.comments.length) {
        fb += '<div class="fb-cm">';
        d.comments.forEach(c => { fb += '<div class="fb-cmrow"><b>' + esc(c.author) + '</b> ' + esc(c.text) + '</div>'; });
        fb += '</div>';
      }
    }
    document.getElementById('mFb').innerHTML = fb;
    // Actions
    let act = '';
    if (d.assignId) act += '<button class="dlbtn" id="mDone">' + (d.submitted ? 'Done — undo' : 'Mark done') + '</button>';
    document.getElementById('mActions').innerHTML = act;
    const doneBtn = document.getElementById('mDone');
    if (doneBtn) doneBtn.addEventListener('click', () => markDone(d));
    // Notes (synced to Canvas) + focus timer
    let nh = '';
    if (d.assignId) {
      nh = '<div class="notes-h">Notes <span class="notes-sub">— synced to your Canvas planner</span></div>'
        + '<textarea id="mNote" rows="3" placeholder="Private notes for this assignment..."></textarea>'
        + '<div class="notes-row"><button class="ghostbtn" id="mNoteSave">Save note</button>'
        + '<button class="ghostbtn" id="mFocus">Focus 25 min</button><span class="logged" id="mLogged"></span></div>';
    }
    document.getElementById('mNotes').innerHTML = nh;
    if (d.assignId) {
      document.getElementById('mNote').value = d.note || '';
      document.getElementById('mNoteSave').addEventListener('click', () => saveNote(d));
      document.getElementById('mFocus').addEventListener('click', () => startFocus(d));
      const lg = getLogged(d.uid); document.getElementById('mLogged').textContent = lg ? (lg + ' min logged') : '';
    }
    // Submit (text / URL)
    const canText = (d.subTypes || []).includes('online_text_entry');
    const canUrl = (d.subTypes || []).includes('online_url');
    let sub = '';
    if (d.assignId && !d.submitted && (canText || canUrl)) {
      const opts = (canText ? '<option value="online_text_entry">Text entry</option>' : '')
        + (canUrl ? '<option value="online_url">Website URL</option>' : '');
      sub = '<div class="sub-h">Submit</div><select id="subType">' + opts + '</select>'
        + '<textarea id="subText" rows="4" placeholder="Type your submission..."></textarea>'
        + '<input id="subUrl" type="url" placeholder="https://..." style="display:none">'
        + '<div class="notes-row"><button class="dlbtn" id="subBtn">Submit to Canvas</button>'
        + '<span class="sub-warn">This submits for real.</span></div>';
    } else if (d.assignId && !d.submitted) {
      sub = '<div class="sub-note">Submitted through Canvas (e.g. zyBooks or a quiz) — use “Open in Canvas”.</div>';
    }
    document.getElementById('mSubmit').innerHTML = sub;
    if (document.getElementById('subType')) {
      const st = document.getElementById('subType'), tx = document.getElementById('subText'), ur = document.getElementById('subUrl');
      const upd = () => { const u = st.value === 'online_url'; ur.style.display = u ? '' : 'none'; tx.style.display = u ? 'none' : ''; };
      st.addEventListener('change', upd); upd();
      document.getElementById('subBtn').addEventListener('click', () => submitWork(d));
    }
    modal.classList.add('open');
  }
  async function submitWork(d) {
    const type = document.getElementById('subType').value;
    const content = type === 'online_url' ? document.getElementById('subUrl').value.trim() : document.getElementById('subText').value;
    if (!content) { showToast('Nothing to submit yet.'); return; }
    if (!confirm('Submit this to Canvas now? This counts as a real submission.')) return;
    try {
      const r = await fetch('/api/canvas/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ course_id: d.courseId, assign_id: d.assignId, type, content }) });
      const j = await r.json();
      if (j.ok) { showToast('Submitted to Canvas. Click to refresh.', () => location.reload()); closeModal(); }
      else showToast('Submit failed: ' + (j.error || 'unknown'));
    } catch (e) { showToast('Submit needs the installed app.'); }
  }
  function closeModal() { modal.classList.remove('open'); }
  async function markDone(d) {
    const makeDone = !d.submitted;
    try {
      const r = await fetch('/api/canvas/done', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assign_id: d.assignId, done: makeDone }) });
      const j = await r.json();
      if (j.ok) { showToast(makeDone ? 'Marked done in Canvas — reload to update.' : 'Marked not done — reload to update.', () => location.reload()); closeModal(); }
      else showToast(j.error || 'Could not update (installed app only).');
    } catch (e) { showToast('Mark-done needs the installed app.'); }
  }
  document.querySelectorAll('[data-id]').forEach(c =>
    c.addEventListener('click', () => openModal(+c.dataset.id)));
  document.getElementById('modalX').addEventListener('click', closeModal);
  modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  // ---- Week board (drag-to-reschedule, saved in localStorage) ----
  const KANBAN = JSON.parse(document.getElementById('kanbandata').textContent);
  const PLAN_KEY = 'canvasPlannerPlan';
  const HIDE_KEY = 'canvasPlannerHidden';
  const SORT_KEY = 'canvasPlannerSort';
  const loadHidden = () => { try { return new Set(JSON.parse(localStorage.getItem(HIDE_KEY)) || []); } catch (e) { return new Set(); } };
  const saveHidden = s => localStorage.setItem(HIDE_KEY, JSON.stringify([...s]));
  let traySort = localStorage.getItem(SORT_KEY) || 'due';

  function sortItems(arr) {
    const a = arr.slice();
    if (traySort === 'points') a.sort((x, y) => (Number(y.points) || 0) - (Number(x.points) || 0));
    else if (traySort === 'course') a.sort((x, y) => x.course.localeCompare(y.course) || (x.due || '~').localeCompare(y.due || '~'));
    else if (traySort === 'title') a.sort((x, y) => x.title.localeCompare(y.title));
    else a.sort((x, y) => (x.due || '~').localeCompare(y.due || '~'));
    return a;
  }

  function renderChips() {
    const hidden = loadHidden();
    const seen = {}; const courses = [];
    KANBAN.forEach(it => { if (!(it.course in seen)) { seen[it.course] = it.color; courses.push(it.course); } });
    const box = document.getElementById('courseChips');
    box.innerHTML = '';
    courses.forEach(c => {
      const chip = document.createElement('span');
      chip.className = 'chip' + (hidden.has(c) ? ' off' : '');
      chip.innerHTML = '<span class="cdot" style="background:' + seen[c] + '"></span>' + esc(c);
      chip.addEventListener('click', () => {
        const h = loadHidden();
        if (h.has(c)) h.delete(c); else h.add(c);
        saveHidden(h); renderChips(); renderBoard();
      });
      box.appendChild(chip);
    });
  }
  const DAY = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const loadPlan = () => { try { return JSON.parse(localStorage.getItem(PLAN_KEY)) || {}; } catch (e) { return {}; } };
  const savePlan = p => localStorage.setItem(PLAN_KEY, JSON.stringify(p));
  const ymd = d => d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
  const dueYmd = iso => iso ? ymd(new Date(iso)) : null;
  const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };

  // Relative, human due labels: "today 10:00 PM", "tomorrow", "Fri 11:59 PM"...
  function relDue(iso) {
    const d = new Date(iso), now = new Date();
    const days = Math.round((new Date(d).setHours(0,0,0,0) - new Date(now).setHours(0,0,0,0)) / 864e5);
    const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (days < 0) return { label: Math.abs(days) + 'd overdue', cls: ' urgent' };
    if (days === 0) return { label: 'today ' + time, cls: ' urgent' };
    if (days === 1) return { label: 'tomorrow ' + time, cls: ' soon' };
    if (days <= 6) return { label: DAY[d.getDay()] + ' ' + time, cls: '' };
    return { label: MON[d.getMonth()] + ' ' + d.getDate(), cls: '' };
  }

  async function quickDone(it, det, card) {
    card.style.opacity = '.4';
    try {
      const r = await fetch('/api/canvas/done', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assign_id: det.assignId, done: true }) });
      const j = await r.json();
      if (!j.ok) { card.style.opacity = ''; showToast(j.error || 'Could not update (installed app only).'); return; }
      it.done = true; det.submitted = true; renderBoard();
      showToast('Marked done in Canvas. Click to undo.', async () => {
        try { await fetch('/api/canvas/done', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ assign_id: det.assignId, done: false }) }); } catch (e) {}
        it.done = false; det.submitted = false; renderBoard();
      });
    } catch (e) { card.style.opacity = ''; showToast('Mark-done needs the installed app.'); }
  }

  function makeCard(it) {
    const card = document.createElement('div'); card.className = 'kcard'; card.draggable = true;
    card.dataset.uid = it.uid; card.dataset.did = it.did;
    card.style.background = it.color + '24'; card.style.border = '0.5px solid ' + it.color + '59';
    const due = it.due ? relDue(it.due) : null;
    card.innerHTML = '<div class="kt">' + esc(it.title) + '</div><div class="km"><span style="color:' + it.color + '">' + esc(it.course) + '</span>' + (due ? '<span class="due' + due.cls + '">' + (it.approx ? '~due ' : 'due ') + esc(due.label) + '</span>' : '') + '</div>';
    const det = DATA[it.did];
    if (det && det.assignId && !det.submitted) {
      const b = document.createElement('button'); b.className = 'kdone'; b.type = 'button';
      b.title = 'Mark done in Canvas'; b.textContent = '✓';
      b.addEventListener('click', e => { e.stopPropagation(); quickDone(it, det, card); });
      card.appendChild(b);
    }
    card.addEventListener('dragstart', e => { e.dataTransfer.setData('text/uid', it.uid); card.classList.add('dragging'); });
    card.addEventListener('dragend', () => card.classList.remove('dragging'));
    card.addEventListener('click', () => openModal(+card.dataset.did));
    return card;
  }

  function weekCols() {
    const today = new Date(); today.setHours(0,0,0,0);
    const view = localStorage.getItem('semester.boardView') || 'rolling';
    const weekStart = parseInt(localStorage.getItem('semester.weekStart') || '0', 10);
    const weekends = (localStorage.getItem('semester.weekends') || 'on') === 'on';
    let start = new Date(today);
    if (view === 'week') { const diff = (today.getDay() - weekStart + 7) % 7; start.setDate(today.getDate() - diff); }
    const cols = [];
    for (let i = 0; cols.length < 7 && i < 16; i++) {
      const d = new Date(start); d.setDate(start.getDate() + i);
      const wd = d.getDay(); if (!weekends && (wd === 0 || wd === 6)) continue;
      cols.push({key: ymd(d), date: d});
    }
    return {cols, todayKey: ymd(today)};
  }

  // ---- "Up next" strip: the next deadline + how much lands on each day ----
  function renderUpNext() {
    const box = document.getElementById('upnext'); if (!box) return;
    const now = new Date();
    const live = KANBAN.filter(it => !it.done && it.due);
    const byDay = {};
    live.forEach(it => { const k = dueYmd(it.due); byDay[k] = (byDay[k] || 0) + 1; });
    const today = new Date(); today.setHours(0,0,0,0);
    const parts = [];
    for (let i = 0; i < 7; i++) {
      const d = new Date(today); d.setDate(today.getDate() + i);
      const n = byDay[ymd(d)]; if (!n) continue;
      const name = i === 0 ? 'today' : i === 1 ? 'tomorrow' : DAY[d.getDay()];
      parts.push('<span class="un-day' + (i <= 1 ? ' hot' : '') + '"><b>' + n + '</b> due ' + name + '</span>');
    }
    const next = live.filter(it => new Date(it.due) > now).sort((a, b) => a.due.localeCompare(b.due))[0];
    let lead = '<span class="un-next">Nothing due in the next 7 days.</span>';
    if (next) {
      const hrs = Math.max(1, Math.round((new Date(next.due) - now) / 36e5));
      const when = hrs <= 24 ? 'in ' + hrs + 'h' : relDue(next.due).label;
      lead = '<span class="un-next">Next up: <b>' + esc(next.title) + '</b> · ' + when + '</span>';
    }
    box.innerHTML = lead + parts.join('');
  }

  function renderBoard() {
    const plan = loadPlan();
    const board = document.getElementById('weekboard');
    const tray = document.getElementById('weektray');
    board.innerHTML = ''; tray.innerHTML = '';
    const {cols, todayKey} = weekCols();
    const windowKeys = new Set(cols.map(c => c.key));
    const targets = {};
    cols.forEach(c => {
      const isToday = c.key === todayKey;
      const el = document.createElement('div'); el.className = 'col' + (isToday ? ' today' : ''); el.dataset.key = c.key;
      el.innerHTML = '<h4>' + (isToday ? 'Today' : DAY[c.date.getDay()]) + ' <span class="d">' + MON[c.date.getMonth()] + ' ' + c.date.getDate() + '</span></h4>';
      board.appendChild(el); targets[c.key] = el;
    });
    tray.dataset.key = 'tray'; targets['tray'] = tray;

    const hidden = loadHidden();
    const visible = sortItems(KANBAN.filter(it => !hidden.has(it.course) && !it.done));
    const undated = [];
    visible.forEach(it => {
      const pd = plan[it.uid];
      const dueK = it.due ? dueYmd(it.due) : null;
      // Only honor a placement that's a visible day AND not after the due date.
      const ok = pd && windowKeys.has(pd) && (!dueK || pd <= dueK);
      if (!ok && !it.due) { undated.push(it); return; }
      targets[ok ? pd : 'tray'].appendChild(makeCard(it));
    });

    const thr = parseInt(localStorage.getItem('semester.dayThreshold') || '4', 10);
    cols.forEach(c => { const el = targets[c.key];
      const n = el.querySelectorAll('.kcard').length;
      el.classList.toggle('load-warn', n >= thr && n < thr + 2);
      el.classList.toggle('load-heavy', n >= thr + 2);
      if (n) el.querySelector('h4').insertAdjacentHTML('beforeend', '<span class="load">' + n + '</span>');
      else { const p = document.createElement('div'); p.className = 'empty-col'; p.textContent = '·'; el.appendChild(p); }
    });
    // Deadlines for unplanned work show faintly on their due day, so the week never looks empty.
    visible.forEach(it => {
      const dk = it.due ? dueYmd(it.due) : null;
      if (!dk || !targets[dk] || plan[it.uid] === dk) return;
      const g = document.createElement('div'); g.className = 'ghost-due';
      g.textContent = 'due · ' + it.title; g.title = it.title;
      g.addEventListener('click', () => openModal(+it.did));
      targets[dk].appendChild(g);
    });
    document.getElementById('undatedBox')?.remove();
    if (undated.length) {
      const box = document.createElement('details'); box.id = 'undatedBox'; box.className = 'undated';
      box.innerHTML = '<summary>No due date · ' + undated.length + '</summary><div class="kwrap"></div>';
      undated.forEach(it => box.querySelector('.kwrap').appendChild(makeCard(it)));
      tray.after(box);
    }
    if (!tray.querySelector('.kcard')) { const p = document.createElement('div'); p.className = 'tray-empty'; p.textContent = 'All scheduled.'; tray.appendChild(p); }
    renderUpNext();

    Object.values(targets).forEach(el => {
      el.addEventListener('dragover', e => { e.preventDefault(); el.classList.add('drop'); });
      el.addEventListener('dragleave', () => el.classList.remove('drop'));
      el.addEventListener('drop', e => {
        e.preventDefault(); el.classList.remove('drop');
        const uid = e.dataTransfer.getData('text/uid'); if (!uid) return;
        const key = el.dataset.key;
        if (key !== 'tray') {
          const it = KANBAN.find(x => x.uid === uid);
          if (it && it.due && key > dueYmd(it.due)) { showToast('Cannot schedule after the due date.'); return; }
        }
        const p = loadPlan();
        if (key === 'tray') delete p[uid]; else p[uid] = key;
        savePlan(p); renderBoard();
      });
    });
  }

  const sortSel = document.getElementById('traySort');
  sortSel.value = traySort;
  sortSel.addEventListener('change', () => { traySort = sortSel.value; localStorage.setItem(SORT_KEY, traySort); renderBoard(); });
  renderChips();
  renderBoard();

  // ---- Auto-schedule: fill the tray across the week by strategy ----
  function autoSchedule(algo) {
    const {cols} = weekCols();
    const dayKeys = cols.map(c => c.key);
    if (!dayKeys.length) return;
    const cap = parseInt(localStorage.getItem('semester.dayThreshold') || '4', 10);
    const plan = loadPlan();
    const hidden = loadHidden();
    const load = {}; dayKeys.forEach(k => load[k] = 0);
    // Seed each day's load from cards already placed by hand (we keep those).
    KANBAN.forEach(it => { if (hidden.has(it.course)) return; const d = plan[it.uid]; if (d && load[d] !== undefined) load[d]++; });
    // Candidates: visible items still in the tray (unscheduled), most urgent first.
    const tray = KANBAN.filter(it => it.due && !it.done && !hidden.has(it.course) && !(plan[it.uid] && dayKeys.includes(plan[it.uid])));
    tray.sort((a, b) => (a.due || '~').localeCompare(b.due || '~'));
    const idxOf = k => dayKeys.indexOf(k);
    for (const it of tray) {
      const dk = it.due ? dueYmd(it.due) : null;
      let allowed = dayKeys.filter(k => !dk || k <= dk);   // never past the due date
      if (!allowed.length) allowed = [dayKeys[0]];          // due before the window -> earliest day
      const open = allowed.filter(k => load[k] < cap);      // respect the daily cap
      if (!open.length) continue;                            // no room -> leave it in the tray
      let pick;
      if (algo === 'front') pick = open[0];
      else if (algo === 'jit') pick = open[open.length - 1];
      else if (algo === 'balanced') pick = open.reduce((b, k) => load[k] < load[b] ? k : b, open[0]);
      else {  // deadline-first: open day nearest the suggested start day
        const sk = it.start ? dueYmd(it.start) : null;
        let ti = 0;
        if (sk) { const ix = idxOf(sk); ti = ix >= 0 ? ix : Math.max(0, dayKeys.filter(k => k <= sk).length - 1); }
        pick = open.reduce((b, k) => Math.abs(idxOf(k) - ti) < Math.abs(idxOf(b) - ti) ? k : b, open[0]);
      }
      plan[it.uid] = pick; load[pick]++;
    }
    savePlan(plan); renderBoard();
  }
  document.getElementById('autoBtn').addEventListener('click', () => {
    const sel = document.getElementById('autoAlgo');
    autoSchedule(sel.value);
    showToast('Week planned · ' + sel.options[sel.selectedIndex].text);
  });
  document.getElementById('clearPlan').addEventListener('click', () => {
    const p = loadPlan(); KANBAN.forEach(it => delete p[it.uid]); savePlan(p); renderBoard();
    showToast('Cleared — everything is back in the tray.');
  });

  // ---- Calendar export (.ics) ----
  const pad = n => String(n).padStart(2, '0');
  const icsDate = d => d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate());
  function buildICS() {
    const inclDue = $id('icsDue').checked, inclPlan = $id('icsPlan').checked, plan = loadPlan();
    const L = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Semester//EN', 'CALSCALE:GREGORIAN'];
    const stamp = icsDate(new Date()) + 'T000000Z';
    const ev = (date, summary, uid) => { const d = new Date(date), e = new Date(d); e.setDate(d.getDate() + 1);
      L.push('BEGIN:VEVENT', 'UID:' + uid + '@semester', 'DTSTAMP:' + stamp,
             'DTSTART;VALUE=DATE:' + icsDate(d), 'DTEND;VALUE=DATE:' + icsDate(e),
             'SUMMARY:' + summary.replace(/[,;\\]/g, ' '), 'END:VEVENT'); };
    KANBAN.forEach(it => {
      if (inclDue && it.due) ev(new Date(it.due), 'Due: ' + it.title + ' (' + it.course + ')', 'due-' + it.uid);
      if (inclPlan && plan[it.uid]) ev(plan[it.uid] + 'T12:00:00', 'Work on: ' + it.title + ' (' + it.course + ')', 'plan-' + it.uid);
    });
    L.push('END:VCALENDAR'); return L.join('\r\n');
  }
  $id('icsExport').addEventListener('click', () => {
    const blob = new Blob([buildICS()], { type: 'text/calendar' }), url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'semester.ics'; a.click(); URL.revokeObjectURL(url);
  });

  // Live feed URL (installed app only — the server regenerates it on every request)
  if (location.protocol.indexOf('http') === 0 && $id('icsFeedRow')) {
    const feedUrl = location.origin + '/calendar.ics';
    $id('icsFeedRow').style.display = '';
    $id('icsFeedUrl').textContent = feedUrl;
    $id('icsCopy').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(feedUrl); showToast('Feed URL copied — paste it into your calendar app.'); }
      catch (e) { prompt('Copy this URL:', feedUrl); }
    });
  }

  // ---- Export selected sections as AI-ready JSON ----
  const EXPORT = JSON.parse(document.getElementById('exportdata').textContent);
  const EXP_SECTIONS = ['assignments', 'discussions', 'announcements', 'grades', 'courses'];
  const loadExpSel = () => { try { return JSON.parse(LS.getItem('semester.exportSel')) || { assignments: true, announcements: true }; } catch (e) { return { assignments: true, announcements: true }; } };
  EXP_SECTIONS.forEach(s => {
    const cb = $id('exp_' + s); if (!cb) return;
    cb.checked = loadExpSel()[s] === true;
    cb.addEventListener('change', () => { const sel = loadExpSel(); sel[s] = cb.checked; LS.setItem('semester.exportSel', JSON.stringify(sel)); });
  });
  $id('aiExport').addEventListener('click', () => {
    const sel = loadExpSel();
    const out = { instructions: EXPORT.instructions, generated: EXPORT.generated, today: EXPORT.today };
    const counts = [];
    EXP_SECTIONS.forEach(s => { if (sel[s] && EXPORT[s]) { out[s] = EXPORT[s]; counts.push(EXPORT[s].length + ' ' + s); } });
    if (!counts.length) { showToast('Pick at least one section to export.'); return; }
    const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' }), url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'semester-export-' + EXPORT.today + '.json'; a.click(); URL.revokeObjectURL(url);
    showToast('Exported ' + counts.join(', ') + '.');
  });

  // ---- Auto-refresh nudge (installed app only; static file just no-ops) ----
  const PAGE_BUILT = __PAGE_BUILT__;
  async function checkUpdates() {
    if ((LS.getItem('semester.autoOn') || 'on') !== 'on') return;
    try { const r = await fetch('/api/status', { cache: 'no-store' }); const s = await r.json();
      if (s.built && s.built > PAGE_BUILT) showToast('Fresh data from Canvas — click to reload.', () => location.reload());
    } catch (e) {}
  }
  setInterval(checkUpdates, 5 * 60 * 1000);


  // ---- Notifications while the app is open ----
  function notifyDueSoon() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'default') Notification.requestPermission();
    if (Notification.permission !== 'granted') return;
    let done; try { done = new Set(JSON.parse(LS.getItem('semester.notified')) || []); } catch (e) { done = new Set(); }
    const now2 = new Date(), todayK = ymd(now2), plan = loadPlan();
    KANBAN.forEach(it => {
      if (it.due) { const hrs = (new Date(it.due) - now2) / 36e5, k = 'due:' + it.uid;
        if (hrs > 0 && hrs <= 12 && !done.has(k)) { new Notification('Due soon: ' + it.title, { body: it.course + ' — due ' + it.dueLabel }); done.add(k); } }
      if (it.start) { const sk = 'start:' + it.uid + ':' + todayK;
        if (ymd(new Date(it.start)) === todayK && !done.has(sk)) { new Notification('Time to start: ' + it.title, { body: it.course + ' — suggested start day' }); done.add(sk); } }
    });
    LS.setItem('semester.notified', JSON.stringify([...done]));
  }
  setTimeout(notifyDueSoon, 2500); setInterval(notifyDueSoon, 30 * 60 * 1000);

  // ---- Grades: live what-if / what-do-I-need projector ----
  const GRADES = JSON.parse(document.getElementById('gradesdata').textContent);
  function projected(course, hypo) {
    if (course.weighted) {
      const groups = {};
      course.items.forEach((it, i) => { const v = (hypo[i] != null) ? hypo[i] : (it.graded ? it.score : null);
        if (v == null || !it.points) return; const g = groups[it.group] || (groups[it.group] = { w: it.weight || 0, e: 0, p: 0 }); g.e += v; g.p += it.points; });
      let ws = 0, acc = 0; Object.values(groups).forEach(g => { if (g.p > 0 && g.w > 0) { acc += g.w * (g.e / g.p); ws += g.w; } });
      return ws > 0 ? acc / ws * 100 : null;
    }
    let e = 0, p = 0; course.items.forEach((it, i) => { const v = (hypo[i] != null) ? hypo[i] : (it.graded ? it.score : null); if (v == null || !it.points) return; e += v; p += it.points; });
    return p > 0 ? e / p * 100 : null;
  }
  function neededFor(course, target) {
    const rem = course.items.map((it, i) => i).filter(i => !course.items[i].graded && course.items[i].points);
    if (!rem.length) return null;
    const at = pct => { const h = {}; rem.forEach(i => h[i] = course.items[i].points * pct / 100); return projected(course, h); };
    const best = at(100), worst = at(0);
    if (best == null) return null;
    if (best < target) return Infinity;              // even 100% on the rest falls short
    if (worst != null && worst >= target) return 0;  // already there
    let lo = 0, hi = 100;
    for (let k = 0; k < 40; k++) { const mid = (lo + hi) / 2; const hypo = {}; rem.forEach(i => hypo[i] = course.items[i].points * mid / 100);
      const pr = projected(course, hypo); if (pr == null) return null; if (pr < target) lo = mid; else hi = mid; }
    return (lo + hi) / 2;
  }
  function renderGrades() {
    const box = document.getElementById('gradesBox');
    if (!GRADES.length) { box.innerHTML = '<p class="empty">No grades available.</p>'; return; }
    box.innerHTML = '';
    GRADES.forEach(course => {
      const hypo = {};
      const card = document.createElement('div'); card.className = 'course-card'; card.style.borderTop = '3px solid ' + course.color;
      const cur = course.score != null ? (course.score.toFixed(1) + '%' + (course.grade ? (' · ' + course.grade) : '')) : '—';
      let rows = '';
      course.items.forEach((it, i) => {
        const val = it.graded && it.score != null ? it.score : '';
        rows += '<tr><td>' + esc(it.title) + '</td><td class="gs"><input type="number" data-i="' + i + '" value="' + val + '"' + (it.graded ? '' : ' placeholder="—"') + '> <span class="gp">/ ' + (it.points != null ? (+it.points).toFixed(0) : '?') + '</span></td></tr>';
      });
      const p0 = projected(course, {});
      card.innerHTML = '<div class="cc-top"><h3><span class="dot" style="background:' + course.color + '"></span>' + esc(course.cleanName) + '</h3><span class="bigscore">' + cur + '</span></div>'
        + '<div class="proj">Projected <span class="pv" data-pv>' + (p0 != null ? p0.toFixed(1) + '%' : '—') + '</span>'
        + '<span class="needline">Target <input type="number" class="target-in" data-target value="90">% → <span data-need></span></span></div>'
        + '<table class="gtable">' + rows + '</table>';
      box.appendChild(card);
      const pv = card.querySelector('[data-pv]'), needEl = card.querySelector('[data-need]'), tin = card.querySelector('[data-target]');
      const recompute = () => {
        const pr = projected(course, hypo); pv.textContent = pr != null ? pr.toFixed(1) + '%' : '—';
        const tgt = parseFloat(tin.value); const need = (!isNaN(tgt)) ? neededFor(course, tgt) : null;
        needEl.innerHTML = need == null ? 'all graded' : need > 100 ? 'out of reach — even 100% on the rest won’t get there' : need <= 0 ? 'you’re already there' : ('need <b>' + need.toFixed(1) + '%</b> avg on remaining');
      };
      card.querySelectorAll('input[data-i]').forEach(inp => inp.addEventListener('input', () => {
        const i = +inp.dataset.i, v = parseFloat(inp.value); if (inp.value === '' || isNaN(v)) delete hypo[i]; else hypo[i] = v; recompute();
      }));
      tin.addEventListener('input', recompute); recompute();
    });
  }
  renderGrades();

  // ---- Notes (synced) + Focus timer ----
  const timeLog = () => { try { return JSON.parse(LS.getItem('semester.time')) || {}; } catch (e) { return {}; } };
  const getLogged = uid => Math.round(timeLog()[uid] || 0);
  async function saveNote(d) {
    const text = document.getElementById('mNote').value;
    try {
      const r = await fetch('/api/canvas/note', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assign_id: d.assignId, text, title: d.title }) });
      const j = await r.json();
      showToast(j.ok ? 'Note saved to your Canvas planner.' : 'Could not save note (installed app only).');
    } catch (e) { showToast('Notes need the installed app.'); }
  }
  let focusTimer = null, focusRemain = 0, focusUid = null, focusPaused = false;
  const focusBar = document.getElementById('focusBar');
  const fmtT = s => Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  function startFocus(d) {
    focusUid = d.uid; focusRemain = 25 * 60; focusPaused = false;
    focusBar.innerHTML = '<b>Focus</b> <span class="ft">25:00</span> <button data-p aria-label="pause">Pause</button><button data-s aria-label="stop">Stop</button>';
    focusBar.classList.add('show');
    focusBar.querySelector('[data-p]').onclick = () => { focusPaused = !focusPaused; focusBar.querySelector('[data-p]').textContent = focusPaused ? 'Resume' : 'Pause'; };
    focusBar.querySelector('[data-s]').onclick = () => stopFocus(false);
    clearInterval(focusTimer);
    focusTimer = setInterval(() => {
      if (focusPaused) return;
      if (--focusRemain <= 0) { stopFocus(true); return; }
      focusBar.querySelector('.ft').textContent = fmtT(focusRemain);
    }, 1000);
    closeModal();
  }
  function stopFocus(completed) {
    clearInterval(focusTimer); focusBar.classList.remove('show');
    const mins = completed ? 25 : Math.round((25 * 60 - focusRemain) / 60);
    if (focusUid && mins > 0) { const t = timeLog(); t[focusUid] = (t[focusUid] || 0) + mins; LS.setItem('semester.time', JSON.stringify(t)); }
    if (completed) { showToast('Focus session done — 25 min logged.');
      if ('Notification' in window && Notification.permission === 'granted') new Notification('Focus session complete', { body: 'Nice work — take a break.' }); }
  }

  // ---- Inbox (read + reply + compose) ----
  let inboxLoaded = false;
  const inboxBox = document.getElementById('inboxBox');
  const jget = async url => (await fetch(url)).json();
  const jpost = async (url, b) => (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) })).json();
  const fmtDate = s => { try { return new Date(s).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); } catch (e) { return ''; } };
  async function loadInbox() {
    inboxLoaded = true; inboxBox.innerHTML = '<p class="empty">Loading messages…</p>';
    const j = await jget('/api/canvas/conversations');
    if (!j.ok) { inboxBox.innerHTML = '<p class="empty">' + esc(j.error || 'Inbox needs the installed app.') + '</p>'; return; }
    if (!j.items.length) { inboxBox.innerHTML = '<p class="empty">No messages.</p>'; return; }
    inboxBox.innerHTML = j.items.map(c => '<div class="conv' + (c.unread ? ' unread' : '') + '" data-id="' + c.id + '"><div class="cs">' + esc(c.subject) + '</div><div class="cw">' + esc(c.with || '') + ' · ' + fmtDate(c.date) + '</div><div class="cp">' + esc(c.preview || '') + '</div></div>').join('');
    inboxBox.querySelectorAll('.conv').forEach(el => el.addEventListener('click', () => openThread(+el.dataset.id)));
  }
  async function openThread(id) {
    inboxBox.innerHTML = '<p class="empty">Loading…</p>';
    const j = await jget('/api/canvas/conversation?id=' + id);
    if (!j.ok) { inboxBox.innerHTML = '<p class="empty">' + esc(j.error || 'Error') + '</p>'; return; }
    let h = '<span class="backlink" id="ibBack">← Inbox</span><h2 style="font-size:18px;margin:0 0 12px">' + esc(j.subject || '') + '</h2><div class="thread">';
    h += j.messages.map(m => '<div class="msg"><span class="ma">' + esc(m.author) + '</span> <span class="md">' + fmtDate(m.date) + '</span><div class="mb">' + esc(m.body || '') + '</div></div>').join('');
    h += '</div><div class="composer"><textarea id="replyBody" rows="3" placeholder="Reply…"></textarea><button class="dlbtn" id="replyBtn">Send reply</button></div>';
    inboxBox.innerHTML = h;
    document.getElementById('ibBack').onclick = () => loadInbox();
    document.getElementById('replyBtn').onclick = async () => {
      const body = document.getElementById('replyBody').value.trim(); if (!body) { showToast('Reply is empty.'); return; }
      const r = await jpost('/api/canvas/reply', { id, body });
      if (r.ok) { showToast('Reply sent.'); openThread(id); } else showToast('Could not send: ' + (r.error || ''));
    };
  }
  function composeView() {
    const opts = COURSES.map(c => '<option value="' + c.id + '">' + esc(c.name) + '</option>').join('');
    inboxBox.innerHTML = '<span class="backlink" id="ibBack">← Inbox</span><div class="composer">'
      + '<select id="cmpCourse"><option value="">Choose a course…</option>' + opts + '</select>'
      + '<div id="cmpTeachers" class="empty" style="font-size:13px;margin-bottom:8px"></div>'
      + '<input id="cmpSubject" placeholder="Subject"><textarea id="cmpBody" rows="4" placeholder="Message…"></textarea>'
      + '<button class="dlbtn" id="cmpSend">Send</button></div>';
    document.getElementById('ibBack').onclick = () => loadInbox();
    let recipients = [];
    document.getElementById('cmpCourse').addEventListener('change', async e => {
      recipients = []; const cid = e.target.value; const box = document.getElementById('cmpTeachers');
      if (!cid) { box.textContent = ''; return; }
      box.textContent = 'Loading instructors…';
      const j = await jget('/api/canvas/teachers?course_id=' + cid);
      if (!j.ok || !j.teachers.length) { box.textContent = 'No instructors found.'; return; }
      recipients = j.teachers.map(t => t.id);
      box.innerHTML = 'To: ' + j.teachers.map(t => esc(t.name)).join(', ');
    });
    document.getElementById('cmpSend').onclick = async () => {
      const subject = document.getElementById('cmpSubject').value.trim(), body = document.getElementById('cmpBody').value.trim();
      if (!recipients.length) { showToast('Pick a course first.'); return; }
      if (!body) { showToast('Message is empty.'); return; }
      const r = await jpost('/api/canvas/compose', { course_id: document.getElementById('cmpCourse').value, recipients, subject, body });
      if (r.ok) { showToast('Message sent.'); loadInbox(); } else showToast('Could not send: ' + (r.error || ''));
    };
  }
  document.querySelector('.nav[data-p="inbox"]').addEventListener('click', () => { if (!inboxLoaded) loadInbox(); });
  document.getElementById('inboxRefresh').addEventListener('click', loadInbox);
  document.getElementById('composeBtn').addEventListener('click', composeView);

  // ---- Auto-update: check GitHub for a newer release ----
  const LABEL = { git: 'Update & restart', selfupdate: 'Download & install', download: 'Download' };
  async function applyUpdate(mode) {
    if (mode === 'selfupdate') showToast('Downloading & installing — Semester will relaunch…');
    else showToast('Updating…');
    try {
      const j = await (await fetch('/api/update/run', { method: 'POST' })).json();
      if (j.mode === 'git') showToast(j.ok ? 'Pulled latest — restart Semester to apply.' : ('Update failed: ' + (j.output || j.error || '')));
      else if (j.mode === 'selfupdate') showToast(j.result === 'installing' ? 'Installing… the app will reopen on the new version.' : 'Opening the download page…');
      else showToast('Opening the download page…');
    } catch (e) { /* app may be quitting to swap itself — that's expected */ }
  }
  async function checkUpdate(manual) {
    try {
      const j = await (await fetch('/api/update')).json();
      if (j.newer) {
        const bar = document.getElementById('updBar');
        bar.innerHTML = 'Semester ' + j.latest + ' is available. <button id="updGo">' + (LABEL[j.mode] || 'Download') + '</button><button class="x" id="updX">✕</button>';
        bar.classList.add('show');
        document.getElementById('updGo').onclick = () => applyUpdate(j.mode);
        document.getElementById('updX').onclick = () => bar.classList.remove('show');
      } else if (manual) { showToast("You're up to date (v" + j.current + ")."); }
    } catch (e) { if (manual) showToast('Update check failed.'); }
  }
  document.getElementById('checkUpdate').addEventListener('click', () => checkUpdate(true));
  const autoUp = document.getElementById('autoUpdate');
  if (autoUp) {
    autoUp.checked = LS.getItem('semester.autoupdate') !== 'off';
    autoUp.addEventListener('change', () => LS.setItem('semester.autoupdate', autoUp.checked ? 'on' : 'off'));
  }
  if (LS.getItem('semester.autoupdate') !== 'off') setTimeout(() => checkUpdate(false), 3000);

  // ---- Classes (Google Classroom-style: grid -> class page) ----
  const CLASSES = JSON.parse(document.getElementById('classesdata').textContent);
  const CLS_PAL = ['#6366f1', '#10b981', '#f59e0b', '#ec4899', '#06b6d4', '#8b5cf6'];
  const clsColor = i => CLS_PAL[i % CLS_PAL.length];
  const ICON = { Assignment: 'Assignment', Quiz: 'Quiz', Discussion: 'Discussion', Page: 'Page', File: 'File', ExternalUrl: 'Link', ExternalTool: 'Tool' };
  let clsTab = 'stream';
  function renderClasses() {
    const v = document.getElementById('classView');
    if (!CLASSES.length) { v.innerHTML = '<p class="empty">No classes found.</p>'; return; }
    v.innerHTML = '<div class="classgrid">' + CLASSES.map((c, i) => {
      const grade = c.score != null ? (c.score.toFixed(1) + '%' + (c.grade ? ' ' + c.grade : '')) : '—';
      return '<div class="classcard" data-ci="' + i + '"><div class="top" style="background:' + clsColor(i) + '"><h3>' + esc(c.name) + '</h3></div>'
        + '<div class="bot"><span>' + c.pending + ' to do</span><span class="grade">' + grade + '</span></div></div>';
    }).join('') + '</div>';
    v.querySelectorAll('[data-ci]').forEach(el => el.addEventListener('click', () => { clsTab = 'stream'; renderClassDetail(+el.dataset.ci); }));
  }
  function renderClassDetail(i) {
    const c = CLASSES[i], v = document.getElementById('classView');
    const tabs = [['stream', 'Stream'], ['classwork', 'Classwork'], ['grades', 'Grades'], ['materials', 'Materials']];
    let body = '';
    if (clsTab === 'stream') {
      body = c.stream.length ? c.stream.map(a => '<a class="ann" href="' + (a.url || '#') + '" target="_blank"><div class="card-top"><span class="course">' + esc(c.name) + '</span><span class="date">' + (a.posted ? fmtDate(a.posted) : '') + '</span></div><div class="title">' + esc(a.title) + '</div><div class="prev">' + esc(a.preview || '') + '</div></a>').join('') : '<p class="empty">No announcements.</p>';
    } else if (clsTab === 'classwork') {
      let h = c.modules.map(m => '<div class="mod"><h4>' + esc(m.name || 'Module') + '</h4>' + (m.items || []).map(it => {
        if (it.type === 'SubHeader') return '<div class="modrow" style="font-weight:600;color:var(--dim)"><span class="mt">' + esc(it.title || '') + '</span></div>';
        return '<a class="modrow" href="' + (it.url || '#') + '" target="_blank"><span class="ic">' + (ICON[it.type] || '') + '</span><span class="mt">' + esc(it.title || '') + '</span>' + (it.due_local ? '<span class="md">' + (it.status === 'done' ? '✓ ' : '') + 'due ' + esc(it.due_local) + '</span>' : '') + '</a>';
      }).join('') + '</div>').join('');
      if (c.quizzes.length) h += '<div class="mod"><h4>Quizzes</h4>' + c.quizzes.map(q => '<a class="modrow" href="' + (q.url || '#') + '" target="_blank"><span class="ic">Quiz</span><span class="mt">' + esc(q.title || '') + '</span>' + (q.due_local ? '<span class="md">due ' + esc(q.due_local) + '</span>' : '') + '</a>').join('') + '</div>';
      body = h || '<p class="empty">No classwork posted.</p>';
    } else if (clsTab === 'grades') {
      const grade = c.score != null ? (c.score.toFixed(1) + '%' + (c.grade ? ' · ' + c.grade : '')) : '—';
      body = '<div class="proj">Current grade <span class="pv">' + grade + '</span></div>'
        + (c.grades.length ? '<table class="gtable">' + c.grades.map(g => '<tr><td>' + esc(g.title) + '</td><td class="gs">' + (g.score != null ? g.score : '—') + ' <span class="gp">/ ' + (g.points != null ? (+g.points).toFixed(0) : '?') + '</span></td></tr>').join('') + '</table>' : '<p class="empty">No graded items yet.</p>');
    } else {
      let h = '<div class="matlist">';
      if (c.pages.length) h += '<h4 style="font-size:14px;margin:6px 0 8px">Pages</h4>' + c.pages.map(p => '<a href="' + (p.url || '#') + '" target="_blank"><span>' + esc(p.title || '') + '</span></a>').join('');
      if (c.files.length) h += '<h4 style="font-size:14px;margin:14px 0 8px">Files</h4>' + c.files.map(f => '<a href="' + (f.url || '#') + '" target="_blank"><span>' + esc(f.name || '') + '</span><span class="sz">' + (f.size ? Math.round(f.size / 1024) + ' KB' : '') + '</span></a>').join('');
      h += '</div>';
      body = (c.pages.length || c.files.length) ? h : '<p class="empty">No materials.</p>';
    }
    v.innerHTML = '<div class="clstitle"><button class="clsback" id="clsBack">← Classes</button><h1 style="margin:0;font-size:22px">' + esc(c.name) + '</h1></div>'
      + '<div class="clstabs">' + tabs.map(t => '<button class="clstab' + (t[0] === clsTab ? ' on' : '') + '" data-ct="' + t[0] + '">' + t[1] + '</button>').join('') + '</div><div>' + body + '</div>';
    document.getElementById('clsBack').addEventListener('click', renderClasses);
    v.querySelectorAll('[data-ct]').forEach(b => b.addEventListener('click', () => { clsTab = b.dataset.ct; renderClassDetail(i); }));
  }
  renderClasses();
// ---- Global search: filters cards across every panel ----
const searchBox = document.getElementById('globalSearch');
if (searchBox) {
  searchBox.addEventListener('input', () => {
    const q = searchBox.value.trim().toLowerCase();
    document.querySelectorAll('.card, .ann, .course-card, .kcard, .modrow, .classcard').forEach(el => {
      el.style.display = (!q || el.textContent.toLowerCase().includes(q)) ? '' : 'none';
    });
    document.querySelectorAll('section').forEach(sec => {
      const any = [...sec.querySelectorAll('.card, .ann')].some(el => el.style.display !== 'none');
      if (sec.querySelector('.card, .ann')) sec.style.display = (!q || any) ? '' : 'none';
    });
  });
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'f') { e.preventDefault(); searchBox.focus(); }
  });
}
