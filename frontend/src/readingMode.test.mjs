import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  assignReferencesToParagraphs,
  legalTextBlocks,
  mergeOverlappingReferences,
  paragraphCitationSegments,
  readingSpine,
  readerReferences,
  readerTextBlocks,
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

test('legal text preserves nested list markers and does not split paragraph references', () => {
  const blocks = legalTextBlocks(
    'A firm must do the following: (1) retain records; (2) provide: '
    + '(a) its name; (b) all of: (i) the date; (ii) the amount; and (c) '
    + 'the information set out in (1).'
  );
  assert.deepEqual(
    blocks.map(({ kind, marker, depth, text }) => ({
      kind,
      marker,
      depth,
      text,
    })),
    [
      { kind: 'prose', marker: '', depth: 0, text: 'A firm must do the following:' },
      { kind: 'list-item', marker: '(1)', depth: 0, text: 'retain records;' },
      { kind: 'list-item', marker: '(2)', depth: 0, text: 'provide:' },
      { kind: 'list-item', marker: '(a)', depth: 1, text: 'its name;' },
      { kind: 'list-item', marker: '(b)', depth: 1, text: 'all of:' },
      { kind: 'list-item', marker: '(i)', depth: 2, text: 'the date;' },
      { kind: 'list-item', marker: '(ii)', depth: 2, text: 'the amount; and' },
      {
        kind: 'list-item',
        marker: '(c)',
        depth: 1,
        text: 'the information set out in (1).',
      },
    ],
  );
});

test('reader falls back to complete body text when server blocks are incomplete', () => {
  const body = '(1) First limb. (2) Second limb. (a) Nested limb A. (b) Nested limb B.';
  const incomplete = [
    { kind: 'list-item', marker: '(1)', depth: 0, text: 'First limb.' },
    { kind: 'list-item', marker: '(2)', depth: 0, text: 'Second limb.' },
  ];

  assert.deepEqual(
    readerTextBlocks(body, incomplete).map(block => block.marker),
    ['(1)', '(2)', '(a)', '(b)'],
  );
});

test('reader omits an empty resolution-policy target when an explicit readable link covers the citation', () => {
  const root = {
    id: 'close-links-5-2',
    text: 'The Close Links Monthly Report can be found here.',
  };
  const emptyPart = { id: 'close-links-part', title: 'Close Links', text: '' };
  const report = { id: 'monthly-report', title: 'REP001a', text: 'Report source text.' };
  const references = readerReferences(root, {
    nodes: [root, emptyPart, report],
    edges: [
      {
        id: 'policy-resolution',
        from_node_id: root.id,
        to_node_id: emptyPart.id,
        edge_type: 'references',
        source_method: 'resolution_policy_v1',
        evidence_text: 'The Close Links Monthly Report can be found here.',
        metadata: { reference: 'The Close Links Monthly Report', target_text_available: false },
      },
      {
        id: 'explicit-report',
        from_node_id: root.id,
        to_node_id: report.id,
        edge_type: 'references',
        source_method: 'html_link',
        evidence_text: 'here',
        metadata: { href: 'https://example.test/report.pdf' },
      },
    ],
  });

  assert.deepEqual(references.map(reference => reference.node.id), [report.id]);
});

test('reader omits a duplicate resolution-policy citation when an explicit link occurrence exists', () => {
  const root = { id: 'source', text: 'See Insurance Company – Internal Contagion Risk 4.1.' };
  const target = { id: 'target', title: '4.1', text: 'Target source text.' };
  const occurrence = {
    occurrence_id: 'html-occurrence',
    group_id: 'html-occurrence',
    source_node_id: root.id,
    target_node_id: target.id,
    status: 'materialized',
    citation_text: 'Insurance Company – Internal Contagion Risk 4.1',
    group_text: 'Insurance Company – Internal Contagion Risk 4.1',
    span_start: 4,
    span_end: 52,
  };
  const policyOccurrence = {
    ...occurrence,
    occurrence_id: 'policy-occurrence',
    group_id: 'policy-occurrence',
    source_method: 'resolution_policy_v1',
  };
  const references = readerReferences(root, {
    nodes: [root, target],
    edges: [
      {
        id: 'explicit',
        from_node_id: root.id,
        to_node_id: target.id,
        edge_type: 'references',
        source_method: 'html_anchor_resolved',
        metadata: { reference_occurrences: [occurrence] },
      },
      {
        id: 'policy',
        from_node_id: root.id,
        to_node_id: target.id,
        edge_type: 'references',
        source_method: 'regex_named_reference',
        metadata: {
          reference: 'Insurance Company – Internal Contagion Risk 4.1',
          reference_occurrences: [policyOccurrence],
        },
      },
    ],
  });

  assert.equal(references.length, 1);
  assert.equal(references[0].edge.id, 'explicit');
});

