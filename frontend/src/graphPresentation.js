const GENERIC_EXTERNAL_TITLES = new Set([
  'bank of england',
  'boe',
  'pra',
  'prudential regulation authority',
  'here',
  'click here',
]);

function clean(value) {
  return String(value ?? '').trim();
}

function extensionFromUrl(url = '') {
  try {
    const path = new URL(url, 'https://placeholder.local').pathname;
    const match = path.match(/\.([a-z0-9]+)$/i);
    return match ? match[1].toLowerCase() : '';
  } catch {
    const match = String(url).split(/[?#]/)[0].match(/\.([a-z0-9]+)$/i);
    return match ? match[1].toLowerCase() : '';
  }
}

export function documentBadge(node) {
  const ext = extensionFromUrl(node?.url || node?.target_url || node?.title || '');
  if (ext === 'pdf') return { label: 'PDF', kind: 'pdf' };
  if (['xls', 'xlsx', 'xlt', 'xltx', 'xlsm'].includes(ext)) return { label: ext.toUpperCase(), kind: 'spreadsheet' };
  return null;
}

function isExternalDocument(node) {
  return ['external_reference', 'rule_reference'].includes(node?.node_type) && documentBadge(node);
}

function genericExternalTitle(title) {
  return GENERIC_EXTERNAL_TITLES.has(clean(title).toLowerCase());
}

function documentBaseLabel(node, badge) {
  const url = clean(node?.url).toLowerCase();
  if (url.includes('regulatory-reporting') || url.includes('reporting') || url.includes('template')) return 'Reporting template';
  return badge?.kind === 'spreadsheet' ? 'Spreadsheet document' : 'PDF document';
}

export function displayNodeTitle(node) {
  if (!node) return 'Unloaded node';
  const badge = documentBadge(node);
  const title = clean(node.title) || 'Untitled node';

  if (isExternalDocument(node)) {
    if (genericExternalTitle(title)) return `${documentBaseLabel(node, badge)} · ${badge.label}`;
    if (!title.includes(`· ${badge.label}`)) return `${title} · ${badge.label}`;
  }

  if (/^article\b/i.test(title)) return title;
  const part = node.metadata?.part_title || node.metadata?.document_title;
  if (part && /^\d/.test(title) && !title.startsWith(part)) return `${part} ${title}`;
  return title;
}

export function relativeNodeRole(node, selectedId, graph) {
  if (!node?.id || !selectedId || node.id === selectedId) return node?.id === selectedId ? 'selected' : 'related';
  const edges = graph?.edges || [];
  if (edges.some(e => e.edge_type === 'contains' && e.from_node_id === node.id && e.to_node_id === selectedId)) return 'parent';
  if (edges.some(e => e.edge_type === 'contains' && e.from_node_id === selectedId && e.to_node_id === node.id)) return 'child';
  return 'related';
}

export function edgeDirectionLabel(edge, selectedId) {
  if (!edge || !selectedId) return 'related';
  if (edge.from_node_id === selectedId) return 'outgoing';
  if (edge.to_node_id === selectedId) return 'incoming';
  return 'related';
}

export function edgeDirectionGlyph(edge, selectedId) {
  const direction = edgeDirectionLabel(edge, selectedId);
  if (direction === 'outgoing') return '→';
  if (direction === 'incoming') return '←';
  return '↔';
}
