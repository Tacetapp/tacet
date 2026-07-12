// Voice-profile calibration: the settings gear menu plus the record/encode/save
// modal. Records mic audio, resamples to 16 kHz mono locally, and ships PCM to
// the server for embedding (never leaves as audio beyond this one enrollment).
import { $ } from './util.js';
import { send, onMessage, onOpen, wsOpen } from './ws.js';

const VOICE_TARGET_DURATION_S = 10;
const VOICE_SAMPLE_RATE = 16000;

let btnSettings, settingsMenu, voiceStatusEl, btnVoiceSetup, btnVoiceDelete;
let voiceModal, voiceRecordBtn, voiceRerecordBtn, voiceSaveBtn, voicePlayback;
let voiceProgress, voiceTimer, voiceModalError, voiceModalCloseBtn, voiceModalBackdrop;

// Persistent speaker registry ("known voices", §7). Opt-in on the server; the
// whole UI stays hidden until a speaker_registry message reports enabled:true.
let knownVoicesStatusEl, btnKnownVoicesManage, knownVoicesModal, knownVoicesList;
let btnKnownVoicesClear, knownVoicesModalClose, knownVoicesModalBackdrop;
let knownVoicesEnabled = false;

let voiceMediaStream = null, voiceMediaRecorder = null, voiceAudioChunks = [];
let voicePcm = null, voiceRecordStartedAt = 0, voiceRecordTimerId = null;
let voiceAutoStopTimerId = null, voiceRecording = false;

function toggleSettingsMenu(force) {
  if (!settingsMenu || !btnSettings) return;
  const open = force !== undefined ? force : settingsMenu.classList.contains('hidden');
  settingsMenu.classList.toggle('hidden', !open);
  btnSettings.setAttribute('aria-expanded', String(open));
}

function updateVoiceProfileStatus({ exists, created_at }) {
  if (!voiceStatusEl) return;
  if (exists) {
    const when = created_at ? ` · ${new Date(created_at).toLocaleDateString()}` : '';
    voiceStatusEl.textContent = `Voice profile: Active${when}`;
    btnVoiceSetup.textContent = 'Re-record voice profile…';
    btnVoiceDelete.style.display = '';
  } else {
    voiceStatusEl.textContent = 'Voice profile: Not set';
    btnVoiceSetup.textContent = 'Set up voice profile…';
    btnVoiceDelete.style.display = 'none';
  }
}

// ── Known voices (persistent speaker registry) ─────────────────────────────

// Reflect a speaker_registry status payload {enabled, people:[{name,
// updated_at, meeting_count}]}. The status row + manage item stay hidden
// entirely while the feature is disabled server-side (opt-in privacy gate).
function renderSpeakerRegistry({ enabled, people }) {
  knownVoicesEnabled = !!enabled;
  const list = Array.isArray(people) ? people : [];
  if (!knownVoicesEnabled) {
    knownVoicesStatusEl?.classList.add('hidden');
    btnKnownVoicesManage?.classList.add('hidden');
    if (knownVoicesModal && !knownVoicesModal.classList.contains('hidden')) {
      knownVoicesModal.classList.add('hidden');
    }
    return;
  }
  if (knownVoicesStatusEl) {
    knownVoicesStatusEl.textContent = `Known voices: ${list.length}`;
    knownVoicesStatusEl.classList.remove('hidden');
  }
  btnKnownVoicesManage?.classList.remove('hidden');
  renderKnownVoicesList(list);
}

function renderKnownVoicesList(people) {
  if (!knownVoicesList) return;
  knownVoicesList.textContent = '';
  if (!people.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = 'No known voices yet.';
    knownVoicesList.appendChild(empty);
    return;
  }
  for (const person of people) {
    const row = document.createElement('div');
    row.className = 'known-voice-row';

    const info = document.createElement('div');
    info.className = 'known-voice-info';
    const name = document.createElement('span');
    name.className = 'known-voice-name';
    name.textContent = person.name || '(unnamed)';
    const meta = document.createElement('span');
    meta.className = 'known-voice-meta';
    const meetings = Number(person.meeting_count) || 0;
    const parts = [`${meetings} meeting${meetings === 1 ? '' : 's'}`];
    if (person.updated_at) {
      const d = new Date(person.updated_at);
      if (!Number.isNaN(d.getTime())) parts.push(d.toLocaleDateString());
    }
    meta.textContent = parts.join(' · ');
    info.appendChild(name);
    info.appendChild(meta);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn btn-small known-voice-delete';
    del.textContent = 'Delete';
    del.addEventListener('click', () => {
      if (!confirm(`Forget ${person.name || 'this voice'}?`)) return;
      send({ type: 'speaker_registry_delete', name: person.name });
    });

    row.appendChild(info);
    row.appendChild(del);
    knownVoicesList.appendChild(row);
  }
}

