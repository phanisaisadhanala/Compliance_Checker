const { app, BrowserWindow, ipcMain, dialog, shell, Menu, Tray, nativeImage } = require('electron');
const { autoUpdater } = require('electron-updater');
const path  = require('path');
const http  = require('http');
const { spawn } = require('child_process');
const fs    = require('fs');
const log   = require('electron-log');

// ── Logging setup ─────────────────────────────────────────────────────────────
log.transports.file.level = 'info';
autoUpdater.logger        = log;
autoUpdater.logger.transports.file.level = 'info';
log.info('PhasorGrid RelayPro starting. Version:', app.getVersion());

// ── Globals ───────────────────────────────────────────────────────────────────
let splashWin  = null;
let mainWin    = null;
let flaskProc  = null;
let tray       = null;
const FLASK_PORT = 5051;
const FLASK_URL  = `http://127.0.0.1:${FLASK_PORT}`;

// ── Single instance lock ──────────────────────────────────────────────────────
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) { app.quit(); }
else {
  app.on('second-instance', () => {
    if (mainWin) { if (mainWin.isMinimized()) mainWin.restore(); mainWin.focus(); }
  });
}

// ── Paths ─────────────────────────────────────────────────────────────────────
function getPythonPath() {
  if (app.isPackaged) {
    // Inside NSIS installer, Python is bundled next to app.exe
    return path.join(process.resourcesPath, 'python', 'python.exe');
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

function getAppPyPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'backend', 'app.py');
  }
  return path.join(__dirname, 'src', 'app.py');
}

function getIndexPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'frontend', 'index.html');
  }
  return path.join(__dirname, 'src', 'index.html');
}

