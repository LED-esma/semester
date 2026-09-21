// The panels that used to arrive as server-built HTML: To-Do, Discussions, Announcements,
// Courses, plus the counts around them. Everything here draws from the one payload the page
// embeds (and /api/data serves), so there is a single description of a dashboard.
//
// Date labels are NOT built here. They arrive pre-formatted from Python, which is the only
// place that knows how to print a day without a leading zero on Windows as well as Mac.
function renderViews(D) {
  const esc = s => { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; };
  const lines = s => esc(s).replace(/\n/g, '<br>');
  const dot = color => '<span class="dot" style="background:' + esc(color || '#888') + '"></span>';
  const $ = id => document.getElementById(id);

  // ---- The numbers in the sidebar and the stat row ----
  const counts = D.counts || {};
  document.querySelectorAll('[data-count]').forEach(el => {
    el.textContent = counts[el.dataset.count] != null ? counts[el.dataset.count] : 0;
  });
  $('updatedAt').textContent = 'Updated ' + (D.updated || '');
  $('verNum').textContent = D.version || '';
  if (D.version) $('verLink').href = 'https://github.com/LED-esma/semester/releases/tag/v' + D.version;
  $('accentInput').value = localStorage.getItem('semester.accent') || D.accent || '#6366f1';

  // A card is the same shape wherever it appears; only the note beside its due date changes.
  const card = (d, i, note) =>
    '<div class="card" data-id="' + i + '" style="border-left-color:' + esc(d.color || '#555') + '">' +
      '<div class="card-top"><span class="course">' + dot(d.color) + esc(d.course) + '</span>' +
        (d.points ? '<span class="pts">' + esc(d.points) + ' pts</span>' : '') + '</div>' +
      '<div class="title">' + esc(d.title) + '</div>' +
      '<div class="card-bot"><span class="due">' + esc(d.due) + '</span>' + (note || '') + '</div>' +
    '</div>';

  const items = D.items || [];
  const withIndex = items.map((d, i) => ({ d: d, i: i }));

  // ---- To-Do: unfinished work, grouped by how soon it lands ----
  let todo = '';
  if ((D.warnings || []).length) {
    todo = '<div class="warn"><h3>Heavy days ahead — start early</h3><ul>' +
      D.warnings.map(w => '<li><b>' + esc(w.label) + '</b> — ' + esc(w.n) + ' things due</li>').join('') +
      '</ul></div>';
  }

  let anyTodo = false;
  (D.buckets || []).forEach(b => {
    const key = b[0], label = b[1], color = b[2];
    const rows = withIndex.filter(x => !x.d.submitted && x.d.bucket === key);
    if (!rows.length) return;
    anyTodo = true;
    const cells = rows.map(x => card(x.d, x.i,
      // A start date only helps while there is still time to act on it.
      (x.d.start && (key === 'week' || key === 'later'))
        ? '<span class="start">start ' + esc(x.d.start) + '</span>' : '')).join('');
    todo += '<section><h2 style="color:' + esc(color) + '">' + esc(label) +
            ' <span class="count">' + rows.length + '</span></h2>' +
            '<div class="grid">' + cells + '</div></section>';
  });
  $('todo').innerHTML = todo + (anyTodo ? '' : '<p class="empty">Nothing pending.</p>');

  // ---- Discussions: the graded ones, finished last ----
  const graded = withIndex.filter(x => x.d.type === 'discussion' && x.d.graded)
    .sort((a, b) => (a.d.submitted ? 1 : 0) - (b.d.submitted ? 1 : 0) ||
                    (a.d.dueIso ? 0 : 1) - (b.d.dueIso ? 0 : 1) ||
                    (a.d.dueIso || '').localeCompare(b.d.dueIso || ''));
  $('disc').innerHTML = graded.length
    ? '<div class="grid">' + graded.map(x => card(x.d, x.i,
        x.d.submitted ? '<span class="start">Done</span>' : '')).join('') + '</div>'
    : '<p class="empty">No graded discussions.</p>';

  // ---- Announcements ----
  const ann = D.announcements || [];
  $('ann').innerHTML = ann.length ? ann.map(a =>
    '<a class="ann" href="' + esc(a.url || '#') + '" target="_blank" style="border-left-color:' + esc(a.color || '#555') + '">' +
      '<div class="card-top"><span class="course">' + dot(a.color) + esc(a.course) + '</span>' +
        '<span class="date">' + esc(a.postedLabel) + '</span></div>' +
      '<div class="title">' + esc(a.title) + '</div>' +
      '<div class="prev">' + lines(a.preview) + '</div>' +
    '</a>').join('') : '<p class="empty">No recent announcements.</p>';

  // ---- Courses: syllabus, modules, files, pages ----
  const linklist = rows => (rows || []).filter(r => r.url)
    .map(r => '<li><a href="' + esc(r.url) + '" target="_blank">' + esc(r.title || '(untitled)') + '</a></li>').join('');
  const drawer = (label, rows, inner) => (inner
    ? '<details><summary>' + label + ' (' + rows.length + ')</summary><ul class="clist">' + inner + '</ul></details>' : '');

  const crs = D.courseCards || [];
  $('crs').innerHTML = crs.length ? crs.map(c => {
    const mods = (c.modules || []).map(m =>
      '<li class="modname">' + esc(m.name || 'Module') + '</li>' + linklist(m.items)).join('');
    return '<div class="course-card" style="border-top:3px solid ' + esc(c.color || '#888') + '">' +
      '<div class="cc-top"><h3>' + dot(c.color) + esc(c.name) + '</h3>' +
        '<span class="pill">' + esc(c.pending) + ' to do</span></div>' +
      '<div class="links">' +
        '<a href="' + esc(c.url) + '" target="_blank">Open course ↗</a>' +
        '<a href="' + esc(c.syllabusUrl) + '" target="_blank">Full syllabus ↗</a></div>' +
      '<details><summary>Syllabus preview</summary><div class="syl">' +
        (c.syllabus ? lines(c.syllabus) : '<i>No syllabus text posted — open the course to view.</i>') +
      '</div></details>' +
      drawer('Modules', c.modules || [], mods) +
      drawer('Files', c.files || [], linklist(c.files)) +
      drawer('Pages', c.pages || [], linklist(c.pages)) +
    '</div>';
  }).join('') : '<p class="empty">No courses found.</p>';
}