function openKnownVoicesModal() {
  if (!knownVoicesModal) return;
  if (wsOpen()) send({ type: 'speaker_registry_status' });
  knownVoicesModal.classList.remove('hidden');
}
function closeKnownVoicesModal() { knownVoicesModal?.classList.add('hidden'); }

function openVoiceModal() { resetVoiceModal(); voiceModal.classList.remove('hidden'); }
function closeVoiceModal() {
  if (voiceRecording) stopVoiceRecording({ skipTimer: true });
  stopMediaStream();
  resetVoiceModal();
  voiceModal.classList.add('hidden');
}

function resetVoiceModal() {
  voiceRecording = false;
  voicePcm = null;
  voiceAudioChunks = [];
  voiceRecordBtn.disabled = false;
  voiceRecordBtn.classList.remove('recording');
  voiceRecordBtn.querySelector('.voice-record-label').textContent = 'Record';
  voiceRerecordBtn.disabled = true;
  voiceSaveBtn.disabled = true;
  voiceSaveBtn.textContent = 'Save';
  voiceProgress.style.width = '0%';
  voiceTimer.textContent = `00:00 / 00:${String(VOICE_TARGET_DURATION_S).padStart(2, '0')}`;
  voicePlayback.classList.add('hidden');
  voicePlayback.src = '';
  hideVoiceModalError();
  clearInterval(voiceRecordTimerId); voiceRecordTimerId = null;
  clearTimeout(voiceAutoStopTimerId); voiceAutoStopTimerId = null;
}

function showVoiceModalError(msg) {
  if (!voiceModalError) return;
  voiceModalError.textContent = msg;
  voiceModalError.classList.remove('hidden');
}
function hideVoiceModalError() {
  if (!voiceModalError) return;
  voiceModalError.classList.add('hidden');
  voiceModalError.textContent = '';
}

function setVoiceSaving(saving) {
  voiceSaveBtn.disabled = saving || !voicePcm;
  voiceSaveBtn.textContent = saving ? 'Saving…' : 'Save';
  voiceRerecordBtn.disabled = saving || !voicePcm;
  voiceRecordBtn.disabled = saving;
}