// ── Start Flask ───────────────────────────────────────────────────────────────
function startFlask() {
  const python = getPythonPath();
  const script = getAppPyPath();
  log.info(`Starting Flask: ${python} ${script}`);
  flaskProc = spawn(python, [script], {
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  flaskProc.stdout.on('data', d => log.info('[Flask]', d.toString().trim()));
  flaskProc.stderr.on('data', d => log.warn('[Flask]', d.toString().trim()));
  flaskProc.on('close', code => log.info('Flask exited with code', code));
}

// ── Wait for Flask to become ready ───────────────────────────────────────────
function waitForFlask(retries = 30, delay = 500) {
  return new Promise((resolve, reject) => {
    let attempts = 0;
    const check  = () => {
      attempts++;
      http.get(`${FLASK_URL}/health`, res => {
        if (res.statusCode === 200) resolve();
        else retry();
      }).on('error', () => {
        if (attempts >= retries) reject(new Error('Flask did not start in time'));
        else setTimeout(check, delay);
      });
    };
    const retry = () => setTimeout(check, delay);
    check();
  });
}

// ── Splash screen ─────────────────────────────────────────────────────────────
function createSplash() {
  splashWin = new BrowserWindow({
    width:  480,
    height: 320,
    frame:   false,
    transparent: true,
    resizable:   false,
    alwaysOnTop: true,
    skipTaskbar: true,
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  splashWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(getSplashHTML())}`);
  splashWin.center();
}

function getSplashHTML() {
  return `<!DOCTYPE html><html>
<head><meta charset="UTF-8"><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    background: linear-gradient(135deg, #003580 0%, #0055cc 50%, #1a73e8 100%);
    border-radius: 16px; overflow: hidden; height: 320px;
    font-family: 'Segoe UI', sans-serif; color: #fff;
    display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 18px;
    -webkit-app-region: drag;
    box-shadow: 0 24px 80px rgba(0,0,0,0.5);
  }
  .logo-wrap { display:flex; flex-direction:column; align-items:center; gap:8px; }
  .logo-icon { font-size: 3rem; filter: drop-shadow(0 4px 16px rgba(0,0,0,0.3)); }
  .logo-name { font-size: 1.8rem; font-weight: 800; letter-spacing: -0.03em; }
  .logo-sub  { font-size: .7rem; letter-spacing: .18em; text-transform: uppercase; opacity: .75; }
  .progress-track { width: 260px; height: 4px; background: rgba(255,255,255,.2); border-radius:4px; overflow:hidden; }
  .progress-bar   { height:100%; width:0%; background:#90EEC0; border-radius:4px;
                    animation: load 2.8s ease-in-out forwards; }
  @keyframes load { 0%{width:0%} 40%{width:55%} 75%{width:80%} 100%{width:100%} }
  .status { font-size:.72rem; opacity:.65; letter-spacing:.06em; }
  .dots span { animation: blink 1.2s infinite; }
  .dots span:nth-child(2){ animation-delay:.2s; }
  .dots span:nth-child(3){ animation-delay:.4s; }
  @keyframes blink { 0%,80%,100%{opacity:0} 40%{opacity:1} }
</style></head>
<body>
  <div class="logo-wrap">
    <div class="logo-icon">⚡</div>
    <div class="logo-name">PhasorGrid RelayPro</div>
    <div class="logo-sub">Relay Intelligence Platform</div>
  </div>
  <div class="progress-track"><div class="progress-bar"></div></div>
  <div class="status">Starting backend<span class="dots"><span>.</span><span>.</span><span>.</span></span></div>
</body></html>`;
}

// ── Main window ───────────────────────────────────────────────────────────────
function createMainWindow() {
  mainWin = new BrowserWindow({
    width:  1280,
    height: 820,
    minWidth:  960,
    minHeight: 600,
    show: false,
    title: 'PhasorGrid — RelayPro (Intelligent Relay Compliance & Validation Platform)',
    icon: path.join(__dirname, 'assets', 'icon.png'),
    backgroundColor: '#f0f3f9',
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  // Load the frontend (served from file, not external browser)
  mainWin.loadFile(getIndexPath());

  // Show only after page finishes loading
  mainWin.once('ready-to-show', () => {
    if (splashWin) { splashWin.destroy(); splashWin = null; }
    mainWin.show();
    mainWin.focus();
    // Check for updates after app opens
    setTimeout(() => autoUpdater.checkForUpdatesAndNotify(), 3000);
  });

  mainWin.on('closed', () => { mainWin = null; });

  // Build native menu
  buildMenu();
}

// ── System tray ───────────────────────────────────────────────────────────────
function createTray() {
  const iconPath = path.join(__dirname, 'assets', 'tray-icon.png');
  if (!fs.existsSync(iconPath)) return;
  tray = new Tray(iconPath);
  tray.setToolTip('PhasorGrid');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open PhasorGrid RelayPro', click: () => { if (mainWin) mainWin.show(); } },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]));
  tray.on('double-click', () => { if (mainWin) mainWin.show(); });
}

// ── Native menu ───────────────────────────────────────────────────────────────
function buildMenu() {
  const template = [
    {
      label: 'File',
      submenu: [
        { label: 'New Check',  accelerator: 'CmdOrCtrl+N', click: () => mainWin?.webContents.send('menu-reset') },
        { type:  'separator' },
        { label: 'Quit',       accelerator: 'CmdOrCtrl+Q', click: () => app.quit() },
      ],
    },
    {
      label: 'View',
      submenu: [
        { label: 'Reload',     accelerator: 'CmdOrCtrl+R',     click: () => mainWin?.reload() },
        { label: 'Zoom In',    accelerator: 'CmdOrCtrl+=',     click: () => mainWin?.webContents.setZoomFactor(mainWin.webContents.getZoomFactor() + 0.1) },
        { label: 'Zoom Out',   accelerator: 'CmdOrCtrl+-',     click: () => mainWin?.webContents.setZoomFactor(mainWin.webContents.getZoomFactor() - 0.1) },
        { label: 'Reset Zoom', accelerator: 'CmdOrCtrl+0',     click: () => mainWin?.webContents.setZoomFactor(1) },
        { type:  'separator' },
        { label: 'Dev Tools',  accelerator: 'CmdOrCtrl+Shift+I', click: () => mainWin?.webContents.toggleDevTools() },
      ],
    },
    {
      label: 'Help',
      submenu: [
        { label: 'Check for Updates', click: () => autoUpdater.checkForUpdatesAndNotify() },
        { label: 'View Logs',         click: () => shell.openPath(log.transports.file.getFile().path) },
        { type:  'separator' },
        { label: `Version ${app.getVersion()}`, enabled: false },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── Auto-updater events ───────────────────────────────────────────────────────
autoUpdater.on('update-available', info => {
  log.info('Update available:', info.version);
  if (mainWin) mainWin.webContents.send('update-available', info);
});
autoUpdater.on('update-downloaded', info => {
  log.info('Update downloaded:', info.version);
  if (mainWin) mainWin.webContents.send('update-downloaded', info);
});
autoUpdater.on('error', err => log.error('AutoUpdater error:', err));

// IPC: user clicks "Restart & Update"
ipcMain.on('restart-and-install', () => {
  autoUpdater.quitAndInstall();
});

// IPC: user opens external link from renderer
ipcMain.on('open-external', (_, url) => shell.openExternal(url));

// ── App lifecycle ─────────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  createSplash();
  startFlask();
  createTray();

  try {
    await waitForFlask();
    log.info('Flask is ready');
  } catch (err) {
    log.error('Flask failed to start:', err);
    dialog.showErrorBox('Startup Error',
      'The backend service failed to start. Please reinstall PhasorGrid RelayPro.\n\n' + err.message);
    app.quit(); return;
  }

  createMainWindow();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
});

app.on('will-quit', () => {
  if (flaskProc) { flaskProc.kill('SIGTERM'); log.info('Flask process terminated'); }
  if (tray)      { tray.destroy(); }
});
