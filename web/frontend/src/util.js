// Shared helpers: DOM lookups, sanitized markdown, timestamp math, toasts.
import { marked } from 'marked';
import DOMPurify from 'dompurify';

marked.setOptions({ breaks: true, gfm: true, async: false });

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// All markdown → HTML goes through DOMPurify. innerHTML is only ever set from
// the return value of this function or from static, hand-written markup (§4).
export function renderMarkdown(md) {
  return DOMPurify.sanitize(marked.parse(md || ''));
}

// True when a scroll container is at (or within `threshold`px of) the bottom.
export function isNearBottom(el, threshold = 40) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
}

export function formatDate(iso) {
  if (!iso) return 'Unknown';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

// Parse an SRT timestamp "HH:MM:SS,mmm" → seconds. null if unparseable.
export function srtToSeconds(ts) {
  if (!ts || typeof ts !== 'string') return null;
  const m = ts.match(/(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})/);
  if (!m) return null;
  return (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]) + (+m[4]) / 1000;
}

// Seconds → "m:ss" (or "h:mm:ss" past an hour).
export function fmtClock(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

let toastHost = null;
export function showToast(text, kind = 'ok') {
  if (!toastHost) {
    toastHost = document.createElement('div');
    toastHost.id = 'toast-host';
    document.body.appendChild(toastHost);
  }
  const t = document.createElement('div');
  t.className = `toast toast-${kind}`;
  t.textContent = text;
  toastHost.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.remove(), 300);
  }, 4000);
}

// Copy a rendered element as both rich HTML and plain text (reused by the chat
// copy button and the summary export bar — §5.1).
export function copyRich(contentEl, onDone) {
  const html = contentEl.innerHTML;
  const plain = contentEl.innerText;
  const blob = new Blob([html], { type: 'text/html' });
  const blobPlain = new Blob([plain], { type: 'text/plain' });
  navigator.clipboard.write([
    new ClipboardItem({ 'text/html': blob, 'text/plain': blobPlain }),
  ]).then(() => onDone?.(true)).catch(() => onDone?.(false));
}
