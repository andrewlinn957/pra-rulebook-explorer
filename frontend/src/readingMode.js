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

function isListBoundary(source, markerStart, previousMarkerEnd = -1) {
  if (markerStart === 0) return true;
  const before = source.slice(0, markerStart);
  if (/\n[ \t]*$/.test(before)) return true;
  if (
    previousMarkerEnd >= 0
    && !source.slice(previousMarkerEnd, markerStart).trim()
  ) return true;
  const trimmed = before.trimEnd();
  if (/[;:.]$/.test(trimmed)) return true;
  if (/,\s*$/.test(trimmed)) return true;
  return /[;:.,]\s*(?:and|or)$/i.test(trimmed);
}

function listMarkerDepth(marker, previous, lastAlphabeticMarker = '') {
  const value = marker.slice(1, -1);
  if (/^\d+$/.test(value)) return 0;
  if (/^[A-Z]$/.test(value)) return 3;
  if (/^[ivxlcdm]{2,}$/i.test(value)) return 2;
  if (/^[a-z]$/i.test(value)) {
    const lower = value.toLowerCase();
    if (/^[ivxlcdm]$/.test(lower)) {
      const alphabeticalPredecessor = lastAlphabeticMarker.toLowerCase();
      const alphabeticalSequence = alphabeticalPredecessor.length === 1
        && lower.charCodeAt(0) === alphabeticalPredecessor.charCodeAt(0) + 1;
      if (alphabeticalSequence) return 1;
      if (previous?.rawDepth === 1 || previous?.rawDepth === 2) return 2;
    }
    return 1;
  }
  return 0;
}

