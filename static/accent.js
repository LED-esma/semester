// Accent colours the student picks can be anything, including light greens and yellows that are
// unreadable as text on white. Derive the versions that stay readable (WCAG 4.5:1, aiming for 4.8
// because the backgrounds are slightly off-white), changing the colour only as much as it takes.
function applyAccent(hex) {
  const root = document.documentElement.style;
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((hex || '').trim());
  if (!m) return;
  let h = m[1]; if (h.length === 3) h = [...h].map(c => c + c).join('');
  const c = [0, 2, 4].map(i => parseInt(h.substr(i, 2), 16));
  const lum = x => { const [r, g, b] = x.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const contrast = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const mix = (x, to, p) => x.map((v, i) => Math.round(v + (to[i] - v) * p));
  const hx = x => '#' + x.map(v => v.toString(16).padStart(2, '0')).join('');
  const readableOn = bg => { const to = lum(bg) > 0.5 ? [0, 0, 0] : [255, 255, 255];
    for (let p = 0; p <= 1.001; p += 0.04) { const t = mix(c, to, p); if (contrast(t, bg) >= 4.8) return hx(t); } return hx(to); };
  root.setProperty('--accent', '#' + h);
  root.setProperty('--accent-ink-light', readableOn([255, 255, 255]));   // accent-coloured text, light theme
  root.setProperty('--accent-ink-dark', readableOn([26, 29, 36]));       // and dark theme
  let f = c, p = 0;                                                      // filled buttons with white text
  while (contrast(f, [255, 255, 255]) < 4.8 && p < 1) { p += 0.04; f = mix(c, [0, 0, 0], p); }
  root.setProperty('--accent-fill', hx(f));
}
