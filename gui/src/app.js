// OpenFilmKit desktop: the page. Plain DOM, no framework; all work happens in the Python back end.
'use strict';

const $ = (id) => document.getElementById(id);
const statusEl = $('status');
const say = (text) => { statusEl.textContent = ''; setTimeout(() => { statusEl.textContent = text; }, 30); };

// ---- small helpers --------------------------------------------------------------------
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
  return el;
}
function fromSvg(markup) { const t = document.createElement('template'); t.innerHTML = markup.trim(); return t.content.firstChild; }
// A window for anything that used to fold open: details, messages, the film picker.
function openModal(title, ...content) {
  const dlg = $('info-dialog');
  $('info-title').textContent = title;
  $('info-body').replaceChildren(...content.flat().filter(Boolean));
  dlg.classList.toggle('wide', content.some((c) => c && c.classList && c.classList.contains('picker')));
  dlg.classList.toggle('own-footer', content.some((c) => c && c.classList && c.classList.contains('sheet')));
  if (!dlg.open) dlg.showModal();
  ($('info-body').querySelector('input') || $('info-x')).focus();
}
function closeModal() { const d = $('info-dialog'); if (d.open) d.close(); }
const withIcon = (name, text) => [icon(name), text];
// A warning or error card: the panel's neutral card with an icon in front, no colored edge.
const flag = (kind, attrs, ...kids) => h(attrs.tag || 'div', { ...attrs, tag: null, class: `note note-${kind}${attrs.class ? ` ${attrs.class}` : ''}` },
  h('span', { class: 'note-icon' }, icon(kind === 'error' ? 'alert-circle' : 'alert-triangle', 16)), h('div', { class: 'note-body' }, ...kids));
const detailsButton = (title, ...content) => h('button', { class: 'btn btn-small btn-text more-btn', type: 'button', onclick: () => openModal(title, ...content) }, icon('info-circle'), 'Details');
const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage full or blocked: settings just aren't kept */ } },
};
async function call(method, params) {
  const r = await window.ofk.call(method, params);
  if (!r.ok) throw Object.assign(new Error(r.error), { detail: r.detail });
  return r.result;
}
const basename = (p) => (p || '').split(/[\\/]/).pop();
const STATE_WORDS = { 'to convert': '', 'to clean': 'exported', cleaned: 'exported' };
// "frame-03" reads "Frame 3", "frame-06-rescan" reads "Frame 6, rescan". Other names stay as they are.
function nice(name) {
  const m = /^frame[-_ ]?0*(\d+)(?:[-_ ](.+))?$/i.exec(String(name || ''));
  return m ? `Frame ${m[1]}${m[2] ? `, ${m[2].replace(/[-_]+/g, ' ')}` : ''}` : name;
}
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// ---- the adjustments --------------------------------------------------------------------
// Named for what a person sees. Brightness is the kit's gamma, Color its saturation.
const SLIDERS = [
  { key: 'gamma', label: 'Brightness', min: 0.6, max: 1.8, step: 0.02, def: 1.0 },
  { key: 'contrast', label: 'Contrast', min: 0.7, max: 2.2, step: 0.02, def: 1.3 },
  { key: 'warmth', label: 'Warmth', min: -1, max: 1, step: 0.02, def: 0 },
  { key: 'saturation', label: 'Color', min: 0, max: 2, step: 0.02, def: 1.0 },
];
// The slider runs −100…+100 with 0 at the default, so every knob starts centered; each half maps to its own range.
const toPos = (sl, v) => Math.round((v >= sl.def ? (v - sl.def) / (sl.max - sl.def) : (v - sl.def) / (sl.def - sl.min)) * 100);
const toVal = (sl, pos) => +(pos >= 0 ? sl.def + (pos / 100) * (sl.max - sl.def) : sl.def + (pos / 100) * (sl.def - sl.min)).toFixed(4);
function fmt(n) { const r = Math.round(n); return r > 0 ? `+${r}` : r < 0 ? `−${-r}` : '0'; }
function defaults() { return Object.fromEntries(SLIDERS.map((s) => [s.key, s.def])); }   // turn: automatic until set

// ---- app state ----------------------------------------------------------------------
const state = {
  folder: null, places: [], current: null, stocks: [],
  view: 'sheet',                               // 'sheet' (contact sheet) or 'photo' (one picture, large)
  settings: store.get('ofk.settings', {}),   // per master path, kept across launches
  stockFor: store.get('ofk.stock', {}),       // per master path: a film choice made here
  thumbs: new Map(), previewSeq: 0, lastPreview: null, autoImage: null, comparing: false,
  error: null, saved: null, warnings: [], busy: false, film: null,
  changed: store.get('ofk.changed', {}),     // per master: adjusted after its picture was saved
};
function settingsFor(master) { return { ...defaults(), ...(state.settings[master] || {}) }; }
const allFrames = () => state.places.flatMap((p) => p.frames);
const keyOf = (f) => f.master || f.picture;
// What a frame's picture is now: not saved yet, saved, or saved but adjusted since.
function frameStatus(f) {
  if (!f.master) return 'saved';
  if (f.state === 'to convert') return 'new';
  return state.changed[f.master] ? 'changed' : 'saved';
}
const STATUS_WORD = { new: 'Not exported yet', saved: 'Exported', changed: 'Changed since exporting' };
// Undo: before each change, the frame's settings and film are remembered. A slider drag counts as one change.
const history = {};                       // per master: [{ settings, stock }], newest last
const future = {};                        // per master: what Undo took back, for Redo
let lastRemember = { master: null, key: null, at: 0 };
function remember(master, key) {
  const now = Date.now();
  if (key && lastRemember.master === master && lastRemember.key === key && now - lastRemember.at < 1200) { lastRemember.at = now; return; }
  lastRemember = { master, key, at: now };
  (history[master] = history[master] || []).push({ settings: state.settings[master] ? { ...state.settings[master] } : null, stock: state.stockFor[master] || null });
  if (history[master].length > 100) history[master].shift();
  future[master] = [];                    // a new change ends the redo trail
  renderUndo();
}
function applySnapshot(master, snap) {
  if (snap.settings) state.settings[master] = snap.settings; else delete state.settings[master];
  if (snap.stock) state.stockFor[master] = snap.stock; else delete state.stockFor[master];
  store.set('ofk.settings', state.settings); store.set('ofk.stock', state.stockFor);
  noteChange(master); state.saved = null; state.autoImage = null; state.film = null;
  renderInspector(); preview();
}
function undo() {
  const frame = state.current;
  const h0 = frame && frame.master && history[frame.master];
  if (!h0 || !h0.length) return;
  lastRemember = { master: null, key: null, at: 0 };
  (future[frame.master] = future[frame.master] || []).push(snapshotOf(frame.master));
  applySnapshot(frame.master, h0.pop());
  say(h0.length ? `Undone. ${plural(h0.length, 'more change', 'more changes')} to undo` : 'Undone. Back to how it started');
}
// A crop box is [x0, y0, x1, y1] in 0..1 fractions of the turned, mirrored, uncropped picture.
const roundBox = (b) => b.map((n) => Math.round(n * 10000) / 10000);
function turnBox(b, by) {
  const [x0, y0, x1, y1] = b;
  return roundBox(by > 0 ? [1 - y1, x0, 1 - y0, x1] : [y0, 1 - x1, y1, 1 - x0]);
}
function mirrorBox(b) { const [x0, y0, x1, y1] = b; return roundBox([1 - x1, y0, 1 - x0, y1]); }
function snapshotOf(master) { return { settings: state.settings[master] ? { ...state.settings[master] } : null, stock: state.stockFor[master] || null }; }
function redo() {
  const frame = state.current;
  const f0 = frame && frame.master && future[frame.master];
  if (!f0 || !f0.length) return;
  lastRemember = { master: null, key: null, at: 0 };
  (history[frame.master] = history[frame.master] || []).push(snapshotOf(frame.master));
  applySnapshot(frame.master, f0.pop());
  say(f0.length ? `Redone. ${plural(f0.length, 'more change', 'more changes')} to redo` : 'Redone');
}
function undoAll() {
  const frame = state.current;
  if (!frame || !frame.master) return;
  if (!state.settings[frame.master] && !state.stockFor[frame.master]) return;
  remember(frame.master, null);             // so Undo can bring it all back
  lastRemember = { master: null, key: null, at: 0 };
  applySnapshot(frame.master, { settings: null, stock: null });
  say('All changes undone. Undo brings them back');
}
function renderUndo() {
  const frame = state.current;
  const n = frame && frame.master && history[frame.master] ? history[frame.master].length : 0;
  const u = $('undo-btn'); const a = $('undo-all-btn'); const r = $('redo-btn');
  const nr = frame && frame.master && future[frame.master] ? future[frame.master].length : 0;
  if (r) { r.setAttribute('aria-disabled', nr ? 'false' : 'true'); r.title = nr ? `Redo (⇧⌘Z) · ${plural(nr, 'step', 'steps')}` : 'Nothing to redo'; }
  if (u) { u.setAttribute('aria-disabled', n ? 'false' : 'true'); u.title = n ? `Undo the last change (⌘Z) · ${plural(n, 'step', 'steps')}` : 'Nothing to undo'; }
  const any = frame && frame.master && (state.settings[frame.master] || state.stockFor[frame.master]);
  if (a) a.setAttribute('aria-disabled', any ? 'false' : 'true');
}

function noteChange(master) {
  const f = allFrames().find((x) => x.master === master);
  if (!f || f.state === 'to convert' || state.changed[master]) return;
  state.changed[master] = true; store.set('ofk.changed', state.changed); markCell(f);
}
function noteSaved(master) {
  if (state.changed[master]) { delete state.changed[master]; store.set('ofk.changed', state.changed); }
}
function markCell(f) {
  const cell = document.querySelector(`.cell[data-key="${CSS.escape(keyOf(f))}"]`);
  if (!cell) return;
  const st = frameStatus(f);
  cell.dataset.status = st;
  cell.setAttribute('aria-label', `${nice(f.name)}, ${STATUS_WORD[st].toLowerCase()}`);
  const word = cell.querySelector('.st'); if (word) word.textContent = st === 'new' ? '' : st === 'changed' ? 'changed' : 'exported';
  renderExportButton();
}

