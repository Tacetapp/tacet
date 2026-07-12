'use strict';

const {
  app, BrowserWindow, Menu, Tray, nativeImage, ipcMain, dialog, shell,
  safeStorage, systemPreferences, globalShortcut, powerSaveBlocker,
} = require('electron');
const crypto = require('crypto');
const path = require('path');
const fs = require('fs');
const zlib = require('zlib');
const http = require('http');
const net = require('net');
const { spawn } = require('child_process');

const { SCHEMA, serverEnvFromConfig } = require('./config');

// Password-type field keys — routed through safeStorage instead of config.json (§4.4).
const _passwordKeys = SCHEMA
  .flatMap((g) => g.fields.filter((f) => f.type === 'password').map((f) => f.key));

// Per-launch WebSocket auth token (§4.1). Generated once; injected into the
// server env and embedded in every URL the main window loads.
const WS_TOKEN = crypto.randomBytes(32).toString('hex');

// ── Paths ─────────────────────────────────────────────
// In dev, the backend lives in the repo (one level up from electron/).
// When packaged, electron-builder copies it to resources/backend (see
// package.json → build.extraResources).
const REPO_ROOT = app.isPackaged
  ? path.join(process.resourcesPath, 'backend')
  : path.join(__dirname, '..');

const VENV_PYTHON = path.join(REPO_ROOT, '.venv', 'bin', 'python');
const CONFIG_PATH = path.join(app.getPath('userData'), 'config.json');
const LOG_PATH = path.join(app.getPath('userData'), 'server.log');
// The user's profile (freeform Markdown) personalizes prompts. In dev it lives
// at the repo root (read by the backend); when packaged it lives in the
// writable userData dir. Passed to the server as USER_PROFILE_PATH.
const PROFILE_PATH = app.isPackaged
  ? path.join(app.getPath('userData'), 'profile.md')
  : path.join(REPO_ROOT, 'profile.md');
// First-run marker + encrypted provider secrets (never in config.json plaintext).
const ONBOARDED_PATH = path.join(app.getPath('userData'), '.onboarded');
const SECRETS_PATH = path.join(app.getPath('userData'), 'secrets.json');

const PREFERRED_PORT = 8000;

// ── State ─────────────────────────────────────────────
let mainWindow = null;
let settingsWindow = null;
let onboardingWindow = null;
let onboardingFinishing = false;
let serverProcess = null;
let serverPort = PREFERRED_PORT;
let shuttingDown = false;
let restarting = false;
let logStream = null;

// ── Tray + meeting state ───────────────────────────────
let tray = null;
let _trayIcons = null;        // cached { idle, recording } nativeImages
let _powerSaveId = -1;        // active powerSaveBlocker id, or -1 when none
let meetingState = { active: false, paused: false, elapsed: 0 };

// ── Config persistence ────────────────────────────────
function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  } catch {
    return {};
  }
}

function saveConfig(config) {
  fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2), 'utf8');
}

// ── Onboarding state + provider secrets ───────────────
function isOnboarded() {
  return fs.existsSync(ONBOARDED_PATH);
}

function markOnboarded() {
  fs.mkdirSync(path.dirname(ONBOARDED_PATH), { recursive: true });
  fs.writeFileSync(ONBOARDED_PATH, new Date().toISOString(), 'utf8');
}

// Store an API key encrypted via the OS keychain (safeStorage). Falls back to
// a plaintext marker only if encryption is unavailable (rare).
function saveSecret(name, value) {
  let secrets = {};
  try {
    secrets = JSON.parse(fs.readFileSync(SECRETS_PATH, 'utf8'));
  } catch {}
  if (!value) {
    delete secrets[name];
  } else if (safeStorage.isEncryptionAvailable()) {
    secrets[name] = { enc: safeStorage.encryptString(value).toString('base64') };
  } else {
    log(`⚠️  WARNING: OS keychain (safeStorage) unavailable — storing secret "${name}" in PLAINTEXT at ${SECRETS_PATH}. Enable your login keychain to secure it.`);
    secrets[name] = { plain: value };
  }
  fs.mkdirSync(path.dirname(SECRETS_PATH), { recursive: true });
  fs.writeFileSync(SECRETS_PATH, JSON.stringify(secrets), 'utf8');
}

