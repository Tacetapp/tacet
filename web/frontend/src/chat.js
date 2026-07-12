// Chat: message list, streaming responses, image attachments, copy button.
// One chat unit is reparented by ui.js — a live overlay drawer over the
// transcript column, or the review-mode right rail (§5.1). Works in both.
import { $, renderMarkdown, isNearBottom, copyRich } from './util.js';
import { state } from './state.js';
import { send, onMessage } from './ws.js';

const ACCEPTED_TYPES = { 'image/png': 'png', 'image/jpeg': 'jpeg', 'image/gif': 'gif', 'image/webp': 'webp' };

let chatEl, chatMessages, chatForm, chatInput, sendBtn, attachBtn, fileInput, attachPreview, catchupBtn, drawerClose;
let streamingEl = null;
let streamingRaw = '';
let pendingImages = [];

function clearEmpty(el) { el.querySelector('.empty-state')?.remove(); }

export function addMessage(role, text, imageDataUrls) {
  clearEmpty(chatMessages);
  const div = document.createElement('div');
  div.className = `msg msg-${role}`;
  if (imageDataUrls && imageDataUrls.length) {
    const imagesDiv = document.createElement('div');
    imagesDiv.className = 'msg-images';
    for (const dataUrl of imageDataUrls) {
      const img = document.createElement('img');
      img.src = dataUrl.startsWith('data:') ? dataUrl : `data:image/png;base64,${dataUrl}`;
      img.alt = 'Attached image';
      imagesDiv.appendChild(img);
    }
    div.appendChild(imagesDiv);
  }
  const content = document.createElement('div');
  content.className = 'msg-content';
  if (role === 'user') content.textContent = text;
  else if (text) content.innerHTML = renderMarkdown(text);   // sanitized
  div.appendChild(content);
  if (text && role !== 'user') addCopyBtn(div);
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return div;
}

function addCopyBtn(msgDiv) {
  if (msgDiv.querySelector('.btn-copy')) return;
  const btn = document.createElement('button');
  btn.className = 'btn-copy';
  btn.title = 'Copy with formatting';
  btn.textContent = '📋';
  btn.addEventListener('click', () => {
    copyRich(msgDiv.querySelector('.msg-content'), (ok) => {
      btn.textContent = ok ? '✓' : '⚠';
      setTimeout(() => (btn.textContent = '📋'), 1500);
    });
  });
  msgDiv.appendChild(btn);
}

function showTyping() {
  if (chatMessages.querySelector('.typing-indicator')) return;
  const div = document.createElement('div');
  div.className = 'typing-indicator';
  div.innerHTML = '<span></span><span></span><span></span>';   // static markup
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}
function removeTyping() { chatMessages.querySelector('.typing-indicator')?.remove(); }

export function resetChat() {
  chatMessages.innerHTML = '<p class="empty-state">Ask questions about the meeting…</p>';
  streamingEl = null;
  streamingRaw = '';
  clearPendingImages();
  closeDrawer();
}

// ── Live drawer (over the transcript column) ─────────────
function openDrawer() { if (state.mode === 'live') chatEl.classList.add('open'); }
function closeDrawer() { chatEl.classList.remove('open'); }

// Send a question programmatically (notes "Ask" / "Ask more", catch-me-up).
export function askInChat(text) {
  if (!state.chatReady || !text) return;
  addMessage('user', text);
  showTyping();
  send({ type: 'chat', text });
  openDrawer();
}

function submitChat() {
  const text = chatInput.value.trim();
  if ((!text && !pendingImages.length) || !state.chatReady) return;
  addMessage('user', text, pendingImages.map((img) => img.data));
  showTyping();
  const msg = { type: 'chat', text: text || '(see attached images)' };
  if (pendingImages.length) msg.images = pendingImages.map((img) => ({ format: img.format, data: img.data }));
  send(msg);
  chatInput.value = '';
  chatInput.style.height = 'auto';
  clearPendingImages();
  openDrawer();
}