// ---- the contact sheet ------------------------------------------------------------------
function renderSheet() {
  const body = $('photo-body');
  body.replaceChildren();
  if (!state.folder) {
    body.append(h('div', { class: 'empty first' },
      h('h2', {}, 'Turn film into pictures'),
      h('ol', { class: 'steps' },
        h('li', {}, h('b', {}, 'Scan.'), ' Put a strip of film in the holder, slide it into the scanner, and scan each frame.'),
        h('li', {}, h('b', {}, 'Look them over.'), ' Each frame is turned upright and color-corrected for you. Fix any that look off.'),
        h('li', {}, h('b', {}, 'Export.'), ' Save every picture at once, ready to share or print.')),
      h('div', { class: 'row' },
        h('button', { class: 'btn btn-large btn-primary', type: 'button', onclick: openScan }, 'Start scanning'),
        h('button', { class: 'btn btn-large', type: 'button', onclick: openFolder }, 'Open scans I already have')),
      h('p', { class: 'hint' }, 'Your original scans are never changed.')));
    return;
  }
  if (!allFrames().length) {
    body.append(h('div', { class: 'empty' }, h('h2', {}, 'No scans here yet'),
      h('p', {}, 'Scan the film to fill this roll.'),
      h('button', { class: 'btn btn-large btn-primary', type: 'button', onclick: openScan }, 'Go to Scan')));
    return;
  }
  const several = state.places.filter((p) => p.frames.length).length > 1;
  for (const place of state.places) {
    if (!place.frames.length) continue;
    const list = h('ul', { class: 'sheet-grid', role: 'list' });
    for (const f of place.frames) {
      const key = keyOf(f);
      const current = state.current && keyOf(state.current) === key;
      const img = h('img', { alt: '', width: 150, height: 100 });
      const cached = state.thumbs.get(key);
      if (cached) img.src = cached; else queueThumb(f, img);
      const exporting = exp.running && exp.now === f.master;
      const st = frameStatus(f);
      list.append(h('li', {}, h('button', {
        class: 'cell', type: 'button', 'aria-current': current ? 'true' : 'false', 'data-key': key, 'data-status': st,
        'aria-label': `${nice(f.name)}, ${exporting ? 'exporting now' : STATUS_WORD[st].toLowerCase()}`,
        onclick: () => select(f), ondblclick: () => openLarge(f), onkeydown: sheetKeys,
      }, h('span', { class: 'pic' }, img, h('span', { class: 'badge', 'aria-hidden': 'true' }, icon('check'))),
      h('span', { class: 'meta' }, h('b', {}, nice(f.name)), ' ',   // the space: so the words seen read as the spoken label does
        exporting ? h('span', { class: 'saving' }, 'exporting…') : h('span', { class: 'st' }, st === 'new' ? '' : st === 'changed' ? 'changed' : 'exported')))));
    }
    body.append(h('section', { class: 'place' }, several || place.label ? h('h3', {}, place.label) : null, list));
  }
}
function sheetKeys(e) {
  const cells = [...document.querySelectorAll('.cell')];
  const i = cells.indexOf(e.currentTarget);
  if (e.key === 'Enter') { e.preventDefault(); const f = allFrames().find((x) => keyOf(x) === e.currentTarget.dataset.key); if (f) openLarge(f); return; }
  const cols = Math.max(1, getComputedStyle(e.currentTarget.closest('.sheet-grid')).gridTemplateColumns.split(' ').length);
  const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: cols, ArrowUp: -cols }[e.key];
  if (!step) return;
  e.preventDefault();
  const next = cells[i + step];
  if (next) { next.focus(); next.click(); }
}

// Thumbnails load one at a time in the background, so the sheet is usable at once.
const thumbQueue = [];
let thumbRunning = false;
function queueThumb(frame, img) { thumbQueue.push({ frame, img }); if (!thumbRunning) runThumbs(); }
async function runThumbs() {
  thumbRunning = true;
  while (thumbQueue.length) {
    const { frame, img } = thumbQueue.shift();
    const key = keyOf(frame);
    if (!state.thumbs.has(key)) {
      try {
        const src = frame.picture ? await call('thumb', { path: frame.picture, side: 360 })
          : await call('thumb', { path: frame.master, master: true, side: 360, settings: settingsFor(frame.master), stock: state.stockFor[frame.master] || null });
        state.thumbs.set(key, src);
      } catch { state.thumbs.set(key, ''); }
    }
    if (state.view === 'scan' && state.thumbs.get(key)) { renderScanStage(); continue; }
    if (img.isConnected && state.thumbs.get(key)) img.src = state.thumbs.get(key);
    else { const el = document.querySelector(`.cell[data-key="${CSS.escape(key)}"] img`); if (el && state.thumbs.get(key)) el.src = state.thumbs.get(key); }
  }
  thumbRunning = false;
}

// ---- one picture, large -----------------------------------------------------------------
function render() {
  $('layout').classList.toggle('queue-mode', state.view === 'queue');
  const tab = state.view === 'queue' ? 'queue' : state.view === 'scan' ? 'scan' : 'roll';
  for (const v of ['scan', 'roll', 'queue']) $(`tab-${v}`).setAttribute('aria-selected', String(tab === v));
  $('export-btn').classList.toggle('btn-primary', state.view !== 'scan');   // one main button at a time: on Scan it's Scan next frame
  if (state.view === 'scan') { renderScanStage(); renderScanPanel(); }
  else if (state.view === 'queue') renderQueue();
  else if (state.view === 'photo' && state.current) renderLarge();
  else renderSheet();
}
// Zoom in the large view: wheel or pinch zooms around the pointer, drag pans, double-click toggles
// between fit and 2x, + / − / 0 on the keyboard. Kept per frame while it stays open.
const zoom = { s: 1, x: 0, y: 0, key: null };
const ZMIN = 1, ZMAX = 8;
function resetZoom() { zoom.s = 1; zoom.x = 0; zoom.y = 0; }
function applyZoom() {
  const layer = document.querySelector('.zoom-layer');
  if (layer) layer.style.transform = `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`;
  scheduleSharp();
  const pic = document.querySelector('.big-pic'); if (pic) pic.classList.toggle('zoomed', zoom.s > 1);
  const pct = $('zoom-pct'); if (pct) pct.textContent = zoom.s === 1 ? 'Fit' : `${Math.round(zoom.s * 100)}%`;
  for (const [id, off] of [['zoom-out', zoom.s <= ZMIN], ['zoom-in', zoom.s >= ZMAX], ['zoom-fit', zoom.s === 1]]) { const b = $(id); if (b) b.setAttribute('aria-disabled', String(off)); }
}
// Zoom to `to`, keeping the point (px, py) — relative to the picture area's center — still under the pointer.
function zoomTo(to, px = 0, py = 0) {
  const s1 = Math.max(ZMIN, Math.min(ZMAX, to));
  if (s1 === 1) { resetZoom(); applyZoom(); return; }
  zoom.x = px - (px - zoom.x) * (s1 / zoom.s);
  zoom.y = py - (py - zoom.y) * (s1 / zoom.s);
  zoom.s = s1; clampPan(); applyZoom();
}
function clampPan() {
  const img = document.querySelector('.zoom-layer'); const pic = document.querySelector('.big-pic');
  if (!img || !pic) return;
  const mx = Math.max(0, (img.offsetWidth * zoom.s - pic.clientWidth) / 2 + 40);
  const my = Math.max(0, (img.offsetHeight * zoom.s - pic.clientHeight) / 2 + 40);
  zoom.x = Math.max(-mx, Math.min(mx, zoom.x)); zoom.y = Math.max(-my, Math.min(my, zoom.y));
}
// The preview, fitted into the picture area: the layer gets that exact size so a sharp patch can sit on it.
function fitLayer() {
  const pic = document.querySelector('.big-pic'); const layer = document.querySelector('.zoom-layer'); const img = layer && layer.querySelector('img.base');
  if (!pic || !img || !img.naturalWidth) return;
  const pad = 40, cw = pic.clientWidth - pad, ch = pic.clientHeight - pad;
  const k = Math.min(cw / img.naturalWidth, ch / img.naturalHeight);
  layer.style.width = `${Math.round(img.naturalWidth * k)}px`; layer.style.height = `${Math.round(img.naturalHeight * k)}px`;
}
window.addEventListener('resize', () => { if (state.view === 'photo') { fitLayer(); clampPan(); applyZoom(); } });

// Sharp zoom: past 1.5x, a moment after the view stops moving, the visible part is rendered from the full scan
// (preview_region) and laid over the preview. Only the newest request's answer is kept.
const sharp = { timer: null, seq: 0 };
function visibleBox() {
  const pic = document.querySelector('.big-pic'); const layer = document.querySelector('.zoom-layer');
  if (!pic || !layer || !layer.offsetWidth) return null;
  const w = layer.offsetWidth, h = layer.offsetHeight, cw = pic.clientWidth, ch = pic.clientHeight;
  const fx = (u) => Math.max(0, Math.min(1, (u / zoom.s - zoom.x / zoom.s + w / 2) / w));
  const fy = (v) => Math.max(0, Math.min(1, (v / zoom.s - zoom.y / zoom.s + h / 2) / h));
  const box = [fx(-cw / 2), fy(-ch / 2), fx(cw / 2), fy(ch / 2)].map((n) => Math.round(n * 10000) / 10000);
  return box[2] - box[0] > 0.001 && box[3] - box[1] > 0.001 ? box : null;
}
function scheduleSharp() {
  clearTimeout(sharp.timer);
  const layer = document.querySelector('.zoom-layer');
  if (!layer) return;
  if (zoom.s <= 1.5 || state.comparing) { sharp.seq++; layer.querySelector('img.sharp')?.remove(); return; }
  sharp.timer = setTimeout(fetchSharp, 300);
}
async function fetchSharp() {
  const frame = state.current; const box = visibleBox(); const pic = document.querySelector('.big-pic');
  if (!frame || !frame.master || !box || !pic) return;
  const seq = ++sharp.seq;
  const side = Math.min(4000, Math.round(Math.max(pic.clientWidth, pic.clientHeight) * (window.devicePixelRatio || 1)));
  try {
    const r = await call('preview_region', { master: frame.master, box, settings: settingsFor(frame.master), stock: state.stockFor[frame.master] || null, side });
    if (seq !== sharp.seq || state.current !== frame || state.view !== 'photo') return;
    const layer = document.querySelector('.zoom-layer'); if (!layer) return;
    const b = r.box || box;
    let patch = layer.querySelector('img.sharp');
    if (!patch) { patch = h('img', { class: 'sharp', alt: '', draggable: 'false' }); layer.append(patch); }
    patch.onload = () => { patch.style.opacity = '1'; };
    patch.style.opacity = '0';
    patch.style.left = `${b[0] * 100}%`; patch.style.top = `${b[1] * 100}%`;
    patch.style.width = `${(b[2] - b[0]) * 100}%`; patch.style.height = `${(b[3] - b[1]) * 100}%`;
    patch.src = r.image;
  } catch { /* the softer preview stays */ }
}

function pointer(e, pic) { const r = pic.getBoundingClientRect(); return [e.clientX - r.left - r.width / 2, e.clientY - r.top - r.height / 2]; }
function wireZoom(pic) {
  pic.addEventListener('wheel', (e) => {
    e.preventDefault();
    const [px, py] = pointer(e, pic);
    const k = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0025));   // pinch arrives as ctrl+wheel with small deltas
    zoomTo(zoom.s * k, px, py);
  }, { passive: false });
  let drag = null;
  pic.addEventListener('pointerdown', (e) => {
    if (zoom.s === 1 || e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, ox: zoom.x, oy: zoom.y }; pic.setPointerCapture(e.pointerId); pic.classList.add('dragging');
  });
  pic.addEventListener('pointermove', (e) => { if (!drag) return; zoom.x = drag.ox + e.clientX - drag.x; zoom.y = drag.oy + e.clientY - drag.y; clampPan(); applyZoom(); });
  const end = () => { drag = null; pic.classList.remove('dragging'); };
  pic.addEventListener('pointerup', end); pic.addEventListener('pointercancel', end);
  pic.addEventListener('dblclick', (e) => { const [px, py] = pointer(e, pic); zoomTo(zoom.s > 1 ? 1 : 2, px, py); });
}
document.addEventListener('keydown', (e) => {
  if (state.view !== 'photo' || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target.matches && e.target.matches('input, select, textarea')) return;
  if (document.querySelector('dialog[open]')) return;
  if (e.key === '+' || e.key === '=') { e.preventDefault(); zoomTo(zoom.s * 1.5); }
  else if (e.key === '-' || e.key === '_') { e.preventDefault(); zoomTo(zoom.s / 1.5); }
  else if (e.key === '0') { e.preventDefault(); zoomTo(1); }
});

