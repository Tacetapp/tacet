// Unknown-speaker naming: cards pinned to the top of the right rail in both
// modes (time-sensitive tasks), plus an inline rename popover opened from a
// "Speaker N" label in the transcript (§5.2/§5.3). Both send confirm_speaker.
import { $ } from './util.js';
import { send, onMessage } from './ws.js';

const speakerCards = new Map();   // cluster → card element
let lastCandidates = [];          // cached for the inline popover
let skipAll = false;
let cardsHost, group, badgeEl, skipAllBtn, openPopoverEl = null;

export function getCandidates() { return lastCandidates.slice(); }

function jump(index) {
  document.dispatchEvent(new CustomEvent('jump-to-segment', { detail: index }));
}

function updateBadge() {
  const n = speakerCards.size;
  if (badgeEl) {
    badgeEl.textContent = String(n);
    badgeEl.classList.toggle('has-items', n > 0);
  }
  if (group) group.classList.toggle('hidden', n === 0);
}

function removeSpeakerCard(cluster) {
  speakerCards.get(cluster)?.remove();
  speakerCards.delete(cluster);
  updateBadge();
}

export function clearSpeakerCards() {
  for (const card of speakerCards.values()) card.remove();
  speakerCards.clear();
  updateBadge();
  skipAll = false;
  if (skipAllBtn) {
    skipAllBtn.classList.remove('active');
    skipAllBtn.textContent = 'Skip all';
  }
  closePopover();
}

// A cluster just received a real name via speaker_update — dismiss its card.
export function resolveClusters(nameSet) {
  for (const cluster of Array.from(speakerCards.keys())) {
    const card = speakerCards.get(cluster);
    if (card._assignedTo && nameSet.has(card._assignedTo)) removeSpeakerCard(cluster);
  }
}

function updateCandidateRadios(wrap, cluster, candidates) {
  const prev = wrap.querySelector('input[type=radio]:checked');
  const prevValue = prev ? prev.value : null;
  wrap.innerHTML = '';
  if (!candidates.length) {
    const hint = document.createElement('div');
    hint.className = 'speaker-candidates-empty';
    hint.textContent = 'No suggested names — type below.';
    wrap.appendChild(hint);
    return;
  }
  const radioName = `speaker-radio-${cluster}`;
  for (const name of candidates) {
    const label = document.createElement('label');
    label.className = 'speaker-candidate';
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = radioName;
    radio.value = name;
    if (prevValue === name) radio.checked = true;
    label.appendChild(radio);
    label.appendChild(document.createTextNode(' ' + name));
    wrap.appendChild(label);
  }
}

function addSpeakerCard({ cluster, samples, candidates, mode }) {
  if (speakerCards.has(cluster)) {
    updateCandidateRadios(speakerCards.get(cluster).querySelector('.speaker-candidates'),
      cluster, candidates || []);
    return;
  }
  if (!cardsHost) return;
  group?.classList.remove('hidden');

  const card = document.createElement('div');
  card.className = 'speaker-card';
  card.dataset.cluster = cluster;
  card._assignedTo = null;

  const header = document.createElement('div');
  header.className = 'speaker-card-header';
  const clusterStrong = document.createElement('strong');
  clusterStrong.textContent = cluster;
  header.appendChild(clusterStrong);
  const hintSpan = document.createElement('span');
  hintSpan.className = 'speaker-card-hint';
  hintSpan.textContent = mode === 'batch' ? 'Unresolved' : 'New speaker';
  header.appendChild(hintSpan);
  card.appendChild(header);

  const samplesEl = document.createElement('div');
  samplesEl.className = 'speaker-samples';
  for (const s of (samples || [])) {
    const li = document.createElement('div');
    li.className = 'speaker-sample';
    li.textContent = `"${s.text}"`;
    li.title = 'Jump to transcript';
    if (s.index) {
      li.dataset.segIndex = s.index;
      li.addEventListener('click', () => jump(s.index));
    }
    samplesEl.appendChild(li);
  }
  card.appendChild(samplesEl);

  const candsWrap = document.createElement('div');
  candsWrap.className = 'speaker-candidates';
  card.appendChild(candsWrap);
  updateCandidateRadios(candsWrap, cluster, candidates || []);

  const customRow = document.createElement('div');
  customRow.className = 'speaker-custom';
  const customInput = document.createElement('input');
  customInput.type = 'text';
  customInput.placeholder = 'Other name…';
  customInput.className = 'speaker-custom-input';
  customRow.appendChild(customInput);
  card.appendChild(customRow);

  const actions = document.createElement('div');
  actions.className = 'speaker-actions';
  const btnConfirm = document.createElement('button');
  btnConfirm.className = 'btn btn-primary btn-tiny';
  btnConfirm.textContent = 'Confirm';
  btnConfirm.disabled = true;
  const btnSkip = document.createElement('button');
  btnSkip.className = 'btn btn-tiny';
  btnSkip.textContent = 'Skip';
  const btnDefer = document.createElement('button');
  btnDefer.className = 'btn btn-tiny';
  btnDefer.textContent = 'Ask me later';
  if (mode !== 'live') btnDefer.style.display = 'none';
  actions.append(btnConfirm, btnSkip, btnDefer);
  card.appendChild(actions);

  const refreshConfirm = () => {
    const hasRadio = !!card.querySelector('input[type=radio]:checked');
    const hasCustom = customInput.value.trim().length > 0;
    btnConfirm.disabled = !(hasRadio || hasCustom);
  };
  candsWrap.addEventListener('change', refreshConfirm);
  customInput.addEventListener('input', refreshConfirm);

  btnConfirm.addEventListener('click', () => {
    const custom = customInput.value.trim();
    const checked = card.querySelector('input[type=radio]:checked');
    const name = custom || (checked ? checked.value : '');
    if (!name) return;
    card._assignedTo = name;
    send({ type: 'confirm_speaker', cluster, name });
    card.classList.add('pending');
    btnConfirm.disabled = btnSkip.disabled = btnDefer.disabled = true;
  });
  btnSkip.addEventListener('click', () => {
    send({ type: 'skip_speaker', cluster });
    removeSpeakerCard(cluster);
  });
  btnDefer.addEventListener('click', () => {
    send({ type: 'defer_speaker', cluster });
    removeSpeakerCard(cluster);
  });

  cardsHost.appendChild(card);
  speakerCards.set(cluster, card);
  updateBadge();
}

