'use strict';

// All settingsAPI calls go over IPC so the preload can be sandboxed (§5.4).
const { schema: schemaFn, get, save, pickDirectory, openLog, secretsPresent } =
  window.settingsAPI;

const sidebarEl = document.getElementById('sidebar');
const contentEl = document.getElementById('content');
const saveBtn = document.getElementById('saveBtn');
const logBtn = document.getElementById('logBtn');
const statusEl = document.getElementById('status');
const restartNote = document.getElementById('restartNote');
const searchInput = document.getElementById('search');

let schema = [];
let current = {};
// Password-field keys whose secret is already stored — show placeholder, not value.
const _storedSecrets = new Set();

// ── Helpers ────────────────────────────────────────────────
function fieldId(key) { return `f_${key}`; }

// ── Conditional visibility (schema `showWhen`) ─────────────
// LLM_PROVIDER defaults to 'openrouter'. Normalize centrally so an unset/blank
// value (a stored '' or a missing key) resolves to 'openrouter' when matching a
// showWhen. (The backend — llm_service.provider_name / auth_check — treats an
// unset/blank LLM_PROVIDER as openrouter as well.)
const PROVIDER_KEY = 'LLM_PROVIDER';
// Keys that some other field's showWhen depends on (rebuilt from the schema in
// render()); their controls re-run visibility on change.
let _driverKeys = new Set();

function normalizeCondValue(key, value) {
  const s = (value ?? '').toString().trim();
  if (key === PROVIDER_KEY) return s.toLowerCase() || 'openrouter';
  return s;
}

// True if a field should be shown given the current value of its driver control.
function fieldVisible(field) {
  const cond = field.showWhen;
  if (!cond) return true;
  const driver = document.getElementById(fieldId(cond.key));
  if (!driver) return true; // driver not rendered → don't hide
  const cur = normalizeCondValue(cond.key, driver.value);
  return cond.values.some((v) => normalizeCondValue(cond.key, v) === cur);
}

// Show/hide every conditional field per its showWhen. Hidden fields keep their
// values (we only toggle a display class), so switching providers never clears
// anything. Search uses a separate class, so cond-hidden always wins.
function applyVisibility() {
  for (const group of schema) {
    for (const field of group.fields) {
      if (!field.showWhen) continue;
      const wrap = contentEl.querySelector(`.field[data-key="${field.key}"]`);
      if (wrap) wrap.classList.toggle('cond-hidden', !fieldVisible(field));
    }
  }
}

// ── Render field ───────────────────────────────────────────
function renderField(field) {
  const wrap = document.createElement('div');
  wrap.className = 'field';
  wrap.dataset.key = field.key;
  // Store lowercased label + help for search matching.
  wrap.dataset.label = field.label.toLowerCase();
  wrap.dataset.help = (field.help || '').toLowerCase();

  // Row: label + ↺ reset button
  const header = document.createElement('div');
  header.className = 'field-header';

  const title = document.createElement('label');
  title.className = 'title';
  title.setAttribute('for', fieldId(field.key));
  title.textContent = field.label;
  header.appendChild(title);

  const resetBtn = document.createElement('button');
  resetBtn.type = 'button';
  resetBtn.className = 'btn-reset';
  resetBtn.title = 'Reset to default (clear value)';
  resetBtn.textContent = '↺';
  resetBtn.addEventListener('click', () => resetFieldToDefault(field));
  header.appendChild(resetBtn);

  wrap.appendChild(header);

  // Control
  let control;
  if (field.type === 'select') {
    control = document.createElement('select');
    for (const opt of field.options) {
      const o = document.createElement('option');
      o.value = opt.value;
      o.textContent = opt.label;
      control.appendChild(o);
    }
  } else {
    control = document.createElement('input');
    control.type =
      field.type === 'password' ? 'password'
      : field.type === 'number' ? 'number'
      : 'text';
    if (field.placeholder) control.placeholder = field.placeholder;
    if (field.step) control.step = field.step;
    // Password fields: track pristine state so unchanged fields aren't sent (§4.4).
    if (field.type === 'password') {
      control.dataset.pristine = 'true';
      control.addEventListener('input', () => { control.dataset.pristine = 'false'; });
    }
  }
  control.id = fieldId(field.key);
  control.dataset.key = field.key;
  // Any change marks the form dirty for the "restart pending" indicator.
  control.addEventListener('change', updateRestartNote);
  control.addEventListener('input', updateRestartNote);
  // If other fields' visibility depends on this control, re-evaluate on change.
  if (_driverKeys.has(field.key)) {
    control.addEventListener('change', applyVisibility);
  }

  if (field.type === 'directory') {
    const row = document.createElement('div');
    row.className = 'row';
    const browse = document.createElement('button');
    browse.type = 'button';
    browse.className = 'browse';
    browse.textContent = 'Browse…';
    browse.addEventListener('click', async () => {
      const dir = await pickDirectory();
      if (dir) { control.value = dir; updateRestartNote(); }
    });
    row.appendChild(control);
    row.appendChild(browse);
    wrap.appendChild(row);
  } else {
    wrap.appendChild(control);
  }

  if (field.help) {
    const help = document.createElement('p');
    help.className = 'help';
    help.textContent = field.help;
    wrap.appendChild(help);
  }

  return wrap;
}

