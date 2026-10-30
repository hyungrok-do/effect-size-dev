// The model roles and the names people see for them, for both editions.
//
//   manager  reads the problem, plans the search, checks overlap, summarizes, reviews flagged extractions,
//            reads each paper's estimands and models
//   fetcher  downloads what the browser may not (Claude only: OpenAI has no server-side fetch)
//   worker   screens every record and reads every paper
//   code     runs the searches, gets the text, checks every quote
//
// Activity logs and papers saved before OpenAI was offered name the Claude models instead of the
// roles ('opus', 'sonnet', 'haiku'); roleOf reads those too.

const LEGACY = { opus: 'manager', sonnet: 'fetcher', haiku: 'worker' };
export const roleOf = (agent) => LEGACY[agent] || agent;

// The models of papers extracted before each paper recorded its model (extracted_model): Claude's,
// the only ones there were.
export const LEGACY_MODELS = { manager: 'claude-opus-5-5', fetcher: 'claude-sonnet-5-5', worker: 'claude-haiku-5-5' };

// A model id as people know it: "claude-opus-5-5" -> "Opus 5.5", "gpt-6.1-sol" -> "GPT-6.1 Sol".
export function modelLabel(model) {
  const id = String(model || '').replace(/-\d{4}-\d{2}-\d{2}$/, ''); // a dated snapshot is still its model
  const claude = /^claude-([a-z]+)-(\d+)(?:-(\d+))?/.exec(id);
  if (claude) return `${claude[1][0].toUpperCase()}${claude[1].slice(1)} ${claude[2]}${claude[3] ? `.${claude[3]}` : ''}`;
  const gpt = /^gpt-([\d.]+)(?:-(.+))?$/.exec(id);
  if (gpt) {
    const rest = (gpt[2] || '').split('-').filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1));
    return [`GPT-${gpt[1]}`, ...rest].join(' ');
  }
  return id;
}

// The name of each role for a set of models ({manager, fetcher?, worker}); a role without a model is left out.
export function roleNames(models) {
  const names = { code: 'Code' };
  for (const role of ['manager', 'fetcher', 'worker']) if (models?.[role]) names[role] = modelLabel(models[role]);
  return names;
}

// Who did a logged step: the models its calls were served by, or else the role's current name.
export function whoDid(event, names) {
  const models = [...new Set((event.models || []).map(modelLabel))];
  return models.length ? models.join(', ') : names[roleOf(event.agent)] || event.agent;
}

// The three ways to run the agents, as the Agents page offers them. `credential` is what the person
// gives: an API key, or a ChatGPT sign-in held by the local runtime (also used by the hosted page's
// companion connection).
export const PROVIDERS = {
  anthropic: {
    label: 'Claude API', vendor: 'Anthropic', credential: 'key', billing: 'Anthropic API key · billed per use',
    keyName: 'Anthropic API key', placeholder: 'sk-ant-…', prefix: 'sk-ant-', console: ['https://console.anthropic.com/settings/keys', 'Anthropic Console'],
  },
  openai: {
    label: 'OpenAI API', vendor: 'OpenAI', credential: 'key', billing: 'OpenAI API key · billed per use',
    keyName: 'OpenAI API key', placeholder: 'sk-…', prefix: 'sk-', console: ['https://platform.openai.com/api-keys', 'OpenAI dashboard'],
  },
  chatgpt: {
    label: 'OpenAI subscription', vendor: 'OpenAI', credential: 'sign-in', billing: 'ChatGPT Plus or Pro · no key',
  },
};

export const looksLikeAnthropicKey = (key) => /^sk-ant-[A-Za-z0-9_-]{8,}$/.test(key);
export const looksLikeOpenAIKey = (key) => /^sk-[A-Za-z0-9_-]{20,}$/.test(key) && !key.startsWith('sk-ant-');

// What a pasted key is, by its shape: 'anthropic', 'openai', 'ncbi', or null when it is none of these.
export function keyKind(key) {
  if (looksLikeAnthropicKey(key)) return 'anthropic';
  if (looksLikeOpenAIKey(key)) return 'openai';
  if (/^[0-9a-f]{36}$/i.test(key)) return 'ncbi';
  return null;
}

// Admin keys manage a whole organization (its members, keys and billing), so a page that only runs
// models has no use for one. They have the shape of an ordinary key, so this is checked first.
const isAdminKey = (key) => /^sk-ant-admin/.test(key) || /^sk-admin-/.test(key);

// What is wrong with a key pasted for `provider`, if anything, in words for the page. `wrong` when it is
// surely another key (an admin key, the other vendor's, or an NCBI key): that one is never sent. A key
// of an unfamiliar shape only gets a warning, so a new key format doesn't lock anyone out.
export function keyProblem(provider, key) {
  if (!key) return null;
  if (isAdminKey(key)) {
    return { wrong: true, text: 'This is an admin key, which can manage the whole account; use an ordinary API key. It is not sent.' };
  }
  const { keyName, vendor, prefix } = PROVIDERS[provider];
  const kind = keyKind(key);
  if (kind === 'ncbi') {
    return { wrong: true, text: `This looks like an NCBI API key, not an ${keyName}; an NCBI key goes in its own box. It is not sent to ${vendor}.` };
  }
  if (kind && kind !== provider) {
    const other = PROVIDERS[kind];
    return { wrong: true, text: `This is an ${other.keyName}, not an ${keyName}: choose ${other.label} to use it. It is not sent to ${vendor}.` };
  }
  if (kind !== provider) return { wrong: false, text: `This doesn't look like an ${keyName}: those start with ${prefix}.` };
  return null;
}

// The models each provider's roles can use, best first. A ChatGPT plan lists its own (the sign-in's catalog).
export const MODEL_CHOICES = {
  anthropic: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'],
  openai: ['gpt-6.1-sol', 'gpt-6-luna'],
};

// What each role does, for the Agents page and the cards.
export const ROLE_INFO = {
  manager: ['Manager', 'Reads the problem, plans the search, reviews flagged extractions, reads each paper’s estimands and models, '
    + 'checks overlap, writes the summary'],
  fetcher: ['Fetcher', 'Downloads what the browser may not read (Claude only)'],
  worker: ['Workers', 'Screen every record and extract every paper'],
  code: ['Code', 'Runs the searches, gets the text, checks every quote'],
};