// Decrypt all stored secrets into a flat { NAME: value } map for the server env.
function loadSecrets() {
  let secrets = {};
  try {
    secrets = JSON.parse(fs.readFileSync(SECRETS_PATH, 'utf8'));
  } catch {
    return {};
  }
  const out = {};
  for (const [name, entry] of Object.entries(secrets)) {
    try {
      if (entry.enc) {
        out[name] = safeStorage.decryptString(Buffer.from(entry.enc, 'base64'));
      } else if (entry.plain) {
        out[name] = entry.plain;
      }
    } catch (err) {
      log(`Failed to decrypt secret ${name}: ${err.message}`);
    }
  }
  return out;
}

// ── Logging ───────────────────────────────────────────

// Rotate server.log if it exceeds ~10 MB before opening the write stream (§4.4).
function _rotateLogIfNeeded() {
  try {
    const stat = fs.statSync(LOG_PATH);
    if (stat.size > 10 * 1024 * 1024) {
      fs.renameSync(LOG_PATH, LOG_PATH + '.1');
    }
  } catch {
    /* file may not exist yet — that's fine */
  }
}

function log(line) {
  const stamped = `[electron] ${new Date().toISOString()} ${line}`;
  console.log(stamped);
  try {
    if (!logStream) {
      _rotateLogIfNeeded();
      logStream = fs.createWriteStream(LOG_PATH, { flags: 'a' });
    }
    logStream.write(stamped + '\n');
  } catch {
    /* logging is best-effort */
  }
}

// ── Tray icon generation ──────────────────────────────
// Build a minimal valid PNG from raw RGBA pixel data using only built-in
// Node.js modules (no canvas dependency). Used to generate the tray icon
// programmatically so no image file needs to be bundled.
function _makePng(size, drawFn) {
  const rows = [];
  for (let y = 0; y < size; y++) {
    const row = Buffer.alloc(1 + size * 4);
    row[0] = 0; // PNG filter: None
    for (let x = 0; x < size; x++) {
      const [r, g, b, a] = drawFn(x, y);
      const base = 1 + x * 4;
      row[base] = r; row[base + 1] = g; row[base + 2] = b; row[base + 3] = a;
    }
    rows.push(row);
  }
  const idat = zlib.deflateSync(Buffer.concat(rows));

  // Minimal CRC-32 for PNG chunks.
  const crcT = (() => {
    const t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let j = 0; j < 8; j++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
      t[i] = c;
    }
    return t;
  })();
  const crc32 = (buf) => {
    let c = 0xFFFFFFFF;
    for (const b of buf) c = crcT[(c ^ b) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  };
  const chunk = (type, data) => {
    const lenB = Buffer.alloc(4); lenB.writeUInt32BE(data.length);
    const typeB = Buffer.from(type);
    const crcB = Buffer.alloc(4);
    crcB.writeUInt32BE(crc32(Buffer.concat([typeB, data])));
    return Buffer.concat([lenB, typeB, data, crcB]);
  };

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0); ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; ihdr[9] = 6; // 8-bit depth, RGBA colour type
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), // PNG signature
    chunk('IHDR', ihdr),
    chunk('IDAT', idat),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// Build a circular dot icon at logical `size` px with given fill `radius`.
