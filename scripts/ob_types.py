"""Local reviewed OB type bridge. Never discovers instances or runs pipelines."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from serein.ingest import ob_types


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preview', 'apply', 'undo', 'export'))
    parser.add_argument('--database', required=True)
    parser.add_argument('--input', help='Reviewed JSON: namespace, records and identity_map')
    parser.add_argument('--output', required=True, help='New receipt file; never overwrites')
    parser.add_argument('--batch-id')
    parser.add_argument('--expected-preview-sha256')
    parser.add_argument('--backup')
    args = parser.parse_args()
    target = Path(args.output)
    if target.exists():
        parser.error('Output already exists; read it before retrying')
    if not target.parent.is_dir():
        parser.error('Output parent must exist')
    if args.action in ('preview', 'apply'):
        if not args.input:
            parser.error('--input is required')
        envelope = json.loads(Path(args.input).read_text(encoding='utf-8'))
        kwargs = {'namespace': envelope['namespace']}
        if 'self_mappings' in envelope:
            kwargs['self_mappings'] = envelope['self_mappings']
        if args.action == 'apply':
            if not all((args.batch_id, args.expected_preview_sha256, args.backup)):
                parser.error('apply requires --batch-id, --expected-preview-sha256 and --backup')
            kwargs.update(batch_id=args.batch_id, expected_preview_sha256=args.expected_preview_sha256, backup_path=args.backup)
        result = getattr(ob_types, args.action)(args.database, envelope['records'], envelope['identity_map'], **kwargs)
    elif args.action == 'undo':
        if not args.batch_id:
            parser.error('--batch-id is required')
        result = ob_types.undo(args.database, args.batch_id)
    else:
        result = ob_types.export_types(args.database)
    with target.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write(chr(10))
    print(json.dumps({'status': result.get('status', 'saved'), 'output': str(target.resolve())}))


if __name__ == '__main__':
    main()
