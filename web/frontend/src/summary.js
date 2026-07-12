// Review-mode Summary tab (§5.1): renders the meeting summary, turns an
// "## Action Items" section into a client-side checkable list, and drives the
// export bar (Copy · Export .md · Draft follow-up email). The summary is the
// app's best output — it gets its own tab instead of scrolling away in chat.
import { $, renderMarkdown, copyRich, showToast } from './util.js';
import { state } from './state.js';
import { onMessage } from './ws.js';

let contentEl, copyBtn, exportBtn, emailBtn;
let summaryRaw = '';                 // accumulated markdown (streaming + loaded)
const checkedItems = new Set();      // action items ticked this session (client-only)

function isActionHeading(el) {
  return /^h[1-3]$/i.test(el.tagName) && /action items/i.test(el.textContent || '');
}

// After sanitized markdown is in the DOM, upgrade the Action Items list into
// checkboxes. Pure DOM manipulation on already-sanitized nodes — no innerHTML.
function decorateActionItems() {
  const heading = [...contentEl.children].find(isActionHeading);
  if (!heading) return;
  let list = heading.nextElementSibling;
  while (list && !/^(ul|ol)$/i.test(list.tagName)) list = list.nextElementSibling;
  if (!list) return;
  list.classList.add('action-items');
  for (const li of list.querySelectorAll(':scope > li')) {
    if (li.querySelector('.action-check')) continue;
    const label = li.textContent.trim();
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.className = 'action-check';
    box.checked = checkedItems.has(label);
    li.classList.toggle('done', box.checked);
    box.addEventListener('change', () => {
      li.classList.toggle('done', box.checked);
      if (box.checked) checkedItems.add(label);
      else checkedItems.delete(label);
    });
    li.prepend(box);
  }
}

function render() {
  if (!summaryRaw) {
    contentEl.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'empty-state';
    p.textContent = 'The meeting summary will appear here.';
    contentEl.appendChild(p);
    return;
  }
  contentEl.innerHTML = renderMarkdown(summaryRaw);   // sanitized
}

export function resetSummary() {
  summaryRaw = '';
  checkedItems.clear();
  if (contentEl) render();
}

// Populate from a loaded session (already-complete markdown).
export function showSummary(md) {
  summaryRaw = md || '';
  render();
  decorateActionItems();
}

export function hasSummary() { return !!summaryRaw; }

// ── Export actions ───────────────────────────────────────
function draftEmail() {
  if (!summaryRaw) return;
  const title = state.meetingTitle && state.meetingTitle !== 'Unknown Meeting'
    ? state.meetingTitle : 'Meeting';
  const subject = `Follow-up: ${title}`;
  const body = summaryRaw;
  const href = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  // mailto has a practical URL length ceiling; fall back to clipboard for long
  // summaries so the draft is never silently truncated.
  if (href.length < 1900) {
    window.location.href = href;
  } else {
    copyRich(contentEl, (ok) => showToast(
      ok ? 'Summary too long for email — copied to clipboard instead.'
         : 'Could not open email or copy.', ok ? 'ok' : 'err'));
  }
}

function exportMarkdown() {
  if (!summaryRaw) return;
  const title = (state.meetingTitle && state.meetingTitle !== 'Unknown Meeting'
    ? state.meetingTitle : 'meeting-summary').replace(/[^\w.-]+/g, '-').toLowerCase();
  const blob = new Blob([summaryRaw], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${title}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function initSummary() {
  contentEl = $('#summary-content');
  copyBtn = $('#btn-copy-summary');
  exportBtn = $('#btn-export-md');
  emailBtn = $('#btn-draft-email');

  copyBtn?.addEventListener('click', () => {
    copyRich(contentEl, (ok) => {
      copyBtn.textContent = ok ? 'Copied ✓' : 'Copy failed';
      setTimeout(() => (copyBtn.textContent = 'Copy'), 1500);
    });
  });
  exportBtn?.addEventListener('click', exportMarkdown);
  emailBtn?.addEventListener('click', draftEmail);

  // Streaming summary (end-of-meeting) lands in the Summary tab, not chat.
  onMessage('summary_chunk', ({ text }) => {
    summaryRaw += text;
    render();
  });
  onMessage('summary_done', () => {
    render();
    decorateActionItems();
  });
}
