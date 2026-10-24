// The search specification as structured tables, each item edited where it is: click it (or press
// Enter on it) and it becomes a field; Enter or clicking away keeps the change, Escape drops it, and
// each change is saved at once. Shared by the browser edition (site/app.js) and the local server's
// page (web/app.js); the local server serves this folder at /lib. Styles are in spec-panel.css.

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

// -- fields ----------------------------------------------------------------------------

// One input for a field: {label, kind: 'text' | 'select' | 'list' | 'statistics', options, list}.
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

const text = (label, extra = {}) => ({ label, kind: 'text', ...extra });
const choice = (label, options, extra = {}) => ({ label, kind: 'select', options, ...extra });

// A value as shown when it is not being edited.
function shown(field, value) {
  if (field.show) return field.show(value);
  if (field.kind === 'select') return field.options[value] || value;
  if (field.kind === 'statistics') return value.map((k) => STATISTICS[k] || k).join(' · ');
  return Array.isArray(value) ? value.join(', ') : value;
}

const isEmpty = (value) => value == null || value === '' || (Array.isArray(value) && !value.length);

// -- the parts -----------------------------------------------------------------------------
// Each part draws itself with the panel's `edit(id, field, value, set)`, which makes one value
// editable where it is, and `rows(...)`, which makes a table of records with a row per record.

const ROW_PARTS = {
  outcomes: { blank: () => ({ role: 'secondary', name: '', measure: '', type: 'continuous', timepoint: '' }), empty: (o) => !o.name && !o.measure },
  numbers: {
    blank: () => ({ outcome: '', statistics: [], per: 'each_arm', timepoint: '', purpose: '' }),
    empty: (s) => !s.outcome && !s.statistics.length && !s.purpose,
  },
};

