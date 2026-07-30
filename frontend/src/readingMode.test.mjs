import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  assignReferencesToParagraphs,
  paragraphCitationSegments,
  readerReferences,
  referenceDisplayTitle,
  readingRelationship,
  referenceShelfDensity,
  splitLegalParagraphs,
} from './readingMode.js';

const interfaceSource = readFileSync(new URL('./main.jsx', import.meta.url), 'utf8');
const interfaceStyles = readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

test('legal text becomes readable paragraphs without losing numbered clauses', () => {
  const paragraphs = splitLegalParagraphs(
    'A firm must comply. (1) It must report promptly. (2) It must retain records.'
  );
  assert.deepEqual(paragraphs, [
    'A firm must comply.',
    '(1) It must report promptly.',
    '(2) It must retain records.',
  ]);
});

test('reference relationships use concise editorial labels', () => {
  assert.deepEqual(readingRelationship('references'), {
    code: 'REF',
    label: 'Cross-reference',
  });
  assert.equal(readingRelationship('uses_defined_term').code, 'DEF');
  assert.equal(readingRelationship('amends').code, 'RELATED');
});

test('reader keeps references one hop from the fixed root provision', () => {
  const root = { id: 'root', title: '4.1', text: 'See 5.8 and firm.' };
  const graph = {
    nodes: [
      root,
      { id: 'rule-58', title: '5.8', text: 'Referenced rule.' },
      { id: 'term-firm', title: 'firm', text: 'means an authorised person.' },
      { id: 'second-level', title: '6.2', text: 'Not directly linked.' },
    ],
    edges: [
      {
        id: 'ref',
        from_node_id: 'root',
        to_node_id: 'rule-58',
        edge_type: 'references',
        metadata: { reference: '5.8' },
      },
      {
        id: 'def',
        from_node_id: 'root',
        to_node_id: 'term-firm',
        edge_type: 'uses_defined_term',
        evidence_text: 'firm',
        metadata: {},
      },
      {
        id: 'nested',
        from_node_id: 'rule-58',
        to_node_id: 'second-level',
        edge_type: 'references',
        metadata: { reference: '6.2' },
      },
      {
        id: 'incoming',
        from_node_id: 'second-level',
        to_node_id: 'root',
        edge_type: 'references',
        metadata: { reference: '4.1' },
      },
    ],
  };
  const references = readerReferences(root, graph);
  assert.deepEqual(references.map(reference => reference.node.id), [
    'rule-58',
    'term-firm',
  ]);
});

test('reader retains the direction of guidance references rolled up to a document', () => {
  const paragraph = {
    id: 'ss-paragraph',
    title: 'SS5/15 2.3',
    text: 'Paragraph 41 of IAS 19 applies.',
  };
  const target = {
    id: 'ias-19',
    title: 'IAS 19',
    text: 'Employee benefits source text.',
  };
  const graph = {
    nodes: [
      { id: 'ss-document', title: 'SS5/15' },
      target,
    ],
    edges: [
      {
        id: 'outgoing-rollup',
        from_node_id: 'ss-document',
        to_node_id: target.id,
        edge_type: 'references',
        metadata: {
          reference: 'IAS 19',
          rolled_up_from_node_ids: [paragraph.id],
          rolled_up_from_from_node_ids: [paragraph.id],
        },
      },
      {
        id: 'incoming-rollup',
        from_node_id: 'other-document',
        to_node_id: 'ss-document',
        edge_type: 'references',
        metadata: {
          rolled_up_from_node_ids: [paragraph.id],
          rolled_up_from_to_node_ids: [paragraph.id],
        },
      },
    ],
  };

  const references = readerReferences(paragraph, graph);
  assert.deepEqual(references.map(reference => reference.node.id), ['ias-19']);
});

test('citations are clickable segments in their containing paragraph', () => {
  const paragraphs = ['A firm must apply 5.8 before submitting the return.'];
  const reference = {
    id: 'rule-58|REF',
    citation: '5.8',
    node: { id: 'rule-58', title: '5.8' },
    edge: { metadata: { reference: '5.8' } },
  };
  const assigned = assignReferencesToParagraphs(paragraphs, [reference]);
  assert.equal(assigned[0].paragraphIndex, 0);
  const segments = paragraphCitationSegments(paragraphs[0], assigned);
  assert.equal(segments[1].type, 'citation');
  assert.equal(segments[1].text, '5.8');
  assert.equal(segments[1].reference.id, 'rule-58|REF');
});

