// App shell: derives the UI mode from the server `status` broadcast and lays out
// the three movable panels (transcript, live-notes, chat) accordingly — LIVE,
// REVIEW, or IDLE (§5.1). Also owns the header state control, the recording
// timer + title beacon, review tabs, capture status, auth + auto-stop banners,
// and the main↔rail resize divider. No page reloads — just a body class + a few
// appendChild moves.
import { $, showToast } from './util.js';
import { state, deriveMode } from './state.js';
import { send, onMessage } from './ws.js';
import { refreshChatEnabled } from './chat.js';

let statusDot, timerEl, btnStart, recordingChip, reviewMeta, btnPause, btnStop, btnNew;
let meetingTitleEl, meetingDurationEl;
let workspace, idleView, liveTranscriptHost, liveChatHost, tabTranscript, tabNotes, rightRail;
let transcriptPanel, notesPanel, chatEl;
let captureStatusEl, captureDot, captureLabel, captureTarget;

let timerInterval = null;
let currentMode = null;
let autoStopTimer = null;
let lastElapsedMs = 0;
let _desktopStateInterval = null; // 10 s tick while live → tray bridge

// ── Connection dot ───────────────────────────────────────
export function setStatusDot(kind) {
  if (!statusDot) return;
  statusDot.className = `status-dot ${kind}`;
  statusDot.title = kind === 'online' ? 'Connected' : 'Disconnected';
}

// ── Timer + title beacon ─────────────────────────────────
function updateTimer() {
  if (!state.meetingStartMs) return;
  const nowPausedMs = state.pauseStartedAt ? Date.now() - state.pauseStartedAt : 0;
  lastElapsedMs = Math.max(0, Date.now() - state.meetingStartMs - state.pausedElapsedMs - nowPausedMs);
  const s = Math.floor(lastElapsedMs / 1000);
  const h = String(Math.floor(s / 3600)).padStart(2, '0');
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const sec = String(s % 60).padStart(2, '0');
  timerEl.textContent = `${h}:${m}:${sec}`;
}

function updateTitle() {
  if (!state.meetingActive) { document.title = 'Tacet'; return; }
  const raw = timerEl.textContent || '00:00:00';
  const parts = raw.split(':');
  const mmss = parts.length === 3 ? `${parts[1]}:${parts[2]}` : raw;
  document.title = state.meetingPaused ? `⏸ ${mmss}` : `● ${mmss}`;
}

function startTimerLoop() {
  if (timerInterval) return;
  timerInterval = setInterval(() => { updateTimer(); updateTitle(); }, 1000);
}
function stopTimerLoop() { clearInterval(timerInterval); timerInterval = null; }

function reviewDurationText() {
  if (state.startedAt && state.endedAt) {
    const ms = new Date(state.endedAt) - new Date(state.startedAt);
    if (ms > 0) return `${Math.max(1, Math.round(ms / 60000))} min`;
  }
  if (lastElapsedMs > 0) return `${Math.max(1, Math.round(lastElapsedMs / 60000))} min`;
  return '';
}

// ── Mode layout ──────────────────────────────────────────
function applyMode(mode) {
  if (mode === currentMode) return;
  currentMode = mode;
  document.body.classList.remove('mode-idle', 'mode-live', 'mode-review');
  document.body.classList.add(`mode-${mode}`);

  if (mode === 'idle') {
    workspace.hidden = true;
    idleView.hidden = false;
    return;
  }
  idleView.hidden = true;
  workspace.hidden = false;

  if (mode === 'live') {
    liveTranscriptHost.appendChild(transcriptPanel);
    liveChatHost.appendChild(chatEl);
    rightRail.appendChild(notesPanel);
  } else { // review
    tabTranscript.appendChild(transcriptPanel);
    tabNotes.appendChild(notesPanel);
    rightRail.appendChild(chatEl);
    showTab('summary');
  }
}

