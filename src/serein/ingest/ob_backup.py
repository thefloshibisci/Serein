"""Lossless, offline adapter for explicit OB ZIP backups; archive text is data."""
import base64
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import stat
from zoneinfo import ZoneInfo
import zipfile

import yaml

from ..core.store import digest, encode


MAX_BYTES = 512 * 1024 * 1024
FRONTMATTER = re.compile(rb'\A(?:\xef\xbb\xbf)?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)', re.S)


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('Legacy YAML object keys must be strings')
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        # Reject NaN/infinity instead of emitting non-JSON review envelopes.
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError('Unsupported YAML value; retain original ZIP for review')


def parse_bucket(raw, path):
    match = FRONTMATTER.match(raw)
    if not match:
        raise ValueError('Bucket requires exact YAML frontmatter: ' + path)
    metadata = _json_value(yaml.safe_load(match[1].decode('utf-8')))
    if not isinstance(metadata, dict) or not isinstance(metadata.get('id'), str) or not metadata['id'].strip():
        raise ValueError('Bucket requires a stable string ID: ' + path)
    body = raw[match.end():].decode('utf-8')
    return {'id': metadata['id'], 'content': body, 'metadata': metadata,
            'backup_member': path, 'backup_member_sha256': digest(raw),
            'body_sha256': digest(body), 'ob_reader_body_sha256': digest(body.strip()),
            'original_file_b64': base64.b64encode(raw).decode('ascii')}


def erased(metadata):
    return bool(metadata.get('tombstone') or metadata.get('deleted_at') or metadata.get('deleted')
                or metadata.get('erasure_mode') or metadata.get('lifecycle') in ('deleted', 'superseded'))


def is_self(metadata):
    from .ob_self import is_self_record
    return (str(metadata.get('type', '')).lower() == 'i'
            or str(metadata.get('source_tool', '')).lower() == 'i'
            or is_self_record({'metadata': metadata}))


def genre(record):
    meta = record['metadata']
    if erased(meta):
        return 'erased'
    if meta.get('type') == 'letter' or meta.get('source_tool') == 'letter' or '__letter__' in (meta.get('tags') or []):
        return 'letter'
    if meta.get('type') in ('feel', 'plan'):
        return meta['type']
    if is_self(meta):
        return 'self'
    return 'memory'


def authored_time(record, timezone_policy):
    original = record['metadata'].get('created')
    result = {'original': original, 'value': None, 'basis': 'unknown'}
    if not original:
        return result
    try:
        parsed = datetime.fromisoformat(str(original).replace('Z', '+00:00'))
    except ValueError:
        return {**result, 'basis': 'invalid_original_time'}
    if parsed.tzinfo is not None:
        return {**result, 'value': str(original), 'basis': 'original_explicit_offset'}
    if not timezone_policy:
        return {**result, 'basis': 'missing_source_timezone'}
    zone = timezone_policy.get('zone')
    evidence = timezone_policy.get('evidence')
    if not isinstance(evidence, str) or not evidence.strip() or not isinstance(zone, str):
        raise ValueError('Naive timestamp interpretation requires source timezone and evidence')
    return {**result, 'value': parsed.replace(tzinfo=ZoneInfo(zone)).isoformat(),
            'basis': 'inferred_source_timezone' if timezone_policy.get('confidence') == 'inferred' else 'reviewed_source_timezone',
            'zone': zone, 'evidence': evidence}


def _module(raw, module):
    """Inspect fixed known tables only, never execute SQL supplied by a backup."""
    conn = sqlite3.connect(':memory:')
    try:
        conn.deserialize(raw)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        state = [dict(row) for row in conn.execute('SELECT * FROM module_state')]
        if len(state) != 1:
            raise ValueError('Module requires one retained scope state')
        result = {'module': module, 'state': state[0], 'claims': [dict(row) for row in conn.execute('SELECT * FROM claims')]}
        if module == 'you':
            result['projections'] = [dict(row) for row in conn.execute('SELECT * FROM projections')]
        else:
            result['persons'] = [dict(row) for row in conn.execute('SELECT * FROM persons')]
        return result
    finally:
        conn.close()