// Reset a field to "empty" = use the backend default.
function resetFieldToDefault(field) {
  const el = document.getElementById(fieldId(field.key));
  if (!el) return;
  if (field.type === 'select') {
    // First option is the default (convention: '' value = backend default).
    el.value = field.options[0]?.value ?? '';
  } else if (field.type === 'password') {
    el.value = '';
    el.dataset.pristine = 'false'; // treat as changed so save will clear the secret
    el.placeholder = _storedSecrets.has(field.key)
      ? '(will be cleared on save)'
      : (field.placeholder || '');
  } else {
    el.value = '';
  }
  updateRestartNote();
  // Resetting the provider select (or any driver) may change what's shown.
  applyVisibility();
}

// ── Render sidebar + section panels ───────────────────────
function render() {
  sidebarEl.innerHTML = '';
  contentEl.innerHTML = '';

  // Collect keys other fields' visibility depends on before rendering fields,
  // so renderField() can wire change listeners on the driver controls.
  _driverKeys = new Set();
  for (const group of schema) {
    for (const field of group.fields) {
      if (field.showWhen) _driverKeys.add(field.showWhen.key);
    }
  }

  schema.forEach((group, idx) => {
    // Sidebar nav item
    const navBtn = document.createElement('button');
    navBtn.type = 'button';
    navBtn.className = 'sidebar-item' + (idx === 0 ? ' active' : '');
    navBtn.dataset.section = group.section;
    navBtn.textContent = group.section;
    navBtn.title = group.section;
    navBtn.addEventListener('click', () => {
      // Clicking a sidebar tab also clears any active search.
      if (searchInput.value) {
        searchInput.value = '';
        document.body.classList.remove('searching');
      }
      activateSection(group.section);
    });
    sidebarEl.appendChild(navBtn);

    // Content panel for this section
    const panel = document.createElement('div');
    panel.className = 'section-panel' + (idx === 0 ? ' active' : '');
    panel.dataset.section = group.section;

    const groupEl = document.createElement('div');
    groupEl.className = 'group';

    const h2 = document.createElement('h2');
    h2.textContent = group.section;
    groupEl.appendChild(h2);

    if (group.blurb) {
      const blurb = document.createElement('p');
      blurb.className = 'blurb';
      blurb.textContent = group.blurb;
      groupEl.appendChild(blurb);
    }

    for (const field of group.fields) groupEl.appendChild(renderField(field));
    panel.appendChild(groupEl);
    contentEl.appendChild(panel);
  });
}

// Show the panel for the given section name; update sidebar highlight.
function activateSection(section) {
  for (const btn of sidebarEl.querySelectorAll('.sidebar-item')) {
    btn.classList.toggle('active', btn.dataset.section === section);
  }
  for (const panel of contentEl.querySelectorAll('.section-panel')) {
    panel.classList.toggle('active', panel.dataset.section === section);
  }
  contentEl.scrollTop = 0;
}

// ── Search ─────────────────────────────────────────────────
function onSearch() {
  const q = searchInput.value.trim().toLowerCase();

  if (!q) {
    // Restore normal tab mode.
    document.body.classList.remove('searching');
    // Un-hide all fields that were hidden by a previous search.
    for (const f of contentEl.querySelectorAll('.field.search-hidden')) {
      f.classList.remove('search-hidden');
    }
    for (const p of contentEl.querySelectorAll('.section-panel')) {
      p.classList.remove('search-visible');
    }
    // Re-activate the currently highlighted sidebar tab.
    const active = sidebarEl.querySelector('.sidebar-item.active');
    if (active) activateSection(active.dataset.section);
    return;
  }

  document.body.classList.add('searching');

  for (const panel of contentEl.querySelectorAll('.section-panel')) {
    panel.classList.remove('active', 'search-visible');
    let anyVisible = false;
    for (const field of panel.querySelectorAll('.field')) {
      const matches =
        field.dataset.label.includes(q) || field.dataset.help.includes(q);
      field.classList.toggle('search-hidden', !matches);
      if (matches) anyVisible = true;
    }
    if (anyVisible) panel.classList.add('search-visible');
  }
  contentEl.scrollTop = 0;
}

