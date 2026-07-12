// Transcript rendering: speaker-turn blocks, stick-to-bottom "↓ Live" pill,
// jump-to-segment, and inline speaker rename on cluster labels (§5.2/§5.3).
import { $, isNearBottom } from './util.js';
import { state, indexSegments } from './state.js';
import { onMessage } from './ws.js';
import { openSpeakerPopover, resolveClusters } from './speakers.js';

const CLUSTER_RE = /^Speaker \d+$/;   // matches backend _CLUSTER_RE

let transcriptContent, livePill;

function isClusterLabel(name) {
  return !!name && CLUSTER_RE.test(name);
}

// Build a turn block for a speaker. Cluster labels ("Speaker N") are clickable
// to open the inline rename popover.
function makeTurnBlock(speaker) {
  const block = document.createElement('div');
  block.className = 'turn' + (speaker === 'You' ? ' turn-you' : '');
  block.dataset.speaker = speaker || '';
  if (speaker) {
    const label = document.createElement('div');
    label.className = 'speaker-label' + (speaker === 'You' ? ' speaker-you' : '');
    label.textContent = speaker;
    if (isClusterLabel(speaker)) {
      label.classList.add('speaker-renamable');
      label.title = 'Click to name this speaker';
      label.addEventListener('click', (e) => {
        e.stopPropagation();
        openSpeakerPopover(speaker, label);
      });
    }
    block.appendChild(label);
  }
  return block;
}

// Append new segments, extending the last turn block when the speaker matches.
export function appendSegments(newSegs) {
  if (!newSegs || newSegs.length === 0) return;
  const stick = isNearBottom(transcriptContent);
  clearEmpty();
  indexSegments(newSegs);
  let lastBlock = transcriptContent.lastElementChild;
  for (const seg of newSegs) {
    state.segments.push(seg);
    const speaker = seg.speaker || '';
    if (!lastBlock || !lastBlock.classList.contains('turn') || lastBlock.dataset.speaker !== speaker) {
      lastBlock = makeTurnBlock(speaker);
      transcriptContent.appendChild(lastBlock);
    }
    const span = document.createElement('span');
    span.className = 'seg seg-new';
    if (seg.index) span.dataset.segIndex = seg.index;
    span.textContent = seg.text + ' ';
    lastBlock.appendChild(span);
  }
  if (stick) {
    transcriptContent.scrollTop = transcriptContent.scrollHeight;
    if (livePill) livePill.hidden = true;
  } else if (livePill) {
    livePill.hidden = false;
  }
}

// Full rebuild — used when segment speakers change retroactively / on load.
export function renderTranscript() {
  transcriptContent.innerHTML = '';
  if (state.segments.length === 0) {
    showEmpty();
    return;
  }
  let currentSpeaker = Symbol('none');
  let currentBlock = null;
  for (const seg of state.segments) {
    const speaker = seg.speaker || '';
    if (speaker !== currentSpeaker) {
      currentBlock = makeTurnBlock(speaker);
      transcriptContent.appendChild(currentBlock);
      currentSpeaker = speaker;
    }
    const span = document.createElement('span');
    span.className = 'seg';
    if (seg.index) span.dataset.segIndex = seg.index;
    span.textContent = seg.text + ' ';
    currentBlock.appendChild(span);
  }
  transcriptContent.scrollTop = transcriptContent.scrollHeight;
}

export function resetTranscript() {
  showEmpty();
}

// Load a full segment set (session load): replace state + re-render.
export function loadSegments(segs) {
  state.segments = (segs || []).slice();
  state.segByIndex = new Map();
  indexSegments(state.segments);
  renderTranscript();
}

function showEmpty() {
  transcriptContent.innerHTML =
    '<p class="empty-state">Transcript will appear here when the meeting starts…</p>';
}

function clearEmpty() {
  transcriptContent.querySelector('.empty-state')?.remove();
}

// Scroll a transcript segment into view and flash it. Reused by notes + speakers.
export function jumpToSegment(index) {
  if (index == null) return;
  const span = transcriptContent.querySelector(`[data-seg-index="${index}"]`);
  if (!span) return;
  // In review mode the transcript lives behind a tab — surface it first.
  document.dispatchEvent(new CustomEvent('show-transcript-tab'));
  span.scrollIntoView({ behavior: 'smooth', block: 'center' });
  span.classList.add('seg-flash');
  setTimeout(() => span.classList.remove('seg-flash'), 1500);
}

export function initTranscript() {
  transcriptContent = $('#transcript-content');
  livePill = $('#transcript-live-pill');

  if (livePill) {
    livePill.addEventListener('click', () => {
      transcriptContent.scrollTop = transcriptContent.scrollHeight;
      livePill.hidden = true;
    });
  }
  transcriptContent.addEventListener('scroll', () => {
    if (isNearBottom(transcriptContent) && livePill) livePill.hidden = true;
  });

  // Speakers rail dispatches this (avoids a circular import).
  document.addEventListener('jump-to-segment', (e) => jumpToSegment(e.detail));

  onMessage('transcript', ({ segments }) => appendSegments(segments));

  onMessage('speaker_update', ({ updates }) => {
    if (!updates) return;
    const byIndex = new Map(updates.map((u) => [u.index, u.speaker]));
    for (const seg of state.segments) {
      if (byIndex.has(seg.index)) seg.speaker = byIndex.get(seg.index);
    }
    renderTranscript();
    // Let the speaker rail dismiss any card whose cluster just got a real name.
    resolveClusters(new Set(updates.map((u) => u.speaker)));
  });
}
