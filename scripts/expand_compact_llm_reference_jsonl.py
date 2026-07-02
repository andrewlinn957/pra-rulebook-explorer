#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path

FIELDS = [
    'reference_text',
    'target_kind',
    'target_title_or_identifier',
    'target_part_or_document',
    'jurisdiction_or_source',
    'evidence_quote',
    'reason',
    'confidence',
]

def expand_ref(ref):
    if isinstance(ref, dict):
        return ref
    if not isinstance(ref, list):
        raise ValueError(f'reference is not list/dict: {ref!r}')
    if len(ref) != len(FIELDS):
        raise ValueError(f'compact reference has {len(ref)} fields, expected {len(FIELDS)}: {ref!r}')
    return dict(zip(FIELDS, ref))

def main():
    ap = argparse.ArgumentParser(description='Expand compact PRA LLM reference JSONL to full importer schema')
    ap.add_argument('input', type=Path)
    ap.add_argument('output', type=Path)
    args = ap.parse_args()
    lines = args.input.read_text().splitlines()
    out = []
    refs = 0
    for i, line in enumerate(lines, 1):
        obj = json.loads(line)
        node_id = obj.get('node_id', obj.get('n'))
        raw_refs = obj.get('references', obj.get('r', []))
        if not node_id:
            raise SystemExit(f'line {i}: missing node id')
        if not isinstance(raw_refs, list):
            raise SystemExit(f'line {i}: references/r is not a list')
        full_refs = [expand_ref(r) for r in raw_refs]
        refs += len(full_refs)
        out.append(json.dumps({'node_id': node_id, 'references': full_refs}, ensure_ascii=False))
    args.output.write_text('\n'.join(out) + ('\n' if out else ''))
    print(json.dumps({'input_lines': len(lines), 'output_lines': len(out), 'references': refs}, indent=2))

if __name__ == '__main__':
    main()
