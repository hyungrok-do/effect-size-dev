// The search specification as structured tables: a readable view of each part, edited one part at a
// time. Shared by the browser edition (site/app.js) and the local server's page (web/app.js); the
// local server serves this folder at /lib. Styles are in spec-panel.css.

export const DESIGNS = {
  parallel_rct: 'Parallel-group randomized trial',
  cluster_rct: 'Cluster randomized trial',
  crossover_rct: 'Crossover trial',
  single_arm: 'Single-arm study',
  cohort: 'Cohort study',
  case_control: 'Case–control study',
  cross_sectional: 'Cross-sectional study',
  other: 'Other',
};
export const OUTCOME_TYPES = {
  continuous: 'Continuous', binary: 'Binary', count: 'Count',
  time_to_event: 'Time to event', ordinal: 'Ordinal', unclear: 'Unclear',
};
export const STATISTICS = {
  n: 'n', mean: 'Mean', sd: 'SD', se: 'SE', ci_lower: 'CI lower', ci_upper: 'CI upper', median: 'Median',
  q1: 'Q1', q3: 'Q3', min: 'Min', max: 'Max', events: 'Events', percent: '%', mean_change: 'Mean change',
  sd_change: 'SD of change', mean_difference: 'Mean difference', standardized_mean_difference: 'SMD',
  odds_ratio: 'Odds ratio', risk_ratio: 'Risk ratio', hazard_ratio: 'Hazard ratio', risk_difference: 'Risk difference',
  p_value: 'p', t_statistic: 't', icc: 'ICC', correlation: 'Correlation', other: 'Other',
};
const COMPARATOR_KINDS = {
  placebo: 'Placebo', waitlist: 'Waitlist', usual_care: 'Usual care', no_intervention: 'No intervention',
  active: 'Active comparator', other: 'Other',
};
const ROLES = { primary: 'Primary', secondary: 'Secondary' };
const PER = { each_arm: 'Each arm', between_groups: 'Between groups', one_group: 'One group' };
const ELEMENTS = {
  population: 'Population', intervention: 'Intervention', comparator: 'Comparator', outcome: 'Outcome', design: 'Design', other: 'Other',
};
const CONCEPTS = {
  population: 'Population', condition: 'Condition', intervention: 'Intervention', comparator: 'Comparator',
  outcome: 'Outcome', design: 'Design', other: 'Other',
};
const KEEP = { include: 'Include', exclude: 'Exclude' };
// What a statistic may be typed as: its key or its label, in any case.
const STATISTIC_NAMES = new Map(Object.entries(STATISTICS).flatMap(([key, label]) => [
  [key, key], [key.replaceAll('_', ' '), key], [label.toLowerCase(), key],
]));

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

const badge = (text, kind = '') => h('span', { class: `badge ${kind}`.trim() }, text);
const missing = (text = 'Not stated') => h('span', { class: 'missing' }, text);
const splitList = (text) => text.split(/[,;\n]/).map((s) => s.trim()).filter(Boolean);

// A specification saved before the structured fields, in the current shape. Mirrors upgrade_spec in
// python/esf/schemas.py.
export function upgradeSpec(spec) {
  if (spec.population && typeof spec.population === 'object') return spec;
  const timepoint = spec.timepoint || '';
  const outcomes = (spec.outcomes || []).map((name, i) => ({
    name, measure: '', role: i ? 'secondary' : 'primary', type: spec.outcome_type || 'unclear', timepoint,
  }));
  const first = outcomes.length ? outcomes[0].name : '';
  return {
    question: spec.question || '',
    design: spec.design || 'other',
    population: { description: spec.population || '', condition: '', age: '', setting: '' },
    intervention: { name: spec.intervention || '', delivery: '', dose: '', duration: '' },
    comparator: { name: spec.comparator || '', kind: 'other' },
    outcomes,
    // The old free-text rows keep their wording as the purpose.
    statistics_needed: (spec.statistics_needed || []).map((text) => ({
      outcome: first, statistics: [], per: 'each_arm', timepoint, purpose: text,
    })),
    stated_values: spec.stated_values || [],
    inclusion_criteria: (spec.inclusion_criteria || []).map((rule) => ({ about: 'other', rule })),
    exclusion_criteria: (spec.exclusion_criteria || []).map((rule) => ({ about: 'other', rule })),
    search_terms: spec.search_terms?.length ? [{ concept: 'other', terms: [...spec.search_terms] }] : [],
    open_questions: spec.open_questions || [],
  };
}

