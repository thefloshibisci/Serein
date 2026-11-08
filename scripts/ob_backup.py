"""Offline OB ZIP adapter/archive CLI. Uses only explicitly named local paths."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from serein.ingest import ob_archive, ob_backup, ob_types, ob_migration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('adapt', 'preview', 'archive', 'typed-preview', 'migration-preview', 'migrate', 'undo', 'list', 'read'))
    parser.add_argument('--input')
    parser.add_argument('--database')
    parser.add_argument('--namespace')
    parser.add_argument('--timezone-policy', help='Reviewed JSON with zone and source evidence')
    parser.add_argument('--expected-preview-sha256')
    parser.add_argument('--backup')
    parser.add_argument('--record-id')
    parser.add_argument('--genre', default='')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists() or not output.parent.is_dir():
        parser.error('Use a new output path in an existing directory')
    if args.action == 'adapt':
        if not args.input or not args.namespace:
            parser.error('adapt requires --input ZIP and --namespace')
        policy = json.loads(Path(args.timezone_policy).read_text('utf-8')) if args.timezone_policy else None
        result = ob_backup.read_backup(args.input, namespace=args.namespace, timezone_policy=policy)
    else:
        if not args.database:
            parser.error('--database is required')
        if args.action in ('list', 'read', 'undo'):
            if not args.namespace:
                parser.error('--namespace is required')
            if args.action == 'undo':
                result = ob_migration.undo(args.database, args.namespace)
            elif args.action == 'read':
                if not args.record_id:
                    parser.error('--record-id is required')
                result = ob_archive.read_record(args.database, args.namespace, args.record_id)
            else:
                result = ob_archive.list_records(args.database, args.namespace, genre=args.genre)
        else:
            if not args.input:
                parser.error('--input reviewed envelope is required')
            envelope = json.loads(Path(args.input).read_text('utf-8'))
            if args.action == 'preview':
                result = ob_archive.preview(args.database, envelope)
            elif args.action == 'migration-preview':
                result = ob_migration.preview(args.database, envelope)
            elif args.action == 'migrate':
                if not args.expected_preview_sha256 or not args.backup:
                    parser.error('migrate requires --expected-preview-sha256 and --backup')
                result = ob_migration.apply(args.database, envelope, expected_preview_sha256=args.expected_preview_sha256,
                                            backup_path=args.backup)
            elif args.action == 'archive':
                if not args.expected_preview_sha256 or not args.backup:
                    parser.error('archive requires --expected-preview-sha256 and --backup')
                result = ob_archive.apply(args.database, envelope, expected_preview_sha256=args.expected_preview_sha256,
                                          backup_path=args.backup)
            else:
                records = ob_backup.typed_records(envelope)
                # Preserve existing 200-item transaction boundary, do not silently drop extras.
                result = {'namespace': envelope['namespace'], 'batches': []}
                for start in range(0, len(records), 200):
                    batch = records[start:start+200]
                    mappings = {key: value for key, value in envelope.get('self_mappings', {}).items()
                                if key in {row['id'] for row in batch}}
                    result['batches'].append(ob_types.preview(args.database, batch, envelope.get('identity_map', {}),
                        namespace=envelope['namespace'], self_mappings=mappings))
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'output': str(output.resolve()), 'status': result.get('status', 'saved')}))


if __name__ == '__main__':
    main()