test('joined Article citations expose each referenced provision separately', () => {
  const paragraph = 'Articles 378 and 379 of the CRR shall not apply.';
  const references = [
    {
      id: 'article-378|REF',
      citation: 'Articles 378',
      node: { id: 'article-378', title: 'UK CRR Article 378' },
      edge: { metadata: { reference: 'Articles 378' } },
    },
    {
      id: 'article-379|REF',
      citation: '379',
      node: { id: 'article-379', title: 'UK CRR Article 379' },
      edge: { metadata: { reference: '379' } },
    },
  ];
  const segments = paragraphCitationSegments(paragraph, references);
  assert.deepEqual(
    segments.filter(segment => segment.type === 'citation').map(segment => segment.text),
    ['Articles 378', '379'],
  );
});

test('occurrence groups retain repeated citations to the same target', () => {
  const root = {
    id: 'audit-24',
    text: 'Apply Article 16. Article 16 applies again.',
  };
  const target = { id: 'article-16', title: 'Article 16', text: 'Source text.' };
  const graph = {
    nodes: [root, target],
    edges: [{
      id: 'article-16-edge',
      from_node_id: root.id,
      to_node_id: target.id,
      edge_type: 'references',
      metadata: {
        reference_occurrences: [
          {
            occurrence_id: 'first',
            group_id: 'group-first',
            source_node_id: root.id,
            target_node_id: target.id,
            status: 'materialized',
            citation_text: 'Article 16',
            group_text: 'Article 16',
            span_start: 6,
            span_end: 16,
            metadata: { group_span: { start: 6, end: 16 } },
          },
          {
            occurrence_id: 'second',
            group_id: 'group-second',
            source_node_id: root.id,
            target_node_id: target.id,
            status: 'materialized',
            citation_text: 'Article 16',
            group_text: 'Article 16',
            span_start: 18,
            span_end: 28,
            metadata: { group_span: { start: 18, end: 28 } },
          },
        ],
      },
    }],
  };

  const references = readerReferences(root, graph);
  assert.equal(references.length, 2);
  assert.notEqual(references[0].id, references[1].id);
  const placed = assignReferencesToParagraphs([root.text], references);
  assert.deepEqual(placed.map(reference => reference.match.start), [6, 18]);
});

test('a coordinated range is one clickable citation with every target accessible', () => {
  const root = {
    id: 'audit-24',
    text: 'Apply paragraphs 5 to 8 of Schedule 1.',
  };
  const nodes = [
    root,
    ...[5, 6, 7, 8].map(number => ({
      id: `paragraph-${number}`,
      title: `Schedule 1 paragraph ${number}`,
      text: `Paragraph ${number} source text.`,
    })),
  ];
  const groupText = 'paragraphs 5 to 8 of Schedule 1';
  const edges = nodes.slice(1).map((node, index) => ({
    id: `edge-${node.id}`,
    from_node_id: root.id,
    to_node_id: node.id,
    edge_type: 'references',
    metadata: {
      reference_occurrences: [{
        occurrence_id: `occurrence-${node.id}`,
        group_id: 'schedule-range',
        source_node_id: root.id,
        target_node_id: node.id,
        status: 'materialized',
        citation_text: `paragraphs ${index + 5}`,
        group_text: groupText,
        span_start: 6,
        span_end: 39,
        metadata: { group_span: { start: 6, end: 39 } },
      }],
    },
  }));

  const references = readerReferences(root, { nodes, edges });
  assert.equal(references.length, 1);
  assert.equal(references[0].members.length, 4);
  assert.equal(referenceDisplayTitle(references[0]), groupText);
  const placed = assignReferencesToParagraphs([root.text], references);
  assert.equal(placed[0].paragraphIndex, 0);
  assert.equal(paragraphCitationSegments(root.text, placed)[1].text, groupText);
});