// -- editors -------------------------------------------------------------------------

// One input for a field: {key, label, kind: 'text' | 'select' | 'list' | 'statistics', options}.
function fieldInput(field, value, attrs = {}) {
  if (field.kind === 'select') {
    const select = h('select', attrs, Object.entries(field.options).map(([key, label]) => h('option', { value: key }, label)));
    select.value = value;
    return select;
  }
  const input = h('input', { type: 'text', ...attrs });
  if (field.kind === 'list') input.value = (value || []).join(', ');
  else if (field.kind === 'statistics') {
    input.value = (value || []).map((key) => STATISTICS[key] || key).join(', ');
    input.dataset.kind = 'statistics';
    input.title = `Separate with commas. Known: ${Object.values(STATISTICS).join(', ')}`;
    input.addEventListener('input', () => checkStatistics(input));
  } else input.value = value ?? '';
  return input;
}

function checkStatistics(input) {
  const unknown = splitList(input.value).filter((name) => !STATISTIC_NAMES.has(name.toLowerCase()));
  input.setCustomValidity(unknown.length ? `Not a statistic this tool knows: ${unknown.join(', ')}. ${input.title}` : '');
  return !unknown.length;
}

function readInput(field, input) {
  if (field.kind === 'list') return splitList(input.value);
  if (field.kind === 'statistics') return splitList(input.value).map((name) => STATISTIC_NAMES.get(name.toLowerCase())).filter(Boolean);
  return field.kind === 'select' ? input.value : input.value.trim();
}

// A list of records edited as cards, one per record: its fields with their labels, and Remove.
// A field marked wide takes a whole line; `list` names a datalist of suggestions for a text field.
function rowsEditor(rows, fields, blank, addLabel) {
  const cards = h('div', { class: 'row-cards' });
  const addRow = (row) => {
    const inputs = fields.map((f) => fieldInput(f, row[f.key], { 'aria-label': f.label, list: f.list }));
    const card = h('div', { class: 'row-card' },
      h('div', { class: 'row-fields' }, fields.map((f, i) => h('label', { class: f.wide ? 'row-field wide' : 'row-field' },
        h('span', {}, f.label), inputs[i]))),
      h('button', { type: 'button', class: 'secondary small remove', onclick: () => card.remove() }, 'Remove'));
    card.inputs = inputs;
    cards.append(card);
    return card;
  };
  rows.forEach(addRow);
  const editor = h('div', { class: 'rows-editor' }, cards,
    h('button', { type: 'button', class: 'secondary small add-row', onclick: () => addRow(blank()).inputs[0].focus() }, addLabel));
  editor.read = () => [...cards.children].map((card) => Object.fromEntries(fields.map((f, i) => [f.key, readInput(f, card.inputs[i])])));
  return editor;
}

// Labeled fields for one record's parts, e.g. the population's description, condition, age and setting.
function fieldsEditor(groups) {
  const editor = h('div', { class: 'fields-editor' }, groups.map(({ id: group, title, fields, record }) => h('fieldset', {},
    h('legend', {}, title),
    h('div', { class: 'grid' }, fields.map((f) => {
      const id = `spec-${group}-${f.key}`;
      f.input = fieldInput(f, record[f.key], { id });
      return h('div', { class: f.wide ? 'field wide' : 'field' }, h('label', { for: id }, f.label), f.input);
    })))));
  editor.read = () => groups.map(({ fields }) => Object.fromEntries(fields.map((f) => [f.key, readInput(f, f.input)])));
  return editor;
}

// -- the panel -----------------------------------------------------------------------------

