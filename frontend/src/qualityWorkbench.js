const OPEN_FEEDBACK_STATUSES = new Set(['pending', 'failed', 'running']);

export function buildQualityQueues({ validation = {}, feedback = {} } = {}) {
  const feedbackItems = feedback.items || [];
  const unresolved = findUnresolvedReferences(validation);
  return [
    {
      id: 'feedback',
      label: 'Feedback',
      description: 'Process node feedback repairs',
      items: feedbackItems,
      count: feedbackItems.filter(item => OPEN_FEEDBACK_STATUSES.has(item.status)).length,
    },
    {
      id: 'unverified-links',
      label: 'Unverified links',
      description: 'Resolve links that cannot be verified',
      rows: unresolved.rows,
      patterns: unresolved.patterns,
      count: unresolved.rows.length,
    },
  ];
}

export function summariseQueue(queue) {
  if (queue.id === 'feedback') {
    const items = queue.items || [];
    const open = items.filter(item => OPEN_FEEDBACK_STATUSES.has(item.status)).length;
    const done = items.filter(item => item.status === 'completed').length;
    return { primary: `${open} open`, secondary: `${done} done` };
  }
  const count = queue.count ?? (queue.rows || []).length;
  return { primary: `${count} links`, secondary: queue.patterns?.length ? `${queue.patterns.length} patterns` : 'review queue' };
}

export function filterQueueRows(rows, query) {
  const needle = String(query || '').trim().toLowerCase();
  if (!needle) return rows || [];
  return (rows || []).filter(row => Object.values(row || {}).some(value => String(value ?? '').toLowerCase().includes(needle)));
}

export function findUnresolvedReferences(validation = {}) {
  const directRows = validation.unresolved_reference_samples || [];
  const directPatterns = validation.unresolved_reference_patterns || [];
  const check = (validation.checks || []).find(item => normalise(item.check) === 'unresolved references');
  return {
    rows: directRows.length ? directRows : (check?.samples || check?.rows || []),
    patterns: directPatterns.length ? directPatterns : (check?.patterns || []),
  };
}

function normalise(value) {
  return String(value || '').replaceAll('_', ' ').replaceAll('-', ' ').trim().toLowerCase();
}
