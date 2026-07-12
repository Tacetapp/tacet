// Thin orchestrator: self-hosted fonts + styles, module wiring, and the few
// cross-cutting WS handlers (reset / session_loaded / meeting_info / error /
// info) that touch several panels at once. Everything else lives in src/*.
import '@fontsource/dm-sans/400.css';
import '@fontsource/dm-sans/500.css';
import '@fontsource/dm-sans/600.css';
import '@fontsource/dm-mono/400.css';
import '@fontsource/dm-mono/500.css';
import '@fontsource/newsreader/400.css';
import '@fontsource/newsreader/400-italic.css';
import './style.css';

import { showToast } from './src/util.js';
import { state, resetTranscriptState } from './src/state.js';
import { connect, onMessage } from './src/ws.js';
import { initUI, setStatusDot, resetShell } from './src/ui.js';
import { initTranscript, resetTranscript, loadSegments } from './src/transcript.js';
import { initSpeakers, clearSpeakerCards } from './src/speakers.js';
import { initNotes, resetNotes, addInsight } from './src/notes.js';
import { initChat, resetChat, addMessage } from './src/chat.js';
import { initSummary, resetSummary, showSummary } from './src/summary.js';
import { initSessions, closeSessionModal } from './src/sessions.js';
import { initVoice } from './src/voice.js';

// ── Init (register handlers) then connect ────────────────
initUI();
initTranscript();
initSpeakers();
initNotes();
initChat();
initSummary();
initSessions();
initVoice();

// ── Cross-cutting handlers ───────────────────────────────
function resetEverything() {
  resetTranscriptState();
  resetTranscript();
  resetNotes();
  resetChat();
  resetSummary();
  clearSpeakerCards();
  resetShell();
  state.meetingTitle = '';
  state.startedAt = '';
  state.endedAt = '';
}

onMessage('reset', resetEverything);

onMessage('session_loaded', ({ segments, insights, summary, chat_history, started_at, ended_at, meeting_title }) => {
  resetTranscriptState();
  resetNotes();
  resetChat();
  resetSummary();
  clearSpeakerCards();

  state.meetingTitle = meeting_title || '';
  state.startedAt = started_at || '';
  state.endedAt = ended_at || '';

  loadSegments(segments || []);
  for (const ins of insights || []) addInsight(ins);
  if (summary) showSummary(summary);

  if (chat_history && chat_history.length) {
    for (const msg of chat_history) {
      const role = msg.role === 'user' ? 'user' : 'assistant';
      const text = msg.content?.[0]?.text || '';
      if (text) addMessage(role, text);
    }
  } else {
    const title = meeting_title && meeting_title !== 'Unknown Meeting' ? meeting_title : 'this session';
    addMessage('assistant',
      `Loaded **${title}** — ${(segments || []).length} transcript segments, ${(insights || []).length} insights. Ask me anything about it.`);
  }
  closeSessionModal();
  // Mode → REVIEW arrives via the `status` message that follows this one.
});

onMessage('meeting_info', ({ title, attendees }) => {
  if (title && title !== 'Unknown Meeting') {
    state.meetingTitle = title;
    addMessage('assistant', `📅 ${title}\nAttendees: ${(attendees || []).join(', ') || 'N/A'}`);
  }
});

// Errors/info surface as toasts — the live chat drawer may be collapsed.
onMessage('error', ({ text }) => showToast(text, 'err'));
onMessage('info', ({ text }) => showToast(text, 'ok'));

connect(setStatusDot);