// ── Review tabs ──────────────────────────────────────────
export function showTab(name) {
  for (const btn of document.querySelectorAll('.review-tab')) {
    btn.classList.toggle('active', btn.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll('.tab-panel')) {
    panel.classList.toggle('active', panel.id === `tab-${name}`);
  }
}

// ── Header state control ─────────────────────────────────
function updateHeader(mode) {
  btnStart.style.display = mode === 'idle' ? '' : 'none';
  recordingChip.hidden = mode !== 'live';
  reviewMeta.hidden = mode !== 'review';

  if (mode === 'live') {
    btnPause.textContent = state.meetingPaused ? '▶' : '⏸';
    btnPause.title = state.meetingPaused ? 'Resume' : 'Pause';
    recordingChip.classList.toggle('paused', state.meetingPaused);
  }
  if (mode === 'review') {
    const title = state.meetingTitle && state.meetingTitle !== 'Unknown Meeting'
      ? state.meetingTitle : 'Meeting';
    meetingTitleEl.textContent = title;
    const dur = reviewDurationText();
    meetingDurationEl.textContent = dur ? `· ${dur}` : '';
  }
  document.body.classList.toggle('meeting-paused', state.meetingActive && state.meetingPaused);
}

// ── Desktop bridge (Electron tray + powerSaveBlocker) ────
// Sends meeting state to the Electron main process via contextBridge.
// window.desktop is only defined inside Electron; the guard keeps the plain-
// browser flow unaffected.
function pushMeetingState() {
  if (typeof window.desktop?.setMeetingState !== 'function') return;
  window.desktop.setMeetingState({
    active: state.meetingActive,
    paused: state.meetingPaused,
    elapsed: lastElapsedMs,
  });
}

// ── status handler ───────────────────────────────────────
function onStatus({ meeting_active, paused, chat_ready }) {
  const wasPaused = state.meetingPaused;
  state.meetingActive = meeting_active;
  state.meetingPaused = Boolean(paused);
  state.chatReady = chat_ready;

  if (state.meetingPaused && !wasPaused) {
    state.pauseStartedAt = Date.now();
  } else if (!state.meetingPaused && wasPaused && state.pauseStartedAt) {
    state.pausedElapsedMs += Date.now() - state.pauseStartedAt;
    state.pauseStartedAt = null;
  }

  if (meeting_active && !state.meetingStartMs) state.meetingStartMs = Date.now();
  if (!meeting_active) {
    state.pausedElapsedMs = 0;
    state.pauseStartedAt = null;
    hideAutoStopBanner();
  }

  const mode = deriveMode();
  state.mode = mode;
  applyMode(mode);
  updateHeader(mode);
  refreshChatEnabled();

  timerEl.classList.toggle('active', mode === 'live');
  if (mode === 'live' && !state.meetingPaused) { startTimerLoop(); updateTimer(); }
  else { stopTimerLoop(); if (mode === 'live') updateTimer(); }
  updateTitle();

  // Push state to Electron main process (tray tooltip/menu + powerSaveBlocker).
  // updateTimer() above refreshes lastElapsedMs, so call pushMeetingState after.
  pushMeetingState();
  if (meeting_active && !_desktopStateInterval) {
    // 10 s periodic tick so elapsed time in the tray tooltip stays fresh.
    _desktopStateInterval = setInterval(pushMeetingState, 10000);
  } else if (!meeting_active && _desktopStateInterval) {
    clearInterval(_desktopStateInterval);
    _desktopStateInterval = null;
  }
}

// Called by main.js on `reset` — return to a clean idle shell.
export function resetShell() {
  stopTimerLoop();
  state.meetingStartMs = null;
  state.pausedElapsedMs = 0;
  state.pauseStartedAt = null;
  state.meetingPaused = false;
  lastElapsedMs = 0;
  timerEl.textContent = '00:00:00';
  timerEl.classList.remove('active');
  hideAutoStopBanner();
  captureStatusEl.classList.add('hidden');
  captureTarget.classList.add('hidden');
  captureTarget.innerHTML = '<option value="display">Entire Screen</option>';
  document.body.classList.remove('meeting-paused');
  updateTitle();
}

// ── Capture status + target ──────────────────────────────
function onCaptureStatus({ capturing, system_audio, mic }) {
  if (capturing) {
    captureStatusEl.classList.remove('hidden');
    captureDot.className = 'capture-dot active';
    const parts = [];
    if (system_audio) parts.push('System Audio');
    if (mic) parts.push('Mic');
    captureLabel.textContent = parts.join(' + ') || 'Capturing';
  } else {
    captureDot.className = 'capture-dot';
    captureLabel.textContent = 'Stopped';
  }
}

function onCaptureApps({ apps }) {
  const prevValue = captureTarget.value;
  captureTarget.innerHTML = '<option value="display">Entire Screen</option>';
  for (const app of apps) {
    if (!app.name) continue;
    const opt = document.createElement('option');
    opt.value = app.bundle_id;
    opt.textContent = app.name;
    captureTarget.appendChild(opt);
  }
  if (prevValue && prevValue !== 'display') captureTarget.value = prevValue;
  captureTarget.classList.remove('hidden');
}

// ── Auth banner (DOM-safe; §4.2) ─────────────────────────
const _authDismissed = new Set();
function updateAuthBanner(sso) {
  const banner = $('#auth-banner');
  if (!banner) return;
  const items = [];
  for (const [key, info] of [['sso', sso]]) {
    if (!info) continue;
    if (info.ok && _authDismissed.has(key)) _authDismissed.delete(key);
    if (!info.ok && !_authDismissed.has(key)) items.push({ key, message: info.message, command: info.command });
  }
  banner.innerHTML = '';
  if (!items.length) { banner.classList.add('hidden'); return; }
  for (const { key, message, command } of items) {
    const itemDiv = document.createElement('div');
    itemDiv.className = 'auth-item';
    itemDiv.dataset.authKey = key;
    const icon = document.createElement('span');
    icon.className = 'auth-icon';
    icon.textContent = '⚠';
    itemDiv.appendChild(icon);
    const msgSpan = document.createElement('span');
    msgSpan.textContent = message;
    itemDiv.appendChild(msgSpan);
    if (command) {
      const cmdSpan = document.createElement('span');
      cmdSpan.appendChild(document.createTextNode('— run '));
      const code = document.createElement('code');
      code.textContent = command;
      cmdSpan.appendChild(code);
      itemDiv.appendChild(cmdSpan);
    }
    const dismiss = document.createElement('button');
    dismiss.className = 'auth-dismiss';
    dismiss.title = 'Dismiss';
    dismiss.textContent = '×';
    dismiss.addEventListener('click', () => {
      _authDismissed.add(key);
      itemDiv.remove();
      if (!banner.querySelector('.auth-item')) banner.classList.add('hidden');
    });
    itemDiv.appendChild(dismiss);
    banner.appendChild(itemDiv);
  }
  banner.classList.remove('hidden');
}

// ── Auto-stop banner ─────────────────────────────────────
function reasonToText(reason) {
  switch (reason) {
    case 'inactivity': return 'no audio activity';
    case 'calendar_end': return 'meeting past calendar end time';
    case 'websocket_closed': return 'browser closed';
    case 'capture_error': return 'screen/audio capture stopped';
    default: return reason || 'inactive';
  }
}
function showAutoStopBanner(reason, secondsUntil) {
  const banner = $('#autostop-banner');
  if (!banner) return;
  const textEl = banner.querySelector('.autostop-text');
  let remaining = Math.max(1, secondsUntil || 30);
  const label = reasonToText(reason);
  const render = () => { textEl.textContent = `Auto-stopping in ${remaining}s — ${label}.`; };
  render();
  banner.classList.remove('hidden');
  clearInterval(autoStopTimer);
  autoStopTimer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) { clearInterval(autoStopTimer); autoStopTimer = null; return; }
    render();
  }, 1000);
}
function hideAutoStopBanner() {
  const banner = $('#autostop-banner');
  if (!banner) return;
  banner.classList.add('hidden');
  clearInterval(autoStopTimer);
  autoStopTimer = null;
}

