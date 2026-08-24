export const ISSUE_STATUSES = Object.freeze(['open', 'in_progress', 'resolved', 'wont_fix']);

export function filterIssues(items, status = 'all') {
  if (status === 'all') return items;
  return items.filter(item => item.status === status);
}

export function issueCounts(items) {
  const counts = { all: items.length };
  for (const status of ISSUE_STATUSES) counts[status] = 0;
  for (const item of items) {
    if (Object.hasOwn(counts, item.status)) counts[item.status] += 1;
  }
  return counts;
}

export function issueStatusLabel(status) {
  return {
    open: 'Open',
    in_progress: 'In progress',
    resolved: 'Resolved',
    wont_fix: "Won't fix",
  }[status] || status || 'Unknown';
}

export function issueDateLabel(value) {
  if (!value) return 'Unknown date';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  }).format(date);
}
