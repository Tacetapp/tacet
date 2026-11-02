// Single source of truth for every user-configurable option.
//
// The settings page renders straight from this schema, and the Electron main
// process turns the saved values into environment variables for the Python
// server. Every key here maps 1:1 to an env var the backend reads (see
// web/backend/*.py). Changing any of them requires a server restart, which the
// app does automatically on save.

// Conditional visibility for the AI Provider section. LLM_PROVIDER defaults to
// 'openrouter' now (llm_service.provider_name / auth_check / settings.js all
// normalize an unset/blank value to 'openrouter'), so the Bedrock fields show
// only for the explicit 'bedrock' value.
const SHOW_BEDROCK = { key: 'LLM_PROVIDER', values: ['bedrock'] };
const SHOW_OPENROUTER = { key: 'LLM_PROVIDER', values: ['openrouter'] };
const SHOW_VENICE = { key: 'LLM_PROVIDER', values: ['venice'] };
const SHOW_OLLAMA = { key: 'LLM_PROVIDER', values: ['ollama'] };

const SCHEMA = [
  {
    section: 'Storage',
    blurb: 'Where meetings are written. Leave blank to use the app default.',
    fields: [
      {
        key: 'MEETINGS_DIR',
        label: 'Meetings folder',
        type: 'directory',
        placeholder: '(app default: <repo>/meetings)',
        help: 'Folder where each meeting is saved as a markdown wiki file.',
      },
      {
        key: 'KNOWLEDGE_DIR',
        label: 'Knowledge vault folder',
        type: 'directory',
        placeholder: '(app default: <meetings folder>/knowledge)',
        help: 'Where the synthesized projects/people/sources wiki is built from your meetings.',
      },
    ],
  },
  {
    section: 'AI Provider',
    blurb: 'Pick your provider; only its settings are shown. Keys are stored in the macOS keychain.',
    fields: [
      {
        key: 'LLM_PROVIDER',
        label: 'Provider',
        type: 'select',
        options: [
          { value: 'openrouter', label: 'OpenRouter (default)' },
          { value: 'venice', label: 'Venice' },
          { value: 'ollama', label: 'Ollama (local)' },
          { value: 'bedrock', label: 'AWS Bedrock' },
        ],
        help: 'Which LLM powers insights, chat, and summaries. OpenRouter/Venice use an API key stored encrypted below. Ollama defaults to local inference without an API key; custom endpoints can use the optional server API key. Bedrock authenticates with the Bedrock API key below (or an AWS profile/SSO as a fallback) via the official Anthropic SDK, using the anthropic.-prefixed model IDs below.',
      },
      // ── AWS Bedrock — via the official Anthropic SDK. Shown only for 'bedrock'.
      {
        key: 'AWS_BEARER_TOKEN_BEDROCK',
        label: 'Bedrock API key',
        type: 'password',
        placeholder: 'Paste your Bedrock API key',
        help: 'The standard path. Generate one in the AWS console (Bedrock → API keys). When set, no AWS CLI/SSO/profile is needed — this single key authenticates all Bedrock calls. You still need to set an AWS region below (it selects the Bedrock endpoint, independent of the key). Stored encrypted via the OS keychain, never in plaintext config. Leave blank to fall back to AWS profile/SSO credentials.',
        showWhen: SHOW_BEDROCK,
      },
      {
        key: 'AWS_REGION',
        label: 'AWS region',
        type: 'text',
        placeholder: 'e.g. us-east-1',
        help: 'Region for Bedrock. Required in both auth modes — it selects the Bedrock endpoint whether you use an API key or a profile. Also used as the default Bedrock region.',
        showWhen: SHOW_BEDROCK,
      },
      {
        key: 'AWS_PROFILE',
        label: 'AWS profile (advanced fallback)',
        type: 'text',
        placeholder: 'default',
        help: 'Named profile from ~/.aws/config, used only when no Bedrock API key is set above. Requires AWS CLI + SSO login (aws sso login).',
        showWhen: SHOW_BEDROCK,
      },
      {
        key: 'BEDROCK_MODEL_ID',
        label: 'Chat & summary model',
        type: 'text',
        placeholder: 'anthropic.claude-opus-4-8',
        help: 'Frontier Claude model used for live chat and the end-of-meeting summary. Takes the anthropic.-prefixed Messages-API Bedrock ID.',
        showWhen: SHOW_BEDROCK,
      },
      {
        key: 'BEDROCK_FAST_MODEL_ID',
        label: 'Background model (fast tier)',
        type: 'text',
        placeholder: 'anthropic.claude-haiku-4-5',
        help: 'Cheaper Claude model for background extraction — insights, curation, rolling summary, and vault ingest. Defaults to Haiku for a large cost cut with no visible quality loss.',
        showWhen: SHOW_BEDROCK,
      },
      {
        key: 'BEDROCK_VISION_MODEL_ID',
        label: 'Vision model',
        type: 'text',
        placeholder: 'anthropic.claude-sonnet-4-6',
        help: 'Model used to analyse periodic screen captures.',
        showWhen: SHOW_BEDROCK,
      },
      {
        key: 'BEDROCK_TEMPERATURE',
        label: 'Temperature',
        type: 'number',
        step: '0.1',
        placeholder: '(model default — omitted)',
        help: 'Optional sampling temperature for Bedrock. Leave blank to omit it entirely (recent Claude models reject a temperature parameter). Set only if your chosen model accepts it.',
        showWhen: SHOW_BEDROCK,
      },
      // ── OpenRouter.
      {
        key: 'OPENROUTER_API_KEY',
        label: 'OpenRouter API key',
        type: 'password',
        placeholder: 'sk-or-…',
        help: 'API key for the OpenRouter provider (used when Provider = OpenRouter). Stored encrypted via the OS keychain, never in plaintext config.',
        showWhen: SHOW_OPENROUTER,
      },
      {
        key: 'OPENROUTER_MODEL',
        label: 'OpenRouter model',
        type: 'text',
        placeholder: 'anthropic/claude-3.5-sonnet',
        help: 'Model id used when the provider is OpenRouter.',
        showWhen: SHOW_OPENROUTER,
      },
      {
        key: 'OPENROUTER_FAST_MODEL',
        label: 'OpenRouter background model',
        type: 'text',
        placeholder: '(same as OpenRouter model)',
        help: 'Optional cheaper model id for background extraction on OpenRouter. Blank reuses the main OpenRouter model (no tiering).',
        showWhen: SHOW_OPENROUTER,
      },
      // ── Venice.
      {
        key: 'VENICE_API_KEY',
        label: 'Venice API key',
        type: 'password',
        placeholder: 'venice-…',
        help: 'API key for the Venice provider (used when Provider = Venice). Stored encrypted via the OS keychain, never in plaintext config.',
        showWhen: SHOW_VENICE,
      },
      {
        key: 'VENICE_MODEL',
        label: 'Venice model',
        type: 'text',
        placeholder: 'llama-3.3-70b',
        help: 'Model id used when the provider is Venice.',
        showWhen: SHOW_VENICE,
      },
      {
        key: 'VENICE_FAST_MODEL',
        label: 'Venice background model',
        type: 'text',
        placeholder: '(same as Venice model)',
        help: 'Optional cheaper model id for background extraction on Venice. Blank reuses the main Venice model (no tiering).',
        showWhen: SHOW_VENICE,
      },
      // ── Ollama (fully local).
      {
        key: 'OLLAMA_BASE_URL',
        label: 'Ollama base URL',
        type: 'text',
        placeholder: 'http://localhost:11434/v1',
        help: 'Base URL for the Ollama OpenAI-compatible API (used when Provider = Ollama). Change only if Ollama is running on a non-default port or a remote machine.',
        showWhen: SHOW_OLLAMA,
      },
      {
        key: 'OLLAMA_API_KEY',
        label: 'Local server API key (optional)',
        type: 'password',
        placeholder: 'Required only by authenticated servers',
        help: 'Bearer token for an authenticated OpenAI-compatible server such as oMLX. Stored using the OS keychain.',
        showWhen: SHOW_OLLAMA,
      },
      {
        key: 'OLLAMA_MODEL',
        label: 'Ollama model',
        type: 'text',
        placeholder: 'llama3.1',
        help: 'Model to use with Ollama (e.g. llama3.1, mistral, qwen2.5). Inference runs on the configured server. No API key is needed for standard local Ollama. Screen analysis is unavailable through this connector.',
        showWhen: SHOW_OLLAMA,
      },
      {
        key: 'OLLAMA_FAST_MODEL',
        label: 'Ollama background model',
        type: 'text',
        placeholder: '(same as Ollama model)',
        help: 'Optional smaller model for background extraction on Ollama (insights, curation, vault ingest). Blank reuses the main Ollama model (no tiering).',
        showWhen: SHOW_OLLAMA,
      },
      // ── Cross-provider, but vision-dependent — kept last.
      {
        key: 'SCREEN_ANALYSIS_ENABLED',
        label: 'Screen analysis',
        type: 'select',
        options: [
          { value: '', label: 'On (default — needs a vision model)' },
          { value: 'false', label: 'Off' },
        ],
        help: 'Periodically analyzes your screen for visual context. Needs Screen Recording + a vision-capable provider; auto-skips on text-only providers.',
      },
    ],
  },
  {
    section: 'LLM cost & latency',
    blurb: 'Tune how often and how much the assistant calls the LLM. The defaults minimize cost with no visible quality loss; change only if you want more (or fewer) background calls.',
    fields: [
      {
        key: 'ANALYSIS_MIN_SEGMENTS',
        label: 'Analysis batch size (segments)',
        type: 'number',
        placeholder: '8',
        help: 'Run a background insight pass once this many new transcript segments have accumulated. Higher = fewer, larger analysis calls.',
      },
      {
        key: 'ANALYSIS_MIN_INTERVAL_S',
        label: 'Analysis min interval (seconds)',
        type: 'number',
        placeholder: '45',
        help: 'A smaller batch (3+ segments) also fires once this many seconds have passed since the last analysis, so insights still appear during slow stretches.',
      },
      {
        key: 'CHAT_INSIGHT_ENABLED',
        label: 'Mine chat replies for insights',
        type: 'select',
        options: [
          { value: '', label: 'Off (default)' },
          { value: 'true', label: 'On' },
        ],
        help: 'When on, each chat answer triggers an extra LLM call to extract new terms/context into the insights panel. Off by default — low value per call.',
      },
      {
        key: 'SUMMARY_AB_TEST',
        label: 'Summary A/B test',
        type: 'select',
        options: [
          { value: '', label: 'Off — single summary (default)' },
          { value: 'true', label: 'On — generate both variants' },
        ],
        help: 'When off, the end-of-meeting summary is generated once. When on, two summary styles are generated side-by-side (2× cost and latency) for comparison.',
      },
    ],
  },
  {
    section: 'Transcription',
    blurb: 'Local speech-to-text. The backend is auto-detected from your chip; override only if needed.',
    fields: [
      {
        key: 'WHISPER_BACKEND',
        label: 'Backend',
        type: 'select',
        options: [
          { value: '', label: 'Auto (mlx on Apple Silicon, faster-whisper on Intel)' },
          { value: 'mlx', label: 'mlx-whisper (Apple Silicon GPU)' },
          { value: 'faster-whisper', label: 'faster-whisper (CPU)' },
        ],
        help: 'Force a transcription backend. Auto is recommended.',
      },
      {
        key: 'TRANSCRIPTION_QUALITY',
        label: 'Quality',
        type: 'select',
        options: [
          { value: '', label: 'Auto (chip default)' },
          { value: 'fast', label: 'Fast (lower latency)' },
          { value: 'balanced', label: 'Balanced' },
          { value: 'accurate', label: 'Accurate (slower)' },
        ],
        help: 'Speed/accuracy preset. Maps to the right Whisper model for your chip.',
      },
      {
        key: 'WHISPER_VOCABULARY',
        label: 'Custom vocabulary',
        type: 'text',
        placeholder: 'e.g. Kubernetes, OKR, Acme Corp, Priya',
        help: 'Comma-separated names/jargon/acronyms to bias transcription toward. Improves spelling of terms Whisper mishears. Applies to every meeting.',
      },
      {
        key: 'WHISPER_LANGUAGE',
        label: 'Language',
        type: 'text',
        placeholder: 'en',
        help: 'Spoken language code (e.g. en, es, fr). Defaults to English.',
      },
      {
        key: 'WHISPER_COMPUTE_TYPE',
        label: 'CPU compute type',
        type: 'text',
        placeholder: 'int8',
        help: 'faster-whisper compute type (int8, int8_float16, float16, …).',
      },
      {
        key: 'WHISPER_CPU_THREADS',
        label: 'CPU threads',
        type: 'number',
        placeholder: '0 (auto)',
        help: 'faster-whisper CPU threads. 0 lets it pick.',
      },
    ],
  },
  {
    section: 'Voice profile',
    blurb: 'Controls how your own voice is matched to label your speech as “You”.',
    fields: [
      {
        key: 'VOICE_FINGERPRINT_THRESHOLD',
        label: 'Match threshold',
        type: 'number',
        step: '0.01',
        placeholder: '0.55',
        help: 'Cosine similarity above which a segment is considered yours (0–1).',
      },
      {
        key: 'VOICE_PROFILE_MIN_DURATION_S',
        label: 'Min sample seconds',
        type: 'number',
        step: '0.5',
        placeholder: '5.0',
        help: 'Minimum audio length required to enrol a voice profile.',
      },
      {
        key: 'SPEAKER_REGISTRY_ENABLED',
        label: 'Remember named voices across meetings',
        type: 'select',
        options: [
          { value: '', label: 'Off (default)' },
          { value: 'true', label: 'On' },
        ],
        help: 'Opt-in. Stores voice fingerprints of people you name so they are labelled from their first words in future meetings. Privacy: these fingerprints are biometric-adjacent — only names you explicitly confirm are stored, never any audio — delete anyone anytime from the settings menu.',
      },
      {
        key: 'SPEAKER_REGISTRY_PATH',
        label: 'Known-voices file',
        type: 'text',
        placeholder: 'speaker-registry.json (beside meetings)',
        help: 'Where the known-voice fingerprints are stored. Leave blank for the default location next to your meetings folder. Deleting this file forgets everyone.',
      },
    ],
  },
  {
    section: 'Auto-stop',
    blurb: 'When the meeting ends itself. All values in the units shown.',
    fields: [
      {
        key: 'AUTOSTOP_INACTIVITY_MIN',
        label: 'Inactivity (minutes)',
        type: 'number',
        placeholder: '10',
        help: 'Stop after this many minutes with no speech.',
      },
      {
        key: 'AUTOSTOP_WS_GRACE_S',
        label: 'Window-close grace (seconds)',
        type: 'number',
        placeholder: '60',
        help: 'Stop this long after the last app window closes mid-meeting.',
      },
      {
        key: 'AUTOSTOP_UNDO_WINDOW_S',
        label: 'Undo window (seconds)',
        type: 'number',
        placeholder: '30',
        help: 'How long the “undo auto-stop” banner stays before stopping.',
      },
    ],
  },
  {
    section: 'Tuning',
    blurb: 'Advanced transcription / diarization knobs. The defaults are well tuned — change these only if you know what you are doing. Leave blank to use the default shown.',
    fields: [
      // Vocabulary prompt assembly
      {
        key: 'WHISPER_PROMPT_INCLUDE_ATTENDEES',
        label: 'Bias toward attendee names',
        type: 'select',
        options: [
          { value: '', label: 'Off (default)' },
          { value: 'true', label: 'On' },
        ],
        help: 'Also feed the meeting attendee list into the transcription vocabulary prompt.',
      },
      {
        key: 'WHISPER_PROMPT_INCLUDE_SPEAKER_NAMES',
        label: 'Bias toward confirmed speaker names',
        type: 'select',
        options: [
          { value: '', label: 'Off (default)' },
          { value: 'true', label: 'On' },
        ],
        help: 'Also feed names you assign to speakers during a meeting into the vocabulary prompt.',
      },
      {
        key: 'WHISPER_VOCAB_FROM_VAULT',
        label: 'Bias toward vault names',
        type: 'select',
        options: [
          { value: '', label: 'Off (default)' },
          { value: 'true', label: 'On' },
        ],
        help: 'Also feed people and project titles from your knowledge vault into the transcription vocabulary prompt, so their spellings come out right without any configuration.',
      },
      {
        key: 'WHISPER_PROMPT_MAX_TOKENS',
        label: 'Vocabulary prompt cap (tokens)',
        type: 'number',
        placeholder: '150',
        help: 'Soft limit on the assembled vocabulary prompt length.',
      },
      // Whisper decode params
      {
        key: 'WHISPER_NO_SPEECH_THRESHOLD',
        label: 'No-speech threshold',
        type: 'number',
        step: '0.05',
        placeholder: '0.6',
        help: 'Above this no-speech probability, a segment is dropped as silence.',
      },
      {
        key: 'WHISPER_COMPRESSION_RATIO_THRESHOLD',
        label: 'Compression-ratio threshold',
        type: 'number',
        step: '0.1',
        placeholder: '2.4',
        help: 'Gzip compression ratio above which output is treated as a hallucination loop.',
      },
      {
        key: 'WHISPER_LOGPROB_THRESHOLD',
        label: 'Log-probability threshold',
        type: 'number',
        step: '0.1',
        placeholder: '-1.0',
        help: 'Average token log-prob below which a segment is discarded as low-confidence.',
      },
      {
        key: 'WHISPER_TEMPERATURE',
        label: 'Decoding temperature',
        type: 'number',
        step: '0.1',
        placeholder: '0.0',
        help: '0 = greedy/deterministic. Higher adds randomness (rarely useful live).',
      },
      {
        key: 'WHISPER_BEAM_SIZE',
        label: 'Beam size (CPU backend)',
        type: 'number',
        placeholder: '1',
        help: 'faster-whisper beam width. 1 = greedy (lowest latency).',
      },
      {
        key: 'WHISPER_CONDITION_ON_PREVIOUS_TEXT',
        label: 'Condition on previous text',
        type: 'select',
        options: [
          { value: '', label: 'Off (default)' },
          { value: 'true', label: 'On' },
        ],
        help: 'Feed prior output back into the decoder. Off avoids repetition loops; no effect on our per-turn buffers.',
      },
      {
        key: 'WHISPER_WORD_TIMESTAMPS',
        label: 'Word-level timestamps',
        type: 'select',
        options: [
          { value: '', label: 'Off (default)' },
          { value: 'true', label: 'On' },
        ],
        help: 'Decode per-word timestamps (groundwork for click-a-word → audio and tighter alignment). Costs some decode speed; segment output is unchanged today.',
      },
      // Voice activity detection
      {
        key: 'VAD_THRESHOLD',
        label: 'VAD speech threshold',
        type: 'number',
        step: '0.05',
        placeholder: '0.5',
        help: 'Silero speech probability above which a frame counts as speech (0–1).',
      },
      {
        key: 'VAD_MIN_SILENCE_MS',
        label: 'End-of-turn silence (ms)',
        type: 'number',
        placeholder: '800',
        help: 'Silence this long ends a speech turn and triggers transcription.',
      },
      {
        key: 'VAD_MIN_SPEECH_MS',
        label: 'Min speech to keep (ms)',
        type: 'number',
        placeholder: '500',
        help: 'Speech turns shorter than this are discarded as noise.',
      },
      {
        key: 'VAD_MAX_SPEECH_S',
        label: 'Max turn before hard cut (s)',
        type: 'number',
        placeholder: '20',
        help: 'A turn longer than this is force-transcribed even without a pause.',
      },
      {
        key: 'VAD_PROGRESSIVE_FINALIZE',
        label: 'Progressive finalization',
        type: 'select',
        options: [
          { value: '', label: 'On (default)' },
          { value: 'false', label: 'Off' },
        ],
        help: 'Emit long monologues in chunks at short pauses so text appears live.',
      },
      {
        key: 'VAD_SOFT_CUT_S',
        label: 'Soft-cut after (s)',
        type: 'number',
        placeholder: '8',
        help: 'A turn longer than this is chunked at the next short pause.',
      },
      {
        key: 'VAD_SOFT_CUT_SILENCE_MS',
        label: 'Soft-cut pause (ms)',
        type: 'number',
        placeholder: '200',
        help: 'Minimum mid-turn pause used as a progressive-finalization cut point.',
      },
      {
        key: 'SILENCE_RMS_THRESHOLD',
        label: 'Silence RMS threshold',
        type: 'number',
        step: '0.001',
        placeholder: '0.003',
        help: 'Audio quieter than this RMS is skipped before transcription.',
      },
      // Speaker diarization
      {
        key: 'SPEAKER_SIMILARITY_THRESHOLD',
        label: 'Same-speaker similarity',
        type: 'number',
        step: '0.01',
        placeholder: '0.55',
        help: 'Cosine similarity above which a voice matches an existing speaker (0–1).',
      },
      {
        key: 'SPEAKER_EMBEDDING_UPDATE_RATE',
        label: 'Speaker centroid update rate',
        type: 'number',
        step: '0.05',
        placeholder: '0.1',
        help: 'How fast a matched speaker’s reference voiceprint adapts (0–1).',
      },
      {
        key: 'SPEAKER_MIN_AUDIO_DURATION',
        label: 'Min seconds for new speaker',
        type: 'number',
        step: '0.5',
        placeholder: '2.0',
        help: 'Minimum clip length to create a brand-new speaker cluster.',
      },
      {
        key: 'SPEAKER_MIN_EMBED_DURATION',
        label: 'Min seconds to match speaker',
        type: 'number',
        step: '0.5',
        placeholder: '1.0',
        help: 'Shorter clips down to this can match an existing speaker but not create one.',
      },
      {
        key: 'SPEAKER_RECLUSTER_DISTANCE',
        label: 'Reconcile merge distance',
        type: 'number',
        step: '0.05',
        placeholder: '0.55',
        help: 'Cosine distance for the end-of-meeting pass that merges split speakers.',
      },
      {
        key: 'SPEAKER_EMBEDDING_MODEL',
        label: 'Speaker embedding model',
        type: 'text',
        placeholder: 'speechbrain/spkrec-ecapa-voxceleb',
        help: 'HuggingFace source for the ECAPA-TDNN voice embedding model.',
      },
      // Mic-bleed guards
      {
        key: 'BLEED_GUARD_WINDOW_S',
        label: 'Bleed look-back (s)',
        type: 'number',
        placeholder: '3.0',
        help: 'How far back to check for concurrent system audio when guarding mic bleed.',
      },
      {
        key: 'BLEED_GUARD_RMS_RATIO',
        label: 'Bleed RMS ratio',
        type: 'number',
        step: '0.05',
        placeholder: '0.5',
        help: 'Mic counts as bleed if its RMS is below system RMS × this.',
      },
      {
        key: 'BLEED_GUARD_MIN_SYSTEM_RMS',
        label: 'Bleed min system RMS',
        type: 'number',
        step: '0.005',
        placeholder: '0.02',
        help: 'Ignore system audio quieter than this when checking for bleed.',
      },
      {
        key: 'VAD_BLEED_MIN_OVERLAP_RATIO',
        label: 'Bleed overlap ratio',
        type: 'number',
        step: '0.05',
        placeholder: '0.5',
        help: 'Mic window must overlap a system VAD turn by at least this fraction.',
      },
      {
        key: 'VAD_BLEED_MIC_PROB_CEILING',
        label: 'Bleed mic prob ceiling',
        type: 'number',
        step: '0.05',
        placeholder: '0.8',
        help: 'Mic VAD peak must be below this to be treated as possible bleed.',
      },
      {
        key: 'VAD_BLEED_SYSTEM_PROB_FLOOR',
        label: 'Bleed system prob floor',
        type: 'number',
        step: '0.05',
        placeholder: '0.9',
        help: 'System VAD peak must be at least this to override the mic as bleed.',
      },
    ],
  },
  {
    section: 'Advanced',
    blurb: 'You usually do not need these.',
    fields: [
      {
        key: 'HF_TOKEN',
        label: 'HuggingFace token',
        type: 'password',
        placeholder: 'Optional — speeds up model downloads',
        help: 'Token used when downloading Whisper / speaker models. Stored encrypted, never in plaintext config.',
      },
      {
        key: 'LOG_CONTENT',
        label: 'Log transcript content',
        type: 'select',
        options: [
          { value: '', label: 'On (default)' },
          { value: 'false', label: 'Off — log lengths only' },
        ],
        help: 'When off, log lines that include transcript or chat text log character counts instead of content, reducing what ends up in server.log.',
      },
    ],
  },
];

// Flat list of every env key the schema manages — used to scrub stale keys and
// to decide whether a settings change requires restarting the server.
function allKeys() {
  const keys = [];
  for (const group of SCHEMA) {
    for (const field of group.fields) keys.push(field.key);
  }
  return keys;
}

// Password-type field keys — these go through safeStorage (loadSecrets) and
// must never be emitted by serverEnvFromConfig even if they leaked into config.json.
const _passwordKeySet = new Set(
  SCHEMA.flatMap((g) => g.fields.filter((f) => f.type === 'password').map((f) => f.key)),
);

// Turn saved config values into a clean env-var object. Empty strings are
// dropped so the backend falls back to its own defaults rather than seeing "".
// Password fields are intentionally excluded — they are injected via loadSecrets().
function serverEnvFromConfig(config) {
  const env = {};
  for (const key of allKeys()) {
    if (_passwordKeySet.has(key)) continue; // secrets injected by loadSecrets()
    const value = config[key];
    if (value === undefined || value === null) continue;
    const str = String(value).trim();
    if (str !== '') env[key] = str;
  }
  return env;
}

module.exports = { SCHEMA, allKeys, serverEnvFromConfig };
