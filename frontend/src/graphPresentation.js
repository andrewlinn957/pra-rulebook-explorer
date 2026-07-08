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

function normaliseTemplateCode(value) {
  return clean(value).replace(/\s+/g, '').toUpperCase();
}

function sentenceCaseTemplateName(value) {
  const text = clean(value).replace(/\s+/g, ' ');
  if (!text) return '';
  if (text !== text.toUpperCase()) return text;
  return text.toLowerCase()
    .replace(/^./, c => c.toUpperCase())
    .replace(/\b(cet1|at1|t2|crr|irb|pd|lgd|crm|rwa|rwas|npe|ccr|sa-crr|sa|sme)\b/gi, m => m.toUpperCase());
}

function reportingTemplateDisplayTitle(node, fallbackTitle) {
  if (node?.node_type !== 'Template') return '';
  const metadata = node.metadata || {};
  const code = normaliseTemplateCode(metadata.template_code || fallbackTitle);
  const raw = clean(metadata.template_title || metadata.title || node.text || '');
  if (!code || !raw) return '';
  const spacedCode = code.replace(/^([A-Z]+)(\d)/, '$1 $2');
  const escapedCode = spacedCode.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s*');
  const match = raw.match(new RegExp(`${escapedCode}(?:\.\d+)?\\s*[-–—]\\s*([^|()]+)`, 'i'))
    || raw.match(/^[\d.]+\s*\|?\s*([^|()]+)/);
  const name = sentenceCaseTemplateName(match?.[1] || '');
  if (!name || normaliseTemplateCode(name) === code) return '';
  return `${code} · ${name}`;
}

export function displayNodeTitle(node) {
  if (!node) return 'Unloaded node';
  const badge = documentBadge(node);
  const title = clean(node.title) || 'Untitled node';
  const templateTitle = reportingTemplateDisplayTitle(node, title);
  if (templateTitle) return templateTitle;

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