// ── Resize divider (main column ↔ right rail) ────────────
function initResize() {
  const handle = $('#v-resize');
  if (!handle) return;
  let startX, startWidth;
  const onMove = (e) => {
    const delta = startX - e.clientX;              // drag left → wider rail
    rightRail.style.width = Math.max(240, Math.min(680, startWidth + delta)) + 'px';
    rightRail.style.flex = 'none';
  };
  const onUp = () => {
    handle.classList.remove('active');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  };
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    handle.classList.add('active');
    startX = e.clientX;
    startWidth = rightRail.offsetWidth;
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'ew-resize';
    document.body.style.userSelect = 'none';
  });
}

export function initUI() {
  statusDot = $('#status-dot');
  timerEl = $('#timer');
  btnStart = $('#btn-start');
  recordingChip = $('#recording-chip');
  reviewMeta = $('#review-meta');
  btnPause = $('#btn-pause');
  btnStop = $('#btn-stop');
  btnNew = $('#btn-new');
  meetingTitleEl = $('#meeting-title');
  meetingDurationEl = $('#meeting-duration');

  workspace = $('#workspace');
  idleView = $('#idle-view');
  liveTranscriptHost = $('#live-transcript-host');
  liveChatHost = $('#live-chat-host');
  tabTranscript = $('#tab-transcript');
  tabNotes = $('#tab-notes');
  rightRail = $('#right-rail');
  transcriptPanel = $('#transcript-panel');
  notesPanel = $('#notes-panel');
  chatEl = $('#chat');

  captureStatusEl = $('#capture-status');
  captureDot = $('#capture-dot');
  captureLabel = $('#capture-label');
  captureTarget = $('#capture-target');

  // Header controls
  const startMeeting = () => send({ type: 'start' });
  btnStart.addEventListener('click', startMeeting);
  $('#btn-start-idle')?.addEventListener('click', startMeeting);
  btnStop.addEventListener('click', () => {
    send({ type: 'stop' });
    showToast('Ending meeting — generating summary…', 'ok');
  });
  btnPause.addEventListener('click', () => send({ type: state.meetingPaused ? 'resume' : 'pause' }));
  btnNew.addEventListener('click', () => send({ type: 'new_meeting' }));

  captureTarget.addEventListener('change', () => {
    send({ type: 'set_capture_target', bundle_id: captureTarget.value });
  });

  // Review tabs
  for (const btn of document.querySelectorAll('.review-tab')) {
    btn.addEventListener('click', () => showTab(btn.dataset.tab));
  }
  document.addEventListener('show-transcript-tab', () => {
    if (state.mode === 'review') showTab('transcript');
  });

  // Auto-stop banner buttons
  const asBanner = $('#autostop-banner');
  asBanner?.querySelector('.btn-autostop-undo')?.addEventListener('click', () => {
    send({ type: 'auto_stop_undo' });
    hideAutoStopBanner();
  });
  asBanner?.querySelector('.btn-autostop-dismiss')?.addEventListener('click', hideAutoStopBanner);

  // WS handlers owned by the shell
  onMessage('status', onStatus);
  onMessage('capture_status', onCaptureStatus);
  onMessage('capture_apps', onCaptureApps);
  onMessage('auth_status', ({ sso }) => updateAuthBanner(sso));
  onMessage('auto_stop_pending', ({ reason, seconds_until }) => showAutoStopBanner(reason, seconds_until));
  onMessage('auto_stop_cancelled', hideAutoStopBanner);
  onMessage('auto_stop_fired', ({ reason }) => {
    hideAutoStopBanner();
    showToast(`Meeting auto-stopped: ${reasonToText(reason)}. Generating summary…`, 'ok');
  });

  // Receive commands from the Electron tray menu and global shortcut.
  // window.desktop is undefined in the plain-browser flow — guard before use.
  if (typeof window.desktop?.onMeetingCommand === 'function') {
    window.desktop.onMeetingCommand((cmd) => {
      if (cmd === 'start') {
        send({ type: 'start' });
      } else if (cmd === 'pause') {
        send({ type: 'pause' });
      } else if (cmd === 'resume') {
        send({ type: 'resume' });
      } else if (cmd === 'stop') {
        send({ type: 'stop' });
        showToast('Ending meeting — generating summary…', 'ok');
      }
    });
  }

  initResize();
  applyMode('idle');
}
