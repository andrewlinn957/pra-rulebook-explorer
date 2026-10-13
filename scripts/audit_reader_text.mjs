// Compare the reader's reconstructed text with every exported atomic source.
import fs from 'node:fs';
import readline from 'node:readline';
import { readerTextBlocks } from '../frontend/src/readingMode.js';

const compact = value => String(value || '').replace(/\s+/g, ' ').trim();
export function checkText(node, renderBlocks = readerTextBlocks) {
  const metadata = node.metadata || JSON.parse(node.metadata_json || '{}');
  const source = compact(node.text);
  const rendered = compact(renderBlocks(node.text, metadata.text_blocks)
    .map(block => block.marker ? `${block.marker} ${block.text}` : block.text).join(' '));
  return { id: node.id, node_type: node.node_type, title: node.title,
    matches: source === rendered,
    content_matches: source.replace(/\s/g, '') === rendered.replace(/\s/g, ''),
    source_length: source.length, rendered_length: rendered.length,
    source_preview: source.slice(0, 180), rendered_preview: rendered.slice(0, 180) };
}

if (process.argv[2]) {
  const lines = readline.createInterface({ input: fs.createReadStream(process.argv[2]), crlfDelay: Infinity });
  let scanned = 0;
  const changed = [];
  const byType = {};
  for await (const line of lines) {
    if (!line.trim()) continue;
    const result = checkText(JSON.parse(line));
    scanned += 1;
    if (!result.matches) {
      changed.push(result);
      byType[result.node_type] = (byType[result.node_type] || 0) + 1;
    }
  }
  const contentChanged = changed.filter(result => !result.content_matches);
  const contentByType = {};
  for (const result of contentChanged) contentByType[result.node_type] = (contentByType[result.node_type] || 0) + 1;
  process.stdout.write(JSON.stringify({ scanned, changed: changed.length, changed_by_type: byType,
    content_changed: contentChanged.length, content_changed_by_type: contentByType,
    findings: changed }, null, 2) + '\n');
}
