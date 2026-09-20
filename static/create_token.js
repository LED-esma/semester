(function () {
  window.__semesterResult = '';
  var m = document.cookie.match(/(?:^|;\s*)_csrf_token=([^;]+)/);
  function ask(days) {
    var tok = { purpose: 'Semester' };
    if (days) tok.expires_at = new Date(Date.now() + days * 864e5 - 36e5).toISOString();
    return fetch('/api/v1/users/self/tokens', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json',
                 'X-CSRF-Token': m ? decodeURIComponent(m[1]) : '' },
      body: JSON.stringify({ token: tok }) })
    .then(function (r) { return r.text().then(function (t) {
      var j = {}; try { j = JSON.parse(t.replace(/^while\(1\);/, '')); } catch (e) {}
      return { status: r.status, j: j, msg: t };  // Canvas sends errors as a bare list: [{"message": "Expiration date is required"}]
    }); });
  }
  // No expiry if the school allows it; otherwise the longest it allows (4cd requires one, max 90 days).
  ask(0).then(function (a) {
    if (a.status !== 400 || !/expir/i.test(a.msg)) return a;
    var cap = a.msg.match(/(\d+)\s*days/);
    return ask(cap ? +cap[1] : 90);
  }).then(function (a) {
    window.__semesterResult = JSON.stringify({ status: a.status, token: a.j.visible_token || a.j.token || null,
                                               id: a.j.id || null, expires_at: a.j.expires_at || null,
                                               blocked: /not authorized|unauthorized/i.test(a.msg)
                                                        && !/unauthenticated|authorization required/i.test(a.msg) });
  }).catch(function () { window.__semesterResult = JSON.stringify({ status: 0, token: null }); });
  return 'started';
})()