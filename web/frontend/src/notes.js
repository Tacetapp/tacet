// "Live notes" feed (§5.2): one chronological stream, newest on top.
// - alert insights → full cards (chip + mm:ss + jump)
// - note insights  → compact rows that expand on click
// - suggested_question → ephemeral "Worth asking" slot (max 2, newest wins)
// - term → collapsed Glossary drawer; visual → collapsed Screen drawer
// - ~10 visible behind an "earlier ⌄" expander; unread dot; no scroll yank.
import { $, srtToSeconds, fmtClock } from './util.js';
import { state } from './state.js';
import { onMessage } from './ws.js';
import { askInChat } from './chat.js';
import { jumpToSegment } from './transcript.js';

const VISIBLE_CAP = 10;
const CAT_LABEL = {
  key_point: 'Key point', context: 'Context', term: 'Term',
  visual: 'Screen', suggested_question: 'Question', decision: 'Decision',
};

const notesById = new Map();     // id → { ins, el, home }
const worthQueue = [];           // [{ id, el }] max 2
let earlierExpanded = false;

let feedEl, worthHost, notesScroll, notesItems, notesEarlier, unreadDot, railHead;
let glossaryDrawer, glossaryBody, glossaryCount, screenDrawer, screenBody, screenCount;

function insightTime(ins) {
  if (ins.seg != null && state.segByIndex.has(ins.seg)) {
    const sec = srtToSeconds(state.segByIndex.get(ins.seg).start);
    if (sec != null) return fmtClock(sec);
  }
  if (state.meetingStartMs) return fmtClock((Date.now() - state.meetingStartMs) / 1000);
  return '';
}

function askText(ins) {
  if (ins.category === 'term' && ins.label) {
    return `Explain "${ins.label}" in more detail and how it relates to this discussion.`;
  }
  if (ins.category === 'suggested_question') return ins.text;
  if (ins.category === 'visual') return `Tell me more about what's on screen: ${ins.text.slice(0, 200)}`;
  return `Tell me more about: ${ins.text}`;
}

// ── Main feed (alerts + notes) ───────────────────────────
function buildFeedItem(ins) {
  const item = document.createElement('div');
  item.className = 'note-item ' + (ins.priority === 'alert' ? 'note-alert' : 'note-note');
  item.dataset.insightId = ins.id;

  const head = document.createElement('div');
  head.className = 'note-head';
  const chip = document.createElement('span');
  chip.className = 'note-chip';
  chip.textContent = (ins.priority === 'alert' ? '⚡ ' : '') + (CAT_LABEL[ins.category] || 'Note');
  head.appendChild(chip);
  const time = insightTime(ins);
  if (time) {
    const t = document.createElement('span');
    t.className = 'note-time';
    t.textContent = time;
    head.appendChild(t);
  }
  if (ins.seg != null) {
    const jump = document.createElement('button');
    jump.className = 'note-jump';
    jump.textContent = 'jump ↗';
    jump.title = 'Jump to transcript';
    jump.addEventListener('click', (e) => { e.stopPropagation(); jumpToSegment(ins.seg); });
    head.appendChild(jump);
  }
  item.appendChild(head);

  const text = document.createElement('div');
  text.className = 'note-text';
  text.textContent = ins.text;
  item.appendChild(text);

  const actions = document.createElement('div');
  actions.className = 'note-actions';
  actions.appendChild(makeAction('Ask more', () => askInChat(askText(ins))));
  if (ins.seg != null) actions.appendChild(makeAction('Jump to transcript', () => jumpToSegment(ins.seg)));
  actions.appendChild(makeAction('Dismiss', () => removeInsight(ins.id)));
  item.appendChild(actions);

  head.addEventListener('click', () => item.classList.toggle('expanded'));
  text.addEventListener('click', () => item.classList.toggle('expanded'));
  return item;
}

function makeAction(label, fn) {
  const b = document.createElement('button');
  b.className = 'note-act';
  b.textContent = label;
  b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
  return b;
}

function addFeedItem(ins) {
  const el = buildFeedItem(ins);
  const atTop = notesScroll.scrollTop <= 8;
  const prevTop = notesScroll.scrollTop;
  notesItems.insertBefore(el, notesItems.firstChild);
  el.classList.add('note-in');
  setTimeout(() => el.classList.remove('note-in'), 900);
  notesById.set(ins.id, { ins, el, home: 'feed' });
  if (atTop) {
    notesScroll.scrollTop = 0;
  } else {
    // Never yank: keep the viewport where it was, flag unread.
    notesScroll.scrollTop = prevTop + el.offsetHeight;
    showUnread();
  }
  reflowCap();
}

function reflowCap() {
  const items = [...notesItems.children];
  const overflow = items.length - VISIBLE_CAP;
  items.forEach((el, i) => el.classList.toggle('earlier-hidden', !earlierExpanded && i >= VISIBLE_CAP));
  if (overflow > 0 && !earlierExpanded) {
    notesEarlier.hidden = false;
    notesEarlier.textContent = `earlier (${overflow}) ⌄`;
  } else {
    notesEarlier.hidden = true;
  }
}