async function startVoiceRecording() {
  hideVoiceModalError();
  try {
    voiceMediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    showVoiceModalError(`Microphone access denied (${err.name}). Allow access and try again.`);
    return;
  }
  voiceAudioChunks = [];
  const mimeType = pickSupportedMime();
  try {
    voiceMediaRecorder = mimeType
      ? new MediaRecorder(voiceMediaStream, { mimeType })
      : new MediaRecorder(voiceMediaStream);
  } catch (err) {
    stopMediaStream();
    showVoiceModalError(`Recorder init failed: ${err.message}`);
    return;
  }
  voiceMediaRecorder.ondataavailable = (ev) => { if (ev.data && ev.data.size > 0) voiceAudioChunks.push(ev.data); };
  voiceMediaRecorder.onstop = async () => {
    clearInterval(voiceRecordTimerId); clearTimeout(voiceAutoStopTimerId);
    voiceRecordTimerId = null; voiceAutoStopTimerId = null;
    stopMediaStream();
    voiceProgress.style.width = '100%';
    voiceRecordBtn.classList.remove('recording');
    voiceRecordBtn.querySelector('.voice-record-label').textContent = 'Record';
    voiceRecording = false;
    if (!voiceAudioChunks.length) { showVoiceModalError('No audio captured — please try again.'); return; }
    const blob = new Blob(voiceAudioChunks, { type: voiceMediaRecorder?.mimeType || 'audio/webm' });
    voicePlayback.src = URL.createObjectURL(blob);
    voicePlayback.classList.remove('hidden');
    try { voicePcm = await decodeTo16kMono(blob); }
    catch (err) { showVoiceModalError(`Couldn't process recording: ${err.message}`); voicePcm = null; return; }
    voiceSaveBtn.disabled = false;
    voiceRerecordBtn.disabled = false;
  };
  voiceMediaRecorder.start();
  voiceRecording = true;
  voiceRecordStartedAt = Date.now();
  voiceRecordBtn.classList.add('recording');
  voiceRecordBtn.querySelector('.voice-record-label').textContent = 'Stop';
  voicePlayback.classList.add('hidden');
  voicePcm = null;
  voiceSaveBtn.disabled = true;
  voiceRerecordBtn.disabled = true;
  voiceRecordTimerId = setInterval(() => {
    const elapsed = Math.min(VOICE_TARGET_DURATION_S, (Date.now() - voiceRecordStartedAt) / 1000);
    voiceProgress.style.width = `${(elapsed / VOICE_TARGET_DURATION_S) * 100}%`;
    const mm = String(Math.floor(elapsed / 60)).padStart(2, '0');
    const ss = String(Math.floor(elapsed % 60)).padStart(2, '0');
    voiceTimer.textContent = `${mm}:${ss} / 00:${String(VOICE_TARGET_DURATION_S).padStart(2, '0')}`;
  }, 100);
  voiceAutoStopTimerId = setTimeout(() => stopVoiceRecording(), VOICE_TARGET_DURATION_S * 1000);
}

function stopVoiceRecording({ skipTimer = false } = {}) {
  if (!voiceMediaRecorder) return;
  try { if (voiceMediaRecorder.state !== 'inactive') voiceMediaRecorder.stop(); } catch (_) {}
  if (skipTimer) {
    clearInterval(voiceRecordTimerId); clearTimeout(voiceAutoStopTimerId);
    voiceRecordTimerId = null; voiceAutoStopTimerId = null;
  }
}

function stopMediaStream() {
  if (!voiceMediaStream) return;
  for (const t of voiceMediaStream.getTracks()) { try { t.stop(); } catch (_) {} }
  voiceMediaStream = null;
}

function pickSupportedMime() {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
  for (const m of candidates) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported?.(m)) return m;
  }
  return '';
}

async function decodeTo16kMono(blob) {
  const arrayBuf = await blob.arrayBuffer();
  const tmpCtx = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await tmpCtx.decodeAudioData(arrayBuf);
  tmpCtx.close?.();
  const channelData = decoded.numberOfChannels === 1 ? decoded.getChannelData(0) : mixToMono(decoded);
  const targetLength = Math.ceil(channelData.length * VOICE_SAMPLE_RATE / decoded.sampleRate);
  const offline = new OfflineAudioContext(1, targetLength, VOICE_SAMPLE_RATE);
  const srcBuffer = offline.createBuffer(1, channelData.length, decoded.sampleRate);
  srcBuffer.copyToChannel(channelData, 0);
  const src = offline.createBufferSource();
  src.buffer = srcBuffer;
  src.connect(offline.destination);
  src.start(0);
  const rendered = await offline.startRendering();
  return rendered.getChannelData(0).slice();
}

function mixToMono(audioBuffer) {
  const n = audioBuffer.length;
  const out = new Float32Array(n);
  for (let ch = 0; ch < audioBuffer.numberOfChannels; ch++) {
    const d = audioBuffer.getChannelData(ch);
    for (let i = 0; i < n; i++) out[i] += d[i];
  }
  for (let i = 0; i < n; i++) out[i] /= audioBuffer.numberOfChannels;
  return out;
}