// ── Image attachments ────────────────────────────────────
function addImageFiles(files) {
  for (const file of files) {
    const fmt = ACCEPTED_TYPES[file.type];
    if (!fmt) continue;
    if (pendingImages.length >= 5) break;
    const reader = new FileReader();
    reader.onload = () => {
      pendingImages.push({ data: reader.result.split(',')[1], format: fmt, name: file.name, dataUrl: reader.result });
      renderAttachmentPreview();
    };
    reader.readAsDataURL(file);
  }
}
function renderAttachmentPreview() {
  attachPreview.innerHTML = '';
  if (!pendingImages.length) {
    attachPreview.classList.add('hidden');
    attachBtn.classList.remove('has-images');
    return;
  }
  attachPreview.classList.remove('hidden');
  attachBtn.classList.add('has-images');
  pendingImages.forEach((img, i) => {
    const thumb = document.createElement('div');
    thumb.className = 'attachment-thumb';
    const el = document.createElement('img');
    el.src = img.dataUrl;
    el.alt = img.name;
    thumb.appendChild(el);
    const removeBtn = document.createElement('button');
    removeBtn.className = 'remove-btn';
    removeBtn.textContent = '×';
    removeBtn.addEventListener('click', () => { pendingImages.splice(i, 1); renderAttachmentPreview(); });
    thumb.appendChild(removeBtn);
    attachPreview.appendChild(thumb);
  });
  const hint = document.createElement('span');
  hint.className = 'attachment-hint';
  hint.textContent = `${pendingImages.length}/5 images`;
  attachPreview.appendChild(hint);
}
function clearPendingImages() {
  pendingImages = [];
  if (attachPreview) renderAttachmentPreview();
  if (fileInput) fileInput.value = '';
}

// Reflect chat_ready on the input controls.
export function refreshChatEnabled() {
  const ready = state.chatReady;
  chatInput.disabled = !ready;
  sendBtn.disabled = !ready;
  attachBtn.disabled = !ready;
}

export function initChat() {
  chatEl = $('#chat');
  chatMessages = $('#chat-messages');
  chatForm = $('#chat-form');
  chatInput = $('#chat-input');
  sendBtn = chatForm.querySelector('.btn-send');
  attachBtn = $('#btn-attach');
  fileInput = $('#file-input');
  attachPreview = $('#attachment-preview');
  catchupBtn = $('#btn-catchup');
  drawerClose = $('#chat-drawer-close');

  onMessage('chat_chunk', ({ text }) => {
    removeTyping();
    if (!streamingEl) { streamingEl = addMessage('assistant', ''); streamingRaw = ''; }
    streamingRaw += text;
    const stick = isNearBottom(chatMessages);
    streamingEl.querySelector('.msg-content').innerHTML = renderMarkdown(streamingRaw);
    if (stick) chatMessages.scrollTop = chatMessages.scrollHeight;
  });
  onMessage('chat_done', () => {
    removeTyping();
    if (streamingEl) addCopyBtn(streamingEl);
    streamingEl = null;
    streamingRaw = '';
  });

  chatForm.addEventListener('submit', (e) => { e.preventDefault(); submitChat(); });
  chatInput.addEventListener('input', () => {
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 150) + 'px';
    chatInput.style.overflowY = chatInput.scrollHeight > 150 ? 'auto' : 'hidden';
  });
  chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); chatForm.requestSubmit(); }
  });
  chatInput.addEventListener('focus', openDrawer);

  catchupBtn?.addEventListener('click', () => {
    askInChat('Catch me up — give me a 5-bullet recap of the last few minutes of this meeting.');
  });
  drawerClose?.addEventListener('click', closeDrawer);

  attachBtn.addEventListener('click', () => { if (state.chatReady) fileInput.click(); });
  fileInput.addEventListener('change', () => { if (fileInput.files.length) addImageFiles(fileInput.files); fileInput.value = ''; });
  chatInput.addEventListener('paste', (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const imgs = [];
    for (const item of items) if (ACCEPTED_TYPES[item.type]) imgs.push(item.getAsFile());
    if (imgs.length) { e.preventDefault(); addImageFiles(imgs); }
  });

  let dragCounter = 0;
  chatEl.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragCounter++;
    if (dragCounter === 1 && state.chatReady && !chatEl.querySelector('#drop-overlay')) {
      const overlay = document.createElement('div');
      overlay.className = 'drop-overlay';
      overlay.id = 'drop-overlay';
      overlay.innerHTML = '<div class="drop-overlay-text">Drop images here (PNG, JPG, GIF, WebP)</div>';
      chatEl.appendChild(overlay);
    }
  });
  chatEl.addEventListener('dragleave', (e) => { e.preventDefault(); if (--dragCounter === 0) $('#drop-overlay')?.remove(); });
  chatEl.addEventListener('dragover', (e) => e.preventDefault());
  chatEl.addEventListener('drop', (e) => {
    e.preventDefault();
    dragCounter = 0;
    $('#drop-overlay')?.remove();
    if (!state.chatReady) return;
    const files = [...e.dataTransfer.files].filter((f) => ACCEPTED_TYPES[f.type]);
    if (files.length) addImageFiles(files);
  });
}