const PARTS = {
  comparison: {
    title: 'The comparison',
    view: (spec, panel) => {
      const { population: p, intervention: i, comparator: c } = spec;
      const facets = (prefix, record, pairs) => h('span', { class: 'facets' }, pairs.map(([key, label]) => h('span', {},
        h('span', { class: 'k' }, label), ' ', panel.edit(`${prefix}.${key}`, text(label), record[key], (v) => { record[key] = v; }, '—'))));
      return h('table', { class: 'spec-table kv' }, h('tbody', {},
        h('tr', {}, h('th', { scope: 'row' }, 'Population'),
          h('td', {}, h('strong', {}, panel.edit('population.description', text('Population'), p.description, (v) => { p.description = v; })),
            facets('population', p, [['condition', 'Condition'], ['age', 'Age'], ['setting', 'Setting']]))),
        h('tr', {}, h('th', { scope: 'row' }, 'Intervention'),
          h('td', {}, h('strong', {}, panel.edit('intervention.name', text('Intervention'), i.name, (v) => { i.name = v; })),
            facets('intervention', i, [['delivery', 'Delivery'], ['dose', 'Dose'], ['duration', 'Duration']]))),
        h('tr', {}, h('th', { scope: 'row' }, 'Compared with'),
          h('td', {}, h('strong', {}, panel.edit('comparator.name', text('Compared with'), c.name, (v) => { c.name = v; }, 'None')), ' ',
            panel.edit('comparator.kind', choice('Kind of comparator', COMPARATOR_KINDS, { show: (v) => badge(COMPARATOR_KINDS[v] || v) }),
              c.kind, (v) => { c.kind = v; }))),
        h('tr', {}, h('th', { scope: 'row' }, 'Design'),
          h('td', {}, panel.edit('design', choice('Design', DESIGNS), spec.design, (v) => { spec.design = v; }))),
      ));
    },
  },
  outcomes: {
    title: 'Outcomes',
    view: (spec, panel) => panel.rows('outcomes', spec.outcomes, [
      { key: 'role', ...choice('Role', ROLES, { show: (v) => (v === 'primary' ? badge('Primary', 'accent') : ROLES[v] || v) }) },
      { key: 'name', ...text('Outcome', { show: (v) => h('strong', {}, v), first: true }) },
      { key: 'measure', ...text('Measure') },
      { key: 'type', ...choice('Type', OUTCOME_TYPES) },
      { key: 'timepoint', ...text('When') },
    ], { noun: 'outcome', empty: 'No outcomes yet.' }),
  },
  numbers: {
    title: 'Effect sizes to find',
    view: (spec, panel) => [
      // The outcome field suggests the outcomes listed above.
      h('datalist', { id: 'spec-outcome-names' }, spec.outcomes.map((o) => h('option', { value: o.name }))),
      panel.rows('numbers', spec.statistics_needed, [
        { key: 'outcome', ...text('Outcome', { list: 'spec-outcome-names', show: (v) => h('strong', {}, v), placeholder: 'Any', first: true }) },
        { key: 'statistics', label: 'Find', kind: 'statistics' },
        { key: 'per', ...choice('For', PER) },
        { key: 'timepoint', ...text('When', { placeholder: 'Any' }) },
        { key: 'purpose', ...text('Purpose') },
      ], { noun: 'effect size', empty: 'Nothing listed yet.' }),
    ],
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
    hint: 'How the workers decide which titles and abstracts to keep.',
    count: (spec) => `${spec.inclusion_criteria.length} to include · ${spec.exclusion_criteria.length} to exclude`,
    view: (spec, panel) => {
      // One table of both lists; a rule moves between them when its first column changes.
      const rules = [
        ...spec.inclusion_criteria.map((c) => ({ keep: 'include', ...c })),
        ...spec.exclusion_criteria.map((c) => ({ keep: 'exclude', ...c })),
      ];
      const writeBack = (list) => {
        spec.inclusion_criteria = list.filter((r) => r.keep === 'include').map(({ about, rule }) => ({ about, rule }));
        spec.exclusion_criteria = list.filter((r) => r.keep === 'exclude').map(({ about, rule }) => ({ about, rule }));
      };
      return panel.rows('screening', rules, [
        { key: 'keep', ...choice('Keep or drop', KEEP, { show: (v) => badge(KEEP[v] || v, v === 'include' ? 'ok' : 'err') }) },
        { key: 'about', ...choice('About', ELEMENTS) },
        { key: 'rule', ...text('Rule', { first: true }) },
      ], { noun: 'rule', empty: 'No rules yet.', writeBack, blank: () => ({ keep: 'include', about: 'population', rule: '' }) });
    },
  },
  terms: {
    title: 'Search terms',
    folded: true,
    hint: 'Synonyms, MeSH headings and instrument names, by concept, separated by commas. The manager builds the search from these.',
    count: (spec) => `${spec.search_terms.reduce((n, g) => n + g.terms.length, 0)} in ${spec.search_terms.length} groups`,
    view: (spec, panel) => panel.rows('terms', spec.search_terms, [
      { key: 'concept', ...choice('Concept', CONCEPTS, { show: (v) => h('strong', {}, CONCEPTS[v] || v) }) },
      { key: 'terms', ...text('Terms', { kind: 'list', show: (v) => h('ul', { class: 'chips' }, v.map((t) => h('li', {}, t))), first: true }) },
    ], { noun: 'group', empty: 'None yet.', blank: () => ({ concept: 'outcome', terms: [] }) }),
  },
};

// A table that is read, not edited (the values the paragraph states).
function table(head, rows, empty) {
  if (!rows.length) return h('p', { class: 'muted small empty' }, empty);
  return h('div', { class: 'table-wrap' }, h('table', { class: 'spec-table' },
    h('thead', {}, h('tr', {}, head.map((label) => h('th', { scope: 'col' }, label)))),
    h('tbody', {}, rows.map((cells) => h('tr', {}, cells.map((cell, i) => h('td', { 'data-label': head[i] }, h('span', {}, cell))))))));
}

export class SpecPanel {
  // onSave(spec) keeps a change; it is called after every change.
  constructor(root, { onSave }) {
    this.root = root;
    this.onSave = onSave;
    this.draft = null;
    this.active = null; // the field being edited: {id, finish(nextId), cancel()}
    this.openers = new Map(); // id -> opens that value's field (rebuilt on every draw)
    this.folded = new Map(); // part -> open or not, for the parts that fold
    this.isDisabled = false;
    this.saving = 0; // saves under way
  }

  // Show a specification. While a field is open or a save is under way, the panel's own draft is the
  // newest version, and a copy read back from storage meanwhile may be older: it is shown only when
  // `replace` says it belongs to another search.
  show(spec, { replace = false } = {}) {
    const next = upgradeSpec(spec);
    if (this.draft && JSON.stringify(next) === JSON.stringify(this.draft)) return; // already shown
    if (this.draft && !replace && (this.active || this.saving)) return;
    this.active = null;
    this.draft = structuredClone(next);
    this.render();
  }