// Returns a nativeImage marked as a macOS template (auto light/dark).
function _makeDotIcon(size, radius) {
  const makeRepr = (sz, r) => {
    const cx = sz / 2 - 0.5, cy = sz / 2 - 0.5;
    return _makePng(sz, (x, y) => {
      const d2 = (x - cx) ** 2 + (y - cy) ** 2;
      return d2 <= r * r ? [0, 0, 0, 255] : [0, 0, 0, 0];
    });
  };
  const img = nativeImage.createFromBuffer(makeRepr(size, radius));
  // Add @2x Retina representation for crisp rendering on HiDPI displays.
  img.addRepresentation({ scaleFactor: 2, buffer: makeRepr(size * 2, radius * 2) });
  img.setTemplateImage(true); // macOS: system handles dark/light mode colouring
  return img;
}

function _getTrayIcons() {
  if (!_trayIcons) {
    _trayIcons = {
      idle: _makeDotIcon(16, 4.5),
      recording: _makeDotIcon(16, 7),
    };
  }
  return _trayIcons;
}

function _fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

// ── Port selection ────────────────────────────────────
function isPortFree(port) {
  return new Promise((resolve) => {
    const tester = net
      .createServer()
      .once('error', () => resolve(false))
      .once('listening', () => tester.close(() => resolve(true)))
      .listen(port, '127.0.0.1');
  });
}

async function pickPort() {
  // Packaged app: ALWAYS use an ephemeral port. A "free" check on 8000 can
  // false-positive when another server holds the wildcard bind (BSD lets a
  // specific 127.0.0.1 bind coexist with 0.0.0.0 under SO_REUSEADDR), which
  // let a foreign dev server share the port and answer our window's WS.
  // Only dev builds prefer the stable 8000 (for the Vite /ws proxy).
  if (!app.isPackaged && await isPortFree(PREFERRED_PORT)) return PREFERRED_PORT;
  // Fall back to an OS-assigned ephemeral port. The frontend derives the
  // WebSocket URL from window.location, so any port works.
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

// ── Health check ──────────────────────────────────────
function pingServer(port) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: '/', timeout: 2000 },
      (res) => {
        res.resume();
        resolve(res.statusCode > 0);
      },
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForServer(port, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await pingServer(port)) return true;
    if (!serverProcess) return false; // process died while we waited
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

// ── Server lifecycle ──────────────────────────────────
function pythonAvailable() {
  return fs.existsSync(VENV_PYTHON);
}