// Crop mode: the whole scan with the crop drawn on it. Drag an edge or corner to change it; Done keeps it.
const crop = { on: false, image: null, auto: null, box: null, loading: false, error: null, seq: 0 };
async function startCrop() {
  const frame = state.current;
  if (!frame || !frame.master) return;
  resetZoom();
  Object.assign(crop, { on: true, image: null, auto: null, box: null, loading: true, error: null });
  const seq = ++crop.seq;
  renderLarge();
  try {
    const r = await call('preview', { master: frame.master, settings: { ...settingsFor(frame.master), uncropped: true }, stock: state.stockFor[frame.master] || null });
    if (seq !== crop.seq || !crop.on) return;
    if (!r.auto_crop) throw new Error('This version of the back end can’t show the uncropped scan yet.');
    const cur = settingsFor(frame.master).crop;
    crop.image = r.image; crop.auto = r.auto_crop;
    crop.box = Array.isArray(cur) ? [...cur] : cur === 'none' ? [0, 0, 1, 1] : [...(r.crop_used || r.auto_crop)];
  } catch (e) { if (seq === crop.seq) crop.error = e.message; }
  crop.loading = false;
  if (seq === crop.seq) renderLarge();
}
function endCrop(apply) {
  const frame = state.current;
  if (apply && frame && frame.master && crop.box) {
    const b = crop.box.map((n) => Math.round(n * 10000) / 10000);
    const same = (a, c) => a && c && a.every((n, i) => Math.abs(n - c[i]) < 0.002);
    const value = b[0] <= 0.001 && b[1] <= 0.001 && b[2] >= 0.999 && b[3] >= 0.999 ? 'none' : same(b, crop.auto) ? null : b;
    const cur = settingsFor(frame.master).crop ?? null;
    if (JSON.stringify(value) !== JSON.stringify(cur)) {
      remember(frame.master, null);
      const next = { ...settingsFor(frame.master) };
      if (value === null) delete next.crop; else next.crop = value;
      state.settings[frame.master] = next;
      store.set('ofk.settings', state.settings); noteChange(frame.master); state.saved = null; state.autoImage = null;
      say(value === null ? 'Automatic crop' : value === 'none' ? 'No crop' : 'Crop changed');
      state.lastPreview = null;
      preview();
    }
  }
  crop.on = false; crop.seq++;
  renderLarge(); renderInspector();
}
function renderCropEditor(pic) {
  if (crop.loading) { pic.append(h('p', { class: 'loading', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Showing the whole scan…')); return; }
  if (crop.error) { pic.append(h('p', { class: 'loading', role: 'alert' }, crop.error)); return; }
  const layer = h('div', { class: 'zoom-layer crop-layer' },
    h('img', { class: 'base', src: crop.image, alt: `${nice(state.current.name)}, the whole scan`, draggable: 'false', onload: () => { fitLayer(); drawCropBox(); } }),
    h('div', { class: 'shade shade-t' }), h('div', { class: 'shade shade-b' }), h('div', { class: 'shade shade-l' }), h('div', { class: 'shade shade-r' }),
    crop.auto ? h('div', { class: 'auto-mark', title: 'Where the automatic crop cuts' }) : null,
    h('div', { class: 'crop-box', tabindex: '0', role: 'group', 'aria-label': 'Crop. Drag an edge or corner; arrow keys move the nearest edge.' },
      ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'].map((k) => h('span', { class: `handle h-${k}`, 'data-edge': k }))));
  pic.append(layer);
  wireCrop(layer);
}
function drawCropBox() {
  const layer = document.querySelector('.crop-layer'); if (!layer || !crop.box) return;
  const [x0, y0, x1, y1] = crop.box.map((n) => n * 100);
  const set = (sel, css) => Object.assign(layer.querySelector(sel).style, css);
  set('.crop-box', { left: `${x0}%`, top: `${y0}%`, width: `${x1 - x0}%`, height: `${y1 - y0}%` });
  set('.shade-t', { left: '0', top: '0', width: '100%', height: `${y0}%` });
  set('.shade-b', { left: '0', top: `${y1}%`, width: '100%', height: `${100 - y1}%` });
  set('.shade-l', { left: '0', top: `${y0}%`, width: `${x0}%`, height: `${y1 - y0}%` });
  set('.shade-r', { left: `${x1}%`, top: `${y0}%`, width: `${100 - x1}%`, height: `${y1 - y0}%` });
  if (crop.auto) { const [a0, b0, a1, b1] = crop.auto.map((n) => n * 100); set('.auto-mark', { left: `${a0}%`, top: `${b0}%`, width: `${a1 - a0}%`, height: `${b1 - b0}%` }); }
  const lbl = $('crop-size');
  if (lbl) { const keep = Math.round((crop.box[2] - crop.box[0]) * (crop.box[3] - crop.box[1]) * 100); lbl.textContent = `Keeping ${keep}% of the scan`; }
}
function wireCrop(layer) {
  const MIN = 0.05;
  let drag = null;
  const move = (edge, dx, dy, from) => {
    let [x0, y0, x1, y1] = from;
    if (edge === 'move') { const w = x1 - x0, hh = y1 - y0; x0 = Math.max(0, Math.min(1 - w, x0 + dx)); y0 = Math.max(0, Math.min(1 - hh, y0 + dy)); return [x0, y0, x0 + w, y0 + hh]; }
    if (edge.includes('w')) x0 = Math.max(0, Math.min(x1 - MIN, x0 + dx));
    if (edge.includes('e')) x1 = Math.min(1, Math.max(x0 + MIN, x1 + dx));
    if (edge.includes('n')) y0 = Math.max(0, Math.min(y1 - MIN, y0 + dy));
    if (edge.includes('s')) y1 = Math.min(1, Math.max(y0 + MIN, y1 + dy));
    return [x0, y0, x1, y1];
  };
  layer.addEventListener('pointerdown', (e) => {
    const edge = e.target.dataset.edge || (e.target.classList.contains('crop-box') ? 'move' : null);
    if (!edge) return;
    e.preventDefault(); e.stopPropagation();
    drag = { edge, x: e.clientX, y: e.clientY, from: [...crop.box] };
    layer.setPointerCapture(e.pointerId);
  });
  layer.addEventListener('pointermove', (e) => {
    if (!drag) return;
    crop.box = move(drag.edge, (e.clientX - drag.x) / layer.offsetWidth, (e.clientY - drag.y) / layer.offsetHeight, drag.from);
    drawCropBox();
  });
  const end = () => { drag = null; };
  layer.addEventListener('pointerup', end); layer.addEventListener('pointercancel', end);
  // Keyboard: arrows nudge the whole box; with Shift they grow it, with Alt they shrink it.
  layer.querySelector('.crop-box').addEventListener('keydown', (e) => {
    const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    if (!d) return;
    e.preventDefault();
    const step = 0.005;
    const [dx, dy] = d;
    if (e.shiftKey) crop.box = move(dx < 0 ? 'w' : dx > 0 ? 'e' : dy < 0 ? 'n' : 's', dx * step, dy * step, crop.box);
    else if (e.altKey) crop.box = move(dx < 0 ? 'e' : dx > 0 ? 'w' : dy < 0 ? 's' : 'n', dx * step, dy * step, crop.box);
    else crop.box = move('move', dx * step, dy * step, crop.box);
    drawCropBox();
  });
}

function renderLarge() {
  if (crop.on) return renderCropMode();
  const body = $('photo-body');
  const area = $('photo-area');
  body.replaceChildren();
  area.setAttribute('aria-busy', state.busy ? 'true' : 'false');
  if (zoom.key !== state.current.master) { resetZoom(); zoom.key = state.current.master; }
  const src = state.comparing && state.autoImage ? state.autoImage : state.lastPreview;
  const pic = h('div', { class: 'big-pic', title: 'Scroll or pinch to zoom · drag to move · double-click for 2×' },
    src ? h('div', { class: 'zoom-layer' }, h('img', { class: 'base', src, alt: `${nice(state.current.name)}${state.comparing ? ', before adjustments' : ''}`, draggable: 'false', onload: () => { fitLayer(); clampPan(); applyZoom(); } }))
      : h('p', { class: 'loading', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Developing the picture…'),
    state.comparing ? h('span', { class: 'photo-tag' }, 'Before: no adjustments') : null);
  if (src) wireZoom(pic);
  body.append(h('div', { class: 'big' },
    h('div', { class: 'big-bar' },
      h('button', { class: 'btn btn-small', type: 'button', title: 'Back to the roll (Esc)', onclick: backToSheet }, 'Back to roll'),
      h('span', { class: 'name' }, nice(state.current.name)),
      h('span', { class: 'hint' }, STATUS_WORD[frameStatus(state.current)]),
      h('span', { class: 'sp' }),
      h('div', { class: 'zoom-ctl', role: 'group', 'aria-label': 'Zoom' },
        h('button', { class: 'btn btn-small icon-only', type: 'button', id: 'zoom-out', 'aria-label': 'Zoom out', title: 'Zoom out (−)', onclick: () => zoomTo(zoom.s / 1.5) }, icon('zoom-out')),
        h('button', { class: 'btn btn-small zoom-pct', type: 'button', id: 'zoom-fit', title: 'Fit the whole picture (0)', onclick: () => zoomTo(1) }, h('span', { id: 'zoom-pct' }, 'Fit')),
        h('button', { class: 'btn btn-small icon-only', type: 'button', id: 'zoom-in', 'aria-label': 'Zoom in', title: 'Zoom in (+)', onclick: () => zoomTo(zoom.s * 1.5) }, icon('zoom-in'))),
      state.current.master ? h('button', { class: 'btn btn-small', type: 'button', title: 'Change how much is cropped', onclick: startCrop }, ...withIcon('crop', 'Crop')) : null),
    pic));
  applyZoom();
  if (src) showDeveloping();
}
function renderCropMode() {
  const body = $('photo-body');
  const pic = h('div', { class: 'big-pic cropping' });
  renderCropEditor(pic);
  const ready = !crop.loading && !crop.error;
  body.replaceChildren(h('div', { class: 'big' },
    h('div', { class: 'big-bar' },
      h('span', { class: 'name' }, 'Crop'), h('span', { class: 'hint', id: 'crop-size' }, ''),
      h('span', { class: 'sp' }),
      ready ? h('button', { class: 'btn btn-small btn-text', type: 'button', title: 'Use the automatic crop', onclick: () => { crop.box = [...crop.auto]; drawCropBox(); } }, 'Back to automatic') : null,
      ready ? h('button', { class: 'btn btn-small btn-text', type: 'button', title: 'Keep the whole scan, holder edge and all', onclick: () => { crop.box = [0, 0, 1, 1]; drawCropBox(); } }, 'Don’t crop') : null,
      h('button', { class: 'btn btn-small', type: 'button', onclick: () => endCrop(false) }, 'Cancel'),
      ready ? h('button', { class: 'btn btn-small btn-primary', type: 'button', onclick: () => endCrop(true) }, ...withIcon('check', 'Done')) : null),
    pic));
  drawCropBox();
}
function openLarge(f) {
  if (state.current !== f) select(f);
  state.view = 'photo'; render(); renderInspector();
  $('photo-area').focus();
}
function backToSheet() {
  state.view = 'sheet'; state.comparing = false; render(); renderInspector();
  document.querySelector('.cell[aria-current="true"]')?.focus();
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && state.view === 'photo' && !document.querySelector('dialog[open]')) { e.preventDefault(); if (crop.on) endCrop(false); else backToSheet(); }
});

async function select(frame) {
  if (crop.on) { crop.on = false; crop.seq++; }
  state.current = frame;
  state.lastPreview = null; state.autoImage = null; state.error = null; state.saved = null; state.warnings = []; state.film = null;
  store.set('ofk.lastFrame', keyOf(frame));
  render(); renderInspector();
  if (frame.master) {
    await preview();
  } else {
    state.busy = true; render();
    try { state.lastPreview = await call('thumb', { path: frame.picture, side: 1600 }); }
    catch (e) { state.error = e; }
    state.busy = false; render(); renderInspector();
  }
  document.querySelector('.cell[aria-current="true"]')?.scrollIntoView({ block: 'nearest' });
}

// Only the newest preview request counts: a slider drag fires many, and older answers are dropped.
let previewTimer = null;
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(preview, 40); }
async function preview() {
  const frame = state.current;
  if (!frame || !frame.master) return;
  const seq = ++state.previewSeq;
  state.developing = true; state.developStart = Date.now(); showDeveloping(); watchDeveloping(seq);
  const loadingTimer = setTimeout(() => { if (seq === state.previewSeq && !state.lastPreview) { state.busy = true; render(); } }, 300);
  try {
    const r = await call('preview', { master: frame.master, settings: settingsFor(frame.master), stock: state.stockFor[frame.master] || null });
    if (seq !== state.previewSeq || state.current !== frame) return;
    const filmChanged = !state.film || JSON.stringify([state.film.film_card, state.film.scan_card]) !== JSON.stringify([r.film_card, r.scan_card]);
    state.lastPreview = r.image; state.warnings = r.warnings || []; state.film = r; state.error = null;
    state.thumbs.set(frame.master, r.image);
    const cellImg = document.querySelector(`.cell[data-key="${CSS.escape(frame.master)}"] img`);
    if (cellImg) cellImg.src = r.image;
    if (filmChanged) renderFilm();
  } catch (e) {
    if (seq !== state.previewSeq) return;
    state.error = e;
  } finally {
    clearTimeout(loadingTimer);
  }
  if (seq === state.previewSeq) state.developing = false;
  state.busy = false;
  if (state.view === 'photo') renderLarge();
  renderMini(); renderInspectorNotes(); showDeveloping();
}

async function toggleCompare(on) {
  const frame = state.current;
  if (!frame || !frame.master) return;
  state.comparing = on;
  if (on && !state.autoImage) {
    try { state.autoImage = (await call('preview', { master: frame.master, settings: { ...defaults(), auto: false, rotate: state.film ? state.film.rotate : 0 }, stock: state.stockFor[frame.master] || null })).image; }
    catch { state.comparing = false; }
  }
  if (state.view === 'photo') renderLarge();
  renderMini();
}

// ---- the inspector ------------------------------------------------------------------
function renderInspector() {
  if (state.view === 'scan') { renderScanPanel(); return; }
  const body = $('inspector-body');
  body.replaceChildren();
  const frame = state.current;
  if (!frame) { body.append(h('p', { class: 'panel-empty' }, 'Choose a frame to adjust it.')); return; }
  body.append(h('h3', {}, nice(frame.name)));
  if (state.view === 'sheet') body.append(h('div', { id: 'mini' }));
  if (!frame.master) {
    body.append(h('p', { class: 'hint' }, `${basename(frame.picture)} is a finished picture. Its scan isn't in this folder, so it can't be adjusted here.`),
      h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onclick: () => window.ofk.showInFolder(frame.picture) }, 'Show in folder')));
    body.append(h('div', { id: 'notes' }));
    renderMini(); renderInspectorNotes();
    return;
  }
  const s = settingsFor(frame.master);

  body.append(h('h3', {}, 'Film and scan'), h('div', { id: 'film' }), h('div', { id: 'scan', class: 'scan-slot' }));

  // Turn the picture as the person sees it. The back end turns first and mirrors after, so when the picture
  // is mirrored a visual quarter turn right is a quarter turn left underneath. A manual crop box turns with it.
  const turn = (by) => {
    remember(frame.master, null);
    const cur = settingsFor(frame.master);
    const now = cur.rotate ?? (state.film ? state.film.rotate : 0);
    const r = (((now || 0) + (cur.mirror ? -by : by)) % 360 + 360) % 360;
    state.settings[frame.master] = { ...cur, rotate: r, ...(Array.isArray(cur.crop) ? { crop: turnBox(cur.crop, by) } : {}) };
    store.set('ofk.settings', state.settings); noteChange(frame.master); state.saved = null; state.autoImage = null;
    say(by > 0 ? 'Turned right' : 'Turned left'); preview();
  };
  const mirror = () => {
    remember(frame.master, null);
    const cur = settingsFor(frame.master);
    const m = !cur.mirror;
    state.settings[frame.master] = { ...cur, mirror: m, ...(Array.isArray(cur.crop) ? { crop: mirrorBox(cur.crop) } : {}) };
    store.set('ofk.settings', state.settings); noteChange(frame.master); state.saved = null; state.autoImage = null;
    say(m ? 'Mirrored' : 'Not mirrored'); renderInspector(); preview();
  };
  body.append(h('h3', {}, 'Adjust'),
    h('div', { class: 'field inline' }, h('span', { class: 'label', id: 'turn-label' }, 'Turn'),
      h('div', { class: 'row', role: 'group', 'aria-labelledby': 'turn-label' },
        h('button', { class: 'btn btn-small', type: 'button', onclick: () => turn(-90) }, ...withIcon('rotate', 'Left')),
        h('button', { class: 'btn btn-small', type: 'button', onclick: () => turn(90) }, ...withIcon('rotate-clockwise', 'Right')),
        h('button', { class: 'btn btn-small', type: 'button', 'aria-pressed': String(!!s.mirror), title: 'Flip left to right', onclick: mirror }, ...withIcon('flip-horizontal', 'Mirror')))));

  for (const sl of SLIDERS) {
    const id = `sl-${sl.key}`;
    const value = h('output', { class: 'slider-value', for: id, id: `${id}-v` }, fmt(toPos(sl, s[sl.key])));
    const input = h('input', { type: 'range', id, min: -100, max: 100, step: 1, value: toPos(sl, s[sl.key]),
      'aria-valuetext': fmt(toPos(sl, s[sl.key])),
      oninput: (e) => {
        remember(frame.master, sl.key);
        const v = toVal(sl, parseFloat(e.target.value));
        state.settings[frame.master] = { ...settingsFor(frame.master), [sl.key]: v };
        store.set('ofk.settings', state.settings); noteChange(frame.master);
        value.textContent = fmt(toPos(sl, v)); e.target.setAttribute('aria-valuetext', fmt(toPos(sl, v)));
        state.saved = null; schedulePreview();
      },
      ondblclick: (e) => { e.target.value = 0; e.target.dispatchEvent(new Event('input')); } });
    body.append(h('div', { class: 'slider-line' }, h('label', { for: id }, sl.label), input, value));
  }

  // Dust: an adjustment like the others, kept per frame; the preview shows it and Save makes it.
  body.append(h('div', { class: 'field check' },
    h('input', { type: 'checkbox', id: 'dust', checked: !!s.dust, 'aria-describedby': 'dust-hint',
      onchange: (e) => {
        remember(frame.master, null);
        state.settings[frame.master] = { ...settingsFor(frame.master), dust: e.target.checked };
        store.set('ofk.settings', state.settings); noteChange(frame.master); state.saved = null;
        say(e.target.checked ? 'Removing dust' : 'Dust left as scanned'); preview();
      } }),
    h('label', { for: 'dust', title: 'Fills in specks and hairs. Faces and the date stamp are left alone.' }, 'Remove dust'),
    h('span', { class: 'visually-hidden', id: 'dust-hint' }, 'Fills in specks and hairs. Faces and the date stamp are left alone.')));

  const compare = h('button', { class: 'btn', type: 'button', 'aria-pressed': 'false',
    onpointerdown: () => toggleCompare(true), onpointerup: () => toggleCompare(false), onpointerleave: () => state.comparing && toggleCompare(false),
    onkeydown: (e) => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); toggleCompare(true); } },
    onkeyup: (e) => { if (e.key === ' ' || e.key === 'Enter') toggleCompare(false); } }, 'Hold to see before');
  body.append(h('div', { id: 'notes' }), h('div', { class: 'actions pinned' },
    h('button', { class: 'btn', type: 'button', id: 'save-btn', onclick: () => save(false) }, 'Export this picture'),
    h('div', { class: 'undo-row' },
      h('button', { class: 'btn', type: 'button', id: 'undo-btn', onclick: undo }, ...withIcon('arrow-back-up', 'Undo')),
      h('button', { class: 'btn', type: 'button', id: 'redo-btn', onclick: redo }, ...withIcon('arrow-forward-up', 'Redo')),
      h('button', { class: 'btn', type: 'button', id: 'undo-all-btn', onclick: undoAll }, ...withIcon('arrow-back-up-double', 'Undo all'))),
    h('div', { class: 'compare-row' }, compare,
      h('button', { class: 'btn', type: 'button', title: 'Export a 16-bit TIFF for editing in another program', onclick: () => save(true) }, '16-bit TIFF'))));
  renderMini(); renderFilm(); renderInspectorNotes(); renderUndo();
}