const PARTS = {
  comparison: {
    title: 'The comparison',
    view: (spec) => {
      const facets = (pairs) => {
        const shown = pairs.filter(([, v]) => v);
        return shown.length ? h('span', { class: 'facets' }, shown.map(([k, v]) => h('span', {}, h('span', { class: 'k' }, k), ' ', v))) : null;
      };
      const { population: p, intervention: i, comparator: c } = spec;
      return h('table', { class: 'spec-table kv' }, h('tbody', {},
        h('tr', {}, h('th', { scope: 'row' }, 'Population'),
          h('td', {}, h('strong', {}, p.description || missing()), facets([['Condition', p.condition], ['Age', p.age], ['Setting', p.setting]]))),
        h('tr', {}, h('th', { scope: 'row' }, 'Intervention'),
          h('td', {}, h('strong', {}, i.name || missing()), facets([['Delivery', i.delivery], ['Dose', i.dose], ['Duration', i.duration]]))),
        h('tr', {}, h('th', { scope: 'row' }, 'Compared with'),
          h('td', {}, h('strong', {}, c.name || missing('None')), ' ', badge(COMPARATOR_KINDS[c.kind] || c.kind))),
        h('tr', {}, h('th', { scope: 'row' }, 'Design'), h('td', {}, DESIGNS[spec.design] || spec.design)),
      ));
    },
    edit: (spec) => {
      const editor = fieldsEditor([
        { id: 'question', title: 'The question', record: spec, fields: [
          { key: 'question', label: 'In one sentence', wide: true },
          { key: 'design', label: 'Design', kind: 'select', options: DESIGNS },
        ] },
        { id: 'population', title: 'Population', record: spec.population, fields: [
          { key: 'description', label: 'Who', wide: true }, { key: 'condition', label: 'Condition', wide: true },
          { key: 'age', label: 'Age' }, { key: 'setting', label: 'Setting' },
        ] },
        { id: 'intervention', title: 'Intervention', record: spec.intervention, fields: [
          { key: 'name', label: 'What', wide: true }, { key: 'delivery', label: 'Delivery' },
          { key: 'dose', label: 'Dose' }, { key: 'duration', label: 'Duration' },
        ] },
        { id: 'comparator', title: 'Compared with', record: spec.comparator, fields: [
          { key: 'name', label: 'What' }, { key: 'kind', label: 'Kind', kind: 'select', options: COMPARATOR_KINDS },
        ] },
      ]);
      editor.apply = (draft) => {
        const [question, population, intervention, comparator] = editor.read();
        Object.assign(draft, question, { population, intervention, comparator });
      };
      return editor;
    },
  },
  outcomes: {
    title: 'Outcomes',
    view: (spec) => table(['Role', 'Outcome', 'Measure', 'Type', 'When'], spec.outcomes.map((o) => [
      o.role === 'primary' ? badge('Primary', 'accent') : ROLES[o.role] || o.role, h('strong', {}, o.name || missing()),
      o.measure || missing(), OUTCOME_TYPES[o.type] || o.type, o.timepoint || missing(),
    ]), 'No outcomes yet.'),
    edit: (spec) => {
      const editor = rowsEditor(spec.outcomes, [
        { key: 'name', label: 'Outcome' }, { key: 'measure', label: 'Measure' },
        { key: 'role', label: 'Role', kind: 'select', options: ROLES }, { key: 'type', label: 'Type', kind: 'select', options: OUTCOME_TYPES },
        { key: 'timepoint', label: 'When' },
      ], () => ({ role: 'secondary', name: '', measure: '', type: 'continuous', timepoint: '' }), 'Add an outcome');
      editor.apply = (draft) => { draft.outcomes = editor.read().filter((o) => o.name || o.measure); };
      return editor;
    },
  },
  numbers: {
    title: 'Numbers to find',
    view: (spec) => table(['Outcome', 'Find', 'For', 'When', 'Purpose'], spec.statistics_needed.map((s) => [
      h('strong', {}, s.outcome || missing('Any')), s.statistics.length ? s.statistics.map((k) => STATISTICS[k] || k).join(' · ') : missing('—'),
      PER[s.per] || s.per, s.timepoint || missing('Any'), s.purpose,
    ]), 'Nothing listed yet.'),
    edit: (spec) => {
      const editor = rowsEditor(spec.statistics_needed, [
        { key: 'outcome', label: 'Outcome', list: 'spec-outcome-names' }, { key: 'statistics', label: 'Find', kind: 'statistics' },
        { key: 'per', label: 'For', kind: 'select', options: PER }, { key: 'timepoint', label: 'When' },
        { key: 'purpose', label: 'Purpose', wide: true },
      ], () => ({ outcome: spec.outcomes[0]?.name || '', statistics: [], per: 'each_arm', timepoint: '', purpose: '' }), 'Add a row');
      // The outcome field suggests the outcomes listed above.
      editor.prepend(h('datalist', { id: 'spec-outcome-names' }, spec.outcomes.map((o) => h('option', { value: o.name }))));
      editor.apply = (draft) => { draft.statistics_needed = editor.read().filter((s) => s.outcome || s.statistics.length || s.purpose); };
      return editor;
    },
  },
  stated: {
    title: 'Values your paragraph states',
    view: (spec) => table(['What', 'Value', 'Where your paragraph says it'], spec.stated_values.map((v) => [
      v.parameter, h('strong', {}, v.value), h('q', { class: 'quote' }, v.quote),
    ]), 'None.'),
  },
  screening: {
    title: 'Screening rules',
    folded: true,
    hint: 'How Haiku decides which titles and abstracts to keep.',
    count: (spec) => `${spec.inclusion_criteria.length} to include · ${spec.exclusion_criteria.length} to exclude`,
    view: (spec) => table(['Keep or drop', 'About', 'Rule'], [
      ...spec.inclusion_criteria.map((c) => [badge('Include', 'ok'), ELEMENTS[c.about] || c.about, c.rule]),
      ...spec.exclusion_criteria.map((c) => [badge('Exclude', 'err'), ELEMENTS[c.about] || c.about, c.rule]),
    ], 'No rules yet.'),
    edit: (spec) => {
      const rows = [
        ...spec.inclusion_criteria.map((c) => ({ keep: 'include', ...c })),
        ...spec.exclusion_criteria.map((c) => ({ keep: 'exclude', ...c })),
      ];
      const editor = rowsEditor(rows, [
        { key: 'keep', label: 'Keep or drop', kind: 'select', options: KEEP },
        { key: 'about', label: 'About', kind: 'select', options: ELEMENTS }, { key: 'rule', label: 'Rule', wide: true },
      ], () => ({ keep: 'include', about: 'population', rule: '' }), 'Add a rule');
      editor.apply = (draft) => {
        const all = editor.read().filter((r) => r.rule);
        draft.inclusion_criteria = all.filter((r) => r.keep === 'include').map(({ about, rule }) => ({ about, rule }));
        draft.exclusion_criteria = all.filter((r) => r.keep === 'exclude').map(({ about, rule }) => ({ about, rule }));
      };
      return editor;
    },
  },
  terms: {
    title: 'Search terms',
    folded: true,
    hint: 'Synonyms, MeSH headings and instrument names, by concept. Opus builds the search from these.',
    count: (spec) => `${spec.search_terms.reduce((n, g) => n + g.terms.length, 0)} in ${spec.search_terms.length} groups`,
    view: (spec) => table(['Concept', 'Terms'], spec.search_terms.map((g) => [
      h('strong', {}, CONCEPTS[g.concept] || g.concept), h('ul', { class: 'chips' }, g.terms.map((t) => h('li', {}, t))),
    ]), 'None yet.'),
    edit: (spec) => {
      const editor = rowsEditor(spec.search_terms, [
        { key: 'concept', label: 'Concept', kind: 'select', options: CONCEPTS },
        { key: 'terms', label: 'Terms (separate with commas)', kind: 'list', wide: true },
      ], () => ({ concept: 'outcome', terms: [] }), 'Add a group');
      editor.apply = (draft) => { draft.search_terms = editor.read().filter((g) => g.terms.length); };
      return editor;
    },
  },
};

