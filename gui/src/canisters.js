// Film canisters for the Film card: each brand's colors and a nod to its lettering, never a copy of a logo.
// Fonts are Inter and Damion (both OFL). Only SVG presentation attributes, so the page's CSP stays strict.
'use strict';

(function () {
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  // word: what goes on the band. A style can also carry a sub line (Kirkland's "Signature").
  const BRANDS = [
    { test: /^kodak-(royal)?gold/, body: '#f2b705', band: '#d22a1e', ink: '#ffd23f', word: 'GOLD', weight: 900, size: 10, ls: 0.8, numInk: '#1a1a1a' },
    { test: /^kodak-portra/, body: '#f2b705', band: '#ffffff', ink: '#1a1a1a', word: 'PORTRA', weight: 800, size: 8, ls: 0.4, numInk: '#1a1a1a' },
    { test: /^kodak-ektachrome|^kodak-elitechrome|^kodak-kodachrome/, body: '#f2b705', band: '#d22a1e', ink: '#ffffff', word: 'KODAK', weight: 900, size: 8.5, ls: 0.4, numInk: '#1a1a1a' },
    { test: /^kodak-/, body: '#f2b705', band: '#d22a1e', ink: '#ffffff', word: 'KODAK', weight: 900, size: 8.5, ls: 0.4, numInk: '#1a1a1a' },
    { test: /^fuji-/, body: '#0b8a4a', band: '#ffffff', ink: '#0b8a4a', word: 'FUJI', weight: 800, size: 10, ls: 0.2, italic: true, numInk: '#ffffff' },
    { test: /^kirkland-/, body: '#111111', band: '#111111', ink: '#ffffff', word: 'KIRKLAND', weight: 900, size: 7, ls: 0, numInk: '#ffffff',
      sub: 'Signature', subFamily: 'Damion', subSize: 11, subInk: '#e31837' },
    { test: /^imation-/, body: '#1d3f8f', band: '#ffffff', ink: '#1d3f8f', word: 'IMATION', weight: 300, size: 8.5, ls: 0, fit: 36, numInk: '#ffffff' },
    { test: /^scotch-/, body: '#c8102e', band: '#ffffff', ink: '#c8102e', word: 'Scotch', weight: 800, size: 9, ls: 0.2, italic: true, numInk: '#ffffff' },
    { test: /^ferrania-/, body: '#f4c300', band: '#1d3f8f', ink: '#ffffff', word: 'SOLARIS', weight: 800, size: 7.5, ls: 0.4, numInk: '#1d3f8f' },
    { test: /^agfa-/, body: '#d4301c', band: '#ffffff', ink: '#d4301c', word: 'AGFA', weight: 800, size: 10, ls: 1.2, numInk: '#ffffff' },
    { test: /^konica-/, body: '#1c1c1c', band: '#e30613', ink: '#ffffff', word: 'KONICA', weight: 800, size: 7.5, ls: 0.6, numInk: '#e30613' },
    { test: /^cinestill-/, body: '#1c1c1c', band: '#c8102e', ink: '#ffffff', word: 'CINESTILL', weight: 800, size: 6.5, ls: 0.3, numInk: '#ffffff' },
    { test: /^ilford-/, body: '#151515', band: '#f2f2f2', ink: '#151515', word: 'ILFORD', weight: 900, size: 8, ls: 0.4, numInk: '#f2f2f2' },
  ];
  const UNKNOWN = { body: '#8a8a8a', band: '#bdbdbd', ink: '#555555', word: '?', weight: 800, size: 11, ls: 0, numInk: '#e6e6e6' };

  function iso(id, name) {
    const m = String(name || '').match(/\b(\d{2,4})\b/) || String(id || '').match(/(\d{2,4})/);
    return m ? m[1] : '';
  }

  function svg(id, name, maker, speed) {
    const s = BRANDS.find((b) => b.test.test(id || ''))
      || (maker && maker !== 'unknown' && BRANDS.find((b) => b.test.test(`${maker}-`))) || UNKNOWN;
    const num = s === UNKNOWN ? '' : (speed ? String(speed) : iso(id, name));
    const word = `<text x="27" y="35.5" text-anchor="middle" font-family="Inter" font-weight="${s.weight}" font-size="${s.size}"`
      + (s.fit ? ` textLength="${s.fit}" lengthAdjust="spacing"` : ` letter-spacing="${s.ls}"`)
      + `${s.italic ? ' font-style="italic"' : ''} fill="${s.ink}">${esc(s.word)}</text>`;
    const sub = s.sub ? `<text x="27" y="45" text-anchor="middle" font-family="${s.subFamily}" font-size="${s.subSize}" fill="${s.subInk}">${esc(s.sub)}</text>` : '';
    const n = num ? `<text x="27" y="${s.sub ? 55 : 53}" text-anchor="middle" font-family="Inter" font-weight="800" font-size="${s.sub ? 8 : 10}" fill="${s.numInk}">${esc(num)}</text>` : '';
    return `<svg width="58" height="70" viewBox="0 0 58 70" aria-hidden="true" focusable="false">
      <defs><linearGradient id="can-shade" x1="0" x2="1"><stop offset="0" stop-color="#000" stop-opacity=".18"/><stop offset=".35" stop-color="#fff" stop-opacity=".18"/><stop offset="1" stop-color="#000" stop-opacity=".25"/></linearGradient></defs>
      <rect x="21" y="0" width="14" height="6" rx="2" fill="#6f6f6f"/>
      <rect x="5" y="6" width="44" height="6" rx="2" fill="#a6a6a6"/>
      <rect x="7" y="12" width="40" height="46" fill="${s.body}"/>
      <rect x="7" y="12" width="40" height="46" fill="url(#can-shade)"/>
      <rect x="7" y="24" width="40" height="16" fill="${s.band}"/>
      ${word}${sub}${n}
      <rect x="5" y="58" width="44" height="6" rx="2" fill="#a6a6a6"/>
      <rect x="47" y="20" width="9" height="30" fill="#5b3a17"/>
    </svg>`;
  }

  window.canisters = { svg, iso };
})();