// After 5 seconds of developing, a progress bar: real progress when the back end reports it
// (preview-progress events), otherwise elapsed time and a moving stripe, never a made-up percentage.
const develop = { fraction: null, step: '', secs: null };
// "About 40 seconds left" / "About 3 minutes left": 10-second steps under a minute, whole minutes above.
function timeLeft(secs) {
  if (typeof secs !== 'number' || !isFinite(secs)) return null;
  if (secs < 60) return `about ${Math.max(10, Math.ceil(secs / 10) * 10)} seconds left`;
  const m = Math.round(secs / 60);
  return `about ${m} ${m === 1 ? 'minute' : 'minutes'} left`;
}
const cap = (t) => t ? t[0].toUpperCase() + t.slice(1) : t;
function watchDeveloping(seq) {
  develop.fraction = null; develop.step = ''; develop.secs = null;
  const tick = () => {
    if (!state.developing || seq !== state.previewSeq) { document.querySelectorAll('.develop-bar').forEach((b) => b.remove()); return; }
    if (Date.now() - state.developStart >= 5000) drawDevelopBar();
    setTimeout(tick, 500);
  };
  setTimeout(tick, 500);
}
function drawDevelopBar() {
  const left = timeLeft(develop.secs);
  const long = /measuring/i.test(develop.step);
  const label = [develop.step || 'Developing', left ? cap(left) : long ? 'Estimating…' : null].filter(Boolean).join(' · ');
  for (const host of document.querySelectorAll('.big-pic, .mini')) {
    let bar = host.querySelector('.develop-bar');
    if (!bar) {
      bar = h('div', { class: 'develop-bar', role: 'progressbar', 'aria-label': 'Developing the picture' }, h('div', { class: 'track' }, h('i')), h('span', { class: 'txt' }));
      host.append(bar);
      host.querySelector('.developing')?.remove();
    }
    const known = typeof develop.fraction === 'number';
    bar.classList.toggle('indeterminate', !known);
    if (known) { bar.setAttribute('aria-valuemin', 0); bar.setAttribute('aria-valuemax', 100); bar.setAttribute('aria-valuenow', Math.round(develop.fraction * 100)); bar.querySelector('i').style.width = `${Math.round(develop.fraction * 100)}%`; }
    else bar.removeAttribute('aria-valuenow');
    bar.setAttribute('aria-valuetext', label);
    bar.querySelector('.txt').textContent = label;
  }
}