def read_backup(path, *, namespace, timezone_policy=None):
    if not isinstance(namespace, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', namespace):
        raise ValueError('Use an explicit stable source namespace')
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Backup ZIP exceeds 512 MiB')
    original = path.read_bytes()
    records, members, modules = [], [], []
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 10000 or sum(info.file_size for info in infos) > MAX_BYTES:
            raise ValueError('Backup exceeds the offline member/size limits')
        names = set()
        for info in infos:
            name = info.orig_filename
            if name != info.filename:
                raise ValueError('Normalized or unsafe backup member name')
            parts = PurePosixPath(name)
            if (name in names or parts.is_absolute() or '..' in parts.parts or '\\' in name
                    or PureWindowsPath(name).drive or ':' in name
                    or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                raise ValueError('Duplicate, linked, encrypted or unsafe backup member')
            names.add(name)
            if info.is_dir():
                continue
            raw = archive.read(info)
            members.append({'path': name, 'bytes': len(raw), 'sha256': digest(raw)})
            if name.startswith('buckets/') and name.endswith('.md') and not name.startswith('buckets/_app/'):
                record = parse_bucket(raw, name)
                record['genre'] = genre(record)
                record['authored_time'] = authored_time(record, timezone_policy)
                records.append(record)
            elif name in ('you/you.sqlite3', 'them/them.sqlite3'):
                modules.append(_module(raw, name.partition('/')[0]))
        if len({record['id'] for record in records}) != len(records):
            raise ValueError('Duplicate old IDs require explicit review; no automatic merge')
        meta = json.loads(archive.read('export_meta.json')) if 'export_meta.json' in names else {}
        if 'backup_manifest.json' in names:
            manifest = json.loads(archive.read('backup_manifest.json'))
            expected = {row['path']: (row['size'], row['sha256']) for row in manifest['files']}
            actual = {row['path']: (row['bytes'], row['sha256']) for row in members if row['path'] != 'backup_manifest.json'}
            if expected != actual or manifest['file_count'] != len(actual):
                raise ValueError('Backup manifest does not match exact ZIP members')
    result = {'format': 'ob-backup-review-v1', 'namespace': namespace, 'backup_sha256': digest(original),
              'prepared_at': datetime.now(timezone.utc).isoformat(),
              'backup_bytes': len(original), 'backup_b64': base64.b64encode(original).decode('ascii'),
              'source_version': meta.get('version'), 'exported_at_original': meta.get('exported_at'),
              'members': members, 'records': records, 'modules': modules,
              'timezone_policy': timezone_policy, 'identity_map': {}, 'self_mappings': {},
              'scope_bindings': {}}
    result['envelope_sha256'] = envelope_digest(result)
    return result


def envelope_digest(envelope):
    return digest(encode({key: value for key, value in envelope.items() if key != 'envelope_sha256'}))


def validate_envelope(envelope):
    """Only explicit mapping/policy decisions may differ from the frozen ZIP."""
    if envelope.get('format') != 'ob-backup-review-v1' or envelope.get('envelope_sha256') != envelope_digest(envelope):
        raise ValueError('Reviewed archive envelope hash does not match')
    raw = base64.b64decode(envelope['backup_b64'], validate=True)
    if len(raw) != envelope['backup_bytes'] or digest(raw) != envelope['backup_sha256']:
        raise ValueError('Original ZIP hash does not match')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        originals = {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}
    expected_buckets = {path for path in originals if path.startswith('buckets/') and path.endswith('.md')
                        and not path.startswith('buckets/_app/')}
    if expected_buckets != {row['backup_member'] for row in envelope['records']}:
        raise ValueError('Archive review must retain every original bucket')
    seen = set()
    for record in envelope['records']:
        if record['id'] in seen:
            raise ValueError('Duplicate archive identities')
        seen.add(record['id'])
        original_record = base64.b64decode(record['original_file_b64'], validate=True)
        if originals.get(record['backup_member']) != original_record:
            raise ValueError('Record differs from original ZIP member')
        parsed = parse_bucket(original_record, record['backup_member'])
        if any(record[key] != value for key, value in parsed.items()):
            raise ValueError('Record differs from its immutable YAML/body original')
        if record['genre'] != genre(record) or record['authored_time'] != authored_time(record, envelope.get('timezone_policy')):
            raise ValueError('Record classification/time differs from the reviewed source policy')
    expected_modules = [_module(originals[path], path.partition('/')[0]) for path in
                        ('you/you.sqlite3', 'them/them.sqlite3') if path in originals]
    if expected_modules != envelope['modules']:
        raise ValueError('Module catalog differs from immutable original databases')
    bindings = envelope.get('scope_bindings', {})
    if not isinstance(bindings, dict) or set(bindings) - {'you', 'them'}:
        raise ValueError('Unknown OB module scope bindings')
    for values in bindings.values():
        if not isinstance(values, list) or any(not isinstance(value, dict)
                or set(value) != {'source_scope_key', 'target', 'evidence'}
                or value.get('target') != 'instance_ai' or not str(value.get('source_scope_key', '')).strip()
                or not str(value.get('evidence', '')).strip() for value in values):
            raise ValueError('Scope binding requires exact source scope, instance AI target and review evidence')
    return raw


def typed_records(envelope):
    """Select supported authored records without cleaning or promoting ordinary memories."""
    selected = []
    for record in envelope['records']:
        if record['genre'] not in ('feel', 'plan', 'self'):
            continue
        selected.append({**record, 'authored_at': record['authored_time']['value'],
                         'archived': record['backup_member'].startswith('buckets/archive/')})
    return selected