function table(head, rows, empty) {
  if (!rows.length) return h('p', { class: 'muted small empty' }, empty);
  return h('div', { class: 'table-wrap' }, h('table', { class: 'spec-table' },
    h('thead', {}, h('tr', {}, head.map((label) => h('th', { scope: 'col' }, label)))),
    h('tbody', {}, rows.map((cells) => h('tr', {}, cells.map((cell, i) => h('td', { 'data-label': head[i] }, h('span', {}, cell))))))));
}

export class SpecPanel {
  // onSave(spec) keeps an edit; it is called when a part's Done is pressed.
  constructor(root, { onSave }) {
    this.root = root;
    this.onSave = onSave;
    this.draft = null;
    this.editors = new Map(); // part -> its open editor
    this.folded = new Map(); // part -> open or not, for the parts that fold
    this.isDisabled = false;
  }

  show(spec) {
    this.draft = structuredClone(upgradeSpec(spec));
    this.editors.clear();
    this.render();
  }

  set disabled(value) {
    this.isDisabled = value;
    for (const el of this.root.querySelectorAll('button, input, select')) el.disabled = value;
  }

  // The specification with any open editor applied, or null when an editor has a value to fix.
  collect() {
    const open = [...this.editors.keys()];
    const failed = open.find((part) => !this.apply(part));
    if (open.length) this.render();
    if (failed) {
      this.invalid(this.editors.get(failed))?.reportValidity();
      return null;
    }
    return structuredClone(this.draft);
  }

