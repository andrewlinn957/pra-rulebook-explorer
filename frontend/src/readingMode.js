const READING_EDGE_TYPES = new Set([
  'references',
  'uses_defined_term',
  'amends',
]);

export const READING_EDGE_TYPE_LIST = [...READING_EDGE_TYPES];

export function readingRelationship(edgeType) {
  if (['uses_defined_term', 'defines'].includes(edgeType)) {
    return { code: 'DEF', label: 'Definition' };
  }
  if (edgeType === 'references') {
    return { code: 'REF', label: 'Cross-reference' };
  }
  return { code: 'RELATED', label: 'Related provision' };
}

function compactWhitespace(value = '') {
  return String(value).replace(/\s+/g, ' ').trim();
}

function sentenceChunks(value, targetLength = 720) {
  if (value.length <= 1100) return [value];
  const sentences = value.match(/[^.!?;]+(?:[.!?;]+(?=\s|$)|$)/g) || [value];
  const chunks = [];
  let current = '';
  for (const sentence of sentences) {
    const next = compactWhitespace(sentence);
    if (!next) continue;
    if (current && current.length + next.length + 1 > targetLength) {
      chunks.push(current);
      current = next;
    } else {
      current = current ? `${current} ${next}` : next;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

export function splitLegalParagraphs(value = '') {
  const source = String(value).replace(/\r\n?/g, '\n').trim();
  if (!source) return [];
  const structural = source
    .replace(/[ \t]+/g, ' ')
    .replace(/\s+(?=(?:\(\d+\)|\([a-z]\)|\([ivx]+\))\s)/gi, '\n\n')
    .replace(/\s+(?=\[(?:Note|Editor|Source)\b)/gi, '\n\n');
  return structural
    .split(/\n\s*\n+/)
    .map(compactWhitespace)
    .filter(Boolean)
    .flatMap(paragraph => sentenceChunks(paragraph));
}

function citationNeedles(reference) {
  const edge = reference.edge || {};
  const metadata = edge.metadata || {};
  const evidence = compactWhitespace(edge.evidence_text || '');
  const values = [
    metadata.reference,
    metadata.term_title,
    metadata.target_title,
    reference.citation,
    evidence.length <= 90 ? evidence : '',
    reference.node?.title,
  ];
  const needles = values.map(compactWhitespace).filter(value => value.length > 1);
  if (reference.relationship?.code === 'DEF') {
    for (const value of [...needles]) {
      if (/^[a-z][a-z -]+$/i.test(value) && !value.endsWith('s')) {
        needles.push(`${value}s`);
      }
    }
  }
  return [...new Set(needles)]
    .sort((a, b) => b.length - a.length);
}

function boundaryMatch(text, needle, start) {
  const before = start > 0 ? text[start - 1] : '';
  const after = text[start + needle.length] || '';
  const first = needle[0] || '';
  const last = needle[needle.length - 1] || '';
  if (/[a-z0-9]/i.test(first) && /[a-z0-9]/i.test(before)) return false;
  if (/[a-z0-9]/i.test(last) && /[a-z0-9]/i.test(after)) return false;
  return true;
}

function citationMatch(paragraph, reference) {
  const haystack = paragraph.toLocaleLowerCase();
  for (const needle of citationNeedles(reference)) {
    const lowerNeedle = needle.toLocaleLowerCase();
    let start = haystack.indexOf(lowerNeedle);
    while (start >= 0) {
      if (boundaryMatch(paragraph, needle, start)) {
        return { start, end: start + needle.length };
      }
      start = haystack.indexOf(lowerNeedle, start + 1);
    }
  }
  return null;
}

export function readerReferences(rootNode, graph = {}) {
  const byId = new Map((graph.nodes || []).map(node => [node.id, node]));
  const references = [];
  const seen = new Set();
  for (const edge of graph.edges || []) {
    if (!READING_EDGE_TYPES.has(edge.edge_type)) continue;
    // The reading spine follows citations made by the current provision.
    // Guidance references may be displayed at their document ancestor in the
    // graph. Preserve the original source direction so a paragraph reader can
    // still recognise its own outgoing citation without admitting incoming
    // neighbours.
    const rolledUpSources = edge.metadata?.rolled_up_from_from_node_ids || [];
    if (
      edge.from_node_id !== rootNode?.id
      && !rolledUpSources.includes(rootNode?.id)
    ) continue;
    const targetId = edge.to_node_id;
    const target = byId.get(targetId);
    if (!target || target.id === rootNode.id) continue;
    const relationship = readingRelationship(edge.edge_type);
    const key = `${target.id}|${relationship.code}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const metadata = edge.metadata || {};
    references.push({
      id: key,
      node: target,
      edge,
      relationship,
      citation: compactWhitespace(
        metadata.reference
        || metadata.term_title
        || metadata.target_title
        || (edge.evidence_text || '').length <= 90 && edge.evidence_text
        || target.title
      ),
      sourceHeading: compactWhitespace(
        target.metadata?.part_title
        || target.metadata?.document_title
        || target.metadata?.source_title
        || 'PRA Rulebook'
      ),
    });
  }
  return references.sort((a, b) => {
    const priority = { REF: 0, DEF: 1, RELATED: 2 };
    return priority[a.relationship.code] - priority[b.relationship.code]
      || a.citation.localeCompare(b.citation, undefined, { numeric: true });
  });
}

export function assignReferencesToParagraphs(paragraphs, references) {
  return references.map(reference => {
    for (let paragraphIndex = 0; paragraphIndex < paragraphs.length; paragraphIndex += 1) {
      const match = citationMatch(paragraphs[paragraphIndex], reference);
      if (match) return { ...reference, paragraphIndex, match };
    }
    return { ...reference, paragraphIndex: -1, match: null };
  });
}

export function paragraphCitationSegments(paragraph, references) {
  const matches = references
    .map(reference => ({ reference, match: citationMatch(paragraph, reference) }))
    .filter(item => item.match)
    .sort((a, b) => a.match.start - b.match.start
      || b.match.end - b.match.start - (a.match.end - a.match.start));
  const accepted = [];
  let occupiedUntil = -1;
  for (const item of matches) {
    if (item.match.start < occupiedUntil) continue;
    accepted.push(item);
    occupiedUntil = item.match.end;
  }
  const segments = [];
  let cursor = 0;
  for (const item of accepted) {
    if (item.match.start > cursor) {
      segments.push({ type: 'text', text: paragraph.slice(cursor, item.match.start) });
    }
    segments.push({
      type: 'citation',
      text: paragraph.slice(item.match.start, item.match.end),
      reference: item.reference,
    });
    cursor = item.match.end;
  }
  if (cursor < paragraph.length) {
    segments.push({ type: 'text', text: paragraph.slice(cursor) });
  }
  return segments.length ? segments : [{ type: 'text', text: paragraph }];
}

export function referenceShelfDensity(availableHeight, referenceCount) {
  if (!referenceCount) return 'full';
  const spacePerReference = Math.max(0, Number(availableHeight) || 0) / referenceCount;
  if (spacePerReference >= 150) return 'full';
  if (spacePerReference >= 104) return 'compact';
  if (spacePerReference >= 70) return 'dense';
  return 'summary';
}