function proseBlocks(value) {
  return String(value)
    .split(/\n\s*\n+/)
    .map(compactWhitespace)
    .filter(Boolean)
    .flatMap(text => sentenceChunks(text))
    .map(text => ({
      kind: /^\[(?:Note|Editor|Source)\b/i.test(text) ? 'note' : 'prose',
      marker: '',
      depth: 0,
      text,
    }));
}

export function legalTextBlocks(value = '') {
  const source = String(value).replace(/\r\n?/g, '\n').trim();
  if (!source) return [];
  const candidates = [];
  const markerPattern = /\((?:\d{1,3}|[a-z]{1,4}|[A-Z])\)/g;
  let match;
  let previousMarkerEnd = -1;
  while ((match = markerPattern.exec(source))) {
    if (!isListBoundary(source, match.index, previousMarkerEnd)) continue;
    candidates.push({
      marker: match[0],
      start: match.index,
      end: match.index + match[0].length,
    });
    previousMarkerEnd = match.index + match[0].length;
  }
  if (!candidates.length) return proseBlocks(source);

  const blocks = [];
  if (candidates[0].start > 0) {
    blocks.push(...proseBlocks(source.slice(0, candidates[0].start)));
  }
  let previousListBlock = null;
  let lastAlphabeticMarker = '';
  for (let index = 0; index < candidates.length; index += 1) {
    const candidate = candidates[index];
    const nextStart = candidates[index + 1]?.start ?? source.length;
    const text = compactWhitespace(source.slice(candidate.end, nextStart));
    const rawDepth = listMarkerDepth(
      candidate.marker,
      previousListBlock,
      lastAlphabeticMarker,
    );
    const block = {
      kind: 'list-item',
      marker: candidate.marker,
      rawDepth,
      depth: rawDepth,
      text,
    };
    blocks.push(block);
    previousListBlock = block;
    if (rawDepth === 0) lastAlphabeticMarker = '';
    if (rawDepth === 1) lastAlphabeticMarker = candidate.marker.slice(1, -1);
  }
  const listBlocks = blocks.filter(block => block.kind === 'list-item');
  const minimumDepth = Math.min(...listBlocks.map(block => block.rawDepth));
  return blocks.map(block => {
    if (block.kind !== 'list-item') return block;
    const { rawDepth, ...rest } = block;
    return { ...rest, depth: Math.max(0, rawDepth - minimumDepth) };
  });
}

export function splitLegalParagraphs(value = '') {
  return legalTextBlocks(value).map(block => (
    block.marker ? `${block.marker} ${block.text}`.trim() : block.text
  ));
}

export function readingSpine(contents = {}) {
  const root = contents.root;
  if (!root) return [];
  const entries = [];
  const seen = new Set();
  function visit(node, depth, isRoot = false) {
    if (!node || seen.has(node.id)) return;
    seen.add(node.id);
    const children = node.children || [];
    const isStructuralContainer = children.length > 0
      && new Set([
        'rulebook',
        'part',
        'chapter',
        'guidance_document',
        'guidance_section',
      ]).has(node.node_type);
    if (isRoot || compactWhitespace(node.text || '') || children.length) {
      entries.push({
        node,
        depth,
        isRoot,
        bodyText: isStructuralContainer ? '' : node.text || '',
      });
    }
    for (const child of children) visit(child, depth + 1);
  }
  visit({ ...root, children: contents.children || [] }, 0, true);
  return entries;
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

function citationMatch(paragraph, reference, startAt = 0) {
  const haystack = paragraph.toLocaleLowerCase();
  for (const needle of citationNeedles(reference)) {
    const lowerNeedle = needle.toLocaleLowerCase();
    let start = haystack.indexOf(lowerNeedle, startAt);
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
  const occurrenceGroups = new Map();
  const fallbackGroups = new Map();
  const seenOccurrences = new Set();
  const occurrenceCoveredTargets = new Set(
    (graph.edges || []).flatMap(edge => (
      edge.metadata?.reference_occurrences || []
    ))
      .filter(occurrence => (
        occurrence.status === 'materialized'
        && occurrence.source_node_id === rootNode?.id
      ))
      .map(occurrence => `${occurrence.target_node_id}|REF`),
  );
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
    const relationship = readingRelationship(edge.edge_type);
    const metadata = edge.metadata || {};
    const occurrences = (metadata.reference_occurrences || [])
      .filter(occurrence => occurrence.status === 'materialized')
      .filter(occurrence => (
        occurrence.source_node_id === rootNode?.id
        || edge.from_node_id === rootNode?.id
        || rolledUpSources.includes(occurrence.source_node_id)
      ));
    if (occurrences.length) {
      for (const occurrence of occurrences) {
        if (!occurrence.occurrence_id || seenOccurrences.has(occurrence.occurrence_id)) {
          continue;
        }
        seenOccurrences.add(occurrence.occurrence_id);
        const target = byId.get(occurrence.target_node_id)
          || byId.get(edge.to_node_id);
        if (!target || target.id === rootNode?.id) continue;
        const groupKey = `${occurrence.group_id || occurrence.occurrence_id}|${relationship.code}`;
        const group = occurrenceGroups.get(groupKey) || {
          id: `occurrence-group:${rootNode.id}:${groupKey}`,
          occurrenceGroupId: occurrence.group_id || occurrence.occurrence_id,
          node: target,
          edge,
          relationship,
          citation: compactWhitespace(
            occurrence.group_text
            || occurrence.citation_text
            || target.title
          ),
          sourceHeading: compactWhitespace(
            target.metadata?.instrument_title
            || target.metadata?.part_title
            || target.metadata?.official_document_title
            || target.metadata?.document_title
            || target.metadata?.source_title
            || 'PRA Rulebook'
          ),
          sourceSpan: occurrence.metadata?.group_span || occurrence.source_span || {
            start: occurrence.span_start,
            end: occurrence.span_end,
          },
          members: [],
        };
        group.members.push({
          id: occurrence.occurrence_id,
          occurrence,
          node: target,
          edge,
          relationship,
          citation: compactWhitespace(occurrence.citation_text || target.title),
        });
        occurrenceGroups.set(groupKey, group);
      }
      continue;
    }

    const target = byId.get(edge.to_node_id);
    if (!target || target.id === rootNode?.id) continue;
    const targetKey = `${target.id}|${relationship.code}`;
    if (occurrenceCoveredTargets.has(targetKey)) continue;
    const citation = compactWhitespace(
      metadata.reference
      || metadata.term_title
      || metadata.target_title
      || target.title
      || (edge.evidence_text || '').length <= 90 && edge.evidence_text
    );
    const groupKey = `${citation.toLocaleLowerCase()}|${relationship.code}`;
    const group = fallbackGroups.get(groupKey) || {
      id: `fallback-group:${rootNode.id}:${groupKey}`,
      node: target,
      edge,
      relationship,
      citation,
      sourceHeading: compactWhitespace(
        target.metadata?.instrument_title
        || target.metadata?.part_title
        || target.metadata?.official_document_title
        || target.metadata?.document_title
        || target.metadata?.source_title
        || 'PRA Rulebook'
      ),
      members: [],
    };
    if (!group.members.some(member => member.node?.id === target.id)) {
      group.members.push({
        id: `${rootNode.id}|${targetKey}`,
        occurrence: null,
        node: target,
        edge,
        relationship,
        citation: compactWhitespace(metadata.reference || target.title),
      });
    }
    fallbackGroups.set(groupKey, group);
  }
  for (const group of occurrenceGroups.values()) {
    group.members.sort((a, b) => (
      Number(a.occurrence?.span_start || 0) - Number(b.occurrence?.span_start || 0)
      || a.citation.localeCompare(b.citation, undefined, { numeric: true })
    ));
    references.push(group);
  }
  references.push(...fallbackGroups.values());
  return references.sort((a, b) => {
    const priority = { REF: 0, DEF: 1, RELATED: 2 };
    return Number(a.sourceSpan?.start ?? Number.MAX_SAFE_INTEGER)
      - Number(b.sourceSpan?.start ?? Number.MAX_SAFE_INTEGER)
      || priority[a.relationship.code] - priority[b.relationship.code]
      || a.citation.localeCompare(b.citation, undefined, { numeric: true });
  });
}

export function assignReferencesToParagraphs(paragraphs, references) {
  const nextMatchByCitation = new Map();
  return references.map(reference => {
    const citationKey = compactWhitespace(reference.citation).toLocaleLowerCase();
    const previous = nextMatchByCitation.get(citationKey) || {
      paragraphIndex: 0,
      offset: 0,
    };
    for (
      let paragraphIndex = previous.paragraphIndex;
      paragraphIndex < paragraphs.length;
      paragraphIndex += 1
    ) {
      const startAt = paragraphIndex === previous.paragraphIndex
        ? previous.offset
        : 0;
      const paragraph = typeof paragraphs[paragraphIndex] === 'string'
        ? paragraphs[paragraphIndex]
        : paragraphs[paragraphIndex]?.text || '';
      const match = citationMatch(paragraph, reference, startAt);
      if (match) {
        nextMatchByCitation.set(citationKey, {
          paragraphIndex,
          offset: match.end,
        });
        return { ...reference, paragraphIndex, match };
      }
    }
    return { ...reference, paragraphIndex: -1, match: null };
  });
}

export function mergeOverlappingReferences(references) {
  const unmatched = references.filter(reference => reference.paragraphIndex < 0);
  const byParagraph = new Map();
  for (const reference of references) {
    if (reference.paragraphIndex < 0 || !reference.match) continue;
    byParagraph.set(
      reference.paragraphIndex,
      [...(byParagraph.get(reference.paragraphIndex) || []), reference],
    );
  }
  const merged = [];
  for (const paragraphReferences of byParagraph.values()) {
    const ordered = [...paragraphReferences].sort((a, b) => (
      a.match.start - b.match.start
      || b.match.end - b.match.start - (a.match.end - a.match.start)
    ));
    const clusters = [];
    for (const reference of ordered) {
      const current = clusters[clusters.length - 1];
      if (current && reference.match.start < current.end) {
        current.references.push(reference);
        current.end = Math.max(current.end, reference.match.end);
      } else {
        clusters.push({
          start: reference.match.start,
          end: reference.match.end,
          references: [reference],
        });
      }
    }
    for (const cluster of clusters) {
      if (cluster.references.length === 1) {
        merged.push(cluster.references[0]);
        continue;
      }
      const primary = [...cluster.references].sort((a, b) => (
        b.match.end - b.match.start - (a.match.end - a.match.start)
      ))[0];
      const relationshipCodes = new Set(
        cluster.references.map(reference => reference.relationship.code),
      );
      const members = [];
      const memberIds = new Set();
      for (const reference of cluster.references) {
        for (const member of reference.members || []) {
          if (memberIds.has(member.id)) continue;
          memberIds.add(member.id);
          members.push({
            ...member,
            relationship: member.relationship || reference.relationship,
          });
        }
      }
      merged.push({
        ...primary,
        id: `overlap-group:${cluster.references.map(item => item.id).sort().join(':')}`,
        citation: primary.citation,
        relationship: relationshipCodes.size === 1
          ? primary.relationship
          : { code: 'RELATED', label: 'Linked provisions' },
        members,
        overlappingReferences: cluster.references,
      });
    }
  }
  return [...merged, ...unmatched].sort((a, b) => (
    a.paragraphIndex - b.paragraphIndex
    || Number(a.match?.start ?? Number.MAX_SAFE_INTEGER)
      - Number(b.match?.start ?? Number.MAX_SAFE_INTEGER)
  ));
}

export function paragraphCitationSegments(paragraph, references) {
  const text = typeof paragraph === 'string' ? paragraph : paragraph?.text || '';
  const matches = references
    .map(reference => ({
      reference,
      match: reference.match || citationMatch(text, reference),
    }))
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
      segments.push({ type: 'text', text: text.slice(cursor, item.match.start) });
    }
    segments.push({
      type: 'citation',
      text: text.slice(item.match.start, item.match.end),
      reference: item.reference,
    });
    cursor = item.match.end;
  }
  if (cursor < text.length) {
    segments.push({ type: 'text', text: text.slice(cursor) });
  }
  return segments.length ? segments : [{ type: 'text', text }];
}

export function referenceDisplayTitle(reference) {
  if ((reference?.members || []).length > 1) {
    return reference.citation || `${reference.members.length} linked provisions`;
  }
  return reference?.node?.title || reference?.citation || 'Linked provision';
}

export function referenceShelfDensity(
  availableHeight,
  measuredHeights = {},
) {
  const available = Math.max(0, Number(availableHeight) || 0);
  for (const density of ['full', 'compact', 'dense', 'summary']) {
    const required = Math.max(0, Number(measuredHeights[density]) || 0);
    if (required > 0 && required <= available + 1) return density;
  }
  return 'summary';
}