  invalid(editor) {
    for (const input of editor.querySelectorAll('input[data-kind="statistics"]')) checkStatistics(input);
    return [...editor.querySelectorAll('input')].find((input) => !input.checkValidity());
  }

  // Apply a part's editor to the draft and close it; false (and the editor stays) when a value needs fixing.
  apply(part) {
    const editor = this.editors.get(part);
    const bad = this.invalid(editor);
    if (bad) {
      bad.focus();
      bad.reportValidity();
      return false;
    }
    editor.apply(this.draft);
    this.editors.delete(part);
    return true;
  }

  async done(part) {
    if (!this.apply(part)) return;
    this.render();
    this.root.querySelector(`[data-part="${part}"] .edit-box`)?.focus();
    await this.onSave(structuredClone(this.draft));
  }

  edit(part) {
    this.editors.set(part, PARTS[part].edit(this.draft));
    this.render();
    this.editors.get(part).querySelector('input, select')?.focus();
  }

  render() {
    const spec = this.draft;
    const primary = spec.outcomes.find((o) => o.role === 'primary') || spec.outcomes[0];
    const nodes = [
      h('div', { class: 'spec-question' },
        h('p', { class: 'question', id: 'spec-question' }, spec.question || missing('No question yet.')),
        h('p', { class: 'tags' }, badge(DESIGNS[spec.design] || spec.design, 'accent'),
          primary && badge(`${OUTCOME_TYPES[primary.type] || primary.type} primary outcome`, 'accent'))),
      spec.open_questions.length > 0 && h('div', { class: 'callout warn', 'data-part': 'questions' },
        h('h3', {}, 'Worth deciding before you search'), h('ul', {}, spec.open_questions.map((q) => h('li', {}, q)))),
    ];
    for (const [part, def] of Object.entries(PARTS)) {
      if (part === 'stated' && !spec.stated_values.length) continue;
      const editor = this.editors.get(part);
      const button = def.edit && h('button', {
        type: 'button', class: 'secondary small edit-box', 'aria-expanded': String(Boolean(editor)),
        onclick: () => (editor ? this.done(part) : this.edit(part)),
      }, editor ? 'Done' : 'Edit');
      const body = editor || def.view(spec);
      if (editor) {
        // Enter in a one-line field finishes the edit rather than starting the search.
        editor.onkeydown = (event) => {
          if (event.key === 'Enter' && event.target.matches('input')) {
            event.preventDefault();
            this.done(part);
          }
        };
      }
      const titleId = `spec-part-${part}`;
      if (def.folded) {
        const details = h('details', { class: 'spec-box', 'data-part': part, 'data-editing': String(Boolean(editor)) },
          h('summary', {}, h('span', { id: titleId }, def.title), h('span', { class: 'muted small' }, def.count(spec))),
          h('div', { class: 'box-head' }, h('p', { class: 'muted small' }, def.hint), button),
          body);
        details.open = Boolean(editor) || Boolean(this.folded.get(part));
        details.addEventListener('toggle', () => this.folded.set(part, details.open));
        nodes.push(details);
      } else {
        nodes.push(h('section', { class: 'spec-box', 'data-part': part, 'data-editing': String(Boolean(editor)), 'aria-labelledby': titleId },
          h('div', { class: 'box-head' }, h('h3', { id: titleId }, def.title), button),
          body));
      }
    }
    this.root.replaceChildren(...nodes.filter(Boolean));
    this.disabled = this.isDisabled;
  }
}
