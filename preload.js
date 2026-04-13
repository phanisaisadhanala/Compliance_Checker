/**
 * preload.js
 * Secure bridge — exposes a minimal API from main → renderer.
 * contextIsolation is ON so renderer cannot access Node APIs directly.
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Receive update notifications from main process
  onUpdateAvailable:  cb => ipcRenderer.on('update-available',  (_, info) => cb(info)),
  onUpdateDownloaded: cb => ipcRenderer.on('update-downloaded', (_, info) => cb(info)),
  onMenuReset:        cb => ipcRenderer.on('menu-reset',        ()         => cb()),

  // Send commands to main process
  restartAndInstall: ()    => ipcRenderer.send('restart-and-install'),
  openExternal:      (url) => ipcRenderer.send('open-external', url),
});