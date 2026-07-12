'use strict';

const api = window.onboardingAPI;
const steps = [...document.querySelectorAll('.step')];
const dotsEl = document.getElementById('dots');
const backBtn = document.getElementById('backBtn');
const nextBtn = document.getElementById('nextBtn');
const statusEl = document.getElementById('obStatus');

let i = 0;

steps.forEach(() => {
  const d = document.createElement('div');
  d.className = 'dot';
  dotsEl.appendChild(d);
});
const dots = [...dotsEl.children];

function render() {
  steps.forEach((s, idx) => s.classList.toggle('active', idx === i));
  dots.forEach((d, idx) => d.classList.toggle('on', idx === i));
  backBtn.style.visibility = i === 0 ? 'hidden' : 'visible';
  nextBtn.textContent = i === steps.length - 1 ? 'Finish' : 'Next';
  statusEl.textContent = '';
  if (i === 1) refreshPermissions();
}

function setState(id, val) {
  const el = document.getElementById(id);
  const ok = val === 'granted';
  el.textContent = ok ? '✓ granted' : val || 'not granted';
  el.classList.toggle('ok', ok);
}

async function refreshPermissions() {
  try {
    const s = await api.status();
    setState('micState', s.mic);
    setState('screenState', s.screen);
  } catch {
    /* ignore */
  }
}

document.getElementById('micBtn').addEventListener('click', async () => {
  await api.requestMic();
  refreshPermissions();
});

document.getElementById('screenBtn').addEventListener('click', async () => {
  await api.openScreenSettings();
  setTimeout(refreshPermissions, 1500);
});

const providerEl = document.getElementById('provider');
const keyField = document.getElementById('keyField');
const keyHelp = document.getElementById('keyHelp');
const regionField = document.getElementById('regionField');
const regionEl = document.getElementById('region');
const apiKeyEl = document.getElementById('apiKey');
const modelField = document.getElementById('modelField');
const modelEl = document.getElementById('model');

function syncProvider() {
  const p = providerEl.value;
  const isBedrock = p === 'bedrock';
  const isOpenAICompat = p === 'openrouter' || p === 'venice';
  const isOllama = p === 'ollama';
  // Bedrock: API key (optional — blank falls back to AWS profile/SSO) + region,
  // no model input (model IDs live in Settings). OpenRouter/Venice: API key
  // (required) + model, no region. Ollama: no key, no region, model only.
  keyField.classList.toggle('hidden', !(isBedrock || isOpenAICompat));
  regionField.classList.toggle('hidden', !isBedrock);
  modelField.classList.toggle('hidden', !(isOpenAICompat || isOllama));
  if (isBedrock) {
    apiKeyEl.placeholder = 'Paste your Bedrock API key';
    regionEl.placeholder = 'us-east-1';
    keyHelp.textContent =
      'Generate one in the AWS console (Bedrock → API keys). Advanced: leave the key empty to use your AWS profile/SSO credentials instead. Stored encrypted in your macOS Keychain.';
  } else {
    apiKeyEl.placeholder = 'Paste your key';
    keyHelp.textContent = 'Stored encrypted in your macOS Keychain — never in plain text.';
  }
  if (p === 'openrouter') modelEl.placeholder = 'anthropic/claude-3.5-sonnet';
  if (p === 'venice') modelEl.placeholder = 'llama-3.3-70b';
  if (p === 'ollama') modelEl.placeholder = 'llama3.1';
}
providerEl.addEventListener('change', syncProvider);
syncProvider();

backBtn.addEventListener('click', () => {
  if (i > 0) {
    i--;
    render();
  }
});

nextBtn.addEventListener('click', async () => {
  if (i < steps.length - 1) {
    i++;
    render();
    return;
  }
  await finish();
});

async function finish() {
  const provider = providerEl.value;

  // Validate: Bedrock requires a region before we can proceed
  if (provider === 'bedrock' && !regionEl.value.trim()) {
    statusEl.textContent = 'AWS region is required for Bedrock (e.g. us-east-1).';
    return;
  }

  nextBtn.disabled = true;
  statusEl.textContent = 'Setting up…';
  const data = {
    provider,
    apiKey: apiKeyEl.value.trim(),
    region: provider === 'bedrock' ? document.getElementById('region').value.trim() : '',
    openrouterModel: provider === 'openrouter' ? modelEl.value.trim() : '',
    veniceModel: provider === 'venice' ? modelEl.value.trim() : '',
    ollamaModel: provider === 'ollama' ? modelEl.value.trim() : '',
    profile: {
      role: document.getElementById('p_role').value.trim(),
      expertise: document.getElementById('p_expertise').value.trim(),
      organization: document.getElementById('p_org').value.trim(),
      counterparts: document.getElementById('p_counterparts').value.trim(),
    },
  };
  const res = await api.complete(data);
  if (!res || !res.ok) {
    statusEl.textContent = (res && res.error) || 'Setup failed.';
    nextBtn.disabled = false;
  }
  // On success, the main process closes this window and launches the app.
}

render();
