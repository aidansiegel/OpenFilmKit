// OpenFilmKit desktop: the window, and the Python back end (openfilmkit.gui_service) it talks to.
// The renderer gets no Node access; everything goes through the small API in preload.js.
'use strict';
const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

// Where the kit's Python lives. A packaged app will carry its own; until then, in this order:
// OPENFILMKIT_PYTHON, the Homebrew install, then python3 on the PATH.
function findPython() {
  const candidates = [
    process.env.OPENFILMKIT_PYTHON,
    path.join(process.resourcesPath || '', 'python', 'bin', 'python3'),
    '/opt/homebrew/opt/openfilmkit/libexec/venv/bin/python',
    '/usr/local/opt/openfilmkit/libexec/venv/bin/python',
  ].filter(Boolean);
  return candidates.find((p) => fs.existsSync(p)) || 'python3';
}

// The kit's source: this checkout when run from it (gui/ sits inside the repository).
function kitSource() {
  if (process.env.OPENFILMKIT_SOURCE) return process.env.OPENFILMKIT_SOURCE;
  const repo = path.resolve(__dirname, '..', '..');
  return fs.existsSync(path.join(repo, 'openfilmkit', 'gui_service.py')) ? repo : null;
}

class Backend {
  constructor(onEvent) {
    this.onEvent = onEvent;
    this.pending = new Map();
    this.nextId = 1;
    this.stderr = [];
    this.start();
  }

  start() {
    const env = { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1' };
    const src = kitSource();
    if (src) env.PYTHONPATH = src + (env.PYTHONPATH ? path.delimiter + env.PYTHONPATH : '');
    this.python = findPython();
    this.proc = spawn(this.python, ['-m', 'openfilmkit.gui_service'], { env, stdio: ['pipe', 'pipe', 'pipe'] });
    this.proc.on('error', (err) => this.fail(`Couldn't start the OpenFilmKit back end (${this.python}): ${err.message}`));
    this.proc.on('exit', (code) => {
      if (this.stopping) { this.proc = null; return; }   // we asked it to stop: nothing to report
      this.fail(`The OpenFilmKit back end stopped (exit ${code}). ${this.stderr.slice(-3).join(' ')}`.trim());
      this.proc = null;
    });
    readline.createInterface({ input: this.proc.stdout }).on('line', (line) => this.receive(line));
    readline.createInterface({ input: this.proc.stderr }).on('line', (line) => {
      this.stderr.push(line);
      if (this.stderr.length > 200) this.stderr.shift();
      console.error('[backend]', line);
    });
  }

  receive(line) {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    if (msg.event) { this.onEvent(msg); return; }
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    if ('error' in msg) p.reject(Object.assign(new Error(msg.error), { detail: msg.detail }));
    else p.resolve(msg.result);
  }

  fail(message) {
    for (const p of this.pending.values()) p.reject(new Error(message));
    this.pending.clear();
    this.onEvent({ event: 'backend-down', message });
  }

  call(method, params) {
    if (!this.proc) this.start();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.proc.stdin.write(JSON.stringify({ id, method, params: params || {} }) + '\n');
    });
  }

  stop() { this.stopping = true; if (this.proc) this.proc.kill(); }
}

let win;
let backend;

function createWindow() {
  win = new BrowserWindow({
    width: 1280, height: 820, minWidth: 720, minHeight: 520,
    title: 'OpenFilmKit',
    backgroundColor: '#2b2a28',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, nodeIntegration: false, sandbox: true,
    },
  });
  win.loadFile(path.join(__dirname, 'index.html'));
  // Links open in the browser, never in this window.
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  win.webContents.on('will-navigate', (e) => e.preventDefault());
}

function menu() {
  const send = (what) => () => { if (win && !win.isDestroyed()) win.webContents.send('menu', what); };
  const template = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    { label: 'File', submenu: [
      { label: 'Open folder…', accelerator: 'CmdOrCtrl+O', click: send('open') },
      { label: 'Scan…', accelerator: 'CmdOrCtrl+Shift+S', click: send('scan') },
      { type: 'separator' },
      { label: 'Save picture', accelerator: 'CmdOrCtrl+S', click: send('save') },
      { type: 'separator' },
      process.platform === 'darwin' ? { role: 'close' } : { role: 'quit' },
    ] },
    { label: 'Edit', submenu: [
      { label: 'Undo', accelerator: 'CmdOrCtrl+Z', click: send('undo') },
      { label: 'Redo', accelerator: 'Shift+CmdOrCtrl+Z', click: send('redo') },
      { label: 'Undo all changes to this picture', click: send('undo-all') },
      { type: 'separator' },
      { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
    ] },
    { label: 'View', submenu: [
      { label: 'Show before (no adjustments)', accelerator: 'CmdOrCtrl+B', click: send('compare') },
      { type: 'separator' },
      // ⌘+ / ⌘− / ⌘0 zoom the picture when one is open large; elsewhere they zoom the whole window.
      { label: 'Zoom in', accelerator: 'CmdOrCtrl+Plus', click: send('zoom-in') },
      { label: 'Zoom in', accelerator: 'CmdOrCtrl+=', click: send('zoom-in'), visible: false, acceleratorWorksWhenHidden: true },
      { label: 'Zoom out', accelerator: 'CmdOrCtrl+-', click: send('zoom-out') },
      { label: 'Actual size / fit', accelerator: 'CmdOrCtrl+0', click: send('zoom-reset') },
      { type: 'separator' },
      { role: 'togglefullscreen' }, { role: 'toggleDevTools' },
    ] },
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(() => {
  backend = new Backend((event) => { if (win && !win.isDestroyed()) win.webContents.send('backend-event', event); });
  ipcMain.handle('ofk:call', async (_e, method, params) => {
    try { return { ok: true, result: await backend.call(method, params) }; }
    catch (err) { return { ok: false, error: err.message, detail: err.detail || null }; }
  });
  ipcMain.handle('dialog:folder', async (_e, title) => {
    const r = await dialog.showOpenDialog(win, { title: title || 'Open a folder of scans', properties: ['openDirectory', 'createDirectory'] });
    return r.canceled ? null : r.filePaths[0];
  });
  ipcMain.handle('shell:show', (_e, p) => shell.showItemInFolder(p));
  // Window zoom, for when there's no picture to zoom (bigger text for anyone who needs it).
  ipcMain.handle('page:zoom', (e, dir) => {
    const wc = e.sender;
    wc.setZoomLevel(dir === 0 ? 0 : Math.max(-3, Math.min(5, wc.getZoomLevel() + (dir > 0 ? 0.5 : -0.5))));
  });
  menu();
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', () => backend && backend.stop());
