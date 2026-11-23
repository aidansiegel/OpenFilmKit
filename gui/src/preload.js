// The only bridge between the page and the rest of the app.
'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ofk', {
  call: (method, params) => ipcRenderer.invoke('ofk:call', method, params),
  pickFolder: (title) => ipcRenderer.invoke('dialog:folder', title),
  showInFolder: (p) => ipcRenderer.invoke('shell:show', p),
  pageZoom: (dir) => ipcRenderer.invoke('page:zoom', dir),
  onEvent: (fn) => ipcRenderer.on('backend-event', (_e, ev) => fn(ev)),
  onMenu: (fn) => ipcRenderer.on('menu', (_e, what) => fn(what)),
});