test('occurrence-backed references suppress duplicate legacy edges', () => {
  const root = { id: 'root', text: 'See Article 26(6).' };
  const target = { id: 'article-26-6', title: 'Article 26(6)', text: 'Text.' };
  const occurrence = {
    occurrence_id: 'occurrence',
    group_id: 'group',
    source_node_id: root.id,
    target_node_id: target.id,
    status: 'materialized',
    citation_text: 'Article 26(6)',
    group_text: 'Article 26(6)',
    span_start: 4,
    span_end: 17,
    metadata: { group_span: { start: 4, end: 17 } },
  };
  const references = readerReferences(root, {
    nodes: [root, target],
    edges: [
      {
        id: 'legacy',
        from_node_id: root.id,
        to_node_id: target.id,
        edge_type: 'references',
        metadata: { reference: 'Article 26(6)' },
      },
      {
        id: 'occurrence-edge',
        from_node_id: root.id,
        to_node_id: target.id,
        edge_type: 'references',
        metadata: { reference_occurrences: [occurrence] },
      },
    ],
  });
  assert.equal(references.length, 1);
  assert.equal(references[0].members[0].id, 'occurrence');
});

test('defined terms remain clickable when the provision uses a simple plural', () => {
  const reference = {
    id: 'term-rule|DEF',
    citation: 'rule',
    relationship: { code: 'DEF' },
    node: { id: 'term-rule', title: 'rule' },
    edge: { evidence_text: 'rule', metadata: {} },
  };
  const segments = paragraphCitationSegments(
    'The rules apply to the firm.',
    [reference],
  );
  assert.equal(segments[0].type, 'text');
  assert.equal(segments[1].type, 'citation');
  assert.equal(segments[1].text, 'rules');
});

test('shelf density keeps complete cards visible whenever their measured content fits', () => {
  assert.equal(referenceShelfDensity(640, 3, 620), 'full');
  assert.equal(referenceShelfDensity(640, 3, 900), 'compact');
  assert.equal(referenceShelfDensity(420, 4, 600), 'compact');
  assert.equal(referenceShelfDensity(320, 4, 600), 'dense');
  assert.equal(referenceShelfDensity(260, 6, 600), 'summary');
  assert.equal(referenceShelfDensity(320, 2, 300), 'full');
});

test('graph inspector exposes reading mode with inline and pinned reference actions', () => {
  assert.match(interfaceSource, /className="reading-mode-entry"/);
  assert.match(interfaceSource, /function ProvisionReader\(/);
  assert.match(interfaceSource, /function InlineLegalReference\(/);
  assert.match(interfaceSource, /Pin to shelf/);
  assert.match(interfaceSource, /Return inline/);
  assert.match(interfaceSource, /setExpandedId\(current=>current===reference\.id\?'':reference\.id\)/);
  assert.match(interfaceSource, /depth:'1'/);
});

test('reference shelf is space-aware, sticky, scrollable and becomes a narrow-screen drawer', () => {
  assert.match(interfaceSource, /referenceShelfDensity\(availableHeight,references\.length,fullContentHeight\)/);
  assert.match(interfaceSource, /new ResizeObserver\(update\)/);
  assert.match(interfaceSource, /reference-shelf-measurement-list/);
  assert.match(interfaceSource, /<p>{sourceNode\?\.text\|\|'No excerpt available\.'}<\/p>/);
  assert.doesNotMatch(interfaceSource, /truncate\(sourceNode\?\.text/);
  assert.match(interfaceSource, /is-temporarily-expanded/);
  assert.match(interfaceStyles, /\.reference-shelf\{[^}]*position:sticky/);
  assert.match(interfaceStyles, /\.reference-shelf-list\{[^}]*overflow:auto/);
  assert.match(interfaceStyles, /\.reference-shelf-measurement-list\{/);
  assert.match(interfaceStyles, /\.reference-shelf\.density-summary/);
  assert.match(interfaceStyles, /@media\(max-width:860px\)[\s\S]*\.reference-shelf\{[\s\S]*position:fixed/);
  assert.match(interfaceStyles, /\.reference-shelf\.is-mobile-open\{transform:translateX\(0\)\}/);
});
