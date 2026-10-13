#!/usr/bin/env python3
"""Read-only occurrence integrity and ontology inventory, with reader sampling.

This measures recorded references, not absolute recall. Run reference_recall_audit.py
alongside it to create a separate candidate-review ledger. No models or network
requests are used. Reader bundles can be replayed through audit_reader_coverage.mjs.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.graph import reader_bundle
from scripts.safe_connect import connect


def audit(conn: sqlite3.Connection) -> dict:
    """Audit references without relying on the extractor's success flags."""
    counts = Counter()
    examples: dict[str, list[dict]] = {}

    def flag(kind: str, row: dict) -> None:
        counts[kind] += 1
        if len(examples.setdefault(kind, [])) < 12:
            examples[kind].append({key: row.get(key) for key in (
                'occurrence_id', 'source_node_id', 'source_title', 'target_node_id',
                'target_node_type', 'target_url', 'edge_id', 'citation_text', 'group_text',
                'span_start', 'span_end', 'span_basis', 'source_method',
            )})

    nodes = {row['id']: dict(row) for row in conn.execute(
        'SELECT id,node_type,title,text,url,metadata_json FROM node')}
    edges = {row['id']: dict(row) for row in conn.execute(
        'SELECT id,from_node_id,to_node_id,edge_type,source_url FROM edge')}
    readable_versions = set()
    for node in nodes.values():
        if node['node_type'] == 'rule' and (node['text'] or '').strip():
            readable_versions.add(json.loads(node['metadata_json'] or '{}').get('canonical_provision_id'))
    contained_children: dict[str, list[str]] = {}
    for edge in edges.values():
        if edge['edge_type'] == 'contains':
            contained_children.setdefault(edge['from_node_id'], []).append(edge['to_node_id'])

    readable_descendants: dict[str, bool] = {}

    def has_readable_descendant(node_id: str, visiting: set[str] | None = None) -> bool:
        if node_id in readable_descendants:
            return readable_descendants[node_id]
        visiting = set() if visiting is None else visiting
        if node_id in visiting:
            return False
        visiting.add(node_id)
        found = any(
            bool((nodes.get(child, {}).get('text') or '').strip())
            or has_readable_descendant(child, visiting)
            for child in contained_children.get(node_id, [])
        )
        visiting.remove(node_id)
        readable_descendants[node_id] = found
        return found

    def metadata_for(row: dict) -> dict:
        try:
            value = json.loads(row.get('metadata_json') or '{}')
        except (TypeError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    for record in conn.execute('SELECT * FROM reference_occurrence ORDER BY occurrence_id'):
        row = dict(record)
        counts['occurrences'] += 1
        counts[f"status:{row['status']}"] += 1
        source, target = nodes.get(row['source_node_id']), nodes.get(row['target_node_id'])
        edge = edges.get(row['edge_id'])
        row['source_title'] = source['title'] if source else None
        row['target_node_type'] = target['node_type'] if target else None
        row['target_url'] = (target.get('url') or (edge.get('source_url') if edge else '') or '') if target else None
        if source is None:
            flag('missing_source', row)
        elif row['status'] != 'not_reference':
            metadata = metadata_for(row)
            span_basis = metadata.get('span_basis')
            row['span_basis'] = span_basis or 'text'
            text = (source['title'] or '') if span_basis == 'title' else (source['text'] or '')
            if not (0 <= row['span_start'] < row['span_end'] <= len(text)):
                flag('invalid_span', row)
            elif ' '.join(text[row['span_start']:row['span_end']].split()) not in {
                ' '.join((row.get('group_text') or '').split()),
                ' '.join((row.get('citation_text') or '').split()),
            }:
                # Group/range occurrences can share one lexical span. Accept
                # either stored group wording or an individual citation.
                flag('span_text_mismatch_review', row)
        if row['status'] != 'materialized':
            continue
        if target is None:
            flag('materialized_missing_target', row)
        elif not (target['text'] or '').strip() and target['id'] not in readable_versions:
            flag('materialized_target_without_text_or_version', row)
            if has_readable_descendant(target['id']):
                flag('materialized_target_expandable_container', row)
            elif (target.get('url') or (edge.get('source_url') if edge else '') or '').strip():
                flag('materialized_target_url_only', row)
            else:
                flag('materialized_target_without_content_or_url', row)
        if edge is None:
            flag('materialized_missing_edge', row)
        elif (
            edge['from_node_id'] != row['source_node_id']
            or edge['to_node_id'] != row['target_node_id']
            or edge['edge_type'] != {
                'REF': 'references', 'DEF': 'uses_defined_term',
                'AMEND': 'amends', 'AMENDS': 'amends',
            }.get(str(row['relationship_type'] or '').upper())
        ):
            flag('materialized_endpoint_mismatch', row)

    inventories = {}
    for table, column in [('node', 'node_type'), ('edge', 'edge_type'), ('edge', 'source_method'),
                          ('graph_node', 'node_type'), ('graph_edge', 'edge_type'),
                          ('graph_edge', 'review_status')]:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            inventories[f'{table}.{column}'] = [dict(row) for row in conn.execute(
                f'SELECT {column},COUNT(*) count FROM {table} GROUP BY {column} ORDER BY count DESC')]
    return {'scope': 'Recorded occurrences and graph inventory; not absolute citation recall.',
            'counts': dict(counts), 'examples': examples, 'inventories': inventories}


def sample_roots(conn: sqlite3.Connection, per_type: int) -> list[str]:
    """Deterministic, deliberately reference-heavy atomic-source sample."""
    ids = []
    for kind in ('rule', 'guidance_paragraph', 'defined_term'):
        ids.extend(row[0] for row in conn.execute('''
            SELECT n.id FROM node n JOIN edge e ON e.from_node_id=n.id
            WHERE n.node_type=? AND length(trim(n.text))>0 AND e.edge_type='references'
            GROUP BY n.id ORDER BY count(*) DESC,n.id LIMIT ?
        ''', (kind, per_type)))
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'backend/data/rulebook.sqlite3')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--reader-sample-per-type', type=int, default=0)
    parser.add_argument('--reader-root', action='append', default=[])
    parser.add_argument('--export-reader-text', action='store_true', help='Export every rule, guidance paragraph and defined term for text preservation checks')
    parser.add_argument('--strict', action='store_true', help='Exit 1 for broken occurrence links or invalid spans; does not certify recall')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db, readonly=True)
    conn.execute('PRAGMA query_only=ON')
    # Pin a consistent read transaction even when auditing a running database.
    conn.execute('BEGIN')
    try:
        result = audit(conn)
        result['generated_at'] = datetime.now(timezone.utc).isoformat()
        result['database'] = str(args.db.resolve())
        roots = list(dict.fromkeys(args.reader_root + sample_roots(conn, max(0, args.reader_sample_per_type))))
        result['reader_roots'] = roots
        (args.output_dir / 'cross-reference-integrity.json').write_text(json.dumps(result, indent=2) + '\n')
        if args.export_reader_text:
            with (args.output_dir / 'reader-text-corpus.jsonl').open('w') as handle:
                for row in conn.execute("SELECT id,node_type,title,text,metadata_json FROM node WHERE node_type IN ('rule','guidance_paragraph','defined_term') ORDER BY id"):
                    handle.write(json.dumps(dict(row)) + '\n')
        if roots:
            with (args.output_dir / 'reader-bundles.jsonl').open('w') as handle:
                for index, root in enumerate(roots, 1):
                    bundle = reader_bundle(conn, root, reference_depth=2)
                    handle.write(json.dumps({'root_id': root, **bundle}) + '\n')
                    handle.flush()
                    print(f'reader {index}/{len(roots)}: {root}', file=sys.stderr)
        print(json.dumps(result['counts'], indent=2))
    finally:
        conn.close()
    broken = ('missing_source', 'materialized_missing_target', 'materialized_missing_edge',
              'materialized_endpoint_mismatch', 'invalid_span')
    return 1 if args.strict and any(result['counts'].get(key, 0) for key in broken) else 0


if __name__ == '__main__':
    raise SystemExit(main())
