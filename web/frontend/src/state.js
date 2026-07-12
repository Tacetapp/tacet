// Shared, mutable app state. Modules import `state` and read/mutate it directly.
// Mode is *derived* from the server status broadcast — no page reload (§5.1).
export const state = {
  meetingActive: false,
  meetingPaused: false,
  chatReady: false,
  mode: 'idle',              // 'idle' | 'live' | 'review'
  meetingTitle: '',
  segments: [],              // all received transcript segments (in order)
  segByIndex: new Map(),     // segment index → segment (jump + note timestamps)
  meetingStartMs: null,      // wall-clock start, for fallback note timestamps
  pausedElapsedMs: 0,
  pauseStartedAt: null,
  startedAt: '',
  endedAt: '',
};

export function resetTranscriptState() {
  state.segments = [];
  state.segByIndex = new Map();
}

export function indexSegments(segs) {
  for (const s of segs) {
    if (s.index != null) state.segByIndex.set(s.index, s);
  }
}

// Derive the UI mode from the latest status flags.
export function deriveMode() {
  if (state.meetingActive) return 'live';
  if (state.chatReady) return 'review';
  return 'idle';
}