  set disabled(value) {
    this.isDisabled = value;
    this.root.classList.toggle('is-disabled', value);
    for (const el of this.root.querySelectorAll('button, input, select')) el.disabled = value;
    for (const el of this.root.querySelectorAll('.editable')) {
      el.tabIndex = value ? -1 : 0;
      el.setAttribute('aria-disabled', String(value));
    }
  }

  // The specification with the field being edited kept, or null when it has a value to fix.
  collect() {
    if (this.active && !this.active.finish()) return null;
    this.tidy();
    return structuredClone(this.draft);
  }

  // Rows left with nothing in them (added and never filled) are dropped.
  tidy() {
    const spec = this.draft;
    spec.outcomes = spec.outcomes.filter((o) => !ROW_PARTS.outcomes.empty(o));
    spec.statistics_needed = spec.statistics_needed.filter((s) => !ROW_PARTS.numbers.empty(s));
    spec.inclusion_criteria = spec.inclusion_criteria.filter((c) => c.rule);
    spec.exclusion_criteria = spec.exclusion_criteria.filter((c) => c.rule);
    spec.search_terms = spec.search_terms.filter((g) => g.terms.length);
  }

  changed() {
    this.tidy();
    this.render();
    this.save();
  }

  save() {
    this.saving += 1;
    Promise.resolve(this.onSave(structuredClone(this.draft))).finally(() => { this.saving -= 1; });
  }

  // Focus a value (by its id) or one of the panel's buttons (by its key) after a redraw.
  focus(id) {
    this.root.querySelector(`[data-edit="${CSS.escape(id)}"], [data-key="${CSS.escape(id)}"]`)?.focus();
  }

  // One value, shown as it reads, that becomes a field when clicked (or Enter or Space on it).
  edit(id, field, value, set, placeholder = 'Not stated') {
    const content = isEmpty(value) ? missing(field.placeholder || placeholder) : shown(field, value);
    const span = h('span', {
      class: 'editable', role: 'button', tabindex: this.isDisabled ? '-1' : '0', 'data-edit': id,
      title: `Click to change ${field.label.toLowerCase()}`,
    }, content);
    span.setAttribute('aria-label', `${field.label}: ${isEmpty(value) ? 'not stated' : span.textContent}. Change`);
    const open = () => this.start(span, id, field, value, set);
    this.openers.set(id, open);
    // Clicking another value while one is being edited keeps the first and opens the second.
    span.addEventListener('mousedown', (event) => {
      if (!this.active || this.isDisabled) return;
      event.preventDefault();
      this.active.finish(id);
    });
    span.addEventListener('click', () => { if (!this.active && !this.isDisabled) open(); });
    span.addEventListener('keydown', (event) => {
      if ((event.key === 'Enter' || event.key === ' ') && !this.isDisabled) {
        event.preventDefault();
        open();
      }
    });
    return span;
  }