// ── Inline rename popover (transcript label click) ──────────────
function closePopover() {
  openPopoverEl?.remove();
  openPopoverEl = null;
}

export function openSpeakerPopover(cluster, anchorEl) {
  closePopover();
  const pop = document.createElement('div');
  pop.className = 'speaker-popover';

  const title = document.createElement('div');
  title.className = 'speaker-popover-title';
  title.textContent = `Name ${cluster}`;
  pop.appendChild(title);

  const candsWrap = document.createElement('div');
  candsWrap.className = 'speaker-candidates';
  pop.appendChild(candsWrap);
  updateCandidateRadios(candsWrap, `pop-${cluster}`, lastCandidates);

  const customInput = document.createElement('input');
  customInput.type = 'text';
  customInput.placeholder = 'Other name…';
  customInput.className = 'speaker-custom-input';
  pop.appendChild(customInput);

  const actions = document.createElement('div');
  actions.className = 'speaker-actions';
  const confirm = document.createElement('button');
  confirm.className = 'btn btn-primary btn-tiny';
  confirm.textContent = 'Confirm';
  confirm.disabled = true;
  const cancel = document.createElement('button');
  cancel.className = 'btn btn-tiny';
  cancel.textContent = 'Cancel';
  actions.append(confirm, cancel);
  pop.appendChild(actions);

  const refresh = () => {
    const hasRadio = !!pop.querySelector('input[type=radio]:checked');
    confirm.disabled = !(hasRadio || customInput.value.trim().length > 0);
  };
  candsWrap.addEventListener('change', refresh);
  customInput.addEventListener('input', refresh);

  confirm.addEventListener('click', () => {
    const custom = customInput.value.trim();
    const checked = pop.querySelector('input[type=radio]:checked');
    const name = custom || (checked ? checked.value : '');
    if (!name) return;
    send({ type: 'confirm_speaker', cluster, name });
    closePopover();
  });
  cancel.addEventListener('click', closePopover);

  document.body.appendChild(pop);
  const r = anchorEl.getBoundingClientRect();
  const top = Math.min(r.bottom + 6, window.innerHeight - pop.offsetHeight - 8);
  const left = Math.min(r.left, window.innerWidth - pop.offsetWidth - 8);
  pop.style.top = `${Math.max(8, top)}px`;
  pop.style.left = `${Math.max(8, left)}px`;
  openPopoverEl = pop;
  setTimeout(() => customInput.focus(), 40);
}

export function initSpeakers() {
  cardsHost = $('#speaker-cards');
  group = $('.speakers-group');
  badgeEl = $('#badge-speakers');
  skipAllBtn = $('#btn-skip-all-speakers');

  onMessage('speaker_prompt', ({ cluster, samples, candidates }) => {
    if (candidates) lastCandidates = candidates;
    if (skipAll) { send({ type: 'defer_speaker', cluster }); return; }
    addSpeakerCard({ cluster, samples, candidates, mode: 'live' });
  });
  onMessage('speaker_batch_prompt', ({ clusters, candidates }) => {
    if (candidates) lastCandidates = candidates;
    if (!clusters || clusters.length === 0) return;
    for (const c of clusters) {
      addSpeakerCard({ cluster: c.cluster, samples: c.samples, candidates, mode: 'batch' });
    }
  });

  skipAllBtn?.addEventListener('click', () => {
    skipAll = !skipAll;
    skipAllBtn.classList.toggle('active', skipAll);
    skipAllBtn.textContent = skipAll ? 'Skipping' : 'Skip all';
    send({ type: 'skip_all_speakers', on: skipAll });
    if (skipAll) {
      for (const cluster of Array.from(speakerCards.keys())) {
        send({ type: 'defer_speaker', cluster });
        removeSpeakerCard(cluster);
      }
    }
  });

  // Dismiss the popover on outside click / escape.
  document.addEventListener('click', (e) => {
    if (openPopoverEl && !openPopoverEl.contains(e.target) &&
        !e.target.classList?.contains('speaker-renamable')) {
      closePopover();
    }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePopover(); });
}
