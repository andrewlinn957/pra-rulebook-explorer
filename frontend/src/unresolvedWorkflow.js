const ACTIONS = {
  'resolve-internal': {
    id: 'resolve-internal',
    label: 'Resolve internally',
    next_action: 'Resolve to Rulebook node',
    helper: 'Map this placeholder to the matching Rulebook, guidance or glossary node.',
  },
  'check-url': {
    id: 'check-url',
    label: 'Check or repair URL',
    next_action: 'Check URL',
    helper: 'Open the link, follow any redirect, then keep, repair or mark broken.',
  },
  'classify-external': {
    id: 'classify-external',
    label: 'Classify external',
    next_action: 'Classify as external reference',
    helper: 'Keep this as an external source if it is not a Rulebook target.',
  },
  'inspect-context': {
    id: 'inspect-context',
    label: 'Inspect context',
    next_action: 'Inspect source context',
    helper: 'Use the source provision text to identify what the generic link meant.',
  },
  'pattern-fix': {
    id: 'pattern-fix',
    label: 'Pattern fix',
    next_action: 'Batch with similar pattern',
    helper: 'Resolve this through a script or resolver rule rather than row-by-row.',
  },
};

const INTERNAL_HINTS = [
  'pra-rules/',
  'prarulebook.co.uk/rulebook',
  'prarulebook.co.uk/pra-rules',
  'guidance/supervisory-statements',
  'guidance/statements-of-policy',
  'glossary',
];

const GENERIC_TITLES = new Set(['here', 'click here', 'link', 'this link', 'see here', 'more information']);

function text(row, key) {
  return String(row?.[key] ?? '').trim();
}

function haystack(row) {
  return [
    text(row, 'target_type'),
    text(row, 'target_title'),
    text(row, 'target_url'),
    text(row, 'stable_key'),
    text(row, 'source_url'),
    text(row, 'source_title'),
  ].join(' ').toLowerCase();
}

function isRawUrl(value) {
  return /^(https?:\/\/|www\.)/i.test(String(value || '').trim());
}

export function classifyUnresolvedReferenceRow(row) {
  const title = text(row, 'target_title');
  const targetType = text(row, 'target_type').toLowerCase();
  const targetUrl = text(row, 'target_url');
  const h = haystack(row);

  let action_group = 'pattern-fix';
  let why = 'Similar placeholders should be resolved as a batch once the pattern is clear.';

  if (targetType === 'rule_reference' || INTERNAL_HINTS.some(hint => h.includes(hint))) {
    action_group = 'resolve-internal';
    why = 'Looks like an internal PRA Rulebook, guidance, statement of policy or glossary reference.';
  } else if (GENERIC_TITLES.has(title.toLowerCase())) {
    action_group = 'inspect-context';
    why = 'The link text is generic, so the source context is needed before deciding the target.';
  } else if (isRawUrl(title) || isRawUrl(targetUrl)) {
    action_group = 'check-url';
    why = 'The unresolved target is a raw URL, so the next step is to verify or repair the link.';
  } else if (targetType === 'external_reference') {
    action_group = 'classify-external';
    why = 'This appears to be a named external document or website, not a Rulebook node.';
  }

  const action = ACTIONS[action_group];
  return {
    ...row,
    action_group,
    action_label: action.label,
    next_action: action.next_action,
    action_helper: action.helper,
    why,
  };
}

export function buildUnresolvedActionQueues(rows = []) {
  const classified = rows.map(classifyUnresolvedReferenceRow);
  return Object.values(ACTIONS)
    .map(action => ({
      ...action,
      count: classified.filter(row => row.action_group === action.id).length,
      rows: classified.filter(row => row.action_group === action.id),
    }))
    .filter(queue => queue.count > 0);
}