// ── Worth asking (ephemeral) ─────────────────────────────
function addWorthAsking(ins) {
  const el = document.createElement('div');
  el.className = 'worth-card';
  el.dataset.insightId = ins.id;
  const q = document.createElement('div');
  q.className = 'worth-q';
  q.textContent = `"${ins.text}"`;
  el.appendChild(q);
  const row = document.createElement('div');
  row.className = 'worth-actions';
  const ask = document.createElement('button');
  ask.className = 'btn btn-tiny btn-primary';
  ask.textContent = 'Ask';
  ask.addEventListener('click', () => { askInChat(ins.text); removeInsight(ins.id); });
  const dismiss = document.createElement('button');
  dismiss.className = 'btn btn-tiny';
  dismiss.textContent = '✕';
  dismiss.addEventListener('click', () => removeInsight(ins.id));
  row.append(ask, dismiss);
  el.appendChild(row);

  worthHost.appendChild(el);
  worthQueue.push({ id: ins.id, el });
  notesById.set(ins.id, { ins, el, home: 'worth' });
  while (worthQueue.length > 2) {
    const oldest = worthQueue.shift();
    removeInsight(oldest.id);
  }
  worthHost.classList.remove('empty');
}

// ── Drawers (glossary / screen) ──────────────────────────
function addToDrawer(kind, ins) {
  const body = kind === 'glossary' ? glossaryBody : screenBody;
  const el = document.createElement('div');
  el.className = 'drawer-row';
  el.dataset.insightId = ins.id;
  if (ins.category === 'term' && ins.label) {
    const strong = document.createElement('strong');
    strong.textContent = ins.label;
    el.appendChild(strong);
    el.appendChild(document.createTextNode(' — ' + ins.text));
  } else {
    el.textContent = ins.text;
  }
  el.title = 'Ask about this';
  el.addEventListener('click', () => askInChat(askText(ins)));
  body.appendChild(el);
  notesById.set(ins.id, { ins, el, home: kind });
  updateDrawerCounts();
}

function updateDrawerCounts() {
  const g = glossaryBody.children.length;
  const s = screenBody.children.length;
  glossaryCount.textContent = g;
  screenCount.textContent = s;
  glossaryDrawer.classList.toggle('hidden', g === 0);
  screenDrawer.classList.toggle('hidden', s === 0);
}

// ── Routing + removal ────────────────────────────────────
export function addInsight(ins) {
  if (notesById.has(ins.id)) return;
  const cat = ins.category;
  if (cat === 'suggested_question') return addWorthAsking(ins);
  if (cat === 'term') return addToDrawer('glossary', ins);
  if (cat === 'visual') return addToDrawer('screen', ins);
  return addFeedItem(ins);
}

export function removeInsight(id) {
  const entry = notesById.get(id);
  if (!entry) return;
  const { el, home } = entry;
  el.style.transition = 'opacity .25s, transform .25s';
  el.style.opacity = '0';
  el.style.transform = 'translateX(-8px)';
  setTimeout(() => {
    el.remove();
    if (home === 'feed') reflowCap();
    else if (home === 'glossary' || home === 'screen') updateDrawerCounts();
    else if (home === 'worth') {
      const i = worthQueue.findIndex((w) => w.id === id);
      if (i >= 0) worthQueue.splice(i, 1);
      if (!worthQueue.length) worthHost.classList.add('empty');
    }
  }, 250);
  notesById.delete(id);
}

export function resetNotes() {
  notesById.clear();
  worthQueue.length = 0;
  earlierExpanded = false;
  // Remove only appended cards — keep the static "Worth asking" label element.
  worthHost.querySelectorAll('.worth-card').forEach((el) => el.remove());
  worthHost.classList.add('empty');
  notesItems.innerHTML = '';
  glossaryBody.innerHTML = '';
  screenBody.innerHTML = '';
  notesEarlier.hidden = true;
  hideUnread();
  updateDrawerCounts();
}

function showUnread() { unreadDot.hidden = false; }
function hideUnread() { unreadDot.hidden = true; }

export function initNotes() {
  feedEl = $('#notes-feed');
  worthHost = $('#worth-asking');
  notesScroll = $('#notes-scroll');
  notesItems = $('#notes-items');
  notesEarlier = $('#notes-earlier');
  unreadDot = $('#notes-unread');
  railHead = $('#notes-head');
  glossaryDrawer = $('#glossary-drawer');
  glossaryBody = glossaryDrawer.querySelector('.drawer-body');
  glossaryCount = glossaryDrawer.querySelector('.drawer-count');
  screenDrawer = $('#screen-drawer');
  screenBody = screenDrawer.querySelector('.drawer-body');
  screenCount = screenDrawer.querySelector('.drawer-count');

  worthHost.classList.add('empty');

  notesEarlier.addEventListener('click', () => { earlierExpanded = true; reflowCap(); });
  notesScroll.addEventListener('scroll', () => { if (notesScroll.scrollTop <= 8) hideUnread(); });
  railHead?.addEventListener('click', () => { notesScroll.scrollTop = 0; hideUnread(); });

  onMessage('insight', (ins) => addInsight(ins));
  onMessage('insight_remove', ({ id }) => removeInsight(id));

  updateDrawerCounts();
}