async function startServer() {
  if (!pythonAvailable()) {
    throw new Error(
      `Python environment not found at:\n${VENV_PYTHON}\n\n` +
        `Run \`make setup\` in the project to create it.`,
    );
  }

  serverPort = await pickPort();

  const env = {
    ...process.env,
    ...serverEnvFromConfig(loadConfig()),
    ...loadSecrets(), // decrypted provider API keys (OPENROUTER_API_KEY, …)
    WS_AUTH_TOKEN: WS_TOKEN, // per-launch WebSocket auth token (§4.1)
    // Packaged app Resources are read-only — send model downloads/caches to a
    // writable userData dir (HuggingFace whisper/ECAPA weights + ECAPA savedir).
    ...(app.isPackaged
      ? {
          HF_HOME: path.join(app.getPath('userData'), 'hf-cache'),
          MODELS_DIR: path.join(app.getPath('userData'), 'models'),
        }
      : {}),
    PYTHONUNBUFFERED: '1',
    PYTHONPATH: REPO_ROOT,
    USER_PROFILE_PATH: PROFILE_PATH,
    // Belt-and-suspenders for the Intel OpenMP duplicate-runtime issue; main.py
    // also sets this, but exporting early avoids any import-order surprises.
    KMP_DUPLICATE_LIB_OK: 'TRUE',
  };

  const args = [
    '-m',
    'uvicorn',
    'web.backend.main:app',
    '--host',
    '127.0.0.1',
    '--port',
    String(serverPort),
  ];

  log(`Starting server: ${VENV_PYTHON} ${args.join(' ')} (port ${serverPort})`);

  // detached:true puts the server in its own process group so we can kill the
  // whole tree (uvicorn + the Swift capture helper it spawns) on quit.
  const child = spawn(VENV_PYTHON, args, {
    cwd: REPO_ROOT,
    env,
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  serverProcess = child;

  child.stdout.on('data', (d) => log(`[server] ${d.toString().trimEnd()}`));
  child.stderr.on('data', (d) => log(`[server] ${d.toString().trimEnd()}`));
  // An async spawn/exec failure (e.g. python present but not executable) emits
  // 'error'; without a listener Node would throw in the main process.
  child.on('error', (err) => log(`[server] spawn error: ${err.message}`));

  child.on('exit', (code, signal) => {
    log(`Server exited (code=${code}, signal=${signal})`);
    // Only react if THIS child is still the active server. A restart/stop nulls
    // or replaces serverProcess before the old child's exit fires, so a guarded
    // check distinguishes an intentional stop from a real crash.
    if (serverProcess !== child) return;
    serverProcess = null;
    if (!shuttingDown && mainWindow) {
      dialog.showMessageBox(mainWindow, {
        type: 'error',
        title: 'Server stopped',
        message: 'The Tacet server stopped unexpectedly.',
        detail: `See the log for details:\n${LOG_PATH}`,
        buttons: ['Quit', 'Restart server'],
        defaultId: 1,
      }).then(({ response }) => {
        if (response === 1) restartServer();
        else app.quit();
      });
    }
  });

  const ok = await waitForServer(serverPort);
  if (!ok) {
    throw new Error(
      `Server did not become ready on port ${serverPort}.\nSee log: ${LOG_PATH}`,
    );
  }
  log(`Server ready on http://127.0.0.1:${serverPort}`);
}

function killGroup(pid, signal) {
  try {
    // Negative pid → signal the whole process group (kills the Swift capture
    // helper that uvicorn spawned, not just uvicorn itself).
    process.kill(-pid, signal);
    return true;
  } catch {
    try {
      process.kill(pid, signal);
      return true;
    } catch {
      return false; // already gone
    }
  }
}

// Graceful stop used while the app keeps running (e.g. server restart).
function stopServer() {
  if (!serverProcess) return;
  const proc = serverProcess;
  serverProcess = null;
  killGroup(proc.pid, 'SIGTERM');
  setTimeout(() => killGroup(proc.pid, 'SIGKILL'), 2500);
}

// Synchronous best-effort reap used on quit. We can't await timers during
// before-quit, so SIGKILL the group outright — no graceful shutdown is needed
// when the whole app is going away, and this guarantees no leaked server.
function killServerNow() {
  if (!serverProcess) return;
  const proc = serverProcess;
  serverProcess = null;
  killGroup(proc.pid, 'SIGTERM');
  killGroup(proc.pid, 'SIGKILL');
}

async function restartServer() {
  // Guard against overlapping restarts (rapid settings saves, or Restart-server
  // clicked mid-restart) spawning multiple servers.
  if (restarting) {
    log('Restart already in progress; ignoring duplicate request');
    return;
  }
  restarting = true;
  try {
    log('Restarting server…');
    stopServer();
    // Give the OS a moment to release the port before rebinding.
    await new Promise((r) => setTimeout(r, 800));
    await startServer();
    if (mainWindow) mainWindow.loadURL(serverUrl());
  } finally {
    restarting = false;
  }
}

function serverUrl() {
  return `http://127.0.0.1:${serverPort}/?token=${WS_TOKEN}`;
}

// ── Windows ───────────────────────────────────────────
function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: 'Tacet',
    backgroundColor: '#0d1117',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'loading.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // Open external links in the system browser, not inside the app.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http')) shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function openSettings() {
  if (settingsWindow) {
    settingsWindow.focus();
    return;
  }
  settingsWindow = new BrowserWindow({
    width: 760,
    height: 820,
    title: 'Settings',
    parent: mainWindow || undefined,
    backgroundColor: '#0d1117',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // sandbox:true is now safe because the preload no longer require()s local
      // modules — the schema is fetched over IPC instead (§5.4).
    },
  });
  settingsWindow.loadFile(path.join(__dirname, 'settings.html'));
  settingsWindow.on('closed', () => {
    settingsWindow = null;
  });
}