// ── Apply / collect values ─────────────────────────────────
function applyValues(config) {
  for (const group of schema) {
    for (const field of group.fields) {
      const el = document.getElementById(fieldId(field.key));
      if (!el) continue;
      if (field.type === 'password') {
        // Never echo a stored secret back into the field (§4.4).
        el.value = '';
        el.dataset.pristine = 'true';
        if (_storedSecrets.has(field.key)) {
          el.placeholder = '(stored securely — enter to replace)';
        }
      } else if (field.key === PROVIDER_KEY) {
        // An unset/blank provider normalizes to the default ('openrouter').
        el.value = normalizeCondValue(PROVIDER_KEY, config[field.key]);
      } else {
        el.value = config[field.key] ?? '';
      }
    }
  }
  // Values are set — reflect conditional visibility for the current provider.
  applyVisibility();
}

function collectValues() {
  const out = {};
  for (const group of schema) {
    for (const field of group.fields) {
      const el = document.getElementById(fieldId(field.key));
      if (!el) continue;
      if (field.type === 'password') {
        // Only include password fields the user explicitly edited this session.
        if (el.dataset.pristine !== 'true') {
          out[field.key] = el.value.trim(); // '' to clear, non-empty to set/replace
        }
      } else {
        const v = el.value.trim();
        if (v !== '') out[field.key] = v;
      }
    }
  }
  return out;
}

// ── Restart-pending indicator ──────────────────────────────
// Shows when any non-password field has drifted from the last-saved baseline.
function updateRestartNote() {
  let dirty = false;
  outer: for (const group of schema) {
    for (const field of group.fields) {
      if (field.type === 'password') continue; // password changes don't need special note
      const el = document.getElementById(fieldId(field.key));
      if (!el) continue;
      // Normalize the provider field so a stored ''/missing baseline doesn't
      // read as drift against the default ('openrouter') value.
      const nowVal = field.key === PROVIDER_KEY
        ? normalizeCondValue(PROVIDER_KEY, el.value)
        : el.value.trim();
      const origVal = field.key === PROVIDER_KEY
        ? normalizeCondValue(PROVIDER_KEY, current[field.key])
        : String(current[field.key] ?? '').trim();
      if (nowVal !== origVal) { dirty = true; break outer; }
    }
  }
  restartNote.classList.toggle('hidden', !dirty);
}

// ── Status helper ──────────────────────────────────────────
function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = 'status' + (kind ? ' ' + kind : '');
}

// ── Save ───────────────────────────────────────────────────
async function onSave() {
  saveBtn.disabled = true;
  setStatus('Saving and restarting server…');
  const config = collectValues();
  try {
    const result = await save(config);
    if (result.ok) {
      // Update baseline so restart-pending clears.
      current = { ...current, ...config };
      // Re-apply to reset password placeholder text after save.
      try {
        const stored = await secretsPresent();
        _storedSecrets.clear();
        for (const key of stored) _storedSecrets.add(key);
      } catch { /* ignore */ }
      applyValues(current);
      updateRestartNote();
      setStatus('Saved — server restarted.', 'ok');
    } else {
      setStatus(result.error || 'Save failed.', 'err');
    }
  } catch (err) {
    setStatus(String(err.message || err), 'err');
  } finally {
    saveBtn.disabled = false;
  }
}

// ── Init ───────────────────────────────────────────────────
async function init() {
  try {
    schema = await schemaFn();
  } catch (err) {
    contentEl.innerHTML = '';
    const msg = document.createElement('p');
    msg.style.cssText = 'padding:24px;color:var(--err)';
    msg.textContent = `Failed to load settings schema: ${err.message}`;
    contentEl.appendChild(msg);
    return;
  }

  render();

  // Fetch which password secrets are already stored before applying values
  // so placeholders are correct on first paint.
  try {
    const stored = await secretsPresent();
    for (const key of stored) _storedSecrets.add(key);
  } catch { /* secretsPresent may be unavailable in older builds — safe to ignore */ }

  current = (await get()) || {};
  applyValues(current);
  updateRestartNote();

  saveBtn.addEventListener('click', onSave);
  logBtn.addEventListener('click', () => openLog());
  searchInput.addEventListener('input', onSearch);
  // Clear search on Escape.
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { searchInput.value = ''; onSearch(); searchInput.blur(); }
  });
}

init();
