// The model roles and the names people see for them, for both editions.
//
//   manager  reads the problem, plans the search, checks overlap, summarizes, reviews flagged extractions
//   fetcher  downloads what the browser may not (Claude only: OpenAI has no server-side fetch)
//   worker   screens every record and reads every paper
//   code     runs the searches, gets the text, checks every quote
//
// Activity logs and papers saved before OpenAI was offered name the Claude models instead of the
// roles ('opus', 'sonnet', 'haiku'); roleOf reads those too.

const LEGACY = { opus: 'manager', sonnet: 'fetcher', haiku: 'worker' };
export const roleOf = (agent) => LEGACY[agent] || agent;

// A model id as people know it: "claude-opus-5-5" -> "Opus 5.5", "gpt-6.1-sol" -> "GPT-6.1 Sol".
export function modelLabel(model) {
  const id = String(model || '');
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