// ── User profile ──────────────────────────────────────
const PROFILE_TEMPLATE = `# My Profile

This file personalizes your meeting assistant. It's read at the start of each
meeting and shapes live insights and summaries. Edit it freely — plain Markdown,
no fixed format. The more accurate it is, the better the assistant adapts to you.

## Role
A professional attending work meetings.

## Expertise
General business and technology. (List the areas you know well — the assistant
will skip explaining things you'd already understand.)

## Organization
(Not specified — add where you work and what it does.)

## Typical counterparts
(Not specified — who you usually meet with: customers, teammates, vendors,
partners…)

## How I like notes
Clear, terse, factual. Lead with decisions, commitments, risks, and action items.

## Notes
<!-- Free space for anything else worth knowing. The assistant may also add
     learnings about you here over time. -->
`;

// Seed the profile if it doesn't exist yet (first run / packaged userData).
function ensureProfile() {
  try {
    if (!fs.existsSync(PROFILE_PATH)) {
      fs.mkdirSync(path.dirname(PROFILE_PATH), { recursive: true });
      fs.writeFileSync(PROFILE_PATH, PROFILE_TEMPLATE, 'utf8');
      log(`Seeded profile at ${PROFILE_PATH}`);
    }
  } catch (err) {
    log(`Failed to seed profile: ${err.message}`);
  }
}

function openProfile() {
  ensureProfile();
  shell.openPath(PROFILE_PATH);
}

// ── Onboarding window ─────────────────────────────────
function createOnboardingWindow() {
  onboardingWindow = new BrowserWindow({
    width: 640,
    height: 760,
    title: 'Welcome',
    resizable: false,
    backgroundColor: '#0d1117',
    webPreferences: {
      preload: path.join(__dirname, 'onboarding-preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // sandbox:true is safe — onboarding-preload.js uses only electron APIs,
      // no local require() (§5.4).
    },
  });
  onboardingWindow.loadFile(path.join(__dirname, 'onboarding.html'));
  onboardingWindow.on('closed', () => {
    onboardingWindow = null;
    // If the user closed the window without finishing, there's no configured
    // app to show — quit rather than launch a half-set-up state.
    if (!onboardingFinishing && !isOnboarded()) app.quit();
  });
}

// ── Window focus helper ───────────────────────────────
function focusMainWindow() {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    if (!mainWindow.isVisible()) mainWindow.show();
    mainWindow.focus();
    app.focus({ steal: true }); // bring app to foreground on macOS
  } else if (isOnboarded()) {
    startMainApp().catch((err) => log(`focusMainWindow: failed to reopen: ${err.message}`));
  }
}

// ── Tray (macOS-only) ─────────────────────────────────
function buildTrayMenu() {
  const { active, paused, elapsed } = meetingState;
  const sendCmd = (cmd) => mainWindow?.webContents.send('meeting:command', cmd);
  const elapsedStr = elapsed > 0 ? ` — ${_fmtElapsed(elapsed)}` : '';

  return Menu.buildFromTemplate([
    {
      label: 'Show Tacet',
      click: focusMainWindow,
    },
    { type: 'separator' },
    // Recording state display + primary action
    ...(active
      ? [
          { label: `● Recording${elapsedStr}`, enabled: false },
          {
            label: paused ? 'Resume' : 'Pause',
            click: () => sendCmd(paused ? 'resume' : 'pause'),
          },
          {
            label: 'End Meeting',
            click: () => { focusMainWindow(); sendCmd('stop'); },
          },
        ]
      : [
          {
            label: 'Start Meeting',
            click: () => { focusMainWindow(); sendCmd('start'); },
          },
        ]),
    { type: 'separator' },
    { label: 'Settings…', click: openSettings },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ]);
}

