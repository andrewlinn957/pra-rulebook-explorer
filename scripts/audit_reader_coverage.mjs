// Exercise the same text placement functions used by ProvisionReader.
// Input: one {root_id, contents, graph, ...} reader bundle per JSONL line.
import fs from 'node:fs';
import readline from 'node:readline';
import {
  readingSpine, readerTextBlocks, readerReferences,
  assignReferencesToParagraphs, mergeOverlappingReferences, paragraphCitationSegments,
} from '../frontend/src/readingMode.js';

export function auditBundle(bundle) {
  const counts = { sources: 0, groups: 0, placed_groups: 0, unplaced_groups: 0,
    empty_target_members: 0, rendered_citations: 0 };
  const findings = [];
  for (const entry of readingSpine(bundle.contents)) {
    const blocks = readerTextBlocks(entry.bodyText, entry.sourceBlocks);
    if (!blocks.length) continue;
    counts.sources += 1;
    const refs = readerReferences(entry.node, bundle.graph);
    const placed = assignReferencesToParagraphs(blocks, refs);
    counts.groups += refs.length;
    for (const ref of placed) {
      counts[ref.paragraphIndex < 0 ? 'unplaced_groups' : 'placed_groups'] += 1;
      if (ref.paragraphIndex < 0) findings.push({ kind: 'unplaced_group',
        source_id: entry.node.id, source_title: entry.node.title,
        citation: ref.citation, reference_id: ref.id, target_id: ref.node?.id });
      for (const member of ref.members || []) {
        if (member.node?.text?.trim()) continue;
        counts.empty_target_members += 1;
        findings.push({ kind: 'empty_target', source_id: entry.node.id,
          citation: member.citation, target_id: member.node?.id,
          target_type: member.node?.node_type });
      }
    }
    const merged = mergeOverlappingReferences(placed);
    for (let i = 0; i < blocks.length; i += 1) {
      counts.rendered_citations += paragraphCitationSegments(blocks[i],
        merged.filter(ref => ref.paragraphIndex === i))
        .filter(segment => segment.type === 'citation').length;
    }
  }
  return { root_id: bundle.root_id || bundle.contents.root.id, counts, findings };
}

if (process.argv[2]) {
  const lines = readline.createInterface({ input: fs.createReadStream(process.argv[2]), crlfDelay: Infinity });
  const results = [];
  for await (const line of lines) if (line.trim()) results.push(auditBundle(JSON.parse(line)));
  const totals = {};
  for (const result of results) for (const [key, count] of Object.entries(result.counts)) {
    totals[key] = (totals[key] || 0) + count;
  }
  process.stdout.write(JSON.stringify({
    scope: 'Only exported reader bundles; placement is not a measure of undiscovered citations.',
    totals, results,
  }, null, 2) + '\n');
}