// A spinner over the picture (large and small) while a preview is being made.
function showDeveloping() {
  for (const host of document.querySelectorAll('.big-pic, .mini')) {
    let sp = host.querySelector('.developing');
    if (state.developing && !sp) host.append(h('span', { class: 'developing', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Developing…'));
    else if (!state.developing && sp) sp.remove();
    if (state.developing && host.querySelector('.develop-bar')) host.querySelector('.developing')?.remove();
  }
}
function renderMini() {
  const box = $('mini');
  if (!box) return;
  const src = state.comparing && state.autoImage ? state.autoImage : state.lastPreview;
  box.replaceChildren(h('button', { class: 'mini', type: 'button', title: 'Open large', 'aria-label': `Open ${nice(state.current.name)} large`,
    onclick: () => openLarge(state.current) }, src ? h('img', { src, alt: '' }) : null));
  if (src) showDeveloping();
  else box.firstChild.append(h('span', { class: 'developing center', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Developing…'));
}

// The Film card: the canister in the stock's colors, its name and speed, the edge print as read off the
// film, and (folded away) the choice of a different film. The Scan card: what the scan is, and anything
// worth knowing about it. Both come from the back end (film_card and scan_card).
function renderFilm() {
  const box = $('film');
  const frame = state.current;
  if (!box || !frame || !frame.master) return;
  const fc = (state.film && state.film.film_card) || null;
  const sc = (state.film && state.film.scan_card) || null;
  if (!fc) {
    box.replaceChildren(h('div', { class: 'film loading-card', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Reading the film…'));
    const scanBox = $('scan');
    if (scanBox) scanBox.replaceChildren(h('div', { class: 'scan-card loading-card', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Checking the scan…'));
    return;
  }
  const id = fc ? fc.stock_id : (state.stockFor[frame.master] || 'unknown');
  const known = fc ? fc.maker !== 'unknown' && !/^unknown/.test(id) : false;
  const name = fc && known ? fc.name : 'Unknown film';
  const kind = fc ? fc.kind : 'Film';
  const can = fromSvg(window.canisters.svg(id, name, fc ? fc.maker : null, fc ? fc.iso : null));
  const n = sc && sc.frame ? sc.frame : null;
  const num = n ? parseInt(n, 10) : NaN;
  const frames = n ? `▸ ${n}${/A$/.test(n) ? '' : `  ${n}A`}${Number.isNaN(num) ? '' : `  ▸ ${num + 1}`}` : '';
  const edgeText = fc && fc.edge_print ? `${fc.edge_print}  ${frames}` : '';
  const pick = (v) => {
    remember(frame.master, null);
    if (v) state.stockFor[frame.master] = v; else delete state.stockFor[frame.master];
    store.set('ofk.stock', state.stockFor); noteChange(frame.master); state.autoImage = null; state.film = null; closeModal();
    say(v ? `Film set to ${(state.stocks.find((x) => x.id === v) || {}).name || v}` : 'Film from the roll'); renderFilm(); preview();
  };
  const sel = filmPicker(frame, pick);
  const choose = () => openModal('Choose the film', h('p', { class: 'hint' }, 'The name is printed along the film edge. Unknown works fine.'), sel);
  box.replaceChildren(...[
    h('button', { class: 'film film-btn', type: 'button', title: 'Choose a different film', onclick: choose }, can,   // named by what it shows, so Voice Control's "Change" finds it
      h('span', { class: 'film-text' }, h('span', { class: 'n' }, name),
        h('span', { class: 't' }, fc && fc.iso ? `${kind} · ISO ${fc.iso}` : `${kind} · worked out from the scan`)),
      h('span', { class: 'change' }, 'Change')),
    edgeText ? h('div', { class: 'edge', 'aria-label': `Edge print: ${fc.edge_print}` }, edgeText) : null,
  ].filter(Boolean));

  const scanBox = $('scan');
  if (!scanBox) return;
  scanBox.replaceChildren();
  if (!sc) return;
  const facts = [sc.dpi ? `${sc.dpi} dpi` : null, sc.width && sc.height ? `${sc.width} × ${sc.height}` : null, sc.scanner].filter(Boolean);
  const problems = sc.problems && sc.problems.length;
  if (problems) {
    scanBox.append(h('div', { class: 'scan-card has-problems' },
      h('span', { class: 'note-icon' }, icon('alert-triangle', 16)),
      h('div', { class: 'note-body' }, h('div', { class: 'sum' }, sc.summary), h('ul', {}, sc.problems.map((p) => h('li', {}, p))),
        facts.length ? h('div', { class: 't' }, facts.join(' · ')) : null)));
  } else {
    scanBox.append(h('div', { class: 'scan-card folded' }, h('span', { class: 'ok' }, icon('check')), h('span', { class: 'sum' }, sc.summary),
      facts.length ? detailsButton('About this scan', h('dl', { class: 'facts' },
        sc.dpi ? [h('dt', {}, 'Detail'), h('dd', {}, `${sc.dpi} dpi`)] : null,
        sc.width && sc.height ? [h('dt', {}, 'Size'), h('dd', {}, `${sc.width} × ${sc.height} pixels`)] : null,
        sc.scanner ? [h('dt', {}, 'Scanner'), h('dd', {}, sc.scanner)] : null,
        sc.captured_utc ? [h('dt', {}, 'Scanned'), h('dd', {}, when(sc.captured_utc))] : null,
        sc.frame ? [h('dt', {}, 'Frame on the film'), h('dd', {}, sc.frame)] : null)) : null));
  }
}

// The film picker: a search box over every stock, each drawn as its canister. Built lazily, a page at a time.
function filmPicker(frame, pick) {
  const chosen = state.stockFor[frame.master] || '';
  const list = h('ul', { class: 'stock-list', role: 'listbox', 'aria-label': 'Films' });
  const item = (id, name, sub, maker, iso) => h('li', {}, h('button', {
    class: 'stock', type: 'button', role: 'option', 'aria-selected': String(chosen === id), onclick: () => pick(id),
  }, fromSvg(window.canisters.svg(id || 'unknown', name, maker, iso)), h('span', {}, h('b', {}, name), h('br'), h('span', { class: 'hint' }, sub))));
  const draw = (q) => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = state.stocks.filter((st) => st.maker !== 'unknown' && words.every((w) => `${st.name} ${st.maker} ${st.iso || ''}`.toLowerCase().includes(w)));
    list.replaceChildren(...[
      words.length ? null : item('', 'From the roll', 'Whatever the roll says, or worked out from the scan', 'unknown', null),
      hits.slice(0, 60).map((st) => item(st.id, st.name, [st.kind, st.iso ? `ISO ${st.iso}` : null].filter(Boolean).join(' · '), st.maker, st.iso)),
      hits.length > 60 ? h('li', { class: 'hint more' }, `${hits.length - 60} more: type more of the name`) : null,
      !hits.length && words.length ? h('li', { class: 'hint more' }, 'No film by that name. Unknown works fine.') : null,
    ].flat().filter(Boolean));
  };
  const search = h('input', { type: 'text', placeholder: 'Search films (Gold, Superia, 400…)', 'aria-label': 'Search films',
    oninput: (e) => draw(e.target.value) });
  draw('');
  return h('div', { class: 'picker' }, search, list);
}

function renderInspectorNotes() {
  const notes = $('notes');
  if (!notes) return;
  notes.replaceChildren();
  if (state.saved) {
    notes.append(h('div', { class: 'note' }, h('strong', {}, `Exported ${basename(state.saved)}`),
      h('button', { class: 'btn btn-small', type: 'button', onclick: () => window.ofk.showInFolder(state.saved) }, 'Show in folder')));
  }
  if (state.error) {
    notes.append(flag('error', { role: 'alert' }, h('strong', {}, 'That didn’t work'), state.error.message,
      state.error.detail ? detailsButton('What went wrong', h('pre', { class: 'detail-text' }, state.error.detail)) : null));
  }
  if (state.warnings && state.warnings.length && !(state.film && state.film.scan_card)) {
    notes.append(flag('warn', {}, h('strong', {}, 'About this scan'),
      h('ul', {}, state.warnings.map((w) => h('li', {}, w[0].toUpperCase() + w.slice(1))))));
  }
}

async function save(tiff) {
  const frame = state.current;
  if (!frame || !frame.master) return;
  const btn = $('save-btn');
  if (btn) { btn.setAttribute('aria-disabled', 'true'); btn.textContent = 'Exporting…'; }
  say('Exporting the picture');
  try {
    const r = await call('save', { master: frame.master, settings: settingsFor(frame.master), stock: state.stockFor[frame.master] || null, tiff });
    state.saved = r.output; state.error = null; noteSaved(frame.master);
    say(`Saved ${basename(r.output)}`);
    await refresh(true);
  } catch (e) {
    state.error = e;
    say('The picture could not be exported');
  }
  renderInspector();
}

// ---- export: every frame in the folder, in the background --------------------------------
const exp = { running: false, index: 0, total: 0, now: null, result: null, failures: [] };
function exportable() { return allFrames().filter((f) => f.master); }
function renderExportButton() {
  const btn = $('export-btn');
  const frames = exportable();
  btn.hidden = !frames.length;
  if (exp.running) { btn.textContent = 'Exporting…'; btn.setAttribute('aria-disabled', 'true'); return; }
  btn.removeAttribute('aria-disabled');
  btn.textContent = 'Export…';       // it opens the sheet, whose button carries the count
  btn.removeAttribute('title');
}
// The Export sheet: where the pictures go (a stick, a card, a folder) and what kind, before anything starts.
const sheet = { places: null, loading: false, error: null, choice: null, tiff: false, chosen: null };
function gb(bytes) {
  if (typeof bytes !== 'number') return '';
  if (bytes >= 1e12) return `${(bytes / 1e12).toFixed(1)} TB free`;
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB free`;
  return `${Math.max(0, Math.round(bytes / 1e6))} MB free`;
}
// What places() returned: the chosen and last-used folders first, and only while they still exist.
function sheetPlaces() { return (sheet.places || []).slice(); }
async function fetchPlaces() {
  const last = store.get('ofk.exportFolder', null);
  const also = [sheet.chosen && sheet.chosen.path, last && last.path].filter(Boolean);
  try { sheet.places = (await call('places', { also: [...new Set(also)] })).places || []; sheet.error = null; }
  catch (e) { sheet.places = []; sheet.error = e.message; }
}
async function openExportSheet() {
  if (exp.running || !exportable().length) return;
  sheet.error = null;
  if (sheet.loading) return;
  // Sticks and cards come and go while the app is open, so look again every time.
  sheet.loading = true; if (!sheet.places) renderExportSheet();
  await fetchPlaces();
  sheet.loading = false;
  const list = sheetPlaces();
  if (!sheet.choice || !list.some((p) => p.path === sheet.choice)) sheet.choice = list[0] ? list[0].path : null;   // a gone drive isn't returned, so this falls back
  renderExportSheet();
}
function renderExportSheet() {
  const n = exportable().length;
  const list = sheetPlaces();
  const place = list.find((p) => p.path === sheet.choice) || null;
  const need = n * (sheet.tiff ? 150e6 : 8e6);
  const tight = place && typeof place.free === 'number' && place.free < need;
  const card = (p) => h('label', { class: 'opt place' },
    h('input', { type: 'radio', name: 'where', value: p.path, checked: p.path === sheet.choice, onchange: () => { sheet.choice = p.path; renderExportSheet(); } }),
    h('span', {}, h('b', {}, p.name || basename(p.path)), h('br'),
      h('span', { class: 'hint' }, [p.removable ? 'Removable' : null, gb(p.free)].filter(Boolean).join(' · ') || p.path)));
  const body = h('div', { class: 'sheet' },
    h('h3', {}, 'Where'),
    sheet.loading ? h('p', { class: 'loading', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Looking for drives…')
      : h('div', { class: 'detail', role: 'radiogroup', 'aria-label': 'Where' }, list.map(card)),
    sheet.error ? flag('error', { role: 'alert' }, sheet.error) : null,
    h('button', { class: 'btn btn-small', type: 'button', onclick: async () => {
      const path = await window.ofk.pickFolder('Where should the pictures go?');
      if (!path) return;
      sheet.chosen = { path };
      await fetchPlaces();
      if (!sheet.places.some((p) => p.path === path)) sheet.places.unshift({ path, name: basename(path), removable: false, free: null });
      sheet.choice = path; renderExportSheet();
    } }, ...withIcon('folder-open', 'Choose a folder…')),
    h('h3', {}, 'Kind'),
    h('div', { class: 'detail', role: 'radiogroup', 'aria-label': 'Kind' },
      [[false, 'JPEG', 'For sharing, printing and photo apps'], [true, '16-bit TIFF', 'For archiving. Much larger files']].map(([tiff, label, hint]) =>
        h('label', { class: 'opt' },
          h('input', { type: 'radio', name: 'kind', value: tiff ? 'tiff' : 'jpeg', checked: sheet.tiff === tiff, onchange: () => { sheet.tiff = tiff; renderExportSheet(); } }),
          h('span', {}, h('b', {}, label), h('br'), h('span', { class: 'hint' }, hint))))),
    tight ? flag('warn', { role: 'alert' },
      `${place.name || basename(place.path)} may not have room. ${plural(n, sheet.tiff ? 'TIFF' : 'JPEG', sheet.tiff ? 'TIFFs' : 'JPEGs')} need about ${gb(need).replace(' free', '')}, and it has ${gb(place.free)}.`) : null,
    h('div', { class: 'footer' },
      h('button', { class: 'btn btn-text', type: 'button', onclick: closeModal }, 'Cancel'),
      h('button', { class: 'btn btn-primary', type: 'button', 'aria-disabled': place ? null : 'true',
        onclick: () => place && (closeModal(), startExport({ folder: place.path, name: place.name || basename(place.path), tiff: sheet.tiff })) },
        `Export ${plural(n, 'picture', 'pictures')}`)));
  const dlg = $('info-dialog');
  if (dlg.open && $('info-body').querySelector('.sheet')) {   // keep the window and focus; just redraw what's inside
    const focusedName = document.activeElement && document.activeElement.name;
    const focusedVal = document.activeElement && document.activeElement.value;
    $('info-body').replaceChildren(body);
    const back = focusedName && [...body.querySelectorAll(`input[name="${focusedName}"]`)].find((i) => i.value === focusedVal);
    if (back) back.focus();
  } else openModal('Export', body);
}
// opts.folder: also copy every picture there under a plain name. Without it, pictures stay only in the roll.
async function startExport(opts = {}) {
  if (exp.running) return;
  const items = exportable().map((f) => ({ master: f.master, settings: settingsFor(f.master), stock: state.stockFor[f.master] || null }));
  if (!items.length) return;
  try {
    const args = { items, tiff: !!opts.tiff, redo: false };
    if (opts.folder) args.folder = opts.folder;
    const r = await call('export', args);
    if (opts.folder) {
      const p = sheetPlaces().find((x) => x.path === opts.folder);
      store.set('ofk.exportFolder', { path: opts.folder, name: opts.name, removable: p ? !!p.removable : false });
    }
    Object.assign(exp, { running: true, index: 0, total: r.total, now: items[0].master, result: null, failures: [], secs: null,
      firstCopy: null, folderName: opts.name || null, ejected: null, ejectError: null, ejecting: false });
    say(`Exporting ${plural(r.total, 'picture', 'pictures')}`);
  } catch (e) {
    exp.result = { error: e.message };
  }
  renderExportButton(); renderStatus(); if (state.view === 'sheet') renderSheet();
}
async function ejectDrive(path) {
  if (exp.ejecting) return;
  exp.ejecting = true; exp.ejectError = null; renderStatus();
  try { await call('eject', { path }); exp.ejected = true; say(`You can unplug ${exp.folderName || basename(path)} now`); }
  catch (e) { exp.ejectError = e.message; say(e.message); }
  exp.ejecting = false; renderStatus();
}
async function stopExport() {
  try { await call('cancel_export'); say('Stopping after this picture'); exp.stopping = true; renderStatus(); } catch { /* already finished */ }
}
function renderStatus() {
  const bar = $('status-bar');
  bar.replaceChildren();
  const frames = allFrames();
  if (state.folder) bar.append(h('span', {}, plural(frames.length, 'frame', 'frames')),
    h('span', {}, `${frames.filter((f) => f.state !== 'to convert').length} exported`));
  bar.append(h('span', { class: 'sp' }));
  if (exp.running) {
    const progress = h('div', { class: 'progress', role: 'progressbar', 'aria-label': 'Export', 'aria-valuemin': 0,
      'aria-valuemax': exp.total, 'aria-valuenow': exp.index }, h('i'));
    progress.firstChild.style.width = `${exp.total ? (exp.index / exp.total) * 100 : 0}%`;
    bar.append(h('span', { class: 'now' }, exp.stopping ? 'Stopping after this picture…' : [`Exporting ${Math.min(exp.index + 1, exp.total)} of ${exp.total}`, timeLeft(exp.secs)].filter(Boolean).join(' · ')),
      progress,
      exp.stopping ? null : h('button', { class: 'btn', type: 'button', onclick: stopExport }, 'Stop'));
  } else if (exp.result) {
    const r = exp.result;
    const box = h('div', { class: `done-msg${r.error || r.failed ? ' bad' : ''}`, role: 'status' });
    if (r.error) box.append(h('span', {}, `Export didn’t start: ${r.error}`));
    else {
      const parts = [plural(r.saved, 'picture exported', 'pictures exported')];
      if (r.skipped) parts.push(`${r.skipped} already up to date`);
      if (r.failed) parts.push(`${r.failed} failed`);
      box.append(h('b', {}, r.stopped ? 'Export stopped' : 'Export finished'), h('span', {}, parts.join(' · ')));
      if (r.folder) {
        const where = exp.folderName || basename(r.folder);
        const there = (r.saved || 0) + (r.skipped || 0);
        box.replaceChildren(h('b', {}, r.stopped ? 'Export stopped' : `${plural(there, 'picture is', 'pictures are')} in ${where}`));
        if (r.failed || r.stopped) box.append(h('span', {}, parts.join(' · ')));
        if (exp.ejected) box.append(h('span', {}, `You can unplug ${where} now.`));
        else {
          if (exp.firstCopy) box.append(h('button', { class: 'btn', type: 'button', onclick: () => window.ofk.showInFolder(exp.firstCopy) }, ...withIcon('folder-open', 'Show in folder')));
          if (r.removable) box.append(h('button', { class: 'btn', type: 'button', 'aria-disabled': exp.ejecting ? 'true' : null, onclick: () => ejectDrive(r.folder) }, exp.ejecting ? 'Ejecting…' : 'Eject'));
        }
        if (exp.ejectError) box.append(h('span', { class: 'eject-error', role: 'alert' }, exp.ejectError));
      } else if (exp.lastOutput) box.append(h('button', { class: 'btn', type: 'button', onclick: () => window.ofk.showInFolder(exp.lastOutput) }, 'Show in folder'));
      if (r.failed) box.append(h('button', { class: 'btn', type: 'button', onclick: () => setView('queue') }, 'See what failed'));
    }
    box.append(h('button', { class: 'btn btn-text', type: 'button', onclick: () => { exp.result = null; renderStatus(); } }, 'Dismiss'));
    bar.append(box);
  }
}

// ---- the Queue tab: this computer's export, and any shared queues --------------------------
const queue = { work: null, shared: {} };   // shared[name] = { loading, status, error, busy }
function setView(v) {
  if (v === state.view) return;
  state.view = v; state.comparing = false;
  render(); if (v !== 'queue') renderInspector();
  if (v === 'queue') loadWork();
  if (v === 'scan' && !scan.doctor && !scan.checking) checkScanner();
}
async function loadWork() {
  try { queue.work = await call('work'); } catch (e) { queue.work = { error: e.message, export: null, queues: [] }; }
  if (state.view === 'queue') renderQueue();
  for (const name of queue.work.queues || []) if (!queue.shared[name]) loadShared(name);
}
async function loadShared(name) {
  const q = queue.shared[name] = { ...(queue.shared[name] || {}), loading: true, error: null };
  if (state.view === 'queue') renderQueue();
  try { q.status = await call('queue_status', { name }); } catch (e) { q.error = e.message; }
  q.loading = false;
  if (state.view === 'queue') renderQueue();
}
function confirmBox(title, text, ok) {
  const dlg = $('confirm-dialog');
  $('confirm-title').textContent = title; $('confirm-text').textContent = text; $('confirm-ok').textContent = ok;
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
    dlg.returnValue = ''; dlg.showModal(); $('confirm-ok').focus();
  });
}
const PILL = { waiting: '', saved: 'ok', skipped: '', failed: 'bad', running: 'run', done: 'ok', stopped: '', damaged: 'bad' };
const PILL_WORD = { waiting: 'Waiting', saved: 'Exported', skipped: 'Unchanged', failed: 'Failed', running: 'Working', done: 'Done', stopped: 'Stopped', damaged: 'Damaged' };
const pill = (st) => h('span', { class: `pill ${PILL[st] || ''}` }, PILL_WORD[st] || st);
const when = (utc) => { try { return new Date(utc).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); } catch { return ''; } };

function renderQueue() {
  const body = $('photo-body');
  const wrap = h('div', { class: 'queue' });
  wrap.append(renderLocal());
  const names = (queue.work && queue.work.queues) || [];
  if (names.length) {
    wrap.append(h('section', {}, h('h2', {}, 'Shared queues'),
      h('p', { class: 'hint' }, 'Work sent to other computers. Finished pictures come back beside their scans when you bring them back.'),
      names.map(renderShared)));
  }
  body.replaceChildren(wrap);
}
function renderLocal() {
  const sec = h('section', {}, h('h2', {}, 'On this computer'));
  const w = queue.work;
  if (!w) { sec.append(h('p', { class: 'hint' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Loading…')); return sec; }
  const ex = w.export;
  if (!ex && exp.running) {
    sec.append(h('p', { class: 'hint' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Starting the export…'));
    return sec;
  }
  if (!ex) {
    sec.append(h('p', { class: 'hint' }, 'Nothing exported yet. Export makes a finished picture of every frame in the open folder, and its progress shows here.'));
    return sec;
  }
  const d = ex.done;
  const counts = ex.items.reduce((c, it) => ((c[it.status] = (c[it.status] || 0) + 1), c), {});
  const headline = ex.running ? [`Exporting ${Math.min((counts.saved || 0) + (counts.skipped || 0) + (counts.failed || 0) + 1, ex.total)} of ${ex.total}`, timeLeft(exp.secs)].filter(Boolean).join(' · ')
    : d ? `${d.stopped ? 'Stopped' : 'Finished'} ${when(d.finished_utc)}` : 'Export';
  const summary = [counts.saved ? `${counts.saved} exported` : null, counts.skipped ? `${counts.skipped} unchanged` : null,
    counts.failed ? `${counts.failed} failed` : null, counts.waiting ? `${counts.waiting} waiting` : null].filter(Boolean).join(' · ');
  const failed = ex.items.filter((it) => it.status === 'failed');
  sec.append(h('p', { class: 'hint' }, `Started ${when(ex.started_utc)}${ex.tiff ? ' · 16-bit TIFF' : ''}`),
    h('div', { class: 'qcard' },
      h('div', { class: 'qhead' }, ex.running ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : null,
        h('span', { class: 'title' }, headline), h('span', { class: 'hint' }, summary), h('span', { class: 'sp' }),
        ex.running ? h('button', { class: 'btn btn-small', type: 'button', onclick: stopExport }, exp.stopping ? 'Stopping…' : 'Stop') : null,
        !ex.running && failed.length ? h('button', { class: 'btn btn-small', type: 'button', onclick: () => startExport() }, 'Try failed ones again') : null),
      h('ul', { class: 'qrows', role: 'list' }, ex.items.map((it) => h('li', { class: 'qrow' },
        h('span', { class: 'what' }, h('b', {}, nice(basename(it.master).replace(/\.tiff?$/i, ''))),
          it.message ? h('span', { class: 'why' }, ` — ${it.message}`) : null),
        it.output ? h('button', { class: 'btn btn-text btn-small', type: 'button', onclick: () => window.ofk.showInFolder(it.output) }, 'Show') : h('span'),
        exp.running && exp.now === it.master ? pill('running') : pill(it.status))))));
  return sec;
}
function renderShared(name) {
  const q = queue.shared[name] || { loading: true };
  const st = q.status;
  const head = h('div', { class: 'qhead' }, h('span', { class: 'title', title: st && st.where ? st.where : null }, name),
    h('span', { class: 'sp' }),
    q.loading ? h('span', { class: 'hint' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Checking…')
      : h('button', { class: 'btn btn-small', type: 'button', onclick: () => loadShared(name) }, 'Refresh'));
  const card = h('div', { class: 'qcard', 'aria-busy': q.loading ? 'true' : 'false' }, head);
  if (q.error) { card.append(h('div', { class: 'qempty' }, q.error)); return card; }
  if (!st) { card.append(h('div', { class: 'qempty' }, 'Reaching the queue computer…')); return card; }
  const c = st.counts || {};
  card.append(h('div', { class: 'workers' },
    h('span', {}, ['waiting', 'running', 'done', 'failed', 'stopped', 'damaged'].filter((k) => c[k]).map((k) => `${c[k]} ${PILL_WORD[k].toLowerCase()}`).join(' · ') || 'No jobs'),
    (st.workers || []).map((wk) => h('span', { class: wk.silent ? 'silent' : '' }, h('b', {}, wk.name), ` ${wk.seen}${wk.running ? ` · working on ${wk.running}` : ''}`)),
    !(st.workers || []).length ? h('span', {}, 'No workers have reported') : null));
  const doneJobs = (st.jobs || []).filter((j) => j.state === 'done');
  const failedJobs = (st.jobs || []).filter((j) => j.state === 'failed');
  if (doneJobs.length || failedJobs.length) {
    card.append(h('div', { class: 'qhead' },
      doneJobs.length ? h('button', { class: 'btn btn-small btn-primary', type: 'button', 'aria-disabled': q.busy ? 'true' : null,
        onclick: () => sharedAction(name, 'queue_collect', {}, (r) => `Brought back ${plural(r.collected.length, 'picture', 'pictures')}${r.failed.length ? `; ${r.failed.length} couldn’t be` : ''}`) },
      `Bring back ${plural(doneJobs.length, 'finished picture', 'finished pictures')}`) : null,
      failedJobs.length ? h('button', { class: 'btn btn-small', type: 'button', 'aria-disabled': q.busy ? 'true' : null,
        onclick: () => sharedAction(name, 'queue_retry', {}, (r) => `Trying ${plural(r.retried.length, 'job', 'jobs')} again`) },
      `Try ${plural(failedJobs.length, 'failed job', 'failed jobs')} again`) : null));
  }
  if (!(st.jobs || []).length) { card.append(h('div', { class: 'qempty' }, 'Nothing in this queue.')); return card; }
  card.append(h('ul', { class: 'qrows', role: 'list' }, st.jobs.map((j) => h('li', { class: 'qrow' },
    h('span', { class: 'what' }, h('b', {}, j.photo || j.short),
      j.state === 'waiting' && j.why_waiting ? h('span', { class: 'why' }, ` — ${j.why_waiting}`) : null,
      j.error ? h('span', { class: 'why' }, ` — ${j.error}`) : null,
      j.worker && j.state === 'running' ? h('span', { class: 'why' }, ` — on ${j.worker}`) : null),
    h('button', { class: 'btn btn-text btn-small', type: 'button', 'aria-label': `Remove ${j.photo || j.short} from ${name}`,
      onclick: async () => {
        if (!await confirmBox(`Remove ${j.photo || j.short}?`, `This takes the job out of the ${name} queue. Your scan isn’t touched, and you can send it again later.`, 'Remove')) return;
        sharedAction(name, 'queue_remove', { jobs: [j.short || j.id] }, (r) => `Removed ${plural(r.removed.length, 'job', 'jobs')}`);
      } }, 'Remove'),
    pill(j.state)))));
  return card;
}
async function sharedAction(name, method, params, message) {
  const q = queue.shared[name];
  if (!q || q.busy) return;
  q.busy = true; renderQueue();
  try { say(message(await call(method, { name, ...params }))); }
  catch (e) { q.error = e.message; }
  q.busy = false;
  await loadShared(name);
  if (method === 'queue_collect') refresh(true);
}

// ---- folders ----------------------------------------------------------------------
async function openFolder() {
  const folder = await window.ofk.pickFolder('Open a folder of scans');
  if (folder) await load(folder);
}
async function load(folder, keepSelection) {
  try {
    const r = await call('open', { path: folder });
    state.folder = r.folder; state.places = r.places;
    store.set('ofk.lastFolder', r.folder);
    $('where').textContent = r.places.length === 1 ? r.places[0].label : basename(r.folder);
    $('where').title = r.folder;
    document.title = `${basename(r.folder)} · OpenFilmKit`;
    const frames = allFrames();
    const want = keepSelection && state.current ? keyOf(state.current) : store.get('ofk.lastFrame', null);
    const again = frames.find((f) => keyOf(f) === want);
    if (!again && state.view === 'photo') state.view = 'sheet';
    render(); renderExportButton(); renderStatus(); checkChanged();
    if (again && !keepSelection) await select(again);
    else if (again) { state.current = again; render(); }
    else if (!keepSelection && frames.length) await select(frames[0]);
    else renderInspector();
    say(`${plural(frames.length, 'frame', 'frames')} in ${basename(r.folder)}`);
  } catch (e) {
    state.folder = null; state.view = 'sheet'; render(); renderExportButton(); renderStatus();
    state.error = e; renderInspector();
    $('photo-body').replaceChildren(h('div', { class: 'empty' }, h('h2', {}, 'Couldn’t open that folder'), h('p', {}, e.message),
      h('button', { class: 'btn btn-large btn-primary', type: 'button', onclick: openFolder }, 'Open another folder')));
  }
}
const refresh = (keep) => state.folder && load(state.folder, keep);
// Which saved pictures no longer match their frame's adjustments: the back end compares, the same way export does.
async function checkChanged() {
  const items = exportable().map((f) => ({ master: f.master, settings: settingsFor(f.master), stock: state.stockFor[f.master] || null }));
  if (!items.length) return;
  try {
    const r = await call('changed', { items });
    for (const [master, v] of Object.entries(r)) { if (v) state.changed[master] = true; else delete state.changed[master]; }
    store.set('ofk.changed', state.changed);
    for (const f of allFrames()) markCell(f);
    renderExportButton();
  } catch { /* the local guess stands */ }
}

// ---- the Scan tab: the scanner, the roll the scans go into, and each frame as it lands ------------
const scan = { running: false, log: [], doctor: null, checking: false, started: 0, dpi: store.get('ofk.dpi', '3600'), latest: null, big: { key: null, src: null }, fraction: null, secs: null, result: null, rollError: null };
const DETAIL = [
  { dpi: '3600', label: 'Standard', hint: '3600 dpi · about 1 minute a frame', secs: 60 },
  { dpi: '7200', label: 'Most detail', hint: '7200 dpi · about 4 minutes a frame', secs: 240 },
  { dpi: '1800', label: 'Quick', hint: '1800 dpi · a fast test scan, not for archiving', secs: 30 },
];
function currentRoll() { return state.places.find((p) => p.is_roll && (!state.current || p.frames.some((f) => f === state.current))) || state.places.find((p) => p.is_roll) || null; }
function openScan() { setView('scan'); }
async function checkScanner() {
  scan.checking = true; renderScanPanel();
  try { scan.doctor = await call('doctor'); } catch (e) { scan.doctor = { ok: false, message: e.message }; }
  scan.checking = false;
  if (state.view === 'scan') renderScanPanel();
}

// Left: the newest frame large, and the roll so far underneath.
function renderScanStage() {
  const body = $('photo-body');
  const roll = currentRoll();
  const frames = roll ? roll.frames.filter((f) => f.master) : [];
  const latest = scan.latest && frames.find((f) => f.master === scan.latest) || frames[frames.length - 1] || null;
  let main;
  if (scan.running) {
    main = h('div', { class: 'scan-now' },
      h('h2', {}, `Scanning ${nice(`frame-${frames.length + 1}`)}…`), scanProgress(),
      h('p', {}, 'Leave the holder where it is until the scan finishes.'));
  } else if (scan.result && !scan.result.ok) {
    const r = scan.result;
    main = h('div', { class: 'scan-now scan-failed', role: 'alert' }, icon(r.stopped ? 'player-stop' : 'alert-triangle', 32),
      h('h2', {}, r.stopped ? 'Scan stopped' : 'This frame wasn’t scanned'),
      h('p', {}, r.message || 'The scan didn’t finish. Nothing was saved.'),
      h('p', { class: 'hint' }, 'Leave the holder where it is, so this frame isn’t skipped.'),
      h('button', { class: 'btn btn-large btn-primary', type: 'button', 'aria-disabled': roll && scan.doctor && scan.doctor.ok ? null : 'true',
        onclick: () => roll && scan.doctor && scan.doctor.ok && startScan(roll) }, ...withIcon('refresh', 'Scan again')));
  } else if (!latest) {
    main = h('div', { class: 'scan-now' },
      h('div', { class: 'insert', 'aria-hidden': 'true' }, h('span'), h('span'), h('span'), h('span')),
      h('h2', {}, roll ? 'Put the first strip in the scanner' : 'Choose where the scans go'),
      h('p', {}, roll ? 'Lay the film in the holder shiny side up, slide it into the scanner until the first frame clicks into place, then press Scan next frame.'
        : 'Each roll of film gets its own folder. Make a new one on the right, or open one you already have.'));
  } else {
    const key = latest.master;
    const src = state.thumbs.get(key);
    if (!src) queueThumb(latest, { isConnected: false });
    const r = scan.result && scan.result.ok && scan.result.output === key ? scan.result : null;
    const fresh = scan.latest === key;          // scanned in this launch, not just the newest frame in the folder
    const warns = r && Array.isArray(r.warnings) ? r.warnings.filter((w) => !(r.empty && /^this frame looks empty/i.test(w))) : [];   // the empty headline already says it
    // The strip's 360-pixel thumbnail is too small for the stage; fetch a large one for the newest frame only.
    if (scan.big.key !== key) {
      scan.big = { key, src: null };
      call('thumb', { path: latest.master, master: true, side: 1600, settings: settingsFor(latest.master), stock: state.stockFor[latest.master] || null })
        .then((big) => { if (scan.big.key === key) { scan.big.src = big; if (state.view === 'scan' && !scan.running) renderScanStage(); } })
        .catch(() => { /* the small thumbnail stays */ });
    }
    const stageSrc = scan.big.key === key && scan.big.src || src;
    main = h('figure', { class: 'scan-latest' },
      stageSrc ? h('img', { src: stageSrc, alt: `${nice(latest.name)}${fresh ? ", just scanned" : ""}` }) : h('div', { class: 'ph' }, h('span', { class: 'spinner', 'aria-hidden': 'true' })),
      r && (r.empty || warns.length) ? h('div', { class: 'scan-warn', role: 'alert' }, icon('alert-triangle', 18),
        h('div', {}, r.empty ? h('strong', {}, 'This frame looks empty. The holder may be past the end of the strip.') : null,
          warns.length ? h('ul', { role: 'list' }, warns.map((w) => h('li', {}, w))) : null)) : null,
      h('figcaption', {}, h('b', {}, nice(latest.name)), r && r.empty ? ' · looks empty. Check the holder before scanning again.' : fresh ? ' · just scanned. Slide the holder to the next frame, then scan again.' : ' · the last frame of this roll. Put the next frame in the holder, then scan.',
        h('button', { class: 'btn btn-small', type: 'button', onclick: () => { setView('sheet'); const f = allFrames().find((x) => x.master === key); if (f) openLarge(f); } }, 'Adjust it')));
  }
  const strip = frames.length ? h('div', { class: 'scan-strip' }, h('h3', {}, `${roll.label} · ${plural(frames.length, 'frame', 'frames')}`),
    h('ul', { role: 'list', tabindex: '0', 'aria-label': `Frames scanned into ${roll.label}` }, frames.map((f) => {   // it scrolls sideways: reachable by keyboard
      // Empty alt until the picture arrives: Chromium draws no broken-image icon for alt="", so the shimmer placeholder shows.
      const img = h('img', { alt: '', width: 96, height: 64, onload: (e) => { e.target.alt = nice(f.name); } });
      const c = state.thumbs.get(f.master); if (c) img.src = c; else queueThumb(f, img);
      return h('li', { class: f === latest ? 'is-latest' : '' }, img, h('span', {}, nice(f.name)));
    }))) : null;
  body.replaceChildren(h('div', { class: 'scan-stage' }, main, strip));
}

// Right: is the scanner there, which roll, how much detail, and the button.
function renderScanPanel() {
  const body = $('inspector-body');
  if (state.view !== 'scan' || !body) return;
  const roll = currentRoll();
  const d = scan.doctor;
  const ready = d && d.ok;
  const status = scan.checking || !d
    ? h('div', { class: 'scanner checking', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Looking for the scanner…')
    : ready ? h('div', { class: 'scanner ok' }, h('span', { class: 'dot', 'aria-hidden': 'true' }), 'Scanner ready',
        h('button', { class: 'btn btn-text btn-small', type: 'button', onclick: checkScanner }, ...withIcon('refresh', 'Check again')))
      : h('div', { class: 'scanner bad', role: 'alert' },
        h('span', { class: 'no-scanner' }, icon('eye-off', 28)),
        h('div', {}, h('strong', {}, 'No scanner found'),
          h('p', {}, 'Check that it’s plugged in and switched on, then check again.'),
          h('div', { class: 'row' }, h('button', { class: 'btn btn-small', type: 'button', onclick: checkScanner }, ...withIcon('refresh', 'Check again')),
            d.message ? detailsButton('Why the scanner wasn’t found', h('pre', { class: 'detail-text' }, d.message)) : null)));
  const det = DETAIL.find((x) => x.dpi === scan.dpi) || DETAIL[0];
  const elapsed = scan.running ? Math.round((Date.now() - scan.started) / 1000) : 0;
  const can = !scan.running && roll && ready;
  body.replaceChildren(...[
    h('h3', {}, 'Scanner'), status,
    h('h3', {}, 'Roll'),
    roll ? h('div', { class: 'scan-roll' }, h('b', {}, roll.label), h('span', { class: 'hint' }, `${plural(roll.frames.length, 'frame', 'frames')} so far`))
      : h('p', { class: 'hint' }, 'Scans go into a folder for this roll.'),
    scan.rollError ? flag('error', { role: 'alert' }, scan.rollError) : null,
    h('div', { class: 'row' },
      h('button', { class: 'btn btn-small', type: 'button', 'aria-disabled': scan.running ? 'true' : null, onclick: () => !scan.running && newRoll() }, 'New roll…'),
      h('button', { class: 'btn btn-small btn-text', type: 'button', 'aria-disabled': scan.running ? 'true' : null, onclick: () => !scan.running && openFolder() }, 'Open a roll…')),
    h('h3', {}, 'Detail'),
    h('div', { class: 'detail', role: 'radiogroup', 'aria-label': 'Detail' }, DETAIL.map((x) => h('label', { class: 'opt' },
      h('input', { type: 'radio', name: 'dpi', value: x.dpi, checked: x.dpi === scan.dpi, disabled: scan.running,
        onchange: () => { scan.dpi = x.dpi; store.set('ofk.dpi', x.dpi); } }),
      h('span', {}, h('b', {}, x.label), h('br'), h('span', { class: 'hint' }, x.hint))))),
    h('div', { class: 'actions pinned scan-go' },
      scan.running
        ? h('button', { class: 'btn btn-large', type: 'button', onclick: async () => { await call('cancel_scan'); say('Scan stopped'); } }, ...withIcon('player-stop', 'Stop scan'))
        : h('button', { class: 'btn btn-large btn-primary', type: 'button', 'aria-disabled': can ? null : 'true',
          title: !roll ? 'Make or open a roll first' : !ready ? 'The scanner isn’t ready' : null,
          onclick: () => can && startScan(roll) }, scan.result && !scan.result.ok ? 'Scan again' : 'Scan next frame'),
      h('button', { class: 'btn btn-small btn-text more-btn', type: 'button', onclick: () => openModal('Scanner messages',
        h('pre', { class: 'scan-log', id: 'scan-log', 'aria-live': 'off' }, scan.log.slice(-40).join('\n') || 'Nothing yet.')) }, icon('info-circle'), 'Scanner messages'))].filter(Boolean));
}
// Before the first progress event the scanner is warming up and calibrating, so the bar just sweeps.
function scanText() {
  if (scan.fraction == null) return 'Getting the scanner ready…';
  const pct = Math.round(scan.fraction * 100);
  const left = timeLeft(scan.secs);
  return left ? `${pct}% · ${left}` : `${pct}%`;
}
function scanProgress() {
  const known = scan.fraction != null;
  const bar = h('i'); if (known) bar.style.width = `${Math.round(scan.fraction * 100)}%`;
  return h('div', { class: `scan-progress${known ? '' : ' indeterminate'}`, id: 'scan-progress', role: 'progressbar', 'aria-label': 'Scanning',
    'aria-valuemin': known ? '0' : null, 'aria-valuemax': known ? '100' : null, 'aria-valuenow': known ? String(Math.round(scan.fraction * 100)) : null, 'aria-valuetext': scanText() },
    h('div', { class: 'track' }, bar), h('span', { class: 'hint', id: 'scan-elapsed' }, scanText()));
}
function updateScanProgress() {
  const el = $('scan-progress');
  if (el) el.replaceWith(scanProgress());
}

async function newRoll() {
  const parent = await window.ofk.pickFolder('Where should the new roll folder go?');
  if (!parent) return;
  scan.rollError = null;
  try {
    const r = await call('new_roll', { parent });      // the back end names it "Roll <local date>", then "… 2"
    scan.result = null;
    await load(r.path);
    say(`Made ${basename(r.path)}`);
  } catch (e) { scan.rollError = `Couldn’t make a new roll: ${e.message}`; scan.log.push(e.message); say(scan.rollError); }
  if (state.view === 'scan') { renderScanStage(); renderScanPanel(); }
}
async function startScan(roll) {
  if (scan.running) return;
  scan.running = true; scan.started = Date.now(); scan.log = ['Scanning…']; scan.fraction = null; scan.secs = null; scan.result = null; scan.rollError = null;
  renderScanStage(); renderScanPanel();
  say('Scanning started');
  try { await call('scan', { target: roll.path, dpi: parseInt(scan.dpi, 10) }); }
  catch (e) { scan.running = false; scan.log.push(e.message); renderScanStage(); renderScanPanel(); }
}

// ---- events from the back end ------------------------------------------------------------
window.ofk.onEvent(async (ev) => {
  if (ev.event === 'scan') {
    scan.log.push(ev.line);
    const el = $('scan-log'); if (el) { el.textContent = scan.log.slice(-40).join('\n'); el.scrollTop = el.scrollHeight; }
  } else if (ev.event === 'scan-progress') {
    scan.fraction = typeof ev.fraction === 'number' ? Math.max(0, Math.min(1, ev.fraction)) : scan.fraction;
    scan.secs = typeof ev.seconds_left === 'number' ? ev.seconds_left : null;
    if (state.view === 'scan') updateScanProgress();
  } else if (ev.event === 'scan-done') {
    scan.running = false; scan.fraction = null; scan.secs = null;
    const made = ev.ok ? (ev.output || ev.result?.captures?.[0]?.output || null) : null;   // a failed scan's planned path was never written
    scan.result = { ok: !!ev.ok && !!made, stopped: !!ev.stopped, output: made, message: ev.message || null,
      warnings: Array.isArray(ev.warnings) ? ev.warnings : [], empty: !!ev.empty };
    scan.log.push(scan.result.ok ? 'Done.' : (ev.message || `The scan stopped (code ${ev.code}).`));
    say(scan.result.ok ? (ev.empty ? 'Scan finished, but the frame looks empty' : 'Scan finished. Slide the holder to the next frame')
      : (ev.message || 'The scan didn’t finish'));
    if (made) scan.latest = made;
    await refresh(true);                      // stay on Scan; the new frame shows here and in Roll
    if (!ev.ok) scan.log.push('Nothing was saved from this scan.');
    if (state.view === 'scan') { renderScanStage(); renderScanPanel(); }
  } else if (ev.event === 'export') {
    if (ev.copy && !exp.firstCopy) exp.firstCopy = ev.copy;     // skipped frames are copied too
    exp.index = ev.index + 1; exp.secs = typeof ev.seconds_left === 'number' ? ev.seconds_left : null;
    const items = exportable();
    exp.now = items[ev.index + 1] ? items[ev.index + 1].master : null;
    if (ev.status === 'saved') {
      exp.lastOutput = ev.output; noteSaved(ev.master);
      const f = allFrames().find((x) => x.master === ev.master);
      if (f && f.state === 'to convert') f.state = 'to clean';
    } else if (ev.status === 'failed') exp.failures.push(`${basename(ev.master)}: ${ev.message}`);
    renderStatus(); if (state.view === 'sheet') renderSheet(); else if (state.view === 'queue') loadWork();
  } else if (ev.event === 'export-done') {
    Object.assign(exp, { running: false, stopping: false, now: null, result: ev });
    say(`Export ${ev.stopped ? 'stopped' : 'done'}: ${ev.saved} exported${ev.failed ? `, ${ev.failed} failed` : ''}`);
    if (exp.failures.length) { state.error = new Error(`Some pictures couldn’t be exported:\n${exp.failures.join('\n')}`); }
    await refresh(true);
    renderInspectorNotes(); renderStatus(); if (state.view === 'queue') loadWork();
  } else if (ev.event === 'preview-progress') {
    if (state.current && ev.master === state.current.master && state.developing) {
      develop.fraction = typeof ev.fraction === 'number' ? Math.max(0, Math.min(1, ev.fraction)) : null; develop.step = ev.step || ''; develop.secs = typeof ev.seconds_left === 'number' ? ev.seconds_left : null;
      if (Date.now() - state.developStart >= 5000) drawDevelopBar();
    }
  } else if (ev.event === 'backend-down') {
    state.error = new Error(ev.message); renderInspectorNotes();
    if (exp.running) { Object.assign(exp, { running: false, stopping: false, now: null, result: { error: ev.message } }); renderExportButton(); renderStatus(); }
  }
});

// ---- start ----------------------------------------------------------------------
$('open-btn').addEventListener('click', openFolder);
$('info-x').append(icon('x', 18));
$('tab-scan').addEventListener('click', () => setView('scan'));
$('export-btn').addEventListener('click', openExportSheet);
$('tab-roll').addEventListener('click', () => setView('sheet'));
$('tab-queue').addEventListener('click', () => setView('queue'));
window.ofk.onMenu((what) => {
  if (what === 'open') openFolder();
  else if (what === 'scan') openScan();
  else if (what === 'save') save(false);
  else if (what === 'compare') { toggleCompare(!state.comparing); }
  else if (what === 'undo') {
    const el = document.activeElement;
    if (el && el.matches('input[type="text"], textarea')) document.execCommand('undo');   // text boxes keep their own undo
    else if (state.view !== 'queue') undo();
  } else if (what === 'redo') {
    const el = document.activeElement;
    if (el && el.matches('input[type="text"], textarea')) document.execCommand('redo');
    else if (state.view !== 'queue') redo();
  } else if (what === 'zoom-in' || what === 'zoom-out' || what === 'zoom-reset') {
    const dir = what === 'zoom-in' ? 1 : what === 'zoom-out' ? -1 : 0;
    if (state.view === 'photo' && state.lastPreview && !document.querySelector('dialog[open]')) zoomTo(dir === 0 ? 1 : dir > 0 ? zoom.s * 1.5 : zoom.s / 1.5);
    else window.ofk.pageZoom(dir);
  } else if (what === 'undo-all') { if (state.view !== 'queue') undoAll(); }
});

(async function start() {
  render(); renderInspector(); renderStatus();
  try { state.stocks = await call('stocks'); } catch { state.stocks = []; }
  const last = store.get('ofk.lastFolder', null);
  if (last) await load(last);
  if (!state.folder) setView('scan');
})();