  start(span, id, field, value, set) {
    if (this.isDisabled || !span.isConnected) return;
    const input = fieldInput(field, value, { 'aria-label': field.label, class: 'inline-input', id: `spec-${id.replaceAll('.', '-')}`, list: field.list });
    const box = span.closest('.spec-box');
    span.replaceWith(input);
    box?.setAttribute('data-editing', 'true');
    let done = false;
    // After the change is kept: open the value clicked next, or put the focus where Tab sent it (left
    // alone when it went outside the panel), or back on this value.
    const close = (next, how) => {
      done = true;
      this.active = null;
      this.tidy();
      this.render();
      if (how === 'open' && next && this.openers.has(next)) this.openers.get(next)();
      else if (how === 'focus') { if (next) this.focus(next); } else if (how !== 'away') this.focus(id);
    };
    const finish = (next = null, how = next ? 'open' : 'self') => {
      if (done) return true;
      if (field.kind === 'statistics' && !checkStatistics(input)) {
        input.focus();
        input.reportValidity();
        return false;
      }
      const updated = readInput(field, input);
      const changed = JSON.stringify(updated) !== JSON.stringify(value);
      if (changed) set(updated);
      close(next, how);
      if (changed) this.save();
      return true;
    };
    const cancel = () => {
      if (!done) close(null, 'self');
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault(); // keeps the change rather than starting the search
        finish();
      } else if (event.key === 'Escape') {
        event.preventDefault();
        cancel();
      }
    });
    input.addEventListener('blur', (event) => {
      if (done) return;
      const to = event.relatedTarget;
      if (to && !this.root.contains(to)) finish(null, 'away');
      else finish(to?.dataset?.edit || to?.dataset?.key || null, 'focus');
    });
    if (field.kind === 'select') input.addEventListener('change', () => finish());
    this.active = { id, finish, cancel };
    input.focus();
    if (input.select) input.select();
  }

  // Records as a table, a row per record and each cell editable, with × to remove a row and a
  // button to add one. `writeBack(rows)` stores the rows when they are a copy (the screening rules).
  rows(part, rows, columns, { noun, empty, writeBack = () => {}, blank = ROW_PARTS[part]?.blank }) {
    const first = (columns.find((c) => c.first) || columns[0]).key;
    // A button that keeps the field being edited before it acts.
    const action = (label, attrs, act) => {
      const button = h('button', { type: 'button', 'data-key': attrs.key, ...attrs, key: null }, label);
      button.addEventListener('mousedown', (event) => { if (this.active) event.preventDefault(); });
      button.addEventListener('click', () => {
        if (this.active && !this.active.finish()) return;
        act();
      });
      return button;
    };
    const add = action(`+ Add ${/^[aeiou]/.test(noun) ? 'an' : 'a'} ${noun}`, { class: 'secondary small add-row', key: `${part}.add` }, () => {
      rows.push(blank());
      writeBack(rows);
      this.render();
      this.openers.get(`${part}.${rows.length - 1}.${first}`)?.();
    });
    if (!rows.length) return [h('p', { class: 'muted small empty' }, empty), add];
    return [h('div', { class: 'table-wrap' }, h('table', { class: 'spec-table' },
      h('thead', {}, h('tr', {}, columns.map((c) => h('th', { scope: 'col' }, c.label)), h('th', { class: 'row-remove' }, h('span', { class: 'visually-hidden' }, 'Remove')))),
      h('tbody', {}, rows.map((row, i) => h('tr', {},
        columns.map((c) => h('td', { 'data-label': c.label },
          this.edit(`${part}.${i}.${c.key}`, c, row[c.key], (v) => { row[c.key] = v; writeBack(rows); }, '—'))),
        h('td', { class: 'row-remove' }, action('×', { class: 'remove', key: `${part}.${i}.remove`, 'aria-label': `Remove ${noun} ${i + 1}`, title: `Remove this ${noun}` }, () => {
          rows.splice(i, 1);
          writeBack(rows);
          this.changed();
        }))))))), add];
  }

  render() {
    this.openers.clear();
    const spec = this.draft;
    const primary = spec.outcomes.find((o) => o.role === 'primary') || spec.outcomes[0];
    const nodes = [
      h('p', { class: 'muted small edit-hint' }, 'Enter or clicking away keeps a change; Escape drops it.'),
      h('div', { class: 'spec-question' },
        h('p', { class: 'question', id: 'spec-question' },
          this.edit('question', text('The question'), spec.question, (v) => { spec.question = v; }, 'No question yet.')),
        h('p', { class: 'tags' }, badge(DESIGNS[spec.design] || spec.design, 'accent'),
          primary && badge(`${OUTCOME_TYPES[primary.type] || primary.type} primary outcome`, 'accent'))),
      spec.open_questions.length > 0 && h('div', { class: 'callout warn', 'data-part': 'questions' },
        h('h3', {}, 'Worth deciding before you search'), h('ul', {}, spec.open_questions.map((q) => h('li', {}, q)))),
    ];
    for (const [part, def] of Object.entries(PARTS)) {
      if (part === 'stated' && !spec.stated_values.length) continue;
      const body = [def.view(spec, this)].flat(Infinity);
      const titleId = `spec-part-${part}`;
      if (def.folded) {
        const details = h('details', { class: 'spec-box', 'data-part': part },
          h('summary', {}, h('span', { id: titleId }, def.title), h('span', { class: 'muted small' }, def.count(spec))),
          h('p', { class: 'muted small hint' }, def.hint),
          body);
        details.open = Boolean(this.folded.get(part));
        details.addEventListener('toggle', () => this.folded.set(part, details.open));
        nodes.push(details);
      } else {
        nodes.push(h('section', { class: 'spec-box', 'data-part': part, 'aria-labelledby': titleId },
          h('div', { class: 'box-head' }, h('h3', { id: titleId }, def.title)),
          body));
      }
    }
    this.root.replaceChildren(...nodes.filter(Boolean));
    this.disabled = this.isDisabled;
  }
}