test('reading spine includes nested child provisions in source order', () => {
  const spine = readingSpine({
    root: { id: 'chapter', title: 'Chapter', text: '' },
    children: [
      {
        id: 'section',
        title: 'Section',
        text: '',
        children: [{ id: 'rule-1', title: '1.1', text: 'First rule.' }],
      },
      { id: 'rule-2', title: '1.2', text: 'Second rule.' },
    ],
  });
  assert.deepEqual(
    spine.map(entry => [entry.node.id, entry.depth, entry.isRoot, entry.bodyText]),
    [
      ['chapter', 0, true, ''],
      ['section', 1, false, ''],
      ['rule-1', 2, false, 'First rule.'],
      ['rule-2', 1, false, 'Second rule.'],
    ],
  );
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

test('overlapping cross-references and definitions remain accessible through one citation', () => {
  const paragraph = 'Apply Art. 109 of the Solvency II Directive.';
  const assigned = assignReferencesToParagraphs([paragraph], [
    {
      id: 'article',
      citation: 'Art. 109 of the Solvency II Directive',
      relationship: { code: 'REF', label: 'Cross-reference' },
      node: { id: 'article-109', title: 'Article 109' },
      edge: { metadata: {} },
      members: [{ id: 'article-member', node: { id: 'article-109' } }],
    },
    {
      id: 'directive',
      citation: 'Solvency II Directive',
      relationship: { code: 'DEF', label: 'Definition' },
      node: { id: 'directive-term', title: 'Solvency II Directive' },
      edge: { metadata: {} },
      members: [{ id: 'directive-member', node: { id: 'directive-term' } }],
    },
  ]);
  const merged = mergeOverlappingReferences(assigned);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].relationship.code, 'RELATED');
  assert.deepEqual(
    merged[0].members.map(member => [member.node.id, member.relationship.code]),
    [['article-109', 'REF'], ['directive-term', 'DEF']],
  );
  assert.equal(
    paragraphCitationSegments(paragraph, merged)
      .filter(segment => segment.type === 'citation').length,
    1,
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

test('occurrence citation text anchors mixed citations sharing one target edge', () => {
  const root = {
    id: 'lcr-article-10-1',
    text: 'Apply Article 115(2) of CRR.\nThen Article 115(4).',
  };
  const target = { id: 'article-115', title: 'UK CRR Article 115', text: 'Source text.' };
  const occurrences = [
    {
      occurrence_id: 'article-115-2',
      group_id: 'group-115-2',
      source_node_id: root.id,
      target_node_id: target.id,
      status: 'materialized',
      citation_text: 'Article 115(2)',
      group_text: 'Article 115(2)',
      span_start: 6,
      span_end: 20,
    },
    {
      occurrence_id: 'article-115-4',
      group_id: 'group-115-4',
      source_node_id: root.id,
      target_node_id: target.id,
      status: 'materialized',
      citation_text: 'Article 115(4)',
      group_text: 'Article 115(4)',
      span_start: 34,
      span_end: 48,
    },
  ];
  const references = readerReferences(root, {
    nodes: [root, target],
    edges: [{
      id: 'article-115-edge',
      from_node_id: root.id,
      to_node_id: target.id,
      edge_type: 'references',
      metadata: {
        reference: 'Article 115(2) of CRR',
        reference_occurrences: occurrences,
      },
    }],
  });

  const paragraphs = ['Apply Article 115(2) of CRR.', 'Then Article 115(4).'];
  const assigned = assignReferencesToParagraphs(paragraphs, references);
  assert.deepEqual(
    assigned.map(reference => [reference.citation, reference.paragraphIndex, reference.match?.start]),
    [['Article 115(2)', 0, 6], ['Article 115(4)', 1, 5]],
  );
  const merged = mergeOverlappingReferences(assigned);
  assert.deepEqual(
    merged.flatMap(reference => paragraphCitationSegments(
      paragraphs[reference.paragraphIndex],
      [reference],
    ))
      .filter(segment => segment.type === 'citation')
      .map(segment => segment.text),
    ['Article 115(2)', 'Article 115(4)'],
  );
});

test('citation matching does not treat a shorter article qualifier as a prefix', () => {
  const root = {
    id: 'covered-bond-rule',
    text: 'Apply Article 129(1)(c), then Article 129(1).',
  };
  const target = { id: 'article-129', title: 'UK CRR Article 129', text: 'Source text.' };
  const occurrences = [
    {
      occurrence_id: 'article-129-1-c',
      group_id: 'group-129-1-c',
      source_node_id: root.id,
      target_node_id: target.id,
      status: 'materialized',
      citation_text: 'Article 129(1)(c)',
      group_text: 'Article 129(1)(c)',
      span_start: 6,
      span_end: 23,
    },
    {
      occurrence_id: 'article-129-1',
      group_id: 'group-129-1',
      source_node_id: root.id,
      target_node_id: target.id,
      status: 'materialized',
      citation_text: 'Article 129(1)',
      group_text: 'Article 129(1)',
      span_start: 30,
      span_end: 44,
    },
  ];
  const references = readerReferences(root, {
    nodes: [root, target],
    edges: [{
      id: 'article-129-edge',
      from_node_id: root.id,
      to_node_id: target.id,
      edge_type: 'references',
      metadata: {
        reference: 'Article 129(4)',
        reference_occurrences: occurrences,
      },
    }],
  });

  const assigned = assignReferencesToParagraphs([root.text], references);
  assert.deepEqual(
    assigned.map(reference => [reference.citation, reference.match?.start]),
    [['Article 129(1)(c)', 6], ['Article 129(1)', 30]],
  );
  assert.deepEqual(
    paragraphCitationSegments(root.text, mergeOverlappingReferences(assigned))
      .filter(segment => segment.type === 'citation')
      .map(segment => segment.text),
    ['Article 129(1)(c)', 'Article 129(1)'],
  );
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

test('legacy edges with the same citation become one reference with all targets retained', () => {
  const root = { id: 'root', text: 'Apply 3.3.' };
  const first = { id: 'first-33', title: '3.3', text: 'First candidate.' };
  const second = { id: 'second-33', title: '3.3', text: 'Second candidate.' };
  const references = readerReferences(root, {
    nodes: [root, first, second],
    edges: [
      {
        id: 'first',
        from_node_id: root.id,
        to_node_id: first.id,
        edge_type: 'references',
        evidence_text: 'General Provisions 3.3',
        metadata: {},
      },
      {
        id: 'second',
        from_node_id: root.id,
        to_node_id: second.id,
        edge_type: 'references',
        metadata: { reference: '3.3' },
      },
    ],
  });
  assert.equal(references.length, 1);
  assert.equal(references[0].citation, '3.3');
  assert.deepEqual(
    references[0].members.map(member => member.node.id),
    ['first-33', 'second-33'],
  );
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
  assert.equal(referenceShelfDensity(640, {
    full: 620, compact: 480, dense: 320, summary: 210,
  }), 'full');
  assert.equal(referenceShelfDensity(640, {
    full: 641.5, compact: 580, dense: 360, summary: 230,
  }), 'compact');
  assert.equal(referenceShelfDensity(420, {
    full: 900, compact: 421.5, dense: 380, summary: 240,
  }), 'dense');
  assert.equal(referenceShelfDensity(260, {
    full: 900, compact: 600, dense: 340, summary: 220,
  }), 'summary');
  assert.equal(referenceShelfDensity(200, {
    full: 900, compact: 600, dense: 340, summary: 220,
  }), 'summary');
});

test('graph inspector exposes reading mode with inline and pinned reference actions', () => {
  assert.match(interfaceSource, /className="reading-mode-entry"/);
  assert.match(interfaceSource, /function ProvisionReader\(/);
  assert.match(interfaceSource, /function InlineLegalReference\(/);
  assert.match(interfaceSource, /Pin to shelf/);
  assert.match(interfaceSource, /Return inline/);
  assert.match(interfaceSource, /setExpandedId\(current=>current===reference\.id\?'':reference\.id\)/);
  assert.match(interfaceSource, /\/reader/);
  assert.match(interfaceSource, /reference_depth=\$\{referenceDepth\}/);
  assert.match(interfaceSource, /aria-label="Reference depth"/);
  assert.match(interfaceSource, /\[1,2,3\]\.map\(depth/);
  assert.match(interfaceSource, /level<maxDepth/);
  assert.doesNotMatch(interfaceSource, /Loading one-level references/);
});

test('expanded reader references retain access to the legal text parser', () => {
  const readerImport = interfaceSource.match(
    /import \{([\s\S]*?)\} from '\.\/readingMode\.js';/,
  )?.[1] || '';
  assert.match(readerImport, /legalTextBlocks/);
  assert.match(interfaceSource, /const blocks=useMemo\(\(\)=>legalTextBlocks\(selectedNode\?\.text\|\|''\)/);
});

test('nested reader references expand and pin without collapsing their ancestors', () => {
  const pinHandler = interfaceSource.match(
    /function pinReference\(reference\)\{([\s\S]*?)\n  \}/,
  )?.[1] || '';
  assert.match(pinHandler, /setPinned\(/);
  assert.doesNotMatch(pinHandler, /setExpandedId\(/);
  assert.match(interfaceSource, /function activateNestedReference\(nestedReference\)/);
  assert.match(interfaceSource, /pinnedIds\.has\(nestedReference\.id\)/);
  assert.match(interfaceSource, /onPinnedActivate\(nestedReference\)/);
  assert.match(interfaceSource, /onClick=\{\(\)=>\{onPin\(reference\);onCollapse\(\);\}\}/);
  assert.match(interfaceSource, /level=\{level\+1\}[\s\S]*pinnedIds=\{pinnedIds\}/);
});

test('reference shelf is space-aware, sticky, scrollable and becomes a narrow-screen drawer', () => {
  assert.match(interfaceSource, /referenceShelfDensity\(availableHeight,measuredHeights\)/);
  assert.match(interfaceSource, /new ResizeObserver\(update\)/);
  assert.match(interfaceSource, /\['full','compact','dense','summary'\]\.map\(measurementDensity/);
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

test('reading issue reports preserve draft text while minimising the description', () => {
  assert.match(interfaceSource, /reading-issue-layer/);
  assert.match(interfaceSource, /aria-modal="false"/);
  assert.match(interfaceSource, /Minimise description/);
  assert.match(interfaceSource, /Expand description/);
  assert.match(interfaceSource, /aria-expanded=\{!minimised\}/);
  assert.match(interfaceSource, /value=\{text\}/);
  assert.match(interfaceSource, /setText\(e\.target\.value\)/);
  assert.match(interfaceSource, /is-minimised/);
  assert.match(interfaceSource, /reportError=\{error\}/);
  assert.match(interfaceSource, /className="issue-report-error" role="alert"/);
  assert.match(
    interfaceSource,
    /id=\{readingIssue\?'reading-issue-description':undefined\} className=\{`issue-report-body[^]*issue-context-note[^]*<\/p>\n    <\/div>[^]*<div className="modal-actions">/,
  );

  assert.match(interfaceStyles, /\.shell\.reading-view-mode>\.canvas\{[^}]*--reader-shelf-width/);
  assert.match(interfaceStyles, /\.shell\.reading-view-mode>\.canvas\{[^}]*--reader-issue-width/);
  assert.doesNotMatch(interfaceStyles, /\.provision-reader\{[^}]*--reader-shelf-width/);
  assert.match(
    interfaceStyles,
    /\.provision-reader-layout\{[^}]*var\(--reader-shelf-width\)[\s\S]*?\.reading-issue-layer \.reading-issue-report-modal\{[^}]*var\(--reader-shelf-width\)/,
  );
  assert.match(interfaceStyles, /\.reading-issue-layer\{[^}]*position:absolute/);
  assert.match(interfaceStyles, /\.reading-issue-layer\{[^}]*pointer-events:none/);
  assert.match(interfaceStyles, /\.reading-issue-layer \.reading-issue-report-modal\{[^}]*pointer-events:auto/);
  assert.match(interfaceStyles, /right:calc\(var\(--reader-shelf-width\) \+ 18px\)/);
  assert.match(interfaceStyles, /width:var\(--reader-issue-width\)/);
  assert.match(interfaceStyles, /@media\(min-width:1101px\)[\s\S]*\.shell\.reading-issue-open \.provision-title-block/);
  assert.match(interfaceStyles, /padding-right:min\(calc\(var\(--reader-issue-width\) \+ 42px\),45%\)/);
  assert.match(interfaceStyles, /\.issue-report-body\.is-minimised[^\{]*\{[^}]*display:none/);
  assert.match(interfaceStyles, /\.issue-report-description textarea[^}]*resize:vertical/);
  assert.doesNotMatch(interfaceStyles, /\.shell\.reading-issue-open \.provision-reader-layout\{[^}]*minmax\(280px,320px\)/);
  assert.doesNotMatch(interfaceStyles, /\.shell\.reading-issue-open \.reference-shelf\{[^}]*grid-column:3/);
  assert.match(interfaceStyles, /@media\(max-width:1100px\)[\s\S]*\.reading-issue-layer/);

  const readerLayer = interfaceSource.match(
    /reading-issue-layer[\s\S]*?(?=modal-backdrop|function ProvisionReader)/,
  )?.[0] || '';
  assert.doesNotMatch(readerLayer, /onClick=[^>]*onClose/);
  assert.match(interfaceSource, /modal-backdrop[\s\S]*if\(e\.target===e\.currentTarget\)onClose\(\)/);
});

test('multi-provision reader reports expose scoped provision flags and an explicit whole-node action', () => {
  assert.match(interfaceSource, /function readerIssueTargetLabel\(node\)/);
  assert.match(interfaceSource, /const showProvisionReportFlags=provisionCount>1/);
  assert.match(interfaceSource, /className="reading-provision-heading-actions"/);
  assert.match(interfaceSource, /className="report-issue-flag"/);
  assert.match(interfaceSource, /onReportIssue\?\.\(section\.node\)/);
  assert.match(interfaceSource, /readerIssueTargetLabel\(node\)/);
  assert.match(interfaceStyles, /\.reading-provision-heading-actions\{/);
  assert.match(interfaceStyles, /\.report-issue-flag\{/);
});

test('reader issue reports render inside the canvas and other reports stay after the inspector', () => {
  assert.match(
    interfaceSource,
    /<main className="canvas">[\s\S]*readingNode\?[\s\S]*issueReportNode&&<IssueReportModal[\s\S]*context="reading_mode"[\s\S]*<\/main>/,
  );
  assert.match(
    interfaceSource,
    /<\/main>[\s\S]*<aside className=\{panelOpen\?'inspector open':'inspector'\}>[\s\S]*<\/aside>[\s\S]*\{issueReportNode&&!readingNode&&<IssueReportModal[\s\S]*context="graph_view"/,
  );
});

test('closing reading mode clears its issue draft without changing graph report context', () => {
  assert.match(
    interfaceSource,
    /onClose=\{\(\)=>\{setReadingNode\(null\);setIssueReportNode\(null\);setIssueText\(''\);setError\(''\);\}\}/,
  );
  assert.match(
    interfaceSource,
    /context="reading_mode" reportError=\{error\} onClose=\{\(\)=>\{setIssueReportNode\(null\);setIssueText\(''\);setError\(''\);\}\}/,
  );
  assert.match(
    interfaceSource,
    /context="graph_view" onClose=\{\(\)=>setIssueReportNode\(null\)\}/,
  );
  assert.match(interfaceSource, /aria-controls=\{readingIssue\?'reading-issue-description':undefined\}/);
  assert.match(interfaceSource, /id=\{readingIssue\?'reading-issue-description':undefined\}/);
  assert.match(
    interfaceSource,
    /\{issueReportNode&&!readingNode&&<IssueReportModal[\s\S]*context="graph_view"/,
  );
});
