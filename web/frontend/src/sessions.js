// Sessions library: modal behind the header library icon, debounced search,
// knowledge-base sync bar, per-session re-ingest, and ingest/sync toasts.
// All dynamic values use textContent — session titles/snippets can contain
// LLM- or transcript-authored content and must never reach innerHTML (§4.2).
import { $, formatDate, showToast } from './util.js';
import { send, onMessage } from './ws.js';

let modal, list, searchInput, searchDebounce = null;

function open() {
  modal.classList.remove('hidden');
  searchInput.value = '';
  send({ type: 'list_sessions' });
  setTimeout(() => searchInput.focus(), 100);
}
function close() { modal.classList.add('hidden'); }

function renderSessionList(sessions) {
  list.innerHTML = '';
  if (!sessions.length) {
    const p = document.createElement('p');
    p.className = 'empty-state';
    p.textContent = searchInput.value.trim() ? 'No matching sessions.' : 'No saved sessions yet.';
    list.appendChild(p);
    return;
  }
  for (const s of sessions) {
    const div = document.createElement('div');
    div.className = 'session-item';

    const dateDiv = document.createElement('div');
    dateDiv.className = 'session-date';
    const titlePart = s.title && s.title !== 'Unknown Meeting' ? s.title + ' — ' : '';
    dateDiv.textContent = titlePart + formatDate(s.started_at);
    div.appendChild(dateDiv);

    const metaDiv = document.createElement('div');
    metaDiv.className = 'session-meta';
    metaDiv.textContent = s.segment_count + ' segments' + (s.has_summary ? ' · has summary' : '');
    div.appendChild(metaDiv);

    if (s.snippet) {
      const snippetDiv = document.createElement('div');
      snippetDiv.className = 'session-snippet';
      const sourceSpan = document.createElement('span');
      sourceSpan.className = 'snippet-source';
      sourceSpan.textContent = s.snippet_source || '';
      snippetDiv.appendChild(sourceSpan);
      snippetDiv.appendChild(document.createTextNode(' ' + s.snippet));
      div.appendChild(snippetDiv);
    }

    div.addEventListener('click', () => { send({ type: 'load_session', session_id: s.id }); });

    const reingestBtn = document.createElement('button');
    reingestBtn.className = 'session-reingest';
    reingestBtn.title = 'Re-file this meeting into the knowledge vault';
    reingestBtn.textContent = '↻';
    reingestBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      send({ type: 'reingest_session', session_id: s.id });
      showToast('Re-filing meeting…', 'ok');
    });
    div.appendChild(reingestBtn);
    list.appendChild(div);
  }
}

export function initSessions() {
  modal = $('#session-modal');
  list = $('#session-list');
  searchInput = $('#session-search-input');

  $('#btn-library')?.addEventListener('click', open);
  $('#btn-library-idle')?.addEventListener('click', open);
  $('#modal-close')?.addEventListener('click', close);
  modal.querySelector('.modal-backdrop')?.addEventListener('click', close);

  searchInput.addEventListener('input', () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      const q = searchInput.value.trim();
      send(q ? { type: 'search_sessions', query: q } : { type: 'list_sessions' });
    }, 300);
  });

  $('#btn-kb-sync')?.addEventListener('click', () => {
    const btn = $('#btn-kb-sync');
    const deep = !!$('#kb-deep-toggle')?.checked;
    const status = $('#kb-sync-status');
    if (btn) btn.disabled = true;
    if (status) status.textContent = deep ? 'Deep sync starting…' : 'Syncing…';
    send({ type: 'sync_knowledge', deep });
  });

  onMessage('sessions', ({ sessions }) => renderSessionList(sessions));

  onMessage('ingest_done', ({ ok, project_slug, skipped, error }) => {
    if (ok && skipped) return;
    if (ok) showToast(project_slug ? `Filed under “${project_slug}”` : 'Filed into the knowledge vault', 'ok');
    else showToast(`Knowledge ingest failed${error ? ': ' + error : ''}`, 'err');
  });

  onMessage('sync_progress', ({ step }) => {
    const el = $('#kb-sync-status');
    if (el) el.textContent = (step || 'Working') + '…';
  });

  onMessage('sync_done', (r) => {
    const btn = $('#btn-kb-sync');
    const status = $('#kb-sync-status');
    if (btn) btn.disabled = false;
    if (status) status.textContent = '';
    if (!r.ok) { showToast(`Sync failed${r.error ? ': ' + r.error : ''}`, 'err'); return; }
    const parts = [];
    if (r.index_rebuilt) parts.push('index rebuilt');
    if (r.backlinks_repaired) parts.push(`${r.backlinks_repaired} backlink${r.backlinks_repaired === 1 ? '' : 's'} repaired`);
    const brokenN = Array.isArray(r.broken_links) ? r.broken_links.length : (r.broken_links || 0);
    if (brokenN) parts.push(`${brokenN} broken link${brokenN === 1 ? '' : 's'}`);
    if (r.merged_projects) parts.push(`${r.merged_projects} project${r.merged_projects === 1 ? '' : 's'} merged`);
    if (r.merged_people) parts.push(`${r.merged_people} ${r.merged_people === 1 ? 'person' : 'people'} merged`);
    if (r.briefs_refreshed) parts.push(`${r.briefs_refreshed} brief${r.briefs_refreshed === 1 ? '' : 's'} refreshed`);
    showToast(`Knowledge base synced — ${parts.length ? parts.join(' · ') : 'already in sync'}`, brokenN ? 'err' : 'ok');
  });
}

export { close as closeSessionModal };
