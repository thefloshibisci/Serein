"""Time metadata for memory cards; source-message bounds are not event occurrence claims."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo('Asia/Shanghai')


def _source_time(value):
    if not value:
        return ''
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return ''
    # Bridge's original naive message timestamps are UTC, as in FactEventStore.
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(LOCAL_TZ).isoformat(timespec='seconds')


def memory_dates(document, kind):
    meta = document['metadata']
    if meta.get('memory_role') == 'feel':
        # Import time is not the time a historical feeling was authored.
        return {key: meta[key] for key in ('authored_at', 'imported_at') if meta.get(key)}
    if kind == 'event':
        start = _source_time(meta.get('source_started_at'))
        end = _source_time(meta.get('source_ended_at'))
        day = str(meta.get('local_date') or start[:10] or '')
        end_day = str(meta.get('local_end_date') or end[:10] or '')
        values = {'date': day, 'end_date': end_day,
                  'date_basis': 'source_messages' if day or start or end else '',
                  'started_at': start, 'ended_at': end}
        # Imported records may retain only local day/minute precision.
        if not start and day and meta.get('local_start_time'):
            values['local_start'] = f"{day} {meta['local_start_time']}"
        if not end and end_day and meta.get('local_end_time'):
            values['local_end'] = f"{end_day} {meta['local_end_time']}"
        if any(values.get(k) for k in ('started_at', 'ended_at', 'local_start', 'local_end')):
            values['timezone'] = 'Asia/Shanghai'
    else:
        day = str(meta.get('date') or meta.get('event_date') or '')
        values = {'date': day, 'date_basis': 'memory_date' if day else ''}
    if not any(values.get(k) for k in ('date', 'started_at', 'ended_at')):
        values = {'recorded_at': str(meta.get('created') or meta.get('created_at') or document.get('created_at') or '')}
    return {k: v for k, v in values.items() if v}


def date_lines(card):
    return [f'{key}: {card[key]}' for key in
            ('date', 'end_date', 'date_basis', 'started_at', 'ended_at',
             'local_start', 'local_end', 'timezone', 'recorded_at', 'authored_at', 'imported_at') if card.get(key)]