function pcmToBase64(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function saveVoiceProfile() {
  if (!voicePcm) return;
  setVoiceSaving(true);
  send({ type: 'voice_profile_save', audio: pcmToBase64(voicePcm), sample_rate: VOICE_SAMPLE_RATE });
}

export function initVoice() {
  btnSettings = $('#btn-settings');
  settingsMenu = $('#settings-menu');
  voiceStatusEl = $('#voice-profile-status');
  btnVoiceSetup = $('#btn-voice-profile-setup');
  btnVoiceDelete = $('#btn-voice-profile-delete');
  voiceModal = $('#voice-modal');
  voiceRecordBtn = $('#voice-record-btn');
  voiceRerecordBtn = $('#voice-rerecord-btn');
  voiceSaveBtn = $('#voice-save-btn');
  voicePlayback = $('#voice-playback');
  voiceProgress = $('#voice-progress');
  voiceTimer = $('#voice-timer');
  voiceModalError = $('#voice-modal-error');
  voiceModalCloseBtn = $('#voice-modal-close');
  voiceModalBackdrop = voiceModal?.querySelector('.modal-backdrop');

  knownVoicesStatusEl = $('#known-voices-status');
  btnKnownVoicesManage = $('#btn-known-voices-manage');
  knownVoicesModal = $('#known-voices-modal');
  knownVoicesList = $('#known-voices-list');
  btnKnownVoicesClear = $('#btn-known-voices-clear');
  knownVoicesModalClose = $('#known-voices-modal-close');
  knownVoicesModalBackdrop = knownVoicesModal?.querySelector('.modal-backdrop');

  onMessage('voice_profile', (payload) => {
    updateVoiceProfileStatus(payload);
    if (payload.exists && !voiceModal.classList.contains('hidden') && voicePcm) closeVoiceModal();
    setVoiceSaving(false);
  });
  onMessage('voice_profile_error', ({ text }) => {
    showVoiceModalError(text);
    setVoiceSaving(false);
  });
  onMessage('speaker_registry', renderSpeakerRegistry);

  btnSettings?.addEventListener('click', (e) => {
    e.stopPropagation();
    const willOpen = settingsMenu?.classList.contains('hidden');
    toggleSettingsMenu();
    // Refresh the known-voices count each time the menu opens so it reflects
    // reality (and reveals the UI once the feature is turned on server-side).
    if (willOpen && wsOpen()) send({ type: 'speaker_registry_status' });
  });
  document.addEventListener('click', (e) => {
    if (!settingsMenu || settingsMenu.classList.contains('hidden')) return;
    if (!settingsMenu.contains(e.target) && e.target !== btnSettings) toggleSettingsMenu(false);
  });

  btnVoiceSetup?.addEventListener('click', () => { toggleSettingsMenu(false); openVoiceModal(); });
  btnVoiceDelete?.addEventListener('click', () => {
    toggleSettingsMenu(false);
    if (!confirm('Delete your voice profile? Mic bleed filtering will stop.')) return;
    send({ type: 'voice_profile_delete' });
  });

  btnKnownVoicesManage?.addEventListener('click', () => { toggleSettingsMenu(false); openKnownVoicesModal(); });
  knownVoicesModalClose?.addEventListener('click', closeKnownVoicesModal);
  knownVoicesModalBackdrop?.addEventListener('click', closeKnownVoicesModal);
  btnKnownVoicesClear?.addEventListener('click', () => {
    if (!confirm('Forget all known voices? This deletes every stored voice fingerprint.')) return;
    send({ type: 'speaker_registry_clear' });
  });

  voiceModalCloseBtn?.addEventListener('click', closeVoiceModal);
  voiceModalBackdrop?.addEventListener('click', closeVoiceModal);
  voiceRecordBtn?.addEventListener('click', () => { voiceRecording ? stopVoiceRecording() : startVoiceRecording(); });
  voiceRerecordBtn?.addEventListener('click', resetVoiceModal);
  voiceSaveBtn?.addEventListener('click', () => {
    if (!voicePcm) return;
    if (voicePcm.length / VOICE_SAMPLE_RATE < 5) {
      showVoiceModalError('Recording is too short. Please record at least 5 seconds.');
      return;
    }
    saveVoiceProfile();
  });

  // Ask for current status after each (re)connect so the menu reflects reality.
  onOpen(() => setTimeout(() => {
    if (!wsOpen()) return;
    send({ type: 'voice_profile_status' });
    // Reveals/hides the known-voices UI based on the server-side opt-in gate.
    send({ type: 'speaker_registry_status' });
  }, 500));
}
