"""Continuity chronology uses source records, never a title or import order."""
from datetime import date, datetime, time, timezone

from ..recall.dates import LOCAL_TZ


SOURCE_FIELDS = ('source_ended_at', 'source_started_at', 'local_end_date',
                 'local_end_time', 'local_date', 'local_start_time')


def _stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # Same historical convention as the canonical Event source bounds.
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (TypeError, ValueError):
        return None


def _local(day, clock):
    try:
        parsed_day = date.fromisoformat(str(day))
    except (TypeError, ValueError):
        return None
    try:
        parsed_time = time.fromisoformat(str(clock)) if clock else time.min
        if parsed_time.tzinfo is not None:
            parsed_time = time.min
    except (TypeError, ValueError):
        parsed_time = time.min
    return datetime.combine(parsed_day, parsed_time, LOCAL_TZ)


def event_time(values, created_at):
    stamp = (_stamp(values.get('source_ended_at'))
             or _local(values.get('local_end_date'), values.get('local_end_time'))
             or _stamp(values.get('source_started_at'))
             or _local(values.get('local_date'), values.get('local_start_time')))
    known_source = stamp is not None
    stamp = stamp or _stamp(created_at)
    # Date-unknown records can fill remaining slots, but cannot displace dated
    # source records just because a historical import created them today.
    order = (known_source, stamp.timestamp() if stamp else float('-inf'))
    fields = {'date_basis': 'source_records' if known_source else 'stored_at' if stamp else 'unknown',
              'date': stamp.astimezone(LOCAL_TZ).date().isoformat() if stamp else ''}
    if stamp:
        fields['reference_time'] = stamp.astimezone(LOCAL_TZ).isoformat(timespec='seconds')
    return order, fields


def recent_candidates(store):
    columns = ','.join(f"json_extract(r.metadata_json,'$.{field}') AS {field}" for field in SOURCE_FIELDS)
    rows = store.conn.execute('SELECT d.id,d.created_at,' + columns + ' FROM documents d JOIN revisions r '
        "ON r.document_id=d.id AND r.number=d.revision WHERE d.kind='event' AND d.lifecycle='active'")
    candidates = []
    for row in rows:
        order, fields = event_time(dict(row), row['created_at'])
        candidates.append(((*order, row['id']), row['id'], fields))
    return sorted(candidates, reverse=True)