function updateTray() {
  if (!tray) return;
  const { active, paused, elapsed } = meetingState;
  const icons = _getTrayIcons();
  tray.setImage(active ? icons.recording : icons.idle);
  const tip = active
    ? (paused ? `⏸ Meeting paused — ${_fmtElapsed(elapsed)}` : `● Recording — ${_fmtElapsed(elapsed)}`)
    : 'Tacet';
  tray.setToolTip(tip);
  tray.setContextMenu(buildTrayMenu());
}

function createTray() {
  if (process.platform !== 'darwin') return; // menu-bar tray is macOS-only
  const icons = _getTrayIcons();
  tray = new Tray(icons.idle);
  tray.setToolTip('Tacet');
  // On macOS, left-clicking a tray item already shows the context menu via
  // setContextMenu; this click handler is a belt-and-suspenders focus action.
  tray.on('click', focusMainWindow);
  updateTray();
}

// ── Application menu ───────────────────────────────────
function buildMenu() {
  const isMac = process.platform === 'darwin';
  const template = [
    ...(isMac
      ? [
          {
            label: app.name,
            submenu: [
              { role: 'about' },
              { type: 'separator' },
              {
                label: 'Settings…',
                accelerator: 'CmdOrCtrl+,',
                click: openSettings,
              },
              {
                label: 'Edit profile…',
                click: openProfile,
              },
              { type: 'separator' },
              { role: 'hide' },
              { role: 'hideOthers' },
              { role: 'unhide' },
              { type: 'separator' },
              { role: 'quit' },
            ],
          },
        ]
      : []),
    {
      label: 'File',
      submenu: [
        ...(isMac
          ? []
          : [
              {
                label: 'Settings…',
                accelerator: 'CmdOrCtrl+,',
                click: openSettings,
              },
              {
                label: 'Edit profile…',
                click: openProfile,
              },
              { type: 'separator' },
            ]),
        { role: isMac ? 'close' : 'quit' },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { label: 'Reload app', accelerator: 'CmdOrCtrl+R', click: () => mainWindow && mainWindow.loadURL(serverUrl()) },
        {
          label: 'Restart server',
          accelerator: 'CmdOrCtrl+Shift+R',
          click: () => {
            // Surface failures so the user knows the server didn't come back (§5.4).
            restartServer().catch((err) => {
              dialog.showMessageBox(mainWindow || null, {
                type: 'error',
                title: 'Server restart failed',
                message: 'The backend server could not be restarted.',
                detail: `${String(err.message || err)}\n\nSee log for details:\n${LOG_PATH}`,
                buttons: ['OK', 'Open Log'],
                defaultId: 0,
              }).then(({ response }) => { if (response === 1) shell.openPath(LOG_PATH); });
            });
          },
        },
        { type: 'separator' },
        {
          label: 'Open server log',
          click: () => shell.openPath(LOG_PATH),
        },
        { type: 'separator' },
        { role: 'toggleDevTools' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ── IPC (settings window ↔ main) ──────────────────────
// Expose schema over IPC so the settings preload can fetch it without
// requiring('./config'), enabling sandbox:true (§5.4).
ipcMain.handle('settings:schema', () => SCHEMA);

ipcMain.handle('settings:get', () => loadConfig());

ipcMain.handle('settings:save', async (_evt, config) => {
  // Route password-type fields through safeStorage; never write them to config.json (§4.4).
  // The settings UI only sends a password field when the user explicitly typed a new value
  // (or typed an empty string to clear). An absent key means "leave existing secret alone".
  for (const key of _passwordKeys) {
    if (key in config) {
      saveSecret(key, config[key]); // saves (non-empty) or deletes (empty)
      delete config[key];           // scrub from the plaintext config object
    }
  }
  saveConfig(config);
  try {
    await restartServer();
    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err.message || err) };
  }
});

// Return which password-field secrets are currently stored, so the settings UI
// can show a "(stored securely)" placeholder instead of echoing the value.
ipcMain.handle('settings:secretsPresent', () => {
  const secrets = loadSecrets();
  return _passwordKeys.filter((k) => k in secrets);
});

ipcMain.handle('settings:pickDirectory', async () => {
  const result = await dialog.showOpenDialog(settingsWindow || mainWindow, {
    properties: ['openDirectory', 'createDirectory'],
  });
  if (result.canceled || result.filePaths.length === 0) return null;
  return result.filePaths[0];
});

ipcMain.handle('settings:openLog', () => shell.openPath(LOG_PATH));

// ── IPC (renderer meeting state → tray + power) ───────
// The main meeting UI (running in the renderer) pushes state updates here so
// the tray menu and powerSaveBlocker can reflect live meeting status without
// the main process needing its own WS client (§5.3 item 1 & 3).
ipcMain.on('meeting:state', (_evt, { active, paused, elapsed } = {}) => {
  meetingState = {
    active: Boolean(active),
    paused: Boolean(paused),
    elapsed: Number(elapsed) || 0,
  };
  updateTray();

  // powerSaveBlocker: keep the OS from suspending while recording (§5.4).
  if (meetingState.active && _powerSaveId === -1) {
    _powerSaveId = powerSaveBlocker.start('prevent-app-suspension');
    log(`powerSaveBlocker started (id=${_powerSaveId})`);
  } else if (!meetingState.active && _powerSaveId !== -1) {
    powerSaveBlocker.stop(_powerSaveId);
    log(`powerSaveBlocker stopped (id=${_powerSaveId})`);
    _powerSaveId = -1;
  }
});

// ── IPC (onboarding window ↔ main) ────────────────────
ipcMain.handle('onboarding:status', () => {
  const isMac = process.platform === 'darwin';
  return {
    mic: isMac ? systemPreferences.getMediaAccessStatus('microphone') : 'granted',
    screen: isMac ? systemPreferences.getMediaAccessStatus('screen') : 'granted',
  };
});

ipcMain.handle('onboarding:requestMic', async () => {
  if (process.platform !== 'darwin') return true;
  try {
    return await systemPreferences.askForMediaAccess('microphone');
  } catch {
    return false;
  }
});

ipcMain.handle('onboarding:openScreenSettings', async () => {
  // Screen Recording can't be granted programmatically — but the app only
  // APPEARS in that settings pane after it has attempted a capture API call.
  // Touch desktopCapturer once so macOS registers Tacet in the pane (and shows
  // its own prompt on first use), then deep-link the pane.
  try {
    const { desktopCapturer } = require('electron');
    await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: { width: 1, height: 1 } });
  } catch { /* registration attempt is best-effort */ }
  shell.openExternal(
    'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture',
  );
});

ipcMain.handle('onboarding:complete', async (_evt, data) => {
  try {
    const d = data || {};
    // 1) Provider + model/region → config.json (non-secret).
    const config = loadConfig();
    if (d.provider) {
      // Store the provider id explicitly. provider_name() treats a missing/''
      // value as 'openrouter' (the default), so onboarding always writes the
      // chosen value to avoid relying on that fallback.
      config.LLM_PROVIDER = d.provider;
    }
    if (d.provider === 'bedrock' && d.region) {
      // Region is required in both Bedrock auth modes; it selects the endpoint.
      config.AWS_REGION = d.region;
    }
    if (d.openrouterModel) config.OPENROUTER_MODEL = d.openrouterModel;
    if (d.veniceModel) config.VENICE_MODEL = d.veniceModel;
    if (d.ollamaModel) config.OLLAMA_MODEL = d.ollamaModel;
    saveConfig(config);

    // 2) API key → encrypted secrets (never config.json). For Bedrock the key is
    // the AWS_BEARER_TOKEN_BEDROCK bearer token; an empty value falls back to
    // AWS profile/SSO credentials.
    if (d.provider === 'bedrock') saveSecret('AWS_BEARER_TOKEN_BEDROCK', d.apiKey || '');
    if (d.provider === 'openrouter') saveSecret('OPENROUTER_API_KEY', d.apiKey || '');
    if (d.provider === 'venice') saveSecret('VENICE_API_KEY', d.apiKey || '');

    // 3) Discovery answers → profile.md.
    if (d.profile) {
      const p = d.profile;
      const md =
        `# My Profile\n\n## Role\n${p.role || '(not specified)'}\n\n` +
        `## Expertise\n${p.expertise || '(not specified)'}\n\n` +
        `## Organization\n${p.organization || '(not specified)'}\n\n` +
        `## Typical counterparts\n${p.counterparts || '(not specified)'}\n\n` +
        `## How I like notes\nClear, terse, factual. Lead with decisions, commitments, risks, and action items.\n\n` +
        `## Notes\n<!-- The assistant may add learnings about you here over time. -->\n`;
      fs.mkdirSync(path.dirname(PROFILE_PATH), { recursive: true });
      fs.writeFileSync(PROFILE_PATH, md, 'utf8');
    }

    markOnboarded();
    onboardingFinishing = true;
    if (onboardingWindow) onboardingWindow.close();
    await startMainApp();
    return { ok: true };
  } catch (err) {
    log(`Onboarding completion failed: ${err.message}`);
    return { ok: false, error: String(err.message || err) };
  }
});

// ── App startup ───────────────────────────────────────
async function startMainApp() {
  createMainWindow();
  try {
    await startServer();
    if (mainWindow) mainWindow.loadURL(serverUrl());
  } catch (err) {
    log(`Startup failed: ${err.message}`);
    const { response } = await dialog.showMessageBox(mainWindow, {
      type: 'error',
      title: 'Could not start Tacet',
      message: 'The backend server failed to start.',
      detail: String(err.message || err),
      buttons: ['Open Settings', 'Open Log', 'Quit'],
      defaultId: 0,
    });
    if (response === 0) openSettings();
    else if (response === 1) shell.openPath(LOG_PATH);
    else app.quit();
  }
}

async function boot() {
  buildMenu();
  ensureProfile();
  createTray(); // macOS menu-bar tray (no-op on other platforms)

  // Global shortcut ⌥⌘M: focus app if a meeting is active; start one if idle.
  // Deliberately does NOT auto-stop — stopping via shortcut would be too destructive.
  if (process.platform === 'darwin') {
    const ok = globalShortcut.register('Alt+Meta+M', () => {
      if (meetingState.active) {
        focusMainWindow();
      } else {
        focusMainWindow();
        // Small delay to let the window appear before the command arrives.
        setTimeout(() => mainWindow?.webContents.send('meeting:command', 'start'), 150);
      }
    });
    if (!ok) log('⚠️  Global shortcut Alt+Meta+M could not be registered (may be in use)');
  }

  // First launch → run onboarding; it starts the main app when finished.
  if (!isOnboarded()) {
    createOnboardingWindow();
    return;
  }
  await startMainApp();
}

// Single-instance lock: a second launch focuses the existing window.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(boot);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });
}

app.on('window-all-closed', () => {
  app.quit();
});

app.on('before-quit', () => {
  shuttingDown = true;
  killServerNow();
  globalShortcut.unregisterAll();
  if (_powerSaveId !== -1) {
    powerSaveBlocker.stop(_powerSaveId);
    _powerSaveId = -1;
  }
});

// Last-resort guards: if the main process dies for any reason, don't leak the
// server process group.
process.on('exit', killServerNow);
process.on('SIGINT', () => app.quit());
process.on('SIGTERM', () => app.quit());
